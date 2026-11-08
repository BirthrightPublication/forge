"""Local dubbing stages executed by the existing FORGE worker subprocess.

Project/database ownership stays in the parent service. This module only reads
immutable payload snapshots, preserves verified private media caches, and returns
proposed stage results for the parent's optimistic merge.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid
import zipfile

from .core import json_write, safe_name
from . import dubbing_audio as audio

STAGES = {'transcribe', 'translate', 'voice', 'align', 'separate', 'mix', 'effects', 'lipsync', 'encode', 'export', 'metadata', 'prepare_upload', 'voice_reference'}


def _progress(value, message, **metrics):
    from .worker import progress
    progress(value, message, **metrics)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _fingerprint(segment):
    from .dubbing_store import segment_fingerprint
    return segment_fingerprint(segment)


def _stage_fingerprint(project, stage):
    from .dubbing_store import stage_fingerprint
    return stage_fingerprint(project, stage)


def _file(path, kind, settings=None, *, media=False, **extra):
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError('A dubbing stage produced an empty or missing output.')
    item = {'kind': kind, 'path': str(path), 'name': path.name, 'size': path.stat().st_size,
            'sha256': audio.file_hash(path), **extra}
    if media:
        measured = audio.media_info(path, settings or {})
        item['duration'] = measured['duration']
        if measured['video_streams']:
            stream = measured['video_streams'][0]
            item.update(width=stream.get('width'), height=stream.get('height'), frame_rate=stream.get('avg_frame_rate'))
        elif measured['audio_streams']:
            stream = measured['audio_streams'][0]
            item.update(sample_rate=int(stream.get('sample_rate') or 0), channels=stream.get('channels'))
    return item


def _own_output(path, stage_dir, kind):
    """Never hand an original/reference to the worker's move/publish loop."""
    path = Path(path).resolve()
    if path.is_relative_to(stage_dir.resolve()) and not path.is_symlink():
        return path
    target = stage_dir / (safe_name(kind) + '-copy-' + uuid.uuid4().hex[:8] + path.suffix.lower())
    shutil.copyfile(path, target)
    if audio.file_hash(path) != audio.file_hash(target):
        raise RuntimeError('An external stage output could not be copied and verified safely.')
    return target


def _source(payload):
    value = payload.get('source') or (payload.get('dubbing', {}).get('project') or {}).get('source') or {}
    path = Path(value.get('path', '')).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError('The original source video or audio is no longer available. Relink it before processing.')
    path = path.resolve()
    stat = path.stat()
    expected = payload.get('source_stat') or value.get('source_stat') or value
    if expected.get('size') is not None and stat.st_size != expected['size']:
        raise ValueError('The original source changed after the job was queued.')
    if expected.get('mtime_ns') is not None and stat.st_mtime_ns != expected['mtime_ns']:
        raise ValueError('The original source changed after the job was queued.')
    if value.get('sha256') and audio.file_hash(path) != value['sha256']:
        raise ValueError('The original source no longer matches its saved hash.')
    return path


def _artifacts(context):
    value = context.get('artifacts') or []
    if isinstance(value, dict):
        result = []
        for kind, entry in value.items():
            for item in entry if isinstance(entry, list) else [entry]:
                if isinstance(item, dict):
                    result.append({'kind': kind, **item})
        return result
    return value if isinstance(value, list) else []


def _find_artifact(context, kinds, *, optional=False):
    if isinstance(kinds, str):
        kinds = [kinds]
    matches = [item for kind in kinds for item in reversed(_artifacts(context)) if item.get('kind') == kind]
    for item in matches:
        path = Path(item.get('path', ''))
        if not path.is_absolute() or not path.is_file():
            continue
        if item.get('size') is not None and path.stat().st_size != item['size']:
            raise ValueError('A prior dubbing output changed. Retry or relink the affected stage.')
        if item.get('sha256') and audio.file_hash(path) != item['sha256']:
            raise ValueError('A prior dubbing output failed its saved integrity check.')
        return path.resolve()
    if optional:
        return None
    raise ValueError('Complete the required earlier stage first: ' + ', '.join(kinds) + '.')


def _selected_artifact(context, options, field, kinds, *, optional=True):
    identity = options.get(field)
    if identity:
        items = [item for item in _artifacts(context) if item.get('id', item.get('artifact_id')) == identity and item.get('kind') in kinds]
        if not items:
            raise ValueError('The selected saved audio stem is no longer available in this project.')
        return _find_artifact({'artifacts': items}, kinds)
    return _find_artifact(context, kinds, optional=optional)


def _segments(project, context, options):
    items = project.get('segments') or []
    if not isinstance(items, list) or len(items) > 10000:
        raise ValueError('A dubbing project accepts at most 10,000 speech segments.')
    selected = context.get('segment_ids', options.get('segment_ids'))
    if selected is not None:
        if not isinstance(selected, list) or not selected or len(selected) > 10000 or any(not isinstance(value, str) for value in selected):
            raise ValueError('Choose at least one saved speech segment for this operation.')
        wanted = set(selected)
        if wanted - {item.get('id') for item in items}:
            raise ValueError('A selected speech segment no longer belongs to this project.')
        items = [item for item in items if item.get('id') in wanted]
    return copy.deepcopy(items)


def _update(original, **fields):
    return {'id': original['id'], 'expected_input_hash': _fingerprint(original), **fields}


def _finish(stage_dir, files, warnings, result):
    # The parent receives absolute metadata paths and rewrites newly published
    # paths. The human-readable report contains names/hashes, not stale work paths.
    report = copy.deepcopy(result)
    report['artifacts'] = [{key: value for key, value in item.items() if key != 'path'} for item in result.get('artifacts', [])]
    report['warnings'] = list(dict.fromkeys(str(value) for value in warnings))
    report['path_note'] = 'Use saved project artifact records for current file locations; names/hashes here identify this stage.'
    path = stage_dir / 'dubbing-stage.json'
    json_write(path, report)
    return [*files, path], report['warnings'], result


def _translate(project, context, options, stage_dir, data, result):
    from . import dubbing_models
    selected = _segments(project, context, options)
    if not selected:
        raise ValueError('Transcribe the source or add timed English segments before translating.')
    mode = project.get('mode', 'localized')
    if mode not in {'faithful', 'localized', 'alternative'}:
        raise ValueError('Choose a supported dubbing translation mode.')
    component = project.get('options', {}).get('translation', {}).get('engine', 'opus-en-ru')
    glossary = context.get('glossary') or {}
    explicit_selection = context.get('segment_ids', options.get('segment_ids')) is not None
    replace = context.get('replace_manual_text') is True
    if not explicit_selection and not replace:
        selected = [item for item in selected if not ((item.get('manual_edit') or item.get('reviewed')) and str(item.get('text') or '').strip())]
    if not selected:
        result['project_updates'] = {'translation_status': 'existing_reviewed_text_preserved'}
        return _finish(stage_dir, [], ['Existing edited/reviewed Russian text was preserved. Select particular segments to request new translation suggestions.'], result)
    request = {'task': 'translate', 'segments': selected, 'glossary': glossary, 'mode': mode,
               'overwrite_edited': True,
               'source_language': 'en', 'target_language': 'ru'}
    pinned = project.get('options', {}).get('translation', {}).get('model_revision') or (context.get('model_revisions') or {}).get(component)
    if pinned:
        request['model_revision'] = pinned
    translated = dubbing_models.invoke(component, data, request, progress=_progress)
    if pinned and translated.get('model_revision') != pinned:
        raise RuntimeError('Translation did not use the approved immutable model revision.')
    by_id = {item.get('id'): item for item in translated.get('segments', [])}
    if set(by_id) != {item['id'] for item in selected}:
        raise RuntimeError('Translation returned missing or unrelated segment identities.')
    warnings, updates, display = list(translated.get('warnings') or []), [], []
    for original in selected:
        incoming = by_id[original['id']]
        text = incoming.get('text', incoming.get('translation', ''))
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise RuntimeError('The translation engine returned an empty or invalid Russian segment.')
        # Engine results cannot change the original speaker or media boundaries.
        for field in ('start', 'end', 'speaker'):
            if field in incoming and incoming[field] != original.get(field):
                raise RuntimeError('Translation changed an original timestamp or speaker identity.')
        flags = []
        numbers = re.findall(r'\d+(?:[.,:/-]\d+)*', original.get('source_text', ''))
        if any(number not in text for number in numbers):
            flags.append('Review dates, numbers and citations against the English source.')
        protected = bool(original.get('manual_edit') or original.get('reviewed')) and bool(original.get('text', '').strip())
        common = {'translation_model': component, 'translation_model_revision': translated.get('model_revision', ''),
                  'translation_flags': flags}
        if protected and not replace:
            updates.append(_update(original, translation_suggestion=text, **common))
            display.append({**original, 'translation_suggestion': text})
            warnings.append(str(original['id']) + ': Existing edited/reviewed Russian text was preserved; a new suggestion is available for review.')
        else:
            updates.append(_update(original, text=text, reviewed=False, manual_edit=False, **common))
            display.append({**original, 'text': text, 'reviewed': False, 'manual_edit': False})
        warnings.extend(str(original['id']) + ': ' + message for message in flags)
    if mode == 'localized':
        warnings.append('Localized mode uses the installed English–Russian translation model and editable review. It does not invent or fact-check new commentary.')
    elif mode == 'alternative':
        warnings.append('Alternative commentary remains an editable user-written track. Review and approve substantive changes before synthesis.')
    path = stage_dir / 'russian-translation.json'
    json_write(path, {'source_language': 'en', 'target_language': 'ru', 'mode': mode,
                      'model': component, 'model_revision': translated.get('model_revision'), 'segments': display})
    result.update(segment_updates=updates, artifacts=[_file(path, 'translation')],
                  project_updates={'translation_model': component, 'translation_model_revision': translated.get('model_revision', '')})
    return _finish(stage_dir, [path], warnings, result)


def _transcribe(project, context, options, stage_dir, data, result, payload):
    from .models import Transcribe
    from .worker import transcribe
    source = _source(payload)
    selected = project.get('options', {}).get('asr') or {}
    if selected.get('engine', 'whisper') != 'whisper':
        raise ValueError('Dubbing requires timed Whisper recognition; Bark recognition does not provide validated segment timing.')
    configured = {key: value for key, value in selected.items() if key in Transcribe.model_fields}
    configured.update(engine='whisper', language='en', task='transcribe', timestamps='word', formats=['json'], style='raw')
    if not configured.get('model_revision'):
        configured['model_revision'] = (context.get('model_revisions') or {}).get('whisper-' + configured.get('model', 'large-v3-turbo'), '')
    configured = Transcribe.model_validate(configured).model_dump()
    from . import dubbing_models
    existing = dubbing_models.existing_whisper_state(data, configured['model'])
    core_ready = existing.get('ready') is True and (not configured.get('model_revision') or configured['model_revision'] == existing.get('model_revision'))
    isolated = None
    if not core_ready:
        try:
            isolated = dubbing_models.resolve('whisper-' + configured['model'], data, revision=configured.get('model_revision', ''))
        except (ValueError, RuntimeError, OSError) as error:
            raise RuntimeError('Speech recognition needs a compatible installed model. Open Set Up Dubbing Studio and choose Install Recommended or Repair; existing Whisper environments are preserved.') from error
    if isolated:
        recognized = dubbing_models.invoke('whisper-' + configured['model'], data,
            {'task': 'transcribe', 'source': str(source), **configured}, progress=_progress)
        entries = []
        for item in recognized.get('segments', []):
            entries.append({**item, 'text': item.get('source_text') or item.get('text') or ''})
        path = stage_dir / 'english-transcript.json'
        json_write(path, {'engine': 'faster-whisper', 'model': configured['model'],
                         'info': {key: recognized[key] for key in ('duration', 'model_revision', 'actual_device') if key in recognized},
                         'segments': entries})
        files, warnings = [path], list(recognized.get('warnings') or [])
    else:
        files, warnings = transcribe(source, stage_dir, 'english-transcript', configured, data)
    json_files = [path for path in files if Path(path).suffix.lower() == '.json']
    if len(json_files) != 1 or Path(json_files[0]).stat().st_size > 30_000_000:
        raise RuntimeError('Whisper did not return its expected bounded timed transcript.')
    recognized = json.loads(Path(json_files[0]).read_text('utf-8'))
    existing = project.get('segments') or []
    new_segments, updates = [], []
    for index, entry in enumerate(recognized.get('segments', [])):
        start = audio.finite(entry.get('start'), 0, 86400, name='Recognized speech start')
        end = audio.finite(entry.get('end'), 0, 86400, name='Recognized speech end')
        if end <= start or not str(entry.get('text') or '').strip():
            continue
        prior = next((item for item in existing if abs(item.get('start', -100)-start) <= .03 and abs(item.get('end', -100)-end) <= .03), None)
        identifier = prior['id'] if prior else 'segment-' + _hash([str(source), index, start, end])[:20]
        english = str(entry['text']).strip()
        base = {'id': identifier, 'start': start, 'end': end, 'speaker': entry.get('speaker') or (prior or {}).get('speaker') or 'speaker1',
                'source_text': english, 'text': '', 'reviewed': False, 'manual_edit': False,
                'words': copy.deepcopy(entry.get('words') or []), 'recognition_flags': copy.deepcopy(entry.get('review_flags') or [])}
        if prior:
            base = {**copy.deepcopy(prior), **base, 'text': prior.get('text', ''), 'manual_edit': prior.get('manual_edit', False),
                    'reviewed': bool(prior.get('reviewed')) and prior.get('source_text', '').strip() == english}
            updates.append(_update(prior, **{key: value for key, value in base.items() if key != 'id'}))
        else:
            updates.append({**base, 'expected_input_hash': None, 'new_segment': True})
        new_segments.append(base)
    if not new_segments:
        raise ValueError('No nonempty timed speech was detected. Choose another audio track or add a reviewed manual transcript.')
    info = recognized.get('info') or {}
    if configured.get('model_revision') and info.get('model_revision') != configured['model_revision']:
        raise RuntimeError('Speech recognition did not use the approved immutable model revision.')
    result.update(segment_updates=updates, artifacts=[_file(path, 'english_transcript') for path in files],
                  project_updates={'transcription': {'engine': 'whisper', 'model': configured['model'],
                    'model_revision': info.get('model_revision', ''), 'language': info.get('language', 'en'),
                    'duration': info.get('duration'), 'method': 'Existing FORGE faster-whisper timed recognition'},
                    'raw_transcript': {'path': str(json_files[0]), 'model': configured['model'], 'model_revision': info.get('model_revision', '')},
                    'source_audio': {'path': str(source), 'stream_index': 0, 'source_stat': payload.get('source_stat')},
                    'original_audio_reference': {'path': str(source), 'stream_index': 0, 'source_stat': payload.get('source_stat')}})
    if existing:
        warnings.append('Existing Russian edits were retained for matching timed segments. Unmatched existing segments are not silently deleted.')
    return _finish(stage_dir, files, warnings, result)


def _voice_profile(project, context, segment, cache=None):
    config = project.get('options', {}).get('tts') or {}
    speaker = (project.get('speakers') or {}).get(segment.get('speaker', 'speaker1')) or {}
    profile_id = segment.get('voice_profile_id') or speaker.get('voice_profile_id') or config.get('voice_profile_id')
    if cache is not None and profile_id in cache:
        return profile_id, cache[profile_id]
    profiles = context.get('voice_profiles') or {}
    if isinstance(profiles, list):
        profiles = {item.get('id'): item for item in profiles}
    profile = copy.deepcopy(profiles.get(profile_id) or {})
    if not profile:
        raise ValueError('Choose and save a permitted voice profile for ' + str(segment.get('speaker', 'speaker1')) + ' before generating Russian speech.')
    if profile.get('consent') is not True:
        raise ValueError('Explicit permission is required for the selected identifiable voice reference.')
    preview = context.get('preview_approved') is True
    if profile.get('approved') is not True and not preview:
        raise ValueError('Listen to and approve the prepared voice reference before using it in a production render.')
    prepared = profile.get('prepared_reference') or {}
    if not isinstance(prepared, dict):
        raise ValueError('Prepare and approve the saved local voice reference before synthesis.')
    reference = prepared.get('path') or profile.get('reference_audio') or profile.get('ref_audio')
    path = Path(reference or '').expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError('The selected permitted voice reference is unavailable. Repair or relink the profile.')
    reference_hash = audio.file_hash(path)
    expected = prepared.get('sha256') or profile.get('reference_sha256')
    if expected and expected != reference_hash:
        raise ValueError('The voice reference changed since it was approved. Listen and approve it again.')
    profile.update(reference_audio=str(path.resolve()), reference_sha256=reference_hash)
    if len(str(profile.get('reference_text', profile.get('ref_text', '')))) > 5000:
        raise ValueError('Reference text describes the short prepared clip, not a full manuscript. Use at most 5,000 characters.')
    if cache is not None:
        cache[profile_id] = profile
    return profile_id, profile


def _cache_read(root, key, settings):
    try:
        receipt = json.loads((root / 'receipt.json').read_text('utf-8'))
        path = root / 'audio.wav'
        if receipt.get('key') != key or not path.is_file() or path.is_symlink():
            return None
        if path.stat().st_size != receipt.get('size') or audio.file_hash(path) != receipt.get('sha256'):
            return None
        measured = audio.audio_info(path, settings)
        if abs(measured['duration'] - receipt['duration']) > .025:
            return None
        return {**receipt, 'path': str(path.resolve()), 'cached': True}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def _cache_store(source, cache, key, fingerprint, settings):
    from .model_cache import cache_lock
    with cache_lock(cache.parent / (key + '.lock'), _progress, timeout=120):
        existing = _cache_read(cache, key, settings)
        if existing:
            return existing
        # Preserve a corrupt prior cache for diagnosis, never treat it as a hit.
        if cache.exists():
            cache.rename(cache.with_name(key + '.invalid-' + uuid.uuid4().hex))
        temporary = cache.with_name('.' + key + '-' + uuid.uuid4().hex)
        temporary.mkdir(parents=True)
        try:
            target = temporary / 'audio.wav'
            from .audio_effects import levels
            peak = levels(source, settings).get('peak_dbfs')
            if peak is None or peak <= -80:
                raise RuntimeError('The voice model produced silent or inaudible audio. Test or repair the voice component before retrying.')
            normalization = -(peak + .5) if peak is not None and peak > -.5 else 0
            prepared = source
            if normalization:
                prepared = temporary / 'safe-peak.wav'
                audio.ffmpeg(['-i', source, '-map', '0:a:0', '-af', f'volume={normalization:.9f}dB',
                              '-c:a', 'pcm_f32le', prepared], settings)
            audio.extract_audio(prepared, target, settings, rate=48000, channels=2)
            measured = audio.audio_info(target, settings)
            record = {'schema': 1, 'key': key, 'sha256': audio.file_hash(target), 'size': target.stat().st_size,
                      **measured, 'fingerprint': fingerprint, 'peak_normalization_db': normalization}
            if prepared != source:
                Path(prepared).unlink()
            json_write(temporary / 'receipt.json', record)
            temporary.rename(cache)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return {**record, 'path': str((cache / 'audio.wav').resolve()), 'cached': False}


def _voice(project, context, options, stage_dir, data, result):
    from . import dubbing_models
    selected = _segments(project, context, options)
    preview = context.get('preview_approved') is True
    if not selected:
        raise ValueError('Review a nonempty Russian script before generating narration.')
    if not preview and (context.get('review_approved') is not True or any(item.get('reviewed') is not True for item in selected)):
        raise ValueError('Review and explicitly approve the Russian translation before generating production speech.')
    config = project.get('options', {}).get('tts') or {}
    settings = result.pop('_settings')
    cache_root = data / 'dubbing' / 'cache' / 'voices'
    cache_root.mkdir(parents=True, exist_ok=True)
    prepared, missing, groups, warnings = [], [], {}, []
    profile_cache, model_cache = {}, {}
    for original in selected:
        text = original.get('text', '')
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('Each Russian voice segment needs nonempty text of at most 4,000 characters. Split longer commentary into timed segments.')
        profile_id, profile = _voice_profile(project, context, original, profile_cache)
        component = profile.get('model') or profile.get('engine') or config.get('engine', 'qwen3-tts-0.6b-base')
        pinned = profile.get('model_revision') or config.get('model_revision') or (context.get('model_revisions') or {}).get(component, '')
        if (component, pinned) not in model_cache:
            model_cache[(component, pinned)] = dubbing_models.resolve(component, data, revision=pinned)
        resolved = model_cache[(component, pinned)]
        revision = resolved.get('model_revision') or resolved.get('revision')
        if not isinstance(revision, str) or not re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})', revision):
            raise ValueError('The selected voice model lacks a verified immutable revision. Repair it before synthesis.')
        effects = copy.deepcopy(config.get('effects') or []) + copy.deepcopy(profile.get('effects') or profile.get('audio_effects') or [])
        expression = original.get('emotion') or config.get('expression')
        expression_profile = (context.get('expression_profiles') or {}).get(expression) or {}
        if expression_profile.get('chain'):
            effects += copy.deepcopy(expression_profile['chain'])
        speed = config.get('speed', 1) * original.get('speed', profile.get('speaking_rate', profile.get('speed', 1)))
        speed = audio.finite(speed, .5, 2, name='Speaking rate')
        pitch = audio.finite(config.get('pitch', 0) + profile.get('pitch', 0), -12, 12, name='Voice depth/pitch')
        from .audio_effects import validate_chain
        validate_chain([*effects, *([{'type': 'pitch', 'params': {'tempo': speed, 'semitones': pitch}}] if speed != 1 or pitch else [])])
        fingerprint = {'schema': 1, 'text': text, 'language': 'ru', 'component': component, 'model_revision': revision,
            'runtime_revision': resolved.get('runtime_revision', ''), 'profile_id': profile_id,
            'reference_sha256': profile['reference_sha256'], 'reference_text': profile.get('reference_text', profile.get('ref_text', '')),
            'x_vector_only_mode': bool(profile.get('x_vector_only_mode') or config.get('x_vector_only_mode') or not str(profile.get('reference_text', profile.get('ref_text', ''))).strip()), 'effects': effects, 'speed': speed, 'pitch': pitch,
            'generation': copy.deepcopy(config.get('generation') or {})}
        key = _hash(fingerprint)
        cache = cache_root / key
        entry = {'original': original, 'profile': profile, 'component': component, 'revision': revision,
                 'key': key, 'cache': cache, 'fingerprint': fingerprint, 'effects': effects, 'speed': speed, 'pitch': pitch}
        hit = _cache_read(cache, key, settings)
        if hit:
            entry['audio'] = hit
        else:
            missing.append(entry)
            groups.setdefault((component, revision), []).append(entry)
        prepared.append(entry)
        if not str(fingerprint['reference_text']).strip():
            warnings.append(str(original['id']) + ': No reference transcript was available. Embedding-only voice cloning is selected; listen to the Russian result before production approval.')
        if expression and not expression_profile.get('chain'):
            warnings.append(str(original['id']) + ': The emotion label is retained for review; this Base voice model has no validated native emotion control. Select an Audio Effects expression preset to apply a measured effect.')
    completed = len(prepared) - len(missing)
    _progress(100 * completed / len(prepared), f'{completed} / {len(prepared)} voice segments verified',
              progress_measured=True, stage='Russian voice')
    for (component, revision), entries in groups.items():
        # Each batch is sequential inside one isolated model process.
        for begin in range(0, len(entries), 16):
            batch = entries[begin:begin+16]
            with tempfile.TemporaryDirectory(prefix='.voice-batch-', dir=cache_root) as temporary:
                temporary = Path(temporary)
                requests = []
                for index, entry in enumerate(batch):
                    profile = entry['profile']
                    path = temporary / f'segment-{index}.wav'
                    entry['temporary_audio'] = path
                    requests.append({'id': entry['original']['id'], 'text': entry['original']['text'], 'output': str(path),
                        'ref_audio': profile['reference_audio'], 'ref_text': profile.get('reference_text', profile.get('ref_text', '')),
                        'ref_audio_sha256': profile['reference_sha256'],
                        'consent': True, 'language': 'Russian', 'x_vector_only_mode': entry['fingerprint']['x_vector_only_mode']})
                failure = None
                def save_available():
                    nonlocal completed
                    for entry in batch:
                        if 'audio' in entry:
                            continue
                        path = entry['temporary_audio']
                        if not path.is_file():
                            continue
                        # Registered runtimes publish each final audio path by
                        # atomic rename, then emit progress. A killed later
                        # inference cannot erase this already verified segment.
                        audio.audio_info(path, settings)
                        processed = path
                        chain = copy.deepcopy(entry['effects'])
                        if entry['speed'] != 1 or entry['pitch'] != 0:
                            chain.append({'type': 'pitch', 'params': {'tempo': entry['speed'], 'semitones': entry['pitch']}})
                        if chain:
                            from .audio_effects import render_chain
                            processed = path.with_name(path.stem + '-effects.wav')
                            render_chain(path, processed, chain, settings)
                        entry['audio'] = _cache_store(processed, entry['cache'], entry['key'], entry['fingerprint'], settings)
                        completed += 1
                def segment_progress(value, message, **metrics):
                    save_available()
                    _progress(100 * completed / len(prepared), str(message),
                              progress_measured=True, stage='Russian voice')
                try:
                    generated = dubbing_models.invoke(component, data, {'task': 'synthesize', 'segments': requests,
                        'model_revision': revision, 'device': config.get('device', 'auto'),
                        'generation': config.get('generation') or {}}, progress=segment_progress)
                    if generated.get('model_revision') and generated['model_revision'] != revision:
                        raise RuntimeError('The voice model changed during synthesis; its output was not silently accepted.')
                except BaseException as error:
                    failure = error
                # A later failure must not discard earlier playable batch outputs.
                try:
                    save_available()
                    _progress(100 * completed / len(prepared), f'{completed} / {len(prepared)} Russian voice segments verified',
                              progress_measured=True, stage='Russian voice')
                except Exception:
                    if failure is None:
                        raise
                if failure is not None:
                    raise failure
                if any('audio' not in entry for entry in batch):
                    raise RuntimeError('The voice runtime did not return all requested playable segments; verified earlier segments remain cached.')
    updates, artifacts, outputs = [], [], []
    for entry in prepared:
        original, value = entry['original'], entry['audio']
        value = {key: item for key, item in value.items() if key not in {'fingerprint', 'schema', 'key'}}
        value.update(model_revision=entry['revision'], engine=entry['component'], cache_key=entry['key'])
        updates.append(_update(original, audio=value, voice_profile_id=entry['fingerprint']['profile_id']))
        artifacts.append(_file(value['path'], 'voice_segment', settings, media=True, segment_id=original['id'],
                               model_revision=entry['revision'], cache_key=entry['key'], preview=preview))
        outputs.append({'id': original['id'], 'path': value['path'], 'duration': value['duration'],
                        'cached': value.get('cached', False), 'model_revision': entry['revision']})
        if value.get('peak_normalization_db'):
            warnings.append(str(original['id']) + ': A measured peak reduction was applied before PCM conversion to avoid clipping generated speech.')
    path = stage_dir / 'russian-voice-segments.json'
    json_write(path, {'segments': outputs, 'cache': 'Verified immutable private segment cache; originals are preserved.'})
    result.update(segment_updates=updates, artifacts=artifacts, project_updates={'voice_generation': {'segments': len(updates),
        'cached_segments': sum(entry['audio'].get('cached', False) for entry in prepared), 'target_language': 'ru'}},
        voice_profile_revisions={entry['fingerprint']['profile_id']: entry['profile'].get('revision') for entry in prepared})
    return _finish(stage_dir, [path], warnings, result)


def _audio_segments(project, context, options, data, settings):
    selected = _segments(project, context, options)
    artifacts = _artifacts(context)
    for item in selected:
        value = copy.deepcopy(item.get('audio') or {})
        if value.get('stale'):
            raise ValueError('The Russian text or voice settings changed after this speech was generated. Regenerate that segment before alignment.')
        if not value.get('path'):
            # A segment identity alone cannot prove the cached utterance still
            # speaks its current text. Recover only its saved immutable clip,
            # never an earlier artifact from another synthesis configuration.
            prior = next((entry for entry in reversed(artifacts)
                if entry.get('kind') == 'voice_segment' and entry.get('segment_id') == item['id']
                and not entry.get('stale') and value.get('cache_key') and value.get('sha256')
                and entry.get('cache_key') == value['cache_key'] and entry.get('sha256') == value['sha256']), None)
            if prior:
                value = {**value, **copy.deepcopy(prior)}
        path = Path(value.get('path', ''))
        if not path.is_absolute() or not path.is_file():
            raise ValueError('Generate Russian speech for every selected segment before aligning it.')
        if value.get('sha256') and audio.file_hash(path) != value['sha256']:
            raise ValueError('A generated Russian segment changed. Regenerate only that segment before alignment.')
        measured = audio.audio_info(path, settings)
        item['audio'] = {**value, **measured, 'path': str(path.resolve())}
    return selected


def _align(project, context, options, stage_dir, data, result, settings, payload):
    # Regenerating one utterance must not remove the rest of the narration.
    # Reassemble the full timeline from verified cached speech; only synthesis
    # and translation honor the selected subset.
    whole_context = {key: value for key, value in context.items() if key != 'segment_ids'}
    whole_options = {key: value for key, value in options.items() if key != 'segment_ids'}
    selected = _audio_segments(project, whole_context, whole_options, data, settings)
    config = project.get('options', {}).get('timing') or {}
    original = audio.media_info(_source(payload), settings)
    plan = audio.plan_alignment(selected, config.get('strategy', 'balanced'),
                                max_tempo=config.get('max_tempo', 1.15), min_tempo=config.get('max_slowdown', 1),
                                gap_seconds=config.get('gap_seconds', .08), source_duration=original['duration'])
    output = stage_dir / 'russian-narration.wav'
    rendered = audio.render_alignment(plan, output, settings, allow_overlap=config.get('allow_overlap', False))
    path = stage_dir / 'russian-alignment.json'
    json_write(path, plan)
    updates = [_update(original, timing=entry['timing']) for original, entry in zip(sorted(selected, key=lambda item: (item.get('start', 0), str(item.get('id', '')))), plan['segments'])]
    result.update(segment_updates=updates, artifacts=[_file(output, 'narration', settings, media=True), _file(path, 'alignment')],
                  project_updates={'alignment': {'strategy': plan['strategy'], 'duration': rendered['duration'], 'needs_review': plan['needs_review']}})
    return _finish(stage_dir, [output, path], plan['warnings'], result)


def _original_audio(payload, context, stage_dir, settings):
    found = _find_artifact(context, ['original_audio', 'source_audio'], optional=True)
    if found:
        return found
    source = _source(payload)
    info = audio.media_info(source, settings)
    if not info['audio_streams']:
        return None
    path = stage_dir / 'original-audio.wav'
    audio.extract_audio(source, path, settings)
    return path


def _mix(project, context, options, stage_dir, data, result, settings, payload):
    narration = _find_artifact(context, ['narration', 'aligned_audio'])
    original = _original_audio(payload, context, stage_dir, settings)
    config = copy.deepcopy(project.get('options', {}).get('audio') or {})
    background = _selected_artifact(context, config, 'background_artifact_id', ['background', 'background_audio'])
    vocals = _selected_artifact(context, config, 'vocals_artifact_id', ['vocals', 'original_voice'])
    music = _selected_artifact(context, config, 'music_artifact_id', ['music'])
    output = stage_dir / 'russian-mix.wav'
    mixed = audio.mix_audio(narration, output, settings, config, original_audio=original,
                            background=background, vocals=vocals, music=music)
    files = [output]
    artifacts = [_file(output, 'mix', settings, media=True)]
    if original and original.parent == stage_dir:
        files.append(original); artifacts.append(_file(original, 'original_audio', settings, media=True))
    result.update(artifacts=artifacts, project_updates={'mix': {key: value for key, value in mixed.items() if key != 'path'}})
    return _finish(stage_dir, files, mixed['warnings'], result)


def _separate(project, context, options, stage_dir, data, result, settings, payload):
    from . import dubbing_separation
    original = _original_audio(payload, context, stage_dir, settings)
    if not original:
        raise ValueError('The source has no original audio to separate. Continue with a narration-only mix.')
    config = copy.deepcopy(project.get('options', {}).get('audio') or {})
    config['mode'] = config.get('separation', config.get('separation_mode', 'off'))
    config['device'] = config.get('separation_device', 'auto')
    config['chunk_seconds'] = config.get('separation_chunk_seconds', 10)
    config['model_revision'] = config.get('model_revision') or (context.get('model_revisions') or {}).get('hdemucs-high', '')
    for kind, field in (('background', 'background_artifact_id'), ('vocals', 'vocals_artifact_id'), ('music', 'music_artifact_id')):
        if config.get(field):
            config[kind + '_path'] = str(_selected_artifact(context, config, field, [kind]))
    separated = dubbing_separation.process(original, stage_dir, config, settings, data, progress=_progress)
    files, artifacts = [], []
    for kind in ('background', 'vocals', 'music', 'drums', 'bass', 'other'):
        value = separated.get(kind) or separated.get(kind + '_path')
        if value:
            path = _own_output(value, stage_dir, kind)
            files.append(path); artifacts.append(_file(path, kind, settings, media=True))
    if not artifacts and separated.get('separation_performed') is not False:
        raise RuntimeError('The source-separation adapter returned no verified stems. Use manual stems or original-track ducking.')
    if original.parent == stage_dir:
        files.append(original); artifacts.append(_file(original, 'original_audio', settings, media=True))
    result.update(artifacts=artifacts, project_updates={'separation': {key: value for key, value in separated.items() if key not in {'background', 'vocals', 'music', 'drums', 'bass', 'other'}}})
    return _finish(stage_dir, files, separated.get('warnings') or [], result)


def _effects(project, context, options, stage_dir, data, result, settings):
    from .audio_effects import process as apply_effects
    source = _find_artifact(context, ['mix', 'mastered_audio', 'narration', 'aligned_audio'])
    chain = copy.deepcopy(options.get('effects', options.get('chain', [])))
    files, warnings = apply_effects(source, stage_dir, {'chain': chain, 'format': 'wav',
                    'name': 'russian-mastered', 'write_report': True}, settings)
    media = next(path for path in files if path.suffix.lower() == '.wav')
    entry = _file(media, 'mix', settings, media=True, mastered=True, effects_chain_sha256=_hash(chain))
    result.update(artifacts=[entry, {**entry, 'kind': 'mastered_audio'},
                            *[_file(path, 'audio_effects_report') for path in files if path.suffix.lower() == '.json']],
                  project_updates={'mix': {'path': str(media), 'duration': entry['duration'],
                                          'effects': chain, 'effects_chain_sha256': _hash(chain)}})
    return _finish(stage_dir, files, warnings, result)


def _segment_timing(segment):
    timing = segment.get('timing') or {}
    return {} if timing.get('stale') else timing


def _lipsync(project, context, options, stage_dir, data, result, settings, payload):
    from . import dubbing_visual
    config = copy.deepcopy(project.get('options', {}).get('lip_sync') or {})
    config['model_revision'] = config.get('model_revision') or (context.get('model_revisions') or {}).get(config.get('engine', 'musetalk-1.5'), '')
    if config.get('mode', 'off') == 'off':
        result['project_updates'] = {'lip_sync': {'status': 'off', 'video_unchanged': True}}
        return _finish(stage_dir, [], [], result)
    source = _find_artifact(context, ['lipsync_base_video'], optional=True) or _source(payload)
    narration = _find_artifact(context, ['mix', 'narration', 'aligned_audio'])
    config['speech_windows'] = [{'id': segment.get('id'), 'speaker': segment.get('speaker', 'speaker1'),
                                'start': _segment_timing(segment).get('start', segment.get('start')),
                                'end': _segment_timing(segment).get('end', segment.get('end'))}
                               for segment in project.get('segments') or []]
    if options.get('preview'):
        config['preview'] = True
        config['preview_start'] = audio.finite(options.get('preview_start', 0), 0, 86400, name='Lip-sync preview start')
        config['preview_seconds'] = audio.finite(options.get('preview_seconds', 5), 5, 10, name='Lip-sync preview duration')
    path = stage_dir / ('russian-lipsync-preview.mp4' if config.get('preview') else 'russian-lipsync.mp4')
    # This optional stage never owns/deletes prior audio or translated segments.
    generated = dubbing_visual.process(source, narration, path, config, settings, data, progress=_progress)
    if generated.get('changed') is False:
        result['project_updates'] = {'lip_sync': {**generated, 'status': 'not_applied', 'video_unchanged': True}}
        return _finish(stage_dir, [], generated.get('warnings') or ['No applicable face region was modified; the original video and completed Russian audio are preserved.'], result)
    output = _own_output(generated.get('path') or path, stage_dir, 'lipsync')
    result.update(artifacts=[_file(output, 'lipsync_preview' if config.get('preview') else 'lipsync_video', settings, media=True, preview=bool(config.get('preview')))],
                  project_updates={'lip_sync': {key: value for key, value in generated.items() if key != 'path'}})
    return _finish(stage_dir, [output], generated.get('warnings') or [], result)


def _caption_text(value):
    value = str(value).replace('\r\n', '\n').replace('\r', '\n')
    value = ''.join(character for character in value if character in '\n\t' or ord(character) >= 32)
    return value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('--&gt;', '—')


def _timestamp(seconds, comma=False):
    milliseconds = int(round(audio.finite(seconds, 0, 86400) * 1000))
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, ms = divmod(remainder, 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}{"," if comma else "."}{ms:03d}'


def subtitles(segments, output, *, format='srt'):
    if format not in {'srt', 'vtt'}:
        raise ValueError('Choose SRT or VTT Russian subtitles.')
    output = audio.new_output(output)
    rows = ['WEBVTT\n'] if format == 'vtt' else []
    count = 0
    for item in sorted(segments, key=lambda segment: (_segment_timing(segment).get('start', segment.get('start', 0)), str(segment.get('id', '')))):
        text = str(item.get('text') or '').strip()
        if not text:
            continue
        timing = _segment_timing(item)
        start = audio.finite(timing.get('start', item.get('start')), 0, 86400, name='Subtitle start')
        end = audio.finite(timing.get('end', item.get('end')), 0, 86400, name='Subtitle end')
        if end <= start:
            raise ValueError('Subtitle end must follow its start.')
        count += 1
        heading = f'{count}\n' if format == 'srt' else ''
        rows.append(heading + f'{_timestamp(start, format == "srt")} --> {_timestamp(end, format == "srt")}\n{_caption_text(text)}\n')
    if not count:
        raise ValueError('No Russian text is available to export as subtitles.')
    output.write_text('\n'.join(rows) + '\n', encoding='utf-8')
    return output


def _encode(project, context, options, stage_dir, data, result, settings, payload):
    config = copy.deepcopy(project.get('options', {}).get('export') or {})
    original_source = _source(payload)
    source = _find_artifact(context, ['lipsync_video'], optional=True) or original_source
    narration = _find_artifact(context, ['mix', 'narration', 'aligned_audio'])
    container = config.get('container', 'mp4')
    if container not in {'mp4', 'mkv'}:
        raise ValueError('Select MP4 or MKV for Russian dubbed video.')
    path = stage_dir / (safe_name(project.get('name', 'Russian version')) + '-ru.' + container)
    subtitle = None
    files, artifacts = [], []
    if config.get('subtitles', 'sidecar') != 'none':
        subtitle = subtitles(project.get('segments') or [], stage_dir / 'russian.srt')
        files.append(subtitle); artifacts.append(_file(subtitle, 'russian_srt'))
    generated = audio.encode_video(source, narration, path, settings, config, subtitle=subtitle)
    files.append(path); artifacts.append(_file(path, 'video', settings, media=True))
    if config.get('provenance'):
        from . import __version__
        provenance = stage_dir / 'russian-provenance.json'
        json_write(provenance, {'schema': 1, 'application': {'name': 'FORGE', 'version': __version__},
            'scope': 'Unsigned local processing record; not a C2PA signature or authenticity certification.',
            'source': {'sha256': (payload.get('source') or {}).get('sha256') or audio.file_hash(original_source)},
            'output': {'name': path.name, 'sha256': artifacts[-1]['sha256'], 'size': artifacts[-1]['size']},
            'source_language': 'en', 'target_language': 'ru', 'ai_narration': True,
            'lip_sync_applied': source != original_source,
            'voice_models': sorted({(item.get('audio') or {}).get('engine', '') + '@' + (item.get('audio') or {}).get('model_revision', '')
                                   for item in project.get('segments') or [] if (item.get('audio') or {}).get('engine')})})
        files.append(provenance); artifacts.append(_file(provenance, 'provenance'))
    result.update(artifacts=artifacts, project_updates={'encoded_video': generated})
    return _finish(stage_dir, files, [], result)


def _safe_project_backup(project, context, output, settings, *, include_source=True, source=None):
    output = audio.new_output(output, source)
    snapshot = copy.deepcopy(project)
    # Publishing credentials never belong in an export. Actual credentials live
    # in the publishing adapter, not this project snapshot.
    def sanitize(value):
        if isinstance(value, dict):
            return {key: sanitize(item) for key, item in value.items() if not any(secret in key.lower() for secret in ('token', 'client_secret', 'password', 'credential'))}
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value
    snapshot = sanitize(snapshot)
    files = []
    if include_source and source:
        source = Path(source).resolve()
        files.append((source, 'media/original' + source.suffix.lower()))
        snapshot['source'] = {**snapshot.get('source', {}), 'path': files[-1][1]}
    for item in snapshot.get('segments') or []:
        value = item.get('audio') or {}
        if value.get('path') and Path(value['path']).is_file():
            path = Path(value['path']).resolve()
            if value.get('sha256') and audio.file_hash(path) != value['sha256']:
                raise ValueError('A generated segment changed before project backup.')
            relative = 'segments/' + _hash(item.get('id', ''))[:24] + path.suffix.lower()
            files.append((path, relative)); value['path'] = relative
    voices = sanitize(context.get('voice_profiles') or {})
    for identifier, profile in voices.items() if isinstance(voices, dict) else []:
        prepared = profile.get('prepared_reference') or {}
        path = prepared.get('path') or profile.get('reference_audio')
        if profile.get('consent') is True and path and Path(path).is_file():
            source_path = Path(path).resolve()
            expected = prepared.get('sha256') or profile.get('reference_sha256')
            if expected and audio.file_hash(source_path) != expected:
                raise ValueError('A permitted voice reference changed before backup.')
            relative = 'voices/' + _hash(identifier)[:24] + source_path.suffix.lower()
            files.append((source_path, relative)); profile['reference_audio'] = relative
            if prepared:
                prepared['path'] = relative
    snapshot['voice_profiles'] = voices
    snapshot['backup'] = {'schema': 1, 'source_embedded': bool(include_source and source),
                          'note': 'Private local project backup; keep permitted reference recordings secure. OAuth credentials are excluded.'}
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr('project.json', json.dumps(snapshot, ensure_ascii=False, indent=2))
        for path, relative in files:
            archive.write(path, relative)
        archive.writestr('README.txt', 'FORGE local dubbing project backup. Originals are never modified. Credentials and installed models are not embedded.\n')
    return {'path': str(output), 'files': len(files) + 2, 'source_embedded': bool(include_source and source)}


def _export(project, context, options, stage_dir, data, result, settings, payload):
    segments = project.get('segments') or []
    files, artifacts, warnings = [], [], []
    configured = project.get('options', {}).get('export') or {}
    formats = options.get('formats') or configured.get('formats')
    if formats is None:
        formats = ['video', *configured.get('separate_audio', ['wav']), 'srt', 'vtt', 'txt', 'metadata']
        if configured.get('bilingual'):
            formats.append('bilingual')
        if configured.get('project_backup'):
            formats.append('project')
        if configured.get('provenance'):
            formats.append('provenance')
    allowed = {'video', 'wav', 'flac', 'srt', 'vtt', 'txt', 'metadata', 'bilingual', 'project', 'provenance'}
    if not isinstance(formats, list) or set(formats) - allowed:
        raise ValueError('Choose supported dubbing export formats.')
    # A portable project backup/text export does not depend on prior renders,
    # including unrelated old artifacts that now need repair.
    narration = _find_artifact(context, ['mix', 'narration', 'aligned_audio'], optional=True) if set(formats) & {'wav', 'flac'} else None
    video = _find_artifact(context, ['video', 'export_video'], optional=True) if 'video' in formats else None
    for kind in dict.fromkeys(formats):
        if kind == 'video':
            if not video:
                raise ValueError('Encode a Russian video before exporting it.')
            target = stage_dir / (safe_name(project.get('name', 'Russian version')) + '-ru' + video.suffix.lower())
            shutil.copyfile(video, target)
            if audio.file_hash(target) != audio.file_hash(video):
                raise RuntimeError('Final video copy failed integrity verification.')
            entry = _file(target, 'export_video', settings, media=True)
        elif kind in {'wav', 'flac'}:
            if not narration:
                raise ValueError('Mix or align Russian narration before exporting audio.')
            target = stage_dir / ('russian.' + kind)
            audio.export_audio(narration, target, settings)
            entry = _file(target, 'export_audio', settings, media=True, format=kind)
        elif kind in {'srt', 'vtt'}:
            target = subtitles(segments, stage_dir / ('russian.' + kind), format=kind)
            entry = _file(target, 'russian_' + kind)
        elif kind == 'txt':
            if not any(str(item.get('text') or '').strip() for item in segments):
                raise ValueError('No nonempty Russian transcript is available for export.')
            target = stage_dir / 'russian-transcript.txt'
            target.write_text('\n\n'.join(str(item.get('text') or '') for item in segments) + '\n', encoding='utf-8')
            entry = _file(target, 'russian_transcript')
        elif kind == 'bilingual':
            target = stage_dir / 'english-russian.json'
            json_write(target, {'source_language': 'en', 'target_language': 'ru', 'segments': segments})
            entry = _file(target, 'bilingual_document')
        elif kind == 'project':
            target = stage_dir / 'dubbing-project.zip'
            backup = _safe_project_backup(project, context, target, settings,
                        include_source=options.get('include_source', True) is True, source=_source(payload))
            entry = _file(target, 'project_backup', source_embedded=backup['source_embedded'])
        elif kind == 'provenance':
            source_record = _find_artifact(context, ['provenance'])
            target = stage_dir / 'russian-provenance.json'
            shutil.copyfile(source_record, target)
            entry = _file(target, 'provenance')
        else:
            target = stage_dir / 'russian-metadata.json'
            json_write(target, _metadata_values(project))
            entry = _file(target, 'russian_metadata')
        files.append(target); artifacts.append(entry)
    result.update(artifacts=artifacts, project_updates={'exports': {'formats': list(dict.fromkeys(formats)), 'verified': True}})
    return _finish(stage_dir, files, warnings, result)


def _metadata_values(project):
    source = project.get('publishing') or {}
    options = project.get('options', {}).get('publishing') or {}
    value = copy.deepcopy(project.get('metadata') or source.get('metadata') or options.get('metadata') or {key: options[key] for key in ('title', 'description', 'tags') if key in options})
    for key in ('title', 'description', 'tags'):
        if options.get(key):
            value[key] = copy.deepcopy(options[key])
    if not value.get('title'):
        value['title'] = project.get('name') or 'Русская версия'
    value.setdefault('description', '')
    value.setdefault('tags', [])
    value.update(defaultLanguage='ru', defaultAudioLanguage='ru', subtitle_language='ru')
    value['contains_synthetic_media'] = bool(options.get('contains_synthetic_media', True))
    value['source_project_id'] = project.get('id')
    value['source_video'] = copy.deepcopy(project.get('original_video') or {})
    value['reviewed'] = False
    value['note'] = 'Editable Russian publishing metadata. Review the exact text and intended channel before an explicit Publish or Schedule action.'
    return value


def _metadata(project, context, options, stage_dir, data, result):
    from . import dubbing_models
    metadata = _metadata_values(project)
    warnings = []
    source = project.get('source_metadata') or project.get('source', {}).get('metadata') or {}
    fields = [(key, source.get(key)) for key in ('title', 'description') if isinstance(source.get(key), str) and source.get(key).strip()]
    for kind in ('tags', 'hashtags'):
        values = source.get(kind) or []
        if isinstance(values, list):
            fields += [(kind + '-' + str(index), text) for index, text in enumerate(values[:100]) if isinstance(text, str) and text.strip()]
    chapters = source.get('chapters') or source.get('chapter_headings') or []
    if isinstance(chapters, list):
        fields += [('chapter-' + str(index), item.get('title')) for index, item in enumerate(chapters[:1000])
                   if isinstance(item, dict) and isinstance(item.get('title'), str) and item['title'].strip()]
    existing = project.get('metadata') or {}
    if fields:
        component = project.get('options', {}).get('translation', {}).get('engine', 'opus-en-ru')
        segments = [{'id': 'metadata-' + key, 'start': 0, 'end': 1, 'speaker': 'metadata', 'source_text': value, 'text': ''} for key, value in fields]
        request = {'task': 'translate', 'segments': segments, 'glossary': context.get('glossary') or {}, 'mode': project.get('mode', 'localized')}
        pinned = project.get('options', {}).get('translation', {}).get('model_revision') or (context.get('model_revisions') or {}).get(component)
        if pinned:
            request['model_revision'] = pinned
        translated = dubbing_models.invoke(component, data, request, progress=_progress)
        if pinned and translated.get('model_revision') != pinned:
            raise RuntimeError('Metadata translation did not use the approved immutable model revision.')
        results = {item['id']: item for item in translated.get('segments', [])}
        suggestions = {}
        localized_tags, localized_hashtags, localized_chapters = [], [], []
        configured = project.get('options', {}).get('publishing', {})
        for key, original in fields:
            item = results.get('metadata-' + key)
            if item and isinstance(item.get('text'), str) and item['text'].strip():
                text = item['text']
                if key.startswith('tags-'):
                    localized_tags.append(text); continue
                if key.startswith('hashtags-'):
                    localized_hashtags.append('#' + re.sub(r'[^\w]', '', text, flags=re.UNICODE).lstrip('#')); continue
                if key.startswith('chapter-'):
                    index = int(key.split('-')[-1])
                    localized_chapters.append({**copy.deepcopy(chapters[index]), 'title': text}); continue
                if existing.get('reviewed') or existing.get('manual_edit') or (configured.get(key) or '').strip():
                    suggestions[key] = item['text']
                    warnings.append('Existing edited Russian ' + key + ' was retained; a translation suggestion is available.')
                else:
                    metadata[key] = item['text']
            else:
                warnings.append('The translation engine did not return ' + key + '; enter and review this metadata manually.')
        for key, values in (('tags', localized_tags), ('hashtags', localized_hashtags), ('chapter_headings', localized_chapters)):
            if not values:
                continue
            if existing.get('reviewed') or existing.get('manual_edit') or configured.get(key):
                suggestions[key] = values
            else:
                metadata[key] = values
        if suggestions:
            metadata['translation_suggestions'] = suggestions
        metadata['translation_model_revision'] = translated.get('model_revision', '')
        warnings.extend(translated.get('warnings') or [])
    else:
        warnings.append('No saved English source metadata was available. Enter and review the Russian title/description before publishing.')
    if len(metadata.get('title') or '') > 100:
        warnings.append('The Russian title is longer than YouTube permits. Edit it before publishing; it was not silently truncated.')
    path = stage_dir / 'russian-metadata.json'
    json_write(path, metadata)
    result.update(artifacts=[_file(path, 'russian_metadata')], project_updates={'metadata': metadata})
    return _finish(stage_dir, [path], warnings, result)


def _prepare_upload(project, context, options, stage_dir, data, result, settings):
    video = _find_artifact(context, ['export_video', 'video'])
    subtitles_path = _find_artifact(context, ['russian_srt', 'russian_vtt'], optional=True)
    metadata_path = _find_artifact(context, ['russian_metadata'], optional=True)
    metadata = json.loads(metadata_path.read_text('utf-8')) if metadata_path else _metadata_values(project)
    descriptor = {'project_id': project['id'], 'profile_id': project.get('options', {}).get('publishing', {}).get('profile_id', ''),
                  'video': _file(video, 'video', settings, media=True),
                  'subtitles': _file(subtitles_path, 'subtitles') if subtitles_path else None,
                  'metadata': metadata, 'visibility': 'private', 'review_required': True, 'upload_authorized': False,
                  'manual_fallback': 'https://studio.youtube.com/',
                  'note': 'Preparation never uploads media. Review channel identity, file, metadata and visibility, then explicitly choose Publish or Schedule.'}
    path = stage_dir / 'youtube-upload-review.json'
    json_write(path, descriptor)
    result.update(artifacts=[_file(path, 'upload_review')], project_updates={'upload_preparation': {'status': 'review_required', 'visibility': 'private'}})
    return _finish(stage_dir, [path], [], result)


def _voice_reference(project, context, options, stage_dir, data, result, settings, payload):
    consent = options.get('consent') is True or context.get('voice_consent') is True
    source = _source(payload)
    output = stage_dir / 'permitted-voice-reference.wav'
    start = options.get('start')
    if start is None and (options.get('reference_start', 0) > 0 or options.get('reference_end', 0) > 0):
        start = options.get('reference_start', 0)
    seconds = options.get('seconds', options.get('reference_seconds', 15))
    if options.get('reference_end'):
        seconds = options['reference_end'] - (start or 0)
    prepared = audio.prepare_reference(source, output, settings, consent=consent, start=start, seconds=seconds)
    reference_text = str(options.get('reference_text') or '')[:12000]
    transcript_files = []
    if not reference_text.strip():
        try:
            from . import dubbing_models
            from .models import Transcribe
            model = project.get('options', {}).get('asr', {}).get('model', 'large-v3-turbo')
            existing = dubbing_models.existing_whisper_state(data, model)
            if existing.get('ready'):
                from .worker import transcribe
                transcript_files, notes = transcribe(output, stage_dir, 'voice-reference-transcript',
                    Transcribe(model=model, device='cpu', language='en', timestamps='segment', formats=['json']).model_dump(), data)
                record = json.loads(next(path for path in transcript_files if path.suffix == '.json').read_text('utf-8'))
                reference_text = ' '.join(str(item.get('text') or '').strip() for item in record.get('segments', [])).strip()
                prepared['warnings'].extend(notes)
            else:
                try:
                    installed = dubbing_models.resolve('whisper-' + model, data)
                except (OSError, RuntimeError, ValueError):
                    installed = None
                if installed:
                    record = dubbing_models.invoke('whisper-' + model, data,
                        {'task': 'transcribe', 'source': str(output), 'model_revision': installed['model_revision'],
                         'language': 'en', 'timestamps': 'segment', 'device': 'cpu'}, progress=_progress)
                    reference_text = ' '.join(str(item.get('source_text') or item.get('text') or '').strip() for item in record.get('segments', [])).strip()
        except (OSError, RuntimeError, ValueError) as error:
            prepared['warnings'].append('The prepared reference was kept, but automatic English reference transcription needs review: ' + str(error)[:400])
        if not reference_text:
            prepared['warnings'].append('No reference transcript is available yet. Install/repair Speech Recognition, enter its text, or explicitly test embedding-only cloning before approving the voice.')
    stat = source.stat()
    component = options.get('model') or options.get('engine') or 'qwen3-tts-0.6b-base'
    profile = {'id': options.get('voice_profile_id') or options.get('id') or uuid.uuid4().hex,
               'name': str(options.get('name') or 'My Voice')[:120], 'description': str(options.get('description') or '')[:2000],
               'model': component, 'model_revision': (context.get('model_revisions') or {}).get(component, ''),
               'reference_text': reference_text,
               'target_language': 'ru', 'consent': consent, 'approved': False,
               'original_reference': {'path': str(source), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sha256': audio.file_hash(source)},
               'prepared_reference': _file(output, 'voice_reference', settings, media=True),
               'audio_quality': prepared['quality'], 'reference_window': {'start': prepared['start'], 'end': prepared['end']}}
    result.update(voice_profile_update=profile, artifacts=[profile['prepared_reference'], *[_file(path, 'voice_reference_transcript') for path in transcript_files]])
    return _finish(stage_dir, [output, *transcript_files], prepared['warnings'], result)


def process(payload, stage_dir, data):
    """Return publishable files, warnings and parent-owned proposed state updates."""
    stage_dir, data = Path(stage_dir).resolve(), Path(data).resolve()
    stage_dir.mkdir(parents=True, exist_ok=True)
    context = copy.deepcopy(payload.get('dubbing') or {})
    project = copy.deepcopy(context.get('project') or {})
    options = copy.deepcopy(payload.get('options') or {})
    settings = payload.get('settings') or {}
    tool = str(payload.get('tool', ''))
    stage = tool.removeprefix('dubbing_')
    if stage not in STAGES:
        raise ValueError('Unsupported dubbing production stage.')
    if not isinstance(project, dict) or (stage != 'voice_reference' and not project.get('id')):
        raise ValueError('A saved dubbing project snapshot is required.')
    result = {'schema': 1, 'project_id': project.get('id', ''), 'run_id': context.get('run_id', ''),
              'stage': tool, 'input_revision': project.get('revision'), 'segment_updates': [],
              'project_updates': {}, 'artifacts': [], 'stage_settings_hash': _stage_fingerprint(project, tool) if project.get('id') else ''}
    started = time.monotonic()
    _progress(0, 'Starting ' + stage.replace('_', ' '), progress_measured=False, stage='Dubbing ' + stage.replace('_', ' '))
    if stage == 'transcribe':
        value = _transcribe(project, context, options, stage_dir, data, result, payload)
    elif stage == 'translate':
        value = _translate(project, context, options, stage_dir, data, result)
    elif stage == 'voice':
        result['_settings'] = settings
        value = _voice(project, context, options, stage_dir, data, result)
    elif stage == 'align':
        value = _align(project, context, options, stage_dir, data, result, settings, payload)
    elif stage == 'separate':
        value = _separate(project, context, options, stage_dir, data, result, settings, payload)
    elif stage == 'mix':
        value = _mix(project, context, options, stage_dir, data, result, settings, payload)
    elif stage == 'effects':
        value = _effects(project, context, options, stage_dir, data, result, settings)
    elif stage == 'lipsync':
        value = _lipsync(project, context, options, stage_dir, data, result, settings, payload)
    elif stage == 'encode':
        value = _encode(project, context, options, stage_dir, data, result, settings, payload)
    elif stage == 'export':
        value = _export(project, context, options, stage_dir, data, result, settings, payload)
    elif stage == 'metadata':
        value = _metadata(project, context, options, stage_dir, data, result)
    elif stage == 'prepare_upload':
        value = _prepare_upload(project, context, options, stage_dir, data, result, settings)
    else:
        value = _voice_reference(project, context, options, stage_dir, data, result, settings, payload)
    for path in value[0]:
        path = Path(path)
        if path.is_symlink() or not path.resolve().is_relative_to(stage_dir):
            raise RuntimeError('A dubbing stage attempted to publish an external/original file. Its source was preserved.')
    value[2]['elapsed_seconds'] = max(0, time.monotonic() - started)
    return value
