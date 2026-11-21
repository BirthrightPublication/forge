"""Startup-only reconciliation of verified, already-computed local results.

This module never starts workers, installs components, resumes an upload, or
changes a queue pause. Call it after constructing DubbingService and before
starting its QueueManager. File reads and hashing deliberately precede the
short SQLite transaction which applies metadata and completes the queue row.
Invalid evidence leaves the original job, project and files unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from .dubbing_store import bounded_json, canonical, fingerprint


LOCAL_STAGES = frozenset({
    'dubbing_transcribe', 'dubbing_translate', 'dubbing_voice', 'dubbing_align',
    'dubbing_separate', 'dubbing_mix', 'dubbing_effects', 'dubbing_lipsync',
    'dubbing_encode', 'dubbing_export', 'dubbing_metadata', 'dubbing_voice_reference',
})
RECOVERABLE = frozenset({'failed', 'interrupted', 'completed'})
IDENTITY = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')
MAX_MANIFEST = 32 * 1024 * 1024
MAX_FILES = 10000
RECEIPT_PREFIX = 'dubbing_recovery_applied:'


class InvalidEvidence(ValueError):
    """Durable output evidence is absent, changed, or outside its boundary."""


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _local_path(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise InvalidEvidence('A local evidence path is missing.')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or value.startswith(('//', '\\\\')):
        raise InvalidEvidence('Evidence needs an absolute local path without traversal.')
    return path


def _safe_stat(path, *, directory=False):
    """Reject links in every path component, including Windows junctions."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        value = current.lstat()
        if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            raise InvalidEvidence('Recovery does not follow symbolic links or reparse points.')
        if current != path and not stat.S_ISDIR(value.st_mode):
            raise InvalidEvidence('An evidence parent is not a directory.')
    value = path.lstat()
    if not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)):
        raise InvalidEvidence('Evidence is not a completed regular file.' if not directory else 'The output destination is not a directory.')
    return value


def _read_verified(path, *, expected=None, size=None, maximum=None):
    before = _safe_stat(path)
    if maximum is not None and before.st_size > maximum:
        raise InvalidEvidence('The durable manifest exceeds the recovery bound.')
    if size is not None and (type(size) is not int or size < 0 or size != before.st_size):
        raise InvalidEvidence('An output size no longer matches its durable evidence.')
    digest = hashlib.sha256()
    chunks = [] if maximum is not None else None
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as handle:
        if _stamp(os.fstat(handle.fileno())) != _stamp(before):
            raise InvalidEvidence('An evidence file changed before verification.')
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
                if sum(map(len, chunks)) > maximum:
                    raise InvalidEvidence('The durable manifest grew beyond the recovery bound.')
        if _stamp(os.fstat(handle.fileno())) != _stamp(before):
            raise InvalidEvidence('An evidence file changed during verification.')
    if _stamp(_safe_stat(path)) != _stamp(before):
        raise InvalidEvidence('An evidence path changed during verification.')
    actual = digest.hexdigest()
    if expected is not None and (not isinstance(expected, str) or not SHA256.fullmatch(expected) or expected != actual):
        raise InvalidEvidence('An output failed its saved SHA-256 check.')
    return actual, _stamp(before), b''.join(chunks) if chunks is not None else None


def _pause_reason(store, job):
    if job.get('cancel_intent') or job.get('status') in {'cancelled', 'cancelling'}:
        return 'Explicit cancellation was preserved.'
    if job.get('paused') or job.get('pause_intent'):
        return 'Explicit job pause was preserved.'
    if store.settings().get('queue_paused'):
        restart = store.get('queue_restart_pause', {})
        if not isinstance(restart, dict) or restart.get('automatic') is not True:
            return 'Explicit queue pause was preserved.'
    return ''


def _applied(service, job):
    if service.store.get(RECEIPT_PREFIX + job['id']):
        return True
    context = job.get('payload', {}).get('dubbing') or {}
    run_id = context.get('run_id') or job.get('payload', {}).get('options', {}).get('run_id')
    if run_id:
        try:
            return service.library.run(run_id).get('results', {}).get(job['tool'], {}).get('job_id') == job['id']
        except ValueError:
            return False
    return False


def _verify(job, data):
    identity = job['id']
    if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
        raise InvalidEvidence('The queue record has an invalid local identity.')
    payload = job.get('payload') or {}
    if payload.get('tool') not in (None, job['tool']) or payload.get('job_id') not in (None, identity):
        raise InvalidEvidence('The durable result does not belong to this queue record.')
    destination = _local_path(payload.get('destination'))
    _safe_stat(destination, directory=True)
    manifest = data / 'work' / identity / 'result.json'
    manifest_digest, manifest_stamp, raw = _read_verified(manifest, maximum=MAX_MANIFEST)
    try:
        result = json.loads(raw.decode('utf-8'), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise InvalidEvidence('The durable manifest is not bounded valid JSON.') from error
    if not isinstance(result, dict) or result.get('complete') is not True:
        raise InvalidEvidence('The durable result is not explicitly complete.')
    if 'publishing_result' in result:
        raise InvalidEvidence('Publishing results require their own explicit recovery process.')
    meta = result.get('dubbing_result')
    if not isinstance(meta, dict) or not meta:
        raise InvalidEvidence('The completed manifest has no dubbing result metadata.')
    bounded_json(meta, MAX_MANIFEST)
    if meta.get('stage') not in (None, job['tool']):
        raise InvalidEvidence('The durable result stage differs from the queue record.')
    context = payload.get('dubbing') or {}
    project = context.get('project') or {}
    project_id, run_id = project.get('id'), context.get('run_id') or payload.get('options', {}).get('run_id')
    if meta.get('project_id') and meta['project_id'] != project_id:
        raise InvalidEvidence('The durable result project differs from its frozen snapshot.')
    if meta.get('run_id') and meta['run_id'] != run_id:
        raise InvalidEvidence('The durable result run differs from its frozen snapshot.')
    if job['tool'] != 'dubbing_voice_reference' and (not project_id or not run_id):
        raise InvalidEvidence('The completed local stage needs its saved project and run.')
    outputs, artifacts = result.get('outputs'), meta.get('artifacts', [])
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= MAX_FILES:
        raise InvalidEvidence('The completed stage needs bounded published outputs.')
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= MAX_FILES:
        raise InvalidEvidence('The completed stage needs bounded verified artifact descriptors.')
    entries, descriptors = {}, {}
    for item in outputs:
        if not isinstance(item, dict):
            raise InvalidEvidence('A published output descriptor is invalid.')
        path = _local_path(item.get('path'))
        if not path.is_relative_to(destination) or path == destination or str(path) in entries:
            raise InvalidEvidence('A published output is duplicated or outside its destination.')
        entries[str(path)] = item
    for item in artifacts:
        if not isinstance(item, dict):
            raise InvalidEvidence('An artifact descriptor is invalid.')
        path = _local_path(item.get('path'))
        key = str(path)
        if key not in entries or not path.is_relative_to(destination):
            raise InvalidEvidence('An artifact is not an owned published destination output.')
        if not isinstance(item.get('sha256'), str) or not SHA256.fullmatch(item['sha256']):
            raise InvalidEvidence('Every recovered artifact needs a saved SHA-256 digest.')
        if key in descriptors and descriptors[key]['sha256'] != item['sha256']:
            raise InvalidEvidence('Artifact descriptors disagree about their output digest.')
        descriptors[key] = item
    stamps = {str(manifest): manifest_stamp}
    verified_outputs = []
    for key, item in entries.items():
        saved = descriptors.get(key, {})
        digest = item.get('sha256') or saved.get('sha256')
        if not digest:
            raise InvalidEvidence('An output has no durable SHA-256 evidence; retry it explicitly.')
        if saved.get('sha256') and saved['sha256'] != digest:
            raise InvalidEvidence('Output and artifact digests disagree.')
        size = item.get('size')
        if type(size) is not int or size < 0:
            raise InvalidEvidence('A published output has no valid saved size.')
        if 'size' in saved and saved['size'] != size:
            raise InvalidEvidence('Output and artifact sizes disagree.')
        actual, stamps[key], _ = _read_verified(Path(key), expected=digest, size=size)
        verified_outputs.append({**copy.deepcopy(item), 'sha256': actual})

    # State fields may reference verified new files or the unchanged source
    # snapshot, but must not smuggle another arbitrary local path into a project.
    source = payload.get('source') or project.get('source') or {}
    source_path = source.get('path')

    def check_paths(value, *, original=False):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == 'path':
                    if not isinstance(child, str) or (child not in entries and not (original and child == source_path)):
                        raise InvalidEvidence('Result metadata references an unverified output path.')
                else:
                    check_paths(child, original=original or key in {'source_audio', 'original_audio_reference', 'original_reference'})
        elif isinstance(value, list):
            for child in value:
                check_paths(child, original=original)

    for key in ('segment_updates', 'project_updates', 'voice_profile_update'):
        check_paths(meta.get(key, {}))
    warnings = result.get('warnings', [])
    if not isinstance(warnings, list) or len(warnings) > 1000 or any(not isinstance(item, str) or len(item) > 10000 for item in warnings):
        raise InvalidEvidence('The durable warnings are invalid or oversized.')
    result['outputs'] = verified_outputs
    return result, manifest_digest, stamps


def recover_completed_results(service, *, limit=10000):
    """Reconcile durable local completions before dispatch; never run a job.

    Automatic crash-safety pauses permit verification but remain set. A user
    pause/cancel always wins. Existing applied results are not applied twice.
    External cache artifacts intentionally fail closed: explicit retry can use
    the existing worker's cache verifier without broadening recovery ownership.
    """
    store, manager, library = service.store, service.manager, service.library
    report = {'recovered': [], 'already_applied': [], 'skipped': [], 'invalid': []}
    scheduler = getattr(manager, 'scheduler', None)
    if getattr(manager, 'active', {}) or (scheduler is not None and scheduler.is_alive()):
        raise RuntimeError('Dubbing recovery must finish before queue dispatch starts.')
    if type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError('Choose a bounded startup recovery limit.')
    snapshots = store.jobs(limit=limit)
    superseded = {item.get('payload', {}).get('retry_of') for item in snapshots
                  if item.get('tool') in LOCAL_STAGES and item.get('payload', {}).get('retry_of')}
    for job in snapshots:
        if job.get('tool') not in LOCAL_STAGES:
            continue
        reason = _pause_reason(store, job)
        if reason or job.get('status') not in RECOVERABLE:
            report['skipped'].append({'job_id': job['id'], 'reason': reason or 'This queue state is not recoverable.'})
            continue
        if job['id'] in superseded:
            report['skipped'].append({'job_id': job['id'], 'reason': 'A newer retry attempt owns this work; its history was preserved.'})
            continue
        if job['status'] == 'completed' and _applied(service, job):
            report['already_applied'].append(job['id'])
            continue
        try:
            result, manifest_digest, stamps = _verify(job, store.data)
            # Keep these final filesystem checks outside both queue/SQLite locks.
            for path, expected in stamps.items():
                if _stamp(_safe_stat(Path(path))) != expected:
                    raise InvalidEvidence('Verified evidence changed before metadata application.')
            with manager.lock, library.transaction():
                current = store.job(job['id'])
                reason = _pause_reason(store, current)
                if reason or current['status'] not in RECOVERABLE or fingerprint(current['payload']) != fingerprint(job['payload']):
                    report['skipped'].append({'job_id': job['id'], 'reason': reason or 'The queue record changed during recovery.'})
                    continue
                applied = _applied(service, current)
                if not applied:
                    if job['tool'] != 'dubbing_voice_reference':
                        context = current['payload']['dubbing']
                        library.project(context['project']['id'])
                        run = library.run(context.get('run_id') or current['payload']['options']['run_id'])
                        if run.get('status') in {'cancelled', 'cancelling', 'paused'} or run.get('pause_intent') or run.get('cancel_intent'):
                            report['skipped'].append({'job_id': current['id'], 'reason': 'Explicit workflow pause or cancellation was preserved.'})
                            continue
                        if current['id'] not in run.get('jobs', []):
                            report['skipped'].append({'job_id': current['id'], 'reason': 'The saved workflow owns a different current attempt.'})
                            continue
                    library.apply_result(current, result)
                now = time.time()
                store.db.execute('UPDATE jobs SET status=?,progress=?,message=?,outputs=?,warnings=?,updated=? WHERE id=?',
                    ('completed', 100, 'Completed · verified recovery' + (' · review notes' if result.get('warnings') else ''),
                     canonical(result['outputs']), canonical(result.get('warnings', [])), now, current['id']))
                row = store.db.execute('SELECT value FROM job_meta WHERE id=?', (current['id'],)).fetchone()
                meta = json.loads(row[0]) if row else {}
                meta.update(finished=meta.get('finished') or now, stage='Finished', eta_seconds=None, speed='', progress_measured=False)
                meta.pop('error_code', None)
                meta.pop('error_retryable', None)
                elapsed = result['dubbing_result'].get('elapsed_seconds')
                if not meta.get('execution_seconds') and type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
                    meta['execution_seconds'] = elapsed
                store.db.execute('INSERT OR REPLACE INTO job_meta VALUES (?,?)', (current['id'], canonical(meta)))
                store.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (RECEIPT_PREFIX + current['id'],
                    canonical({'manifest_sha256': manifest_digest, 'recovered_at': now, 'tool': current['tool']})))
            report['already_applied' if applied else 'recovered'].append(current['id'])
        except Exception as error:
            report['invalid'].append({'job_id': job['id'], 'reason': str(error)[:600]})
    return report
