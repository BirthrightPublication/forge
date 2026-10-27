from __future__ import annotations
import contextlib
import copy
import uuid
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
import psutil
from .core import ROOT, Store, no_window
from .resources import MEDIA_ANALYSIS, ResourceMonitor, admission_reason, admission_state, primary_resource, required_resources
from .timing import JobClock

TERMINAL = {'failed', 'cancelled', 'interrupted', 'completed', 'blocked'}


def public_job(job):
    """Expose useful processing choices without QR contents, cookies or prompts."""
    result = {key: value for key, value in job.items() if key != 'payload'}
    options = job.get('payload', {}).get('options', {})
    fields = {'model', 'format', 'quality', 'max_height', 'device', 'language', 'timestamps',
              'formats', 'mode', 'encoder', 'reports', 'preset', 'reencode',
              'report_json', 'report_html', 'retain_reference', 'sample_frames',
              'max_seconds', 'placement'}
    summary = {key: value for key, value in options.items() if key in fields
               and (isinstance(value, (str, int, float, bool)) or (key == 'formats' and isinstance(value, list)))}
    if job['tool'].startswith('download'):
        summary['method'] = 'yt-dlp'
    result.update(options_summary=summary, resources=sorted(required_resources(job)),
                  progress_measured=job.get('progress_measured') is True)
    return result

class QueueManager:
    def __init__(self, store: Store):
        self.store = store
        self.lock = threading.RLock()
        self.maintenance = False
        self.maintenance_pending = False
        self.repair_pending = False
        self.require_feature = None  # Optional server-owned entitlement boundary.
        self.download_planner = None
        self.active: dict[str, subprocess.Popen] = {}
        self.threads: list[threading.Thread] = []
        self.stop_event = threading.Event()
        self.monitor = ResourceMonitor()
        self.scheduler = threading.Thread(target=self.loop, name='forge-scheduler', daemon=True)

    def check_accepting(self):
        if self.maintenance or self.maintenance_pending:
            raise ValueError("FORGE is preparing an application update. New jobs are temporarily blocked.")

    def start(self):
        self.scheduler.start()

    def loop(self):
        while not self.stop_event.wait(0.35):
            try:
                self.dispatch_once()
            except Exception:
                # Preserve the service and show an actionable queue error rather than silently dying.
                self.store.set('queue_error', 'The dispatcher encountered an error. Restart FORGE and review interrupted jobs.')

    def dispatch_once(self):
        # Verification may hash/probe files, so it stays outside scheduler locks.
        if self.download_planner is not None:
            self.download_planner.reconcile()
        metrics = self.monitor.sample(self.store.data)
        with self.lock:
            settings = self.store.settings()
            if settings['queue_paused'] or self.maintenance or self.repair_pending:
                return
            for job in self.store.jobs(status='queued', limit=100000):
                if self.stop_event.is_set():
                    break
                if not self.prepare_dependency(job):
                    continue
                if job['tool'] == 'transcribe' and job['payload'].get('options',{}).get('engine','whisper')=='whisper':
                    from .cuda_health import recent_failure
                    failure = recent_failure(self.store.data)
                    device = job['payload'].get('options', {}).get('device', 'auto')
                    if failure and device == 'cuda':
                        self.store.update_job(job['id'], status='blocked', message='CUDA needs repair: ' + failure + '. Run GPU diagnostics in Updates, or select CPU and retry.')
                        continue
                    resolved = 'cpu' if failure and device == 'auto' else device
                    job['payload']['resolved_device'] = resolved
                    self.store.update_payload(job['id'], job['payload'])
                active_jobs = [self.store.job(id) for id in self.active]
                state = self.capacity_state(settings, active_jobs, metrics)
                reason = admission_reason(job, active_jobs, state)
                if reason:
                    if job['message'] != reason:
                        self.store.update_job(job['id'], message=reason)
                    continue
                # Dependency resolution may have refreshed the worker's source.
                self.launch(self.store.job(job['id']))
            if self.download_planner is not None and not self.maintenance_pending:
                # There is no second waiting execution queue. A plan obtains a
                # processing record only at resource admission, then launches
                # before this lock is released. Active jobs are never preempted.
                for _ in range(5):
                    if self.stop_event.is_set():
                        break
                    def can_admit(candidate):
                        current = [self.store.job(id) for id in self.active]
                        capacity = self.capacity_state(settings, current, metrics)
                        return not admission_reason(candidate, current, capacity)
                    selected = self.download_planner.admit_one(can_admit)
                    if selected is None:
                        break
                    self.launch(selected)
            self.threads = [thread for thread in self.threads if thread.is_alive()]
            if self.store.get('queue_error'):
                self.store.set('queue_error', None)

    def capacity_state(self, settings, active_jobs, metrics):
        state = admission_state(settings, active_jobs, metrics)
        checks = getattr(self.download_planner, 'verification_count', 0) if self.download_planner is not None else 0
        reservations = getattr(self.download_planner, 'verification_reservations', checks) if self.download_planner is not None else 0
        state['usage']['disk'] += reservations
        state['media_verification_count'] = checks
        return state

    def status(self):
        metrics = self.monitor.sample(self.store.data)
        with self.lock:
            settings = self.store.settings()
            active_jobs = [self.store.job(id) for id in self.active]
            state = self.capacity_state(settings, active_jobs, metrics)
            counts = dict.fromkeys(('queued', 'running', 'finalizing', 'cancelling', 'failed', 'blocked', 'interrupted', 'cancelled', 'completed'), 0)
            with self.store.lock:
                counts.update({row[0]: row[1] for row in self.store.db.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status')})
            state.update(paused=settings['queue_paused'], counts=counts,
                         running_count=len(active_jobs), queued_count=counts['queued'],
                         failed_count=counts['failed'] + counts['blocked'] + counts['interrupted'],
                         active_jobs=[public_job(job) for job in active_jobs])
            return state

    @staticmethod
    def resource(job):
        resource = primary_resource(job)
        return 'download' if resource == 'network' else resource

    def prepare_dependency(self, job):
        parent_id=job['payload'].get('depends_on')
        if not parent_id:
            return True
        try:
            parent=self.store.job(parent_id)
        except ValueError:
            self.store.update_job(job['id'], status='blocked', message='The previous Black Scripting step is missing. Rebuild this script before retrying.')
            return False
        if parent['status'] in {'failed','cancelled','interrupted','blocked'}:
            self.store.update_job(job['id'],status='blocked',message='A previous Black Scripting step did not complete. Retry the script group after reviewing that step.')
            return False
        if parent['status']!='completed':
            return False
        from .core import TOOLS
        parent_payload = parent['payload']
        if parent['tool'] in MEDIA_ANALYSIS:
            # Inspection produces optional private reports, never replacement
            # media. Preserve the inspected input without copying large files.
            source = parent_payload.get('source') or {}
            candidates = [(Path(source['path']), parent_payload.get('source_stat'))] if source.get('path') else []
        else:
            candidates = [(Path(output['path']), output.get('source_stat'))
                          for output in parent['outputs']
                          if Path(output['path']).suffix.lower() in TOOLS[job['tool']]['accept']]
        if not candidates or not candidates[0][0].is_file():
            self.store.update_job(job['id'],status='blocked',message='The previous step has no available, compatible media output.')
            return False
        path, expected = candidates[0]
        if path.suffix.lower() not in TOOLS[job['tool']]['accept']:
            self.store.update_job(job['id'], status='blocked', message='The inspected media is incompatible with the next Black Scripting operation.')
            return False
        payload=job['payload']
        stat=path.stat()
        snapshot = {'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
        if expected and snapshot != expected:
            self.store.update_job(job['id'], status='blocked', message='The previous step’s media changed after inspection or publication. Review the source and retry the script group.')
            return False
        already_resolved = payload.get('resolved_parent') == parent_id
        if already_resolved and payload.get('source_stat') != snapshot:
            self.store.update_job(job['id'], status='blocked', message='The dependency media changed while waiting for a worker. Review and retry the script group.')
            return False
        origin = parent_payload.get('original_source') or parent_payload.get('source')
        origin_stat = parent_payload.get('original_source_stat') or parent_payload.get('source_stat')
        if not already_resolved or (payload.get('source') or {}).get('path') != str(path):
            payload['source']=self.store.register(path)
            payload['source_stat']=snapshot
            payload['resolved_parent'] = parent_id
            if origin:
                payload['original_source'] = copy.deepcopy(origin)
                payload['original_source_stat'] = copy.deepcopy(origin_stat)
            self.store.update_payload(job['id'],payload)
        return True

    def launch(self, job: dict):
        clock = JobClock()
        payload = {**job['payload'], 'data': str(self.store.data), 'parent_pid': os.getpid(),
                   'parent_started': psutil.Process().create_time()}
        env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1', 'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1'}
        # pythonw has redirected pipes; use python.exe for worker diagnostics on Windows.
        python = str(Path(sys.executable).with_name('python.exe')) if os.name == 'nt' else sys.executable
        p = None
        try:
            p = subprocess.Popen([python, '-m', 'forge.worker'], cwd=ROOT, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8', errors='replace', **no_window())
            self.active[job['id']] = p
            self.store.update_job(job['id'], status='running', progress=0, message='Starting local worker')
            self.store.update_meta(job['id'],resource=self.resource(job),started=time.time(),stage='Starting',eta_seconds=None,speed='',progress_measured=False)
            p.stdin.write(json.dumps(payload, ensure_ascii=False))
            p.stdin.close()
            t = threading.Thread(target=self.collect, args=(job, p, clock), daemon=True)
            self.threads.append(t)
            t.start()
        except Exception as error:
            if p is not None:
                self.kill(p)
                for stream in (p.stdin, p.stdout, p.stderr):
                    with contextlib.suppress(Exception):
                        stream.close()
            self.active.pop(job['id'], None)
            self.store.update_job(job['id'], status='failed', message='Could not start worker: ' + str(error)[:800])

    def collect(self, job: dict, p: subprocess.Popen, clock=None):
        clock = clock or JobClock()
        id = job['id']
        error = ''
        result = None
        stderr = []
        def drain():
            for line in p.stderr:
                stderr.append(line)
                if len(stderr) > 60:
                    del stderr[:30]
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            for line in p.stdout:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                with self.lock:
                    status = self.store.job(id)['status']
                    if event.get('kind') == 'progress' and status == 'running':
                        clock.enter(str(event.get('stage') or 'Processing')[:200])
                        value = event.get('progress', 0)
                        if type(value) in (int, float) and math.isfinite(value):
                            self.store.update_job(id, progress=max(0, min(99, value)), message=str(event.get('message', ''))[:2000])
                            eta=event.get('eta_seconds')
                            if type(eta) not in (int,float) or not math.isfinite(eta) or eta<0 or eta>10**9: eta=None
                            meta = {'eta_seconds':eta,'speed':str(event.get('speed',''))[:100],
                                    'stage':str(event.get('stage',event.get('message','')))[:200],
                                    'progress_measured':event.get('progress_measured') is True}
                            for field in ('downloaded_bytes','total_bytes','speed_bps','elapsed_seconds','stage_progress','stream_index','stream_count'):
                                if field in {'downloaded_bytes','total_bytes'} and field not in event:
                                    continue
                                number = event.get(field)
                                meta[field] = number if type(number) in (int,float) and math.isfinite(number) and 0 <= number <= 10**18 else None
                            if meta.get('stage_progress') is not None and meta['stage_progress'] > 100:
                                meta['stage_progress'] = None
                            meta['download_speed_bps'] = meta.get('speed_bps')
                            for field in ('transfer_stage','progress_scope'):
                                meta[field] = str(event.get(field) or '')[:100]
                            for field in ('resume_supported','resumed'):
                                if field in event:
                                    meta[field] = event[field] is True
                            self.store.update_meta(id, **meta)
                    elif event.get('kind') == 'finalizing' and status == 'running':
                        clock.enter('Publishing')
                        self.store.update_job(id, status='finalizing', progress=96, message='Publishing verified outputs')
                        self.store.update_meta(id, progress_measured=False)
                    elif event.get('kind') == 'result':
                        result = event
                    elif event.get('kind') == 'error':
                        error = str(event.get('message', 'Processing failed.'))
                        self.store.update_meta(id,error_code=str(event.get('error_code') or '')[:80],
                                               error_retryable=event.get('error_retryable') is True)
            code = p.wait()
            reader.join(timeout=2)
            manifest = self.store.data / 'work' / id / 'result.json'
            if result is None and manifest.is_file():
                with contextlib.suppress(Exception):
                    result = json.loads(manifest.read_text(encoding='utf-8'))
            with self.lock:
                status = self.store.job(id)['status']
                outputs = (result or {}).get('outputs', [])
                warnings = (result or {}).get('warnings', [])
                if status == 'cancelling':
                    self.store.update_job(id, status='cancelled', message='Cancelled. Originals and any already-published outputs were kept.', outputs=outputs, warnings=warnings)
                elif code == 0 and result and result.get('complete'):
                    self.store.update_job(id, status='completed', progress=100, message='Completed' + (' · review notes' if warnings else ''), outputs=outputs, warnings=warnings)
                else:
                    message = error or (''.join(stderr)[-1600:] if stderr else f'Worker stopped (exit {code}). Retry restarts the job.')
                    if job['tool'] == 'transcribe' and any(s in message.lower() for s in ('cublas', 'cudnn', 'no kernel image', 'cuda driver')):
                        from .cuda_health import remember_failure
                        remember_failure(self.store.data, message)
                    self.store.update_job(id, status='interrupted' if self.stop_event.is_set() else 'failed', message=message, outputs=outputs, warnings=warnings)
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.store.update_job(id, status='failed', message='Worker reporting failed: ' + str(exc)[:800])
        finally:
            self.kill(p)
            with self.lock:
                with contextlib.suppress(Exception):
                    self.store.update_meta(id,eta_seconds=None,speed='',stage='Finished',finished=time.time(),**clock.snapshot())
                self.active.pop(id, None)
            # Only this application's exact job-owned work directory is removed.
            stage = Path(job['payload']['destination']) / ('.forge-' + id + '.work')
            shutil.rmtree(stage, ignore_errors=True)
            with contextlib.suppress(Exception):
                p.stdout.close()
                p.stderr.close()

    @staticmethod
    def kill(p):
        if p.poll() is not None:
            return
        try:
            parent = psutil.Process(p.pid)
            children = parent.children(recursive=True)
            for child in reversed(children):
                with contextlib.suppress(psutil.Error):
                    child.kill()
            with contextlib.suppress(psutil.Error):
                parent.kill()
            psutil.wait_procs(children, timeout=3)
            p.wait(timeout=3)
        except (psutil.Error, subprocess.TimeoutExpired):
            with contextlib.suppress(OSError):
                p.kill()

    def cancel(self, id: str):
        with self.lock:
            j = self.store.job(id)
            if j['status'] == 'finalizing':
                raise ValueError('This job is publishing finished files. Cancellation is closed at this final step.')
            if j['status'] == 'queued':
                self.store.update_meta(id, cancel_intent='user')
                self.store.update_job(id, status='cancelled', message='Cancelled before processing.')
            elif j['status'] == 'running':
                self.store.update_meta(id, cancel_intent='user')
                self.store.update_job(id, status='cancelling', message='Stopping worker and child processes')
                p = self.active.get(id)
                if p:
                    self.kill(p)
            else:
                raise ValueError('Only queued or running jobs can be cancelled.')

    def retry(self, id: str):
        with self.lock:
            job = self.store.job(id)
            plan_id = job['payload'].get('download_plan_id')
            if plan_id and self.download_planner is not None:
                self._retry_selection([job], validate_only=True)
                result = self.download_planner.action([plan_id], 'retry')
                return {'plan': result['items'][0], 'queued': False}
            return self._retry_selection([job])[0]

    def reorder(self, id: str, direction: str):
        with self.lock:
            self._reorder_selection([id], direction)

    def _reorder_selection(self, selected, direction):
        if direction not in {'up', 'down', 'first', 'last'}:
            raise ValueError('Unknown queue ordering action.')
        ids = [job['id'] for job in self.store.jobs(status='queued', limit=100000)]
        selected = set(selected)
        if not selected <= set(ids):
            raise ValueError('Only queued jobs can be reordered.')
        if direction in {'first', 'last'}:
            picked = [id for id in ids if id in selected]
            others = [id for id in ids if id not in selected]
            ids = picked + others if direction == 'first' else others + picked
        elif direction == 'up':
            for index in range(1, len(ids)):
                if ids[index] in selected and ids[index - 1] not in selected:
                    ids[index - 1], ids[index] = ids[index], ids[index - 1]
        else:
            for index in range(len(ids) - 2, -1, -1):
                if ids[index] in selected and ids[index + 1] not in selected:
                    ids[index], ids[index + 1] = ids[index + 1], ids[index]
        for rank, id in enumerate(ids):
            self.store.update_meta(id, rank=rank)

    def _retry_selection(self, originals, *, validate_only=False):
        self.check_accepting()
        self.check_features(originals)
        if any(job['status'] not in TERMINAL or job['id'] in self.active for job in originals):
            raise ValueError('Wait until every selected job stops before retrying.')
        selected = {job['id'] for job in originals}
        batch = uuid.uuid4().hex
        planned = []
        pending = sorted(originals, key=lambda job: job['created'])
        seen = set()
        # Plan and validate the whole selection before adding any retry records.
        while pending:
            ready = [job for job in pending if job['payload'].get('depends_on') not in selected
                     or job['payload'].get('depends_on') in seen]
            if not ready:
                raise ValueError('The Black Scripting dependency graph contains a cycle.')
            for job in ready:
                payload = copy.deepcopy(job['payload'])
                if job['tool'].startswith('download'):
                    payload['resume_key'] = payload.get('resume_key') or payload.get('watch_media') or job['id']
                    payload['resume_from_job'] = job['id']
                parent = payload.get('depends_on')
                if parent and parent not in selected and self.store.job(parent)['status'] != 'completed':
                    raise ValueError('Select the unfinished parent step too, or retry the entire Black Scripting group.')
                payload['batch_id'] = batch
                payload['settings'] = self.store.settings()
                if payload.get('source') and not parent:
                    path = Path(payload['source']['path'])
                    if not path.is_file():
                        raise ValueError('Source file no longer exists. Select it again.')
                    stat = path.stat()
                    payload['source_stat'] = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
                    if payload.get('original_source') and payload['original_source'].get('path') == str(path):
                        payload['original_source_stat'] = copy.deepcopy(payload['source_stat'])
                payload.pop('resolved_parent', None)
                planned.append((job, payload))
                seen.add(job['id'])
                pending.remove(job)
        if validate_only:
            return planned
        mapping = {}
        result = []
        for job, payload in planned:
            if payload.get('depends_on') in mapping:
                payload['depends_on'] = mapping[payload['depends_on']]
            new = self.store.add_job(job['tool'], job['label'], payload)
            mapping[job['id']] = new['id']
            result.append(new)
        return result

    def check_features(self, jobs):
        """Validate every retry tool before any selected record is cloned."""
        if self.require_feature is not None:
            for tool in dict.fromkeys(job['tool'] for job in jobs):
                self.require_feature(tool)

    def bulk_action(self, ids, action):
        if action not in {'cancel', 'retry', 'remove', 'up', 'down', 'first', 'last'}:
            raise ValueError('Unknown queue action.')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or any(not isinstance(id, str) or len(id) > 100 for id in ids):
            raise ValueError('Select between 1 and 1000 queue records.')
        ids = list(dict.fromkeys(ids))
        with self.lock:
            originals = [self.store.job(id) for id in ids]
            plans = []
            if action == 'retry':
                self._retry_selection(originals, validate_only=True)
                linked = [job for job in originals if job['payload'].get('download_plan_id') and self.download_planner is not None]
                if linked:
                    plans = self.download_planner.action(list(dict.fromkeys(job['payload']['download_plan_id'] for job in linked)), 'retry')['items']
                remaining = [job for job in originals if job not in linked]
                jobs = self._retry_selection(remaining) if remaining else []
            elif action == 'remove':
                if any(job['status'] not in TERMINAL or job['id'] in self.active for job in originals):
                    raise ValueError('Only stopped, terminal queue records can be removed. Cancel pending work first.')
                selected = set(ids)
                # Deletion is only of database rows. Never unlink sources, outputs,
                # staging directories, or model files when clearing queue history.
                with self.store.lock, self.store.db:
                    references = self.store.db.execute('SELECT id, payload FROM jobs')
                    if any(json.loads(row['payload']).get('depends_on') in selected and row['id'] not in selected
                           for row in references):
                        raise ValueError('A selected record is referenced by another Black Scripting step. Select all dependent records too.')
                    self.store.db.executemany('DELETE FROM job_meta WHERE id=?', [(id,) for id in ids])
                    self.store.db.executemany('DELETE FROM jobs WHERE id=?', [(id,) for id in ids])
                jobs = []
            elif action == 'cancel':
                if any(job['status'] not in {'queued', 'running'} for job in originals):
                    raise ValueError('Only queued or running jobs can be cancelled; no selected jobs were changed.')
                for id in ids:
                    self.cancel(id)
                jobs = [self.store.job(id) for id in ids]
            else:
                self._reorder_selection(ids, action)
                jobs = [self.store.job(id) for id in ids]
            return {'ok': True, 'action': action, 'ids': ids, 'jobs': [public_job(job) for job in jobs], 'plans': plans}

    def retry_batch(self, batch_id: str):
        with self.lock:
            originals = [job for job in self.store.jobs(limit=100000) if job.get('batch_id') == batch_id]
            if not originals:
                raise ValueError('Batch not found.')
            ids = {job['id'] for job in originals}
            if any(job['payload'].get('depends_on') and job['payload']['depends_on'] not in ids for job in originals):
                raise ValueError('The Black Scripting dependency graph is incomplete. No new jobs were created.')
            return self._retry_selection(originals)

    def stop(self):
        self.stop_event.set()
        if self.scheduler.is_alive():
            self.scheduler.join(timeout=3)
        with self.lock:
            processes = list(self.active.values())
        for p in processes:
            self.kill(p)
        for t in self.threads:
            t.join(timeout=6)
        if self.download_planner is not None:
            self.download_planner.close()
