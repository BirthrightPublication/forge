"""Authenticated V0.8 API/queue integration, with real DSP and synthetic media.

No speech model inference is mocked into a success. Speech production tests verify
queued immutable requests; only the FFmpeg Audio Effects case executes a worker.
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import time
import wave

import pytest
from fastapi.testclient import TestClient

from forge.app import create_app
from forge.resources import model_budget_mb, primary_resource, required_resources

TOKEN='synthetic-v8-api-token-not-a-runtime-secret'
BASE='http://127.0.0.1:9888'


@pytest.fixture
def api(tmp_path):
    app=create_app(tmp_path/'data',TOKEN,9888,workers=False)
    with TestClient(app,base_url=BASE,headers={'X-Forge-Token':TOKEN}) as client:
        yield client,app
    app.state.store.close()


def wav(path):
    rate=48000
    with wave.open(str(path),'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(rate)
        out.writeframes(b''.join(struct.pack('<h',round(4000*math.sin(2*math.pi*440*i/rate))) for i in range(rate//2)))
    return path


def registered(client,path):
    response=client.post('/api/register',json={'paths':[str(path)]})
    assert response.status_code==200,response.text
    return response.json()['sources'][0]['id']


def test_speech_routes_preserve_auth_and_origin_boundary(api):
    client,app=api
    with TestClient(app,base_url=BASE) as outsider:
        assert outsider.get('/api/speech/state').status_code==401
        assert outsider.post('/api/speech/generate',json={'text':'Synthetic text.'}).status_code==401
    assert client.get('/api/speech/state',headers={'Origin':'https://example.org'}).status_code==403
    assert client.post('/api/speech/projects',json={'name':'blocked'},headers={'Origin':'https://example.org'}).status_code==403
    assert not app.state.store.jobs()


def test_discovery_is_local_and_reports_actual_availability(api,monkeypatch):
    from forge import speech_engines,bark_asr
    client,app=api
    def no_install(*args,**kwargs):raise AssertionError('Discovery must never install a runtime or model')
    monkeypatch.setattr(speech_engines,'install',no_install)
    monkeypatch.setattr(bark_asr,'install',no_install)
    response=client.get('/api/speech/state')
    assert response.status_code==200,response.text
    data=response.json()
    assert {'kokoro','ming'} <= {engine['id'] for engine in data['engines']}
    assert any(v['id']=='am_onyx' and not v['installed'] for v in data['voices'])
    assert all(not model.get('installed') for model in data['engines'])
    assert data['projects']==[] and not app.state.store.jobs()
    tools=client.get('/api/state').json()['tools']
    assert {'tts','audio_effects','transcribe','download_video'}<=set(tools)
    assert client.get('/api/speech/effects-schema').json()['effects']


def test_project_autosave_revisions_and_legacy_state_survive(api):
    client,app=api
    app.state.store.set('presets',[{'name':'Existing V0.7 preset','tool':'transcribe','options':{}}])
    app.state.store.set('workflows',[{'name':'Unrelated preserved definition'}])
    first=client.post('/api/speech/projects',json={'name':'Synthetic book','text':'Chapter one.','engine':'kokoro','voice':'am_onyx'})
    assert first.status_code==200,first.text
    project=first.json()['project'];identity=project['id'];chapter=project['chapters'][0]['id']
    changed=copy.deepcopy(project);changed['chapters'][0]['text']='Edited one sentence.'
    second=client.post('/api/speech/projects',json={'project':changed})
    assert second.status_code==200,second.text
    assert second.json()['project']['revision']==2
    assert second.json()['project']['chapters'][0]['id']==chapter
    assert client.post('/api/speech/projects',json={'project':project}).status_code==409
    assert client.get('/api/speech/projects/'+identity).json()['chapters'][0]['text']=='Edited one sentence.'
    assert app.state.speech.project(identity,revision=1)['chapters'][0]['text']=='Chapter one.'
    assert app.state.store.get('presets')[0]['name']=='Existing V0.7 preset'
    assert app.state.store.get('workflows')==[{'name':'Unrelated preserved definition'}]


def test_record_crud_is_data_only_and_reference_consent_is_required(api,tmp_path):
    client,app=api;sentinel=tmp_path/'must-not-exist'
    value={'name':'Synthetic profile','engine':'kokoro','voice':'am_onyx','notes':"__import__('pathlib').Path("+repr(str(sentinel))+").touch()"}
    response=client.post('/api/speech/records/voices',json={'record':value})
    assert response.status_code==200,response.text
    saved=response.json()['record']
    assert client.get('/api/speech/records/voices').json()['records'][0]['notes']==value['notes']
    assert not sentinel.exists()
    changed={**saved,'name':'Renamed profile'}
    assert client.post('/api/speech/records/voices',json=changed).status_code==200
    assert client.post('/api/speech/records/voices',json=saved).status_code==409
    assert client.delete('/api/speech/records/voices/'+saved['id']).json()=={'ok':True}
    assert not client.get('/api/speech/records/voices').json()['records']
    assert client.post('/api/speech/records/pickle',json={'name':'not a supported object'}).status_code==400
    assert client.post('/api/speech/records/voices',json={'name':'No consent','reference_audio':str(tmp_path/'voice.wav')}).status_code==400


def test_compile_strips_markers_and_preserves_source(api):
    client,app=api
    text='[[speaker:narrator]]Hello. [[pause:750ms]][[emotion:happy]]Welcome home.[[/emotion]]'
    response=client.post('/api/speech/parse',json={'text':text})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['source']==text
    assert '[[pause:' not in result['spoken_text'] and '[[emotion:' not in result['spoken_text']
    assert 'Hello.' in result['spoken_text'] and 'Welcome home.' in result['spoken_text']
    assert any(s['kind']=='silence' and s['pause_ms']==750 for s in result['segments'])
    assert client.post('/api/speech/parse',json={'text':'[[untrusted:read aloud]]'}).status_code==400
    assert not app.state.store.jobs()


def test_tts_only_enqueues_immutable_project_and_voice_snapshots(api,monkeypatch):
    from forge import speech_engines
    client,app=api
    def no_inference(*args,**kwargs):raise AssertionError('HTTP request must not run speech inference inline')
    monkeypatch.setattr(speech_engines,'synthesize',no_inference)
    project=client.post('/api/speech/projects',json={'name':'Original title','text':'Original text.'}).json()['project']
    profile=client.post('/api/speech/records/voices',json={'name':'Original voice','engine':'kokoro','voice':'am_onyx','model_revision':'a'*40,'blend':[{'voice':'am_onyx','weight':1}]}).json()['record']
    response=client.post('/api/speech/generate',json={'project_id':project['id'],'options':{'voice':profile['id'],'text':'Original text.'}})
    assert response.status_code==200,response.text
    job=response.json()['job'];saved=copy.deepcopy(app.state.store.job(job['id'])['payload'])
    assert job['tool']=='tts' and job['status']=='queued' and job['outputs']==[]
    assert saved['speech_project']['revision']==1 and saved['speech_profile']['revision']==1
    assert saved['options']['voice']=='am_onyx'
    assert saved['options']['model_revision']=='a'*40 and saved['options']['voice_revision']==1
    assert saved['options']['revision']==''
    client.post('/api/speech/projects',json={**project,'name':'New title','text':'New text.'})
    client.post('/api/speech/records/voices',json={**profile,'name':'New voice'})
    assert app.state.store.job(job['id'])['payload']==saved
    assert not list((app.state.store.data/'speech'/'exports').glob('*.wav'))


def test_install_requires_explicit_consent_and_does_not_download_inline(api,monkeypatch):
    from forge import speech_engines
    client,app=api
    monkeypatch.setattr(speech_engines,'install',lambda *args,**kwargs:pytest.fail('Installation cannot run in discovery/request handler'))
    for consent in [None,False,'yes',1]:
        response=client.post('/api/speech/install',json={'engine':'kokoro','consent':consent})
        assert response.status_code==400,response.text
    assert not app.state.store.jobs()
    response=client.post('/api/speech/install',json={'engine':'kokoro','consent':True})
    assert response.status_code==200,response.text
    job=app.state.store.job(response.json()['job']['id'])
    assert job['tool']=='speech_install' and job['status']=='queued' and job['outputs']==[]
    assert job['payload']['options']['consent'] is True
    assert primary_resource(job)=='network'


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='Real media tools required')
def test_effects_queue_worker_outputs_real_verified_media_and_preserves_source(api,tmp_path):
    client,app=api;source=wav(tmp_path/'test-owned-tone.wav');digest=hashlib.sha256(source.read_bytes()).hexdigest()
    sid=registered(client,source)
    response=client.post('/api/speech/effects',json={'source_id':sid,'options':{'chain':[{'type':'gain','params':{'db':-6}}]}})
    assert response.status_code==200,response.text
    job=response.json()['job'];assert job['status']=='queued' and not job['outputs']
    assert client.get('/api/speech/audio',params={'job':job['id']}).status_code==404
    app.state.manager.launch(app.state.store.job(job['id']))
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        finished=app.state.store.job(job['id'])
        if finished['status'] in {'completed','failed','interrupted','cancelled'}:break
        time.sleep(.05)
    assert finished['status']=='completed',finished
    assert len(finished['outputs'])==2
    output=next(o for o in finished['outputs'] if o['path'].endswith('.wav'))
    assert Path(output['path']).is_file() and output['size']>100
    index=finished['outputs'].index(output)
    served=client.get('/api/speech/audio',params={'job':job['id'],'index':index})
    assert served.status_code==200 and served.content==Path(output['path']).read_bytes()
    receipt=json.loads(Path(next(o['path'] for o in finished['outputs'] if o['path'].endswith('.json'))).read_text())
    assert receipt['output_levels']['rms_dbfs']-receipt['input_levels']['rms_dbfs']==pytest.approx(-6,abs=.1)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='Real media tools required')
def test_waveform_and_preview_accept_only_registered_source_references(api,tmp_path):
    client,app=api;source=wav(tmp_path/'registered.wav');sid=registered(client,source)
    for body in [{},{'path':str(source)},{'source_id':'missing'}]:
        assert client.post('/api/speech/waveform',json=body).status_code==400
        assert client.post('/api/speech/effects',json=body).status_code==400
    assert client.get('/api/speech/source-audio',params={'source_id':sid}).content==source.read_bytes()
    response=client.post('/api/speech/waveform',json={'source_id':sid})
    assert response.status_code==200 and len(response.json()['peaks'])==1000
    response=client.post('/api/speech/effects',json={'source_id':sid,'preview':True,'preview_seconds':.25,'options':{'chain':[]}})
    assert response.status_code==200,response.text
    options=app.state.store.job(response.json()['job']['id'])['payload']['options']
    assert options['region']=={'start':0,'end':.25}
    assert client.post('/api/speech/effects',json={'source_id':sid,'preview':True,'preview_seconds':31}).status_code==400


def test_project_save_preserves_current_master_effects_over_legacy_empty_chain(api):
    client,app=api
    chain=[{'type':'gain','params':{'db':-3}}]
    response=client.post('/api/speech/projects',json={'name':'Master chain','text':'Synthetic narration.',
                         'options':{'engine':'kokoro','voice':'am_onyx','master_effects':chain},'master_chain':[]})
    assert response.status_code==200,response.text
    saved=response.json()['project']
    assert client.get('/api/speech/projects/'+saved['id']).json()['options']['master_effects']==chain


def test_tts_and_effects_are_registered_in_linear_black_scripting_and_resource_lanes(api,tmp_path):
    client,app=api;text=tmp_path/'test-owned-script.txt';text.write_text('Synthetic narration.')
    sid=registered(client,text)
    script={'id':'speech-chain','name':'Speech to effects','nodes':[
        {'id':'voice','tool':'tts','options':{'engine':'kokoro','voice':'am_onyx','format':'wav'}},
        {'id':'effects','tool':'audio_effects','options':{'chain':[{'type':'gain','params':{'db':-3}}],'format':'flac'}}],
        'connections':[{'source':'voice','target':'effects'}], 'output':{'mode':'custom','directory':str(tmp_path/'exports')}}
    response=client.post('/api/black-scripting/preview',json={'script':script,'sources':[sid]})
    assert response.status_code==200,response.text
    steps=response.json()['sources'][0]['steps']
    assert [s['tool'] for s in steps]==['tts','audio_effects']
    assert steps[0]['input_format']=='.txt' and steps[0]['output_formats']==['.wav']
    assert steps[1]['input_format']=='.wav' and steps[1]['output_formats']==['.flac']
    cpu={'tool':'tts','payload':{'options':{'engine':'kokoro','device':'cpu'}}}
    gpu={'tool':'tts','payload':{'options':{'engine':'ming','device':'cuda'}}}
    assert primary_resource(cpu)=='cpu' and primary_resource(gpu)=='gpu'
    assert required_resources(cpu)=={'cpu','disk'} and required_resources(gpu)=={'gpu','disk'}
    assert model_budget_mb(gpu)>model_budget_mb(cpu)>0
    assert not app.state.store.jobs()
