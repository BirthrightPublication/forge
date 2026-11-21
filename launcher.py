"""Open the default browser; launch a single local backend when needed."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def maintenance_startup(data):
    """Resume a proved inactive transaction without manual Python commands."""
    import importlib.util
    script = ROOT / 'scripts' / 'managed_install.py'
    if not script.is_file():
        raise RuntimeError('FORGE maintenance needs recovery, but its controlled helper is missing. Use the installed external updater.')
    spec = importlib.util.spec_from_file_location('forge_maintenance_bootstrap',script)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    info = helper.inspect_marker(data)
    if info.get('state') == 'inactive' and info.get('safe_to_clear'):
        helper.clear_orphan_marker(data)
        return False
    if info.get('state') == 'inactive' and info.get('repairable'):
        running = alive(data)
        if running:
            verify_runtime(running)
            return False  # Open the existing Studio's controlled Repair action.
        request_path = helper.recovery_request(data,os.getpid(),helper.process_started(os.getpid()))
        interpreter = Path(sys._base_executable).with_name('python.exe') if os.name == 'nt' else Path(sys._base_executable)
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
        subprocess.Popen([str(interpreter),str(script),'--recover-launch',str(request_path)],cwd=ROOT,
                         env={**os.environ,'FORGE_DATA_DIR':str(data),'PYTHONUTF8':'1'},creationflags=flags)
        return True
    raise RuntimeError('FORGE maintenance is still active or its ownership cannot be verified. '
                       +info.get('message','Wait for the installation to finish or use the installed recovery updater.'))

def data_directory():
    if os.getenv('FORGE_DATA_DIR'):
        return Path(os.environ['FORGE_DATA_DIR']).expanduser().resolve()
    return Path(os.getenv('LOCALAPPDATA', str(Path.home()/'.local'/'share'))) / 'FORGE'

def request(runtime, route, body=None):
    url = f'http://127.0.0.1:{runtime["port"]}' + route
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
          headers={'X-Forge-Token': runtime['token'], 'Content-Type':'application/json'})
    # Loopback traffic must not leave through the user's proxy settings.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=2) as response:
        return json.load(response)

def alive(data):
    try:
        runtime = json.loads((data/'runtime.json').read_text(encoding='utf-8'))
        state = request(runtime, '/api/state')
        return runtime if state.get('version') else None
    except Exception:
        return None

def verify_runtime(runtime, expected=None):
    """Never open or stop a different installation sharing a data directory."""
    if Path(runtime.get('root', '')).resolve() != ROOT:
        raise RuntimeError('Another FORGE installation owns this data directory. Close it before opening this installation.')
    expected = expected or (ROOT/'VERSION.txt').read_text('utf-8').strip()
    state = request(runtime, '/api/state')
    if runtime.get('version') != expected or state.get('version') != expected:
        raise RuntimeError('An older FORGE backend is still running. Finish active jobs, run Stop FORGE.bat, then open FORGE again. Your queue and files are preserved.')
    return runtime

def main():
    if '--stop' in sys.argv:
        runtime = alive(data_directory())
        if runtime:
            if Path(runtime.get('root','')).resolve() != ROOT:
                raise RuntimeError('Refusing to stop a different FORGE installation.')
            request(runtime, '/api/shutdown', {})
        return
    if (ROOT/'.forge-maintenance.json').exists() and maintenance_startup(data_directory()):
        return
    if (ROOT/'.forge-updating').exists() or (ROOT/'.forge-update-state.json').exists():
        raise RuntimeError('A FORGE update is in progress or was interrupted. Run Update FORGE.bat to finish or recover it before opening the Studio.')
    data = data_directory(); data.mkdir(parents=True, exist_ok=True)
    runtime = alive(data)
    if runtime:
        verify_runtime(runtime)
    if not runtime:
        interpreter = ROOT/'.venv'/'Scripts'/'python.exe' if os.name == 'nt' else Path(sys.executable)
        if not interpreter.is_file():
            interpreter = Path(sys.executable)
        # Keep real stdout/stderr for the service log without showing a console.
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        with (data/'service.log').open('ab') as log:
            subprocess.Popen([str(interpreter), str(ROOT/'server.py')], cwd=ROOT, stdout=log, stderr=log,
                stdin=subprocess.DEVNULL, creationflags=flags, start_new_session=(os.name != 'nt'))
        for _ in range(120):
            time.sleep(0.25)
            runtime = alive(data)
            if runtime:
                break
        if not runtime:
            raise RuntimeError('FORGE could not start. Run Setup FORGE.bat, then review ' + str(data/'service.log'))
        verify_runtime(runtime)
    args = [arg for arg in sys.argv[1:] if not arg.startswith('--')]
    sources = []
    if args:
        expanded = []
        for arg in args:
            path = Path(arg.strip('"')).resolve()
            if path.is_dir():
                expanded += [str(p) for p in path.iterdir() if p.is_file()][:500]
            elif path.is_file():
                expanded.append(str(path))
        if expanded:
            sources = request(runtime,'/api/register', {'paths':expanded[:500]})['sources']
    ticket=request(runtime,'/api/launch-ticket',{})['ticket']
    fragment = urllib.parse.urlencode({'launch':ticket, 'sources':','.join(s['id'] for s in sources)})
    webbrowser.open(f'http://127.0.0.1:{runtime["port"]}/#{fragment}')

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), 'FORGE could not open', 0x10)
        else:
            print(str(error), file=sys.stderr)
        sys.exit(1)
