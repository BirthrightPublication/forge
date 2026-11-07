"""Verified, isolated model packs for Dubbing Studio.

The application process never imports torch and never installs into an existing
FORGE/Whisper/speech environment. A quoted installation contains immutable model
revisions, publisher object hashes, binary wheel hashes and a compatible runtime.
Unreviewed model revisions remain unavailable rather than appearing Ready.
"""
from __future__ import annotations

import copy
from collections import deque
import csv
import fnmatch
import hashlib
import io
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from .core import ROOT, executable, json_write, no_window
from .model_cache import cache_lock

REVISION = re.compile(r'^[0-9a-f]{40}(?:[0-9a-f]{24})?$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')
COMPONENT_ID = re.compile(r'^[a-z0-9][a-z0-9.-]{1,80}$')
RUNTIME_ID = re.compile(r'^dubbing-[a-z0-9-]+-[a-f0-9]{16}$')
MANAGED_PYTHON = {
    'id': 'cpython-3.12.16-windows-x64-20261026', 'version': '3.12.16',
    'url': 'https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-x86_64-pc-windows-msvc-install_only.tar.gz',
    'size': 47113364,
    'sha256': '590dfcbe3a34c07301e6332e13e2188b98655cd072b08afa45a7998dc4c22bc5',
    'python_exe_sha256': '8434869ecb57caa30b8b7e6a400f71c216de1d6e6444e6338a501df6bd96193b',
    'license': 'Python Software Foundation and bundled dependency notices; retained in the managed runtime.',
}
QWEN_SOURCE = {
    'kind': 'wheel_source', 'package_prefix': 'qwen_tts/', 'name': 'qwen-tts API source',
    'version': '0.1.1', 'size': 113529,
    'url': 'https://files.pythonhosted.org/packages/31/01/95f3ae26f5b7eb2105f69ad2c15af5a28b9ebdd1258a0b27541f5ebd6225/qwen_tts-0.1.1-py3-none-any.whl',
    'sha256': '11a290d8dabc7ef91a90c54478c8ab19b3edb1d85c0882313721892bdc4af15d',
    'license': 'Apache-2.0',
}
SOX_SOURCE = {
    'kind': 'tar_source', 'package_prefix': 'sox/', 'name': 'python-sox import dependency',
    'version': '1.5.0', 'size': 63905,
    'url': 'https://files.pythonhosted.org/packages/3d/a2/d8e0d8fd7abf509ead4a2cb0fb24e5758b5330166bf9223d5cb9f98a7e8d/sox-1.5.0.tar.gz',
    'sha256': '12c7be5bb1f548d891fe11e82c08cf5f1a1d74e225298f60082e5aeb2469ada0',
    'license': 'BSD-3-Clause; no SoX executable is bundled or used by the 12 Hz adapter.',
}
CHATTERBOX_CODE_REVISION = 'ab55923c27757c90cfcc407baa57dc25a7d25217'
INFERENCE_TIMEOUT_SECONDS = 7200

# Model snapshots must be populated from reviewed publisher metadata, never
# guessed. Discovery can prepare that metadata, but does not activate a model.
MODEL_PINS: dict[str, str] = {}


def _part(id, repository, patterns, required, license):
    return {'id': id, 'repository': repository, 'revision': MODEL_PINS.get(repository, ''),
            'allow_patterns': patterns, 'required_files': required, 'license': license}


def _base_specs():
    qwen_patterns = ['*.json', '*.safetensors', '*.txt', '*.jinja', 'speech_tokenizer/*', 'LICENSE*', 'NOTICE*', 'README.md']
    qwen_required = ['config.json', 'generation_config.json', 'tokenizer_config.json', 'speech_tokenizer/config.json']
    result = [{
        'id': 'opus-en-ru', 'label': 'OPUS-MT · English → Russian', 'kind': 'translation',
        'family': 'core', 'recommended': True, 'requirements': 'scripts/requirements-dubbing-core.txt',
        'runner': 'scripts/dubbing_runtime.py', 'planning_memory_mb': 2000,
        'parts': [_part('translation', 'Helsinki-NLP/opus-mt-en-ru',
            ['config.json', 'generation_config.json', 'tokenizer_config.json', 'source.spm', 'target.spm', 'vocab.json',
             'model.safetensors', 'pytorch_model.bin', 'LICENSE*', 'NOTICE*', 'README.md'],
            ['config.json', 'source.spm', 'target.spm', 'vocab.json'], 'apache-2.0')],
        'capabilities': {'offline': True, 'source_language': 'en', 'target_language': 'ru',
                         'glossary': True, 'localization_model': False, 'instruction_model': False},
        'limitations': ['A compact translation model, not an instruction-following editor. Localized commentary remains editable and requires review.'],
        'sources': [QWEN_SOURCE, SOX_SOURCE], 'smoke': 'from transformers import MarianMTModel,MarianTokenizer',
    }]
    for size, memory in [('0.6b', 4500), ('1.7b', 9000)]:
        repository = 'Qwen/Qwen3-TTS-12Hz-' + size.replace('b', 'B') + '-Base'
        result.append({
            'id': 'qwen3-tts-' + size + '-base', 'label': 'Qwen3-TTS ' + size.replace('b', 'B') + ' Base',
            'kind': 'tts', 'family': 'core', 'recommended': size == '0.6b',
            'requirements': 'scripts/requirements-dubbing-core.txt', 'runner': 'scripts/dubbing_runtime.py',
            'planning_memory_mb': memory, 'parts': [_part('voice', repository, qwen_patterns, qwen_required, 'apache-2.0')],
            'capabilities': {'offline': True, 'languages': ['ru'], 'tasks': ['voice_clone'], 'reference': True,
                             'named_voices': False, 'voice_design': False, 'native_emotion': False},
            'limitations': ['A permitted reference recording is required. Voice identity and accent quality must be reviewed by listening.'],
            'sources': [QWEN_SOURCE, SOX_SOURCE],
            'smoke': 'from qwen_tts import Qwen3TTSModel; import soundfile',
        })
    from .model_cache import REPOSITORIES
    for name, repository in REPOSITORIES.items():
        result.append({
            'id': 'whisper-' + name, 'label': 'Whisper ' + name + ' · isolated dubbing runtime',
            'kind': 'asr', 'family': 'core', 'recommended': False, 'cpu_only': True,
            'requirements': 'scripts/requirements-dubbing-core.txt', 'runner': 'scripts/dubbing_runtime.py',
            'planning_memory_mb': 5000 if name.startswith('large') else 2000,
            'parts': [_part('asr', repository,
                ['config.json', 'model.bin', 'tokenizer.json', 'preprocessor_config.json', 'vocabulary.*', 'README.md', 'LICENSE*'],
                ['config.json', 'model.bin', 'tokenizer.json'], 'mit')],
            'capabilities': {'offline': True, 'timestamps': ['segment', 'word'], 'source_languages': ['en'], 'reference': False},
            'limitations': ['An existing compatible Whisper installation is reused first.',
                           'This isolated fallback uses CPU; CUDA capability is not inferred from GPU presence.'],
            'sources': [QWEN_SOURCE, SOX_SOURCE], 'smoke': 'import faster_whisper,ctranslate2,av; from transformers import MarianMTModel',
        })
    result.append({
        'id': 'chatterbox-multilingual-v3', 'label': 'Chatterbox Multilingual V3 · optional',
        'kind': 'tts', 'family': 'chatterbox', 'recommended': False, 'cpu_only': True,
        'requirements': 'scripts/requirements-dubbing-chatterbox.txt', 'runner': 'scripts/dubbing_runtime.py',
        'planning_memory_mb': 6000,
        'parts': [_part('voice', 'ResembleAI/chatterbox',
            ['ve.pt', 's3gen.pt', 't3_mtl23ls_v3.safetensors', 'grapheme_mtl_merged_expanded_v1.json',
             'conds.pt', 'README.md', 'LICENSE*'],
            ['ve.pt', 's3gen.pt', 't3_mtl23ls_v3.safetensors', 'grapheme_mtl_merged_expanded_v1.json'], 'mit')],
        'capabilities': {'offline': True, 'languages': ['ru'], 'tasks': ['voice_clone'], 'reference': True,
                         'named_voices': False, 'native_emotion': False, 'watermark': True},
        'limitations': ['Upstream describes this as an unsupported research archive.',
                       'This Russian-only API profile omits Chinese/Japanese helpers and automatic Russian stress enhancement.',
                       'This dependency family uses CPU; its older CUDA stack is not validated on RTX 5080.'],
        'sources': [{'kind': 'github_source', 'repository': 'resemble-ai/chatterbox',
                     'revision': CHATTERBOX_CODE_REVISION, 'prefix': 'src/',
                     'license': 'MIT', 'patterns': ['src/chatterbox/*.py', 'src/chatterbox/*.json', 'LICENSE'],
                     'patch_profile': 'chatterbox-ru-api-v1'}],
        'smoke': 'import perth; assert callable(perth.PerthImplicitWatermarker); watermark=perth.PerthImplicitWatermarker(); from chatterbox.mtl_tts import ChatterboxMultilingualTTS',
    })
    return result


class ComponentUnavailable(RuntimeError):
    """A controlled, actionable compatibility or publisher-verification failure."""


def _specs():
    values = _base_specs()
    for module in ('dubbing_visual', 'dubbing_separation'):
        try:
            imported = __import__('forge.' + module, fromlist=['component_specs'])
            provider = getattr(imported, 'component_specs', None)
            if provider:
                values.extend(copy.deepcopy(provider()))
        except ImportError:
            pass
    result = {}
    for value in values:
        key = value.get('id', '')
        if not COMPONENT_ID.fullmatch(key) or '..' in key or key.endswith('.') or key in result:
            raise ValueError('Invalid or duplicate managed dubbing component.')
        value.setdefault('family', key)
        value['family'] = re.sub(r'[^a-z0-9-]', '-', value['family']).strip('-')
        value.setdefault('runner', 'scripts/dubbing_runtime.py')
        value.setdefault('sources', [])
        value.setdefault('recommended', False)
        value.setdefault('planning_memory_mb', 4000)
        result[key] = value
    return result


def _spec(component):
    try:
        return _specs()[component]
    except KeyError:
        raise ValueError('Choose a registered Dubbing Studio component.') from None


def _read(path):
    try:
        value = json.loads(Path(path).read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _hash(path, algorithm='sha256'):
    digest = hashlib.sha1() if algorithm == 'git-sha1' else hashlib.new(algorithm)
    if algorithm == 'git-sha1':
        digest.update(f'blob {Path(path).stat().st_size}\0'.encode())
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _state_path(data):
    return Path(data) / 'dubbing' / 'models.json'


def _roots(data):
    data = Path(data).resolve()
    state = _read(_state_path(data))
    candidates = [state.get('storage_root'), str(Path(data) / 'models' / 'dubbing'), *state.get('previous_roots', [])]
    return list(dict.fromkeys(Path(path).resolve() for path in candidates if isinstance(path, str) and path and Path(path).is_absolute()))


def _component_root(storage, component):
    storage = Path(storage).resolve(); result = storage / component
    if result.is_symlink() or not result.resolve().is_relative_to(storage):
        raise ComponentUnavailable('A managed component directory links outside its selected storage. Repair the storage selection without deleting linked files.')
    return result


def _python(root):
    return Path(root) / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def _safe_relative(name):
    if not isinstance(name, str) or len(name) > 500 or '\\' in name or re.search(r'[:<>|?*\x00-\x1f]', name):
        raise ValueError('Unsafe managed model filename.')
    value = PurePosixPath(name)
    if value.is_absolute() or '..' in value.parts or not value.parts or any(part in {'', '.'} for part in value.parts):
        raise ValueError('Unsafe managed model filename.')
    return value


def _url_allowed(url):
    value = urllib.parse.urlsplit(url)
    host = value.hostname or ''
    return value.scheme == 'https' and not value.username and not value.password and (
        host in {'huggingface.co', 'api.github.com', 'github.com', 'raw.githubusercontent.com',
                 'release-assets.githubusercontent.com', 'files.pythonhosted.org', 'download.pytorch.org', 'download-r2.pytorch.org'}
        or host.endswith(('.huggingface.co', '.hf.co')))


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not _url_allowed(newurl):
            raise ComponentUnavailable('The model publisher redirected to an unapproved host.')
        result = super().redirect_request(request, fp, code, message, headers, newurl)
        if result:
            result.remove_header('Authorization')
        return result


def _open(url, *, headers=None, method='GET', timeout=30):
    if not _url_allowed(url):
        raise ValueError('Managed components use approved HTTPS publishers only.')
    request = urllib.request.Request(url, headers={'User-Agent': 'FORGE-Dubbing-Installer', **(headers or {})}, method=method)
    try:
        return urllib.request.build_opener(_Redirect()).open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        raise ComponentUnavailable(f'Publisher request failed with HTTP {error.code}. Retry when access is available; no unverified component was activated.') from None
    except (OSError, urllib.error.URLError):
        raise ComponentUnavailable('The component publisher is unavailable or blocked. Check network access and Retry; installed components were preserved.') from None


def _fetch_json(url):
    with _open(url) as response:
        data = response.read(4_000_001)
    if len(data) > 4_000_000:
        raise ComponentUnavailable('Publisher metadata exceeds its verification limit.')
    try:
        value = json.loads(data)
    except ValueError:
        raise ComponentUnavailable('The publisher returned invalid component metadata.') from None
    if not isinstance(value, dict):
        raise ComponentUnavailable('The publisher returned invalid component metadata.')
    return value


def _emit(progress, value, message, **metrics):
    if progress:
        progress(max(0, min(99, value)), message, **metrics)


def discover_part(part, *, revision=None):
    """Read publisher pins for review; this never installs or activates weights."""
    repository = part.get('repository', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid approved model repository.')
    revision = revision or part.get('revision') or 'main'
    if revision != 'main' and not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('Choose an immutable publisher revision.')
    info = _fetch_json('https://huggingface.co/api/models/' + repository + '/revision/' + revision + '?blobs=true')
    actual = info.get('sha', '')
    if not re.fullmatch(r'[a-f0-9]{40}', actual) or (revision != 'main' and actual != revision):
        raise ComponentUnavailable('Publisher metadata does not identify the requested immutable revision.')
    license = str(info.get('cardData', {}).get('license') or '').lower()
    expected_license = str(part.get('license') or '').lower()
    if expected_license and license != expected_license:
        raise ComponentUnavailable('The model license must be reviewed before installation; its publisher license differs from the registered component.')
    artifacts = []
    for item in info.get('siblings', []):
        name = item.get('rfilename', '')
        if not any(fnmatch.fnmatchcase(name, pattern) for pattern in part.get('allow_patterns', [])):
            continue
        _safe_relative(name)
        size = item.get('size')
        lfs = item.get('lfs') or {}
        identity = lfs.get('sha256') or lfs.get('oid') or item.get('blobId') or item.get('blob_id')
        algorithm = 'sha256' if lfs else 'git-sha1'
        if not isinstance(size, int) or not 0 < size <= 20_000_000_000 or not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}' if lfs else '[a-f0-9]{40}', identity):
            raise ComponentUnavailable('The publisher did not supply a verifiable size and object identity for every model artifact.')
        artifacts.append({'name': name, 'size': size, 'algorithm': algorithm, 'digest': identity,
                          'url': f'https://huggingface.co/{repository}/resolve/{actual}/' + urllib.parse.quote(name, safe='/')})
    names = {item['name'] for item in artifacts}
    if not 0 < len(artifacts) <= 1000 or not set(part.get('required_files', [])).issubset(names):
        raise ComponentUnavailable('The reviewed component snapshot is missing required model/tokenizer files.')
    if part.get('id') == 'translation' and not names.intersection({'model.safetensors', 'pytorch_model.bin'}):
        raise ComponentUnavailable('The translation snapshot contains no supported tensor weights.')
    if part.get('id') == 'voice' and repository.startswith('Qwen/') and not any(name.endswith('.safetensors') for name in names):
        raise ComponentUnavailable('The Qwen snapshot contains no verified safetensors weights.')
    return {**copy.deepcopy(part), 'revision': actual, 'license': license, 'artifacts': artifacts,
            'bytes': sum(item['size'] for item in artifacts)}


def _parts_plan(spec):
    result = []
    for part in spec.get('parts', []):
        if part.get('artifacts'):
            value = copy.deepcopy(part)
            if not REVISION.fullmatch(value.get('revision', '')):
                raise ComponentUnavailable('This optional component has no reviewed immutable artifact revision yet.')
            for artifact in value['artifacts']:
                _safe_relative(artifact['name'])
                if not _url_allowed(artifact['url']) or not SHA256.fullmatch(artifact.get('sha256', artifact.get('digest', ''))):
                    raise ComponentUnavailable('The optional component has an unverified artifact identity.')
                artifact.update(algorithm='sha256', digest=artifact.get('sha256', artifact.get('digest')))
            value['bytes'] = sum(artifact['size'] for artifact in value['artifacts'])
        else:
            if not re.fullmatch(r'[0-9a-f]{40}', part.get('revision', '')):
                raise ComponentUnavailable('The model publisher inventory has not been verified for this build. Check Compatibility after the reviewed model catalog is available.')
            value = discover_part(part)
        result.append(value)
    if not result:
        raise ComponentUnavailable('No verified model artifacts are registered for this component.')
    return result


def _diagnostic(text):
    lines = str(text).splitlines()
    relevant = [line for line in lines if re.search(r'error|failed|not found|no matching|incompatible', line, re.I)]
    detail = '\n'.join(relevant[-4:] or lines[-3:])
    detail = re.sub(r'https?://\S+', '<publisher URL>', detail)
    detail = re.sub(r'(?i)(token|password|authorization)\s*[=:]\s*\S+', r'\1=<private>', detail)
    return detail[-700:]


def _run(args, *, env=None, timeout=600):
    result = subprocess.run(list(map(str, args)), env=env, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=timeout, **no_window())
    if result.returncode:
        raise ComponentUnavailable('Managed runtime operation failed. ' + _diagnostic(result.stderr or result.stdout))
    return result.stdout


def _ram_bytes():
    try:
        if os.name == 'nt':
            import ctypes
            class Memory(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong),
                            *[(name, ctypes.c_ulonglong) for name in ('total', 'available', 'total_page', 'available_page', 'total_virtual', 'available_virtual', 'extended')]]
            info = Memory(); info.length = ctypes.sizeof(info)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(info)):
                return int(info.total)
        total = int(os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE'))
        for candidate in ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes'):
            try:
                limit = Path(candidate).read_text().strip()
                if limit.isdigit() and int(limit) > 0:
                    total = min(total, int(limit))
            except OSError:
                pass
        return total
    except (AttributeError, OSError, ValueError):
        return None


def hardware(data, settings=None):
    settings = settings or {}
    target = Path(data).resolve()
    while not target.exists() and target != target.parent:
        target = target.parent
    result = {'os': platform.system(), 'architecture': platform.machine(), 'application_python': platform.python_version(),
              'application_python_compatible': (3, 11) <= sys.version_info[:2] <= (3, 12),
              'managed_python_available': os.name == 'nt' and platform.machine().lower() in {'amd64', 'x86_64'},
              'ram_bytes': _ram_bytes(), 'disk_free_bytes': shutil.disk_usage(target).free,
              'ffmpeg': bool(executable('ffmpeg', settings)), 'ffprobe': bool(executable('ffprobe', settings)),
              'gpus': [], 'cuda_available': False, 'cuda_architecture_verified': False,
              'limitations': ['GPU presence is not a model compatibility or performance test.']}
    binary = shutil.which('nvidia-smi')
    if binary:
        try:
            text = _run([binary, '--query-gpu=name,driver_version,memory.total,memory.free', '--format=csv,noheader,nounits'], timeout=8)
            for row in csv.reader(io.StringIO(text)):
                if len(row) != 4:
                    continue
                name, driver, total, free = [part.strip() for part in row]
                version = tuple(int(value) for value in re.findall(r'\d+', driver)[:2])
                minimum = (570, 65) if os.name == 'nt' else (570, 26)
                gpu = {'name': name, 'driver': driver, 'total_vram_mb': int(float(total)), 'free_vram_mb': int(float(free)),
                       'driver_compatible_cu128': version >= minimum, 'physical_inference_tested': False}
                result['gpus'].append(gpu)
            result['cuda_available'] = any(gpu['driver_compatible_cu128'] for gpu in result['gpus'])
        except (OSError, ValueError, subprocess.SubprocessError, ComponentUnavailable):
            result['gpu_diagnostic'] = 'NVIDIA diagnostics did not complete. CPU setup remains available.'
    return result


def compatibility(component, data, settings=None, *, device='auto'):
    spec = _spec(component); info = hardware(data, settings)
    if device not in {'auto', 'cpu', 'cuda'}:
        raise ValueError('Select Auto, CPU or NVIDIA CUDA.')
    reasons = []
    chosen = 'cpu'
    needed = int(spec['planning_memory_mb']) + int((settings or {}).get('gpu_memory_reserve_mb', 1024))
    eligible = [gpu for gpu in info['gpus'] if gpu['driver_compatible_cu128'] and gpu['free_vram_mb'] >= needed]
    if device != 'cpu' and eligible and not spec.get('cpu_only'):
        chosen = 'cuda'
    if device == 'cuda' and chosen != 'cuda':
        reasons.append('The registered CUDA 12.8 runtime requires a compatible NVIDIA driver and sufficient free VRAM; select CPU or repair the driver through NVIDIA.')
    if spec.get('cpu_only') and device == 'cuda':
        reasons.append('This optional runtime is currently validated for CPU only.')
    if chosen == 'cpu' and info.get('ram_bytes') and info['ram_bytes'] < (int(spec['planning_memory_mb']) + 1024) * 1024 * 1024:
        reasons.append('Available system memory is below the conservative CPU model allowance. Select a smaller component or a verified compatible GPU runtime.')
    if not info['application_python_compatible'] and not info['managed_python_available']:
        reasons.append('A compatible isolated Python 3.11–3.12 runtime is required on this platform.')
    if spec.get('unavailable_reason'):
        reasons.append(spec['unavailable_reason'])
    pins_ready = bool(spec.get('parts')) and all(REVISION.fullmatch(part.get('revision', '')) for part in spec['parts'])
    if not pins_ready:
        reasons.append('The exact publisher snapshot is awaiting verification; no unverified model is Ready.')
    return {'component': component, 'compatible': not reasons, 'selected_device': chosen,
            'planning_memory_mb': spec['planning_memory_mb'], 'hardware': info, 'reasons': reasons,
            'physical_gpu_tested': False, 'hardware_benchmark_available': False}


def _runtime_plan():
    compatible = (3, 11) <= sys.version_info[:2] <= (3, 12)
    if compatible:
        return {'kind': 'existing', 'python': str(Path(sys.executable).resolve()), 'version': platform.python_version(), 'download_bytes': 0}
    if os.name != 'nt' or platform.machine().lower() not in {'amd64', 'x86_64'}:
        raise ComponentUnavailable('This platform has no registered managed Python artifact; select an existing compatible isolated runtime.')
    root = None
    return {'kind': 'managed', **MANAGED_PYTHON, 'download_bytes': MANAGED_PYTHON['size']}


def _source_plan(source):
    if source['kind'] != 'github_source':
        return copy.deepcopy(source)
    repository, revision = source['repository'], source['revision']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('Unreviewed runtime source identity.')
    tree = _fetch_json(f'https://api.github.com/repos/{repository}/git/trees/{revision}?recursive=1')
    files = []
    for item in tree.get('tree', []):
        name = item.get('path', '')
        if item.get('type') != 'blob' or not any(fnmatch.fnmatchcase(name, pattern) for pattern in source['patterns']):
            continue
        _safe_relative(name)
        if not re.fullmatch(r'[a-f0-9]{40}', item.get('sha', '')) or not 0 < item.get('size', 0) <= 3_000_000:
            raise ComponentUnavailable('Runtime source inventory is not verifiable.')
        files.append({'name': name, 'size': item['size'], 'digest': item['sha'], 'algorithm': 'git-sha1',
                      'url': f'https://raw.githubusercontent.com/{repository}/{revision}/{name}'})
    if not files or len(files) > 300:
        raise ComponentUnavailable('Runtime source inventory is incomplete or exceeds its limit.')
    return {**copy.deepcopy(source), 'files': files, 'size': sum(item['size'] for item in files)}


def _head_size(url):
    with _open(url, method='HEAD', timeout=20) as response:
        value = response.headers.get('Content-Length', '')
    if not value.isdigit() or not 0 < int(value) <= 20_000_000_000:
        raise ComponentUnavailable('A dependency publisher did not provide its download size. Retry before approving installation.')
    return int(value)


def _existing_parent(path):
    path = Path(path).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _storage_requirements(data, model_root, total, model_bytes):
    data = Path(data).resolve(); model_root = Path(model_root).resolve()
    data_parent, model_parent = _existing_parent(data), _existing_parent(model_root)
    same_volume = data_parent.stat().st_dev == model_parent.stat().st_dev
    if os.name == 'nt':
        same_volume = data_parent.drive.casefold() == model_parent.drive.casefold()
    required = total * 3 + 512_000_000
    result = [{'path': str(data), 'free_bytes': shutil.disk_usage(data_parent).free, 'required_bytes': required}]
    if not same_volume:
        result.append({'path': str(model_root), 'free_bytes': shutil.disk_usage(model_parent).free,
                       'required_bytes': model_bytes * 2 + 128_000_000})
    return result


def _target_platform():
    system = platform.system().lower()
    machine = platform.machine().lower()
    return system, 'x64' if machine in {'amd64', 'x86_64'} else machine


def _wheel_lock_path(spec, runtime, device):
    system, machine = _target_platform()
    version = ''.join(runtime['version'].split('.')[:2])
    name = Path(spec['requirements']).stem + '-' + system + '-' + machine + '-py' + version + '-' + device + '.json'
    return ROOT / 'forge' / 'dubbing_wheel_locks' / name


def _load_wheel_lock(spec, runtime, device):
    path = _wheel_lock_path(spec, runtime, device)
    if not path.is_file():
        return None
    value = _read(path); system, machine = _target_platform()
    if (value.get('schema') != 1 or value.get('profile') != Path(spec['requirements']).stem
            or value.get('requirements_sha256') != _hash(ROOT / spec['requirements'])
            or value.get('system') != system or value.get('architecture') != machine
            or value.get('python_version') != '.'.join(runtime['version'].split('.')[:2])
            or value.get('device') != device or value.get('cross_resolved') is not False
            or system == 'windows' and value.get('native_windows_execution') is not True):
        raise ComponentUnavailable('The verified dependency manifest does not match this runtime/profile. Use the compatible FORGE update or Repair; no cross-version package guesses were installed.')
    wheels = value.get('wheels', [])
    if not isinstance(wheels, list) or not 0 < len(wheels) <= 250:
        raise ComponentUnavailable('The verified dependency manifest has no complete binary inventory.')
    for wheel in wheels:
        if (not _url_allowed(wheel.get('url', '')) or not SHA256.fullmatch(wheel.get('sha256', ''))
                or type(wheel.get('size')) is not int or wheel['size'] <= 0
                or not re.fullmatch(r'[A-Za-z0-9_.-]+', wheel.get('package', ''))
                or not re.fullmatch(r'[A-Za-z0-9_.+!-]+', wheel.get('version', ''))
                or not isinstance(wheel.get('name'), str) or '/' in wheel['name'] or '\\' in wheel['name']
                or not wheel['name'].endswith('.whl')):
            raise ComponentUnavailable('The verified binary dependency inventory contains an invalid artifact.')
    if sum(wheel['size'] for wheel in wheels) != value.get('download_bytes'):
        raise ComponentUnavailable('The verified dependency download size does not match its inventory.')
    return {'wheels': copy.deepcopy(wheels), 'requirements_sha256': value['requirements_sha256'],
            'download_bytes': value['download_bytes'], 'native_manifest': path.name}


def _dependency_plan(spec, runtime, device, data, *, use_lock=True):
    requirements = ROOT / spec['requirements']
    if not requirements.is_file() or not requirements.resolve().is_relative_to(ROOT / 'scripts'):
        raise ComponentUnavailable('The registered dependency profile is missing.')
    target_version = tuple(map(int, runtime['version'].split('.')[:2]))
    if use_lock:
        locked = _load_wheel_lock(spec, runtime, device)
        if locked:
            return locked
    if target_version != sys.version_info[:2]:
        # pip target flags do not reliably replace Requires-Dist environment
        # markers. A native-built lock is mandatory before a Python3.13 app can
        # quote/install its managed3.12 dependencies.
        raise ComponentUnavailable('The verified target-Python dependency manifest is not available in this build. Update the Dubbing Studio component catalog; existing Python environments were preserved.')
    with tempfile.TemporaryDirectory(prefix='forge-dubbing-plan-') as temporary:
        report = Path(temporary) / 'packages.json'
        args = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--timeout', '25', '--retries', '1',
                '--dry-run', '--ignore-installed', '--only-binary=:all:', '--report', report, '-r', requirements]
        torch_version = '2.6.0' if spec.get('family') == 'chatterbox' else '2.8.0'
        suffix = '+cu128' if device == 'cuda' else '+cpu'
        args.extend(['--extra-index-url', 'https://download.pytorch.org/whl/' + ('cu128' if device == 'cuda' else 'cpu'), 'torch==' + torch_version + suffix])
        if spec.get('family') != 'translation':
            args.append('torchaudio==' + torch_version + suffix)
        _run(args, timeout=360)
        value = _read(report)
    wheels = []
    for item in value.get('install', []):
        download = item.get('download_info', {}); metadata = item.get('metadata', {})
        url = download.get('url', ''); sha = download.get('archive_info', {}).get('hashes', {}).get('sha256', '')
        name = urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).name)
        if not _url_allowed(url) or not name.endswith('.whl') or not SHA256.fullmatch(sha):
            raise ComponentUnavailable('Every managed dependency must have a verified binary wheel; arbitrary source builds are not permitted.')
        package, version = metadata.get('name', ''), metadata.get('version', '')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', package) or not re.fullmatch(r'[A-Za-z0-9_.+!-]+', version):
            raise ComponentUnavailable('Invalid dependency package identity.')
        license = metadata.get('license_expression') or metadata.get('license') or 'See publisher LICENSE'
        if len(license) > 160:
            license = 'See the package distribution LICENSE'
        wheels.append({'package': package, 'version': version, 'name': name, 'url': url, 'sha256': sha, 'size': _head_size(url), 'license': license})
    if not 0 < len(wheels) <= 250:
        raise ComponentUnavailable('The complete managed dependency plan could not be verified.')
    return {'wheels': wheels, 'requirements_sha256': _hash(requirements), 'download_bytes': sum(item['size'] for item in wheels)}


def installation_plan(components, data, *, device='auto', settings=None, action='install'):
    if action not in {'install', 'repair'}:
        raise ValueError('Choose Install or Repair before approving component changes.')
    if isinstance(components, str):
        components = [components]
    if not isinstance(components, list) or not 1 <= len(components) <= 12:
        raise ValueError('Select between one and twelve registered components.')
    data = Path(data).resolve(); runtime = _runtime_plan(); plans = []
    for component in dict.fromkeys(components):
        spec = _spec(component); check = compatibility(component, data, settings, device=device)
        if not check['compatible']:
            raise ComponentUnavailable('; '.join(check['reasons']))
        parts = _parts_plan(spec)
        sources = [_source_plan(source) for source in spec['sources']]
        dependencies = _dependency_plan(spec, runtime, check['selected_device'], data)
        identity = {part['id']: part['revision'] for part in parts}
        model_revision = next(iter(identity.values())) if len(identity) == 1 else hashlib.sha256(_canonical(identity)).hexdigest()
        recipe = {'requirements': dependencies['requirements_sha256'], 'wheels': [(w['package'], w['version'], w['sha256']) for w in dependencies['wheels']],
                  'sources': sources, 'device': check['selected_device'], 'python': runtime['version']}
        if action == 'repair':
            recipe['repair_generation'] = uuid.uuid4().hex
        runtime_id = 'dubbing-' + spec['family'] + '-' + hashlib.sha256(_canonical(recipe)).hexdigest()[:16]
        model_bytes = sum(part['bytes'] for part in parts)
        download_bytes = model_bytes + dependencies['download_bytes'] + sum(source['size'] for source in sources)
        plans.append({'component': component, 'label': spec['label'], 'parts': parts, 'sources': sources,
                      'dependencies': dependencies, 'device': check['selected_device'], 'runtime_id': runtime_id,
                      'model_revision': model_revision, 'model_bytes': model_bytes, 'download_bytes': download_bytes,
                      'destination': str(_component_root(_roots(data)[0], component) / model_revision), 'requirements': spec['requirements'],
                      'runner': spec['runner'], 'smoke': spec.get('smoke', ''), 'family': spec['family'], 'capabilities': spec['capabilities']})
        plans[-1]['repair_runtime'] = action == 'repair'
    # A conservative staged-copy allowance is separate from the measured download size.
    unique = {}
    for plan in plans:
        artifacts = [artifact for part in plan['parts'] for artifact in part['artifacts']]
        artifacts += plan['dependencies']['wheels']
        for source in plan['sources']:
            artifacts.extend(source.get('files', [source]))
        for artifact in artifacts:
            identity = (artifact.get('algorithm', 'sha256'), artifact.get('digest') or artifact.get('sha256'), artifact['size'])
            unique[identity] = artifact['size']
    total = sum(unique.values()) + runtime['download_bytes']
    storage = _storage_requirements(data, _roots(data)[0], total, sum(plan['model_bytes'] for plan in plans))
    if any(value['free_bytes'] < value['required_bytes'] for value in storage):
        raise ComponentUnavailable('There is not enough free space for the component download, staged installation and recovery copy. Select another model-storage location or free space before approving.')
    result = {'schema': 1, 'action': action, 'created': time.time(), 'expires': time.time() + 3600, 'runtime': runtime,
              'components': plans, 'download_bytes': total, 'required_storage_bytes': total * 3 + 512_000_000,
              'destination': str(_roots(data)[0]), 'data_identity': str(data), 'approval_required': True,
              'download_size_scope': 'Verified total artifact bytes; already verified cache entries may reduce network transfer.',
              'storage_size_scope': 'Conservative allowance for downloads, extracted files, staging and recovery.',
              'storage_requirements': storage}
    result['plan_hash'] = hashlib.sha256(_canonical(result)).hexdigest()
    json_write(data / 'dubbing' / 'plans' / (result['plan_hash'] + '.json'), result)
    return result


plan_install = installation_plan


def _download(artifact, cache, progress=None):
    """Serialize one content identity across model/runtime/component installers."""
    size = artifact['size']; algorithm = artifact.get('algorithm', 'sha256')
    expected = artifact.get('digest') or artifact.get('sha256', '')
    if (algorithm not in {'sha256', 'git-sha1'} or type(size) is not int or not 0 < size <= 20_000_000_000
            or not re.fullmatch('[a-f0-9]{40}' if algorithm == 'git-sha1' else '[a-f0-9]{64}', expected)):
        raise ValueError('Invalid managed artifact size or integrity identity.')
    cache = Path(cache); cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((algorithm + str(size) + expected).encode()).hexdigest()
    with cache_lock(cache / (key + '.lock')):
        return _download_unlocked(artifact, cache, progress)


def _download_unlocked(artifact, cache, progress=None):
    """Resumable owned transfers; a verified digest is required before reuse."""
    size = artifact['size']; algorithm = artifact.get('algorithm', 'sha256')
    expected = artifact.get('digest') or artifact.get('sha256', '')
    if type(size) is not int or not 0 < size <= 20_000_000_000 or not re.fullmatch('[a-f0-9]{40}' if algorithm == 'git-sha1' else '[a-f0-9]{64}', expected):
        raise ValueError('Invalid managed artifact size or integrity identity.')
    cache = Path(cache); cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((algorithm + str(size) + expected).encode()).hexdigest()
    complete = cache / (key + '.verified'); partial = cache / (key + '.part')
    if complete.is_file() and not complete.is_symlink() and complete.stat().st_size == size and _hash(complete, algorithm) == expected:
        return complete
    if complete.exists():
        complete.rename(cache / (key + '.invalid-' + uuid.uuid4().hex))
    if partial.is_symlink():
        raise ValueError('The managed download cache contains an unsafe link.')
    offset = partial.stat().st_size if partial.is_file() and partial.stat().st_size < size else 0
    headers = {'Range': f'bytes={offset}-'} if offset else {}
    started = time.monotonic(); transferred = 0
    with _open(artifact['url'], headers=headers, timeout=30) as response:
        status = getattr(response, 'status', 200)
        if offset and status == 206:
            content_range = response.headers.get('Content-Range', '')
            if not re.match(r'bytes ' + str(offset) + r'-\d+/' + str(size) + r'$', content_range):
                raise ComponentUnavailable('The publisher returned an invalid resumed transfer range.')
        else:
            offset = 0
        with partial.open('ab' if offset else 'wb') as output:
            while True:
                block = response.read(1024 * 512)
                if not block:
                    break
                transferred += len(block)
                if offset + transferred > size:
                    raise ComponentUnavailable('A model download exceeded its verified publisher size.')
                output.write(block)
                elapsed = max(.001, time.monotonic() - started)
                _emit(progress, 100 * (offset + transferred) / size, 'Downloading verified component artifacts',
                      stage='Component download', progress_measured=True, transferred_bytes=offset + transferred,
                      total_bytes=size, speed_bps=transferred / elapsed,
                      eta_seconds=(size - offset - transferred) / max(1, transferred / elapsed),
                      progress_scope='Current component artifact', resume_supported=bool(status == 206))
            output.flush(); os.fsync(output.fileno())
    if partial.stat().st_size != size or _hash(partial, algorithm) != expected:
        raise ComponentUnavailable('Component artifact integrity verification failed. Repair will retry this private download; installed components remain unchanged.')
    os.replace(partial, complete)
    return complete


def _extract_runtime(archive, destination):
    destination = Path(destination); total = 0; count = 0
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != 'python' or member.name.startswith('/') or '..' in parts or '\\' in member.name:
                raise ValueError('The managed Python archive contains an unsafe path.')
            if len(parts) == 1:
                continue
            relative = _safe_relative('/'.join(parts[1:])); target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True); continue
            if not member.isfile() or member.size < 0 or member.size > 200_000_000:
                raise ValueError('The managed Python archive contains an unsafe member.')
            count += 1; total += member.size
            if count > 30000 or total > 1_000_000_000:
                raise ValueError('The managed Python archive exceeds its verified installation bounds.')
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as stream, target.open('xb') as output:
                shutil.copyfileobj(stream, output)


def _bootstrap_python(runtime, data, progress=None, *, repair=False):
    if runtime['kind'] == 'existing':
        path = Path(runtime['python'])
        if not path.is_file():
            raise ComponentUnavailable('The selected compatible interpreter is no longer available. Run setup compatibility again.')
        version = _run([path, '-I', '-c', 'import sys;print(".".join(map(str,sys.version_info[:3])))'], timeout=30).strip()
        if version != runtime['version']:
            raise ComponentUnavailable('The selected runtime changed after approval. Review a fresh installation plan.')
        return path
    if runtime != {'kind': 'managed', **MANAGED_PYTHON, 'download_bytes': MANAGED_PYTHON['size']}:
        raise ValueError('Unregistered managed Python artifact.')
    root = Path(data) / 'runtimes' / MANAGED_PYTHON['id']
    python = root / 'python.exe'
    marker = _read(root / 'forge-python.json')
    def usable():
        return (not root.is_symlink() and python.is_file() and not python.is_symlink()
                and marker.get('sha256') == MANAGED_PYTHON['sha256'] and marker.get('verified')
                and _hash(python) == MANAGED_PYTHON['python_exe_sha256'])
    if not repair and usable():
        return python
    with cache_lock(root.parent / '.dubbing-python.lock'):
        marker = _read(root / 'forge-python.json')
        if not repair and usable():
            return python
        if root.is_symlink():
            raise ComponentUnavailable('The managed Python directory is linked elsewhere. Existing runtimes were preserved; choose a safe local FORGE data directory.')
        staging = root.parent / ('.python-' + uuid.uuid4().hex); staging.mkdir(parents=True)
        archive = _download(MANAGED_PYTHON, Path(data) / 'dubbing' / 'downloads', progress)
        _extract_runtime(archive, staging)
        staged_python = staging / 'python.exe'
        if _hash(staged_python) != MANAGED_PYTHON['python_exe_sha256']:
            raise ComponentUnavailable('The extracted interpreter differs from its reviewed official archive.')
        version = _run([staged_python, '-I', '-c', 'import sys,ssl,venv;print(".".join(map(str,sys.version_info[:3])))'], timeout=30).strip()
        if version != MANAGED_PYTHON['version']:
            raise ComponentUnavailable('The managed runtime did not report its approved Python version.')
        json_write(staging / 'forge-python.json', {**MANAGED_PYTHON, 'verified': True})
        retained = None
        if root.exists():
            retained = root.with_name('.retained-' + root.name + '-' + uuid.uuid4().hex)
            root.rename(retained)
        try:
            staging.rename(root)
        except BaseException:
            if retained is not None and not root.exists():
                retained.rename(root)
            raise
    return python


def _install_sources(sources, destination, data, progress):
    destination = Path(destination); destination.mkdir(parents=True, exist_ok=True); records = []
    cache = Path(data) / 'dubbing' / 'downloads'
    for source in sources:
        if source['kind'] == 'github_source':
            for artifact in source['files']:
                cached = _download(artifact, cache, progress)
                name = artifact['name']; prefix = source.get('prefix', '')
                relative = name[len(prefix):] if prefix and name.startswith(prefix) else 'licenses/' + name
                target = destination / _safe_relative(relative); target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(cached, target)
                records.append({'path': relative, 'size': target.stat().st_size, 'sha256': _hash(target)})
            if source.get('patch_profile'):
                if source['patch_profile'] != 'chatterbox-ru-api-v1' or source.get('revision') != CHATTERBOX_CODE_REVISION:
                    raise ValueError('Unreviewed runtime patch profile.')
                patched = _patch_chatterbox_api(destination)
                for record in records:
                    if record['path'] in patched:
                        record['upstream_sha256'] = record['sha256']
                        record['sha256'] = _hash(destination / record['path'])
                        record['size'] = (destination / record['path']).stat().st_size
                        record['patch_profile'] = source['patch_profile']
            continue
        cached = _download(source, cache, progress)
        if source['kind'] == 'wheel_source':
            with zipfile.ZipFile(cached) as archive:
                files = []
                for item in archive.infolist():
                    if item.is_dir() or not (item.filename.startswith(source['package_prefix']) or '/licenses/' in item.filename):
                        continue
                    _safe_relative(item.filename)
                    mode = item.external_attr >> 16
                    if (mode & 0o170000) not in {0, 0o100000} or item.file_size > 3_000_000:
                        raise ValueError('The reviewed source wheel contains an unsafe or oversized member.')
                    if PurePosixPath(item.filename).suffix.lower() in {'.dll', '.exe', '.pyd', '.so', '.dylib'}:
                        raise ValueError('Native extensions are not accepted through the reviewed source-only profile.')
                    files.append((item.filename, archive.read(item)))
        elif source['kind'] == 'tar_source':
            files = []
            with tarfile.open(cached, 'r:gz') as archive:
                for item in archive:
                    parts = PurePosixPath(item.name).parts; name = '/'.join(parts[1:])
                    if item.isfile() and (name.startswith(source['package_prefix']) or name.upper().startswith(('LICENSE', 'COPYING'))):
                        if item.size > 3_000_000:
                            raise ValueError('The reviewed source package exceeded its extraction bound.')
                        files.append((name, archive.extractfile(item).read()))
        else:
            raise ValueError('Unknown reviewed runtime source format.')
        if not files or len(files) > 500:
            raise ValueError('The reviewed runtime source inventory is invalid.')
        for name, raw in files:
            _safe_relative(name)
            if not name.startswith(source['package_prefix']):
                name = 'licenses/' + source['name'].replace(' ', '-') + '/' + PurePosixPath(name).name
            target = destination / name; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            records.append({'path': name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    return records


def _patch_chatterbox_api(destination):
    """Explicit reviewed API-profile changes, applied only after Git-blob checks.

    No inference layers or checkpoint tensors are changed. Upstream's existing
    CFM_PARAMS is the identical dictionary used by its otherwise unnecessary
    OmegaConf default; the API source profile has no installed CLI distribution.
    """
    destination = Path(destination)
    init = destination / 'chatterbox/__init__.py'
    source = init.read_text('utf-8')
    before = '__version__ = version("chatterbox-tts")'
    if source.count(before) != 1:
        raise ComponentUnavailable('The archived API version declaration differs from its reviewed patch.')
    source = source.replace(before, '# FORGE reviewed Russian API profile; the full upstream CLI distribution is not installed.\n__version__ = "0.1.7+forge.ru.api1"')
    init.write_text(source, encoding='utf-8')
    flow = destination / 'chatterbox/models/s3gen/flow.py'
    source = flow.read_text('utf-8')
    before = "DictConfig(\n                                           {'sigma_min': 1e-06, 'solver': 'euler', 't_scheduler': 'cosine',\n                                            'training_cfg_rate': 0.2, 'inference_cfg_rate': 0.7,\n                                            'reg_loss_type': 'l1'})"
    if source.count('from omegaconf import DictConfig') != 1 or source.count(before) != 1:
        raise ComponentUnavailable('The archived flow defaults differ from the reviewed API profile.')
    source = source.replace('from omegaconf import DictConfig', '# FORGE API profile reuses the identical upstream CFM_PARAMS AttrDict.')
    source = source.replace(before, 'CFM_PARAMS')
    flow.write_text(source, encoding='utf-8')
    return {'chatterbox/__init__.py', 'chatterbox/models/s3gen/flow.py'}


def _environment(data, component_plan, runtime, progress=None):
    root = Path(data) / 'environments' / component_plan['runtime_id']
    marker = _read(root / 'forge-dubbing-runtime.json')
    if _python(root).is_file() and marker.get('import_tested') and marker.get('requirements_sha256') == component_plan['dependencies']['requirements_sha256']:
        _verify_runtime(root, full=True)
        if component_plan.get('smoke'):
            _run([_python(root), '-c', component_plan['smoke']], env=_runtime_env(root), timeout=180)
        return root
    with cache_lock(root.parent / ('.' + component_plan['runtime_id'] + '.lock')):
        marker = _read(root / 'forge-dubbing-runtime.json')
        if _python(root).is_file() and marker.get('import_tested') and marker.get('requirements_sha256') == component_plan['dependencies']['requirements_sha256']:
            _verify_runtime(root, full=True)
            if component_plan.get('smoke'):
                _run([_python(root), '-c', component_plan['smoke']], env=_runtime_env(root), timeout=180)
            return root
        if root.exists():
            raise ComponentUnavailable('The isolated runtime is incomplete or corrupt. Repair creates a fresh environment without changing existing speech installations.')
        base_python = _bootstrap_python(runtime, data, progress, repair=bool(component_plan.get('repair_runtime')))
        staging = root.parent / ('.' + component_plan['runtime_id'] + '-' + uuid.uuid4().hex)
        staging.parent.mkdir(parents=True, exist_ok=True)
        _emit(progress, 0, 'Creating a compatible isolated dubbing runtime', stage='Runtime installation')
        _run([base_python, '-m', 'venv', staging], timeout=180)
        wheelhouse = staging / 'wheelhouse'; wheelhouse.mkdir()
        lock = []
        for wheel in component_plan['dependencies']['wheels']:
            cached = _download(wheel, Path(data) / 'dubbing' / 'downloads', progress)
            shutil.copyfile(cached, wheelhouse / wheel['name'])
            lock.append(wheel['package'] + '==' + wheel['version'] + ' --hash=sha256:' + wheel['sha256'])
        lock_path = staging / 'forge-dependencies.lock'; lock_path.write_text('\n'.join(lock) + '\n', encoding='utf-8')
        python = _python(staging)
        _emit(progress, 0, 'Installing the approved binary dependencies offline', stage='Runtime installation')
        _run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-index', '--no-deps',
              '--require-hashes', '--find-links', wheelhouse, '-r', lock_path], timeout=900)
        _run([python, '-m', 'pip', 'check'], timeout=60)
        sources = _install_sources(component_plan['sources'], staging / 'upstream', data, progress)
        env = _runtime_env(staging)
        smoke = component_plan.get('smoke') or 'import torch,soundfile'
        _run([python, '-c', smoke], env=env, timeout=180)
        if component_plan['device'] == 'cuda':
            probe = 'import torch,json;assert torch.cuda.is_available(); c=torch.cuda.get_device_capability(); a=torch.cuda.get_arch_list(); assert c[0]<12 or "sm_120" in a; x=torch.ones((16,16),device="cuda"); y=x@x; torch.cuda.synchronize(); print(json.dumps({"device":torch.cuda.get_device_name(),"capability":c,"architectures":a}))'
            _run([python, '-c', probe], env=env, timeout=90)
        json_write(staging / 'forge-dubbing-runtime.json', {'schema': 1, 'id': component_plan['runtime_id'],
            'requirements_sha256': component_plan['dependencies']['requirements_sha256'], 'python': runtime['version'],
            'device': component_plan['device'], 'import_tested': True, 'inference_tested': False,
            'sources': sources, 'dependency_wheels': component_plan['dependencies']['wheels'], 'smoke': smoke})
        staging.rename(root)
    return root


def _validate_plan(plan, data, component):
    if not isinstance(plan, dict) or plan.get('schema') != 1:
        raise ValueError('Review the complete installation plan before approving this component.')
    expected = plan.get('plan_hash'); value = {key: val for key, val in plan.items() if key != 'plan_hash'}
    if not expected or hashlib.sha256(_canonical(value)).hexdigest() != expected or plan.get('data_identity') != str(Path(data).resolve()):
        raise ValueError('The installation plan changed. Review a fresh download and storage summary.')
    saved = _read(Path(data) / 'dubbing' / 'plans' / (expected + '.json'))
    if saved != plan:
        raise ValueError('This component quote was not issued by the local setup planner. Review a fresh installation plan.')
    if not time.time() <= plan.get('expires', 0):
        raise ValueError('The installation approval expired. Review a fresh plan.')
    selected = [value for value in plan.get('components', []) if value.get('component') == component]
    if len(selected) != 1:
        raise ValueError('The approved plan does not contain this component.')
    spec = _spec(component); selected = selected[0]
    if selected['requirements'] != spec['requirements'] or selected['runner'] != spec['runner'] or not RUNTIME_ID.fullmatch(selected['runtime_id']):
        raise ValueError('The approved component recipe no longer matches this application.')
    if _hash(ROOT / selected['requirements']) != selected['dependencies']['requirements_sha256']:
        raise ValueError('Dependency pins changed after approval. Review a fresh installation plan.')
    if not REVISION.fullmatch(selected.get('model_revision', '')):
        raise ValueError('Invalid immutable model revision in the approved plan.')
    if selected['destination'] != str(_component_root(_roots(data)[0], component) / selected['model_revision']):
        raise ValueError('Model storage changed after approval. Review a fresh installation plan; existing downloads are retained.')
    return selected


def install(component, data, *, consent=False, device='auto', progress=None, plan=None, repair=False, action=None, **kwargs):
    if consent is not True:
        raise ValueError('Approve the displayed component downloads and installation destination first.')
    data = Path(data).resolve(); spec = _spec(component)
    if plan is None:
        raise ValueError('A verified installation plan is required; no packages were installed.')
    selected = _validate_plan(plan, data, component)
    action = action or ('repair' if repair else 'install')
    if action not in {'install', 'repair'} or action != plan.get('action', 'install'):
        raise ValueError('The requested action differs from the approved installation plan.')
    repair = action == 'repair'
    if device not in {'auto', selected['device']}:
        raise ValueError('The device differs from the approved installation plan.')
    if any(shutil.disk_usage(_existing_parent(value['path'])).free < value['required_bytes'] for value in plan.get('storage_requirements', [])):
        raise ComponentUnavailable('There is insufficient free space for the approved download, staged installation and recovery copy.')
    root = _component_root(_roots(data)[0], component) / selected['model_revision']
    with cache_lock(data / 'dubbing' / 'locks' / (component + '.lock')):
        if root.exists():
            try:
                receipt = _verify_bundle(root)
                if not repair:
                    resolved = resolve(component, data, receipt['model_revision'])
                    _activate(component, data, receipt['model_revision'])
                    return {'status': 'ready', 'component': component, 'model_revision': receipt['model_revision'], 'cached': True,
                            'runtime_revision': receipt['runtime_id'], 'inference_verified': False}
            except (ValueError, ComponentUnavailable):
                if not repair:
                    raise ComponentUnavailable('This installed component failed verification. Use Repair; existing models were preserved.') from None
        environment = _environment(data, selected, plan['runtime'], progress)
        staging = root.parent / ('.install-' + uuid.uuid4().hex); staging.mkdir(parents=True, exist_ok=False)
        files = {}
        for part in selected['parts']:
            _safe_relative(part['id'])
            for artifact in part['artifacts']:
                target = staging / 'parts' / part['id'] / _safe_relative(artifact['name'])
                cached = _download(artifact, data / 'dubbing' / 'downloads', progress)
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(cached, target)
                stat = target.stat(); name = target.relative_to(staging).as_posix()
                files[name] = {'sha256': _hash(target), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
        receipt = {'schema': 1, 'component': component, 'model_revision': selected['model_revision'],
                   'runtime_id': environment.name, 'device': selected['device'],
                   'parts': [{key: part[key] for key in ('id', 'revision', 'repository', 'license') if key in part} for part in selected['parts']],
                   'files': files, 'installed': time.time(), 'publisher_verified': True, 'inference_tested': False}
        json_write(staging / 'forge-dubbing-model.json', receipt)
        _verify_bundle(staging)
        # Keep the previous active tree intact until the complete replacement is
        # verified. A failed rename/activation restores it; no pip ever edits it.
        previous = root.with_name('.retained-' + root.name + '-' + uuid.uuid4().hex) if root.exists() else None
        journal = data / 'dubbing' / 'locks' / (component + '.activation.json')
        json_write(journal, {'component': component, 'revision': selected['model_revision'],
                            'destination': str(root), 'previous': str(previous) if previous else '',
                            'staging': str(staging), 'status': 'prepared'})
        try:
            if previous:
                root.rename(previous)
            staging.rename(root)
            _activate(component, data, receipt['model_revision'])
            json_write(journal, {'component': component, 'revision': selected['model_revision'],
                                'status': 'completed', 'previous': str(previous) if previous else ''})
        except BaseException:
            if previous and previous.exists():
                if root.exists():
                    root.rename(root.with_name('.failed-' + root.name + '-' + uuid.uuid4().hex))
                previous.rename(root)
            raise
        return {'status': 'ready', 'component': component, 'model_revision': receipt['model_revision'],
                'runtime_revision': receipt['runtime_id'], 'download_bytes': selected['download_bytes'],
                'cached': False, 'inference_verified': False, 'device': selected['device']}


def _activate(component, data, revision):
    with cache_lock(Path(data) / 'dubbing' / 'locks' / 'selection.lock'):
        path = _state_path(data); state = _read(path)
        before = state.setdefault('selected', {}).get(component)
        if before and before != revision:
            state.setdefault('previous_selections', {}).setdefault(component, []).append(before)
        state['selected'][component] = revision
        json_write(path, state)


def _verify_bundle(root, *, fast=False):
    root = Path(root); receipt = _read(root / 'forge-dubbing-model.json')
    if root.is_symlink() or not receipt.get('publisher_verified') or not REVISION.fullmatch(receipt.get('model_revision', '')) or not receipt.get('files'):
        raise ComponentUnavailable('This model has no complete verified installation receipt. Use Repair.')
    for name, expected in receipt['files'].items():
        _safe_relative(name); path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise ComponentUnavailable('A model artifact is missing or linked outside its installation. Use Repair.')
        stat = path.stat()
        if stat.st_size != expected.get('size') or (not fast or stat.st_mtime_ns != expected.get('mtime_ns')) and _hash(path) != expected.get('sha256'):
            raise ComponentUnavailable('A model artifact failed integrity verification. Use Repair.')
    return receipt


def _runtime_env(environment):
    return {**os.environ, 'PYTHONPATH': str(Path(environment) / 'upstream'),
            'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
            'TORCH_FORCE_WEIGHTS_ONLY_LOAD': '1', 'DO_NOT_TRACK': '1'}


def _runtime_probe(environment, marker):
    expected = {item['package']: item['version'] for item in marker.get('dependency_wheels', [])}
    code = ('import importlib.metadata as m,json; expected=json.loads(' + repr(json.dumps(expected)) + '); '
            'bad={name:(m.version(name),version) for name,version in expected.items() if m.version(name)!=version}; '
            'assert not bad,"The isolated dependency versions differ from their approved receipt."')
    _run([_python(environment), '-c', code], env=_runtime_env(environment), timeout=120)
    _run([_python(environment), '-m', 'pip', 'check'], env=_runtime_env(environment), timeout=120)
    if marker.get('smoke'):
        _run([_python(environment), '-c', marker['smoke']], env=_runtime_env(environment), timeout=180)


def _verify_runtime(environment, *, full=False):
    environment = Path(environment); marker = _read(environment / 'forge-dubbing-runtime.json')
    if (environment.is_symlink() or not _python(environment).is_file() or not marker.get('import_tested')
            or marker.get('id') != environment.name or not RUNTIME_ID.fullmatch(environment.name)):
        raise ComponentUnavailable('The isolated model environment is missing or incomplete. Use Repair.')
    for item in marker.get('sources', []):
        _safe_relative(item.get('path', ''))
        source = environment / 'upstream' / item['path']
        if (source.is_symlink() or not source.resolve().is_relative_to((environment / 'upstream').resolve())
                or not source.is_file() or source.stat().st_size != item.get('size') or _hash(source) != item.get('sha256')):
            raise ComponentUnavailable('A reviewed runtime source file failed verification. Use Repair.')
    if full:
        _runtime_probe(environment, marker)
    return marker


def resolve(component, data, revision=''):
    _spec(component); data = Path(data).resolve()
    revision = revision or _read(_state_path(data)).get('selected', {}).get(component, '')
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise ComponentUnavailable('Install and verify this Dubbing Studio component first.')
    for storage in _roots(data):
        root = _component_root(storage, component) / revision
        if not root.is_dir():
            journal = _read(data / 'dubbing' / 'locks' / (component + '.activation.json'))
            previous = Path(journal.get('previous', ''))
            # Recover a process killed between the two final directory renames.
            # Only a retained sibling carrying this exact component may return.
            if (journal.get('status') == 'prepared' and journal.get('revision') == revision
                    and previous.is_absolute() and previous.parent == root.parent
                    and re.fullmatch(r'\.retained-' + re.escape(revision) + r'-[0-9a-f]{32}', previous.name)
                    and previous.is_dir()):
                with cache_lock(data / 'dubbing' / 'locks' / (component + '.lock'), timeout=2):
                    if not root.exists():
                        _verify_bundle(previous, fast=True)
                        previous.rename(root)
            if not root.is_dir():
                continue
        receipt = _verify_bundle(root, fast=True)
        if receipt.get('component') != component or receipt.get('model_revision') != revision or not RUNTIME_ID.fullmatch(receipt.get('runtime_id', '')):
            raise ComponentUnavailable('The model installation receipt belongs to a different component.')
        environment = data / 'environments' / receipt['runtime_id']; runtime = _verify_runtime(environment)
        return {'python': _python(environment), 'root': root, 'revision': revision, 'model_revision': revision,
                'runtime_revision': environment.name, 'device': receipt['device'],
                'paths': {part['id']: root / 'parts' / part['id'] for part in receipt['parts']},
                'environment': environment, 'receipt': receipt}
    raise ComponentUnavailable('The selected model revision is unavailable. Repair it without changing the existing speech environments.')


def verify(component, data, *, revision='', progress=None, **kwargs):
    value = resolve(component, data, revision)
    receipt = _verify_bundle(value['root'])
    _verify_runtime(value['environment'], full=True)
    if _spec(component).get('smoke'):
        _run([value['python'], '-c', _spec(component)['smoke']], env=_runtime_env(value['environment']), timeout=180)
    return {'status': 'passed', 'component': component, 'model_revision': receipt['model_revision'],
            'runtime_revision': value['runtime_revision'], 'files_verified': len(receipt['files']),
            'bytes_verified': sum(file['size'] for file in receipt['files'].values()), 'inference_verified': False}


def invoke(component, data, request, *, progress=None):
    if not isinstance(request, dict):
        raise ValueError('A structured local model request is required.')
    spec = _spec(component); installed = resolve(component, data, request.get('model_revision', ''))
    desired = request.get('device', installed['device'])
    if desired == 'auto':
        desired = installed['device']
    if desired != installed['device']:
        raise ComponentUnavailable('The selected device differs from this isolated runtime. Review installation of the compatible device profile.')
    script = (ROOT / spec['runner']).resolve()
    if not script.is_relative_to(ROOT / 'scripts') or not script.is_file():
        raise ComponentUnavailable('The registered model runtime adapter is unavailable.')
    directory = Path(data) / 'dubbing' / 'requests'; directory.mkdir(parents=True, exist_ok=True)
    identity = uuid.uuid4().hex; request_path = directory / (identity + '.json'); response_path = directory / (identity + '.result.json')
    value = {**copy.deepcopy(request), 'component': component, 'device': desired, 'data': str(Path(data).resolve()),
             'model_revision': installed['model_revision'], 'runtime_revision': installed['runtime_revision'],
             'model_dir': str(installed['root']), 'model_paths': {key: str(path) for key, path in installed['paths'].items()},
             'response': str(response_path), 'response_path': str(response_path)}
    json_write(request_path, value)
    env = _runtime_env(installed['environment'])
    started = time.monotonic()
    try:
        with subprocess.Popen([str(installed['python']), str(script), '--request', str(request_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                              env=env, **no_window()) as process:
            # Drain both pipes concurrently. Torch warnings must never fill
            # stderr and block a real inference process or cancellation.
            events = queue.Queue(); stderr_tail = deque(maxlen=32)
            stdout_done = threading.Event()
            def stdout_reader():
                try:
                    for line in iter(lambda: process.stdout.readline(16384), ''):
                        if len(line) < 16384:
                            try:
                                event = json.loads(line)
                            except ValueError:
                                continue
                            if isinstance(event, dict) and event.get('kind') == 'progress':
                                if events.qsize() < 1000:
                                    events.put(event)
                finally:
                    stdout_done.set()
            def stderr_reader():
                for block in iter(lambda: process.stderr.read(4096), ''):
                    stderr_tail.append(block)
            readers = [threading.Thread(target=stdout_reader, daemon=True), threading.Thread(target=stderr_reader, daemon=True)]
            for reader in readers:
                reader.start()
            deadline = time.monotonic() + INFERENCE_TIMEOUT_SECONDS
            try:
                while process.poll() is None or not stdout_done.is_set() or not events.empty():
                    if time.monotonic() > deadline:
                        raise ComponentUnavailable('The local model exceeded its bounded inference time. Completed project files remain available; retry a smaller selection.')
                    try:
                        event = events.get(timeout=.1)
                    except queue.Empty:
                        continue
                    number = event.get('progress', 0)
                    if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                        number = 0
                    _emit(progress, number, str(event.get('message', 'Processing local model'))[:250],
                          stage=str(event.get('stage', 'Local model inference'))[:100], progress_measured=bool(event.get('progress_measured')),
                          segment_complete=bool(event.get('segment_complete')), segment_id=str(event.get('segment_id', ''))[:100],
                          completed_segments=event.get('completed_segments') if type(event.get('completed_segments')) is int else None)
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5)
                raise
            finally:
                for reader in readers:
                    reader.join(timeout=2)
            process.wait(timeout=5)
            stderr = ''.join(stderr_tail)
            result = _read(response_path)
            if process.returncode or result.get('success') is False or result.get('status') == 'failed' or not result:
                raise ComponentUnavailable('Local dubbing inference failed. ' + str(result.get('error') or _diagnostic(stderr) or 'Use Test Model or Repair.'))
        if result.get('model_revision', installed['model_revision']) != installed['model_revision']:
            raise ComponentUnavailable('The inference result belongs to a different model revision.')
        result.setdefault('model_revision', installed['model_revision']); result.setdefault('runtime_revision', installed['runtime_revision'])
        result['elapsed_seconds'] = time.monotonic() - started
        return result
    finally:
        request_path.unlink(missing_ok=True)
        response_path.unlink(missing_ok=True)


def test(component, data, *, request=None, progress=None, **kwargs):
    spec = _spec(component); value = copy.deepcopy(request or {})
    if spec['kind'] == 'translation':
        value.update(task='translate', segments=value.get('segments') or [{'id': 'test', 'start': 0, 'end': 1, 'speaker': 'test', 'source_text': 'Hello. This is a local translation test.'}])
    elif spec['kind'] == 'tts':
        if value.get('consent') is not True or not value.get('ref_audio'):
            raise ValueError('Choose a permitted voice reference and approve its use before testing Russian synthesis.')
        output = Path(data) / 'dubbing' / 'model-tests' / (uuid.uuid4().hex + '.wav'); output.parent.mkdir(parents=True, exist_ok=True)
        value.update(task='synthesize', text=value.get('text') or 'Здравствуйте. Это проверка русского голоса.', output=str(output))
    elif spec['kind'] == 'asr':
        if not value.get('source'):
            raise ValueError('Choose a permitted speech recording before testing transcription.')
        value.update(task='transcribe')
    else:
        if not value:
            raise ValueError('Select an approved source and preview region to test this optional component.')
    result = invoke(component, data, value, progress=progress)
    if result.get('inference_tested') is not True:
        raise ComponentUnavailable('The component did not produce actual model-inference evidence. A load-only check is not a successful audible model test.')
    path = Path(data) / 'dubbing' / 'model-tests.json'
    with cache_lock(path.with_suffix('.lock')):
        records = _read(path); records[component] = {'model_revision': result['model_revision'], 'runtime_revision': result['runtime_revision'], 'checked': time.time(),
            'status': 'passed', 'native_windows_execution': os.name == 'nt', 'device': result.get('actual_device') or resolve(component, data)['device'],
            'elapsed_seconds': result['elapsed_seconds'], 'inference_tested': True}
        json_write(path, records)
    return {**result, 'status': 'passed', 'inference_tested': True}


def catalog(data):
    values = []
    checks = _read(Path(data) / 'dubbing' / 'model-tests.json')
    for component, spec in _specs().items():
        item = copy.deepcopy(spec)
        pins_ready = bool(spec.get('parts')) and all(REVISION.fullmatch(part.get('revision', '')) for part in spec['parts'])
        item.update(installed=False, ready=False, model_revision='', runtime_revision='', download_bytes=None,
                    artifact_inventory_verified=pins_ready, installation_available=pins_ready and not bool(spec.get('unavailable_reason')),
                    inference_verified=False, windows_model_verified=False)
        try:
            selected = resolve(component, data)
            item.update(installed=True, ready=True, model_revision=selected['model_revision'], runtime_revision=selected['runtime_revision'],
                        device=selected['device'], model_bytes=sum(f['size'] for f in selected['receipt']['files'].values()))
            check = checks.get(component, {})
            if (check.get('model_revision') == selected['model_revision'] and check.get('runtime_revision') == selected['runtime_revision']
                    and check.get('status') == 'passed' and check.get('inference_tested') is True and check.get('device') == selected['device']):
                item['inference_verified'] = True
                item['windows_model_verified'] = bool(check.get('native_windows_execution'))
        except (ValueError, ComponentUnavailable):
            pass
        if not pins_ready:
            item.setdefault('unavailable_reason', 'Exact model artifacts are awaiting publisher verification. This component is not Ready.')
        # Sources are implementation receipts, not extra settings cards.
        item.pop('smoke', None); item.pop('sources', None)
        values.append(item)
    return values


_WHISPER_CHECKS = {}


def existing_whisper_state(data, model='large-v3-turbo'):
    """Inspect the existing cache and compatible imports without changing it."""
    from .model_cache import REPOSITORIES, inventory
    if model not in REPOSITORIES:
        raise ValueError('Choose a registered Whisper model.')
    required = [('faster-whisper', 'faster_whisper', '1.2.1'), ('ctranslate2', 'ctranslate2', '4.6.0'),
                ('av', 'av', '18.1.0'), ('setuptools', 'pkg_resources', '80.9.0')]
    packages = []
    for distribution, module, expected in required:
        try:
            version = importlib.metadata.version(distribution)
            present = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            version, present = '', False
        packages.append({'package': distribution, 'version': version, 'compatible': bool(present and version == expected)})
    compatible = all(item['compatible'] for item in packages)
    key = tuple((item['package'], item['version'], item['compatible']) for item in packages)
    if compatible:
        previous = _WHISPER_CHECKS.get(key)
        if not previous or time.monotonic() - previous[0] > 60:
            try:
                _run([sys.executable, '-c', 'import faster_whisper,ctranslate2,av,pkg_resources'], timeout=12)
                previous = (time.monotonic(), True)
            except (OSError, ComponentUnavailable, subprocess.SubprocessError):
                previous = (time.monotonic(), False)
            _WHISPER_CHECKS[key] = previous
        compatible = previous[1]
    entries = inventory(Path(data)); selected = next((entry for entry in entries if entry['model'] == model), {})
    cached = bool(selected.get('cached'))
    return {'ready': compatible and cached, 'dependencies_ready': compatible, 'model_cached': cached,
            'model': model, 'model_revision': selected.get('selected', ''), 'packages': packages, 'installed_models': entries,
            'repair_component': 'whisper-' + model,
            'reason': '' if compatible and cached else 'Install the isolated Whisper component through Set Up Dubbing Studio; the existing environment and cache are retained.'}


def setup_state(data, settings=None):
    info = hardware(data, settings)
    models = catalog(data)
    try:
        whisper_state = existing_whisper_state(data)
    except (OSError, ValueError):
        whisper_state = {'ready': False, 'installed_models': [], 'dependencies_ready': False, 'model_cached': False}
    recommended = [item for item in models if item.get('recommended')]
    isolated_whisper = next((item for item in models if item['id'] == 'whisper-large-v3-turbo'), {})
    asr_ready = whisper_state['ready'] or bool(isolated_whisper.get('ready'))
    if not asr_ready:
        recommended.extend([item for item in models if item['id'] == 'whisper-large-v3-turbo'])
    return {'title': 'Russian Dubbing', 'hardware': info, 'models': models,
            'recommended_components': [item['id'] for item in recommended],
            'missing_recommended': [item['id'] for item in recommended if not item['ready']],
            'ready': all(item['ready'] for item in recommended) and info['ffmpeg'] and info['ffprobe'] and asr_ready,
            'speech_recognition': {**whisper_state, 'ready': asr_ready, 'isolated_ready': bool(isolated_whisper.get('ready'))},
            'audio_processing': {'ready': info['ffmpeg'] and info['ffprobe']},
            'guided_runtime': {'supported': info['application_python_compatible'] or info['managed_python_available'],
                               'installation_needed': not info['application_python_compatible'], 'managed_python': copy.deepcopy(MANAGED_PYTHON)},
            'model_storage': str(_roots(data)[0]), 'application_environments_preserved': True,
            'physical_gpu_tested': False, 'manual_python_required': False}


def move_storage(data, destination, *, consent=False, progress=None, **kwargs):
    if consent is not True:
        raise ValueError('Approve the new model-storage destination before moving verified components.')
    data = Path(data).resolve(); destination = Path(destination).expanduser()
    if not destination.is_absolute() or destination.is_symlink() or str(destination).startswith(('\\\\', '//')):
        raise ValueError('Choose a complete local model-storage path.')
    destination = destination.resolve(); source = _roots(data)[0]
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('Choose a separate model-storage directory.')
    with cache_lock(data / 'dubbing' / 'locks' / 'storage.lock'):
        destination.mkdir(parents=True, exist_ok=True)
        copied = []
        for component in _specs():
            for bundle in _component_root(source, component).glob('*'):
                if not bundle.is_dir() or not REVISION.fullmatch(bundle.name):
                    continue
                _verify_bundle(bundle)
                target = _component_root(destination, component) / bundle.name
                if target.exists():
                    _verify_bundle(target)
                    if _read(target / 'forge-dubbing-model.json') != _read(bundle / 'forge-dubbing-model.json'):
                        raise ComponentUnavailable('The destination contains a different model receipt. Existing files were not replaced.')
                else:
                    staging = destination / component / ('.copy-' + uuid.uuid4().hex); staging.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(bundle, staging, symlinks=False); _verify_bundle(staging); staging.rename(target)
                copied.append({'component': component, 'revision': bundle.name})
        state = _read(_state_path(data)); state['storage_root'] = str(destination)
        state['previous_roots'] = list(dict.fromkeys([str(source), *state.get('previous_roots', [])]))
        json_write(_state_path(data), state)
    return {'status': 'passed', 'destination': str(destination), 'components': copied,
            'previous_storage_retained': True, 'note': 'Previous verified storage is retained for queued work and rollback. Remove Unused performs a separate conservative cleanup.'}


def _referenced_revisions(data):
    # Conservatively retain every hash referenced in saved records or queue data.
    result = set()
    database = Path(data) / 'forge.sqlite3'
    if not database.is_file():
        return result
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'") if re.fullmatch(r'[A-Za-z0-9_]+', row[0])]
        for table in tables:
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")') if row[1] in {'data', 'value', 'payload', 'options', 'result', 'metadata'}]
            for column in columns:
                rows = connection.execute(f'SELECT "{column}" FROM "{table}" LIMIT 200001').fetchall()
                if len(rows) > 200000:
                    raise ComponentUnavailable('The reference inventory is too large for automatic removal. No models were deleted.')
                for row in rows:
                    if isinstance(row[0], str):
                        result.update(re.findall(r'(?<![a-f0-9])[a-f0-9]{64}|(?<![a-f0-9])[a-f0-9]{40}(?![a-f0-9])', row[0]))
    return result


def remove_unused(component, data, *, consent=False, used_revisions=None, **kwargs):
    _spec(component)
    if consent is not True:
        raise ValueError('Approve removal of unused Dubbing Studio model revisions first.')
    protected = _referenced_revisions(data) | set(used_revisions or [])
    state = _read(_state_path(data)); protected.update(state.get('selected', {}).values())
    for values in state.get('previous_selections', {}).values():
        protected.update(values[-1:])  # Keep at least the last rollback choice.
    removed = []
    with cache_lock(Path(data) / 'dubbing' / 'locks' / 'storage.lock'):
        for storage in _roots(data):
            for root in _component_root(storage, component).glob('*'):
                if not root.is_dir() or root.is_symlink() or not REVISION.fullmatch(root.name) or root.name in protected:
                    continue
                receipt = _verify_bundle(root)
                if receipt['component'] != component:
                    raise ComponentUnavailable('An unexpected component appeared in model storage; cleanup stopped.')
                shutil.rmtree(root); removed.append(root.name)
    return {'status': 'passed', 'removed': removed, 'protected_revision_count': len(protected), 'other_environments_untouched': True}


def repair(component, data, *, consent=False, plan=None, progress=None, device='auto', **kwargs):
    return install(component, data, consent=consent, plan=plan, progress=progress, device=device, repair=True)


move_model_storage = move_storage
check_compatibility = compatibility
