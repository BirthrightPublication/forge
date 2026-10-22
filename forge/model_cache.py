"""Persistent, revision-aware Whisper cache. Only approved model repositories are used."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from .core import json_write

REPOSITORIES = {name: 'Systran/faster-whisper-' + name
                for name in ('tiny', 'base', 'small', 'medium', 'large-v3')}
REPOSITORIES['large-v3-turbo'] = 'mobiuslabsgmbh/faster-whisper-large-v3-turbo'
REQUIRED = ('config.json', 'model.bin', 'tokenizer.json')
PATTERNS = [*REQUIRED, 'vocabulary.*', 'preprocessor_config.json']
REVISION = re.compile(r'^[0-9a-f]{40}$')


@contextlib.contextmanager
def cache_lock(path: Path, progress=lambda *a, **k: None, timeout=3600):
    """OS-owned lock, automatically released on cancellation or a killed worker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b'0')
            handle.flush()
        started = time.monotonic()
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() - started > timeout:
                    raise RuntimeError('Timed out waiting for the model cache. Retry after the current download finishes.')
                progress(0, 'Waiting for the selected model download', stage='Model download')
                time.sleep(.25)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)


def complete(path: Path) -> bool:
    try:
        if not all((path / name).is_file() and (path / name).stat().st_size > 0 for name in REQUIRED):
            return False
        return all(isinstance(json.loads((path / name).read_text('utf-8')), dict)
                   for name in ('config.json', 'tokenizer.json'))
    except (OSError, ValueError):
        return False


def snapshots(data: Path, model: str) -> list[Path]:
    repo = REPOSITORIES[model]
    root = data / 'models' / ('models--' + repo.replace('/', '--')) / 'snapshots'
    return sorted((p for p in root.glob('*') if REVISION.fullmatch(p.name) and complete(p)
                   and not (data / 'models' / 'forge-receipts' / model / (p.name + '.pending.json')).exists()),
                  key=lambda p: p.name)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def inventory(data: Path) -> list[dict]:
    selected = _read(data / 'models' / 'forge-selected.json')
    result = []
    for name, repo in REPOSITORIES.items():
        entries = [{'revision': p.name, 'bytes': sum(f.stat().st_size for f in p.iterdir() if f.is_file())}
                   for p in snapshots(data, name)]
        result.append({'model': name, 'repository': repo, 'cached': bool(entries),
                       'revisions': entries, 'selected': selected.get(name, ''),
                       'bytes': sum(e['bytes'] for e in entries),
                       'note': 'Cache presence only; not an inference test.'})
    return result


def select_revision(data: Path, model: str, revision: str):
    if model not in REPOSITORIES or not REVISION.fullmatch(revision):
        raise ValueError('Choose an approved model and immutable revision.')
    if revision not in {p.name for p in snapshots(data, model)}:
        raise ValueError('Install and verify this revision before selecting it.')
    with cache_lock(data / 'models' / '.forge-selection.lock'):
        values = _read(data / 'models' / 'forge-selected.json')
        values[model] = revision
        json_write(data / 'models' / 'forge-selected.json', values)


def _hash(path: Path, algorithm='sha256') -> str:
    digest = hashlib.new(algorithm)
    if algorithm == 'sha1':
        digest.update(f'blob {path.stat().st_size}\0'.encode())
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _verified(path: Path, receipt: dict) -> bool:
    if not complete(path):
        return False
    for name, metadata in receipt.get('files', {}).items():
        file = path / name
        try:
            stat = file.stat()
            if stat.st_size != metadata['size']:
                return False
            # Rehash changed files. Stable verified files avoid rereading gigabytes per job.
            if stat.st_mtime_ns != metadata['mtime_ns'] and _hash(file) != metadata['sha256']:
                return False
        except (OSError, KeyError):
            return False
    return bool(receipt.get('files'))


@contextlib.contextmanager
def transfer_progress(cache, repo, siblings, model, progress):
    """Observe publisher-cache bytes when sizes/blob identities are available.

    Progress is deliberately not a percentage of the whole transcription job.
    Bytes already staged by an interrupted transfer count toward model completion,
    but not toward this transfer's measured download speed.
    """
    blobs = cache / ('models--' + repo.replace('/', '--')) / 'blobs'
    expected = []
    for item in siblings:
        if item.rfilename not in REQUIRED and not item.rfilename.startswith('vocabulary.') and item.rfilename != 'preprocessor_config.json':
            continue
        lfs = item.lfs
        identity = (lfs.get('sha256') if isinstance(lfs, dict) else getattr(lfs, 'sha256', None)) if lfs else getattr(item, 'blob_id', None)
        size = getattr(item, 'size', None)
        if identity and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', identity) and isinstance(size, int) and size > 0:
            expected.append((blobs / identity, size))
    total = sum(size for _, size in expected)
    def amount():
        count = 0
        for path, size in expected:
            for candidate in (path, path.with_name(path.name + '.incomplete')):
                try:
                    count += min(size, candidate.stat().st_size)
                    break
                except OSError:
                    pass
        return count
    initial, started, stop = amount(), time.monotonic(), threading.Event()
    def monitor():
        while not stop.wait(.5):
            downloaded = amount()
            rate = max(0, downloaded - initial) / max(.001, time.monotonic() - started)
            progress(0, f'Downloading {model}: {downloaded:,} / {total:,} bytes', stage='Model download',
                     speed=f'{rate / 1048576:.1f} MB/s' if rate else '',
                     eta_seconds=(total - downloaded) / rate if rate else None)
    thread = threading.Thread(target=monitor, daemon=True) if total else None
    if thread:
        thread.start()
    try:
        yield
    finally:
        stop.set()
        if thread:
            thread.join(timeout=2)


def ensure_model(data: Path, model: str, progress=lambda *a, **k: None,
                 revision='', *, download=True) -> Path:
    if model not in REPOSITORIES or (revision and not REVISION.fullmatch(revision)):
        raise ValueError('Unknown model or revision.')
    cache = data / 'models'
    with cache_lock(cache / ('.forge-' + model + '.lock'), progress):
        selected = _read(cache / 'forge-selected.json').get(model)
        if selected and (not isinstance(selected, str) or not REVISION.fullmatch(selected)):
            raise ValueError('The saved model revision is invalid. Select a cached revision in Updates.')
        candidates = snapshots(data, model)
        chosen = revision or selected or (candidates[-1].name if candidates else '')
        repo = REPOSITORIES[model]
        root = cache / ('models--' + repo.replace('/', '--')) / 'snapshots'
        receipt_path = cache / 'forge-receipts' / model / (chosen + '.json')
        receipt = _read(receipt_path)
        if chosen and (root / chosen) in candidates:
            path = root / chosen
            if _verified(path, receipt):
                return path
            if not receipt:
                # Adopt a compatible existing cache without relocating or redownloading it.
                progress(0, 'Verifying existing model cache', stage='Model verification')
                _record(path, receipt_path, repo, 'Existing local snapshot; upstream integrity not rechecked')
                select_revision(data, model, chosen)
                return path
        if not download or os.getenv('HF_HUB_OFFLINE', '').lower() in {'1', 'true', 'yes'}:
            available = ', '.join(item['model'] for item in inventory(data) if item['cached']) or 'none'
            raise RuntimeError('Selected model is unavailable offline. Cached models: ' + available + '. Choose a cached model or reconnect and retry.')
        try:
            from huggingface_hub import HfApi, snapshot_download
            from tqdm.auto import tqdm

            class DownloadProgress(tqdm):
                def update(self, n=1):
                    result = super().update(n)
                    if self.unit == 'B' and self.total:
                        rate = self.format_dict.get('rate') or 0
                        progress(0, f'Downloading {model}: {self.n:,} / {self.total:,} bytes',
                                 stage='Model download', speed=f'{rate / 1048576:.1f} MB/s' if rate else '',
                                 eta_seconds=(self.total - self.n) / rate if rate else None)
                    return result

            progress(0, 'Resolving approved model revision', stage='Model download')
            info = HfApi(token=False).model_info(repo, revision=chosen or 'main', files_metadata=True, timeout=15)
            if not REVISION.fullmatch(info.sha):
                raise ValueError('Publisher did not return an immutable model revision.')
            chosen = info.sha
            receipt_path = cache / 'forge-receipts' / model / (chosen + '.json')
            pending = receipt_path.with_suffix('.pending.json')
            repair = bool(receipt) or _read(pending).get('status') == 'integrity_failed'
            json_write(pending, {'repository': repo, 'revision': chosen, 'started': time.time()})
            with transfer_progress(cache, repo, info.siblings, model, progress):
                path = Path(snapshot_download(repo, revision=chosen, cache_dir=str(cache),
                            allow_patterns=PATTERNS, tqdm_class=DownloadProgress, max_workers=2,
                            force_download=repair, etag_timeout=15, token=False))
            if not complete(path):
                raise ValueError('The downloaded model is incomplete. Retry resumes the cache download.')
            progress(0, 'Verifying downloaded model files', stage='Model verification')
            checked = set()
            for sibling in info.siblings:
                name = sibling.rfilename
                if name not in REQUIRED and not (name.startswith('vocabulary.') or name == 'preprocessor_config.json'):
                    continue
                if '/' in name or '\\' in name:
                    raise ValueError('Unexpected model file path.')
                file = path / name
                lfs = sibling.lfs
                expected = (lfs.get('sha256') if isinstance(lfs, dict) else getattr(lfs, 'sha256', None)) if lfs else sibling.blob_id
                if not expected or _hash(file, 'sha256' if lfs else 'sha1') != expected:
                    json_write(pending, {'repository': repo, 'revision': chosen, 'status': 'integrity_failed'})
                    raise ValueError('Model integrity verification failed for ' + name)
                checked.add(name)
            if not set(REQUIRED) <= checked:
                raise ValueError('Publisher integrity metadata is incomplete.')
            _record(path, receipt_path, repo, 'Verified against immutable publisher file hashes')
            pending.unlink(missing_ok=True)
            select_revision(data, model, chosen)
            return path
        except Exception as exc:
            raise RuntimeError('Model installation did not complete. Existing models are kept; retry when online or choose a cached model. ' + str(exc)[:400]) from exc


def _record(path: Path, receipt: Path, repo: str, verification: str):
    files = {p.name: {'size': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns, 'sha256': _hash(p)}
             for p in path.iterdir() if p.is_file()}
    json_write(receipt, {'repository': repo, 'revision': path.name, 'verified': time.time(),
                         'verification': verification, 'files': files})
