"""Drain work before starting an independent, trusted maintenance process."""
from __future__ import annotations

import contextlib
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
        marker = ROOT / '.forge-maintenance.json'
        if marker.exists() and not (self.thread and self.thread.is_alive()):
            from scripts import managed_install
            recovery = managed_install.inspect_marker(self.store.data)
            value = {**value, 'recovery': recovery}
            if recovery.get('state') == 'unknown':
                value['message'] = recovery.get('message','Maintenance ownership requires recovery review.')
            if recovery.get('state') == 'inactive' and recovery.get('safe_to_clear'):
                with self.manager.lock:
                    try:
                        recovered = managed_install.clear_orphan_marker(self.store.data)
                    except RuntimeError:
                        value['recovery'] = managed_install.inspect_marker(self.store.data)
                        return value
                    self.manager.maintenance = self.manager.maintenance_pending = self.manager.repair_pending = False
                    if not value.get('id') or value.get('id') == recovered['id']:
                        value = {**value, 'id': recovered['id'], 'status': 'recovered',
                                 'message': recovered['message'], 'recovery': recovered}
                        self.store.set('maintenance', value)
        return value

    def request(self, engines, *, approve=False, cancel_jobs=False, application_package=None, preserve_queued=False, recovery=None):
        if os.name != 'nt':
            raise ValueError('Managed installation requires Windows. Use the documented pinned requirements on this platform.')
        if not isinstance(engines, list) or (not engines and not application_package and not recovery) or not all(e in ENGINES for e in engines):
            raise ValueError('Choose registered dependency classes.')
        if recovery:
            from scripts import managed_install
            info = managed_install.inspect_marker(self.store.data)
            if info.get('state') != 'inactive' or not info.get('repairable') or any(info.get(key) != recovery.get(key) for key in ('id','marker_sha256')):
                raise ValueError('Interrupted maintenance ownership changed. Refresh Updates before repairing.')
        application = None
        if application_package:
            from .birthright_update.core import digest
            package = Path(application_package).resolve()
            if package.parent != (ROOT / 'Build').resolve() or not package.is_file() or not approve:
                raise ValueError('Application updates require approval and a verified local build.')
            from scripts.update import inspect_release
            manifest,_ = inspect_release(package)
            application = {'path': str(package), 'sha256': digest(package),'version':manifest['version']}
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
                     'application': application, 'recovery': recovery,
                     'preserve_queued': preserve_queued,
                     'message': 'Waiting for active jobs; queued work is preserved.' if preserve_queued else 'Waiting for existing jobs to finish. New jobs are paused.', 'requested': time.time()}
            from . import __version__
            value['expected_version'] = application['version'] if application else __version__
            self.store.set('maintenance', value)
            if cancel_jobs:
                for job in self.store.jobs(limit=100000):
                    if job['status'] in {'queued', 'running'}:
                        self.manager.cancel(job['id'])
            self.thread = threading.Thread(target=self._wait, args=(value, self.cancelled), daemon=True)
            try:
                self.thread.start()
            except Exception:
                self.manager.maintenance_pending = self.manager.maintenance = self.manager.repair_pending = False
                self.store.set('maintenance',{**value,'status':'failed','message':'The maintenance host could not be scheduled. No files were changed; retry Repair when resources are available.'})
                raise ValueError('The maintenance host could not be scheduled. No files were changed.') from None
            return value

    def recover(self):
        from scripts import managed_install
        with self.manager.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('The current installation request is still running.')
            info = managed_install.inspect_marker(self.store.data)
            if info.get('state') != 'inactive' or not info.get('repairable'):
                raise ValueError(info.get('message', 'No verified interrupted installation is available to repair.'))
            if info.get('safe_to_clear'):
                result = managed_install.clear_orphan_marker(self.store.data)
                self.manager.maintenance = self.manager.maintenance_pending = self.manager.repair_pending = False
                value = {'id':result['id'],'status':'recovered','message':result['message'],'completed':time.time()}
                self.store.set('maintenance', value)
                return value
            self.manager.maintenance = self.manager.maintenance_pending = self.manager.repair_pending = False
            return self.request([], approve=True, preserve_queued=True,
                                recovery={key:info[key] for key in ('id','marker_sha256')})

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
                        from scripts import managed_install
                        info = managed_install.inspect_marker(self.store.data)
                        if value.get('recovery'):
                            if info.get('state') != 'inactive' or any(info.get(key) != value['recovery'].get(key) for key in ('id','marker_sha256')):
                                raise RuntimeError('Maintenance ownership changed; no files were modified.')
                        elif info.get('state') == 'inactive' and info.get('safe_to_clear'):
                            managed_install.clear_orphan_marker(self.store.data)
                        else:
                            raise RuntimeError('Earlier maintenance needs Repair in Updates before another installation.')
                    settings = self.store.settings()
                    task = {**value, 'root': str(ROOT), 'data': str(self.store.data),
                            'port': self.app.state.port,
                            'browser_sessions': {key: item['expires'] for key,item in self.store.get('browser_sessions',{}).items()},
                            'parent_pid': os.getpid(), 'parent_started': psutil.Process().create_time(),
                            'queue_paused': settings['queue_paused']}
                    path = self.store.data / 'maintenance' / (value['id'] + '.request.json')
                    json_write(path, task)
                    self.manager.maintenance = True
                    if not value.get('recovery'):
                        json_write(marker, {'id':value['id'],'data':str(self.store.data),'root':str(ROOT),
                                            'owner_pid':os.getpid(),'owner_started':task['parent_started'],'owner_role':'service'})
                        owns_marker = True
                    # The base interpreter survives replacement/rollback of a private environment.
                    python = str(Path(sys._base_executable).with_name('python.exe'))
                    helper = subprocess.Popen([python, str(ROOT / 'scripts' / 'managed_install.py'), str(path)],
                                     cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE,
                                     env={**os.environ, 'FORGE_DATA_DIR': str(self.store.data), 'PYTHONUTF8': '1'})
                    if not value.get('recovery'):
                        json_write(marker, {'id':value['id'],'data':str(self.store.data),'root':str(ROOT),
                                            'owner_pid':helper.pid,'owner_started':psutil.Process(helper.pid).create_time(),
                                            'owner_role':'installer'})
                    self.store.set('maintenance', {**value, 'status': 'starting', 'message': 'FORGE is stopping for installation; it will reopen automatically.'})
                    self.app.state.shutdown()
                    return
        except Exception as exc:
            # This process is still alive, so the helper has not passed its
            # exact-parent exit gate and cannot have changed an environment.
            helper_stopped = helper is None
            if helper is not None:
                with contextlib.suppress(OSError,subprocess.SubprocessError):
                    helper.terminate()
                    helper.wait(timeout=10)
                helper_stopped = helper.poll() is not None
            if owns_marker and helper_stopped:
                with contextlib.suppress(OSError,ValueError):
                    import json
                    current=json.loads(marker.read_text('utf-8'))
                    if current.get('id') == value['id']:
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

    @app.post('/api/maintenance/recover')
    def recover(body: dict):
        if set(body) != {'consent'} or body.get('consent') is not True:
            raise ValueError('Approve recovery of the registered interrupted installation.')
        return service.recover()
