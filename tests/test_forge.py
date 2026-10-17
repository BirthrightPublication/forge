from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from forge.app import create_app
from forge.core import Store, absolute_path, passwords, safe_name
from forge.models import Retune, Transcribe
from forge.queue import QueueManager
from forge.worker import export_transcript, probe, publish, retune, timestamp, wipe

TOKEN='test-token-never-in-release-runtime'
BASE='http://127.0.0.1:9876'

@pytest.fixture
def client(tmp_path):
    app=create_app(tmp_path/'data', TOKEN, 9876, workers=False)
    with TestClient(app,base_url=BASE,headers={'X-Forge-Token':TOKEN}) as c:
        c.app=app
        yield c
    app.state.store.close()

@pytest.fixture
def live(tmp_path):
    app=create_app(tmp_path/'data',TOKEN,9876,workers=True)
    with TestClient(app,base_url=BASE,headers={'X-Forge-Token':TOKEN}) as c:
        c.app=app
        yield c
    app.state.store.close()


def submit_qr(client,folder,name='Test QR',fmt='png'):
    r=client.post('/api/jobs',json={'tool':'qr','options':{'text':'FORGE offline QR test','name':name,'format':fmt},'output':{'mode':'custom','directory':str(folder)}})
    assert r.status_code==200,r.text
    return r.json()['jobs'][0]

def wait_job(client,id,timeout=15):
    start=time.monotonic()
    while time.monotonic()-start<timeout:
        j=next(j for j in client.get('/api/state').json()['jobs'] if j['id']==id)
        if j['status'] in {'completed','failed','cancelled','interrupted'}:return j
        time.sleep(.12)
    raise AssertionError('Job timed out')

def tone(path,rate=44100,seconds=2):
    import math,struct
    samples=b''.join(struct.pack('<h',int(13000*math.sin(2*math.pi*440*i/rate))) for i in range(int(rate*seconds)))
    with wave.open(str(path),'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(rate);f.writeframes(samples)
    return path

@pytest.mark.parametrize('name',['a/b','..\\bad','CON','NUL.txt','a:*?<>"|','\x00','....','ß 文件'])
def test_safe_names(name):
    safe=safe_name(name)
    assert safe and not any(c in safe for c in '<>:"/\\|?*\x00')
    assert safe not in {'CON','NUL.txt','..','.'}

def test_paths_reject_relative():
    with pytest.raises(ValueError):absolute_path('relative.mp3')

def test_paths_reject_fakepath():
    with pytest.raises(ValueError):absolute_path('C:\\fakepath\\video.mp4')

@pytest.mark.parametrize('seconds,value',[(0,'00:00:00.000'),(59.9996,'00:01:00.000'),(3600.25,'01:00:00.250'),(-1,'00:00:00.000'),(360000,'100:00:00.000')])
def test_timestamps(seconds,value):
    assert timestamp(seconds)==value
    assert timestamp(seconds,True)==value.replace('.',',')

def test_passwords_secure_policy():
    out=passwords(length=24,count=30)
    assert len(set(out))==30
    for value in out:
        assert len(value)==24
        assert any(c.islower() for c in value)
        assert any(c.isupper() for c in value)
        assert any(c.isdigit() for c in value)
        assert any(c in '!@#$%^-_=+[]{}?' for c in value)
        assert not any(c in 'Il1O0o' for c in value)

@pytest.mark.parametrize('kwargs',[{'length':1},{'length':300},{'count':0},{'symbols':'\n'},{'count':51}])
def test_password_invalid(kwargs):
    with pytest.raises(ValueError):passwords(**kwargs)

def test_lowercase_only():
    p=passwords(upper=False,numbers=False,symbols='',exclude_ambiguous=False)[0]
    assert p.islower() and p.isalpha()

def test_passwords_not_persisted(client):
    before=client.get('/api/state').json()['jobs']
    r=client.post('/api/passwords',json={})
    assert r.status_code==200
    secret=r.json()['passwords'][0]
    assert r.json()['persisted'] is False
    assert before==client.get('/api/state').json()['jobs']
    for p in client.app.state.store.data.glob('forge.sqlite3*'):
        assert secret.encode() not in p.read_bytes()

@pytest.mark.parametrize('route',['/api/state','/api/sources?ids=x','/api/jobs/fake/outputs/0'])
def test_auth_required(client,route):
    r=client.get(route,headers={'X-Forge-Token':''})
    assert r.status_code==401

def test_bad_token(client):
    assert client.get('/api/state',headers={'X-Forge-Token':'bad'}).status_code==401

@pytest.mark.parametrize('host',['evil.test:9876','127.0.0.1.attacker.test:9876','localhost:9876'])
def test_host_guard(client,host):
    assert client.get('/api/state',headers={'Host':host}).status_code==403

def test_origin_guard(client):
    assert client.post('/api/passwords',json={},headers={'Origin':'https://evil.example'}).status_code==403

def test_fetch_site_guard(client):
    assert client.get('/api/state',headers={'Sec-Fetch-Site':'cross-site'}).status_code==403

def test_preflight_blocked(client):
    assert client.options('/api/passwords').status_code==403

def test_security_headers(client):
    r=client.get('/')
    assert r.status_code==200
    assert r.headers['cache-control']=='no-store'
    assert "frame-ancestors 'none'" in r.headers['content-security-policy']
    assert r.headers['x-content-type-options']=='nosniff'
    assert TOKEN not in r.text

@pytest.mark.parametrize('route',['/assets/server.py','/assets/../forge/core.py','/original_scripts/genpass.bat','/docs','/openapi.json'])
def test_no_arbitrary_static_access(client,route):
    assert client.get(route).status_code==404

def test_real_static_assets(client):
    for name in ['app.js','style.css']:
        assert client.get('/assets/'+name).status_code==200

def test_source_import(client,tmp_path):
    p=tmp_path/'spaces & quotes — 文件.wav';tone(p)
    r=client.post('/api/register',json={'paths':[str(p)]})
    assert r.status_code==200
    source=r.json()['sources'][0]
    assert source['mode']=='native' and source['path']==str(p)
    q=client.post('/api/jobs',json={'tool':'retune','sources':[source['id']],'options':{},'output':{'mode':'source'}})
    assert q.status_code==200,q.text
    j=client.app.state.store.job(q.json()['jobs'][0]['id'])
    assert j['payload']['destination']==str(tmp_path)

def test_browser_staging_cannot_claim_source_folder(client):
    r=client.post('/api/upload?name=drop.wav',content=b'not-real-wav',headers={'Content-Type':'application/octet-stream'})
    assert r.status_code==200
    source=r.json()['source']
    assert source['mode']=='staged'
    r=client.post('/api/jobs',json={'tool':'retune','sources':[source['id']],'output':{'mode':'source'}})
    assert r.status_code==400 and 'original folder' in r.text.lower()

def test_empty_upload_rejected(client):
    assert client.post('/api/upload?name=empty.wav',content=b'').status_code==400

def test_executable_upload_rejected(client):
    assert client.post('/api/upload?name=tool.bat',content=b'echo foo').status_code==400

def test_register_batch_all_or_nothing(client,tmp_path):
    p=tmp_path/'existing.wav';tone(p)
    r=client.post('/api/register',json={'paths':[str(p),str(tmp_path/'missing.wav')]})
    assert r.status_code==400
    assert client.app.state.store.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]==0

@pytest.mark.parametrize('url',['file:///etc/passwd','javascript:alert(1)','https://user:password@example.com/v','not-a-url'])
def test_download_url_validation(client,tmp_path,url):
    r=client.post('/api/jobs',json={'tool':'download_audio','urls':[url],'output':{'mode':'custom','directory':str(tmp_path/'out')}})
    assert r.status_code==400

def test_unregistered_command_blocked(client,tmp_path):
    r=client.post('/api/jobs',json={'tool':'cmd','options':{'command':'anything'}})
    assert r.status_code==422

def test_extra_options_blocked(client,tmp_path):
    r=client.post('/api/jobs',json={'tool':'qr','options':{'text':'test','shell':'cmd'},'output':{'mode':'custom','directory':str(tmp_path)}})
    assert r.status_code==400

def test_unknown_source(client):
    r=client.post('/api/jobs',json={'tool':'retune','sources':['not-found']})
    assert r.status_code==400

def test_custom_subfolder_traversal(client,tmp_path):
    r=client.post('/api/jobs',json={'tool':'qr','options':{'text':'test'},'output':{'mode':'custom','directory':str(tmp_path),'subfolder':'../escape'}})
    assert r.status_code==400

def test_settings_validation(client):
    assert client.post('/api/settings',json={'workers':9}).status_code==422

def test_settings_preserve_queue_pause(client):
    assert client.post('/api/queue',json={'paused':True}).status_code==200
    client.post('/api/settings',json={'workers':1})
    assert client.get('/api/state').json()['settings']['queue_paused'] is True

def test_saved_presets(client):
    r=client.post('/api/presets',json={'tool':'retune','name':'Studio','options':{'format':'flac'},'output':{'mode':'source'}})
    assert r.status_code==200
    assert client.get('/api/state').json()['presets'][0]['options']['format']=='flac'

def test_preset_does_not_store_cookie_path(client):
    client.post('/api/presets',json={'tool':'download_audio','name':'Audio','options':{'cookies':'C:\\secret-cookies.txt'},'output':{'mode':'default'}})
    assert client.get('/api/state').json()['presets'][0]['options']['cookies']==''

def test_recovery_marks_interrupted(tmp_path):
    s=Store(tmp_path)
    j=s.add_job('qr','old',{'destination':str(tmp_path)})
    s.update_job(j['id'],status='running');s.close()
    s=Store(tmp_path)
    assert s.job(j['id'])['status']=='interrupted'
    assert s.settings()['queue_paused'] is True
    s.close()

def test_queue_pause_cancel_retry(live,tmp_path):
    live.post('/api/queue',json={'paused':True})
    j=submit_qr(live,tmp_path/'out')
    time.sleep(.7)
    assert live.app.state.store.job(j['id'])['status']=='queued'
    assert live.post('/api/jobs/'+j['id']+'/cancel',json={}).status_code==200
    r=live.post('/api/jobs/'+j['id']+'/retry',json={})
    assert r.status_code==200 and r.json()['id']!=j['id']
    live.post('/api/queue',json={'paused':False})
    result=wait_job(live,r.json()['id'])
    assert result['status']=='completed',result
    assert Path(result['outputs'][0]['path']).is_file()

@pytest.mark.parametrize('fmt',['png','svg'])
def test_qr_real_worker(live,tmp_path,fmt):
    j=submit_qr(live,tmp_path/'out',fmt=fmt)
    result=wait_job(live,j['id'])
    assert result['status']=='completed',result
    output=Path(result['outputs'][0]['path'])
    assert output.suffix=='.'+fmt
    if fmt=='png':
        with Image.open(output) as image:assert image.size[0]>100
    else:assert '<svg' in output.read_text()
    r=live.get(f'/api/jobs/{j["id"]}/outputs/0')
    assert r.status_code==200 and r.content==output.read_bytes()


def test_concurrent_collision_preserves_outputs(live,tmp_path):
    jobs=[submit_qr(live,tmp_path/'out',name='same') for _ in range(4)]
    results=[wait_job(live,j['id']) for j in jobs]
    assert all(r['status']=='completed' for r in results),results
    paths=[r['outputs'][0]['path'] for r in results]
    assert len(set(paths))==4
    assert len(list((tmp_path/'out').glob('*.png')))==4


def test_publish_no_overwrite(tmp_path):
    final=tmp_path/'output.txt';final.write_text('original')
    stage=tmp_path/'stage.txt';stage.write_text('new')
    target=publish(stage,final)
    assert final.read_text()=='original'
    assert target.name=='output (1).txt' and target.read_text()=='new'


def test_export_transcripts(tmp_path):
    segments=[{'start':1.25,'end':2.75,'text':'Hello world.','words':[{'start':1.25,'end':1.6,'word':'Hello','probability':.9},{'start':1.7,'end':2.75,'word':'world.','probability':.8}]}]
    options=Transcribe(timestamps='word',formats=['txt','srt','vtt','json']).model_dump()
    outputs=export_transcript(segments,{'language':'en'},options,tmp_path,'sample')
    assert len(outputs)==4
    assert '[00:00:01.250] Hello' in outputs[0].read_text()
    assert '00:00:01,250 --> 00:00:02,750' in outputs[1].read_text()
    assert outputs[2].read_text().startswith('WEBVTT\n\n')
    assert json.loads(outputs[3].read_text())['segments'][0]['words'][1]['word']=='world.'


def test_txt_no_timestamp_does_not_disable_srt(tmp_path):
    o=Transcribe(timestamps='none',formats=['txt','srt']).model_dump()
    export_transcript([{'start':1,'end':2,'text':'Sample.'}],{},o,tmp_path,'x')
    assert (tmp_path/'x.txt').read_text()=='Sample.\n'
    assert '00:00:01,000 -->' in (tmp_path/'x.srt').read_text()


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg not installed')
@pytest.mark.parametrize('rate',[44100,48000])
def test_real_retune_frequency_and_original(rate,tmp_path):
    import numpy as np
    source=tone(tmp_path/f'tone-{rate}.wav',rate,4)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    output=tmp_path/'retuned.wav'
    retune(source,output,Retune(format='wav').model_dump(),{})
    raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(output),'-f','f32le','-ac','1','-ar',str(rate),'pipe:1'])
    data=np.frombuffer(raw,dtype='<f4')
    middle=data[rate:-rate]
    spectrum=np.abs(np.fft.rfft(middle*np.hanning(len(middle))))
    peak=np.fft.rfftfreq(len(middle),1/rate)[spectrum.argmax()]
    assert abs(peak-432)<1,(rate,peak)
    assert abs(len(data)/rate-4)<.1
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg not installed')
def test_media_metadata_removal_preserves_source(tmp_path):
    source=tmp_path/'tagged.mp3'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=1','-metadata','title=Sensitive Title','-metadata','artist=Original Artist',str(source)],check=True)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    clean=tmp_path/'clean.mp3'
    before,after,warnings=wipe(source,clean,{})
    assert before['format_tags']['title']=='Sensitive Title'
    assert 'Sensitive Title' not in json.dumps(after)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
    assert probe(clean,{})['streams'][0]['codec_name']=='mp3'


def test_image_metadata_removal_preserves_original(tmp_path):
    source=tmp_path/'tagged.png';im=Image.new('RGB',(64,48),(100,50,25));meta=PngImagePlugin.PngInfo();meta.add_text('Author','Private Person');im.save(source,pnginfo=meta)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    out=tmp_path/'clean.png'
    before,after,notes=wipe(source,out,{})
    assert before['info']['Author']=='Private Person'
    assert not after['info']
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
    with Image.open(out) as image:assert image.size==(64,48)


def test_pdf_sanitization_rejected(tmp_path):
    source=tmp_path/'document.pdf';source.write_text('%PDF-1.7')
    with pytest.raises(RuntimeError):wipe(source,tmp_path/'clean.pdf',{})


def test_animated_image_rejected(tmp_path):
    p=tmp_path/'animated.webp';Image.new('RGB',(10,10),'red').save(p,save_all=True,append_images=[Image.new('RGB',(10,10),'blue')],duration=100,loop=0)
    with pytest.raises(RuntimeError):wipe(p,tmp_path/'out.webp',{})


def test_changed_source_fails_without_output(live,tmp_path):
    p=tmp_path/'sample.png';Image.new('RGB',(8,8),'white').save(p)
    live.post('/api/queue',json={'paused':True})
    source=live.post('/api/register',json={'paths':[str(p)]}).json()['sources'][0]
    r=live.post('/api/jobs',json={'tool':'metadata','sources':[source['id']],'output':{'mode':'source'}})
    id=r.json()['jobs'][0]['id']
    p.write_bytes(b'changed')
    live.post('/api/queue',json={'paused':False})
    j=wait_job(live,id)
    assert j['status']=='failed' and 'source changed' in j['message'].lower()
    assert not j['outputs']


def test_staging_cleanup_keeps_pending_and_native(client,tmp_path):
    native=tmp_path/'native.wav';tone(native)
    client.post('/api/register',json={'paths':[str(native)]})
    staged=client.post('/api/upload?name=staged.wav',content=b'wave').json()['source']
    unused=client.post('/api/upload?name=unused.wav',content=b'wave').json()['source']
    client.post('/api/jobs',json={'tool':'retune','sources':[staged['id']],'output':{'mode':'custom','directory':str(tmp_path/'out')}})
    r=client.post('/api/staging/clear',json={})
    assert r.status_code==200
    assert Path(staged['path']).is_file() and native.is_file()
    assert not Path(unused['path']).exists()


def test_cancellation_kills_child_process(client,tmp_path):
    import psutil
    store=client.app.state.store;manager=client.app.state.manager
    j=store.add_job('qr','cancellation test',{'destination':str(tmp_path)})
    child_pid_file=tmp_path/'child.txt'
    code="import subprocess,sys,time,pathlib;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);pathlib.Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)"
    p=subprocess.Popen([sys.executable,'-c',code,str(child_pid_file)])
    try:
        for _ in range(100):
            if child_pid_file.exists():break
            time.sleep(.02)
        child=int(child_pid_file.read_text())
        store.update_job(j['id'],status='running')
        manager.active[j['id']]=p
        manager.cancel(j['id'])
        assert p.poll() is not None
        if psutil.pid_exists(child):assert psutil.Process(child).status()==psutil.STATUS_ZOMBIE
    finally:
        manager.active.clear()
        if p.poll() is None:p.kill()


def test_private_audit_opt_in_only(live,tmp_path):
    p=tmp_path/'image.png';Image.new('RGB',(16,16),'white').save(p)
    sid=live.post('/api/register',json={'paths':[str(p)]}).json()['sources'][0]['id']
    for wanted in [False,True]:
        r=live.post('/api/jobs',json={'tool':'metadata','sources':[sid],'options':{'audit_report':wanted},'output':{'mode':'source'}})
        j=wait_job(live,r.json()['jobs'][0]['id'])
        assert j['status']=='completed',j
        assert len(j['outputs'])==(2 if wanted else 1)
        assert p.is_file()


def test_palette_png_preserves_color_and_alpha(tmp_path):
    p=tmp_path/'palette.png';o=tmp_path/'clean.png'
    im=Image.new('P',(8,8),1);im.putpalette([255,0,0,0,255,0]+[0]*762)
    im.putpixel((0,0),0);im.save(p,transparency=0)
    with Image.open(p) as before: expected=before.convert('RGBA').tobytes()
    wipe(p,o,{})
    with Image.open(o) as after: assert after.convert('RGBA').tobytes()==expected


def test_no_speech_exports(tmp_path):
    o=Transcribe(formats=['txt','srt','vtt','json']).model_dump()
    files=export_transcript([],{},o,tmp_path,'silence')
    assert len(files)==4
    assert (tmp_path/'silence.txt').read_text()=='[No speech detected]\n'
    assert (tmp_path/'silence.srt').stat().st_size==0
    final=publish(tmp_path/'silence.srt',tmp_path/'finished.srt',allow_empty=True)
    assert final.is_file()

@pytest.mark.parametrize('gpu,fail_gpu',[(False,False),(True,False),(True,True)])
def test_transcription_adapter_contract(tmp_path,monkeypatch,gpu,fail_gpu):
    import types
    from forge.worker import transcribe
    calls=[]
    class Model:
        def __init__(self,name,**kw):
            calls.append((name,kw))
            if fail_gpu and kw['device']=='cuda': raise RuntimeError('CUDA unavailable')
        def transcribe(self,path,**kw):
            assert kw['word_timestamps'] and kw['initial_prompt']=='FORGE'
            segment=types.SimpleNamespace(start=.2,end=1.2,text=' Test words.',avg_logprob=-.2,no_speech_prob=.01,
                words=[types.SimpleNamespace(start=.2,end=.6,word=' Test',probability=.98)])
            return iter([segment]),types.SimpleNamespace(language='en',language_probability=.99,duration=2)
    monkeypatch.setitem(sys.modules,'faster_whisper',types.SimpleNamespace(WhisperModel=Model))
    monkeypatch.setitem(sys.modules,'ctranslate2',types.SimpleNamespace(get_cuda_device_count=lambda:int(gpu)))
    monkeypatch.setenv('HF_HUB_OFFLINE','1')
    o=Transcribe(model='base',timestamps='word',vocabulary='FORGE',formats=['txt','json']).model_dump()
    files,warnings=transcribe(tmp_path/'fixture.wav',tmp_path,'test',o,tmp_path)
    assert len(files)==2 and calls[0][1]['local_files_only']
    assert calls[-1][1]['device']==('cpu' if fail_gpu or not gpu else 'cuda')
    assert bool(warnings)==fail_gpu
    assert '[00:00:00.200] Test' in files[0].read_text()

@pytest.mark.parametrize('tool,fmt',[('download_audio','mp3'),('download_video','mkv')])
def test_download_adapter_contract(tmp_path,monkeypatch,tool,fmt):
    import types
    from forge.models import Download
    from forge.worker import download
    seen={}
    class YDL:
        def __init__(self,p): seen.update(p)
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def extract_info(self,url,download):
            assert url=='https://example.org/test' and download
            (tmp_path/f'media.{fmt}').write_bytes(b'mocked media only')
    monkeypatch.setitem(sys.modules,'yt_dlp',types.SimpleNamespace(YoutubeDL=YDL))
    monkeypatch.setattr('forge.worker.probe',lambda *a:{'streams':[{'codec_type':'audio'}]})
    monkeypatch.setattr('forge.worker.executable',lambda n,s:'/usr/bin/'+n)
    files,_=download('https://example.org/test',tmp_path,Download(format=fmt).model_dump(),{},tool)
    assert len(files)==1 and seen['noplaylist'] and not seen['overwrites']
    assert seen['remote_components']==[] and seen['cachedir'] is False
    assert seen['match_filter']({'is_live':True})
    assert not any('exec' in str(p).lower() for p in seen['postprocessors'])
