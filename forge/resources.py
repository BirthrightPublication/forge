"""Measured host telemetry and conservative admission for local worker processes.

VRAM figures in MODEL_MEMORY_MB are planning estimates, never measurements.
Workers use CUDA device zero, so only that device is budgeted here. Independent
worker processes cannot share a loaded model; matching GPU models run serially.
"""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import psutil

from .core import no_window

MODEL_MEMORY_MB = {'tiny': 700, 'base': 900, 'small': 1800, 'medium': 3500,
                   'large-v3': 6500, 'large-v3-turbo': 4500}
ENCODER_MEMORY_MB = 768  # Planning allowance for NVENC; not a measured allocation.
MEDIA_ANALYSIS = {'audio_fingerprint', 'video_fingerprint', 'tracking_audit',
                  'provenance', 'privacy_verify'}


def bounded(value, default=1, maximum=5):
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def primary_resource(job):
    tool = job['tool']
    options = job.get('payload', {}).get('options', {})
    if tool.startswith('download') or tool == 'model_download':
        return 'network'
    if tool == 'speech_install':
        return 'network'
    if tool == 'tts' and options.get('device','cpu') != 'cpu':
        return 'gpu'
    if tool == 'transcribe' and job.get('payload', {}).get('resolved_device', options.get('device', 'auto')) != 'cpu':
        # If CUDA is unavailable the worker may fall back to CPU. Keep one
        # conservative inference lane rather than assume GPU memory is known.
        return 'gpu'
    if (tool == 'watermark' and options.get('mode', 'video') in {'video', 'both'}
            and options.get('encoder', 'auto') in {'auto', 'nvenc'}):
        return 'gpu'
    if tool == 'metadata':
        return 'disk'
    return 'cpu'


def required_resources(job):
    resource = primary_resource(job)
    result = {resource}
    if job['tool'] in {'retune', 'download_audio', 'download_video', 'model_download',
                       'watermark', 'privacy_export','tts','audio_effects','speech_install'} | MEDIA_ANALYSIS:
        result.add('disk')
    if job['tool'] == 'watermark':
        # Overlay/audio filters and CPU fallback still consume a CPU lane.
        result.add('cpu')
    return result


def model_budget_mb(job):
    if job['tool'] == 'tts':
        engine = job.get('payload',{}).get('options',{}).get('engine','kokoro')
        return {'kokoro':2000,'ming':6000,'ming-0.5b':6000,'ming-tiny':24000}.get(engine,12000)
    if job['tool'] == 'watermark':
        return ENCODER_MEMORY_MB
    options = job.get('payload', {}).get('options', {})
    budget = MODEL_MEMORY_MB.get(options.get('model', 'large-v3'), 6500)
    return budget + (2048 if options.get('diarization') else 0)


class ResourceMonitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.cached = None
        self.checked = 0.0

    @staticmethod
    def nvidia():
        binary = shutil.which('nvidia-smi')
        if not binary:
            return {'available': False, 'source': 'unavailable', 'devices': [],
                    'reason': 'NVIDIA memory telemetry is unavailable; GPU inference is serialized.'}
        try:
            result = subprocess.run(
                [binary, '--query-gpu=index,uuid,name,memory.total,memory.free', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=2, check=True, **no_window())
            devices = []
            for row in csv.reader(io.StringIO(result.stdout)):
                if len(row) != 5:
                    continue
                index, gpu_uuid, name, total, free = [value.strip() for value in row]
                total, free = int(total), int(free)
                if int(index) < 0 or total <= 0 or not 0 <= free <= total:
                    continue
                devices.append({'index': int(index), 'uuid': gpu_uuid, 'name': name[:160], 'total_mb': total, 'free_mb': free})
            # ctranslate2 defaults to visible device zero, which can be remapped
            # by an inherited CUDA mask. Do not budget a different physical GPU.
            visible = os.environ.get('CUDA_VISIBLE_DEVICES', '0').split(',')[0].strip()
            if visible.startswith('GPU-'):
                matches = [device for device in devices if device['uuid'].startswith(visible)]
            elif visible == '0' and len(devices) == 1:
                matches = devices
            else:
                matches = []
            if len(matches) != 1:
                # CUDA FASTEST_FIRST ordinals need not match NVML indices on
                # multi-GPU hosts. Guessing can budget the wrong physical card.
                return {'available': False, 'source': 'nvidia-smi', 'devices': devices,
                        'reason': 'CUDA device mapping is ambiguous. GPU inference is serialized; use a GPU UUID in CUDA_VISIBLE_DEVICES for memory admission checks.'}
            device_index = matches[0]['index']
            return {'available': True, 'source': 'nvidia-smi', 'devices': devices,
                    'admission_device_index': device_index, 'checked_at': time.time()}
        except (OSError, ValueError, subprocess.SubprocessError):
            return {'available': False, 'source': 'unavailable', 'devices': [],
                    'reason': 'NVIDIA memory query failed; GPU inference is serialized.'}

    def sample(self, data):
        with self.lock:
            now = time.monotonic()
            if self.cached is not None and now - self.checked < 1:
                return self.cached
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage(str(data))
            self.cached = {'cpu_percent': psutil.cpu_percent(),
                           'cpu_count': psutil.cpu_count(logical=False) or psutil.cpu_count() or 1,
                           'memory_available_mb': memory.available // 1048576,
                           'memory_total_mb': memory.total // 1048576,
                           'disk_free_mb': disk.free // 1048576,
                           'gpu': self.nvidia(), 'checked_at': time.time()}
            self.checked = now
            return self.cached


def admission_state(settings, active_jobs, metrics):
    mode = settings.get('concurrency_mode', 'auto')
    mode = mode if mode in {'auto', 'manual'} else 'auto'
    # Auto adjusts admission beneath the configured ceiling; new installations
    # default to five, while explicit existing worker choices remain respected.
    maximum = bounded(settings.get('workers', 5), 5)
    available = metrics['memory_available_mb']
    memory_pressure = available < max(512, metrics['memory_total_mb'] * .06)
    cpu_pressure = metrics['cpu_percent'] >= 92
    effective = maximum
    if mode == 'auto':
        effective = min(maximum, max(2, metrics['cpu_count'] + 2))
        if memory_pressure:
            effective = 1
        elif cpu_pressure:
            effective = min(effective, 2)
    limits = {'cpu': bounded(settings.get('cpu_workers', 2), 2),
              'gpu': bounded(settings.get('gpu_workers', 1)),
              'network': bounded(settings.get('download_workers', 2), 2),
              'disk': bounded(settings.get('disk_workers', 2), 2)}
    if cpu_pressure and mode == 'auto':
        limits['cpu'] = 1
    if not metrics['gpu']['available']:
        limits['gpu'] = 1
    usage = {kind: sum(kind in required_resources(job) for job in active_jobs) for kind in limits}
    return {'mode': mode, 'maximum_workers': maximum, 'effective_workers': effective,
            'limits': limits, 'usage': usage, 'memory_pressure': memory_pressure,
            'cpu_pressure': cpu_pressure, 'metrics': metrics,
            'gpu_budget': {'kind': 'conservative_estimate', 'model_memory_mb': MODEL_MEMORY_MB,
                           'encoder_memory_mb': ENCODER_MEMORY_MB,
                           'reserve_mb': max(256, min(16384, int(settings.get('gpu_memory_reserve_mb', 1024)))),
                           'reserved_for_active_mb': sum(model_budget_mb(job) for job in active_jobs if primary_resource(job) == 'gpu'),
                           'note': 'Model and encoder budgets are separate planning estimates, subtracted from measured free memory as an additional safety margin. Matching GPU models and video encoders run one at a time; pressure delays new starts without stopping active jobs.'}}


def admission_reason(job, active_jobs, state):
    if len(active_jobs) >= state['effective_workers']:
        return 'Waiting for an available worker slot.'
    for resource in required_resources(job):
        if state['usage'][resource] >= state['limits'][resource]:
            return f'Waiting for the {resource} resource limit.'
    if 'disk' in required_resources(job) and state['metrics']['disk_free_mb'] < 128:
        return 'Waiting for free space on the application data drive (less than 128 MiB available).'
    if 'disk' in required_resources(job) and job.get('payload', {}).get('destination'):
        destination = Path(job['payload']['destination'])
        try:
            # A queued destination may have been removed since validation. Its
            # nearest existing parent still identifies the output filesystem.
            while not destination.exists() and destination != destination.parent:
                destination = destination.parent
            if psutil.disk_usage(str(destination)).free < 128 * 1048576:
                return 'Waiting for free space on the output drive (less than 128 MiB available).'
        except OSError:
            return 'Waiting for the output drive to become available.'
    if state['memory_pressure'] and job['tool'] != 'qr':
        return 'Waiting for available system memory.'
    if job['tool'] == 'model_download' and any(j['tool'] in {'transcribe', 'model_download'} for j in active_jobs):
        return 'Waiting for exclusive access to the local model cache.'
    if job['tool'] == 'transcribe' and any(j['tool'] == 'model_download' for j in active_jobs):
        return 'Waiting for the local model download to finish.'
    if primary_resource(job) == 'gpu':
        options = job.get('payload', {}).get('options', {})
        gpu_jobs = [j for j in active_jobs if primary_resource(j) == 'gpu']
        if job['tool'] == 'watermark' and any(j['tool'] == 'watermark' for j in gpu_jobs):
            return 'Waiting for the GPU video encoder lane.'
        if job['tool'] == 'transcribe' and any(j['tool'] == 'transcribe' and j.get('payload', {}).get('options', {}).get('model', 'large-v3') == options.get('model', 'large-v3') for j in gpu_jobs):
            return 'Waiting to reuse the GPU lane; duplicate model loads are serialized.'
        gpu = state['metrics']['gpu']
        if gpu['available']:
            device = next((d for d in gpu['devices'] if d['index'] == gpu.get('admission_device_index', 0)), None)
            budget = state['gpu_budget']
            required = model_budget_mb(job) + budget['reserve_mb'] + budget['reserved_for_active_mb']
            if device is None or device['free_mb'] < required:
                return 'Waiting for GPU memory; admission uses measured free VRAM and conservative model or encoder estimates.'
    return ''
