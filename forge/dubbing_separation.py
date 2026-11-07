"""Optional verified source separation and a practical manual-stem fallback.

No normalisation, equaliser or ducking operation is described as neural source
separation. Original narration and background are not promised to be perfectly
separable. Checkpoint approval is independent from a runtime's source licence.
"""
from __future__ import annotations

import math
from pathlib import Path
import time

from .core import absolute_path


class SeparationUnavailable(RuntimeError):
    pass


def component_specs():
    return [{
        'id': 'hdemucs-high', 'label': 'Hybrid Demucs · optional audio stems',
        'kind': 'separation', 'family': 'hdemucs', 'recommended': False, 'planning_memory_mb': 6144,
        'smoke': 'import torch, torchaudio, numpy, soundfile',
        'requirements': 'scripts/requirements-dubbing-separation.txt',
        'runner': 'scripts/dubbing_visual_runtime.py',
        'license': 'TorchAudio BSD-2-Clause / Demucs MIT; checkpoint terms need review',
        'parts': [],
        'artifacts': [{'id': 'weights', 'url': 'https://download.pytorch.org/torchaudio/models/hdemucs_high_trained.pt',
                       'path': 'weights/hdemucs_high_trained.pt', 'bytes': 334697255, 'sha256': ''}],
        'capabilities': {'tasks': ['separate', 'probe', 'test'], 'stems': ['drums', 'bass', 'other', 'vocals'],
                         'sample_rate': 44100, 'estimated_vram_mb': 6144,
                         'model_inference_validated': False, 'rtx_5080_validated': False},
        'unavailable_reason': 'Checkpoint-specific licence approval and an immutable SHA-256 are required before installing this optional model.',
        'limitations': ['Music stems are an approximation; English narration and background cannot always be isolated cleanly.',
                        'Manual background stems and ordinary ducking work without installing this model.'],
    }]


def _audio(path, settings):
    from .worker import duration, probe
    selected = absolute_path(str(path))
    info = probe(selected, settings)
    seconds = duration(info)
    if not any(s.get('codec_type') == 'audio' for s in info.get('streams', [])):
        raise ValueError('The selected stem has no playable audio.')
    if not math.isfinite(seconds) or not 0 < seconds <= 86400:
        raise ValueError('Audio stems need a known duration of at most 24 hours.')
    return selected, seconds


def process(source_audio, output_dir, options, settings, data, *, progress=None, cancel=None):
    from .worker import run_ffmpeg
    if not isinstance(options, dict):
        raise ValueError('Audio separation options must be an object.')
    started = time.monotonic()
    source, seconds = _audio(source_audio, settings)
    mode = options.get('mode', 'off')
    if mode not in {'off', 'manual', 'hdemucs', 'auto'}:
        raise ValueError('Choose original audio, manual stems or the approved separation model.')
    if cancel and cancel():
        raise InterruptedError('Audio separation cancelled; original audio was preserved.')
    if mode == 'off':
        return {'original': str(source), 'background': None, 'vocals': None, 'mode': 'off',
                'separation_performed': False, 'duration': seconds,
                'inference_tested': False,
                'warnings': ['Original audio still contains English narration. Use voice-level reduction, ducking or an approved background stem.'],
                'metrics': {'elapsed_seconds': time.monotonic() - started, 'gpu_used': False}}
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if mode == 'manual':
        if not options.get('background_path'):
            raise ValueError('Choose a local background stem, or use ordinary ducking without separation.')
        background, length = _audio(options['background_path'], settings)
        target = output / 'background.wav'
        if target.exists() or target == source or target == background:
            raise ValueError('A new stem output is required; original and existing files are never overwritten.')
        # Trim only the new copy. A shorter stem is padded, never looped without
        # a separate explicit user operation.
        run_ffmpeg(['-i', str(background), '-vn', '-af', f'apad=whole_dur={seconds:.9f}',
                    '-t', f'{seconds:.9f}', '-ar', '48000', '-ac', '2', '-c:a', 'pcm_s24le', str(target)], settings, seconds)
        _, actual = _audio(target, settings)
        if abs(actual - seconds) > .1:
            raise RuntimeError('The prepared background stem did not retain the source timeline.')
        warnings = ['Manual background stem selected; no neural separation was performed.']
        if abs(length - seconds) > .1:
            warnings.append('The new background copy was trimmed or padded with silence to match the source timeline; its original was preserved.')
        vocals = None
        if options.get('vocals_path'):
            selected, _ = _audio(options['vocals_path'], settings)
            vocals = str(selected)
        if cancel and cancel():
            raise InterruptedError('Stem preparation cancelled; original audio was preserved.')
        return {'original': str(source), 'background': str(target), 'vocals': vocals, 'mode': mode,
                'separation_performed': False, 'inference_tested': False, 'duration': actual, 'warnings': warnings,
                'metrics': {'elapsed_seconds': time.monotonic() - started, 'gpu_used': False}}
    try:
        from .dubbing_models import invoke, resolve
        spec = resolve('hdemucs-high', Path(data), revision=options.get('model_revision', ''))
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        raise SeparationUnavailable('The optional source-separation model is not ready. Use a manual background stem or ducking; '
                                    'original media remains unchanged. ' + str(exc)) from exc
    result = invoke('hdemucs-high', Path(data), {'task': 'separate', 'source': str(source),
                    'output_dir': str(output), 'device': options.get('device', 'auto'), 'model_revision': spec['model_revision'],
                    'settings': {key: settings.get(key, '') for key in ('ffmpeg', 'ffprobe')},
                    'options': {'chunk_seconds': options.get('chunk_seconds', 10),
                                'overlap_seconds': options.get('overlap_seconds', 1),
                                'gpu_reserve_mb': options.get('gpu_reserve_mb', 1024)}}, progress=progress)
    if not isinstance(result, dict) or result.get('model_inference') is not True or result.get('inference_tested') is not True:
        raise RuntimeError('No actual source-separation inference result was returned.')
    for key in ('background', 'vocals'):
        path, actual = _audio(result.get(key, ''), settings)
        if not path.is_relative_to(output) or abs(actual - seconds) > .1:
            raise RuntimeError('Source-separation output escaped its staging directory or changed the timeline.')
    if cancel and cancel():
        raise InterruptedError('Source separation cancelled; original audio was preserved.')
    return {**result, 'original': str(source), 'mode': 'hdemucs', 'separation_performed': True,
            'inference_tested': True,
            'model_revision': spec['model_revision'],
            'warnings': [*result.get('warnings', []), 'Separation is approximate; review music and speech leakage before export.'],
            'metrics': {**result.get('metrics', {}), 'elapsed_seconds': time.monotonic() - started}}
