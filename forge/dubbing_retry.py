"""Atomic retries of persistent Dubbing graphs without replaying verified work."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import time
import uuid

STOPPED = {'failed', 'cancelled', 'interrupted', 'blocked'}
ACTIVE = {'running', 'cancelling', 'finalizing'}
PENDING = {'queued', 'blocked'}


def run_id(job):
    payload = job['payload']
    return payload.get('options', {}).get('run_id') or payload.get('dubbing', {}).get('run_id') or ''


def _source_unchanged(job):
    """Retries may retain source identity, never silently adopt replacement bytes."""
    payload = job['payload']
    source = payload.get('source')
    if not source:
        return
    path = Path(source.get('path', ''))
    if not path.is_file():
        raise ValueError('The original dubbing source is unavailable. Restore it or import a new project.')
    expected = payload.get('source_stat') or source
    stat = path.stat()
    if any(key not in expected for key in ('size', 'mtime_ns')) or any(
            expected[key] != getattr(stat, 'st_' + key) for key in ('size', 'mtime_ns')):
        raise ValueError('The original dubbing source changed. Import it as a new project; saved work is retained.')


def retry_selection(manager, originals, *, validate_only=False, batch=False):
    """Called under QueueManager.lock; all validation precedes a single savepoint.

    Failed rows remain immutable history. Production run IDs and their frozen
    inputs survive; only current attempt IDs and pending dependency edges change.
    Run-less installation chains retain their original approved installation plan.
    """
    store = manager.store
    if not originals:
        return []
    if not all(job['tool'].startswith('dubbing_') for job in originals):
        raise ValueError('Retry Dubbing workflows separately from unrelated queue tools.')
    manager.check_accepting()
    with store.lock:
        all_jobs = {job['id']: job for job in store.jobs(limit=100000)}
        try:
            selected_jobs = [all_jobs[job['id']] for job in originals]
        except KeyError:
            raise ValueError('A selected dubbing record no longer exists.') from None
        runs = {}
        for identity in {run_id(job) for job in selected_jobs} - {''}:
            if manager.dubbing is None:
                raise ValueError('Open Dubbing Studio before retrying a saved production workflow.')
            value = manager.dubbing.library.run(identity)
            current = manager.dubbing.library._project(value['project_id'])
            if current.get('source') != value.get('snapshot', {}).get('source'):
                raise ValueError('The saved dubbing source identity changed. Review a new workflow before retrying.')
            if any(job_id not in all_jobs for job_id in value.get('jobs', [])):
                raise ValueError('The dubbing workflow is incomplete. Restore its queue records before retrying.')
            runs[identity] = copy.deepcopy(value)
        # Old failed attempts share a batch with their replacements. Only the
        # canonical run/installation heads may be retried, never both histories.
        superseded = {job['payload'].get('retry_of') for job in all_jobs.values()
                      if job['tool'].startswith('dubbing_') and job['payload'].get('retry_of')}
        canonical = set()
        for job in selected_jobs:
            identity = run_id(job)
            if identity:
                if job['id'] in runs[identity]['jobs']:
                    canonical.add(job['id'])
            elif job['id'] not in superseded:
                canonical.add(job['id'])
        if batch:
            selected_jobs = [job for job in selected_jobs if job['id'] in canonical and job['status'] != 'completed'
                             and job['status'] != 'queued']
        elif any(job['id'] not in canonical for job in selected_jobs):
            raise ValueError('This dubbing attempt was already replaced. Retry its current attempt instead.')
        if not selected_jobs:
            return []
        if any(job['status'] not in STOPPED or job['id'] in manager.active for job in selected_jobs):
            raise ValueError('Wait until selected dubbing stages stop; completed stages are retained.')
        # Mutating a running workflow's topology races its worker publication.
        scope = {job_id for value in runs.values() for job_id in value['jobs']}
        scope.update(job['id'] for job in selected_jobs if not run_id(job))
        if any(all_jobs[job_id]['status'] in ACTIVE or job_id in manager.active for job_id in scope):
            raise ValueError('Wait until this dubbing workflow stops before retrying its failed stages.')
        selected = {job['id'] for job in selected_jobs}
        latest = {job['payload'].get('retry_of'): job['id'] for job in all_jobs.values()
                  if job['tool'].startswith('dubbing_') and job['payload'].get('retry_of')}
        def current_parent(identity):
            seen = set()
            while identity in latest:
                if identity in seen:
                    raise ValueError('The dubbing retry history contains a dependency cycle.')
                seen.add(identity); identity = latest[identity]
            return identity
        planned, seen = [], set()
        pending = sorted(selected_jobs, key=lambda job: (job['created'], job['id']))
        while pending:
            ready = [job for job in pending if current_parent(job['payload'].get('depends_on', '')) not in selected
                     or current_parent(job['payload'].get('depends_on', '')) in seen]
            if not ready:
                raise ValueError('The dubbing dependency graph contains a cycle.')
            for job in ready:
                _source_unchanged(job)
                payload = copy.deepcopy(job['payload'])
                parent = current_parent(payload.get('depends_on', ''))
                if parent and parent not in selected:
                    if parent not in all_jobs:
                        raise ValueError('A dubbing parent record is missing. Restore its workflow before retrying.')
                    if all_jobs[parent]['status'] != 'completed':
                        raise ValueError('Retry the unfinished dubbing parent too, or retry the saved workflow.')
                payload['depends_on'] = parent
                payload.pop('resolved_parent', None)
                planned.append((job, payload)); seen.add(job['id']); pending.remove(job)
        # Rebind descendants still waiting on the failed attempt. Their identity
        # stays stable, so an existing review gate is never cloned or bypassed.
        descendants = {}
        parents = set(selected)
        while True:
            added = False
            for job in all_jobs.values():
                if job['id'] in selected or job['id'] in descendants:
                    continue
                if job['payload'].get('depends_on') not in parents:
                    continue
                if job['status'] in ACTIVE or job['id'] in manager.active:
                    raise ValueError('A dependent dubbing stage is active. Wait until it stops before retrying.')
                if job['status'] not in PENDING:
                    continue
                if not job['tool'].startswith('dubbing_') or run_id(job) not in runs and run_id(job):
                    raise ValueError('The failed stage has a dependent outside its saved dubbing workflow.')
                _source_unchanged(job)
                descendants[job['id']] = job; parents.add(job['id']); added = True
            if not added:
                break
        manager.check_features([job for job, _ in planned] + list(descendants.values()))
        if validate_only:
            return planned
        now = time.time()
        mapping = {job['id']: uuid.uuid4().hex for job, _ in planned}
        savepoint = 'dubbing_retry_' + uuid.uuid4().hex
        store.db.execute('SAVEPOINT ' + savepoint)
        try:
            attempts = []
            for job, payload in planned:
                identity = mapping[job['id']]
                payload.update(job_id=identity, retry_of=job['id'],
                               retry_root=payload.get('retry_root') or job['id'], attempt=int(payload.get('attempt', 1)) + 1)
                payload['depends_on'] = mapping.get(payload.get('depends_on'), payload.get('depends_on', ''))
                meta = {key: job[key] for key in ('priority', 'rank', 'resource', 'batch_id', 'workflow_name', 'step', 'steps',
                        'dubbing_project_id', 'dubbing_run_id') if key in job}
                meta.update(depends_on=payload['depends_on'], stage='Awaiting retry', progress_measured=False)
                store.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)', (identity, job['tool'], job['label'],
                    'queued', 0, 'Retry queued; verified completed stages are retained.', json.dumps(payload, ensure_ascii=False), '[]', '[]', now, now))
                store.db.execute('INSERT INTO job_meta VALUES (?,?)', (identity, json.dumps(meta, ensure_ascii=False)))
                attempts.append({'from': job['id'], 'to': identity, 'stage': job['tool'], 'attempt': payload['attempt'], 'time': now})
            for identity, job in descendants.items():
                payload = copy.deepcopy(job['payload'])
                parent = mapping.get(payload.get('depends_on'), payload.get('depends_on', ''))
                payload['depends_on'] = parent
                payload.pop('resolved_parent', None)
                row = store.db.execute('SELECT value FROM job_meta WHERE id=?', (identity,)).fetchone()
                meta = json.loads(row[0]) if row else {}
                meta.update(depends_on=parent, stage='Awaiting dispatch')
                for field in ('error_code', 'error_retryable', 'finished', 'cancel_intent'):
                    meta.pop(field, None)
                store.db.execute("UPDATE jobs SET payload=?,status='queued',message=?,updated=? WHERE id=?", (
                    json.dumps(payload, ensure_ascii=False), 'Waiting for the retried dubbing parent.', now, identity))
                store.db.execute('INSERT OR REPLACE INTO job_meta VALUES (?,?)', (identity, json.dumps(meta, ensure_ascii=False)))
            for identity, value in runs.items():
                value['jobs'] = [mapping.get(job_id, job_id) for job_id in value['jobs']]
                value.setdefault('attempt_history', []).extend(item for item in attempts if run_id(all_jobs[item['from']]) == identity)
                value['status'] = 'queued'
                manager.dubbing.library.save_run(value)
            setup_plans = store.get('dubbing_setup_plans', {})
            changed = False
            for quote in setup_plans.values():
                if any(identity in mapping for identity in quote.get('jobs', [])):
                    old_jobs = set(quote['jobs'])
                    quote['jobs'] = [mapping.get(identity, identity) for identity in quote['jobs']]
                    quote.setdefault('attempt_history', []).extend(item for item in attempts if item['from'] in old_jobs)
                    changed = True
            if changed:
                store.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', ('dubbing_setup_plans', json.dumps(setup_plans, ensure_ascii=False)))
            store.db.execute('RELEASE SAVEPOINT ' + savepoint)
        except BaseException:
            store.db.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
            store.db.execute('RELEASE SAVEPOINT ' + savepoint)
            raise
        return [store.job(mapping[job['id']]) for job, _ in planned]
