"""Trusted external Windows maintenance runner. Uses the base Python, not .venv.

No downloaded scripts, arbitrary command strings or elevated process creation.
Do not import application dependencies: the UI environment may need repair.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent.parent
ENGINES = {'core': ('requirements-core.txt', '.venv'),
           'transcription': ('requirements-transcription.txt', '.venv'),
           'downloads': ('requirements-downloads.txt', '.venv'),
           'speakers': ('requirements-diarization.txt', '.diarization-venv'),
           'media-privacy': ('scripts/requirements-media-privacy.txt', '.venv')}


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
        if not (backup / environment / 'Scripts' / 'python.exe').is_file():
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


def recover():
    """Explicit recovery command; refuses to race a live maintenance owner."""
    marker = ROOT / '.forge-maintenance.json'
    with maintenance_lock():
        task = json.loads(marker.read_text('utf-8'))
        if not re.fullmatch(r'[a-f0-9]{32}', task.get('id', '')):
            raise ValueError('Invalid maintenance recovery marker.')
        from launcher import alive
        data = Path(task['data']).resolve()
        if alive(data):
            raise RuntimeError('Stop FORGE before recovering an interrupted environment.')
        backup = data / 'maintenance' / task['id']
        journal = backup / 'transaction.json'
        if journal.is_file():
            record = json.loads(journal.read_text('utf-8'))
            if record.get('state') not in {'completed', 'rolled_back'}:
                restore_environments(backup, record['changed'], record['saved'])
                write(journal, {**record, 'state': 'rolled_back'})
        # Application code has its own verified journal and recovery entrypoint.
        if (ROOT / '.forge-update-state.json').exists() or (ROOT / '.forge-updating').exists():
            python = ROOT / '.venv' / 'Scripts' / 'python.exe'
            run([str(python), str(ROOT / 'scripts' / 'update.py'), '--root', str(ROOT), '--yes', '--no-launch'])
        write(data / 'maintenance' / (task['id'] + '.result.json'),
              {'status': 'recovered', 'message': 'Interrupted maintenance recovered. Previous environments and user data were preserved.'})
        marker.unlink()
        print('Interrupted maintenance recovered. Open FORGE normally.')


def write(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


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
    journal = backup / 'transaction.json'
    def stage(status,message):
        write(data/'maintenance'/(task['id']+'.result.json'),{'status':status,'message':message})
    def checkpoint(state='installing'):
        write(journal, {'state': state, 'changed': changed, 'saved': list(saved)})
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
            release = Release('birthright.forge', manifest['version'], '', '', ())
            adapter = LocalUpdaterAdapter(ROOT, python, package, service_stopped=True)
            apply_update(adapter, release, package, approved=True)
        checkpoint('completed')
        return {'status': 'installed', 'message': 'Installation verified on disk; checking the fresh interpreter and restarted service.',
                'backup': str(backup)}
    except Exception:
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


def restart_verified(task, result_path):
    data = Path(task['data'])
    python = ROOT / '.venv' / 'Scripts' / 'python.exe'
    write(result_path, {'status':'validating', 'message':'Verifying fresh imports and application caches.'})
    expected = validate_import(python)
    # Suspend dispatch during health validation, without deleting queued work.
    preference = 'from forge.core import Store; import sys; from pathlib import Path; s=Store(Path(sys.argv[1])); v=s.settings(); v["queue_paused"]=sys.argv[2]=="True"; s.set("settings",v)'
    run([str(python), '-c', preference, str(data), 'True'], timeout=30)
    write(result_path, {'status':'restarting','message':'Starting FORGE '+expected+' and checking its process and health.'})
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
    try:
        subprocess.Popen([str(python.with_name('pythonw.exe')),str(ROOT/'launcher.py')],cwd=ROOT,env=environment)
    except OSError:
        print('The service passed health checks. Open FORGE.bat to reopen your browser.')
    return expected


def main():
    if os.name != 'nt':
        raise RuntimeError('This maintenance runner requires Windows.')
    if sys.argv[1:] == ['--recover']:
        if sys.prefix != sys.base_prefix:
            raise RuntimeError('Run recovery with base Python: py -3 scripts/managed_install.py --recover')
        recover()
        return 0
    request = Path(sys.argv[1]).resolve()
    task = json.loads(request.read_text('utf-8'))
    if (not re.fullmatch(r'[a-f0-9]{32}', task.get('id', '')) or Path(task['root']).resolve() != ROOT
            or request != Path(task['data']).resolve() / 'maintenance' / (task['id'] + '.request.json')
            or not isinstance(task.get('engines'), list) or (not task['engines'] and not task.get('application'))
            or any(engine not in ENGINES for engine in task['engines'])):
        raise ValueError('Invalid maintenance request.')
    result_path = request.with_name(task['id'] + '.result.json')
    stopped = safe = False
    # Keep ownership through journal validation and marker removal. Recovery and
    # a second installer must never race the original transaction's cleanup.
    with maintenance_lock():
        marker = ROOT / '.forge-maintenance.json'
        if marker.exists() and json.loads(marker.read_text('utf-8')).get('id') != task['id']:
            raise RuntimeError('Another maintenance transaction requires completion or recovery.')
        write(marker, {'id': task['id'], 'data': task['data']})
        try:
            wait_for_parent(int(task['parent_pid']), float(task['parent_started']))
            stopped = True
            write(result_path, {'status': 'installing', 'message': 'Installing verified dependency classes; keep this console open.'})
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
                version = restart_verified(task, result_path)
                if result['status']=='installed':
                    result.update(status='completed', version=version, completed=time.time(),
                                  message='FORGE '+version+' restarted and passed process, import and health checks. Existing data and environments were preserved.')
                else:
                    result.update(version=version, completed=time.time(),
                                  message=result['message']+' The preserved installation restarted and passed health checks; the attempted update still failed.')
            except Exception as exc:
                result.update(status='failed', message=str(exc)[:1000]+' Use the external updater for rollback or recovery; backups are retained.')
        write(result_path, result)
        print(result['message'])
    if stopped:
        if not safe:
            print('Recovery remains pending. Run: py -3 scripts/managed_install.py --recover')
            return 1
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
