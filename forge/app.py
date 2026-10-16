from __future__ import annotations
import asyncio
import contextlib
import importlib.metadata
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from . import __version__
from .core import ROOT, TOOLS, Store, absolute_path, dependency_state, no_window, passwords, safe_name
from .models import JobRequest, OPTION_MODELS, Password, Pick, Preset, QueueAction, Register, Settings
from .queue import QueueManager
from .auth import BrowserSessions

UPDATE_PACKAGES = ['fastapi','uvicorn','pillow','qrcode','psutil','faster-whisper','yt-dlp']

def public_job(job):
    return {k:v for k,v in job.items() if k != 'payload'}


def create_app(data: Path, token: str, port: int, *, workers=True):
    store = Store(data)
    manager = QueueManager(store)
    sessions = BrowserSessions(store,port)
    origin = f'http://127.0.0.1:{port}'
    picker_lock = threading.Lock()
    update_lock = threading.Lock()
    app = None

    def update_check(force=False):
        if not update_lock.acquire(blocking=False):
            return store.get('updates', {'busy': True})
        try:
            old = store.get('updates', {})
            if not force and time.time() - old.get('checked', 0) < 86400:
                return old
            entries = []
            for name in UPDATE_PACKAGES:
                try:
                    installed = importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:
                    installed = None
                entry = {'package': name, 'installed': installed, 'latest': None}
                try:
                    req = urllib.request.Request(f'https://pypi.org/pypi/{name}/json', headers={'User-Agent':f'FORGE/{__version__} release-check'})
                    with urllib.request.urlopen(req, timeout=4) as response:
                        doc = json.loads(response.read(4_000_000))
                    entry['latest'] = doc['info']['version']
                except Exception:
                    entry['error'] = 'Unavailable / offline'
                entries.append(entry)
            result = {'checked': time.time(), 'packages': entries, 'note': 'Discovery only. No code or models were installed or executed.'}
            store.set('updates', result)
            return result
        finally:
            update_lock.release()

    @asynccontextmanager
    async def lifespan(app):
        if workers:
            manager.start()
        if store.settings()['daily_update_check']:
            threading.Thread(target=update_check, daemon=True).start()
        yield
        manager.stop()
        # Keep store available to test clients after lifespan teardown; launcher process exits next.

    app = FastAPI(title='FORGE Local Studio', version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.manager = manager
    app.state.shutdown = None

    @app.middleware('http')
    async def local_guard(request: Request, call_next):
        if request.headers.get('host', '') != f'127.0.0.1:{port}':
            return JSONResponse({'detail':'Untrusted Host header.'}, status_code=403)
        request_origin = request.headers.get('origin')
        if request_origin and request_origin != origin:
            return JSONResponse({'detail':'Cross-origin access is blocked.'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail':'Cross-site access is blocked.'}, status_code=403)
        if request.method == 'OPTIONS':
            return JSONResponse({'detail':'Cross-origin requests are not enabled.'}, status_code=403)
        if request.url.path.startswith('/api/'):
            supplied = request.headers.get('x-forge-token', '')
            native=secrets.compare_digest(supplied,token)
            session=sessions.lookup(request.cookies.get(sessions.cookie,''))
            bootstrap=request.url.path=='/api/session' and request.method=='POST' and request.headers.get('x-forge-bootstrap')=='1'
            if not native and not session and not bootstrap:
                return JSONResponse({'detail':'Open FORGE once from its Windows launcher to remember this browser profile.'},status_code=401)
            if not native and not bootstrap and request.method not in {'GET','HEAD'}:
                if not session or not secrets.compare_digest(request.headers.get('x-forge-csrf',''),session['csrf']):
                    return JSONResponse({'detail':'Browser request verification failed. Reload the FORGE page.'},status_code=403)
            if manager.maintenance and request.method not in {'GET','HEAD'} and request.url.path not in {'/api/shutdown','/api/update/prepare','/api/update/cancel'}:
                return JSONResponse({'detail':'FORGE is preparing an application update. Changes are temporarily blocked.'},status_code=409)
            length=request.headers.get('content-length','0')
            if request.url.path!='/api/upload' and length.isdigit() and int(length)>2_000_000:
                return JSONResponse({'detail':'Request is too large.'},status_code=413)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({'detail':str(exc)}, status_code=400)

    @app.exception_handler(OSError)
    async def filesystem_error(request, exc):
        return JSONResponse({'detail':'Local filesystem operation failed: ' + str(exc)}, status_code=400)

    @app.get('/health')
    def health():
        return {'app':'FORGE','version':__version__}

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'web' / 'index.html')

    @app.get('/assets/{name}')
    def asset(name: str):
        if name not in {'app.js','v2.js','v3.js','style.css','forge.ico'}:
            raise HTTPException(404)
        return FileResponse(ROOT / 'web' / name)

    @app.get('/api/state')
    def state():
        jobs = store.visible_jobs()
        return {'version':__version__, 'tools':TOOLS, 'jobs':[public_job(j) for j in jobs],
                'settings':store.settings(), 'dependencies':dependency_state(store.settings()),
                'data_directory':str(store.data), 'presets':store.get('presets', []),
                'updates':store.get('updates'), 'queue_error':store.get('queue_error'),
                'platform':sys.platform, 'history_limit':500,'workflows':store.get('workflows',[]),'diagnostics':store.get('diagnostics'),'application_directory':str(ROOT),'favorites':store.get('favorites',[])}

    @app.post('/api/register')
    def register(body: Register):
        paths = [absolute_path(p) for p in body.paths]
        return {'sources':[store.register(p) for p in paths]}

    @app.get('/api/sources')
    def sources(ids: str):
        values = ids.split(',')
        if len(values) > 500:
            raise ValueError('Too many source references.')
        return {'sources':[store.source(i) for i in values if i]}

    @app.post('/api/register-folder')
    def folder_register(body: dict):
        folder = absolute_path(str(body.get('path', '')), directory=True)
        accepted = {x for tool in TOOLS.values() for x in tool['accept']}
        paths = []
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() in accepted:
                paths.append(path)
            if len(paths) > 500:
                break
        return {'sources':[store.register(p) for p in paths[:500]], 'truncated':len(paths)>500}

    @app.post('/api/pick')
    def pick(body: Pick):
        if not picker_lock.acquire(blocking=False):
            raise ValueError('A native dialog is already open. Check behind the browser window.')
        try:
            picker_python = str(Path(sys.executable).with_name('python.exe')) if os.name == 'nt' else sys.executable
            p = subprocess.run([picker_python, str(ROOT/'scripts'/'native_picker.py'), body.kind],
                capture_output=True, encoding='utf-8', errors='replace', timeout=300, env={**os.environ, 'PYTHONUTF8':'1'}, **no_window())
            if p.returncode:
                raise ValueError('Native dialog could not open. Use Paste paths or browser drop instead. ' + p.stderr[-300:])
            paths = json.loads(p.stdout)['paths']
            if body.kind == 'files':
                return {'sources':[store.register(absolute_path(p)) for p in paths]}
            return {'paths':paths}
        except subprocess.TimeoutExpired as exc:
            raise ValueError('The native dialog timed out. Open it again when ready.') from exc
        finally:
            picker_lock.release()

    @app.post('/api/upload')
    async def upload(request: Request, name: str):
        filename = safe_name(name)
        # Extensions outside the tool allowlist are not useful staging uploads.
        allowed = {x for tool in TOOLS.values() for x in tool['accept']}
        if Path(filename).suffix.lower() not in allowed:
            raise ValueError('That file type is not supported by the installed tools.')
        folder = store.data / 'staging' / uuid.uuid4().hex
        folder.mkdir()
        path = folder / filename
        size = 0
        try:
            with path.open('xb') as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 50 * 1024**3:
                        raise ValueError('Browser staging is limited to 50 GB per file. Use Native browse for larger files.')
                    f.write(chunk)
            if size == 0:
                raise ValueError('The dropped file is empty.')
            return {'source':store.register(path, 'staged')}
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def destination(spec, source, settings):
        if spec.mode == 'source':
            if not source:
                raise ValueError('This tool has no source folder. Choose a custom or default export folder.')
            if source['mode'] != 'native':
                raise ValueError('The original folder is unknown for a browser drop. Use Native browse, Paste paths, or choose the original folder as a custom destination.')
            folder = Path(source['path']).parent
        elif spec.mode == 'custom':
            folder = absolute_path(spec.directory, directory=True, must_exist=False)
        elif spec.mode == 'last':
            if not settings['last_output']:
                raise ValueError('There is no last-used folder yet. Choose a custom folder first.')
            folder = absolute_path(settings['last_output'], directory=True, must_exist=False)
        else:
            folder = absolute_path(settings['default_output'], directory=True, must_exist=False)
        if spec.subfolder:
            if spec.subfolder != safe_name(spec.subfolder):
                raise ValueError('Subfolder must be a simple folder name, not a path.')
            folder = folder / spec.subfolder
        folder.mkdir(parents=True, exist_ok=True)
        # Fail before enqueuing when output is not writable.
        test = folder / ('.forge-write-test-' + uuid.uuid4().hex)
        try:
            with test.open('xb') as f:
                f.write(b'')
        finally:
            test.unlink(missing_ok=True)
        return folder.resolve()

    @app.post('/api/jobs')
    def enqueue(body: JobRequest):
        try:
            options = OPTION_MODELS[body.tool].model_validate(body.options).model_dump()
        except ValidationError as exc:
            raise ValueError('Invalid tool settings: ' + str(exc)) from exc
        settings = store.settings()
        entries = []
        if body.tool in {'transcribe','retune','metadata'}:
            if not body.sources:
                raise ValueError('Add at least one source file.')
            for id in dict.fromkeys(body.sources):
                source = store.source(id)
                p = absolute_path(source['path'])
                if p.suffix.lower() not in TOOLS[body.tool]['accept']:
                    raise ValueError(f'{p.name}: this format is not supported by {TOOLS[body.tool]["title"]}.')
                entries.append((source['name'], source, None))
        elif body.tool.startswith('download_'):
            if not body.urls:
                raise ValueError('Paste at least one HTTP or HTTPS media URL.')
            is_audio = body.tool == 'download_audio'
            if options['format'] not in ({'mp3','m4a','flac','wav'} if is_audio else {'mp4','mkv'}):
                raise ValueError('Select an output format suitable for this downloader.')
            if options['cookies']:
                options['cookies'] = str(absolute_path(options['cookies']))
            for url in dict.fromkeys(body.urls):
                url = url.strip()
                parts = urlsplit(url)
                if len(url) > 8000 or parts.scheme not in {'http','https'} or not parts.hostname or parts.username or parts.password:
                    raise ValueError('Only full HTTP/HTTPS URLs without embedded login credentials are accepted.')
                entries.append((parts.hostname + parts.path[:90], None, url))
        else:
            entries.append((options['name'], None, None))
        # Validate every source and destination before creating any jobs in a batch.
        planned = []
        batch_id=uuid.uuid4().hex
        for label, source, url in entries:
            folder = destination(body.output, source, settings)
            payload = {'tool':body.tool,'options':options,'settings':settings, 'destination':str(folder), 'source':source, 'url':url, 'batch_id':batch_id}
            if source:
                stat = Path(source['path']).stat()
                payload['source_stat'] = {'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
            planned.append((label, payload))
        with manager.lock:
            manager.check_accepting()
            jobs = [store.add_job(body.tool, label, payload) for label,payload in planned]
        if body.output.mode == 'custom':
            settings['last_output'] = str(absolute_path(body.output.directory, directory=True))
            store.set('settings', settings)
        return {'jobs':[public_job(j) for j in jobs]}

    @app.post('/api/jobs/{id}/cancel')
    def cancel(id: str):
        manager.cancel(id)
        return {'ok':True}

    @app.post('/api/jobs/{id}/retry')
    def retry(id: str):
        return public_job(manager.retry(id))

    @app.post('/api/queue')
    def queue(body: QueueAction):
        settings = store.settings()
        settings['queue_paused'] = body.paused
        store.set('settings', settings)
        return {'paused':body.paused}

    @app.post('/api/settings')
    def save_settings(body: Settings):
        values = body.model_dump()
        if values['default_output']:
            absolute_path(values['default_output'], directory=True, must_exist=False)
        # Queue pause state and last folder are not overwritten by a stale settings form.
        current = store.settings()
        values.update(queue_paused=current['queue_paused'], last_output=current['last_output'])
        store.set('settings', values)
        return {'ok':True}

    @app.post('/api/passwords')
    def generate(body: Password):
        return {'passwords':passwords(**body.model_dump()), 'persisted':False}

    @app.post('/api/presets')
    def preset(body: Preset):
        if body.tool not in OPTION_MODELS or body.tool == 'qr':
            raise ValueError('Presets are supported for media and download settings, not secret values or QR contents.')
        options = OPTION_MODELS[body.tool].model_validate(body.options).model_dump()
        if 'cookies' in options:
            options['cookies'] = ''
        values = [p for p in store.get('presets', []) if not (p['name']==body.name and p['tool']==body.tool)]
        values.append({'tool':body.tool,'name':body.name,'options':options,'output':body.output.model_dump()})
        store.set('presets', values[-100:])
        return {'presets':values}

    @app.post('/api/updates/check')
    def updates():
        return update_check(force=True)

    @app.get('/api/jobs/{id}/outputs/{index}')
    def output_file(id: str, index: int):
        job = store.job(id)
        if not 0 <= index < len(job['outputs']):
            raise HTTPException(404)
        item = job['outputs'][index]
        path = Path(item['path'])
        if not path.is_file():
            raise ValueError('This output has been moved or deleted.')
        return FileResponse(path, filename=path.name)

    @app.post('/api/jobs/{id}/reveal/{index}')
    def reveal(id: str, index: int):
        job = store.job(id)
        if not 0 <= index < len(job['outputs']):
            raise ValueError('Output not found.')
        path = Path(job['outputs'][index]['path'])
        if not path.is_file():
            raise ValueError('Output was moved or deleted.')
        if os.name != 'nt':
            return {'path':str(path),'opened':False}
        subprocess.Popen(['explorer.exe', '/select,', str(path)], **no_window())
        return {'opened':True}

    @app.post('/api/staging/clear')
    def clear_staging():
        with manager.lock:
            in_use = {j['payload']['source']['path'] for j in store.jobs(limit=100000) if j['status'] in {'queued','running','finalizing','cancelling'} and j['payload'].get('source')}
            count = 0
            for folder in (store.data/'staging').iterdir():
                if folder.is_dir() and not any(str(p) in in_use for p in folder.iterdir()):
                    shutil.rmtree(folder)
                    count += 1
            return {'removed':count, 'note':'Only unused staging copies were removed. Native originals were not touched.'}

    @app.post('/api/shutdown')
    def shutdown():
        settings = store.settings()
        settings['queue_paused'] = True
        store.set('settings', settings)
        if app.state.shutdown:
            app.state.shutdown()
        return {'ok':True, 'message':'Service stopping; unfinished jobs require a retry.'}

    from .v2_api import mount
    mount(app,store,manager,sessions,destination,token)
    from .update_manager import mount as mount_updates
    mount_updates(app,store,manager,ROOT)
    return app
