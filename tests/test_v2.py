from __future__ import annotations
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from forge.app import create_app
from forge.auth import BrowserSessions
from forge.core import Store
from forge.models import Transcribe, Metadata, Retune, Download, Workflow
from forge.queue import QueueManager
from forge.transcripts import export_transcript, sentence_segments, assign_speakers, subtitle_cues, standalone_html
from forge.worker import probe, edit_metadata, retune, download
from test_forge import client, live, submit_qr, wait_job, tone, BASE, TOKEN


def authorize(client):
    ticket=client.post('/api/launch-ticket').json()['ticket']
    client.headers.pop('X-Forge-Token',None)
    response=client.post('/api/session',json={'ticket':ticket},headers={'X-Forge-Bootstrap':'1'})
    assert response.status_code==200,response.text
    return response


def test_browser_cookie_new_tab_and_csrf(client):
    response=authorize(client)
    cookie=response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie and 'Max-Age=7776000' in cookie
    assert client.get('/api/state').status_code==200
    assert client.get('/api/session').json()['csrf']==response.json()['csrf']
    assert client.post('/api/passwords',json={}).status_code==403
    assert client.post('/api/passwords',json={},headers={'X-Forge-CSRF':response.json()['csrf']}).status_code==200
    assert client.get('/api/state',headers={'Origin':'https://attacker.invalid'}).status_code==403
    assert client.get('/api/state',headers={'Host':'localhost:9876'}).status_code==403
    assert TOKEN not in client.get('/api/state').text
    stored=client.app.state.store.get('browser_sessions')
    value=next(iter(client.cookies.values()))
    assert value not in json.dumps(stored)
    assert hashlib.sha256(value.encode()).hexdigest() in stored


def test_new_client_and_restart_keep_browser_authorization(tmp_path):
    data=tmp_path/'data';a=create_app(data,TOKEN,9876,workers=False)
    with TestClient(a,base_url=BASE,headers={'X-Forge-Token':TOKEN}) as c:
        c.app=a;response=authorize(c);cookies=dict(c.cookies);csrf=response.json()['csrf']
    a.state.store.close()
    # Same browser cookie, different service token, no launcher parameters or JS storage.
    b=create_app(data,'new-runtime-secret',9876,workers=False)
    with TestClient(b,base_url=BASE,cookies=cookies) as other:
        assert other.get('/api/state').status_code==200
        assert other.get('/api/session').json()['csrf']==csrf
    b.state.store.close()


def test_launcher_reopen_does_not_break_existing_tabs(client):
    r=authorize(client);before=dict(client.cookies)
    t=client.post('/api/launch-ticket',headers={'X-Forge-Token':TOKEN}).json()['ticket']
    r2=client.post('/api/session',json={'ticket':t},headers={'X-Forge-Bootstrap':'1'})
    assert r.json()['csrf']==r2.json()['csrf']
    assert dict(client.cookies)==before
    assert client.post('/api/passwords',json={},headers={'X-Forge-CSRF':r.json()['csrf']}).status_code==200


def test_ticket_one_use_expiry_and_forget(client):
    r=authorize(client);csrf=r.json()['csrf']
    assert client.post('/api/session',json={'ticket':'invented'},headers={'X-Forge-Bootstrap':'1'}).status_code==401
    assert client.post('/api/session',json=[],headers={'X-Forge-Bootstrap':'1'}).status_code==400
    assert client.post('/api/session/forget',json={},headers={'X-Forge-CSRF':csrf}).status_code==200
    assert client.get('/api/state').status_code==401
    sessions=BrowserSessions(client.app.state.store,9876)
    t=sessions.ticket();assert sessions.consume(t);assert not sessions.consume(t)
    t=sessions.ticket();sessions.tickets[sessions.digest(t)]=time.time()-1
    assert not sessions.consume(t)
    assert not sessions.consume(None)


def test_expired_cookie_is_rejected(client):
    authorize(client)
    records=client.app.state.store.get('browser_sessions')
    for record in records.values():record['expires']=0
    client.app.state.store.set('browser_sessions',records)
    assert client.get('/api/state').status_code==401


def test_resources_and_queue_order(client,tmp_path):
    a=submit_qr(client,tmp_path,'a');b=submit_qr(client,tmp_path,'b');c=submit_qr(client,tmp_path,'c')
    assert client.post(f'/api/jobs/{c["id"]}/order',json={'direction':'first'}).status_code==200
    assert [j['id'] for j in client.app.state.store.jobs(status='queued')]==[c['id'],a['id'],b['id']]
    assert QueueManager.resource({'tool':'transcribe','payload':{'options':{'device':'auto'}}})=='gpu'
    assert QueueManager.resource({'tool':'transcribe','payload':{'options':{'device':'cpu'}}})=='cpu'
    assert QueueManager.resource({'tool':'download_audio'})=='download'
    r=client.post('/api/jobs/batch-action',json={'action':'cancel','ids':[a['id'],b['id']]})
    assert all(i['ok'] for i in r.json()['results'])
    assert client.post(f'/api/jobs/{a["id"]}/order',json={'direction':'first'}).status_code==400


def test_scheduler_respects_resource_limit(tmp_path,monkeypatch):
    store=Store(tmp_path/'data');settings=store.settings();settings.update(workers=6,cpu_workers=1,gpu_workers=1,download_workers=1);store.set('settings',settings)
    q=QueueManager(store);launched=[]
    for tool,options in [('qr',{}),('qr',{}),('transcribe',{'device':'auto'}),('transcribe',{'device':'auto'}),('download_audio',{}),('download_video',{})]:
        store.add_job(tool,tool,{'tool':tool,'options':options})
    def fake_launch(job):
        q.active[job['id']]=object();store.update_job(job['id'],status='running');launched.append(job)
    monkeypatch.setattr(q,'launch',fake_launch)
    q.start();time.sleep(1.3);q.stop_event.set();q.scheduler.join(2)
    assert len(launched)==3
    assert sorted(q.resource(j) for j in launched)==['cpu','download','gpu']
    store.close()


def register(client,path):
    r=client.post('/api/register',json={'paths':[str(path)]});assert r.status_code==200,r.text
    return r.json()['sources'][0]['id']


def workflow_body(client,source,folder):
    return {'name':'Retune then privacy','sources':[register(client,source)],'steps':[{'tool':'retune','options':{'format':'wav'}},{'tool':'metadata','options':{'audit_report':True}}],'output':{'mode':'custom','directory':str(folder)}}


def test_real_workflow_routes_published_media_and_retries(live,tmp_path):
    source=tone(tmp_path/'voice.wav',seconds=.7);original=source.read_bytes()
    r=live.post('/api/workflows/run',json=workflow_body(live,source,tmp_path/'out'));assert r.status_code==200,r.text
    jobs=r.json()['jobs'];assert len(jobs)==2
    a=wait_job(live,jobs[0]['id']);b=wait_job(live,jobs[1]['id'])
    assert a['status']==b['status']=='completed',(a,b)
    payload=live.app.state.store.job(b['id'])['payload']
    assert payload['source']['path']==a['outputs'][0]['path']
    assert Path(b['outputs'][0]['path']).exists()
    assert source.read_bytes()==original
    retry=live.post('/api/batches/'+r.json()['batch_id']+'/retry',json={})
    assert retry.status_code==200,retry.text
    new=retry.json()['jobs'];assert new[1]['depends_on']==new[0]['id']
    assert new[0]['batch_id']!=a['batch_id']
    assert wait_job(live,new[0]['id'])['status']=='completed'
    end=wait_job(live,new[1]['id']);assert end['status']=='completed'
    assert end['outputs'][0]['path']!=b['outputs'][0]['path']
    assert source.read_bytes()==original


def test_failed_workflow_blocks_dependents(live,tmp_path):
    source=tmp_path/'broken.wav';source.write_bytes(b'not media')
    r=live.post('/api/workflows/run',json=workflow_body(live,source,tmp_path/'out'))
    assert r.status_code==200
    a,b=r.json()['jobs'];assert wait_job(live,a['id'])['status']=='failed'
    for _ in range(40):
        child=live.app.state.store.job(b['id'])
        if child['status']=='blocked':break
        time.sleep(.15)
    assert child['status']=='blocked'
    assert not child['outputs']
    assert live.post('/api/jobs/'+b['id']+'/retry',json={}).status_code==400


@pytest.mark.parametrize('steps',[
    [{'tool':'transcribe'},{'tool':'metadata'}],
    [{'tool':'metadata','options':{'mode':'edit','tags':{'title':'x'}}}],
    [{'tool':'download_audio'}],
])
def test_invalid_workflow_creates_no_jobs(client,tmp_path,steps):
    source=tone(tmp_path/'sample.wav',seconds=.1)
    body={'name':'Invalid','sources':[register(client,source)],'steps':steps,'output':{'mode':'custom','directory':str(tmp_path/'out')}}
    assert client.post('/api/workflows/run',json=body).status_code in {400,422}
    assert not client.app.state.store.jobs()


def test_incomplete_retry_graph_has_no_partial_jobs(client,tmp_path):
    store=client.app.state.store
    a=store.add_job('qr','fixture',{'batch_id':'broken','depends_on':'missing','options':{}})
    store.update_job(a['id'],status='failed')
    with pytest.raises(ValueError):client.app.state.manager.retry_batch('broken')
    assert len(store.jobs())==1


def test_workflow_preset_roundtrip_and_favorites(client):
    body=Workflow(name='Saved',steps=[{'tool':'transcribe','options':{'timestamps':'sentence','formats':['csv','html']}}]).model_dump()
    assert client.post('/api/workflows/save',json=body).status_code==200
    assert client.get('/api/state').json()['workflows'][0]==body
    assert client.post('/api/favorites',json={'tool':'transcribe','pinned':True}).json()['favorites']==['transcribe']
    assert client.post('/api/favorites',json={'tool':'shell','pinned':True}).status_code==400


def sample_segments():
    return [{'start':0.,'end':2.,'text':'Um, hello world. Again!','avg_logprob':-1.2,'no_speech_prob':.7,'words':[
        {'start':0.,'end':.25,'word':'Um,','probability':.2},
        {'start':.3,'end':.7,'word':' hello','probability':.95},
        {'start':.75,'end':1.2,'word':' world.','probability':.95},
        {'start':1.3,'end':2.,'word':' Again!','probability':.95}]}]


def test_transcript_clean_raw_exports_and_safe_html(tmp_path):
    options=Transcribe(style='clean',timestamps='sentence',formats=['txt','srt','vtt','json','csv','html']).model_dump()
    raw=sample_segments();files=export_transcript(raw,{'language':'en'},options,tmp_path,'sample')
    assert len(files)==8
    assert 'Um,' in (tmp_path/'sample_raw.json').read_text()
    preserved=json.loads((tmp_path/'sample_raw.json').read_text())
    assert len(preserved['segments'])==1 and preserved['segments'][0]['text']==raw[0]['text']
    cleaned=json.loads((tmp_path/'sample_clean.json').read_text())
    assert 'Um,' not in ''.join(s['text'] for s in cleaned['segments'])
    assert len(cleaned['segments'])==2
    assert cleaned['segments'][0]['start']==.3
    assert len(cleaned['segments'][0]['review_flags'])==3
    assert raw==sample_segments()
    rows=list(csv.reader(io.StringIO((tmp_path/'sample_clean.csv').read_text())))
    assert rows[0]==['start','end','speaker','text','review_flags']
    assert 'WEBVTT' in (tmp_path/'sample_clean.vtt').read_text()
    hostile=[{'start':0,'end':1,'text':'</script><script>fetch("https://evil.invalid")</script>'}]
    html=standalone_html(hostile,{},'<hostile>')
    assert '\\u003c/script\\u003e' in html and 'connect-src \'none\'' in html
    assert '<script>fetch(' not in html and '<hostile>' not in html


def test_csv_formula_safe_and_speaker_overlap(tmp_path):
    raw=[{'start':0,'end':1,'text':'=2+2'}]
    o=Transcribe(formats=['csv']).model_dump();export_transcript(raw,{},o,tmp_path,'formula')
    row=list(csv.reader(io.StringIO((tmp_path/'formula.csv').read_text())))[1]
    assert row[3]=="'=2+2"
    labeled=assign_speakers(sample_segments(),[{'start':0,'end':.72,'speaker':'Speaker 1'},{'start':.73,'end':2,'speaker':'Speaker 2'}])
    assert labeled[0]['words'][1]['speaker']=='Speaker 1'
    assert labeled[0]['words'][2]['speaker']=='Speaker 2'
    sentences=sentence_segments(labeled)
    assert len(sentences)==3
    assert sample_segments()[0].get('speaker') is None


def test_subtitle_cues_split_by_width_and_time():
    words=[{'word':' caption','start':i*.25,'end':(i+1)*.25} for i in range(30)]
    cues=subtitle_cues([{'start':0,'end':7.5,'text':'caption '*30,'words':words}],{'subtitle_chars':20,'subtitle_lines':2,'subtitle_seconds':2})
    assert len(cues)>2
    for c in cues:
        assert 0<c['end']-c['start']<=2
        assert len(c['caption'].splitlines())<=2
        assert max(map(len,c['caption'].splitlines()))<=20


def test_real_media_tag_edit_selective_and_source_integrity(tmp_path):
    wav=tone(tmp_path/'tone.wav',seconds=.5);source=tmp_path/'tagged.flac'
    subprocess.run(['ffmpeg','-v','error','-i',str(wav),'-metadata','title=Original title','-metadata','artist=Original artist','-metadata','comment=Private comment',str(source)],check=True)
    original=source.read_bytes();edited=tmp_path/'edited.flac'
    o=Metadata(mode='edit',remove_tags=['comment'],tags={'title':'New title'}).model_dump()
    before,after,warnings=edit_metadata(source,edited,o,{})
    tags={k.lower():v for k,v in after['format_tags'].items()}
    assert tags['title']=='New title' and tags['artist']=='Original artist' and 'comment' not in tags
    assert source.read_bytes()==original
    assert probe(edited,{})['streams'][0]['codec_name']=='flac'


def test_real_normalized_wav_depth(tmp_path):
    source=tone(tmp_path/'tone.wav',seconds=1);out=tmp_path/'normalized.wav'
    notes=retune(source,out,Retune(format='wav',wav_bits=16,normalize=True).model_dump(),{})
    stream=probe(out,{})['streams'][0]
    assert stream['bits_per_sample']==16 and stream['sample_rate']=='44100'
    assert any('Single-pass' in n for n in notes)


def test_metadata_inspection_readonly(client,tmp_path):
    from PIL import Image,PngImagePlugin
    p=tmp_path/'sample.png';info=PngImagePlugin.PngInfo();info.add_text('Author','Private')
    Image.new('RGB',(12,12)).save(p,pnginfo=info);before=p.read_bytes()
    r=client.post('/api/metadata/inspect',json={'source':register(client,p)})
    assert r.status_code==200 and 'Private' in r.text
    assert before==p.read_bytes() and not client.app.state.store.jobs()


def test_playlist_subtitle_adapter_parameters(tmp_path,monkeypatch):
    import forge.worker as worker
    seen={}
    class YDL:
        def __init__(self,opts):seen.update(opts)
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def extract_info(self,url,download):
            (tmp_path/'item.mp3').write_bytes(b'mock media')
            (tmp_path/'item.en.vtt').write_text('WEBVTT\n')
    monkeypatch.setitem(sys.modules,'yt_dlp',types.SimpleNamespace(YoutubeDL=YDL))
    monkeypatch.setattr(worker,'probe',lambda *_:{'streams':[{'codec_type':'audio'}]})
    o=Download(playlist=True,playlist_start=2,playlist_end=5,subtitles=True,auto_subtitles=True,subtitle_languages='en,es',filename_style='playlist_index').model_dump()
    files,_=download('https://example.invalid/playlist',tmp_path,o,{},'download_audio')
    assert seen['playliststart']==2 and seen['playlistend']==5 and not seen['noplaylist']
    assert seen['writesubtitles'] and seen['writeautomaticsub'] and seen['subtitleslangs']==['en','es']
    assert '%(playlist_index)03d' in seen['outtmpl']
    assert {p.suffix for p in files}=={'.mp3','.vtt'}
    assert seen['remote_components']==[]


def test_model_download_needs_consent_and_engine(client):
    assert client.post('/api/environment/models/download',json={'model':'tiny','consent':False}).status_code==400
    assert client.post('/api/environment/models/download',json={'model':'arbitrary/url','consent':True}).status_code==422
    r=client.get('/api/environment/models');assert r.status_code==200
    assert len(r.json()['models'])==6


def test_diagnostics_are_readonly(client):
    r=client.post('/api/environment/diagnostics',json={});assert r.status_code==200,r.text
    assert r.json()['inference_tested'] is False
    assert client.get('/api/state').json()['diagnostics']==r.json()


def test_old_job_schema_unchanged_and_settings_defaults(tmp_path):
    store=Store(tmp_path/'data');store.set('settings',{'workers':3,'ffmpeg':'custom.exe','queue_paused':True})
    assert store.settings()['workers']==3 and store.settings()['cpu_workers']==2
    assert store.settings()['ffmpeg']=='custom.exe'
    cols=[r[1] for r in store.db.execute('PRAGMA table_info(jobs)')]
    assert cols==['id','tool','label','status','progress','message','payload','outputs','warnings','created','updated']
    store.close()


def test_source_media_authenticated_and_ranges(client,tmp_path):
    p=tone(tmp_path/'voice.wav',seconds=.1)
    source=client.app.state.store.register(p)
    j=client.app.state.store.add_job('transcribe','voice',{'source':source,'options':{}})
    assert client.get('/api/jobs/'+j['id']+'/media',headers={'X-Forge-Token':''}).status_code==401
    r=client.get('/api/jobs/'+j['id']+'/media',headers={'Range':'bytes=0-15'})
    assert r.status_code==206 and r.content==p.read_bytes()[:16]


@pytest.mark.parametrize('model,fields',[(Metadata,{'tags':{'unsupported':'x'}}),(Metadata,{'remove_tags':['-exec']}),(Retune,{'mp3_bitrate':512}),(Download,{'playlist':True,'playlist_start':1,'playlist_end':600}),(Transcribe,{'diarization':True}),(Transcribe,{'min_speakers':4,'max_speakers':2})])
def test_v2_options_reject_invalid(model,fields):
    with pytest.raises(ValueError):model(**fields)


def test_update_prepare_blocks_new_jobs_and_can_cancel(client,tmp_path):
    response=authorize(client)
    assert client.post('/api/update/prepare',json={},headers={'X-Forge-CSRF':response.json()['csrf']}).status_code==403
    native={'X-Forge-Token':TOKEN}
    result=client.post('/api/update/prepare',json={},headers=native)
    assert result.status_code==200
    assert client.app.state.manager.maintenance
    assert client.post('/api/jobs',json={'tool':'qr','options':{'text':'x'},'output':{'mode':'default'}},headers=native).status_code==409
    assert client.get('/api/state').status_code==200
    assert client.post('/api/update/cancel',json={},headers=native).status_code==200
    assert not client.app.state.manager.maintenance
    assert client.app.state.store.settings()['queue_paused']==result.json()['previous_settings']['queue_paused']


def test_update_prepare_refuses_active_or_queued_work(client,tmp_path):
    submit_qr(client,tmp_path)
    assert client.post('/api/update/prepare',json={}).status_code==400
    assert not client.app.state.manager.maintenance


def test_old_active_jobs_remain_visible_beyond_history_window(client):
    store=client.app.state.store
    with store.lock:
        rows=[]
        for i in range(510):
            status='queued' if i==0 else 'running' if i==1 else 'completed'
            rows.append((f'history-{i}','qr',str(i),status,0,'test','{}','[]','[]',float(i),float(i)))
        store.db.executemany('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)',rows)
        store.db.commit()
    response=client.get('/api/state')
    assert response.status_code==200
    jobs=response.json()['jobs'];ids={j['id'] for j in jobs}
    assert len(jobs)==502 and len(ids)==502
    assert 'history-0' in ids and 'history-1' in ids and 'history-2' not in ids
    assert response.json()['history_limit']==500
