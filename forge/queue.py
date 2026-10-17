from __future__ import annotations
import contextlib
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
        self.active: dict[str, subprocess.Popen] = {}
        self.threads: list[threading.Thread] = []
        self.stop_event = threading.Event()
        self.scheduler = threading.Thread(target=self.loop, name='forge-scheduler', daemon=True)

    def start(self):
        self.scheduler.start()

    def loop(self):
        while not self.stop_event.wait(0.35):
            try:
                with self.lock:
                    settings = self.store.settings()
                    if settings['queue_paused']:
                        continue
                    running_transcription = any(self.store.job(i)['tool'] == 'transcribe' for i in self.active)
                    for job in self.store.jobs(status='queued'):
                        if len(self.active) >= settings['workers'] or self.stop_event.is_set():
                            break
                        if job['tool'] == 'transcribe' and running_transcription:
                            continue
                        self.launch(job)
                        if job['tool'] == 'transcribe':
                            running_transcription = True
                    self.threads = [t for t in self.threads if t.is_alive()]
            except Exception:
                # Preserve the service and show an actionable queue error rather than silently dying.
                self.store.set('queue_error', 'The dispatcher encountered an error. Restart FORGE and review interrupted jobs.')

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
            j = self.store.job(id)
            if j['status'] not in {'failed', 'cancelled', 'interrupted', 'completed'}:
                raise ValueError('Wait until this job stops before retrying.')
            payload = dict(j['payload'])
            payload['settings'] = self.store.settings()
            # Take a new explicit snapshot; retry is a new job, not a false partial resume.
            if payload.get('source'):
                p = Path(payload['source']['path'])
                if not p.is_file():
                    raise ValueError('Source file no longer exists. Select it again.')
                payload['source_stat'] = {'size': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns}
            return self.store.add_job(j['tool'], j['label'], payload)

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
