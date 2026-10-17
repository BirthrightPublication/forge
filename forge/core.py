from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MEDIA = {'.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.mp4', '.mkv', '.mov', '.webm', '.avi', '.m4v', '.wma', '.aiff', '.aif', '.m4b'}
IMAGES = {'.jpg', '.jpeg', '.png', '.webp'}
CLEAN_MEDIA = {'.mp3', '.wav', '.flac', '.m4a', '.mp4', '.mkv', '.mov', '.webm', '.ogg', '.opus'}
TOOLS = {
    'transcribe': {'title': 'Transcribe', 'group': 'Media', 'description': 'Local speech recognition with segment and word timing.', 'accept': sorted(MEDIA)},
    'retune': {'title': '432Hz Conversion', 'group': 'Media', 'description': 'Retune audio or video without replacing the original.', 'accept': sorted(MEDIA)},
    'download_audio': {'title': 'Audio Download', 'group': 'Downloads', 'description': 'Download and extract audio from a supplied URL.', 'accept': []},
    'download_video': {'title': 'Video Download', 'group': 'Downloads', 'description': 'Download video using a fresh yt-dlp integration.', 'accept': []},
    'metadata': {'title': 'Metadata Wipe', 'group': 'Privacy', 'description': 'Create a metadata-reduced image or media copy.', 'accept': sorted(IMAGES | CLEAN_MEDIA)},
    'qr': {'title': 'QR Codes', 'group': 'Utilities', 'description': 'Generate PNG and SVG codes entirely on your computer.', 'accept': []},
    'password': {'title': 'Passwords', 'group': 'Utilities', 'description': 'Cryptographic passwords. Never written to job history.', 'accept': []},
}
DEFAULTS = {'workers': 2, 'ffmpeg': '', 'ffprobe': '', 'last_output': '', 'default_output': '',
            'daily_update_check': False, 'queue_paused': False}


def data_directory() -> Path:
    if os.getenv('FORGE_DATA_DIR'):
        return Path(os.environ['FORGE_DATA_DIR']).expanduser().resolve()
    base = Path(os.getenv('LOCALAPPDATA', str(Path.home() / '.local' / 'share')))
    return base / 'FORGE'


def json_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('x', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def safe_name(value: str, fallback: str = 'output') -> str:
    # Deliberately do not interpret user text as a path or command.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')[:160]
    if not name:
        name = fallback
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I):
        name = '_' + name
    return name


def absolute_path(value: str, *, directory: bool = False, must_exist: bool = True) -> Path:
    value = value.strip().strip('"')
    if not value or '\x00' in value:
        raise ValueError('Enter a complete local path.')
    if 'fakepath' in value.lower():
        raise ValueError('The browser hides this source path. Use Native browse or Copy as path in Explorer.')
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise ValueError('A full absolute path is required, not a relative path.')
    # Network shares are intentionally out of the first local-only build.
    if os.name == 'nt' and (value.startswith('\\\\') or value.startswith('//')):
        raise ValueError('UNC/network paths are not enabled in V0.1. Use a local or mounted drive.')
    p = p.resolve()
    if must_exist and not (p.is_dir() if directory else p.is_file()):
        raise ValueError('Directory not found.' if directory else 'File not found: ' + p.name)
    return p


def executable(name: str, settings: dict) -> str | None:
    configured = settings.get(name, '').strip()
    if configured:
        p = Path(configured.strip('"')).expanduser()
        if p.is_dir():
            p = p / (name + ('.exe' if os.name == 'nt' else ''))
        return str(p.resolve()) if p.is_file() else None
    candidates = [ROOT / 'tools' / (name + '.exe'), ROOT / 'tools' / 'ffmpeg' / 'bin' / (name + '.exe')]
    if os.name == 'nt':
        candidates += [Path('C:/ffmpeg/bin') / (name + '.exe'), Path('C:/yt-dlp') / (name + '.exe')]
    if name == 'ffprobe' and settings.get('ffmpeg'):
        f = Path(settings['ffmpeg'].strip('"'))
        candidates.insert(0, (f if f.is_dir() else f.parent) / ('ffprobe.exe' if os.name == 'nt' else 'ffprobe'))
    for p in candidates:
        if p.is_file():
            return str(p.resolve())
    return shutil.which(name)


def dependency_state(settings: dict) -> list[dict]:
    result = []
    for package, module, label in [('qrcode', 'qrcode', 'QR engine'), ('pillow', 'PIL', 'Image engine'),
                                  ('yt-dlp', 'yt_dlp', 'Download engine'), ('faster-whisper', 'faster_whisper', 'Transcription engine')]:
        installed = importlib.util.find_spec(module) is not None
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = ''
        result.append({'id': package, 'label': label, 'ready': installed, 'detail': version or 'Not installed'})
    for name in ('ffmpeg', 'ffprobe'):
        path = executable(name, settings)
        result.append({'id': name, 'label': name.upper() if name == 'ffmpeg' else 'FFprobe', 'ready': bool(path), 'detail': path or 'Set the executable path below'})
    js = shutil.which('deno') or shutil.which('node')
    result.append({'id': 'javascript', 'label': 'YouTube JS runtime', 'ready': bool(js), 'detail': js or 'Deno or Node may be needed for YouTube'})
    return result


def no_window() -> dict:
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def passwords(length: int = 24, count: int = 1, upper: bool = True, numbers: bool = True,
              symbols: str = '!@#$%^-_=+[]{}?', unicode_chars: bool = False,
              exclude_ambiguous: bool = True) -> list[str]:
    if not 8 <= length <= 256 or not 1 <= count <= 50:
        raise ValueError('Use 8–256 characters and 1–50 passwords.')
    if len(symbols) > 128 or any(ord(c) < 33 for c in symbols):
        raise ValueError('Custom characters must be printable, with no whitespace (maximum 128).')
    groups = [string.ascii_lowercase]
    if upper:
        groups.append(string.ascii_uppercase)
    if numbers:
        groups.append(string.digits)
    if symbols:
        groups.append(symbols)
    if unicode_chars:
        groups.append('äöüßÆØÅÇñ£¥')
    if exclude_ambiguous:
        groups = [''.join(c for c in g if c not in 'Il1O0o') for g in groups]
    groups = [g for g in groups if g]
    alphabet = ''.join(dict.fromkeys(''.join(groups)))
    result = []
    for _ in range(count):
        # Rejection sampling is unbiased among strings satisfying the selected groups.
        for attempt in range(100000):
            candidate = ''.join(secrets.choice(alphabet) for _ in range(length))
            if all(any(c in group for c in candidate) for group in groups):
                result.append(candidate)
                break
        else:
            raise ValueError('This character policy is impractical. Increase the length or simplify it.')
    return result


class Store:
    def __init__(self, data: Path):
        self.data = data.resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        for part in ('staging', 'work', 'models'):
            (self.data / part).mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.data / 'forge.sqlite3', check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, path TEXT NOT NULL, mode TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, tool TEXT NOT NULL, label TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, message TEXT NOT NULL, payload TEXT NOT NULL, outputs TEXT NOT NULL DEFAULT '[]', warnings TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL, updated REAL NOT NULL);
        ''')
        self.db.commit()
        if not self.get('settings'):
            self.set('settings', {**DEFAULTS, 'default_output': str(Path.home() / 'FORGE Exports')})
        with self.lock:
            interrupted = self.db.execute("UPDATE jobs SET status='interrupted', message='Service stopped. Retry restarts this job; existing outputs are kept.', updated=? WHERE status IN ('running','cancelling','finalizing')", (time.time(),)).rowcount
            self.db.commit()
            if interrupted:
                settings = self.settings()
                settings['queue_paused'] = True
                self.set('settings', settings)

    def get(self, key: str, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any):
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))
            self.db.commit()

    def settings(self):
        return {**DEFAULTS, **self.get('settings', {})}

    def register(self, p: Path, mode='native') -> dict:
        p = p.resolve()
        if not p.is_file():
            raise ValueError('The selected item is not a regular file.')
        item = {'id': uuid.uuid4().hex, 'path': str(p), 'mode': mode, 'name': p.name, 'size': p.stat().st_size, 'created': time.time()}
        with self.lock:
            self.db.execute('INSERT INTO sources VALUES (:id,:path,:mode,:name,:size,:created)', item)
            self.db.commit()
        return item

    def source(self, id: str) -> dict:
        with self.lock:
            row = self.db.execute('SELECT * FROM sources WHERE id=?', (id,)).fetchone()
        if not row:
            raise ValueError('Source is no longer available. Select the file again.')
        return dict(row)

    def add_job(self, tool: str, label: str, payload: dict) -> dict:
        now = time.time()
        job = {'id': uuid.uuid4().hex, 'tool': tool, 'label': label, 'status': 'queued', 'progress': 0, 'message': 'Waiting for a processing slot.', 'payload': payload, 'outputs': [], 'warnings': [], 'created': now, 'updated': now}
        payload['job_id'] = job['id']
        with self.lock:
            self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)', (job['id'], tool, label, 'queued', 0, job['message'], json.dumps(payload, ensure_ascii=False), '[]', '[]', now, now))
            self.db.commit()
        return job

    def job(self, id: str) -> dict:
        with self.lock:
            row = self.db.execute('SELECT * FROM jobs WHERE id=?', (id,)).fetchone()
        if not row:
            raise ValueError('Job not found.')
        return self._decode(row)

    @staticmethod
    def _decode(row):
        j = dict(row)
        for key in ('payload', 'outputs', 'warnings'):
            j[key] = json.loads(j[key])
        return j

    def jobs(self, status=None, limit=500) -> list[dict]:
        with self.lock:
            if status:
                rows = self.db.execute('SELECT * FROM jobs WHERE status=? ORDER BY created LIMIT ?', (status, limit)).fetchall()
            else:
                rows = self.db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT ?', (limit,)).fetchall()
        return [self._decode(row) for row in rows]

    def update_job(self, id: str, **fields):
        allowed = {'status', 'progress', 'message', 'outputs', 'warnings'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('Invalid job update.')
        values = [json.dumps(v, ensure_ascii=False) if k in {'outputs', 'warnings'} else v for k, v in fields.items()]
        with self.lock:
            self.db.execute('UPDATE jobs SET ' + ','.join(k+'=?' for k in fields) + ',updated=? WHERE id=?', (*values, time.time(), id))
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()
