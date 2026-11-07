"""Real FFmpeg dubbing tests with explicitly synthetic model-boundary fixtures.

The tiny sine fixtures exercise timing/mux/cache contracts, not neural inference
or voice similarity. Actual model and owner-GPU acceptance remain separate.
"""
from array import array
import copy
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time
import types
import wave
import zipfile

import pytest

from forge import dubbing_audio as audio
from forge import dubbing_pipeline as pipeline


@pytest.fixture(scope='module')
def media_tools():
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Real FFmpeg/FFprobe are required for dubbing media acceptance.'
    return {}


def tone(path, duration=1, frequency=440, rate=24000):
    import struct
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(rate)
        for begin in range(0, int(duration*rate), 4096):
            values = [int(6000*math.sin(2*math.pi*frequency*index/rate)) for index in range(begin, min(begin+4096, int(duration*rate)))]
            output.writeframesraw(struct.pack('<'+'h'*len(values), *values))
    return path


def video(path, seconds=3):
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
        '-f', 'lavfi', '-i', 'color=c=blue:size=160x96:rate=24',
        '-f', 'lavfi', '-i', 'sine=frequency=130:sample_rate=48000', '-t', str(seconds),
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(path)], check=True)
    return path


def rms(path, start, seconds):
    raw = subprocess.check_output(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-ss', str(start), '-t', str(seconds), '-i', str(path), '-map', '0:a:0', '-f', 'f32le', '-ac', '1', '-ar', '8000', '-'])
    samples = array('f'); samples.frombytes(raw)
    return math.sqrt(sum(value*value for value in samples)/max(1, len(samples)))


def artifact(path, kind, **extra):
    path = Path(path)
    return {'id': 'saved-' + kind, 'kind': kind, 'path': str(path), 'size': path.stat().st_size,
            'sha256': audio.file_hash(path), **extra}


def project(source=None):
    return {'id': 'project-russian', 'name': 'Русская версия', 'revision': 1, 'mode': 'localized',
        'source': {'path': str(source)} if source else {}, 'source_language': 'en', 'target_language': 'ru',
        'options': {'asr': {'model': 'large-v3-turbo', 'device': 'cpu'},
            'translation': {'engine': 'opus-en-ru'},
            'tts': {'engine': 'qwen3-tts-0.6b-base', 'device': 'cpu', 'voice_profile_id': 'my-voice'},
            'timing': {'strategy': 'preserve', 'max_tempo': 1.15},
            'audio': {'normalize': False, 'compression': False, 'limiter': False},
            'lip_sync': {'mode': 'off'}, 'export': {'container': 'mp4', 'video_codec': 'copy', 'audio_codec': 'aac', 'subtitles': 'sidecar'},
            'publishing': {'profile_id': 'russian-channel'}},
        'segments': [
            {'id': 'one', 'start': .2, 'end': 1.2, 'speaker': 'speaker1', 'source_text': 'Hello, Alice 2026.', 'text': 'Здравствуйте, Алиса 2026.', 'reviewed': True, 'manual_edit': True},
            {'id': 'two', 'start': 1.6, 'end': 2.6, 'speaker': 'speaker1', 'source_text': 'Second sentence.', 'text': 'Второе предложение.', 'reviewed': True, 'manual_edit': False}],
        'speakers': {'speaker1': {'voice_profile_id': 'my-voice'}}}


def payload(value, stage, data, **context):
    source = value.get('source') or {}
    result = {'tool': 'dubbing_' + stage, 'options': {}, 'settings': {}, 'source': source,
              'dubbing': {'project': value, 'run_id': 'run-one', **context}}
    if source.get('path'):
        stat = Path(source['path']).stat()
        result['source_stat'] = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    return result


def model_fixture(monkeypatch):
    from forge import dubbing_models
    calls = []
    monkeypatch.setattr(dubbing_models, 'resolve', lambda *args, **kwargs: {'model_revision': 'a'*40, 'runtime_revision': 'runtime-fixture'})
    def invoke(component, data, request, **kwargs):
        calls.append(copy.deepcopy(request))
        if request['task'] == 'translate':
            return {'model_revision': request.get('model_revision') or 'a'*40, 'segments': [{**item, 'text': 'Предложенный перевод 2026.'} for item in request['segments']]}
        for index, item in enumerate(request['segments']):
            tone(item['output'], .6, 440+index*110)
        return {'model_revision': request['model_revision'], 'segments': [{'id': item['id'], 'path': item['output']} for item in request['segments']]}
    monkeypatch.setattr(dubbing_models, 'invoke', invoke)
    return calls


def voice_profile(reference):
    return {'my-voice': {'id': 'my-voice', 'revision': 1, 'name': 'Permitted fixture voice',
             'engine': 'qwen3-tts-0.6b-base', 'model_revision': 'a'*40, 'consent': True, 'approved': True,
             'prepared_reference': artifact(reference, 'voice_reference'), 'reference_text': 'Synthetic tone fixture, not actual speech.'}}


def test_audio_extraction_preserves_original_and_measures_window(tmp_path, media_tools):
    source = video(tmp_path/'english.mp4')
    before = audio.file_hash(source)
    measured = audio.extract_audio(source, tmp_path/'selection.wav', {}, start=.25, end=1.75, rate=24000, channels=1)
    assert measured['duration'] == pytest.approx(1.5, abs=.01)
    assert measured['sample_rate'] == 24000 and measured['channels'] == 1
    assert audio.file_hash(source) == before
    with pytest.raises(ValueError, match='original'):
        audio.extract_audio(source, source, {})


@pytest.mark.parametrize('strategy', ['preserve', 'natural', 'balanced', 'manual'])
def test_alignment_never_blindly_accelerates_overlong_speech(strategy):
    segments = [{'id': 'long', 'start': 0, 'end': 1, 'audio': {'duration': 3}},
                {'id': 'next', 'start': 1.5, 'end': 2.5, 'audio': {'duration': .7}}]
    result = audio.plan_alignment(segments, strategy, source_duration=4)
    first = result['segments'][0]['timing']
    assert first['tempo'] <= 1.15
    assert first['measured_duration'] == 3
    if strategy == 'natural':
        assert result['segments'][1]['timing']['start'] >= 3.5
    else:
        assert first['tempo'] == 1
        assert first['needs_review'] and 'Shorten' in ' '.join(first['warnings'])


def test_small_tempo_correction_and_real_timeline_preserve_silence(tmp_path, media_tools):
    one, two = tone(tmp_path/'one.wav', .4), tone(tmp_path/'two.wav', .5, 660)
    plan = audio.plan_alignment([
        {'id': 'one', 'start': .3, 'end': .7, 'audio': {'path': str(one), 'duration': .4}},
        {'id': 'two', 'start': 1.7, 'end': 2.2, 'audio': {'path': str(two), 'duration': .5}}], 'preserve', source_duration=3)
    rendered = audio.render_alignment(plan, tmp_path/'timeline.wav', {})
    assert rendered['duration'] == pytest.approx(3, abs=.02)
    assert rms(tmp_path/'timeline.wav', .02, .15) < .0001
    assert rms(tmp_path/'timeline.wav', .36, .2) > .05
    assert rms(tmp_path/'timeline.wav', 1, .3) < .0001
    modest = audio.plan_alignment([{'id': 'bounded', 'start': 0, 'end': 1, 'audio': {'duration': 1.1}}], 'preserve', source_duration=1)
    assert modest['segments'][0]['timing']['tempo'] == pytest.approx(1.1)


def test_batched_alignment_does_not_pad_every_temporary_to_full_video_length(tmp_path, monkeypatch, media_tools):
    clip = tone(tmp_path/'short.wav', .12)
    segments = [{'id': str(index), 'start': .4+index*.2, 'end': .52+index*.2,
                 'audio': {'path': str(clip), 'duration': .12}} for index in range(30)]
    real = audio.ffmpeg
    batch_durations = []
    def measured(arguments, settings, total=0):
        real(arguments, settings, total)
        path = Path(arguments[-1])
        if path.name.startswith('batch-'):
            batch_durations.append(audio.audio_info(path, settings)['duration'])
    monkeypatch.setattr(audio, 'ffmpeg', measured)
    plan = audio.plan_alignment(segments, 'preserve', source_duration=30)
    output = tmp_path/'long-timeline.wav'
    audio.render_alignment(plan, output, {})
    assert len(batch_durations) == 2 and sum(batch_durations) < 8
    assert audio.audio_info(output, {})['duration'] == pytest.approx(30, abs=.02)
    assert rms(output, .01, .2) < .0001
    assert rms(output, .42, .06) > .05
    assert rms(output, 5.84, .06) > .05


def test_partial_voice_regeneration_alignment_keeps_unselected_narration(tmp_path, media_tools):
    source = video(tmp_path/'english.mp4')
    value = project(source)
    for index, segment in enumerate(value['segments']):
        path = tone(tmp_path/f'voice-{index}.wav', .6, 440+index*110)
        segment['audio'] = {**artifact(path, 'voice_segment'), **audio.audio_info(path, {})}
    request = payload(value, 'align', tmp_path, segment_ids=['two'])
    _, _, metadata = pipeline.process(request, tmp_path/'alignment', tmp_path/'data')
    narration = next(item['path'] for item in metadata['artifacts'] if item['kind'] == 'narration')
    assert {item['id'] for item in metadata['segment_updates']} == {'one', 'two'}
    assert rms(narration, .3, .2) > .05
    assert rms(narration, 1.7, .2) > .05


@pytest.mark.parametrize('retain_path', [True, False])
def test_alignment_rejects_edited_text_stale_speech_without_reusing_prior_artifact(tmp_path, media_tools, retain_path):
    source = video(tmp_path/'english.mp4')
    speech = tone(tmp_path/'old-utterance.wav', .6)
    before = audio.file_hash(speech)
    value = project(source)
    value['segments'] = value['segments'][:1]
    value['segments'][0].update(text='Новый проверенный текст.', reviewed=True)
    saved = {**artifact(speech, 'voice_segment'), 'segment_id': 'one', 'cache_key': 'a'*64}
    value['segments'][0]['audio'] = {**saved, 'stale': True}
    if not retain_path:
        value['segments'][0]['audio'].pop('path')
    with pytest.raises(ValueError, match='Regenerate.*segment'):
        pipeline.process(payload(value, 'align', tmp_path, artifacts=[saved]), tmp_path/'alignment', tmp_path/'data')
    assert audio.file_hash(speech) == before
    assert not (tmp_path/'alignment'/'russian-narration.wav').exists()


@pytest.mark.parametrize('incompatible', ['unknown-cache', 'different-cache', 'stale-artifact', 'different-hash'])
def test_alignment_never_recovers_incompatible_voice_artifact_by_segment_id(tmp_path, media_tools, incompatible):
    source = video(tmp_path/'english.mp4')
    speech = tone(tmp_path/'old-utterance.wav', .6)
    value = project(source)
    value['segments'] = value['segments'][:1]
    saved = {**artifact(speech, 'voice_segment'), 'segment_id': 'one', 'cache_key': 'a'*64}
    value['segments'][0]['audio'] = {'cache_key': 'a'*64, 'sha256': saved['sha256']}
    if incompatible == 'unknown-cache':
        value['segments'][0]['audio'] = {}
    elif incompatible == 'different-cache':
        saved['cache_key'] = 'b'*64
    elif incompatible == 'stale-artifact':
        saved['stale'] = True
    else:
        saved['sha256'] = 'b'*64
    with pytest.raises(ValueError, match='Generate Russian speech'):
        pipeline.process(payload(value, 'align', tmp_path, artifacts=[saved]), tmp_path/'alignment', tmp_path/'data')
    assert not (tmp_path/'alignment'/'russian-narration.wav').exists()


def test_alignment_can_recover_the_exact_saved_clip_and_respects_edited_manual_window(tmp_path, media_tools):
    source = video(tmp_path/'english.mp4')
    speech = tone(tmp_path/'saved-utterance.wav', .6)
    before = audio.file_hash(speech)
    value = project(source)
    value['options']['timing']['strategy'] = 'manual'
    value['segments'] = value['segments'][:1]
    value['segments'][0].update(start=2, end=3,
        timing={'strategy': 'manual', 'start': 0, 'end': .6, 'window_start': 0, 'window_end': 1})
    saved = {**artifact(speech, 'voice_segment'), 'segment_id': 'one', 'cache_key': 'a'*64}
    value['segments'][0]['audio'] = {'cache_key': saved['cache_key'], 'sha256': saved['sha256']}
    request = payload(value, 'align', tmp_path, artifacts=[saved])
    _, _, metadata = pipeline.process(request, tmp_path/'alignment', tmp_path/'data')
    timing = metadata['segment_updates'][0]['timing']
    assert timing['start'] == 2 and timing['end'] == pytest.approx(2.6)
    assert timing['window_start'] == 2 and timing['window_end'] == 3
    narration = next(item['path'] for item in metadata['artifacts'] if item['kind'] == 'narration')
    assert rms(narration, .1, .5) < .0001
    assert rms(narration, 2.1, .3) > .05
    value['segments'][0]['timing'] = timing
    _, _, repeated = pipeline.process(payload(value, 'align', tmp_path, artifacts=[saved]), tmp_path/'alignment-again', tmp_path/'data')
    assert repeated['segment_updates'][0]['timing'] == timing
    repeated_narration = next(item['path'] for item in repeated['artifacts'] if item['kind'] == 'narration')
    assert audio.file_hash(repeated_narration) == audio.file_hash(narration)
    captions = pipeline.subtitles(value['segments'], tmp_path/'russian.srt')
    assert '00:00:02,000 --> 00:00:02,600' in captions.read_text('utf-8')
    assert audio.file_hash(speech) == before


def test_real_mix_reuses_effects_renderer_and_explains_unseparated_track(tmp_path, media_tools):
    narration = tone(tmp_path/'russian.wav', 3, 440)
    original = tone(tmp_path/'original.wav', 3, 110)
    before = audio.file_hash(original)
    result = audio.mix_audio(narration, tmp_path/'mixed.wav', {},
        {'normalize': False, 'compression': False, 'limiter': False, 'ducking': True}, original_audio=original)
    assert result['duration'] == pytest.approx(3, abs=.02)
    assert result['separate_background'] is False
    assert 'not source-separated' in ' '.join(result['warnings'])
    assert result['mastering']['renderer'].startswith('FFmpeg')
    assert audio.file_hash(original) == before and rms(tmp_path/'mixed.wav', .5, .5) > .05


def test_explicit_linear_effects_stage_applies_only_its_frozen_additional_chain(tmp_path, media_tools):
    source = tone(tmp_path/'russian-mix.wav', 3)
    before = audio.file_hash(source)
    value = project()
    value['options']['audio']['effects'] = [{'type': 'gain', 'params': {'db': 24}}]
    request = payload(value, 'effects', tmp_path, artifacts=[artifact(source, 'mix')])
    request['options']['effects'] = [{'type': 'gain', 'params': {'db': 6}}]
    _, _, metadata = pipeline.process(request, tmp_path/'effects', tmp_path/'data')
    output = next(item['path'] for item in metadata['artifacts'] if item['kind'] == 'mix')
    assert rms(output, .5, .5)/rms(source, .5, .5) == pytest.approx(10**(6/20), rel=.03)
    assert any(item['kind'] == 'mastered_audio' for item in metadata['artifacts'])
    assert metadata['project_updates']['mix']['effects'] == request['options']['effects']
    assert audio.file_hash(source) == before


def test_stream_copy_export_preserves_video_packets(tmp_path, media_tools):
    original = video(tmp_path/'original.mp4')
    russian = tone(tmp_path/'russian.wav', 3, 660)
    before = audio.file_hash(original)
    result = audio.encode_video(original, russian, tmp_path/'russian.mp4', {}, {'video_codec': 'copy'})
    def packets(path):
        return subprocess.check_output(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', str(path), '-map', '0:v:0', '-c:v', 'copy', '-f', 'hash', '-hash', 'sha256', '-']).strip()
    assert result['video_stream_copied'] is True and packets(original) == packets(tmp_path/'russian.mp4')
    assert result['output_resolution'] == [160, 96]
    assert audio.file_hash(original) == before


def test_optional_provenance_is_real_unsigned_source_output_hash_record(tmp_path, media_tools):
    source = video(tmp_path/'english.mp4')
    narration = tone(tmp_path/'russian.wav', 3)
    value = project(source); value['options']['export']['provenance'] = True
    _, _, result = pipeline.process(payload(value, 'encode', tmp_path, artifacts=[artifact(narration, 'mix')]), tmp_path/'encode', tmp_path/'data')
    output = next(item for item in result['artifacts'] if item['kind'] == 'video')
    provenance = next(item for item in result['artifacts'] if item['kind'] == 'provenance')
    record = json.loads(Path(provenance['path']).read_text(encoding='utf-8'))
    assert record['source']['sha256'] == audio.file_hash(source)
    assert record['output']['sha256'] == audio.file_hash(output['path'])
    assert record['lip_sync_applied'] is False and 'not a C2PA signature' in record['scope']
    assert 'AI-assisted' in audio.media_info(output['path'], {})['format']['tags']['comment']


def test_longer_narration_requires_explicit_realignment_or_extension(tmp_path, media_tools):
    original = video(tmp_path/'english.mp4', 2)
    russian = tone(tmp_path/'russian.wav', 3)
    with pytest.raises(ValueError, match='longer than the video'):
        audio.encode_video(original, russian, tmp_path/'not-created.mp4', {}, {'video_codec': 'copy'})
    assert not (tmp_path/'not-created.mp4').exists()
    assert original.is_file() and russian.is_file()


def test_utf8_subtitles_use_actual_segment_positions_and_escape_markup(tmp_path):
    items = [{'id': 'one', 'start': 0, 'end': 2, 'text': 'Привет <script>alert(1)</script>!\nНовая строка.',
              'timing': {'start': 1.25, 'end': 2.5}}]
    srt = pipeline.subtitles(items, tmp_path/'russian.srt').read_text('utf-8')
    vtt = pipeline.subtitles(items, tmp_path/'russian.vtt', format='vtt').read_text('utf-8')
    assert '00:00:01,250 --> 00:00:02,500' in srt
    assert '&lt;script&gt;' in srt and '<script>' not in srt and 'Привет' in srt
    assert vtt.startswith('WEBVTT') and '00:00:01.250 --> 00:00:02.500' in vtt


def test_selected_translation_preserves_reviewed_edits_and_offers_suggestion(tmp_path, monkeypatch):
    calls = model_fixture(monkeypatch)
    value = project()
    request = payload(value, 'translate', tmp_path, segment_ids=['one'], glossary={'entries': [{'source': 'Alice', 'target': 'Алиса'}]})
    files, warnings, result = pipeline.process(request, tmp_path/'translate', tmp_path/'data')
    update = result['segment_updates'][0]
    assert len(result['segment_updates']) == 1 and update['id'] == 'one'
    assert 'text' not in update and update['translation_suggestion'] == 'Предложенный перевод 2026.'
    assert update['expected_input_hash'] == pipeline._fingerprint(value['segments'][0])
    assert calls[0]['glossary']['entries'][0]['source'] == 'Alice'
    assert calls[0]['overwrite_edited'] is True
    assert any('preserved' in item for item in warnings)
    assert all(path.is_relative_to(tmp_path/'translate') for path in files)


def test_automatic_translation_does_not_retranslate_reviewed_manual_text(tmp_path, monkeypatch):
    calls = model_fixture(monkeypatch)
    value = project()
    _, warnings, result = pipeline.process(payload(value, 'translate', tmp_path), tmp_path/'unchanged', tmp_path/'data')
    assert not calls and not result['segment_updates']
    assert result['project_updates']['translation_status'] == 'existing_reviewed_text_preserved'
    assert any('preserved' in warning for warning in warnings)


def test_frozen_model_revisions_are_used_without_mutating_project_settings(tmp_path, monkeypatch, media_tools):
    from forge import dubbing_models
    calls = model_fixture(monkeypatch)
    value = project()
    original = copy.deepcopy(value)
    pipeline.process(payload(value, 'translate', tmp_path, segment_ids=['two'],
        model_revisions={'opus-en-ru': 'b'*40}), tmp_path/'translation-pin', tmp_path/'data')
    assert calls[0]['model_revision'] == 'b'*40
    reference = tone(tmp_path/'reference.wav', 3)
    profiles = voice_profile(reference); profiles['my-voice']['model_revision'] = ''
    def resolve(component, data, revision=''):
        assert revision == 'c'*40
        return {'model_revision': revision, 'runtime_revision': 'runtime-fixture'}
    monkeypatch.setattr(dubbing_models, 'resolve', resolve)
    _, _, result = pipeline.process(payload(value, 'voice', tmp_path, voice_profiles=profiles, review_approved=True,
        segment_ids=['one'], model_revisions={'qwen3-tts-0.6b-base': 'c'*40}), tmp_path/'voice-pin', tmp_path/'data')
    assert calls[1]['model_revision'] == 'c'*40
    assert result['segment_updates'][0]['audio']['model_revision'] == 'c'*40
    assert value == original


def test_voice_caches_unaffected_segments_across_selected_regeneration(tmp_path, monkeypatch, media_tools):
    calls = model_fixture(monkeypatch)
    reference = tone(tmp_path/'permitted-reference.wav', 3)
    value = project()
    context = {'voice_profiles': voice_profile(reference), 'review_approved': True}
    _, _, first = pipeline.process(payload(value, 'voice', tmp_path, **context), tmp_path/'voice-one', tmp_path/'data')
    assert len(calls) == 1 and len(calls[0]['segments']) == 2
    unchanged = first['segment_updates'][0]['audio']
    before = audio.file_hash(unchanged['path'])
    value['segments'][1]['text'] = 'Изменилось только второе предложение.'
    _, _, second = pipeline.process(payload(value, 'voice', tmp_path, **context), tmp_path/'voice-two', tmp_path/'data')
    assert len(calls) == 2 and [item['id'] for item in calls[1]['segments']] == ['two']
    assert second['segment_updates'][0]['audio']['path'] == unchanged['path']
    assert second['segment_updates'][0]['audio']['cached'] is True
    assert audio.file_hash(unchanged['path']) == before


def test_global_voice_effects_are_rendered_and_part_of_cache_identity(tmp_path, monkeypatch, media_tools):
    model_fixture(monkeypatch)
    reference = tone(tmp_path/'reference.wav', 3)
    value = project()
    context = {'voice_profiles': voice_profile(reference), 'review_approved': True, 'segment_ids': ['one']}
    _, _, plain = pipeline.process(payload(value, 'voice', tmp_path, **context), tmp_path/'plain', tmp_path/'data')
    value['options']['tts']['effects'] = [{'type': 'gain', 'params': {'db': 6}}]
    _, _, louder = pipeline.process(payload(value, 'voice', tmp_path, **context), tmp_path/'louder', tmp_path/'data')
    before = plain['segment_updates'][0]['audio']; after = louder['segment_updates'][0]['audio']
    assert before['cache_key'] != after['cache_key']
    assert rms(after['path'], .1, .3) / rms(before['path'], .1, .3) == pytest.approx(10**(6/20), rel=.03)


def test_voice_model_revision_change_does_not_reuse_old_model_audio(tmp_path, monkeypatch, media_tools):
    from forge import dubbing_models
    calls = model_fixture(monkeypatch)
    reference = tone(tmp_path/'reference.wav', 3)
    profiles = voice_profile(reference); profiles['my-voice']['model_revision'] = ''
    value = project()
    context = {'voice_profiles': profiles, 'review_approved': True, 'segment_ids': ['one']}
    _, _, first = pipeline.process(payload(value, 'voice', tmp_path, **context), tmp_path/'model-one', tmp_path/'data')
    first_path = first['segment_updates'][0]['audio']['path']
    first_digest = audio.file_hash(first_path)
    monkeypatch.setattr(dubbing_models, 'resolve', lambda *args, **kwargs: {'model_revision': 'b'*40, 'runtime_revision': 'runtime-fixture'})
    _, _, second = pipeline.process(payload(value, 'voice', tmp_path, **context), tmp_path/'model-two', tmp_path/'data')
    assert len(calls) == 2 and calls[1]['model_revision'] == 'b'*40
    assert second['segment_updates'][0]['audio']['path'] != first_path
    assert audio.file_hash(first_path) == first_digest


def test_generated_float_audio_peak_is_reduced_before_pcm_conversion(tmp_path, monkeypatch, media_tools):
    from forge import dubbing_models
    model_fixture(monkeypatch)
    reference = tone(tmp_path/'reference.wav', 3)
    def loud(component, data, request, **kwargs):
        for item in request['segments']:
            subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n', '-f', 'lavfi',
                '-i', 'aevalsrc=2*sin(2*PI*440*t):s=24000:d=0.6', '-c:a', 'pcm_f32le', item['output']], check=True)
        return {'model_revision': 'a'*40}
    monkeypatch.setattr(dubbing_models, 'invoke', loud)
    _, warnings, result = pipeline.process(payload(project(), 'voice', tmp_path,
        voice_profiles=voice_profile(reference), review_approved=True, segment_ids=['one']), tmp_path/'safe', tmp_path/'data')
    generated = result['segment_updates'][0]['audio']
    from forge.audio_effects import levels
    assert generated['peak_normalization_db'] < -6
    assert levels(generated['path'], {})['peak_dbfs'] < -.4
    assert any('avoid clipping' in warning for warning in warnings)


def test_later_model_batch_failure_preserves_earlier_verified_segment_cache(tmp_path, monkeypatch, media_tools):
    from forge import dubbing_models
    model_fixture(monkeypatch)
    reference = tone(tmp_path/'reference.wav', 3)
    value = project()
    context = {'voice_profiles': voice_profile(reference), 'review_approved': True}
    def broken(component, data, request, **kwargs):
        tone(request['segments'][0]['output'], .6)
        kwargs['progress'](50, 'First fixture voice file atomically completed', stage='Russian voice')
        assert list((Path(data)/'dubbing'/'cache'/'voices').glob('*/receipt.json')), 'A completed file must be durable before the model batch returns.'
        raise RuntimeError('Explicit fixture model failure after the first audio file')
    monkeypatch.setattr(dubbing_models, 'invoke', broken)
    with pytest.raises(RuntimeError, match='fixture model failure'):
        pipeline.process(payload(value, 'voice', tmp_path, **context), tmp_path/'failed', tmp_path/'data')
    receipts = list((tmp_path/'data'/'dubbing'/'cache'/'voices').glob('*/receipt.json'))
    assert len(receipts) == 1 and (receipts[0].parent/'audio.wav').is_file()
    calls = []
    def retry(component, data, request, **kwargs):
        calls.append(request)
        for item in request['segments']:
            tone(item['output'], .6)
        return {'model_revision': 'a'*40}
    monkeypatch.setattr(dubbing_models, 'invoke', retry)
    _, _, result = pipeline.process(payload(value, 'voice', tmp_path, **context), tmp_path/'retry', tmp_path/'data')
    assert [item['id'] for item in calls[0]['segments']] == ['two']
    assert result['segment_updates'][0]['audio']['cached'] is True


def test_actual_worker_termination_reuses_already_verified_voice_segment(tmp_path, monkeypatch, media_tools):
    reference = tone(tmp_path/'permitted-reference.wav', 3)
    value = project()
    context = {'voice_profiles': voice_profile(reference), 'review_approved': True}
    request = payload(value, 'voice', tmp_path, **context)
    source = tmp_path/'request.json'; source.write_text(json.dumps(request), encoding='utf-8')
    marker = tmp_path/'segment-durable.txt'
    script = r'''
import json,sys,time,wave,struct,math
from pathlib import Path
from forge import dubbing_models as models,dubbing_pipeline as pipeline
models.resolve=lambda *args,**kwargs:{'model_revision':'a'*40,'runtime_revision':'runtime-fixture'}
def infer(component,data,request,progress=None):
    path=Path(request['segments'][0]['output'])
    with wave.open(str(path),'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(24000)
        stream.writeframes(struct.pack('<'+'h'*14400,*[int(6000*math.sin(2*math.pi*440*i/24000)) for i in range(14400)]))
    progress(50,'Explicit fixture first segment complete',stage='Russian voice')
    Path(sys.argv[4]).write_text('A real playable cache file and receipt exist')
    while True: time.sleep(.1)
models.invoke=infer
pipeline.process(json.loads(Path(sys.argv[1]).read_text()),Path(sys.argv[2]),Path(sys.argv[3]))
'''
    with (tmp_path/'worker.log').open('w') as log:
        child = subprocess.Popen([sys.executable, '-c', script, str(source), str(tmp_path/'killed-worker'), str(tmp_path/'data'), str(marker)],
                                 stdout=log, stderr=subprocess.STDOUT, cwd=Path(__file__).resolve().parents[1])
        try:
            deadline = time.monotonic()+15
            while not marker.exists() and child.poll() is None and time.monotonic()<deadline:
                time.sleep(.025)
            assert marker.is_file(), (tmp_path/'worker.log').read_text(encoding='utf-8')
            child.kill(); child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill(); child.wait(timeout=5)
    calls = model_fixture(monkeypatch)
    _, _, result = pipeline.process(request, tmp_path/'restarted-worker', tmp_path/'data')
    assert len(calls) == 1 and [item['id'] for item in calls[0]['segments']] == ['two']
    assert result['segment_updates'][0]['audio']['cached'] is True


@pytest.mark.parametrize('approved,consent,reviewed,match', [
    (False, True, True, 'approve'), (True, False, True, 'permission'), (True, True, False, 'Review')])
def test_voice_never_bypasses_permission_or_translation_review(tmp_path, monkeypatch, media_tools, approved, consent, reviewed, match):
    calls = model_fixture(monkeypatch)
    reference = tone(tmp_path/'reference.wav', 3)
    profiles = voice_profile(reference)
    profiles['my-voice'].update(approved=approved, consent=consent)
    with pytest.raises(ValueError, match=match):
        pipeline.process(payload(project(), 'voice', tmp_path, voice_profiles=profiles, review_approved=reviewed), tmp_path/'blocked', tmp_path/'data')
    assert not calls


def test_reference_preparation_requires_permission_and_preserves_input(tmp_path, media_tools):
    source = tone(tmp_path/'own-recording.wav', 4)
    before = audio.file_hash(source)
    request = payload({'id': '', 'source': {'path': str(source)}}, 'voice_reference', tmp_path)
    request['options'] = {'name': 'My permitted voice', 'consent': False}
    with pytest.raises(ValueError, match='Approve'):
        pipeline.process(request, tmp_path/'denied', tmp_path/'data')
    request['options'].update(consent=True, seconds=3)
    _, warnings, metadata = pipeline.process(request, tmp_path/'approved', tmp_path/'data')
    profile = metadata['voice_profile_update']
    assert profile['approved'] is False and profile['consent'] is True
    assert profile['prepared_reference']['duration'] == pytest.approx(3, abs=.02)
    assert 'not speaker identification' in ' '.join(warnings)
    assert audio.file_hash(source) == before


def test_manual_stem_helper_cannot_move_original_files_into_publish_loop(tmp_path, monkeypatch, media_tools):
    from forge import dubbing_separation
    source = video(tmp_path/'english.mp4')
    original_vocals = tone(tmp_path/'manual-vocals.wav', 3)
    before = audio.file_hash(original_vocals)
    monkeypatch.setattr(dubbing_separation, 'process', lambda *args, **kwargs: {'vocals': str(original_vocals), 'separation_performed': True})
    value = project(source); value['options']['audio']['separation'] = 'manual'
    files, _, metadata = pipeline.process(payload(value, 'separate', tmp_path), tmp_path/'separate', tmp_path/'data')
    assert all(path.resolve().is_relative_to(tmp_path/'separate') for path in files)
    assert original_vocals.is_file() and audio.file_hash(original_vocals) == before
    assert metadata['artifacts'][0]['path'] != str(original_vocals)


def test_no_applicable_lip_region_does_not_publish_or_move_original(tmp_path, monkeypatch, media_tools):
    from forge import dubbing_visual
    source = video(tmp_path/'english.mp4')
    narration = tone(tmp_path/'ru.wav', 3)
    before = audio.file_hash(source)
    monkeypatch.setattr(dubbing_visual, 'process', lambda *args, **kwargs: {'path': str(source), 'changed': False, 'warnings': ['No suitable speaking face was found.']})
    value = project(source); value['options']['lip_sync'] = {'mode': 'automatic', 'consent': True}
    files, warnings, metadata = pipeline.process(payload(value, 'lipsync', tmp_path, artifacts=[artifact(narration, 'mix')]), tmp_path/'lip', tmp_path/'data')
    assert all(path != source for path in files) and not metadata['artifacts']
    assert metadata['project_updates']['lip_sync']['video_unchanged'] is True
    assert audio.file_hash(source) == before and narration.is_file() and warnings


def test_lip_preview_forwards_the_selected_start_to_the_optional_adapter(tmp_path, monkeypatch, media_tools):
    from forge import dubbing_visual
    source = video(tmp_path/'english.mp4')
    narration = tone(tmp_path/'ru.wav', 3)
    observed = []
    def visual_adapter_fixture(source, narration, output, config, *args, **kwargs):
        observed.append(copy.deepcopy(config))
        return {'changed': False, 'warnings': ['Explicit adapter-boundary fixture; no visual inference.']}
    monkeypatch.setattr(dubbing_visual, 'process', visual_adapter_fixture)
    value = project(source); value['options']['lip_sync'] = {'mode': 'automatic', 'consent': True}
    value['segments'][0]['timing'] = {'start': 0, 'end': .1, 'stale': True}
    request = payload(value, 'lipsync', tmp_path, artifacts=[artifact(narration, 'mix')])
    request['options'].update(preview=True, preview_start=1.25, preview_seconds=5)
    pipeline.process(request, tmp_path/'lip', tmp_path/'data')
    assert observed[0]['preview'] is True
    assert observed[0]['preview_start'] == 1.25 and observed[0]['preview_seconds'] == 5
    assert observed[0]['speech_windows'][0]['start'] == .2
    assert observed[0]['speech_windows'][0]['end'] == 1.2


def test_subtitles_ignore_stale_alignment_and_keep_current_verified_timestamps(tmp_path):
    path = pipeline.subtitles([
        {'id': 'edited', 'start': 2, 'end': 3, 'text': 'Текущий интервал.',
         'timing': {'start': 0, 'end': .5, 'stale': True}},
        {'id': 'verified', 'start': .1, 'end': .4, 'text': 'Проверенное выравнивание.',
         'timing': {'start': 1, 'end': 1.5}}], tmp_path/'current.srt')
    saved = path.read_text('utf-8')
    assert '00:00:02,000 --> 00:00:03,000' in saved
    assert '00:00:01,000 --> 00:00:01,500' in saved
    assert saved.index('Проверенное выравнивание.') < saved.index('Текущий интервал.')


def test_project_backup_is_portable_and_excludes_credentials(tmp_path, media_tools):
    source = video(tmp_path/'english.mp4')
    reference = tone(tmp_path/'reference.wav', 3)
    value = project(source)
    value['publishing'] = {'access_token': 'synthetic-secret-never-backup', 'metadata': {'title': 'Тест'}}
    result = pipeline._safe_project_backup(value, {'voice_profiles': voice_profile(reference)}, tmp_path/'project.zip', {}, source=source)
    assert result['source_embedded'] is True
    with zipfile.ZipFile(tmp_path/'project.zip') as archive:
        saved = json.loads(archive.read('project.json'))
        assert saved['source']['path'] == 'media/original.mp4'
        assert archive.read('media/original.mp4') == source.read_bytes()
        assert b'synthetic-secret-never-backup' not in archive.read('project.json')
        assert any(name.startswith('voices/') for name in archive.namelist())
    assert source.is_file() and reference.is_file()


@pytest.mark.parametrize('include_source', [True, False])
def test_project_only_export_needs_no_previous_render_and_ignores_unrelated_corrupt_artifacts(tmp_path, media_tools, include_source):
    source = video(tmp_path/'english.mp4')
    old_mix = tone(tmp_path/'unrelated-old-mix.wav', 3)
    before = audio.file_hash(source), audio.file_hash(old_mix)
    request = payload(project(source), 'export', tmp_path, artifacts=[
        {**artifact(old_mix, 'mix'), 'sha256': 'f'*64},
        {**artifact(source, 'video'), 'sha256': 'f'*64}])
    request['options'].update(formats=['project'], include_source=include_source)
    _, _, metadata = pipeline.process(request, tmp_path/'backup', tmp_path/'data')
    record = next(item for item in metadata['artifacts'] if item['kind'] == 'project_backup')
    assert record['source_embedded'] is include_source
    with zipfile.ZipFile(record['path']) as archive:
        saved = json.loads(archive.read('project.json'))
        assert saved['backup']['source_embedded'] is include_source
        assert ('media/original.mp4' in archive.namelist()) is include_source
        assert saved['segments'][0]['text'] == 'Здравствуйте, Алиса 2026.'
    assert (audio.file_hash(source), audio.file_hash(old_mix)) == before


def test_burn_subtitles_handles_legitimate_quote_comma_and_space_paths(tmp_path, media_tools):
    available = subprocess.check_output(['ffmpeg', '-hide_banner', '-filters'], stderr=subprocess.DEVNULL, text=True)
    if ' subtitles ' not in available:
        pytest.skip('This FFmpeg build does not support the optional subtitle-burn filter.')
    folder = tmp_path / "O'Connor, [Russian]"
    folder.mkdir()
    source = video(folder/'english.mp4')
    narration = tone(folder/'russian.wav', 3)
    subtitle = pipeline.subtitles(project()['segments'], folder/'Russian.srt')
    result = audio.encode_video(source, narration, folder/'burned.mp4', {},
        {'video_codec': 'libx264', 'subtitles': 'burn'}, subtitle=subtitle)
    assert result['video_stream_copied'] is False and result['duration'] == pytest.approx(3, abs=.2)


def test_existing_whisper_adapter_preserves_source_segment_ids_and_manual_russian_edits(tmp_path, monkeypatch, media_tools):
    from forge import dubbing_models, worker
    source = video(tmp_path/'english.mp4')
    value = project(source)
    monkeypatch.setattr(dubbing_models, 'existing_whisper_state', lambda *args, **kwargs: {'ready': True, 'model_revision': 'a'*40})
    def timed_fixture(source, stage_dir, stem, options, data):
        assert options['engine'] == 'whisper' and options['timestamps'] == 'word'
        path = stage_dir/(stem+'.json')
        path.write_text(json.dumps({'segments': [{'start': .2, 'end': 1.2, 'text': 'Hello, Alice 2026.'}], 'info': {'duration': 3, 'model_revision': 'a'*40}}), encoding='utf-8')
        return [path], []
    monkeypatch.setattr(worker, 'transcribe', timed_fixture)
    _, _, metadata = pipeline.process(payload(value, 'transcribe', tmp_path), tmp_path/'transcribe', tmp_path/'data')
    update = metadata['segment_updates'][0]
    assert update['id'] == 'one' and update['text'] == value['segments'][0]['text']
    assert update['manual_edit'] is True and update['reviewed'] is True


def test_isolated_whisper_adapter_is_used_without_modifying_core_environment(tmp_path, monkeypatch, media_tools):
    from forge import dubbing_models, worker
    source = video(tmp_path/'english.mp4')
    calls = []
    monkeypatch.setattr(dubbing_models, 'existing_whisper_state', lambda *args, **kwargs: {'ready': False})
    monkeypatch.setattr(dubbing_models, 'resolve', lambda *args, **kwargs: {'revision': 'b'*40})
    monkeypatch.setattr(worker, 'transcribe', lambda *args, **kwargs: pytest.fail('The incompatible core ASR must not execute.'))
    def isolated(component, data, request, **kwargs):
        calls.append((component, request))
        return {'segments': [{'id': 'timed-one', 'start': .2, 'end': 1.2, 'speaker': 'speaker1', 'source_text': 'Hello, Alice 2026.'}],
                'duration': 3, 'model_revision': 'b'*40, 'actual_device': 'cpu'}
    monkeypatch.setattr(dubbing_models, 'invoke', isolated)
    _, _, result = pipeline.process(payload(project(source), 'transcribe', tmp_path), tmp_path/'isolated', tmp_path/'data')
    assert calls[0][0] == 'whisper-large-v3-turbo'
    assert calls[0][1]['source'] == str(source) and calls[0][1]['timestamps'] == 'word'
    assert result['project_updates']['transcription']['model_revision'] == 'b'*40


def test_missing_asr_has_gui_repair_error_without_manual_package_commands(tmp_path, monkeypatch, media_tools):
    from forge import dubbing_models
    source = video(tmp_path/'english.mp4')
    marker = tmp_path/'data'/'environments'/'old-whisper'/'preserve.txt'
    marker.parent.mkdir(parents=True); marker.write_bytes(b'Existing isolated environment')
    monkeypatch.setattr(dubbing_models, 'existing_whisper_state', lambda *args, **kwargs: {'ready': False})
    def missing(*args, **kwargs):
        raise RuntimeError('Uninstalled explicit fixture')
    monkeypatch.setattr(dubbing_models, 'resolve', missing)
    with pytest.raises(RuntimeError, match='Set Up Dubbing Studio') as failure:
        pipeline.process(payload(project(source), 'transcribe', tmp_path), tmp_path/'missing', tmp_path/'data')
    assert '.bat' not in str(failure.value) and 'pip' not in str(failure.value)
    assert marker.read_bytes() == b'Existing isolated environment'


def test_upload_preparation_is_local_private_and_never_uploads(tmp_path, media_tools):
    source = video(tmp_path/'russian.mp4')
    value = project(source)
    _, _, metadata = pipeline.process(payload(value, 'prepare_upload', tmp_path, artifacts=[artifact(source, 'video')]), tmp_path/'prepare', tmp_path/'data')
    record = json.loads(Path(metadata['artifacts'][0]['path']).read_text(encoding='utf-8'))
    assert record['upload_authorized'] is False and record['review_required'] is True
    assert record['visibility'] == 'private' and record['manual_fallback'] == 'https://studio.youtube.com/'


def test_metadata_localizes_source_fields_without_erasing_reviewed_russian_edits(tmp_path, monkeypatch):
    calls = model_fixture(monkeypatch)
    value = project()
    value['source_metadata'] = {'title': 'English title', 'description': 'English description',
        'tags': ['History'], 'hashtags': ['Science'], 'chapters': [{'start': 0, 'title': 'Introduction'}]}
    value['metadata'] = {'title': 'Мой исправленный заголовок', 'description': 'Моё описание', 'tags': ['Мои теги'], 'reviewed': True}
    _, _, result = pipeline.process(payload(value, 'metadata', tmp_path, model_revisions={'opus-en-ru': 'b'*40}), tmp_path/'metadata', tmp_path/'data')
    metadata = result['project_updates']['metadata']
    assert metadata['title'] == value['metadata']['title'] and metadata['description'] == value['metadata']['description']
    assert metadata['tags'] == ['Мои теги']
    assert metadata['translation_suggestions']['chapter_headings'][0]['start'] == 0
    assert set(metadata['translation_suggestions']) >= {'title', 'description', 'tags', 'hashtags', 'chapter_headings'}
    assert calls[0]['model_revision'] == 'b'*40 and metadata['reviewed'] is False
