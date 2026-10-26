"""Optional local Underdog recognition. No Suno Bark code or speech synthesis.

Installations resolve publisher identities once, verify every permitted data file,
and retain immutable receipts. No downloaded Python, pickle or remote model code is
loaded. Runtime dependencies live outside FORGE's faster-whisper environment.
"""
from __future__ import annotations

import array
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
import wave
from urllib.parse import quote
from urllib.request import Request, urlopen

from .core import ROOT, executable, json_write, no_window
from .model_cache import cache_lock

MODELS = {
    'bark-3.0': ('Underdog-Bark-3.0', 'Bark 3.0', False),
    'bark-3.0-plus': ('Underdog-Bark-3.0-Plus', 'Bark 3.0 Plus', False),
    'bark-3.0-pro': ('Underdog-Bark-3.0-Pro', 'Bark 3.0 Pro', False),
    'bark-3.0-clean': ('Underdog-Bark-3.0-Clean', 'Bark 3.0 Clean', True),
}
# Immutable publisher snapshots exercised by the V0.8 native acceptance run.
# Advanced explicitly approved installs may select another full commit; ordinary
# installs never track a moving publisher branch.
APPROVED_REVISIONS = {
    'bark-3.0': '85db2ad18ab41e534a222ee253c29191d77ed919',
    'bark-3.0-plus': 'd4fab2f8b95681e5ad3cfb4e8c5f4e0c009b3deb',
    'bark-3.0-pro': '4b5c51e04aa8d16dda9ecdff71bc29f7360149e4',
    'bark-3.0-clean': '6d34ed8639b2b9a33e764acf06748519bbb950cf',
}
REVISION = re.compile(r'^[0-9a-f]{40}$')
PROCESSOR_REPO = 'nvidia/parakeet-tdt-0.6b-v3'
PROCESSOR_REVISION = '541d1f99c6b0c3cd0b11a95167540bb8edefd82b'
DATA_NAMES = {'model.bark', 'config.json', 'generation_config.json', 'preprocessor_config.json',
              'processor_config.json', 'tokenizer.json', 'tokenizer_config.json',
              'special_tokens_map.json', 'vocab.json', 'vocab.txt', 'merges.txt', 'LICENSE',
              'LICENSE.txt', 'NOTICE', 'README.md', 'model.safetensors.index.json'}
MODEL_LIMIT = 8 * 1024**3
CC_BY_4_URL = 'https://creativecommons.org/licenses/by/4.0/'


def _hash(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024**2), b''):
            value.update(block)
    return value.hexdigest()


def _read(path):
    try:
        value = json.loads(Path(path).read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def runtime_python(data):
    state = _read(Path(data) / 'models' / 'bark' / 'runtime.json')
    identity = state.get('id', '')
    if not re.fullmatch(r'[a-f0-9]{32}', identity):
        return None
    env = Path(data) / 'environments' / 'bark' / identity
    python = env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    return python if python.is_file() else None


def _receipt(data, model):
    receipt = _read(Path(data) / 'models' / 'bark' / model / 'selected.json')
    revision = receipt.get('revision', '')
    if not REVISION.fullmatch(revision):
        return {}
    return receipt


def _verified(data, model, *, rehash=False):
    receipt = _receipt(data, model)
    base = Path(data) / 'models' / 'bark' / model
    if receipt.get('repository') != 'ConwayResearch/' + MODELS[model][0] or not receipt.get('files'):
        return False
    try:
        for relative, meta in receipt['files'].items():
            path = base / relative
            if not path.resolve().is_relative_to(base.resolve()) or path.is_symlink():
                return False
            stat = path.stat()
            if stat.st_size != meta['size'] or (rehash or stat.st_mtime_ns != meta['mtime_ns']) and _hash(path) != meta['sha256']:
                return False
        return any(name.endswith('/model.bark') or name.endswith('.safetensors') for name in receipt['files'])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def catalog(data):
    result = []
    runtime = runtime_python(data)
    for model, (repo, title, clean) in MODELS.items():
        receipt = _receipt(data, model)
        cached = _verified(data, model)
        result.append({'id': model, 'model': model, 'engine': 'bark', 'label': title,
                       'repository': 'ConwayResearch/' + repo, 'language': 'en',
                       'installed': bool(cached and runtime), 'cached': cached, 'runtime_installed': bool(runtime),
                       'revision': receipt.get('revision', ''), 'available_revision': APPROVED_REVISIONS[model], 'non_verbatim': clean,
                       'download_bytes': sum(f.get('size', 0) for f in receipt.get('files', {}).values()) or None,
                       'capabilities': {'transcribe': True, 'languages': ['en'], 'translate': False,
                                        'timestamps': False, 'diarization': False, 'devices': ['cpu', 'cuda']},
                       'inference_verified': False,
                       'license': 'CC-BY-4.0; retain publisher model card/NOTICE and any LICENSE',
                       'note': 'Separate optional runtime. Download size is not working RAM/VRAM. '
                               + ('Clean rewrites fillers and is non-verbatim.' if clean else 'Text-only; use Whisper for timing, translation or speaker labels.')})
    return result


def validate_options(options):
    result = dict(options)
    model = result.get('model', 'bark-3.0-pro')
    if model not in MODELS:
        raise ValueError('Choose a registered Bark 3.0 recognition model.')
    if result.get('language', '') not in ('', 'en'):
        raise ValueError('Bark recognition supports English only. Choose Whisper for other languages.')
    if result.get('task', 'transcribe') != 'transcribe':
        raise ValueError('Bark cannot translate. Choose Whisper for translation.')
    if result.get('timestamps', 'none') != 'none' or result.get('diarization'):
        raise ValueError('Bark timing/alignment and speaker labeling are not verified. Choose text-only output or Whisper.')
    formats = result.get('formats', ['txt', 'json'])
    if not isinstance(formats, list) or not formats or any(v not in ('txt', 'json') for v in formats):
        raise ValueError('Bark supports TXT and JSON only until a timing/alignment workflow is verified.')
    if result.get('vocabulary'):
        raise ValueError('Bark does not support Whisper vocabulary prompts.')
    if result.get('style', 'raw') != 'raw':
        raise ValueError('Choose the explicitly non-verbatim Bark Clean model for cleaned recognition.')
    device = result.get('device', 'cpu')
    if device not in ('auto', 'cpu', 'cuda'):
        raise ValueError('Choose CPU, CUDA or Auto.')
    try:
        chunk = float(result.get('chunk_seconds', 25))
        overlap = float(result.get('overlap_seconds', 0))
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Bark chunk and overlap durations must be finite numbers.') from None
    if not math.isfinite(chunk) or not 5 <= chunk <= 30 or not math.isfinite(overlap) or not 0 <= overlap <= min(2, chunk / 4):
        raise ValueError('Bark chunks must be 5–30 seconds with 0–2 seconds of overlap.')
    revision = result.get('model_revision', '')
    if revision and not REVISION.fullmatch(revision):
        raise ValueError('Model revision must be an immutable publisher commit.')
    return {**result, 'engine': 'bark', 'model': model, 'language': 'en', 'task': 'transcribe',
            'timestamps': 'none', 'formats': list(dict.fromkeys(formats)), 'device': device,
            'chunk_seconds': chunk, 'overlap_seconds': overlap}


def _permitted(name, *, processor=False):
    # Exclude arbitrary nested paths, Python, pickle and native extensions.
    if not isinstance(name, str) or '/' in name or '\\' in name or name.startswith('.'):
        return False
    return name in DATA_NAMES or (not processor and re.fullmatch(r'model(?:-\d+-of-\d+)?\.safetensors', name) is not None)


def _publisher_info(repo, revision):
    if not REVISION.fullmatch(revision or ''):
        raise ValueError('Publisher metadata lookup requires an immutable model revision.')
    url = 'https://huggingface.co/api/models/' + repo + '/revision/' + quote(revision, safe='') + '?blobs=true'
    with urlopen(Request(url, headers={'User-Agent': 'FORGE Bark model installer'}), timeout=15) as response:
        raw = response.read(16 * 1024**2 + 1)
    if len(raw) > 16 * 1024**2:
        raise ValueError('Publisher metadata is oversized.')
    info = json.loads(raw)
    if not isinstance(info, dict) or not isinstance(info.get('sha'), str) or not isinstance(info.get('siblings'), list):
        raise ValueError('Publisher metadata is invalid.')
    if info['sha'] != revision:
        raise ValueError('Publisher did not return the requested immutable model revision.')
    return info


def _snapshot(repo, revision, destination, *, processor=False, progress=lambda *a, **k: None):
    info = _publisher_info(repo, revision)
    if not REVISION.fullmatch(info['sha']) or revision and info['sha'] != revision:
        raise ValueError('Publisher did not return the requested immutable model revision.')
    selected = [v for v in info['siblings'] if _permitted(v.get('rfilename', ''), processor=processor)]
    if not selected:
        raise ValueError('Publisher has no compatible data-only model artifacts.')
    total = sum(v.get('size', 0) or 0 for v in selected)
    if total > MODEL_LIMIT:
        raise ValueError('Model exceeds the approved 8 GiB download budget.')
    destination.mkdir(parents=True, exist_ok=True)
    files = {}
    for sibling in selected:
        name = sibling['rfilename']
        lfs = sibling.get('lfs')
        sha = lfs.get('sha256') or lfs.get('oid') if isinstance(lfs, dict) else None
        blob = sibling.get('blobId') or sibling.get('blob_id')
        if not ((sha and re.fullmatch(r'[a-f0-9]{64}', sha)) or (not lfs and blob and re.fullmatch(r'[a-f0-9]{40}', blob))):
            raise ValueError('Publisher file identity is unavailable for ' + name)
        progress(0, 'Downloading ' + name, stage='Bark model download')
        downloaded = destination / (name + '.download-' + uuid.uuid4().hex)
        try:
            url = 'https://huggingface.co/' + repo + '/resolve/' + info['sha'] + '/' + name
            with urlopen(Request(url, headers={'User-Agent': 'FORGE Bark model installer'}), timeout=30) as response, downloaded.open('xb') as output:
                amount = 0
                while block := response.read(1024**2):
                    amount += len(block)
                    if amount > MODEL_LIMIT:
                        raise ValueError('Downloaded model artifact exceeds its size budget.')
                    output.write(block)
                    progress(0, f'Downloading {name}: {amount:,} bytes', stage='Bark model download', downloaded_bytes=amount, total_bytes=sibling.get('size'))
            size = downloaded.stat().st_size
        except BaseException:
            downloaded.unlink(missing_ok=True)
            raise
        if not 0 < size <= MODEL_LIMIT:
            raise ValueError('Model artifact is empty or oversized.')
        digest = _hash(downloaded)
        if sha:
            valid = digest == sha
        else:
            check = hashlib.sha1(f'blob {size}\0'.encode())
            with downloaded.open('rb') as f:
                for block in iter(lambda: f.read(1024**2), b''):
                    check.update(block)
            valid = check.hexdigest() == blob
        if not valid:
            downloaded.unlink(missing_ok=True)
            raise ValueError('Publisher model integrity verification failed for ' + name)
        final = destination / name
        os.replace(downloaded, final)
        stat = final.stat()
        files[name] = {'sha256': digest, 'size': size, 'mtime_ns': stat.st_mtime_ns}
    return info['sha'], files


def _licensing_evidence(info, directory, files):
    """Retain real publisher evidence, including NOTICE-only licensed layouts.

    Bark's official repositories declare CC BY 4.0 in the immutable model card
    and verified NOTICE; they do not currently supply a separate LICENSE file.
    This validates those publisher declarations without inventing such a file.
    """
    card = info.get('cardData')
    if not isinstance(card, dict) or card.get('license') != 'cc-by-4.0':
        raise ValueError('Publisher model metadata must explicitly declare cc-by-4.0; review changed licensing before installation.')

    def document(name, limit):
        metadata = files.get(name)
        path = Path(directory) / name
        if (not isinstance(metadata, dict) or path.is_symlink() or not path.is_file()
                or not 0 < path.stat().st_size <= limit
                or path.stat().st_size != metadata.get('size')
                or _hash(path) != metadata.get('sha256')):
            raise ValueError('Publisher licensing document is missing or unverified: ' + name)
        try:
            return path.read_text(encoding='utf-8')
        except UnicodeError as error:
            raise ValueError('Publisher licensing document must be UTF-8 text: ' + name) from error

    notice = document('NOTICE', 128 * 1024)
    licenses = sorted({'LICENSE', 'LICENSE.txt'} & set(files))
    for name in licenses:
        document(name, 2 * 1024**2)
    if not licenses:
        if not re.search(r'(?<![\w:/.-])' + re.escape(CC_BY_4_URL) + r'(?=$|[\s<>)\]}.,;])', notice):
            raise ValueError('Publisher NOTICE must explicitly reference the CC BY 4.0 license URL when no LICENSE file is supplied.')
        document('README.md', 2 * 1024**2)
    elif 'README.md' in files:
        document('README.md', 2 * 1024**2)
    return {'license': 'CC-BY-4.0', 'publisher_metadata_license': card['license'],
            'source': 'publisher-license-file-and-notice' if licenses else 'publisher-model-card-and-notice',
            'license_url': CC_BY_4_URL, 'license_files': licenses,
            'notice_sha256': files['NOTICE']['sha256'],
            'model_card_sha256': files.get('README.md', {}).get('sha256')}


def install(model, data, consent=False, revision='', device='cpu', progress=lambda *a, **k: None):
    if not consent:
        raise ValueError('Explicit approval is required to install a Bark model and its separate environment.')
    if model not in MODELS or revision and not REVISION.fullmatch(revision):
        raise ValueError('Unknown Bark model or invalid revision.')
    revision = revision or APPROVED_REVISIONS[model]
    if device not in ('cpu', 'cuda'):
        raise ValueError('Approve a CPU or CUDA 12.8 runtime installation.')
    data = Path(data)
    base = data / 'models' / 'bark' / model
    with cache_lock(data / 'models' / 'bark' / '.install.lock', progress):
        runtime_state = _read(data / 'models' / 'bark' / 'runtime.json')
        runtime_compatible = bool(runtime_python(data)) and (device == 'cpu' or runtime_state.get('device') == 'cuda')
        if _verified(data, model, rehash=True) and runtime_compatible and _receipt(data, model)['revision'] == revision:
            return {**_receipt(data, model), 'cached': True, 'inference_verified': False}
        if os.getenv('HF_HUB_OFFLINE', '').lower() in ('1', 'true', 'yes'):
            raise RuntimeError('Bark installation needs a connection. Existing verified models remain available offline.')
        try:
            # Resolve immutable identities before touching any existing runtime.
            info = _publisher_info('ConwayResearch/' + MODELS[model][0], revision)
            resolved = info['sha']
            if resolved != revision:
                raise ValueError('Publisher did not return the requested immutable model revision.')
            target = base / resolved
            resolved, model_files = _snapshot('ConwayResearch/' + MODELS[model][0], resolved, target, progress=progress)
            _, processor_files = _snapshot(PROCESSOR_REPO, PROCESSOR_REVISION, target / 'processor', processor=True, progress=progress)
            if not ((target / 'model.bark').is_file() or any(target.glob('*.safetensors'))):
                raise ValueError('No supported Bark weights were provided by the publisher.')
            if not (target / 'processor' / 'tokenizer.json').is_file():
                raise ValueError('Pinned offline Parakeet processor is incomplete.')
            licensing = _licensing_evidence(info, target, model_files)
            if not runtime_compatible:
                identity = uuid.uuid4().hex
                env = data / 'environments' / 'bark' / identity
                progress(0, 'Creating separate Bark environment', stage='Bark runtime installation')
                subprocess.run([sys.executable, '-m', 'venv', str(env)], check=True, timeout=180, capture_output=True, **no_window())
                python = env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
                index = 'https://download.pytorch.org/whl/' + ('cu128' if device == 'cuda' else 'cpu')
                command = [str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '--timeout', '30', '--retries', '1']
                subprocess.run(command + ['torch==2.8.0', '--index-url', index], check=True, timeout=1200, capture_output=True, **no_window())
                subprocess.run(command + ['-r', str(ROOT / 'scripts' / 'requirements-bark.txt')], check=True, timeout=1200, capture_output=True, **no_window())
                subprocess.run([str(python), '-m', 'pip', 'check'], check=True, timeout=60, capture_output=True, **no_window())
                subprocess.run([str(python), '-c', 'from transformers import ParakeetForTDT; import constriction,numpy,torch'],
                               check=True, timeout=120, capture_output=True, **no_window())
                json_write(data / 'models' / 'bark' / 'runtime.json', {'id': identity, 'device': device, 'created': time.time()})
            files = {resolved + '/' + name: value for name, value in model_files.items()}
            files.update({resolved + '/processor/' + name: value for name, value in processor_files.items()})
            receipt = {'repository': 'ConwayResearch/' + MODELS[model][0], 'revision': resolved,
                       'processor_revision': PROCESSOR_REVISION, 'files': files, 'installed': time.time(),
                       'inference_verified': False, 'license': licensing['license'], 'licensing_evidence': licensing}
            json_write(base / 'selected.json', receipt)
            return receipt
        except Exception as error:
            if isinstance(error, (ValueError, RuntimeError)):
                raise
            raise RuntimeError('Bark installation did not complete. Check connectivity, available disk and the separate runtime; existing models and FORGE environments were preserved.') from None


def chunk_audio(source, directory, settings, *, chunk_seconds=25, overlap_seconds=0):
    """Decode once to disk; bounded chunks cut at low-energy windows, never timestamps."""
    ffmpeg = executable('ffmpeg', settings)
    if not ffmpeg:
        raise RuntimeError('FFmpeg is required to decode Bark input.')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    decoded = directory / 'decoded.wav'
    subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-n', '-protocol_whitelist', 'file,pipe',
                    '-i', str(source), '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(decoded)],
                   check=True, capture_output=True, timeout=1800, **no_window())
    chunks = []
    with wave.open(str(decoded), 'rb') as f:
        rate, frames = f.getframerate(), f.getnframes()
        maximum, overlap, start = int(chunk_seconds * rate), int(overlap_seconds * rate), 0
        while start < frames:
            end = min(frames, start + maximum)
            if end < frames:
                search = min(3 * rate, maximum // 3)
                f.setpos(end - search)
                pcm = array.array('h', f.readframes(search))
                if sys.byteorder != 'little':
                    pcm.byteswap()
                frame = rate // 10
                energies = [(sum(v * v for v in pcm[i:i+frame]), i) for i in range(0, len(pcm)-frame+1, frame)]
                if energies:
                    end = end - search + min(energies)[1] + frame // 2
            f.setpos(start)
            path = directory / f'chunk-{len(chunks):06}.wav'
            with wave.open(str(path), 'wb') as out:
                out.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
                out.writeframes(f.readframes(end - start))
            chunks.append({'index': len(chunks), 'path': str(path), 'audio_window': [start / rate, end / rate]})
            if end >= frames:
                break
            start = max(start + 1, end - overlap)
    if not chunks:
        raise ValueError('Input contains no audio frames.')
    return chunks


def assemble(chunks, *, overlapped=False):
    """Exact suffix/prefix dedup only on overlapped audio; original chunks retained."""
    text = ''
    for i, item in enumerate(chunks):
        if item.get('index') != i or not isinstance(item.get('text'), str):
            raise ValueError('Bark chunks are missing or out of sequence.')
        words = item['text'].split()
        previous = text.split()
        if overlapped:
            # Do not fuzzy-match/rewrite phrases. Small/repeated words stay intact.
            for size in range(min(30, len(previous), len(words)), 2, -1):
                if previous[-size:] == words[:size]:
                    words = words[size:]
                    break
        text = ' '.join(previous + words)
    return text


def word_error_rate(reference, hypothesis):
    """Case/punctuation-sensitive WER; the caller supplies a verified reference."""
    a, b = reference.split(), hypothesis.split()
    if not a:
        return 0.0 if not b else None
    row = list(range(len(b) + 1))
    for i, word in enumerate(a, 1):
        next_row = [i]
        for j, candidate in enumerate(b, 1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j-1] + (word != candidate)))
        row = next_row
    return row[-1] / len(a)


def transcribe(source, stage_dir, stem, options, data, settings):
    options = validate_options(options)
    python = runtime_python(data)
    model = options['model']
    if not python or not _verified(data, model, rehash=True):
        raise RuntimeError('Install and verify the selected Bark model in Speech Models before transcription. Whisper remains available.')
    receipt = _receipt(data, model)
    if options.get('model_revision') and options['model_revision'] != receipt['revision']:
        raise ValueError('The requested immutable Bark revision is not selected. Install/select it before running this preset.')
    work = Path(stage_dir) / ('bark-' + uuid.uuid4().hex)
    chunks = chunk_audio(source, work, settings, chunk_seconds=options['chunk_seconds'], overlap_seconds=options['overlap_seconds'])
    request = {'model_directory': str(Path(data) / 'models' / 'bark' / model / receipt['revision']),
               'device': options['device'], 'chunks': chunks, 'result': str(work / 'result.json')}
    json_write(work / 'request.json', request)
    started = time.monotonic()
    try:
        result = subprocess.run([str(python), str(ROOT / 'scripts' / 'bark_runtime.py'), str(work / 'request.json')],
                                capture_output=True, timeout=86400, **no_window())
        if result.returncode:
            json_write(Path(data) / 'logs' / ('bark-' + uuid.uuid4().hex + '.json'),
                       {'model': model, 'device': options['device'], 'error': result.stderr.decode('utf-8', errors='replace')[-8000:], 'time': time.time()})
            raise RuntimeError('Bark inference failed. Check the selected runtime/device and use CPU when CUDA is unavailable. No transcription was published.')
        report = _read(work / 'result.json')
        recognized = report.get('chunks', [])
        if len(recognized) != len(chunks):
            raise RuntimeError('Bark did not return every audio chunk; partial text was not published.')
        text = assemble(recognized, overlapped=options['overlap_seconds'] > 0)
        warnings = ['Bark output has no recognized word/segment timing. Audio windows are decoding boundaries, not subtitle alignment.']
        if MODELS[model][2]:
            warnings.append('Bark Clean output is non-verbatim: the model can remove fillers or rewrite spoken delivery.')
        if options['overlap_seconds']:
            warnings.append('Overlapping chunks use exact multiword boundary deduplication; review chunk text for boundary errors.')
        files = []
        if 'txt' in options['formats']:
            path = Path(stage_dir) / (stem + ('_non-verbatim' if MODELS[model][2] else '') + '.txt')
            path.write_text(text + '\n', encoding='utf-8'); files.append(path)
        if 'json' in options['formats']:
            path = Path(stage_dir) / (stem + '.json')
            json_write(path, {'text': text, 'segments': [], 'timing': 'unavailable',
                             'chunks': [{k: v for k, v in item.items() if k in {'index', 'text', 'audio_window'}} for item in recognized],
                             'engine': 'bark', 'model': model, 'model_revision': receipt['revision'],
                             'language': 'en', 'non_verbatim': MODELS[model][2], 'device': report.get('device'),
                             'processing_seconds': time.monotonic() - started, 'peak_process_memory_bytes': report.get('peak_process_memory_bytes'),
                             'peak_cuda_allocated_bytes': report.get('peak_cuda_allocated_bytes'),
                             'warnings': warnings})
            files.append(path)
        return files, warnings
    finally:
        shutil.rmtree(work, ignore_errors=True)
