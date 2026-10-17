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

def main():
    data = data_directory(); data.mkdir(parents=True, exist_ok=True)
    runtime = alive(data)
    if '--stop' in sys.argv:
        if runtime:
            request(runtime, '/api/shutdown', {})
        return
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
    fragment = urllib.parse.urlencode({'token':runtime['token'], 'sources':','.join(s['id'] for s in sources)})
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
