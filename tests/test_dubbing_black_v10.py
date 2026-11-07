"""Dubbing's actual Black Scripting API/queue binding and media integration.

Owned generated video and sine-wave cache clips are explicit media fixtures,
not Russian speech, neural inference, voice similarity, or public downloads.
The established Black backend and worker subprocesses remain unmocked.
"""
from __future__ import annotations

from array import array
from contextlib import contextmanager
import copy
import json
import math
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from forge.app import create_app
from forge.dubbing_store import stage_fingerprint
from test_dubbing_queue_v10 import fixture_tone, fixture_video, post, sha256, stream_hash, wait_for_jobs


TOKEN = 'synthetic-dubbing-black-token'
PORT = 19949


@contextmanager
def backend(tmp_path, *, workers=False, reviewed=True):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Real FFmpeg/FFprobe are required.'
    source = fixture_video(tmp_path / 'owned-black-source.mp4')
    app = create_app(tmp_path / 'data', TOKEN, PORT, workers=workers)
    store = app.state.store
    store.set('settings', {**store.settings(), 'automatic_update_checks': False,
        'automatic_dependency_repairs': False, 'daily_update_check': False,
        'queue_paused': False, 'concurrency_mode': 'manual', 'workers': 2})
    audio = fixture_tone(store.data / 'dubbing' / 'cache' / 'synthetic-black.wav', 440)
    try:
        with TestClient(app, base_url=f'http://127.0.0.1:{PORT}', headers={'X-Forge-Token': TOKEN}) as client:
            imported = post(client, '/api/dubbing/import', {'path': str(source), 'name': 'Русский выпуск'})['project']
            saved = app.state.dubbing.library.save_project({**imported, 'segments': [
                {'id': 'owned-segment', 'start': .2, 'end': 1.2, 'speaker': 'speaker1',
                 'source_text': 'Owned English fixture.', 'text': 'Проверенный русский текст.',
                 'reviewed': reviewed, 'manual_edit': True, 'audio': audio}],
                'options': {**imported['options'],
                    'audio': {**imported['options']['audio'], 'original_voice_db': -96,
                              'compression': False, 'limiter': False},
                    'export': {**imported['options']['export'], 'video_codec': 'copy',
                               'project_backup': False, 'subtitles': 'sidecar'},
                    'publishing': {**imported['options']['publishing'], 'title': 'Тестовый русский выпуск',
                                   'description': 'Тестовый текст для проверки.'}}}, imported['revision'], trusted=True)
            yield app, client, saved, source, audio
    finally:
        app.state.manager.stop()
        store.close()


def graph(project, *tools, directory=None):
    nodes = []
    for index, value in enumerate(tools):
        tool, options = value if isinstance(value, tuple) else (value, {})
        if tool.startswith('dubbing_'):
            options = {'project_id': project['id'], **options}
        nodes.append({'id': f'operation-{index + 1}', 'tool': tool, 'options': options,
                      'enabled': True, 'preset_name': ''})
    return {'schema_version': 1, 'id': 'dubbing-black-fixture', 'name': 'Русский линейный выпуск',
            'nodes': nodes, 'connections': [{'source': a['id'], 'target': b['id']}
                for a, b in zip(nodes, nodes[1:])],
            'output': {'mode': 'custom', 'directory': str(directory)} if directory else {'mode': 'source'}}


def preview(client, project, script):
    body = {'script': script, 'sources': [project['source']['id']]}
    result = post(client, '/api/black-scripting/preview', body)
    return {**body, 'preview_token': result['preview_token']}, result


@pytest.mark.parametrize('change', ['text', 'audio-options', 'export-options', 'node-options', 'source'])
def test_black_preview_token_binds_saved_dubbing_project_and_options(tmp_path, change):
    with backend(tmp_path) as (app, client, project, source, _):
        script = graph(project, 'dubbing_align', 'dubbing_mix')
        body, plan = preview(client, project, script)
        assert plan['jobs'] == 2
        assert [row['tool'] for row in plan['sources'][0]['steps']] == ['dubbing_align', 'dubbing_mix']
        assert not app.state.store.jobs()
        if change == 'node-options':
            body['script']['nodes'][0]['options']['segment_ids'] = ['owned-segment']
        elif change == 'source':
            source.write_bytes(source.read_bytes() + b'changed synthetic source')
        else:
            changed = copy.deepcopy(project)
            if change == 'text':
                changed['segments'][0]['text'] = 'Изменённый текст.'
            elif change == 'audio-options':
                changed['options']['audio']['narration_db'] = -3
            else:
                changed['options']['export']['container'] = 'mkv'
            post(client, '/api/dubbing/projects', {'project': changed, 'expected_revision': project['revision']})
        refused = client.post('/api/black-scripting/run', json=body)
        assert refused.status_code in {400, 409}, refused.text
        assert 'Preview' in refused.text or 'source' in refused.text.lower() or 'original' in refused.text.lower()
        assert not app.state.store.jobs()


def test_black_rejects_different_project_source_and_unrelated_media_operations(tmp_path):
    with backend(tmp_path) as (app, client, project, _, _):
        alternate = fixture_video(tmp_path / 'other-owned-source.mp4')
        other = post(client, '/api/dubbing/import', {'path': str(alternate)})['project']
        cases = [
            {'script': graph(project, 'dubbing_align'), 'sources': [other['source']['id']]},
            {'script': graph(project, 'dubbing_align', ('dubbing_mix', {'project_id': other['id']})),
             'sources': [project['source']['id']]},
            {'script': graph(project, 'dubbing_align', 'metadata'), 'sources': [project['source']['id']]},
            {'script': graph(project, 'dubbing_align'), 'sources': [project['source']['id'], other['source']['id']]},
        ]
        for body in cases:
            response = client.post('/api/black-scripting/preview', json=body)
            assert response.status_code == 400, response.text
        assert not app.state.store.jobs()


def test_black_run_freezes_project_and_effect_options_without_resolving_changed_presets(tmp_path):
    with backend(tmp_path) as (app, client, project, _, _):
        chain = [{'type': 'gain', 'params': {'db': -6}}]
        script = graph(project, 'dubbing_align', 'dubbing_mix',
                       ('audio_effects', {'chain': chain, 'format': 'wav'}), 'dubbing_encode',
                       directory=tmp_path / 'black-export')
        script['nodes'][2]['preset_name'] = 'Saved gain'
        body, _ = preview(client, project, script)
        started = post(client, '/api/black-scripting/run', body)
        identities = [job['id'] for job in started['jobs']]
        jobs = [app.state.store.job(identity) for identity in identities]
        assert [job['tool'] for job in jobs] == ['dubbing_align', 'dubbing_mix', 'dubbing_effects', 'dubbing_encode']
        assert jobs[2]['payload']['options']['effects'] == chain
        assert jobs[2]['payload']['depends_on'] == identities[1]
        assert started['batch_id'] == started['run']['id']
        snapshots = [copy.deepcopy(job['payload']['dubbing']['project']) for job in jobs]
        assert all(snapshot['segments'][0]['text'] == 'Проверенный русский текст.' for snapshot in snapshots)
        assert all(snapshot['options']['export']['directory'] == str(tmp_path / 'black-export') for snapshot in snapshots)
        # Saved tool presets are unrelated to the graph's frozen chain.
        post(client, '/api/presets', {'tool': 'audio_effects', 'name': 'Saved gain',
             'options': {'chain': [{'type': 'gain', 'params': {'db': 12}}], 'format': 'wav'},
             'output': {'mode': 'source'}})
        current = app.state.dubbing.library.project(project['id'])
        current['options']['audio']['narration_db'] = -9
        post(client, '/api/dubbing/projects', {'project': current, 'expected_revision': current['revision']})
        assert [app.state.store.job(identity)['payload']['dubbing']['project'] for identity in identities] == snapshots
        assert app.state.store.job(identities[2])['payload']['options']['effects'] == chain
        assert all(job['status'] == 'queued' for job in app.state.store.jobs())
        # Dispatch may refresh derived segment/artifact state, but must either
        # withhold the changed run or retain its approved configuration.
        app.state.dubbing.prepare_job(app.state.store.job(identities[0]))
        dispatched = app.state.store.job(identities[0])['payload']['dubbing']['project']
        assert dispatched['options'] == snapshots[0]['options'], 'Dispatch silently replaced the approved Black project options.'
        # A metadata-only fixture represents an intermediate completion racing
        # with that later UI edit. It does not claim rendered audio/inference.
        app.state.dubbing.library.apply_result(app.state.store.job(identities[0]), {
            'dubbing_result': {'stage_settings_hash': stage_fingerprint(snapshots[0], 'dubbing_align'),
                'project_updates': {'alignment': {'fixture': 'Parent state race only; no media result.'}}}})
        run = app.state.dubbing.library.run(started['run']['id'])
        assert run['snapshot']['options'] == snapshots[0]['options'], 'Intermediate completion replaced the frozen run options with a later UI edit.'


def test_black_selected_translation_preserves_explicit_node_context_at_dispatch(tmp_path, monkeypatch):
    with backend(tmp_path) as (app, client, project, _, _):
        # This is a controller-readiness fixture only. Workers are disabled and
        # no translation model is downloaded, invoked, or represented as tested.
        catalogue = SimpleNamespace(catalog=lambda data: [
            {'id': 'opus-en-ru', 'ready': True, 'model_revision': 'a' * 40}],
            existing_whisper_state=lambda *args: {'ready': False})
        monkeypatch.setattr(app.state.dubbing, 'models', lambda: catalogue)
        project = app.state.dubbing.library.save_project({**project, 'segments': [
            *project['segments'], {**project['segments'][0], 'id': 'unselected-segment',
                'start': 1.6, 'end': 2.6, 'source_text': 'Keep this English source.',
                'text': 'Этот текст надо сохранить.'}]}, project['revision'], trusted=True)
        script = graph(project, ('dubbing_translate', {
            'segment_ids': ['owned-segment'], 'replace_manual_text': True}))
        body, _ = preview(client, project, script)
        started = post(client, '/api/black-scripting/run', body)
        job = app.state.store.job(started['jobs'][0]['id'])
        assert app.state.dubbing.prepare_job(job) is True
        context = app.state.store.job(job['id'])['payload']['dubbing']
        assert context['segment_ids'] == ['owned-segment']
        assert context['replace_manual_text'] is True
        assert context['model_revisions']['opus-en-ru'] == 'a' * 40
        assert [row['id'] for row in context['project']['segments']] == ['owned-segment', 'unselected-segment']
        assert context['project']['segments'][1]['text'] == 'Этот текст надо сохранить.'
        assert app.state.store.job(job['id'])['status'] == 'queued'


@pytest.mark.parametrize('selection', [['unknown-segment'], ['owned-segment', 'owned-segment']])
def test_black_rejects_invalid_node_selection_before_creating_any_job(tmp_path, selection):
    with backend(tmp_path) as (app, client, project, _, _):
        script = graph(project, ('dubbing_translate', {
            'segment_ids': selection, 'replace_manual_text': True}))
        body = {'script': script, 'sources': [project['source']['id']]}
        response = client.post('/api/black-scripting/preview', json=body)
        if response.status_code == 200:
            response = client.post('/api/black-scripting/run', json={
                **body, 'preview_token': response.json()['preview_token']})
        assert response.status_code in {400, 422}, response.text
        assert 'segment' in response.text.lower()
        assert not app.state.store.jobs()


def rms(path):
    raw = subprocess.check_output(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-ss', '0.3', '-t', '0.6', '-i', str(path), '-map', '0:a:0', '-f', 'f32le', '-ac', '1', '-ar', '8000', '-'])
    values = array('f')
    values.frombytes(raw)
    return math.sqrt(sum(value * value for value in values) / len(values))


def test_real_black_dubbing_graph_waits_for_review_and_renders_existing_effect_node(tmp_path):
    with backend(tmp_path, workers=True, reviewed=False) as (app, client, project, source, cached_audio):
        source_before, cache_before = sha256(source), sha256(cached_audio['path'])
        chain = [{'type': 'gain', 'params': {'db': -6}}]
        script = graph(project, 'dubbing_review', 'dubbing_align', 'dubbing_mix',
                       ('audio_effects', {'chain': chain, 'format': 'wav'}), 'dubbing_encode',
                       'dubbing_metadata', 'dubbing_export', 'dubbing_prepare_upload',
                       directory=tmp_path / 'finished-black-export')
        body, plan = preview(client, project, script)
        assert plan['jobs'] == 8
        started = post(client, '/api/black-scripting/run', body)
        identities = [job['id'] for job in started['jobs']]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if app.state.store.job(identities[0])['message'] == 'Review and approve the Russian translation in Dubbing Studio.':
                break
            time.sleep(.05)
        else:
            pytest.fail('The real Black dispatcher did not reach its Dubbing review gate.')
        assert all(job['status'] == 'queued' for job in app.state.store.jobs())
        assert not app.state.manager.active
        assert not (app.state.store.data / 'work' / identities[1]).exists()
        current = client.get('/api/dubbing/projects/' + project['id']).json()
        post(client, '/api/dubbing/review', {'project_id': project['id'], 'run_id': started['run']['id'],
             'expected_revision': current['revision'], 'approve': True})
        jobs = wait_for_jobs(app, identities)
        expected = ['dubbing_review', 'dubbing_align', 'dubbing_mix', 'dubbing_effects',
                    'dubbing_encode', 'dubbing_metadata', 'dubbing_export', 'dubbing_prepare_upload']
        assert [job['tool'] for job in jobs] == expected
        assert all(job['payload']['depends_on'] == identities[index - 1] for index, job in enumerate(jobs) if index)
        assert all(job['payload']['source']['path'] == str(source) for job in jobs)
        run = app.state.dubbing.library.run(started['run']['id'])
        assert run['status'] == 'completed'
        assert run['stage_options']['dubbing_effects']['effects'] == chain
        original_mix = next(item for item in run['results']['dubbing_mix']['artifacts'] if item['kind'] == 'mix')
        mastered = next(item for item in run['results']['dubbing_effects']['artifacts'] if item['kind'] == 'mix')
        assert rms(mastered['path']) / rms(original_mix['path']) == pytest.approx(10 ** (-6 / 20), rel=.02)
        encoded_context = jobs[4]['payload']['dubbing']['artifacts']
        assert any(item['kind'] == 'mix' and item['path'] == mastered['path'] for item in encoded_context)
        current = client.get('/api/dubbing/projects/' + project['id']).json()
        final_video = next(item for item in current['artifacts'] if item['kind'] == 'export_video' and not item.get('stale'))
        assert Path(final_video['path']).parent == tmp_path / 'finished-black-export'
        assert stream_hash(source) == stream_hash(final_video['path'])
        # AAC is lossy, but must contain the attenuated master rather than the
        # old mix; a 5% tolerance covers codec delay/filtering of this fixture.
        assert rms(final_video['path']) / rms(original_mix['path']) == pytest.approx(10 ** (-6 / 20), rel=.05)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', final_video['path'], '-f', 'null', '-'], check=True)
        upload = next(item for item in current['artifacts'] if item['kind'] == 'upload_review')
        assert json.loads(Path(upload['path']).read_text('utf-8'))['upload_authorized'] is False
        assert sha256(source) == source_before
        assert sha256(cached_audio['path']) == cache_before
