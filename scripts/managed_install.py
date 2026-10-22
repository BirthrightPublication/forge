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
    def checkpoint(state='installing'):
        write(journal, {'state': state, 'changed': changed, 'saved': list(saved)})
    checkpoint()
    base = sys.executable
    try:
        for environment in environments:
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
            run([str(python), '-m', 'pip', '--isolated', '--disable-pip-version-check', 'download',
                 '--index-url', 'https://pypi.org/simple', '--only-binary=:all:', '--dest', str(wheels), *constraints, *args])
            if environment not in changed:
                changed.append(environment)
                checkpoint()
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
        return {'status': 'completed', 'message': 'Dependencies installed and verified. Environments and model caches were preserved.',
                'backup': str(backup), 'completed': time.time()}
    except Exception:
        restore_environments(backup, changed, saved)
        checkpoint('rolled_back')
        raise


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
        write(result_path, result)
        print(result['message'])
    if stopped:
        if not safe:
            print('Recovery remains pending. Run: py -3 scripts/managed_install.py --recover')
            return 1
        python = ROOT / '.venv' / 'Scripts' / 'python.exe'
        # Restore the previous dispatch preference, including a previously paused queue.
        run([str(python), '-c', 'from forge.core import Store; import sys; from pathlib import Path; s=Store(Path(sys.argv[1])); v=s.settings(); v["queue_paused"]=sys.argv[2]=="True"; s.set("settings",v)',
             task['data'], str(bool(task['queue_paused']))], timeout=30)
        subprocess.Popen([str(python.with_name('pythonw.exe')), str(ROOT / 'launcher.py')], cwd=ROOT)
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
