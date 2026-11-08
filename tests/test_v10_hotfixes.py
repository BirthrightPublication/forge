"""V0.10 repairs with authenticated requests, real media and real worker exits.

Synthesis is an explicitly labelled deterministic tone fixture. These tests do
not claim neural inference; FFmpeg assembly and the worker collector are real.
"""
import copy
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import wave

import pytest
from fastapi.testclient import TestClient

from forge.app import create_app
from forge import speech_books
from forge.core import Store
from forge.queue import QueueManager, worker_failure


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / 'data', 'synthetic-v10-hotfix-token', 19821, workers=False)
    with TestClient(app, base_url='http://127.0.0.1:19821',
                    headers={'X-Forge-Token': 'synthetic-v10-hotfix-token'}) as client:
        yield client, app
    app.state.store.close()


@pytest.fixture
def tone_engine(monkeypatch):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Real FFmpeg and FFprobe are required for assembly acceptance.')
    from forge import speech_engines
    calls = []
    monkeypatch.setattr(speech_engines, 'resolve_options',
                        lambda options, data: {**options, 'model_revision': 'a' * 40})
    def synthesize(text, options, data, output, progress):
        calls.append(text)
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, 24000, 2400, 'NONE', 'not compressed'))
            stream.writeframes(b''.join(struct.pack('<h', round(3000 * math.sin(n * .1))) for n in range(2400)))
        return {'engine': 'synthetic-tone-fixture', 'model_revision': 'a' * 40,
                'sample_rate': 24000, 'neural_inference_tested': False}
    monkeypatch.setattr(speech_engines, 'synthesize', synthesize)
    return calls


def save_book(client):
    response = client.post('/api/speech/projects', json={
        'name': 'Immutable synthetic book',
        'chapters': [{'id': 'one', 'title': 'First', 'text': 'First chapter.'},
                     {'id': 'two', 'title': 'Second', 'text': 'Second chapter.'}],
        'options': {'engine': 'kokoro', 'voice': 'am_onyx', 'text': 'Stale options text.',
                    'chapter_id': 'one', 'chapter_ids': ['one']}})
    assert response.status_code == 200, response.text
    return response.json()['project']


@pytest.mark.parametrize('scope,text,chapter_id,chapter_ids,expected_ids,expected_texts', [
    ('document', None, 'one', None, ['one', 'two'], ['First chapter.', 'Second chapter.']),
    ('document', 'First chapter.\n\nSecond chapter.', 'one', None,
     ['one', 'two'], ['First chapter.', 'Second chapter.']),
    ('chapter', 'Edited second chapter.', 'two', None, ['two'], ['Edited second chapter.']),
    ('chapters', None, 'one', ['two'], ['two'], ['Second chapter.']),
    ('selection', 'Selected speech.', 'two', None, ['selection'], ['Selected speech.']),
    ('preview', 'Preview speech.', 'one', None, ['preview'], ['Preview speech.']),
])
def test_scope_contract_renders_nonempty_text_and_keeps_manuscript(api, tmp_path, tone_engine,
        scope, text, chapter_id, chapter_ids, expected_ids, expected_texts):
    client, app = api
    book = save_book(client)
    before = copy.deepcopy(book)
    body = {'project_id': book['id'], 'scope': scope, 'chapter_id': chapter_id}
    if text is not None:
        body['text'] = text
    if chapter_ids is not None:
        body['chapter_ids'] = chapter_ids
    route = '/api/speech/preview' if scope == 'preview' else '/api/speech/generate'
    response = client.post(route, json=body)
    assert response.status_code == 200, response.text
    payload = app.state.store.job(response.json()['job']['id'])['payload']
    stage = tmp_path / 'render'
    stage.mkdir()
    files, _ = speech_books.process(payload, stage, app.state.store.data)
    report = json.loads(next(path for path in files if path.suffix == '.json').read_text(encoding='utf-8'))
    assert [chapter['id'] for chapter in report['chapters']] == expected_ids
    assert tone_engine == expected_texts
    with wave.open(str(next(path for path in files if path.suffix == '.wav')), 'rb') as rendered:
        assert rendered.getnframes() > 0
    assert client.get('/api/speech/projects/' + book['id']).json() == before


def test_export_uses_all_project_chapters_and_only_cached_audio(api, tmp_path, tone_engine):
    client, app = api
    book = save_book(client)
    generated = client.post('/api/speech/generate', json={'project_id': book['id'], 'scope': 'document'})
    first = tmp_path / 'first'
    first.mkdir()
    speech_books.process(app.state.store.job(generated.json()['job']['id'])['payload'], first, app.state.store.data)
    assert tone_engine == ['First chapter.', 'Second chapter.']
    # Older frontends attached the current chapter and its text even to Export.
    exported = client.post('/api/speech/generate', json={'project_id': book['id'], 'scope': 'export',
                                'text': 'First chapter.', 'chapter_id': 'one'})
    assert exported.status_code == 200, exported.text
    payload = app.state.store.job(exported.json()['job']['id'])['payload']
    assert payload['options']['assemble_only'] is True
    second = tmp_path / 'second'
    second.mkdir()
    files, _ = speech_books.process(payload, second, app.state.store.data)
    report = json.loads(next(path for path in files if path.suffix == '.json').read_text(encoding='utf-8'))
    assert [chapter['id'] for chapter in report['chapters']] == ['one', 'two']
    assert all(segment['reused'] for segment in report['segments'])
    assert tone_engine == ['First chapter.', 'Second chapter.']


def test_fragment_does_not_apply_whole_manuscript_range_offsets(tmp_path, tone_engine):
    project = {'chapters': [{'id': 'selection', 'text': 'Original long manuscript.'}],
               'segment_processing': [{'chapter_id': 'selection', 'start': 100, 'end': 200,
                                       'chain': [{'type': 'gain', 'params': {'db': -6}}]}]}
    stage = tmp_path / 'render'
    stage.mkdir()
    speech_books.process({'speech_project': project, 'options': {'engine': 'kokoro', 'scope': 'selection',
                         'chapter_id': 'selection', 'text': 'Short excerpt.'}, 'settings': {}},
                         stage, tmp_path / 'data')
    assert tone_engine == ['Short excerpt.']


@pytest.mark.parametrize('body', [
    {'scope': 'chapters', 'chapter_ids': []},
    {'scope': 'chapters', 'chapter_ids': ['missing']},
    {'scope': 'chapter', 'chapter_id': 'missing'},
    {'scope': 'selection', 'text': ''},
])
def test_invalid_scope_is_rejected_before_creating_an_executable_job(api, body):
    client, app = api
    book = save_book(client)
    response = client.post('/api/speech/generate', json={'project_id': book['id'], **body})
    assert response.status_code == 400, response.text
    assert app.state.store.jobs() == []


@pytest.mark.parametrize('message,diagnostic,code,category', [
    ('ModuleNotFoundError: No module named ctranslate2', ['Unrelated UserWarning\n'], 1, 'transcription_dependencies'),
    ('RuntimeError: Library cublas64_12.dll cannot be loaded', ['Unrelated UserWarning\n'], 1, 'cuda_runtime'),
    ('av.error.InvalidDataError: Invalid data found when processing input', ['Unrelated UserWarning\n'], 1, 'source_decode'),
    ('', ['TypeError: unexpected metadata_errors keyword\n', 'UserWarning: unrelated deprecation\n'], 1, 'transcription_dependencies'),
    ('', ['UserWarning: unrelated deprecation\n'], -1073741819, 'worker_error'),
])
def test_transcription_error_categories_preserve_the_meaningful_failure(message, diagnostic, code, category):
    result = worker_failure({'tool': 'transcribe'}, message, diagnostic, code)
    assert result['error_code'] == category
    if message:
        assert result['message'] == message
    else:
        assert 'Worker stopped (exit' in result['message']
        if abs(code) > 255:
            assert '0xC0000005' in result['message']
        else:
            assert 'metadata_errors' in result['message']
    assert 'UserWarning' not in result['message']
    assert 'UserWarning' in result['diagnostic']


def test_real_worker_explicit_error_is_not_replaced_by_later_library_warning(tmp_path):
    store = Store(tmp_path / 'data')
    manager = QueueManager(store)
    job = store.add_job('transcribe', 'Synthetic failed worker',
                        {'tool': 'transcribe', 'options': {}, 'destination': str(tmp_path / 'outputs')})
    store.update_job(job['id'], status='running')
    script = "import json,sys;print(json.dumps({'kind':'error','message':'Source decoding failed precisely.','error_code':'source_decode','error_retryable':False}),flush=True);print('UserWarning: unrelated library warning',file=sys.stderr,flush=True);sys.exit(1)"
    process = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding='utf-8')
    manager.active[job['id']] = process
    try:
        manager.collect(job, process)
        actual = store.job(job['id'])
        assert actual['status'] == 'failed'
        assert actual['message'] == 'Source decoding failed precisely.'
        assert actual['error_code'] == 'source_decode'
    finally:
        store.close()


@pytest.mark.parametrize('colored', [False, True])
def test_real_worker_exception_survives_a_large_later_warning_burst(tmp_path, colored):
    store = Store(tmp_path / 'data')
    manager = QueueManager(store)
    job = store.add_job('transcribe', 'Synthetic unstructured failed worker',
                        {'tool': 'transcribe', 'options': {}, 'destination': str(tmp_path / 'outputs')})
    store.update_job(job['id'], status='running')
    cause = 'TypeError: PyAV metadata_errors is incompatible'
    if colored:
        cause = '\x1b[31m' + cause + '\x1b[0m'
    script = 'import sys;print(' + repr(cause) + ",file=sys.stderr);[print('UserWarning: unrelated library warning '+str(i),file=sys.stderr) for i in range(90)];sys.exit(1)"
    process = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding='utf-8')
    manager.active[job['id']] = process
    try:
        manager.collect(job, process)
        actual = store.job(job['id'])
        assert actual['status'] == 'failed'
        assert 'metadata_errors' in actual['message'] and 'UserWarning' not in actual['message']
        assert actual['error_code'] == 'transcription_dependencies'
        assert any('UserWarning' in warning for warning in actual['warnings'])
    finally:
        store.close()
