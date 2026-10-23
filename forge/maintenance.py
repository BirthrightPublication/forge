"""Drain work before starting an independent, trusted maintenance process."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psutil

from .core import ROOT, json_write
from .dependencies import ENGINES


class Maintenance:
    def __init__(self, app, store, manager):
        self.app, self.store, self.manager = app, store, manager
        self.cancelled = threading.Event()
        self.thread = None
        self.store.set('update_transfer', {})

    def status(self):
        transfer = self.store.get('update_transfer', {})
        if transfer: return transfer
        value = self.store.get('maintenance', {})
        path = self.store.data / 'maintenance' / (value.get('id', '') + '.result.json')
        if value.get('id') and path.is_file():
            import json
            try:
                value = {**value, **json.loads(path.read_text('utf-8'))}
            except (OSError, ValueError):
                pass
        if (value.get('status') in {'waiting','starting'} and not self.manager.maintenance_pending
                and not (self.thread and self.thread.is_alive())
                and not (ROOT / '.forge-maintenance.json').exists()):
            value = {**value, 'status':'cancelled', 'message':'The pending installation was interrupted before replacement. Request it again when ready.'}
            self.store.set('maintenance', value)
        if value.get('id') and value.get('status') in {'completed','failed','cancelled','recovered'}:
            with self.store.lock:
                history=self.store.get('maintenance_history',[])
                history=[item for item in history if item.get('id')!=value['id']]
                self.store.set('maintenance_history',[{key:value[key] for key in ('id','status','message','version','requested','completed') if key in value}]+history[:29])
        return value

    def request(self, engines, *, approve=False, cancel_jobs=False, application_package=None, preserve_queued=False):
        if os.name != 'nt':
            raise ValueError('Managed installation requires Windows. Use the documented pinned requirements on this platform.')
        if not isinstance(engines, list) or (not engines and not application_package) or not all(e in ENGINES for e in engines):
            raise ValueError('Choose registered dependency classes.')
        application = None
        if application_package:
            from .birthright_update.core import digest
            package = Path(application_package).resolve()
            if package.parent != (ROOT / 'Build').resolve() or not package.is_file() or not approve:
                raise ValueError('Application updates require approval and a verified local build.')
            application = {'path': str(package), 'sha256': digest(package)}
        engines = list(dict.fromkeys(engines))
        with self.manager.lock:
            self.manager.check_accepting()
            approvals = self.store.get('dependency_approvals', [])
            if not approve and any(engine not in approvals for engine in engines):
                raise ValueError('Approve these dependency classes from PyPI before installation.')
            if approve:
                self.store.set('dependency_approvals', sorted(set(approvals + engines)))
            self.manager.maintenance_pending = True
            preserve_queued = preserve_queued or self.store.settings()['queue_paused']
            self.manager.repair_pending = preserve_queued
            self.cancelled = threading.Event()
            value = {'id': uuid.uuid4().hex, 'engines': engines, 'status': 'waiting',
                     'application': application,
                     'preserve_queued': preserve_queued,
                     'message': 'Waiting for active jobs; queued work is preserved.' if preserve_queued else 'Waiting for existing jobs to finish. New jobs are paused.', 'requested': time.time()}
            self.store.set('maintenance', value)
            if cancel_jobs:
                for job in self.store.jobs(limit=100000):
                    if job['status'] in {'queued', 'running'}:
                        self.manager.cancel(job['id'])
            self.thread = threading.Thread(target=self._wait, args=(value, self.cancelled), daemon=True)
            self.thread.start()
            return value

    def cancel(self):
        with self.manager.lock:
            if self.manager.maintenance:
                raise ValueError('Maintenance is already starting. Review its separate Windows console.')
            self.cancelled.set()
            self.manager.maintenance_pending = False
            self.manager.repair_pending = False
            value = {**self.store.get('maintenance', {}), 'status': 'cancelled', 'message': 'Installation request cancelled.'}
            self.store.set('maintenance', value)
            return value

    def _wait(self, value, cancelled):
        helper = None
        marker = ROOT / '.forge-maintenance.json'
        owns_marker = False
        try:
            while not cancelled.wait(.5):
                with self.manager.lock:
                    if cancelled.is_set():
                        return
                    if time.time()-value['requested'] > 1800:
                        raise RuntimeError('Installation request expired after 30 minutes waiting for work. No files were changed; request it again when ready.')
                    if self.store.settings()['queue_paused']:
                        value['preserve_queued'] = True
                        self.manager.repair_pending = True
                    if self.manager.active or (not value['preserve_queued'] and any(j['status'] == 'queued' for j in self.store.jobs(limit=100000))):
                        continue
                    if self.manager.stop_event.is_set():
                        return
                    if not self.app.state.shutdown:
                        raise RuntimeError('The launcher did not register its shutdown handler.')
                    if marker.exists():
                        raise RuntimeError('Earlier maintenance needs recovery before another installation.')
                    settings = self.store.settings()
                    task = {**value, 'root': str(ROOT), 'data': str(self.store.data),
                            'port': self.app.state.port,
                            'parent_pid': os.getpid(), 'parent_started': psutil.Process().create_time(),
                            'queue_paused': settings['queue_paused']}
                    path = self.store.data / 'maintenance' / (value['id'] + '.request.json')
                    json_write(path, task)
                    self.manager.maintenance = True
                    json_write(marker, {'id': value['id'], 'data': str(self.store.data)})
                    owns_marker = True
                    # The base interpreter survives replacement/rollback of a private environment.
                    python = str(Path(sys._base_executable).with_name('python.exe'))
                    helper = subprocess.Popen([python, str(ROOT / 'scripts' / 'managed_install.py'), str(path)],
                                     cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE,
                                     env={**os.environ, 'FORGE_DATA_DIR': str(self.store.data), 'PYTHONUTF8': '1'})
                    self.store.set('maintenance', {**value, 'status': 'starting', 'message': 'FORGE is stopping for installation; it will reopen automatically.'})
                    self.app.state.shutdown()
                    return
        except Exception as exc:
            # This process is still alive, so the helper has not passed its
            # exact-parent exit gate and cannot have changed an environment.
            if helper is not None:
                helper.terminate()
                helper.wait(timeout=10)
            if owns_marker:
                marker.unlink(missing_ok=True)
            with self.manager.lock:
                self.manager.maintenance = self.manager.maintenance_pending = False
                self.manager.repair_pending = False
                self.store.set('maintenance', {**value, 'status': 'failed', 'message': str(exc)[:1000]})


def mount(app, store, manager):
    service = Maintenance(app, store, manager)
    app.state.maintenance_service = service

    @app.get('/api/maintenance')
    def status():
        return service.status()

    @app.get('/api/maintenance/history')
    def history():
        service.status()
        return {'history':store.get('maintenance_history',[])}

    @app.post('/api/maintenance/install')
    def install(body: dict):
        if body.get('cancel_jobs') and body.get('confirm_cancel') is not True:
            raise ValueError('Confirm cancellation of existing jobs explicitly, or wait for them to finish.')
        return service.request(body.get('engines'), approve=body.get('approve') is True,
                               cancel_jobs=body.get('cancel_jobs') is True)

    @app.post('/api/maintenance/cancel')
    def cancel():
        return service.cancel()
