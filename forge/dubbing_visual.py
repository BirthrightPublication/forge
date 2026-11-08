"""Optional, consented local lip synchronisation over verified model runtimes.

This adapter never downloads or executes upstream scripts. MuseTalk inference
uses a reviewed local implementation; availability requires a pinned, verified
bundle. The renderer's actual inference and hardware validation are reported
separately from its source-level compatibility checks.
"""
from __future__ import annotations

import copy
import math
from pathlib import Path
import re
import tempfile
import time

from .core import absolute_path

MUSE_SOURCE = '0a89dec45a0192b824e3cf4daf96c239440c5ed8'
_MODES = {'off': 'off', 'automatic': 'automatic', 'selected': 'selected',
          'selected_scenes': 'selected', 'full': 'full',
          'full_applicable_footage': 'full'}


class LipSyncUnavailable(RuntimeError):
    """The original video and completed narration remain valid after failure."""
    audio_preserved = True
    original_preserved = True


def component_specs():
    """Descriptors are intentionally unavailable until checkpoint pins exist.

    A source-code commit is not a model checkpoint revision. Empty revisions
    must fail the model installer's integrity gate rather than silently use main.
    """
    values = [{
        'id': 'musetalk-1.5', 'label': 'MuseTalk 1.5 · optional lip sync',
        'kind': 'lip_sync', 'family': 'musetalk', 'recommended': False, 'planning_memory_mb': 8192,
        'smoke': 'import torch, cv2, numpy, transformers, diffusers, safetensors, soundfile',
        'requirements': 'scripts/requirements-dubbing-visual.txt',
        'runner': 'scripts/dubbing_visual_runtime.py',
        'source_repository': 'TMElyralab/MuseTalk', 'source_revision': MUSE_SOURCE,
        'license': 'MuseTalk MIT; each auxiliary checkpoint retains its own terms',
        'parts': [
            {'id': 'musetalk', 'repository': 'TMElyralab/MuseTalk', 'revision': '',
             'allow_patterns': ['musetalkV15/musetalk.json', 'musetalkV15/unet.pth', 'LICENSE', 'README.md'],
             'required_files': ['musetalkV15/musetalk.json', 'musetalkV15/unet.pth'],
             'license': 'MIT; upstream permits commercial use of trained MuseTalk weights'},
            {'id': 'vae', 'repository': 'stabilityai/sd-vae-ft-mse', 'revision': '',
             'allow_patterns': ['config.json', 'diffusion_pytorch_model.safetensors', 'diffusion_pytorch_model.bin', 'README.md', 'LICENSE*'],
             'required_files': ['config.json'], 'license': 'Exact checkpoint terms require verification'},
            {'id': 'whisper', 'repository': 'openai/whisper-tiny', 'revision': '',
             'allow_patterns': ['config.json', 'preprocessor_config.json', 'model.safetensors', 'README.md', 'LICENSE*'],
             'required_files': ['config.json', 'preprocessor_config.json', 'model.safetensors'], 'license': 'MIT'},
        ],
        'capabilities': {'languages': ['ru'], 'tasks': ['lipsync', 'probe', 'test'],
                         'selected_scenes': True, 'preview': True, 'preview_seconds': [5, 10],
                         'face_consent_required': True, 'default_enabled': False,
                         'estimated_vram_mb': 8192, 'cpu_supported': True,
                         'model_inference_validated': False, 'rtx_5080_validated': False},
        'unavailable_reason': 'The exact MuseTalk, VAE and Whisper checkpoint revisions, hashes and licenses must be verified before installation.',
        'limitations': ['The modern runtime port has not yet passed model inference or owner RTX 5080 acceptance.',
                        'Automatic face proposals are not identity recognition or a guarantee of the active speaker.',
                        'MuseTalk upstream demonstration media is not redistributed.'],
    }]
    return values


def _number(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label} must be a finite number between {low} and {high}.')
    return float(value)


def _box(value, *, normalized=False):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError('A face box requires x, y, width and height.')
    bounds = 1 if normalized else 16384
    box = [_number(x, 0, bounds, 'Face coordinate') for x in value]
    if box[2] <= 0 or box[3] <= 0:
        raise ValueError('A face box must have positive width and height.')
    if normalized and (box[0] + box[2] > 1.000001 or box[1] + box[3] > 1.000001):
        raise ValueError('A normalized face box must remain inside the frame.')
    return box


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label} must be a whole number between {low} and {high}.')
    return value


def validate_options(value, *, duration=None):
    if not isinstance(value, dict):
        raise ValueError('Lip-sync options must be an object.')
    mode = _MODES.get(value.get('mode', 'off'))
    if mode is None:
        raise ValueError('Choose Off, Automatic, Selected Scenes or Full Applicable Footage.')
    if value.get('engine', 'musetalk-1.5') != 'musetalk-1.5':
        raise ValueError('This lip-sync adapter supports the verified MuseTalk 1.5 component only.')
    result = {'mode': mode, 'engine': 'musetalk-1.5', 'consent': value.get('consent') is True,
              'model_revision': value.get('model_revision', ''),
              'device': value.get('device', 'auto'), 'regions': [], 'faces': [],
              'preview': value.get('preview') is True,
              'preview_start': _number(value.get('preview_start', 0), 0, 86400, 'Preview start'),
              'preview_seconds': _number(value.get('preview_seconds', 5), 5, 10, 'Preview duration'),
              'batch_size': _integer(value.get('batch_size', 1), 1, 4, 'Frame batch size'),
              'max_height': _integer(value.get('max_height', value.get('max_resolution', 0)), 0, 2160, 'Maximum height'),
              'crf': _integer(value.get('crf', 18), 14, 30, 'Video quality'),
              'gpu_reserve_mb': _integer(value.get('gpu_reserve_mb', 1024), 256, 16384, 'GPU reserve'),
              'face_id': str(value.get('face_id') or ''),
              'speech_windows': [], 'mouth_motion_threshold': _number(value.get('mouth_motion_threshold', .004), 0, .2, 'Mouth motion threshold')}
    if result['device'] not in {'auto', 'cpu', 'cuda'}:
        raise ValueError('Choose Auto, CPU or CUDA for lip sync.')
    if not isinstance(result['model_revision'], str) or result['model_revision'] and not re.fullmatch(r'[a-f0-9]{40}(?:[a-f0-9]{24})?', result['model_revision']):
        raise ValueError('Choose an immutable verified lip-sync model revision.')
    for field in ('preview', 'consent'):
        if field in value and not isinstance(value[field], bool):
            raise ValueError(f'{field} must be boolean.')
    if mode != 'off' and not result['consent']:
        raise ValueError('Confirm permission to modify the selected speakers’ visible faces before lip sync.')
    faces = value.get('faces', [])
    regions = value.get('regions', [])
    windows = value.get('speech_windows', [])
    if not isinstance(faces, list) or len(faces) > 20:
        raise ValueError('Use at most twenty selected faces.')
    if not isinstance(regions, list) or len(regions) > 1000 or not isinstance(windows, list) or len(windows) > 10000:
        raise ValueError('Lip-sync scene and speech selections are too large.')
    ids = set()
    for face in faces:
        if not isinstance(face, dict) or not isinstance(face.get('id'), str) or not face['id'] or len(face['id']) > 100 or face['id'] in ids:
            raise ValueError('Each selected face needs a distinct local identity.')
        if mode != 'off' and face.get('consent') is not True:
            raise ValueError('Confirm permission for each selected face.')
        normalized = face.get('normalized', False)
        if not isinstance(normalized, bool):
            raise ValueError('The normalized face-box flag must be boolean.')
        result['faces'].append({'id': face['id'], 'box': _box(face.get('box'), normalized=normalized),
                                'normalized': normalized, 'consent': face.get('consent') is True})
        ids.add(face['id'])
    if mode != 'off' and not ids:
        raise ValueError('Select and approve at least one visible face before enabling lip sync.')
    if result['face_id'] and result['face_id'] not in ids:
        raise ValueError('The chosen lip-sync face is not in this project’s approved selection.')
    if not result['face_id'] and len(ids) == 1:
        result['face_id'] = next(iter(ids))
    if mode in {'automatic', 'full'} and len(ids) > 1 and not result['face_id']:
        raise ValueError('Choose an approved face for automatic/full rendering; scene selections can switch between faces.')
    for item in regions:
        if not isinstance(item, dict):
            raise ValueError('A lip-sync scene must be an object.')
        start = _number(item.get('start'), 0, 86400, 'Scene start')
        end = _number(item.get('end'), 0, 86400, 'Scene end')
        if end <= start or duration is not None and end > duration + .05:
            raise ValueError('Lip-sync scenes must have increasing times inside the source video.')
        target = str(item.get('face_id') or result['face_id'])
        if target and target not in ids:
            raise ValueError('A scene references an unknown selected face.')
        row = {'start': start, 'end': end, 'face_id': target}
        if item.get('id') is not None:
            if not isinstance(item['id'], str) or len(item['id']) > 100:
                raise ValueError('A scene identity must be a short local identifier.')
            row['id'] = item['id']
        if mode == 'selected' and not target:
            raise ValueError('Each selected scene must identify its approved face.')
        if item.get('box'):
            normalized = item.get('normalized', False)
            if not isinstance(normalized, bool):
                raise ValueError('The normalized scene-box flag must be boolean.')
            row.update(box=_box(item['box'], normalized=normalized), normalized=normalized)
        result['regions'].append(row)
    result['regions'].sort(key=lambda r: (r['start'], r['end']))
    if mode == 'selected' and not result['regions']:
        raise ValueError('Select at least one scene before rendering Selected Scenes lip sync.')
    for window in windows:
        if not isinstance(window, dict):
            raise ValueError('Speech timing must use start/end objects.')
        start = _number(window.get('start'), 0, 86400, 'Speech start')
        end = _number(window.get('end'), 0, 86400, 'Speech end')
        if end <= start:
            raise ValueError('Speech windows must have increasing times.')
        result['speech_windows'].append({'start': start, 'end': end})
    if duration is not None and result['preview_start'] >= duration:
        raise ValueError('Preview start lies beyond the source video.')
    return result


def active_region(at, options):
    """Deterministic scene selection; it never infers a speaker identity."""
    if options['mode'] == 'off':
        return None
    if options.get('speech_windows') and not any(w['start'] <= at < w['end'] for w in options['speech_windows']):
        return None
    if options['mode'] == 'selected':
        return next((r for r in options['regions'] if r['start'] <= at < r['end']), None)
    return next((r for r in options['regions'] if r['start'] <= at < r['end']), {})


def process(source_video, russian_audio, output, options, settings, data, *, progress=None, cancel=None):
    from .worker import duration, probe
    started = time.monotonic()
    source = absolute_path(str(source_video))
    info = probe(source, settings)
    video = [s for s in info.get('streams', []) if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
    if not video:
        raise ValueError('Lip sync requires a video stream.')
    seconds = duration(info)
    if not math.isfinite(seconds) or seconds <= 0 or seconds > 86400:
        raise ValueError('Lip sync requires a known video duration of at most 24 hours.')
    selected = validate_options(options, duration=seconds)
    selected['gpu_reserve_mb'] = max(selected['gpu_reserve_mb'],
                                    _integer(settings.get('gpu_memory_reserve_mb', 1024), 256, 16384, 'GPU reserve'))
    if selected['mode'] == 'off':
        return {'path': str(source), 'duration': seconds, 'changed': False, 'model_revision': '',
                'inference_tested': False,
                'regions': [], 'warnings': [], 'metrics': {'elapsed_seconds': time.monotonic() - started,
                                                         'gpu_used': False, 'frames_changed': 0}}
    if cancel and cancel():
        raise InterruptedError('Lip sync cancelled; original video and Russian narration were preserved.')
    audio = absolute_path(str(russian_audio))
    target = Path(output).expanduser().resolve()
    if target == source or target == audio or target.exists():
        raise ValueError('Choose a new lip-sync output; originals and existing exports are never replaced.')
    if target.suffix.lower() not in {'.mp4', '.mkv'}:
        raise ValueError('Lip-sync previews and renders use MP4 or MKV.')
    audio_info = probe(audio, settings)
    if not any(s.get('codec_type') == 'audio' for s in audio_info.get('streams', [])):
        raise ValueError('The Russian narration has no playable audio stream.')
    if duration(audio_info) > seconds + .25:
        raise ValueError('Russian narration exceeds the video duration. Review alignment before lip sync.')
    try:
        from .dubbing_models import invoke, resolve
        spec = resolve('musetalk-1.5', Path(data), revision=selected['model_revision'])
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        raise LipSyncUnavailable('MuseTalk is not ready. Use Set Up Dubbing Studio → Install Optional or Repair. '
                                 'The original video and completed Russian audio are preserved. ' + str(exc)) from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='.forge-preview-', dir=target.parent) as temporary:
            staged = Path(temporary) / target.name
            request = {'task': 'lipsync', 'source': str(source), 'audio': str(audio), 'output': str(staged),
                       'model_revision': spec['model_revision'],
                       'options': selected, 'settings': {k: settings.get(k, '') for k in ('ffmpeg', 'ffprobe')},
                       'device': selected['device']}
            result = invoke('musetalk-1.5', Path(data), request, progress=progress)
            if cancel and cancel():
                raise InterruptedError('Lip sync cancelled; completed narration was preserved.')
            after = probe(staged, settings)
            if not staged.is_file() or not staged.stat().st_size or not any(s.get('codec_type') == 'video' for s in after.get('streams', [])):
                raise RuntimeError('Lip-sync output verification failed: no playable video.')
            if not isinstance(result, dict) or result.get('model_inference') is not True or result.get('inference_tested') is not True or result.get('frames_changed', 0) <= 0:
                raise RuntimeError('Lip sync did not produce any verified model-generated frames.')
            # A second existence check and no-replace atomic publish preserve an
            # export created by another job while neural inference was running.
            import os
            if os.name == 'nt':
                staged.rename(target)
            else:
                os.link(staged, target)
                staged.unlink()
    except InterruptedError:
        raise
    except Exception as exc:
        raise LipSyncUnavailable('Lip-sync rendering did not complete. The original video, Russian audio and verified segments remain available. '
                                 'Use audio-only dubbing or repair the optional component. ' + str(exc)) from exc
    return {'path': str(target), 'duration': duration(after), 'changed': True,
            'inference_tested': True,
            'model_revision': spec['model_revision'], 'regions': copy.deepcopy(selected['regions']),
            'warnings': list(result.get('warnings', [])),
            'metrics': {**result.get('metrics', {}), 'elapsed_seconds': time.monotonic() - started,
                        'frames_changed': result['frames_changed'], 'model_inference': True}}
