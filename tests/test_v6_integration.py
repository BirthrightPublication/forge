import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from forge.app import create_app
from forge.core import Store
from forge.cuda_health import remember_failure, recent_failure
from forge.dependencies import ENGINES, inspect
from forge.preferences import safe_options
from forge.queue import QueueManager
from forge.resources import primary_resource
from forge.update_catalog import application_entry, Catalog


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path/'data','synthetic-test-token',9876,workers=False)
    with TestClient(app,base_url='http://127.0.0.1:9876',headers={'X-Forge-Token':'synthetic-test-token'}) as client:
        yield client
    app.state.store.close()


@pytest.mark.parametrize('tool,options,sensitive', [
    ('transcribe',{'model':'tiny','device':'cuda','vocabulary':'PRIVATE PROMPT','formats':['txt']},'vocabulary'),
    ('qr',{'kind':'wifi','wifi':{'ssid':'PRIVATE','password':'PRIVATE'},'box_size':15},'wifi'),
    ('qr',{'text':'PRIVATE TEXT','contact':{'name':'PRIVATE PERSON'},'format':'svg'},'text'),
    ('download_audio',{'cookies':'PRIVATE COOKIE PATH','format':'wav'},'cookies'),
    ('watermark',{'text':'PRIVATE WATERMARK','opacity':.4,'audio_clip_path':'PRIVATE PATH'},'text'),
    ('metadata',{'tags':{'comment':'PRIVATE'},'mode':'wipe'},'tags'),
    ('password',{'generated':'PRIVATE PASSWORD','length':40},'generated'),
    ('strings',{'result':['PRIVATE TOKEN'],'length':64},'result'),
])
def test_preferences_filter_content_before_persistence(client,tool,options,sensitive):
    response=client.post('/api/preferences/'+tool,json={'options':options})
    assert response.status_code==200,response.text
    stored=client.app.state.store.get('working_preferences')[tool]
    assert sensitive not in stored['options'] and 'PRIVATE' not in json.dumps(stored)
    assert stored['options']


def test_working_preferences_survive_reopen_without_overwriting_presets(client):
    store=client.app.state.store
    preset={'tool':'transcribe','name':'Keep','options':{'model':'base'}}
    store.set('presets',[preset])
    body={'options':{'model':'small','device':'cpu','formats':['txt','srt']},'output':{'mode':'custom','directory':str(store.data/'exports')}}
    assert client.post('/api/preferences/transcribe',json=body).status_code==200
    reopened=Store(store.data)
    try:
        assert reopened.get('working_preferences')['transcribe']['options']['model']=='small'
        assert reopened.get('presets')==[preset]
    finally:reopened.close()
    assert client.post('/api/preferences/transcribe/reset',json={}).status_code==200
    assert 'transcribe' not in store.get('working_preferences') and store.get('presets')==[preset]


def test_invalid_partial_field_does_not_discard_other_preferences():
    assert safe_options('transcribe',{'model':'unapproved','beam_size':8,'new_unknown':'PRIVATE'})=={'beam_size':8}


def test_saved_output_paths_are_configuration_not_media_history(client):
    assert client.post('/api/preferences/retune',json={'options':{'target_hz':432},'output':{'mode':'custom','directory':'B:\\Exports','subfolder':'Audio'}}).status_code==200
    assert not client.app.state.store.jobs()


def test_cuda_missing_library_blocks_explicit_jobs_without_dispatch(tmp_path):
    store=Store(tmp_path);queue=QueueManager(store)
    job=store.add_job('transcribe','synthetic',{'options':{'device':'cuda','model':'tiny'}})
    remember_failure(tmp_path,'Library cublas64_12.dll cannot be loaded')
    queue.dispatch_once()
    assert store.job(job['id'])['status']=='blocked' and not queue.active
    assert 'cublas64_12.dll' in store.job(job['id'])['message']
    store.close()


def test_auto_fallback_uses_cpu_resource_without_changing_preference(tmp_path,monkeypatch):
    store=Store(tmp_path);queue=QueueManager(store)
    job=store.add_job('transcribe','synthetic',{'options':{'device':'auto','model':'tiny'}})
    remember_failure(tmp_path,'Missing cuDNN runtime')
    launched=[];monkeypatch.setattr(queue,'launch',lambda value:launched.append(value))
    queue.dispatch_once()
    assert launched and primary_resource(launched[0])=='cpu'
    assert store.job(job['id'])['payload']['options']['device']=='auto'
    store.close()


def test_runtime_failure_has_bounded_lifetime(tmp_path,monkeypatch):
    remember_failure(tmp_path,'missing runtime')
    assert recent_failure(tmp_path)
    value=json.loads((tmp_path/'cuda-health.json').read_text());value['checked']-=3601
    (tmp_path/'cuda-health.json').write_text(json.dumps(value))
    assert not recent_failure(tmp_path)


def test_cuda_enumeration_does_not_claim_inference_readiness(monkeypatch):
    from forge import cuda_health
    monkeypatch.setattr(cuda_health,'add_runtime_paths',lambda:None)
    monkeypatch.setattr(cuda_health.shutil,'which',lambda *_:None)
    monkeypatch.setattr(cuda_health.importlib.metadata,'version',lambda *_:'4.6.0')
    monkeypatch.setitem(sys.modules,'ctranslate2',SimpleNamespace(get_cuda_device_count=lambda:1,get_supported_compute_types=lambda *_:['float16']))
    if os.name=='nt':
        monkeypatch.setattr(cuda_health.ctypes,'WinDLL',lambda *_:object())
    report=cuda_health.runtime_report()
    assert report['cuda_devices']==1 and report['runtime_available']
    assert not report['ready'] and not report['inference_tested']


def test_inference_diagnostic_consumes_lazy_generator_before_reporting_ready(tmp_path,monkeypatch):
    from forge import cuda_health
    consumed=[]
    class Model:
        def __init__(self,*a,**k):pass
        def transcribe(self,*a,**k):
            def generate():
                consumed.append(True)
                yield SimpleNamespace(text='synthetic result')
            return generate(),None
    monkeypatch.setattr(cuda_health,'runtime_report',lambda:{'runtime_available':True,'ready':False,'inference_tested':False,'inference_passed':False})
    monkeypatch.setattr('forge.model_cache.ensure_model',lambda *a,**k:tmp_path/'synthetic-model')
    monkeypatch.setitem(sys.modules,'faster_whisper',SimpleNamespace(WhisperModel=Model))
    monkeypatch.setitem(sys.modules,'numpy',SimpleNamespace(zeros=lambda *a,**k:[],float32=float))
    report=cuda_health.inference_report(tmp_path,'tiny')
    assert consumed and report['ready'] and report['inference_passed']
    assert json.loads((tmp_path/'cuda-health.json').read_text())['inference_tested']


def release(tag='v0.6.0'):
    names=[f'FORGE-{tag}-windows-x64.zip',f'FORGE-{tag}-windows-x64.zip.sha256',f'FORGE-{tag}.zip',f'FORGE-{tag}.zip.sha256']
    return {'tag_name':tag,'draft':False,'prerelease':False,'published_at':'2026-10-22T00:00:00Z',
            'assets':[{'name':name,'browser_download_url':'https://github.com/BirthrightPublication/forge/releases/download/'+tag+'/'+name,'size':10} for name in names]}


def test_application_discovery_requires_both_updater_contracts():
    value=release('v9.0.0');entry=application_entry(value)
    assert entry['compatible'] and entry['action']=='update' and entry['published']
    value['assets']=value['assets'][1:]
    assert not application_entry(value)['action']


def test_wrong_publisher_asset_cannot_become_installable():
    value=release();value['assets'][0]['browser_download_url']='https://untrusted.example/app.zip'
    with pytest.raises(ValueError):application_entry(value)


def test_offline_discovery_keeps_processing_independent(client,monkeypatch):
    from forge import update_catalog
    monkeypatch.setattr(update_catalog,'read_json',lambda url:(_ for _ in ()).throw(OSError('offline')))
    catalog=client.app.state.update_catalog
    catalog.refresh(force=True);catalog.thread.join(10)
    value=catalog.snapshot()
    assert not value['busy'] and value['errors'] and value['next_check']>time.time()
    assert client.get('/api/state').status_code==200
    assert value['count']==sum(bool(e.get('action')) for e in value['entries'])
    assert not any(e.get('action') for e in value['entries'] if e['category']=='models' and not e['installed'])


def test_update_install_requires_explicit_approval(client):
    assert client.post('/api/updates/install',json={'ids':['application']}).status_code==400
    assert not client.app.state.store.get('dependency_approvals')


def test_maintenance_cancel_requires_separate_active_job_confirmation(client):
    response=client.post('/api/maintenance/install',json={'engines':['transcription'],'approve':True,'cancel_jobs':True})
    assert response.status_code==400 and 'Confirm cancellation' in response.json()['detail']


def test_repair_pause_preserves_queued_jobs(tmp_path,monkeypatch):
    store=Store(tmp_path);queue=QueueManager(store)
    job=store.add_job('qr','keep queued',{'options':{}})
    queue.repair_pending=True
    monkeypatch.setattr(queue,'launch',lambda *_:pytest.fail('Repair must pause dispatch'))
    queue.dispatch_once()
    assert store.job(job['id'])['status']=='queued'
    store.close()
