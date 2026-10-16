from __future__ import annotations
import contextlib
import copy
import uuid
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
import psutil
from .core import ROOT, Store, no_window

class QueueManager:
    def __init__(self, store: Store):
        self.store = store
        self.lock = threading.RLock()
        self.maintenance = False
        self.active: dict[str, subprocess.Popen] = {}
        self.threads: list[threading.Thread] = []
        self.stop_event = threading.Event()
        self.scheduler = threading.Thread(target=self.loop, name='forge-scheduler', daemon=True)

    def check_accepting(self):
        if self.maintenance:
            raise ValueError("FORGE is preparing an application update. New jobs are temporarily blocked.")

    def start(self):
        self.scheduler.start()

    def loop(self):
        while not self.stop_event.wait(0.35):
            try:
                with self.lock:
                    settings = self.store.settings()
                    if settings['queue_paused']:
                        continue
                    for job in self.store.jobs(status='queued',limit=100000):
                        if len(self.active) >= settings['workers'] or self.stop_event.is_set():
                            break
                        if not self.prepare_dependency(job):
                            continue
                        resource=self.resource(job)
                        active_jobs=[self.store.job(i) for i in self.active]
                        used=sum(self.resource(j)==resource for j in active_jobs)
                        cap=settings.get(resource+'_workers',1)
                        if used>=cap:
                            continue
                        # Model downloads and inference must not race in the shared cache.
                        if job['tool']=='model_download' and any(j['tool'] in {'transcribe','model_download'} for j in active_jobs):
                            continue
                        if job['tool']=='transcribe' and any(j['tool']=='model_download' for j in active_jobs):
                            continue
                        self.launch(job)
                    self.threads = [t for t in self.threads if t.is_alive()]
            except Exception:
                # Preserve the service and show an actionable queue error rather than silently dying.
                self.store.set('queue_error', 'The dispatcher encountered an error. Restart FORGE and review interrupted jobs.')

    @staticmethod
    def resource(job):
        if job['tool'].startswith('download') or job['tool']=='model_download':
            return 'download'
        if job['tool']=='transcribe' and job['payload']['options'].get('device','auto')!='cpu':
            return 'gpu'  # Auto conservatively reserves a GPU slot even on CPU fallback.
        return 'cpu'

    def prepare_dependency(self, job):
        parent_id=job['payload'].get('depends_on')
        if not parent_id:
            return True
        parent=self.store.job(parent_id)
        if parent['status'] in {'failed','cancelled','interrupted','blocked'}:
            self.store.update_job(job['id'],status='blocked',message='A previous workflow step did not complete. Retry the workflow group after reviewing that step.')
            return False
        if parent['status']!='completed':
            return False
        from .core import TOOLS
        candidates=[Path(o['path']) for o in parent['outputs'] if Path(o['path']).suffix.lower() in TOOLS[job['tool']]['accept']]
        if not candidates or not candidates[0].is_file():
            self.store.update_job(job['id'],status='blocked',message='The previous step has no available, compatible media output.')
            return False
        path=candidates[0]
        source=self.store.register(path)
        payload=job['payload']
        payload['source']=source
        stat=path.stat()
        payload['source_stat']={'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
        self.store.update_payload(job['id'],payload)
        return True

    def launch(self, job: dict):
        payload = {**job['payload'], 'data': str(self.store.data), 'parent_pid': os.getpid(),
                   'parent_started': psutil.Process().create_time()}
        env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1', 'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1'}
        # pythonw has redirected pipes; use python.exe for worker diagnostics on Windows.
        python = str(Path(sys.executable).with_name('python.exe')) if os.name == 'nt' else sys.executable
        try:
            p = subprocess.Popen([python, '-m', 'forge.worker'], cwd=ROOT, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8', errors='replace', **no_window())
            self.active[job['id']] = p
            self.store.update_job(job['id'], status='running', progress=0, message='Starting local worker')
            self.store.update_meta(job['id'],resource=self.resource(job),started=time.time(),stage='Starting',eta_seconds=None,speed='')
            p.stdin.write(json.dumps(payload, ensure_ascii=False))
            p.stdin.close()
            t = threading.Thread(target=self.collect, args=(job, p), daemon=True)
            self.threads.append(t)
            t.start()
        except Exception as error:
            self.active.pop(job['id'], None)
            self.store.update_job(job['id'], status='failed', message='Could not start worker: ' + str(error)[:800])

    def collect(self, job: dict, p: subprocess.Popen):
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
                        value = event.get('progress', 0)
                        if isinstance(value, (int, float)):
                            self.store.update_job(id, progress=max(0, min(99, value)), message=str(event.get('message', ''))[:2000])
                            eta=event.get('eta_seconds')
                            if not isinstance(eta,(int,float)) or eta<0 or eta>10**9: eta=None
                            self.store.update_meta(id,eta_seconds=eta,speed=str(event.get('speed',''))[:100],stage=str(event.get('stage',event.get('message','')))[:200])
                    elif event.get('kind') == 'finalizing' and status == 'running':
                        self.store.update_job(id, status='finalizing', progress=96, message='Publishing verified outputs')
                    elif event.get('kind') == 'result':
                        result = event
                    elif event.get('kind') == 'error':
                        error = str(event.get('message', 'Processing failed.'))
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
                    self.store.update_job(id, status='interrupted' if self.stop_event.is_set() else 'failed', message=message, outputs=outputs, warnings=warnings)
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.store.update_job(id, status='failed', message='Worker reporting failed: ' + str(exc)[:800])
        finally:
            self.kill(p)
            with self.lock:
                self.active.pop(id, None)
            with contextlib.suppress(Exception):
                self.store.update_meta(id,eta_seconds=None,speed='',stage='Finished')
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
                self.store.update_job(id, status='cancelled', message='Cancelled before processing.')
            elif j['status'] == 'running':
                self.store.update_job(id, status='cancelling', message='Stopping worker and child processes')
                p = self.active.get(id)
                if p:
                    self.kill(p)
            else:
                raise ValueError('Only queued or running jobs can be cancelled.')

    def retry(self, id: str):
        with self.lock:
            self.check_accepting()
            j = self.store.job(id)
            if j['status'] not in {'failed', 'cancelled', 'interrupted', 'completed','blocked'}:
                raise ValueError('Wait until this job stops before retrying.')
            payload = copy.deepcopy(j['payload'])
            if payload.get('depends_on') and self.store.job(payload['depends_on'])['status']!='completed':
                raise ValueError('Retry the workflow group, not this dependent step by itself.')
            payload['batch_id']=uuid.uuid4().hex
            payload['settings'] = self.store.settings()
            # Take a new explicit snapshot; retry is a new job, not a false partial resume.
            if payload.get('source'):
                p = Path(payload['source']['path'])
                if not p.is_file():
                    raise ValueError('Source file no longer exists. Select it again.')
                payload['source_stat'] = {'size': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns}
            return self.store.add_job(j['tool'], j['label'], payload)

    def reorder(self, id: str, direction: str):
        with self.lock:
            queued=self.store.jobs(status='queued',limit=100000)
            ids=[j['id'] for j in queued]
            if id not in ids:
                raise ValueError('Only queued jobs can be reordered.')
            index=ids.index(id)
            target={'up':max(0,index-1),'down':min(len(ids)-1,index+1),'first':0,'last':len(ids)-1}[direction]
            ids.insert(target,ids.pop(index))
            for rank,jid in enumerate(ids):
                self.store.update_meta(jid,rank=rank)

    def retry_batch(self, batch_id: str):
        with self.lock:
            self.check_accepting()
            originals=sorted([j for j in self.store.jobs(limit=100000) if j.get('batch_id')==batch_id],key=lambda j:j['created'])
            if not originals:
                raise ValueError('Batch not found.')
            if any(j['status'] in {'running','queued','finalizing','cancelling'} for j in originals):
                raise ValueError('Finish or cancel the entire batch before retrying it.')
            mapping={}
            batch=uuid.uuid4().hex
            planned=[]
            seen=set()
            for j in originals:
                parent=j['payload'].get('depends_on')
                if parent and parent not in seen:
                    raise ValueError('The workflow dependency graph is incomplete. No new jobs were created.')
                seen.add(j['id'])
                payload=copy.deepcopy(j['payload'])
                payload['settings']=self.store.settings()
                payload['batch_id']=batch
                if not payload.get('depends_on') and payload.get('source'):
                    path=Path(payload['source']['path'])
                    if not path.is_file():
                        raise ValueError('A workflow source was moved or deleted; reselect it.')
                    stat=path.stat()
                    payload['source_stat']={'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
                planned.append((j,payload))
            results=[]
            for j,payload in planned:
                if payload.get('depends_on'):
                    if payload['depends_on'] not in mapping:
                        raise ValueError('The workflow dependency graph is incomplete.')
                    payload['depends_on']=mapping[payload['depends_on']]
                new=self.store.add_job(j['tool'],j['label'],payload)
                mapping[j['id']]=new['id']
                results.append(new)
            return results

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
