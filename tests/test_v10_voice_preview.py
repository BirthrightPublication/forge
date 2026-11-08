"""Voice auditions must use the requested recipe without changing a manuscript.

Synthesis is an explicitly marked tone fixture. Calls cross the real authenticated
API and render through actual FFmpeg assembly; no neural inference is claimed.
"""
import copy
import json
from pathlib import Path
import shutil
import struct
import subprocess
import wave

from fastapi.testclient import TestClient
import pytest

from forge.app import create_app
from forge import speech_books, speech_engines


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / 'data', 'synthetic-voice-preview-token', 19841, workers=False)
    with TestClient(app, base_url='http://127.0.0.1:19841',
                    headers={'X-Forge-Token': 'synthetic-voice-preview-token'}) as client:
        yield client, app
    app.state.store.close()


@pytest.fixture
def captured_engine(monkeypatch):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual media assembly prerequisites are required.'
    calls = []
    monkeypatch.setattr(speech_engines, 'resolve_options', lambda options, data: {**options, 'model_revision': options.get('model_revision') or 'a' * 40})
    def synthesize(text, options, data, output, progress):
        calls.append((text, copy.deepcopy(options)))
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, 24000, 2400, 'NONE', 'not compressed'))
            stream.writeframes(struct.pack('<2400h', *([1000, -1000] * 1200)))
        return {'engine': options.get('engine'), 'model_revision': options['model_revision'],
                'sample_rate': 24000, 'neural_inference_tested': False}
    monkeypatch.setattr(speech_engines, 'synthesize', synthesize)
    return calls


def book(client, *, base_voice='af_bella', narrator_voice='am_onyx', narrator_blend=None):
    narrator = {'engine': 'kokoro', 'voice': narrator_voice, 'language': 'en'}
    if narrator_blend:
        narrator['blend'] = narrator_blend
    response = client.post('/api/speech/projects', json={
        'name': 'Synthetic audition contract',
        'chapters': [{'id': 'one', 'title': 'First', 'text': 'Narrator line. [[speaker:character]]Character line.'},
                     {'id': 'two', 'title': 'Second', 'text': 'Second narration.'}],
        'options': {'engine': 'kokoro', 'voice': base_voice},
        'speakers': {'narrator': narrator, 'character': {'engine': 'kokoro', 'voice': 'am_adam', 'language': 'en'}}})
    assert response.status_code == 200, response.text
    return response.json()['project']


def render(client, app, tmp_path, route, body):
    response = client.post(route, json=body)
    assert response.status_code == 200, response.text
    payload = app.state.store.job(response.json()['job']['id'])['payload']
    stage = tmp_path / 'render'
    stage.mkdir()
    files, _ = speech_books.process(payload, stage, app.state.store.data)
    with wave.open(str(next(path for path in files if path.suffix == '.wav')), 'rb') as audio:
        assert audio.getnframes() > 0
    return payload


def test_explicit_preview_voice_is_not_replaced_by_saved_narrator(api, tmp_path, captured_engine):
    client, app = api
    project = book(client)
    before = copy.deepcopy(project)
    render(client, app, tmp_path, '/api/speech/preview', {'project_id': project['id'],
        'text': 'Audition this selected voice.',
        'options': {'engine': 'kokoro', 'voice': 'af_bella', 'speakers': project['speakers']}})
    assert [options['voice'] for _, options in captured_engine] == ['af_bella']
    assert client.get('/api/speech/projects/' + project['id']).json() == before


def test_blend_preview_keeps_requested_recipe_instead_of_narrator_recipe(api, tmp_path, captured_engine):
    client, app = api
    project = book(client, narrator_blend=[{'voice': 'am_adam', 'weight': 1}])
    recipe = [{'voice': 'am_onyx', 'weight': .25}, {'voice': 'af_bella', 'weight': .75}]
    render(client, app, tmp_path, '/api/speech/preview', {'project_id': project['id'], 'text': 'Audition this blend.',
        'options': {'engine': 'kokoro', 'voice': 'am_onyx', 'blend': recipe}})
    assert captured_engine[0][1]['voice'] == 'am_onyx'
    assert captured_engine[0][1]['blend'] == recipe


@pytest.mark.parametrize('voice', ['af_bella', 'am_onyx'])
def test_explicit_original_voice_does_not_inherit_a_saved_project_blend(api, tmp_path, captured_engine, voice):
    client, app = api
    project = book(client)
    project['options']['blend'] = [{'voice': 'am_adam', 'weight': 1}]
    response = client.post('/api/speech/projects', json={'project': project})
    assert response.status_code == 200, response.text
    project = response.json()['project']
    render(client, app, tmp_path, '/api/speech/preview', {'project_id': project['id'],
        'text': 'Audition the original voice.', 'options': {'engine': 'kokoro', 'voice': voice}})
    assert captured_engine[0][1]['voice'] == voice
    assert captured_engine[0][1]['blend'] == []
    assert client.get('/api/speech/projects/' + project['id']).json()['options']['blend'] == [{'voice': 'am_adam', 'weight': 1}]


def test_custom_profile_preview_uses_expanded_immutable_voice_recipe(api, tmp_path, captured_engine):
    client, app = api
    project = book(client)
    recipe = [{'voice': 'af_bella', 'weight': .7}, {'voice': 'am_onyx', 'weight': .3}]
    response = client.post('/api/speech/records/voices', json={'name': 'Synthetic saved blend', 'engine': 'kokoro',
        'voice': 'af_bella', 'language': 'en', 'blend': recipe, 'model_revision': 'b' * 40})
    assert response.status_code == 200, response.text
    profile = response.json()['record']
    payload = render(client, app, tmp_path, '/api/speech/preview', {'project_id': project['id'],
        'text': 'Audition the saved voice profile.', 'options': {'engine': 'kokoro', 'voice': profile['id'], 'blend': []}})
    assert captured_engine[0][1]['voice'] == 'af_bella'
    assert captured_engine[0][1]['blend'] == recipe
    assert captured_engine[0][1]['model_revision'] == 'b' * 40
    assert captured_engine[0][1]['voice_revision'] == profile['revision']
    assert payload['speech_profile']['id'] == profile['id']


@pytest.mark.parametrize('scope', ['document', 'chapter'])
def test_production_speaker_assignments_still_override_base_voice(api, tmp_path, captured_engine, scope):
    client, app = api
    project = book(client)
    render(client, app, tmp_path, '/api/speech/generate', {'project_id': project['id'], 'scope': scope,
        **({'chapter_id': 'one'} if scope == 'chapter' else {}),
        'options': {'engine': 'kokoro', 'voice': 'af_bella'}})
    assert [(text, options['voice']) for text, options in captured_engine][:2] == [
        ('Narrator line.', 'am_onyx'), ('Character line.', 'am_adam')]


def test_preview_without_explicit_voice_retains_project_narrator_assignment(api, tmp_path, captured_engine):
    client, app = api
    project = book(client, base_voice='af_bella', narrator_voice='am_onyx')
    render(client, app, tmp_path, '/api/speech/preview', {'project_id': project['id'], 'text': 'Preview the assigned narrator.'})
    assert captured_engine[0][1]['voice'] == 'am_onyx'


def test_explicit_preview_keeps_other_named_speaker_assignments(api, tmp_path, captured_engine):
    client, app = api
    project = book(client)
    render(client, app, tmp_path, '/api/speech/preview', {'project_id': project['id'],
        'text': 'Audition. [[speaker:character]]Character.', 'options': {'engine': 'kokoro', 'voice': 'af_bella'}})
    assert [(text, options['voice']) for text, options in captured_engine] == [
        ('Audition.', 'af_bella'), ('Character.', 'am_adam')]


def test_voice_card_request_clears_old_blend_without_mutating_project(tmp_path):
    assert shutil.which('node'), 'The executable JavaScript contract requires Node.'
    source = (Path(__file__).resolve().parents[1] / 'web' / 'speech_v8.js').read_text(encoding='utf-8')
    start = source.index('async function generate(scope,voiceOverride)')
    end = source.index('\n function queueResponse', start)
    function = source[start:end]
    script = '''
const vm=require('node:vm');
const source=JSON.parse(process.argv[1]);
const project={id:'fixture-project',options:{engine:'kokoro',voice:'am_onyx',blend:[{voice:'am_adam',weight:1}]},speakers:{narrator:{voice:'am_onyx'}},chapters:[{id:'one',text:'Original'}]};
let sent;
const context={structuredClone,project,selectedText:'',selectionStart:0,selectionEnd:0,
 attempt:async fn=>fn(),captureText:()=>{},captureOptions:()=>{},currentOptions:()=>project.options,
 $s:()=>({value:'Audition'}),engine:()=>({installed:true,label:'Kokoro'}),saveProject:async()=>{},
 chapterData:()=>project.chapters[0],output:()=>({mode:'default'}),queueResponse:()=>{},
 api:async(path,body)=>{sent={path,body};return{jobs:[]};}};
vm.runInNewContext(source,context);
context.generate('preview','af_bella').then(()=>process.stdout.write(JSON.stringify({sent,project}))).catch(e=>{console.error(e.message);process.exitCode=1;});
'''
    result = subprocess.run(['node', '-e', script, json.dumps(function)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output['sent']['path'] == '/speech/preview'
    assert output['sent']['body']['options']['voice'] == 'af_bella'
    assert output['sent']['body']['options']['blend'] == []
    assert output['project']['options']['blend'] == [{'voice': 'am_adam', 'weight': 1}]
    assert output['project']['speakers']['narrator']['voice'] == 'am_onyx'
