"""Real Dubbing API → scheduler → subprocess → publication acceptance.

All media are owned, generated fixtures. The cached "Russian" narration is
explicitly a pair of sine waves labelled with reviewed Russian fixture text;
these tests exercise media/queue contracts and make no neural-inference claim.
No inference adapter is replaced and no model or public media is downloaded.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import time
import wave

from fastapi.testclient import TestClient
import pytest

from forge import __version__
from forge.app import create_app


TOKEN = 'synthetic-dubbing-queue-token'
PORT = 19948
STAGES = ['dubbing_align', 'dubbing_mix', 'dubbing_effects', 'dubbing_encode',
          'dubbing_metadata', 'dubbing_export', 'dubbing_prepare_upload']


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                             '-of', 'json', str(path)], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def fixture_video(path):
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
        '-f', 'lavfi', '-i', 'color=c=blue:size=160x96:rate=24',
        '-f', 'lavfi', '-i', 'sine=frequency=130:sample_rate=48000', '-t', '3',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(path)], check=True)
    return path


def fixture_tone(path, frequency):
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 24000
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(struct.pack('<' + 'h' * rate, *[
            int(5500 * math.sin(2 * math.pi * frequency * sample / rate)) for sample in range(rate)]))
    return {'path': str(path), 'size': path.stat().st_size, 'sha256': sha256(path),
            'duration': 1.0, 'sample_rate': rate, 'channels': 1,
            'fixture': 'Synthetic sine, not neural synthesis or Russian speech.'}


def stream_hash(path, selector='0:v:0', codec='copy', format='h264'):
    raw = subprocess.check_output(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
                                   '-i', str(path), '-map', selector, '-c', codec, '-f', format, '-'])
    return hashlib.sha256(raw).hexdigest()


def post(client, route, body):
    response = client.post(route, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def wait_for_jobs(app, identities, timeout=75):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = [app.state.store.job(identity) for identity in identities]
        failures = [job for job in jobs if job['status'] in {'failed', 'blocked', 'cancelled', 'interrupted'}]
        assert not failures, '\n'.join(f"{job['tool']}: {job['status']}: {job['message']}" for job in jobs)
        if all(job['status'] == 'completed' for job in jobs):
            return jobs
        time.sleep(.05)
    pytest.fail('Real worker queue did not finish: ' + json.dumps([
        {'tool': job['tool'], 'status': job['status'], 'message': job['message']}
        for job in [app.state.store.job(identity) for identity in identities]], ensure_ascii=False))


def test_real_media_queue_publishes_and_applies_complete_reviewed_dubbing_workflow(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Real FFmpeg/FFprobe are required.'
    source = fixture_video(tmp_path / 'owned-source.mp4')
    source_before = (source.stat().st_size, source.stat().st_mtime_ns, sha256(source))
    app = create_app(tmp_path / 'data', TOKEN, PORT, workers=True)
    store = app.state.store
    store.set('settings', {**store.settings(), 'automatic_update_checks': False,
        'automatic_dependency_repairs': False, 'daily_update_check': False,
        'queue_paused': False, 'concurrency_mode': 'manual', 'workers': 2})
    cache = store.data / 'dubbing' / 'cache' / 'synthetic-fixtures'
    first_audio = fixture_tone(cache / 'one.wav', 440)
    second_audio = fixture_tone(cache / 'two.wav', 660)
    untouched_audio = fixture_tone(cache / 'unrelated.wav', 880)
    cached_before = {entry['path']: (Path(entry['path']).stat().st_mtime_ns, entry['sha256'])
                     for entry in (first_audio, second_audio, untouched_audio)}
    try:
        with TestClient(app, base_url=f'http://127.0.0.1:{PORT}', headers={'X-Forge-Token': TOKEN}) as client:
            imported = post(client, '/api/dubbing/import', {'path': str(source), 'name': 'Тестовая русская версия'})['project']
            seeded = {**imported,
                'segments': [
                    {'id': 'first', 'start': .2, 'end': 1.2, 'speaker': 'speaker1',
                     'source_text': 'Owned English fixture one.', 'text': 'Первый тестовый фрагмент.',
                     'reviewed': True, 'manual_edit': True, 'audio': first_audio},
                    {'id': 'second', 'start': 1.6, 'end': 2.6, 'speaker': 'speaker1',
                     'source_text': 'Owned English fixture two.', 'text': 'Второй тестовый фрагмент.',
                     'reviewed': True, 'manual_edit': True, 'audio': second_audio}],
                'options': {**imported['options'],
                    'audio': {**imported['options']['audio'], 'original_voice_db': -96,
                              'compression': False, 'limiter': False, 'separation': 'off'},
                    'lip_sync': {**imported['options']['lip_sync'], 'mode': 'off'},
                    'export': {**imported['options']['export'], 'video_codec': 'copy',
                               'subtitles': 'sidecar', 'separate_audio': ['wav', 'flac'],
                               'bilingual': True, 'project_backup': False},
                    'publishing': {**imported['options']['publishing'],
                        'title': 'Русский тестовый выпуск', 'description': 'Проверенный тестовый текст.',
                        'tags': ['тест'], 'visibility': 'private'}}}
            # Only this trusted fixture seeds already-generated clips. Browsers
            # cannot inject derived audio paths through project autosave.
            seeded = app.state.dubbing.library.save_project(seeded, imported['revision'], trusted=True)
            plan = post(client, '/api/dubbing/plan', {'project_id': seeded['id'], 'stages': STAGES})
            started = post(client, '/api/dubbing/run', {'plan_token': plan['plan_token']})
            identities = [job['id'] for job in started['jobs']]
            assert len(identities) == len(STAGES)
            assert [job['tool'] for job in started['jobs']] == STAGES
            jobs = wait_for_jobs(app, identities)
            assert not store.get('queue_error')
            health = client.get('/health')
            assert health.status_code == 200
            assert health.json()['version'] == __version__
            response = client.get('/api/dubbing/projects/' + seeded['id'])
            assert response.status_code == 200, response.text
            project = response.json()
            assert project['revision'] > seeded['revision']
            assert [segment['text'] for segment in project['segments']] == [row['text'] for row in seeded['segments']]
            assert all(row['reviewed'] and row['manual_edit'] and row.get('timing') for row in project['segments'])
            assert project['alignment']['duration'] == pytest.approx(3, abs=.05)
            assert project['exports']['verified'] is True
            assert project['metadata']['title'] == 'Русский тестовый выпуск'
            assert project['metadata']['defaultAudioLanguage'] == 'ru'
            assert project['upload_preparation'] == {'status': 'review_required', 'visibility': 'private'}
            run = app.state.dubbing.library.run(started['run']['id'])
            assert run['status'] == 'completed'
            assert set(run['results']) == set(STAGES)
            assert all(not result['stale_segments'] for result in run['results'].values())
            artifacts = [item for item in project['artifacts'] if not item.get('stale')]
            kinds = {item['kind'] for item in artifacts}
            assert {'narration', 'alignment', 'mix', 'mastered_audio', 'video', 'russian_metadata',
                    'export_video', 'export_audio', 'russian_srt', 'russian_vtt', 'russian_transcript',
                    'bilingual_document', 'upload_review'} <= kinds
            for item in artifacts:
                path = Path(item['path'])
                assert path.is_file() and item['verified'] is True
                assert store.data / 'work' not in path.parents, item
                assert path.stat().st_size == item['size']
                assert sha256(path) == item['sha256']
            for job in jobs:
                assert job['progress'] == 100 and job['outputs']
                assert job.get('started') and job.get('finished')
                assert job['payload']['source']['path'] == str(source)
                assert job['payload']['source_stat'] == {'size': source_before[0], 'mtime_ns': source_before[1]}
                assert job['payload']['dubbing']['review_approved'] is True
                result = json.loads((store.data / 'work' / job['id'] / 'result.json').read_text('utf-8'))
                assert result['complete'] is True
                for item in result['dubbing_result']['artifacts']:
                    assert Path(item['path']).is_file()
                    assert store.data / 'work' not in Path(item['path']).parents
            by_kind = {kind: next(item for item in artifacts if item['kind'] == kind) for kind in kinds}
            # Explicit effects must remain the current mix consumed by encode,
            # rather than falling back to a previous unmastered artifact.
            mastered = run['results']['dubbing_effects']['artifacts']
            expected_mix = next(item['path'] for item in mastered if item['kind'] == 'mix')
            assert project['mix']['path'] == expected_mix
            encoded_context = jobs[STAGES.index('dubbing_encode')]['payload']['dubbing']['artifacts']
            assert any(item['kind'] == 'mix' and item['path'] == expected_mix for item in encoded_context)
            final_video = Path(by_kind['export_video']['path'])
            info = probe(final_video)
            assert float(info['format']['duration']) == pytest.approx(3, abs=.1)
            assert any(stream['codec_type'] == 'audio' for stream in info['streams'])
            assert stream_hash(source) == stream_hash(final_video), 'Copy mode must preserve the encoded source-video bitstream.'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(final_video), '-f', 'null', '-'], check=True)
            for item in artifacts:
                if item['kind'] == 'export_audio':
                    assert probe(item['path'])['streams'][0]['codec_type'] == 'audio'
            assert 'Первый тестовый фрагмент.' in Path(by_kind['russian_srt']['path']).read_text('utf-8')
            descriptor = json.loads(Path(by_kind['upload_review']['path']).read_text('utf-8'))
            assert descriptor['upload_authorized'] is False and descriptor['review_required'] is True
            assert descriptor['visibility'] == 'private'
            assert descriptor['video']['path'] == str(final_video)
            assert Path(descriptor['video']['path']).is_file()
            assert not any(job['tool'] == 'youtube_upload' for job in store.jobs())
    finally:
        app.state.manager.stop()
        store.close()
    assert (source.stat().st_size, source.stat().st_mtime_ns, sha256(source)) == source_before
    for path, (mtime, digest) in cached_before.items():
        assert Path(path).is_file() and Path(path).stat().st_mtime_ns == mtime
        assert sha256(path) == digest


def test_real_dispatch_withholds_alignment_until_explicit_script_review(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Real FFmpeg/FFprobe are required.'
    source = fixture_video(tmp_path / 'owned-review-source.mp4')
    source_before = sha256(source)
    app = create_app(tmp_path / 'data', TOKEN, PORT, workers=True)
    store = app.state.store
    store.set('settings', {**store.settings(), 'automatic_update_checks': False,
        'automatic_dependency_repairs': False, 'daily_update_check': False,
        'queue_paused': False, 'concurrency_mode': 'manual'})
    cached_audio = fixture_tone(store.data / 'dubbing' / 'cache' / 'review-fixture.wav', 440)
    try:
        with TestClient(app, base_url=f'http://127.0.0.1:{PORT}', headers={'X-Forge-Token': TOKEN}) as client:
            imported = post(client, '/api/dubbing/import', {'path': str(source)})['project']
            seeded = app.state.dubbing.library.save_project({**imported, 'segments': [
                {'id': 'needs-review', 'start': .2, 'end': 1.2, 'speaker': 'speaker1',
                 'source_text': 'Owned English fixture.', 'text': 'Текст для проверки.',
                 'reviewed': False, 'manual_edit': True, 'audio': cached_audio}]},
                imported['revision'], trusted=True)
            refused = client.post('/api/dubbing/stage', json={'project_id': seeded['id'], 'stage': 'align'})
            assert refused.status_code == 400 and 'Review' in refused.json()['detail']
            assert not store.jobs()
            plan = post(client, '/api/dubbing/plan', {'project_id': seeded['id'], 'stages': ['review', 'align']})
            started = post(client, '/api/dubbing/run', {'plan_token': plan['plan_token']})
            review_id, align_id = [job['id'] for job in started['jobs']]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                review_job = store.job(review_id)
                if review_job['message'] == 'Review and approve the Russian translation in Dubbing Studio.':
                    break
                time.sleep(.05)
            else:
                pytest.fail('The live dispatcher did not reach its explicit translation-review gate.')
            assert review_job['status'] == 'queued'
            assert store.job(align_id)['status'] == 'queued'
            assert not app.state.manager.active
            assert not (store.data / 'work' / align_id).exists()
            current = client.get('/api/dubbing/projects/' + seeded['id']).json()
            post(client, '/api/dubbing/review', {'project_id': seeded['id'],
                'run_id': started['run']['id'], 'expected_revision': current['revision'], 'approve': True})
            jobs = wait_for_jobs(app, [review_id, align_id])
            assert jobs[0]['message'] == 'Russian script reviewed; approved snapshot retained.'
            current = client.get('/api/dubbing/projects/' + seeded['id']).json()
            assert current['segments'][0]['reviewed'] is True
            assert current['segments'][0]['timing']['start'] == pytest.approx(.2)
            assert any(row['kind'] == 'narration' and Path(row['path']).is_file() for row in current['artifacts'])
    finally:
        app.state.manager.stop()
        store.close()
    assert sha256(source) == source_before
    assert sha256(cached_audio['path']) == cached_audio['sha256']
