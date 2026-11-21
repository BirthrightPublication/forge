"""Project-selected ASR setup through the real authenticated Dubbing API.

The model catalogue and hardware below are explicit availability fixtures;
these checks do not install models or claim neural/native/GPU acceptance.
"""
from __future__ import annotations

import copy

from fastapi.testclient import TestClient
import pytest

from forge.app import create_app
from forge import dubbing_models


@pytest.fixture
def setup_api(tmp_path, monkeypatch):
    calls = []
    catalogue = [
        {'id': 'opus-en-ru', 'recommended': True, 'ready': True},
        {'id': 'qwen3-tts-0.6b-base', 'recommended': True, 'ready': True},
        {'id': 'whisper-large-v3-turbo', 'recommended': False, 'ready': False},
        {'id': 'whisper-tiny', 'recommended': False, 'ready': False},
    ]
    monkeypatch.setattr(dubbing_models, 'hardware', lambda *args: {
        'ffmpeg': True, 'ffprobe': True, 'application_python_compatible': True,
        'managed_python_available': False, 'gpus': []})
    monkeypatch.setattr(dubbing_models, 'catalog', lambda *args: copy.deepcopy(catalogue))

    def existing(data, model='large-v3-turbo'):
        calls.append(model)
        return {'ready': False, 'dependencies_ready': False, 'model_cached': False,
                'installed_models': [], 'model_revision': ''}

    monkeypatch.setattr(dubbing_models, 'existing_whisper_state', existing)
    app = create_app(tmp_path / 'data', 'selected-asr-api-fixture', 19961, workers=False)
    tiny = app.state.dubbing.library.save_project({
        'name': 'Saved Tiny project', 'source': {'path': str(tmp_path / 'unprocessed-fixture.mp4')},
        'options': {'asr': {'model': 'tiny'}}}, trusted=True)
    default = app.state.dubbing.library.save_project({
        'name': 'Saved default project', 'source': {'path': str(tmp_path / 'other-unprocessed-fixture.mp4')}},
        trusted=True)
    with TestClient(app, base_url='http://127.0.0.1:19961',
                    headers={'x-forge-token': 'selected-asr-api-fixture'}) as client:
        yield client, app, tiny, default, catalogue, calls


@pytest.mark.parametrize('tiny_ready', [False, True])
def test_saved_tiny_project_setup_uses_its_asr_instead_of_global_default(setup_api, tiny_ready):
    client, app, tiny, _, catalogue, calls = setup_api
    next(row for row in catalogue if row['id'] == 'whisper-tiny')['ready'] = tiny_ready
    before = copy.deepcopy(app.state.dubbing.library.project(tiny['id']))
    response = client.get('/api/dubbing/state', params={'project_id': tiny['id']})
    assert response.status_code == 200, response.text
    state = response.json()['setup']
    assert state['speech_recognition']['model'] == 'tiny'
    assert state['speech_recognition']['ready'] is tiny_ready
    assert 'whisper-large-v3-turbo' not in state['recommended_components']
    assert state['missing_recommended'] == ([] if tiny_ready else ['whisper-tiny'])
    assert ('whisper-tiny' in state['recommended_components']) is (not tiny_ready)
    assert calls == ['tiny']
    assert app.state.dubbing.library.project(tiny['id']) == before
    assert app.state.store.jobs() == []


def test_default_setup_remains_turbo_before_and_after_selecting_tiny(setup_api):
    client, app, tiny, default, _, calls = setup_api
    before = client.get('/api/dubbing/state')
    selected = client.get('/api/dubbing/state', params={'project_id': tiny['id']})
    after = client.get('/api/dubbing/state')
    blank = client.get('/api/dubbing/state', params={'project_id': ''})
    saved_default = client.get('/api/dubbing/state', params={'project_id': default['id']})
    assert all(response.status_code == 200 for response in (before, selected, after, blank, saved_default))
    assert before.json()['setup'] == after.json()['setup'] == blank.json()['setup'] == saved_default.json()['setup']
    assert after.json()['setup']['speech_recognition']['model'] == 'large-v3-turbo'
    assert after.json()['setup']['missing_recommended'] == ['whisper-large-v3-turbo']
    assert selected.json()['setup']['missing_recommended'] == ['whisper-tiny']
    assert calls == ['large-v3-turbo', 'tiny', 'large-v3-turbo', 'large-v3-turbo', 'large-v3-turbo']
    assert app.state.store.jobs() == []


@pytest.mark.parametrize('identity,detail', [
    ('missing-saved-project', 'Dubbing project not found'),
    ('../not-a-local-project', 'saved local dubbing identity'),
    ('x' * 81, 'saved local dubbing identity'),
])
def test_selected_setup_rejects_missing_or_invalid_projects_before_model_discovery(setup_api, identity, detail):
    client, app, _, _, _, calls = setup_api
    response = client.get('/api/dubbing/state', params={'project_id': identity})
    assert response.status_code == 400
    assert detail in response.json()['detail']
    assert calls == []
    assert app.state.store.jobs() == []
