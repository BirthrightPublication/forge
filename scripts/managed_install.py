"""Trusted external Windows maintenance runner. Uses the base Python, not .venv.

No downloaded scripts, arbitrary command strings or elevated process creation.
Do not import application dependencies: the UI environment may need repair.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import threading
import uuid
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent.parent
ENGINES = {'core': ('requirements-core.txt', '.venv'),
           'transcription': ('requirements-transcription.txt', '.venv'),
           'downloads': ('requirements-downloads.txt', '.venv'),
           'speakers': ('requirements-diarization.txt', '.diarization-venv'),
           'media-privacy': ('scripts/requirements-media-privacy.txt', '.venv')}


def process_started(pid):
    """Creation time for an exact process, without depending on the repaired venv."""
    if type(pid) is not int or pid <= 0:
        raise ValueError('Invalid maintenance process identity.')
    if os.name != 'nt':
        import psutil
        try:
            process = psutil.Process(pid)
            return None if process.status() == psutil.STATUS_ZOMBIE else process.create_time()
        except psutil.NoSuchProcess:
            return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise PermissionError('Cannot verify the maintenance owner process.')
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise OSError('Cannot inspect the maintenance owner process.')
        if exit_code.value != 259:
            return None
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
            raise OSError('Cannot read maintenance owner creation time.')
        return ((created.dwHighDateTime << 32) + created.dwLowDateTime) / 10000000 - 11644473600
    finally:
        kernel.CloseHandle(handle)


def _read_record(path):
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()) or path.stat().st_size > 262144:
        raise ValueError('Maintenance records must be bounded regular local files.')
    value = json.loads(path.read_text('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid maintenance record.')
    return value


def _legacy_hosts(request):
    """Find a pre-V0.10 installer that did not record its PID in the marker."""
    script = os.path.normcase(str(ROOT / 'scripts' / 'managed_install.py'))
    target = os.path.normcase(str(request))
    try:
        import psutil
    except ImportError:
        # The base Windows interpreter can query its own processes without any
        # packages. This is a fixed diagnostic command, never browser-supplied.
        if os.name != 'nt':
            raise RuntimeError('Process inspection is unavailable.')
        query = "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', query],
                                capture_output=True, text=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('Cannot verify legacy maintenance process ownership.')
        records = json.loads(result.stdout or '[]')
        if isinstance(records, dict):
            records = [records]
        for record in records:
            line = record.get('CommandLine')
            if not isinstance(line, str):
                raise RuntimeError('A Python process cannot be inspected safely.')
            normalized = os.path.normcase(line)
            if script in normalized and target in normalized:
                return True
        return False
    for process in psutil.process_iter(['pid', 'name', 'cmdline'], ad_value=None):
        if not str(process.info.get('name') or '').lower().startswith('python'):
            continue
        args = process.info.get('cmdline')
        if args is None:
            if process_started(process.info['pid']) is None:
                continue
            raise RuntimeError('A Python process cannot be inspected safely.')
        normalized = [os.path.normcase(str(value).strip('"')) for value in args]
        if script in normalized and target in normalized:
            return True
    return False


def _marker_info(data=None):
    marker = ROOT / '.forge-maintenance.json'
    if not marker.exists():
        return {'state': 'none', 'repairable': False}
    try:
        record = _read_record(marker)
        identity = record.get('id', '')
        if not re.fullmatch(r'[a-f0-9]{32}', identity):
            raise ValueError('Unknown maintenance record identity.')
        owner_data = Path(record['data']).resolve()
        if data is not None and owner_data != Path(data).resolve():
            raise ValueError('The maintenance marker belongs to another data directory.')
        if 'root' in record and Path(record['root']).resolve() != ROOT.resolve():
            raise ValueError('The maintenance marker belongs to another installation.')
        maintenance = owner_data / 'maintenance'
        if (maintenance.is_symlink() or (hasattr(maintenance,'is_junction') and maintenance.is_junction())
                or maintenance.resolve() != maintenance):
            raise ValueError('The maintenance directory cannot redirect ownership through a link.')
        request = maintenance / (identity + '.request.json')
        task = _read_record(request)
        if (task.get('id') != identity or Path(task.get('root', '')).resolve() != ROOT.resolve()
                or Path(task.get('data', '')).resolve() != owner_data):
            raise ValueError('The marker does not match its registered installation request.')
        started = record.get('owner_started')
        if record.get('owner_pid') is not None:
            if type(started) not in (int, float) or not math.isfinite(started) or started <= 0:
                raise ValueError('The maintenance owner has no valid creation time.')
            actual = process_started(record['owner_pid'])
            if actual is not None and abs(actual - started) <= .01:
                return {'state': 'active', 'repairable': False, 'id': identity,
                        'message': 'The registered maintenance owner is still running.'}
        elif _legacy_hosts(request):
            return {'state': 'active', 'repairable': False, 'id': identity,
                    'message': 'A legacy maintenance process is still running.'}
        backup = maintenance / identity
        if (backup.is_symlink() or (hasattr(backup,'is_junction') and backup.is_junction())
                or backup.resolve() != backup):
            raise ValueError('The maintenance backup cannot redirect ownership through a link.')
        journal = backup / 'transaction.json'
        transaction = _read_record(journal) if journal.is_file() else None
        pending = transaction is not None and transaction.get('state') not in {'completed', 'rolled_back'}
        pending |= any((ROOT / name).exists() for name in ('.forge-update-state.json', '.forge-updating'))
        return {'state': 'inactive', 'repairable': True, 'id': identity, 'data': str(owner_data),
                'needs_recovery': pending, 'safe_to_clear': not pending,
                'marker_sha256': hashlib.sha256(marker.read_bytes()).hexdigest(),
                'message': 'An interrupted installation needs controlled recovery.' if pending else 'An inactive maintenance marker can be safely cleared.'}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        return {'state': 'unknown', 'repairable': False,
                'message': str(error) + ' Existing files were left unchanged; use the recovery updater if needed.'}


def inspect_marker(data=None):
    if not (ROOT / '.forge-maintenance.json').exists():
        return {'state': 'none', 'repairable': False}
    try:
        with maintenance_lock():
            return _marker_info(data)
    except RuntimeError:
        return {'state': 'active', 'repairable': False, 'message': 'A maintenance process holds the installation lock.'}


def clear_orphan_marker(data):
    """Clear only a proved inactive, journal-safe marker under the same OS lock."""
    with maintenance_lock():
        info = _marker_info(data)
        if info.get('state') != 'inactive' or not info.get('safe_to_clear'):
            raise RuntimeError(info.get('message', 'Maintenance ownership could not be verified.'))
        marker = ROOT / '.forge-maintenance.json'
        if hashlib.sha256(marker.read_bytes()).hexdigest() != info['marker_sha256']:
            raise RuntimeError('Maintenance ownership changed; no recovery was performed.')
        destination = Path(data) / 'maintenance'
        write(destination / (info['id'] + '.marker-' + str(time.time_ns()) + '.json'), _read_record(marker))
        write(destination / (info['id'] + '.result.json'),
              {'status': 'recovered', 'completed': time.time(),
               'message': 'Inactive maintenance marker recovered. No application, environment or user files were replaced.'})
        marker.unlink()
        return {**info, 'state': 'recovered', 'repairable':False,
                'message': 'Inactive maintenance marker recovered; FORGE can continue.'}


def recovery_request(data, parent_pid, parent_started):
    """Register a launcher recovery; only the controlled host may mutate files."""
    with maintenance_lock():
        info = _marker_info(data)
        if info.get('state') != 'inactive' or not info.get('repairable'):
            raise RuntimeError(info.get('message', 'Maintenance ownership cannot be verified.'))
        old = _read_record(Path(data) / 'maintenance' / (info['id'] + '.request.json'))
        identity = uuid.uuid4().hex
        request = Path(data) / 'maintenance' / (identity + '.request.json')
        task = {'id':identity,'root':str(ROOT),'data':str(Path(data).resolve()),'engines':[],
                'recovery':{key:info[key] for key in ('id','marker_sha256')},
                'parent_pid':parent_pid,'parent_started':parent_started,
                'port':old.get('port',8765),'queue_paused':bool(old.get('queue_paused',False)),
                'browser_sessions':{},'requested':time.time()}
        write(request,task)
        return request


@contextmanager
def maintenance_lock():
    """Crash-safe ownership, independent of the environment being repaired."""
    sys.path.insert(0, str(ROOT))
    from forge.model_cache import cache_lock
    try:
        with cache_lock(ROOT / '.forge-maintenance.lock', timeout=.5):
            yield
    except RuntimeError as error:
        if str(error).startswith('Timed out waiting for the model cache.'):
            raise RuntimeError('Another maintenance process holds the installation lock. Wait for its console to finish.') from error
        raise


def restore_environments(backup, changed, saved):
    if any(e not in {'.venv', '.diarization-venv'} for e in [*changed, *saved]):
        raise ValueError('Invalid environment in recovery journal.')
    for environment in saved:
        original=backup/environment
        if (original.is_symlink() or (hasattr(original,'is_junction') and original.is_junction())
                or original.resolve().parent != backup.resolve()):
            raise RuntimeError('The environment backup redirects outside its registered transaction.')
        if not (original / 'Scripts' / 'python.exe').is_file():
            raise RuntimeError('The environment backup is incomplete. External repair is required.')
    for environment in reversed(changed):
        if environment not in {'.venv', '.diarization-venv'}:
            raise ValueError('Invalid environment in recovery journal.')
        target = ROOT / environment
        failed = backup / (environment + '-failed-' + str(time.time_ns()))
        if target.exists():
            target.rename(failed)
        if environment in saved:
            original = backup / environment
            shutil.copytree(original, target)


def _recover_locked(allow_owner=None):
    marker = ROOT / '.forge-maintenance.json'
    task = _read_record(marker)
    if not re.fullmatch(r'[a-f0-9]{32}', task.get('id', '')):
        raise ValueError('Invalid maintenance recovery marker.')
    if 'root' in task and Path(task['root']).resolve() != ROOT.resolve():
        raise ValueError('Maintenance belongs to another installation.')
    if task.get('owner_pid') is not None:
        actual = process_started(task['owner_pid'])
        expected = task.get('owner_started')
        if type(expected) not in (int, float) or not math.isfinite(expected):
            raise ValueError('Invalid maintenance owner identity.')
        owned = allow_owner and task.get('recovery_request_id') == allow_owner and task['owner_pid'] == os.getpid()
        if actual is not None and abs(actual - expected) <= .01 and not owned:
            raise RuntimeError('The maintenance owner is still running; its marker was preserved.')
    from launcher import alive
    data = Path(task['data']).resolve()
    maintenance=data/'maintenance'
    if maintenance.is_symlink() or maintenance.resolve()!=maintenance:
        raise ValueError('Maintenance recovery directory ownership cannot be verified.')
    if alive(data):
        raise RuntimeError('Stop FORGE before recovering an interrupted environment.')
    backup = data / 'maintenance' / task['id']
    if backup.is_symlink() or backup.resolve()!=backup:
        raise ValueError('Maintenance recovery backup ownership cannot be verified.')
    journal = backup / 'transaction.json'
    if journal.is_file():
        record = _read_record(journal)
        if record.get('state') not in {'completed', 'rolled_back'}:
            rollback_application(record.get('application'))
            restore_environments(backup, record['changed'], record['saved'])
            write(journal, {**record, 'state': 'rolled_back'})
    # The CLI recovers only the interrupted code journal/marker before it could
    # select any new package. Existing user databases are not rolled backward.
    if (ROOT / '.forge-update-state.json').exists() or (ROOT / '.forge-updating').exists():
        run([sys.executable, str(ROOT / 'scripts' / 'update.py'), '--root', str(ROOT), '--yes', '--no-launch'])
    write(data / 'maintenance' / (task['id'] + '.result.json'),
          {'status': 'recovered', 'message': 'Interrupted maintenance recovered. Previous environments and user data were preserved.'})
    marker.unlink()
    print('Interrupted maintenance recovered. Open FORGE normally.')


def recover():
    """Explicit recovery command; refuses to race a live maintenance owner."""
    with maintenance_lock():
        _recover_locked()


def rollback_application(application):
    if not application:
        return
    sys.path.insert(0, str(ROOT / 'scripts'))
    from update import installed_version, inspect_backup
    current = installed_version(ROOT)
    if current == application['from']:
        return
    if current != application['to']:
        raise RuntimeError('Installed code no longer matches this maintenance transaction; automatic rollback stopped.')
    previous = _read_record(ROOT / 'update-last.json')
    backup = Path(previous.get('backup', '')).resolve()
    if not backup.is_relative_to((ROOT / 'Backups').resolve()):
        raise RuntimeError('Code rollback backup does not belong to this installation.')
    journal = inspect_backup(ROOT, backup)
    if (previous.get('version') != application['to'] or journal.get('to') != application['to']
            or journal.get('from') != application['from'] or journal.get('state') != 'completed'):
        raise RuntimeError('Code rollback journal does not match the approved update.')
    run([sys.executable, str(ROOT / 'scripts' / 'update.py'), '--root', str(ROOT),
         '--rollback', '--yes', '--no-launch'], timeout=300)


def write(path, value):
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class MaintenanceProgress:
    """Read-only loopback status during service downtime; no application imports.

    Existing browser cookies are checked against approved session hashes. No
    installer actions, files, login endpoints or cross-origin reads are exposed.
    """
    def __init__(self, task, result_path):
        self.task,self.path=task,result_path
        self.server=self.thread=None

    def start(self):
        owner=self
        port=int(self.task.get('port',8765))
        if not 1024<=port<=65535: raise ValueError('Invalid maintenance status port.')
        origin='http://127.0.0.1:'+str(port)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                allowed=self.headers.get('Host')=='127.0.0.1:'+str(port)
                allowed &= self.headers.get('Origin') in {None,origin}
                allowed &= self.headers.get('Sec-Fetch-Site','none') in {'none','same-origin'}
                try:
                    cookies=SimpleCookie(self.headers.get('Cookie',''))
                    value=cookies.get('forge_browser_'+str(port))
                    secret=value.value if value else ''
                    expires=owner.task.get('browser_sessions',{}).get(hashlib.sha256(secret.encode()).hexdigest(),0)
                    allowed &= bool(secret) and len(secret)<=256 and expires>time.time()
                except (CookieError,ValueError,TypeError):
                    allowed=False
                code=200 if allowed else 401
                body={'detail':'Authorized same-origin browser session required.'}
                if allowed:
                    code=200 if self.path=='/api/maintenance' else 503
                    body={'detail':'FORGE is performing approved maintenance.'}
                    if code==200:
                        try: body=json.loads(owner.path.read_text('utf-8'))
                        except (OSError,ValueError): body={'status':'starting','message':'Preparing approved maintenance.'}
                payload=json.dumps(body).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type','application/json')
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers();self.wfile.write(payload)
        class Server(ThreadingHTTPServer):
            allow_reuse_address = os.name != 'nt'
            daemon_threads = True
            def get_request(self):
                connection,address=super().get_request()
                connection.settimeout(3)
                return connection,address
        self.server=Server(('127.0.0.1',port),Handler)
        self.server.timeout=1
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.1},daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown();self.server.server_close()
            self.thread.join(timeout=3)
            self.server=None


def run(command, *, timeout=1800):
    result = subprocess.run(command, cwd=ROOT, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Installation verification command failed; see the maintenance console.')


def wait_for_parent(pid, started, timeout=90):
    """Wait on this exact process handle, checking creation time to avoid PID reuse."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # Process has already exited.
            return
        raise OSError('Cannot verify the FORGE process has stopped.')
    try:
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
            raise OSError('Cannot read FORGE process identity.')
        seconds = ((created.dwHighDateTime << 32) + created.dwLowDateTime) / 10000000 - 11644473600
        if abs(seconds - started) > .01:
            raise RuntimeError('FORGE process identity changed. No packages were changed.')
        if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
            raise RuntimeError('FORGE did not stop. No packages were changed.')
    finally:
        kernel.CloseHandle(handle)


def install(task):
    """Back up existing environments; prepare wheels before any package replacement."""
    data = Path(task['data'])
    backup = data / 'maintenance' / task['id']
    backup.mkdir(exist_ok=False)
    environments = list(dict.fromkeys(ENGINES[e][1] for e in task['engines']))
    saved = {}
    changed = []
    application_record = None
    journal = backup / 'transaction.json'
    def stage(status,message):
        write(data/'maintenance'/(task['id']+'.result.json'),{'status':status,'message':message})
    def checkpoint(state='installing'):
        write(journal, {'state': state, 'changed': changed, 'saved': list(saved),
                        'application': application_record})
    checkpoint()
    base = sys.executable
    try:
        for environment in environments:
            stage('backing_up','Backing up the existing '+environment+' environment.')
            target = ROOT / environment
            if target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction()):
                raise ValueError('Managed environments cannot be links or junctions.')
            if target.exists():
                size = sum(p.stat().st_size for p in target.rglob('*') if p.is_file())
                if shutil.disk_usage(backup).free < size + 512 * 1048576:
                    raise RuntimeError('Not enough free space to back up the environment safely.')
                original = backup / environment
                shutil.copytree(target, original)
                saved[environment] = original
                checkpoint()
            python = target / 'Scripts' / 'python.exe'
            if not python.is_file():
                changed.append(environment)
                checkpoint()
                run([base, '-m', 'venv', str(target)])
            wheels = backup / (environment + '-wheels')
            reqs = [str(ROOT / ENGINES[e][0]) for e in task['engines'] if ENGINES[e][1] == environment]
            args = [item for req in reqs for item in ('-r', req)]
            constraints = ['-c', str(ROOT / 'requirements-core.txt')] if environment == '.venv' else []
            stage('downloading','Downloading compatible packages from PyPI.')
            run([str(python), '-m', 'pip', '--isolated', '--disable-pip-version-check', 'download',
                 '--index-url', 'https://pypi.org/simple', '--only-binary=:all:', '--dest', str(wheels), *constraints, *args])
            if environment not in changed:
                changed.append(environment)
                checkpoint()
            stage('installing','Installing verified local dependency wheels.')
            run([str(python), '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install', '--no-index',
                 '--find-links', str(wheels), '--only-binary=:all:', '--upgrade', '--upgrade-strategy', 'only-if-needed', *constraints, *args])
            run([str(python), '-m', 'pip', 'check'], timeout=120)
            modules = {'core': 'fastapi,uvicorn,PIL,qrcode,psutil', 'transcription': 'faster_whisper,ctranslate2',
                       'downloads': 'yt_dlp', 'speakers': 'pyannote.audio', 'media-privacy': 'c2pa'}
            for engine in task['engines']:
                if ENGINES[engine][1] == environment:
                    run([str(python), '-c', 'import ' + modules[engine]], timeout=120)
        application = task.get('application')
        if application:
            stage('installing','Backing up and applying verified application source.')
            package = Path(application['path']).resolve()
            if package.parent != (ROOT / 'Build').resolve() or hashlib.sha256(package.read_bytes()).hexdigest() != application['sha256']:
                raise ValueError('The approved application package changed. Nothing from it was installed.')
            python = ROOT / '.venv' / 'Scripts' / 'python.exe'
            sys.path.insert(0, str(ROOT))
            from forge.birthright_update.core import Release, apply_update
            from forge.update_adapter import LocalUpdaterAdapter
            from update import inspect_release
            manifest, _ = inspect_release(package)
            application_record = {'from': (ROOT / 'VERSION.txt').read_text('utf-8').strip(),
                                  'to': manifest['version'], 'sha256': application['sha256']}
            checkpoint()
            release = Release('birthright.forge', manifest['version'], '', '', ())
            adapter = LocalUpdaterAdapter(ROOT, python, package, service_stopped=True)
            apply_update(adapter, release, package, approved=True)
        checkpoint('completed')
        return {'status': 'installed', 'message': 'Installation verified on disk; checking the fresh interpreter and restarted service.',
                'backup': str(backup)}
    except Exception:
        rollback_application(application_record)
        restore_environments(backup, changed, saved)
        checkpoint('rolled_back')
        raise


def validate_import(python):
    """A new interpreter must import the installed source, never the host's cache."""
    expected = (ROOT / 'VERSION.txt').read_text('utf-8').strip()
    script = ('import compileall,py_compile,sys; from pathlib import Path; '
              'root=Path(sys.argv[1]); sys.path.insert(0,str(root)); '
              'assert all(compileall.compile_dir(str(root/p),quiet=1,force=True,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH) for p in ("forge","scripts")); '
              'import forge; assert forge.__version__==sys.argv[2], "Imported version does not match installed version"; '
              'assert Path(forge.__file__).resolve()==root/"forge"/"__init__.py", "Wrong import root"')
    run([str(python), '-I', '-c', script, str(ROOT), expected], timeout=120)
    return expected


def verify_health(runtime, expected, pid):
    from launcher import request
    if (Path(runtime.get('root','')).resolve() != ROOT or runtime.get('pid') != pid
            or runtime.get('version') != expected):
        raise RuntimeError('Restarted service identity does not match this installation.')
    health = request(runtime, '/health')
    state = request(runtime, '/api/state')
    if health.get('pid') != pid or health.get('version') != expected or state.get('version') != expected:
        raise RuntimeError('Restarted health/imported application version does not match the installed release.')
    if state.get('application_directory') and Path(state['application_directory']).resolve() != ROOT.resolve():
        raise RuntimeError('Restarted state belongs to another installation root.')
    confirmed = request(runtime, '/health')
    if confirmed.get('pid') != pid or confirmed.get('version') != expected:
        raise RuntimeError('The backend identity changed during restart verification.')


def owned_server_pid(runtime, launcher_pid):
    """Windows venv redirectors own a child interpreter with a different PID.

    Require an unbroken current process ancestry to the still-live Popen handle;
    matching an HTTP port or runtime-file PID alone is never sufficient.
    """
    pid=runtime.get('pid')
    if type(pid) is not int or pid<=0:
        raise RuntimeError('The restarted service has no valid process identity.')
    if pid==launcher_pid: return pid
    if os.name!='nt': raise RuntimeError('The restarted service is not the owned child process.')
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('usage',wintypes.DWORD),('pid',wintypes.DWORD),
                  ('heap',ctypes.c_size_t),('module',wintypes.DWORD),('threads',wintypes.DWORD),
                  ('parent',wintypes.DWORD),('priority',wintypes.LONG),('flags',wintypes.DWORD),
                  ('exe',wintypes.WCHAR*260)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    kernel.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    snapshot=kernel.CreateToolhelp32Snapshot(2,0)
    if snapshot==ctypes.c_void_p(-1).value:
        raise OSError('Cannot verify the restarted interpreter process tree.')
    parents={}
    try:
        entry=Entry();entry.size=ctypes.sizeof(Entry)
        if kernel.Process32FirstW(snapshot,ctypes.byref(entry)):
            while True:
                parents[entry.pid]=entry.parent
                if not kernel.Process32NextW(snapshot,ctypes.byref(entry)): break
    finally:
        kernel.CloseHandle(snapshot)
    current=pid
    for _ in range(16):
        current=parents.get(current)
        if current==launcher_pid: return pid
        if not current: break
    raise RuntimeError('The restarted backend is not owned by this maintenance process.')


def stop_owned_child(child):
    if child.poll() is not None: return
    if os.name=='nt':
        # The original process handle remains live: this PID cannot have been reused.
        try:
            subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],cwd=ROOT,
                           timeout=15,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.CalledProcessError:
            if child.poll() is None: raise
    else:
        child.terminate()
    child.wait(timeout=15)


def restart_verified(task, result_path, before_restart=lambda: None):
    data = Path(task['data'])
    python = ROOT / '.venv' / 'Scripts' / 'python.exe'
    write(result_path, {'status':'validating', 'message':'Verifying fresh imports and application caches.'})
    expected = validate_import(python)
    if task.get('expected_version') and task['expected_version'] != expected:
        raise RuntimeError('Installed application version differs from the explicitly approved maintenance version. Restart success was not recorded.')
    # Suspend dispatch during health validation, without deleting queued work.
    preference = 'from forge.core import Store; import sys; from pathlib import Path; s=Store(Path(sys.argv[1])); v=s.settings(); v["queue_paused"]=sys.argv[2]=="True"; s.set("settings",v)'
    run([str(python), '-c', preference, str(data), 'True'], timeout=30)
    write(result_path, {'status':'restarting','message':'Starting FORGE '+expected+' and checking its process and health.'})
    before_restart()  # Release the original loopback port before the new backend binds it.
    environment = {**os.environ, 'FORGE_DATA_DIR':str(data), 'FORGE_PORT':str(task.get('port',8765)), 'PYTHONUTF8':'1'}
    with (data / 'service.log').open('ab') as log:
        child = subprocess.Popen([str(python),str(ROOT/'server.py')],cwd=ROOT,env=environment,
                                 stdout=log,stderr=log,stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic()+60
        last_error = 'No runtime identity was available.'
        while time.monotonic()<deadline:
            if child.poll() is not None:
                raise RuntimeError('The restarted service exited. Review service.log; the verified backup is retained.')
            try:
                runtime = json.loads((data/'runtime.json').read_text('utf-8'))
                server_pid = owned_server_pid(runtime, child.pid)
                verify_health(runtime, expected, server_pid)
                break
            except (OSError, ValueError, RuntimeError) as error:
                last_error = str(error)
                time.sleep(.25)
        else:
            raise RuntimeError('Restart health verification timed out: '+last_error+' No update success was recorded.')
        from launcher import request
        # Restore via the live service to avoid stale in-memory settings.
        request(runtime, '/api/queue', {'paused':bool(task['queue_paused'])})
    except BaseException:
        # This process handle belongs only to the child we created.
        try:
            stop_owned_child(child)
        finally:
            run([str(python), '-c', preference, str(data), str(bool(task['queue_paused']))], timeout=30)
        raise
    task['_verified_backend'] = {'pid': server_pid, 'root': str(ROOT),
                                 'backend_verified': True, 'verified_at': time.time()}
    # An update requested from the Studio already has an authorized open tab;
    # that workspace verifies/reloads itself rather than opening a duplicate.
    if not task.get('browser_sessions'):
        try:
            subprocess.Popen([str(python.with_name('pythonw.exe')),str(ROOT/'launcher.py')],cwd=ROOT,env=environment)
        except OSError:
            print('The service passed health checks. Open FORGE.bat to reopen your browser.')
    return expected


def _execute_recovery(task, result_path):
    bridge = MaintenanceProgress(task, result_path)
    marker = ROOT / '.forge-maintenance.json'
    target = task['recovery']
    with maintenance_lock():
        info = _marker_info(task['data'])
        if (info.get('state') != 'inactive' or info.get('id') != target['id']
                or info.get('marker_sha256') != target['marker_sha256']):
            raise RuntimeError('Recovery ownership changed. No files were replaced.')
        write(marker, {**_read_record(marker), 'root':str(ROOT),'owner_pid':os.getpid(),
                       'owner_started':process_started(os.getpid()),'owner_role':'recovery',
                       'recovery_request_id':task['id']})
        try:
            wait_for_parent(int(task['parent_pid']), float(task['parent_started']))
            write(result_path, {'status':'installing','message':'Recovering the registered interrupted installation; protected user data is retained.'})
            bridge.start()
            _recover_locked(allow_owner=task['id'])
            # Recovery restores a known prior transaction, not the failed target.
            task.pop('expected_version',None)
            version = restart_verified(task, result_path, before_restart=bridge.stop)
            result = {'status':'recovered','version':version,'completed':time.time(),
                      **task.get('_verified_backend',{}),
                      'message':'Previous FORGE '+version+' recovered and its fresh backend passed health checks. Protected projects, models and environments were preserved.'}
        except Exception as error:
            result = {'status':'failed','message':str(error)[:1000]+' Recovery backups and logs were retained.'}
        finally:
            bridge.stop()
        write(result_path, result)
        print(result['message'])
        return 0 if result['status']=='recovered' else 1


def main():
    if os.name != 'nt':
        raise RuntimeError('This maintenance runner requires Windows.')
    if sys.argv[1:] == ['--recover']:
        if sys.prefix != sys.base_prefix:
            raise RuntimeError('Run recovery with base Python: py -3 scripts/managed_install.py --recover')
        recover()
        return 0
    recovery_launch = len(sys.argv)==3 and sys.argv[1]=='--recover-launch'
    if sys.prefix != sys.base_prefix:
        raise RuntimeError('Managed maintenance must use its registered base interpreter. Use Repair in Updates.')
    request = Path(sys.argv[2] if recovery_launch else sys.argv[1]).resolve()
    task = json.loads(request.read_text('utf-8'))
    if (not re.fullmatch(r'[a-f0-9]{32}', task.get('id', '')) or Path(task['root']).resolve() != ROOT
            or request != Path(task['data']).resolve() / 'maintenance' / (task['id'] + '.request.json')
            or not isinstance(task.get('engines'), list) or (not task['engines'] and not task.get('application') and not task.get('recovery'))
            or any(engine not in ENGINES for engine in task['engines'])):
        raise ValueError('Invalid maintenance request.')
    result_path = request.with_name(task['id'] + '.result.json')
    if task.get('recovery'):
        target=task['recovery']
        if (not isinstance(target,dict) or set(target)!={'id','marker_sha256'}
                or not re.fullmatch(r'[a-f0-9]{32}',target.get('id',''))
                or not re.fullmatch(r'[a-f0-9]{64}',target.get('marker_sha256',''))):
            raise ValueError('Invalid registered recovery target.')
        return _execute_recovery(task,result_path)
    if recovery_launch:
        raise ValueError('The recovery launcher requires a registered recovery request.')
    stopped = safe = False
    progress_server = MaintenanceProgress(task,result_path)
    # Keep ownership through journal validation and marker removal. Recovery and
    # a second installer must never race the original transaction's cleanup.
    with maintenance_lock():
        marker = ROOT / '.forge-maintenance.json'
        if marker.exists() and json.loads(marker.read_text('utf-8')).get('id') != task['id']:
            raise RuntimeError('Another maintenance transaction requires completion or recovery.')
        write(marker, {'id':task['id'],'data':task['data'],'root':str(ROOT),
                       'owner_pid':os.getpid(),'owner_started':process_started(os.getpid()),'owner_role':'installer'})
        try:
            wait_for_parent(int(task['parent_pid']), float(task['parent_started']))
            stopped = True
            write(result_path, {'status': 'installing', 'message': 'Installing verified dependency classes; keep this console open.'})
            progress_server.start()
            result = install(task)
        except Exception as exc:
            result = {'status': 'failed', 'message': str(exc)[:1000] + ' Existing environment backups remain in the maintenance directory.'}
        journal = Path(task['data']) / 'maintenance' / task['id'] / 'transaction.json'
        safe = (not stopped or (journal.is_file() and json.loads(journal.read_text('utf-8')).get('state') in {'completed', 'rolled_back'}))
        safe = safe and not any((ROOT / name).exists() for name in ('.forge-updating', '.forge-update-state.json'))
        if safe:
            marker.unlink()
        if stopped and safe and result['status'] in {'installed','failed'}:
            try:
                version = restart_verified(task, result_path, before_restart=progress_server.stop)
                if result['status']=='installed':
                    result.update(status='completed', version=version, completed=time.time(), **task.get('_verified_backend',{}),
                                  message='FORGE '+version+' restarted and passed process, import and health checks. Existing data and environments were preserved.')
                else:
                    result.update(version=version, completed=time.time(),
                                  message=result['message']+' The preserved installation restarted and passed health checks; the attempted update still failed.')
            except Exception as exc:
                original_error=str(exc)[:700]
                try:
                    write(result_path,{'status':'installing','message':'The new backend did not pass health checks. Restoring the previous verified code and environments.'})
                    record=_read_record(journal)
                    rollback_application(record.get('application'))
                    restore_environments(journal.parent,record['changed'],record['saved'])
                    write(journal,{**record,'state':'rolled_back'})
                    rollback_task={**task}
                    if record.get('application'):
                        rollback_task['expected_version']=record['application']['from']
                    version=restart_verified(rollback_task,result_path,before_restart=progress_server.stop)
                    result.update(status='failed',version=version,rollback_verified=True,
                                  **rollback_task.get('_verified_backend',{}),
                                  message='The attempted update failed: '+original_error+' Previous FORGE '+version+' was restored and its backend passed health checks.')
                except Exception as recovery_error:
                    write(marker, {'id':task['id'],'data':task['data'],'root':str(ROOT),
                                   'owner_pid':os.getpid(),'owner_started':process_started(os.getpid()),'owner_role':'recovery-needed'})
                    result.update(status='failed',message=original_error+' Recovery requires Repair in Updates or the external updater: '+str(recovery_error)[:500]+'. Backups are retained.')
        write(result_path, result)
        print(result['message'])
        progress_server.stop()
    if stopped:
        if not safe:
            print('Recovery remains pending. Run: py -3 scripts/managed_install.py --recover')
            return 1
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
