"""CUDA evidence: enumeration and DLL loading never imply successful inference."""
from __future__ import annotations

import ctypes
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .core import json_write, no_window

_handles = []
DLLS = ('cublas64_12.dll', 'cublasLt64_12.dll', 'cudnn64_9.dll')


def add_runtime_paths():
    if os.name != 'nt':
        return
    paths = [Path(sys.prefix) / 'Lib' / 'site-packages' / 'nvidia' / name / 'bin'
             for name in ('cublas', 'cudnn', 'cuda_runtime')]
    if os.getenv('CUDA_PATH'):
        paths.append(Path(os.environ['CUDA_PATH']) / 'bin')
    for path in paths:
        if path.is_dir():
            _handles.append(os.add_dll_directory(str(path)))
            # CTranslate2 also uses LoadLibrary; retain paths for child libraries.
            if str(path) not in os.environ.get('PATH', '').split(os.pathsep):
                os.environ['PATH'] = str(path) + os.pathsep + os.environ.get('PATH', '')


def runtime_report() -> dict:
    result = {'checked': time.time(), 'cuda_devices': 0, 'compute_types': [], 'notes': [],
              'inference_tested': False, 'inference_passed': False, 'ready': False,
              'dlls': {}, 'required_runtime': 'CUDA 12 cuBLAS and cuDNN 9 for the pinned CTranslate2 engine'}
    smi = shutil.which('nvidia-smi')
    if smi:
        try:
            p = subprocess.run([smi, '--query-gpu=name,driver_version,memory.total', '--format=csv,noheader'],
                               capture_output=True, text=True, timeout=10, **no_window())
            result['gpu'] = p.stdout.strip()[:2000]
        except (OSError, subprocess.TimeoutExpired) as exc:
            result['notes'].append('NVIDIA diagnostic: ' + str(exc)[:400])
    add_runtime_paths()
    if os.name == 'nt':
        for name in DLLS:
            try:
                _handles.append(ctypes.WinDLL(name))
                result['dlls'][name] = {'loaded': True}
            except OSError as exc:
                result['dlls'][name] = {'loaded': False, 'error': str(exc)[:300]}
    try:
        import ctranslate2
        result['ctranslate2'] = importlib.metadata.version('ctranslate2')
        result['cuda_devices'] = ctranslate2.get_cuda_device_count()
        if result['cuda_devices']:
            result['compute_types'] = sorted(ctranslate2.get_supported_compute_types('cuda'))
    except Exception as exc:
        result['notes'].append('CTranslate2: ' + str(exc)[:400])
    result['runtime_available'] = bool(result['cuda_devices']) and all(d['loaded'] for d in result['dlls'].values())
    result['notes'].append('NVIDIA-SMI CUDA capability describes the driver, not installed cuBLAS/cuDNN libraries. GPU readiness requires successful inference.')
    return result


def failure_message(report):
    missing = ', '.join(k for k, v in report.get('dlls', {}).items() if not v['loaded'])
    return ('CUDA is unavailable' + (': ' + missing if missing else '') +
            '. Open Updates → GPU runtime for diagnostics. Install the compatible NVIDIA CUDA 12 runtime and cuDNN 9, then run the inference check. CPU remains available.')


def remember_failure(data: Path, message: str):
    json_write(data / 'cuda-health.json', {'checked': time.time(), 'ready': False,
                                          'failure': message[:1600], 'python': sys.executable})


def recent_failure(data: Path):
    try:
        report = json.loads((data / 'cuda-health.json').read_text('utf-8'))
        if report.get('python') == sys.executable and not report.get('ready') and time.time() - report['checked'] < 3600:
            return report.get('failure', '')
    except (OSError, ValueError, KeyError):
        pass
    return ''


def inference_report(data: Path, model: str):
    report = runtime_report()
    if not report['runtime_available']:
        report['failure'] = failure_message(report)
    else:
        try:
            from .model_cache import ensure_model
            from faster_whisper import WhisperModel
            import numpy as np
            path = ensure_model(data, model, download=False)
            engine = WhisperModel(str(path), device='cuda', compute_type='float16', local_files_only=True)
            report['inference_tested'] = True
            segments, _ = engine.transcribe(np.zeros(16000, dtype=np.float32), language='en',
                                           beam_size=1, vad_filter=False, condition_on_previous_text=False)
            list(segments)  # Inference is lazy; constructing the generator is not a test.
            report['inference_passed'] = report['ready'] = True
            report['model'] = model
        except Exception as exc:
            report['failure'] = str(exc)[:1600]
    report['python'] = sys.executable
    json_write(data / 'cuda-health.json', report)
    return report
