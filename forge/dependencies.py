"""Reviewed dependency classes and their existing installation locations."""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from .core import ROOT, executable, no_window

ENGINES = {
    'core': {'label': 'Python processing packages', 'requirements': 'requirements-core.txt', 'environment': '.venv'},
    'transcription': {'label': 'Transcription engine', 'requirements': 'requirements-transcription.txt', 'environment': '.venv'},
    'downloads': {'label': 'Download engine', 'requirements': 'requirements-downloads.txt', 'environment': '.venv'},
    'speakers': {'label': 'Speaker labeling', 'requirements': 'requirements-diarization.txt', 'environment': '.diarization-venv'},
    'media-privacy': {'label': 'Content Credentials', 'requirements': 'scripts/requirements-media-privacy.txt', 'environment': '.venv'},
}
# PyPI wheels only. Updates stay inside these reviewed requirements; app releases
# carry changes to the compatibility contract, not arbitrary latest-version installs.
PACKAGES = {
    'core': [('fastapi', 'fastapi'), ('uvicorn', 'uvicorn'), ('pillow', 'PIL'), ('qrcode', 'qrcode'), ('psutil', 'psutil')],
    'transcription': [('faster-whisper', 'faster_whisper'), ('ctranslate2', 'ctranslate2')],
    'downloads': [('yt-dlp', 'yt_dlp')], 'speakers': [('pyannote.audio', 'pyannote.audio')],
    'media-privacy': [('c2pa-python', 'c2pa')],
}


def requirements(engine, root=ROOT):
    return [line.strip() for line in (root / ENGINES[engine]['requirements']).read_text('utf-8').splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def inspect(settings, root=ROOT):
    entries = []
    for engine, spec in ENGINES.items():
        packages = []
        if engine == 'speakers':
            python = root / spec['environment'] / 'Scripts' / 'python.exe'
            if not python.is_file():
                python = root / spec['environment'] / 'bin' / 'python'
            try:
                result = subprocess.run([str(python), '-c', 'import importlib.metadata,json; print(json.dumps({"version":importlib.metadata.version("pyannote.audio")}))'],
                                        capture_output=True, text=True, timeout=8, **no_window())
                packages = [{'package': 'pyannote.audio', 'version': json.loads(result.stdout)['version'], 'present': result.returncode == 0}]
            except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                packages = [{'package': 'pyannote.audio', 'version': '', 'present': False}]
        else:
            for package, module in PACKAGES[engine]:
                try:
                    version = importlib.metadata.version(package)
                    present = importlib.util.find_spec(module) is not None
                except (ImportError, ValueError):
                    version, present = '', False
                packages.append({'package': package, 'version': version, 'present': present})
        reqs = requirements(engine, root)
        compatible = all(p['present'] for p in packages)
        repairable = not compatible
        if engine=='downloads':
            try:
                year=int(packages[0]['version'].split('.')[0])
                compatible &= 2026<=year<2027
                from .download_support import ejs_compatible
                compatible &= ejs_compatible()
            except (ValueError,ImportError):
                compatible=False
            repairable=not compatible
        for req in reqs:
            name, sep, version = req.partition('==')
            if sep:
                compatible &= any(p['package'] == name and p['version'] == version for p in packages)
        if compatible and engine != 'speakers':
            try:
                modules = ','.join(module for _, module in PACKAGES[engine])
                health = subprocess.run([sys.executable, '-c', 'import ' + modules], capture_output=True,
                                        timeout=10, **no_window())
                compatible = health.returncode == 0
                repairable = not compatible
            except (OSError, subprocess.TimeoutExpired):
                compatible = False
                repairable = True
        entries.append({'id': engine, 'label': spec['label'], 'category': 'dependencies',
                        'installed': ', '.join(p['package'] + ' ' + (p['version'] or 'missing') for p in packages),
                        'available': ', '.join(reqs), 'compatible': bool(compatible),
                        'repairable': repairable,
                        'action': 'repair' if not compatible else '', 'restart': True,
                        'location': str(root / spec['environment']), 'source': 'https://pypi.org/simple',
                        'packages': packages, 'rollback': 'Environment backup before changes'})
    for name in ('ffmpeg', 'ffprobe', 'exiftool'):
        path = executable(name, settings)
        detected = ''
        if path:
            try:
                result = subprocess.run([path, '-ver' if name == 'exiftool' else '-version'],
                                        capture_output=True, text=True, errors='replace', timeout=5, **no_window())
                if result.returncode == 0:
                    detected = result.stdout.splitlines()[0][:180] if result.stdout.strip() else ''
            except (OSError, subprocess.TimeoutExpired):
                pass
        entries.append({'id': name, 'label': name, 'category': 'tools', 'installed': detected or ('Unusable executable' if path else 'Not installed'),
                        'location': path or '', 'available': '', 'compatible': bool(detected), 'action': '', 'restart': False,
                        'notes': 'User-managed executable. Select a trusted compatible installation in Environment.'})
    entries.append({'id': 'gpu-runtime', 'label': 'GPU runtime', 'category': 'gpu', 'action': '',
                    'installed': 'Run diagnostics', 'available': 'CUDA 12 cuBLAS / cuDNN 9', 'restart': True,
                    'notes': 'System runtime and driver changes require explicit confirmation. Driver capability does not prove runtime availability.',
                    'links': ['https://developer.nvidia.com/cuda-downloads', 'https://developer.nvidia.com/cudnn-downloads']})
    return entries
