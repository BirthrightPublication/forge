"""Offline source fixtures exercise persistence and scheduling, not live YouTube access."""
import concurrent.futures
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from forge.addresses import address, COUNTRIES, LOCATIONS
from forge.app import create_app
from forge.core import Store
from forge.download_studio import DownloadStudio, WatchRequest, MediaAction, Destinations
from forge.download_support import diagnostic, validate_template
from forge.generators import AddressConfig
from forge.queue import QueueManager
from forge.source_probe import classify, youtube_source
from forge.source_watch import SourceError


def media(identity='fixture001',kind='video'):
    return {'id':identity,'url':'https://www.youtube.com/watch?v='+identity,'title':'Synthetic '+identity,
            'type':kind,'published':time.time(),'upload_date':'20261023','duration':30,'thumbnail':'','availability':'public'}


class Adapter:
    platform='youtube'
    capabilities={'discovery':True}
    def __init__(self): self.items=[media()];self.title='Synthetic channel';self.failure=None;self.calls=[]
    def inspect(self,url,**kwargs):
        self.calls.append(kwargs)
        if self.failure: raise self.failure
        return {'source_id':'UC_SYNTHETIC_FIXTURE','url':'https://www.youtube.com/channel/UC_SYNTHETIC_FIXTURE',
                'title':self.title,'entries':self.items,'reported_count':None,'has_more':False,'offset':kwargs.get('offset',0)}


@pytest.fixture
def studio(tmp_path):
    store=Store(tmp_path/'data');manager=QueueManager(store);adapter=Adapter()
    service=DownloadStudio(store,manager,adapter)
    service.destinations.remember(tmp_path/'exports',default=True)
    yield service
    manager.stop_event.set()
    for thread in service.threads: thread.join(timeout=5)
    store.close()


def register(studio,**config):
    studio.register(WatchRequest(url='https://www.youtube.com/@fixture',config=config))
    return studio.sources()[0]


def test_future_baseline_then_new_upload_is_atomic_and_persistent(studio):
    source=register(studio)
    assert not studio.store.jobs() and studio.media()[0]['status']=='skipped'
    studio.adapter.items.append(media('fixture002'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _:studio.check_once(source['id']),range(12)))
    assert len(studio.store.jobs())==1
    record=studio.get('watch_media','youtube:fixture002')
    reopened=Store(studio.store.data)
    try:
        other=DownloadStudio(reopened,QueueManager(reopened),studio.adapter)
        other.check_once(source['id'])
        assert len(reopened.jobs())==1 and other.get('watch_media',record['key'])['job_id']==record['job_id']
    finally: reopened.close()


def test_stable_channel_identity_rename_rules_priority_and_folder(studio):
    source=register(studio,initial='latest',priority='low',rules={'video':{'priority':'critical','options':{'format':'mp3'}}})
    first=studio.store.jobs()[0]
    assert first['tool']=='download_audio' and first['priority']=='critical'
    assert 'UC_SYNTHETIC_FIXTURE' in first['payload']['destination']
    studio.adapter.title='Renamed synthetic source';studio.check_once(source['id'])
    assert studio.sources()[0]['folder']==source['folder'] and len(studio.store.jobs())==1
    with pytest.raises(ValueError,match='already monitored'): register(studio)


def test_optional_history_and_live_transition(studio):
    studio.adapter.items=[media('live001','live'),media('later001','upcoming')]
    source=register(studio,initial='future',live_action='replay')
    assert not studio.store.jobs()
    studio.adapter.items=[media('live001','replay'),media('later001','premiere')]
    studio.check_once(source['id']);studio.check_once(source['id'])
    assert len(studio.store.jobs())==2
    studio.adapter.items=[media('historical')]
    studio.check_once(source['id'],archive=True)
    assert len(studio.store.jobs())==2 and studio.get('watch_media','youtube:historical')['status']=='review'


def test_pending_maintenance_and_global_pause_never_lose_discovery(studio):
    source=register(studio)
    studio.manager.maintenance_pending=True
    studio.adapter.items=[media('newpending')];studio.check_once(source['id'])
    assert not studio.store.jobs() and studio.media()[0]['auto_pending']
    studio.manager.maintenance_pending=False;studio.retry_due()
    assert len(studio.store.jobs())==1
    studio.store.set('watch_paused',True)
    studio.adapter.items=[media('newpaused')];studio.check_once(source['id'])
    assert len(studio.store.jobs())==1
    studio.store.set('watch_paused',False);studio.retry_due()
    assert len(studio.store.jobs())==2


def test_failure_backoff_preserves_snapshot_and_auth_is_not_retried(studio):
    source=register(studio)
    studio.adapter.failure=SourceError(diagnostic('network timeout'))
    studio.check_once(source['id'])
    value=studio.sources()[0]
    assert value['status']=='offline' and value['next_check']>time.time()+1000
    assert len(studio.media())==1 and value['last_success']==source['last_success']
    studio.adapter.failure=SourceError(diagnostic("Sign in to confirm you're not a bot. PRIVATE COOKIE VALUE"))
    studio.check_once(source['id'])
    assert studio.sources()[0]['status']=='access_required'
    assert 'PRIVATE' not in json.dumps(studio.snapshot())


def test_failed_jobs_survive_and_queue_retry_updates_media_identity(studio):
    register(studio,initial='latest')
    job=studio.store.jobs()[0]
    studio.store.update_job(job['id'],status='failed',message='network timeout')
    studio.reconcile()
    retried=studio.manager.retry(job['id'])
    assert studio.media()[0]['job_id']==retried['id']
    with pytest.raises(ValueError,match='already'): studio.manager.retry(job['id'])
    assert len(studio.store.jobs())==2
    studio.store.update_job(retried['id'],status='failed',message='Sign in required')
    studio.reconcile()
    studio.action(MediaAction(key=studio.media()[0]['key'],action='skip'))
    studio.retry_due()
    assert len(studio.store.jobs())==2 and studio.media()[0]['status']=='skipped'


def test_completed_files_require_actual_bytes_and_hash_match(studio,tmp_path):
    register(studio,initial='latest')
    job=studio.store.jobs()[0];file=tmp_path/'verified.mp4';file.write_bytes(b'synthetic previously worker-verified fixture')
    studio.store.update_job(job['id'],status='completed',outputs=[{'path':str(file),'size':file.stat().st_size,'name':file.name}])
    studio.reconcile();record=studio.media()[0]
    assert record['status']=='completed' and len(record['outputs'][0]['sha256'])==64
    copy=tmp_path/'copy.mp4';copy.write_bytes(file.read_bytes())
    studio.action(MediaAction(key=record['key'],action='archived',path=str(copy)))
    assert studio.media()[0]['status']=='archived'
    copy.write_bytes(b'unrelated')
    with pytest.raises(ValueError,match='hash'): studio.action(MediaAction(key=record['key'],action='archived',path=str(copy)))


def test_priority_changes_do_not_interrupt_running_work(studio):
    source=register(studio,initial='latest',priority='low')
    studio.adapter.items=[media('high')];studio.check_once(source['id'])
    studio.action(MediaAction(key='youtube:high',action='priority',priority='critical'))
    assert studio.store.jobs(status='queued')[0]['payload']['watch_media']=='youtube:high'
    assert not studio.manager.active


def test_destination_persistence_and_nonexistent_folder_error(studio,tmp_path):
    folder=tmp_path/'named';folder.mkdir()
    studio.destinations.remember(folder,name='Synthetic exports',pinned=True,default=True)
    reopened=Store(studio.store.data)
    try:
        result=Destinations(reopened).snapshot()
        assert result['default']==str(folder) and result['records'][0]['pinned']
    finally: reopened.close()
    from forge.download_studio import writable_directory
    with pytest.raises(ValueError,match='unavailable'): writable_directory(str(tmp_path/'missing'))


@pytest.mark.parametrize('info,expected',[
 ({'duration':20},'unclassified'),({'url':'https://www.youtube.com/shorts/a'},'short'),
 ({'is_live':True},'live'),({'live_status':'is_upcoming'},'upcoming'),({'was_live':True},'replay'),
 ({'is_premiere':True},'premiere'),({'is_podcast':True},'podcast'),({'url':'https://www.youtube.com/clip/a'},'clip'),
 ({'extractor_key':'Youtube'},'video')])
def test_classification_uses_explicit_metadata(info,expected): assert classify(info)==expected


@pytest.mark.parametrize('value',['../%(id)s.%(ext)s','%(unknown)s [%(id)s].%(ext)s','%(title)s.%(ext)s','C:\\%(id)s.%(ext)s'])
def test_unsafe_templates_rejected(value):
    with pytest.raises(ValueError): validate_template(value)


def test_portable_template_and_canonical_channel():
    assert validate_template('%(upload_date)s - %(title)s [%(id)s].%(ext)s')
    assert youtube_source('https://www.youtube.com/@fixture/shorts')=='https://www.youtube.com/@fixture'
    with pytest.raises(ValueError): youtube_source('https://youtube.com.attacker.invalid/@fixture')


@pytest.mark.parametrize('country',list(COUNTRIES))
def test_address_country_coherence_locks_and_single_field(country):
    options=AddressConfig(country=country,count=1,include_unit=True,include_county=True).model_dump()
    first=address(options);current={k:v for k,v in first.items() if k not in {'value','fictional'}}
    for _ in range(10):
        next_row=address(options,current,['city','region','postal_code'],'street')
        assert next_row['street']!=first['street']
        assert all(next_row[k]==first[k] for k in current if k!='street')
        assert (next_row['city'],next_row['region'],next_row['postal_code'],next_row['county']) in LOCATIONS[country]
    with pytest.raises(ValueError,match='Unlock'): address(options,current,['city'],'city')


def test_address_conflicting_locked_geography_fails_instead_of_corrupting():
    options=AddressConfig(count=1,include_county=True).model_dump()
    current={'city':'Pasadena','region':'WA','postal_code':'91101'}
    with pytest.raises(ValueError,match='compatible'): address(options,current,['city','region'])


def test_address_and_watch_api_authentication_and_no_value_persistence(tmp_path):
    app=create_app(tmp_path/'data','synthetic-test-token',9876,workers=False)
    with TestClient(app,base_url='http://127.0.0.1:9876',headers={'X-Forge-Token':'synthetic-test-token'}) as client:
        response=client.post('/api/generators/addresses/regenerate',json={'options':{'count':1}})
        assert response.status_code==200,response.text
        assert 'Fictional' in response.text
        assert 'Fictional' not in json.dumps(app.state.store.get('working_preferences',{}))
        assert client.post('/api/generators/addresses/regenerate',json={'current':{'unknown':'x'}}).status_code==422
        client.headers.pop('X-Forge-Token')
        assert client.get('/api/watch').status_code==401
    app.state.store.close()


def test_launcher_refuses_wrong_root_and_stale_backend(tmp_path,monkeypatch):
    import launcher
    monkeypatch.setattr(launcher,'ROOT',tmp_path)
    (tmp_path/'VERSION.txt').write_text('0.7.0')
    with pytest.raises(RuntimeError,match='Another'): launcher.verify_runtime({'root':str(tmp_path/'other')})
    monkeypatch.setattr(launcher,'request',lambda *args:{'version':'0.6.0'})
    with pytest.raises(RuntimeError,match='older'): launcher.verify_runtime({'root':str(tmp_path),'version':'0.7.0'})


def test_health_checks_process_root_and_both_versions(tmp_path,monkeypatch):
    from test_v6_maintenance import installer
    monkeypatch.setattr(installer,'ROOT',tmp_path)
    runtime={'root':str(tmp_path),'pid':123,'version':'0.7.0'}
    monkeypatch.setattr('launcher.request',lambda r,p:{'version':'0.7.0','pid':123})
    installer.verify_health(runtime,'0.7.0',123)
    with pytest.raises(RuntimeError,match='identity'): installer.verify_health(runtime,'0.7.0',456)
    monkeypatch.setattr('launcher.request',lambda r,p:{'version':'0.6.0','pid':123})
    with pytest.raises(RuntimeError,match='version'): installer.verify_health(runtime,'0.7.0',123)


@pytest.mark.skipif(__import__('os').name!='nt',reason='Native Windows managed service restart acceptance')
def test_native_managed_restart_verifies_new_process_and_preserves_queue(tmp_path,monkeypatch):
    import os
    import py_compile
    import shutil
    import socket
    import subprocess
    import sys
    import launcher
    from test_v6_maintenance import installer
    from forge import __version__
    root=tmp_path/'Forge';root.mkdir();source=Path(__file__).resolve().parents[1]
    for name in ('forge','scripts','web'): shutil.copytree(source/name,root/name,ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('server.py','launcher.py','VERSION.txt','requirements-core.txt','requirements-downloads.txt','requirements-transcription.txt','requirements-diarization.txt'):
        shutil.copyfile(source/name,root/name)
    subprocess.run([sys.executable,'-m','venv','--system-site-packages',str(root/'.venv')],check=True,timeout=60)
    # Reproduce same-size/same-mtime stale bytecode from the previous release.
    init=root/'forge/__init__.py';content=init.read_text();stamp=init.stat()
    init.write_text(content.replace(__version__,'0.6.0'))
    os.utime(init,ns=(stamp.st_atime_ns,stamp.st_mtime_ns));py_compile.compile(str(init),doraise=True)
    init.write_text(content);os.utime(init,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    data=tmp_path/'data';store=Store(data)
    settings=store.settings();settings.update(queue_paused=True,automatic_update_checks=False,daily_update_check=False)
    store.set('settings',settings);store.set('workflows',[{'name':'Synthetic preserved workflow'}])
    queued=store.add_job('qr','Keep queued',{'tool':'qr','options':{'text':'Synthetic preserved queued work'}})
    store.close();(data/'models').mkdir(exist_ok=True);(data/'models/sentinel.bin').write_bytes(b'preserve cached model')
    monkeypatch.setattr(installer,'ROOT',root)
    popen=subprocess.Popen;children=[]
    def launch(command,**kwargs):
        if str(command[1]).endswith('launcher.py'):return SimpleNamespace(pid=0)
        child=popen(command,**kwargs)
        if str(command[1]).endswith('server.py'): children.append(child)
        return child
    monkeypatch.setattr(installer.subprocess,'Popen',launch)
    with socket.socket() as socket_:socket_.bind(('127.0.0.1',0));port=socket_.getsockname()[1]
    result=data/'maintenance/result.json';result.parent.mkdir(exist_ok=True)
    task={'data':str(data),'queue_paused':True,'port':port}
    try:
        assert installer.restart_verified(task,result)==__version__
        runtime=json.loads((data/'runtime.json').read_text())
        pid=installer.owned_server_pid(runtime,children[0].pid)
        installer.verify_health(runtime,__version__,pid)
        with pytest.raises(RuntimeError,match='owned'):
            installer.owned_server_pid(runtime,99999999)
        assert launcher.request(runtime,'/api/state')['settings']['queue_paused'] is True
        reopened=Store(data)
        try:
            assert reopened.job(queued['id'])['status']=='queued'
            assert reopened.get('workflows')==[{'name':'Synthetic preserved workflow'}]
        finally: reopened.close()
        assert (data/'models/sentinel.bin').read_bytes()==b'preserve cached model'
    finally:
        try:
            runtime=json.loads((data/'runtime.json').read_text())
            launcher.request(runtime,'/api/shutdown',{})
        except (OSError,ValueError): pass
        for child in children:
            try: child.wait(timeout=15)
            except subprocess.TimeoutExpired: installer.stop_owned_child(child)


def test_download_authentication_reaches_worker_but_not_presets(tmp_path):
    cookie=tmp_path/'authorized-cookies.txt';cookie.write_text('# Netscape HTTP Cookie File\n')
    app=create_app(tmp_path/'data','synthetic-test-token',9876,workers=False)
    with TestClient(app,base_url='http://127.0.0.1:9876',headers={'X-Forge-Token':'synthetic-test-token'}) as client:
        body={'tool':'download_video','urls':['https://www.youtube.com/watch?v=fixture'],
              'options':{'format':'mp4','cookies':str(cookie)},'output':{'mode':'custom','directory':str(tmp_path/'out')}}
        response=client.post('/api/jobs',json=body)
        assert response.status_code==200,response.text
        assert app.state.store.jobs()[0]['payload']['options']['cookies']==str(cookie)
        response=client.post('/api/presets',json={k:v for k,v in {**body,'name':'Synthetic auth-free preset'}.items() if k!='urls'})
        assert response.status_code==200,response.text
        assert app.state.store.get('presets')[0]['options']['cookies']==''
        assert str(cookie) not in json.dumps(client.get('/api/state').json()['jobs'])
    app.state.store.close()


def test_thumbnail_host_allowlist_and_local_png_conversion(monkeypatch):
    import base64
    import io
    from PIL import Image
    from forge import source_watch
    raw=io.BytesIO();Image.new('RGB',(300,200),'gold').save(raw,format='JPEG')
    opened=[]
    class Opener:
        def open(self,url,**kwargs):opened.append(url);return io.BytesIO(raw.getvalue())
    monkeypatch.setattr(source_watch.urllib.request,'build_opener',lambda *args:Opener())
    assert source_watch.thumbnail_data('https://unapproved.example/private.png')=='' and not opened
    assert source_watch.thumbnail_data('https://i.ytimg.com:8443/image.jpg')=='' and not opened
    value=source_watch.thumbnail_data('https://i.ytimg.com/fixture.jpg')
    assert value.startswith('data:image/png;base64,')
    with Image.open(io.BytesIO(base64.b64decode(value.split(',')[1]))) as image:
        assert max(image.size)==160 and image.format=='PNG'


def test_unclassified_requires_explicit_rule_and_auth_path_is_not_public(studio):
    studio.adapter.items=[media('unknown001','unclassified')]
    source=register(studio,initial='latest',options={'format':'mp4','cookies':'synthetic-local-cookie-path'})
    assert not studio.store.jobs() and studio.media()[0]['status']=='review'
    assert 'synthetic-local-cookie-path' not in json.dumps(studio.snapshot())
    studio.configure(source['id'],{'types':['unclassified'],'options':{'max_height':720}})
    assert studio.sources()[0]['config']['options']['cookies']=='synthetic-local-cookie-path'
    studio.adapter.items.append(media('unknown002','unclassified'));studio.check_once(source['id'])
    assert len(studio.store.jobs())==1
