"""Owned real media, bounded measurement and saved-output API integrity."""
from pathlib import Path
import math
import os
import struct
import subprocess
import wave

from fastapi.testclient import TestClient
import pytest

from forge.app import create_app
from forge import dubbing_media
from test_dubbing_queue_v10 import fixture_video, post


def tone(path):
    with wave.open(str(path),'wb') as handle:
        handle.setnchannels(1);handle.setsampwidth(2);handle.setframerate(24000)
        handle.writeframes(struct.pack('<24000h',*[int(10000*math.sin(2*math.pi*200*i/24000)) for i in range(24000)]))
    return path


def test_real_waveform_has_bounded_measured_peak_bins(tmp_path):
    result=dubbing_media.waveform(tone(tmp_path/'owned.wav'),{},max_points=64)
    assert result['points']==len(result['peaks'])==64
    assert result['duration']==pytest.approx(1)
    assert .29<max(result['peaks'])<.32
    assert result['sample_rate_preview']==8000


def test_waveform_deadline_covers_decoder_reads(monkeypatch,tmp_path):
    monkeypatch.setattr(dubbing_media,'_probe',lambda *args:{'duration':1})
    monkeypatch.setattr(dubbing_media,'executable',lambda *args:'ffmpeg')
    def stalled(*args,**kwargs):
        assert kwargs['timeout']==45 and kwargs.get('capture_output')
        raise subprocess.TimeoutExpired(args[0],45)
    monkeypatch.setattr(dubbing_media.subprocess,'run',stalled)
    with pytest.raises(ValueError,match='time limit'):
        dubbing_media.waveform(tmp_path/'fixture.wav',{})


def test_saved_media_and_native_actions_reject_changed_verified_file(tmp_path,monkeypatch):
    app=create_app(tmp_path/'data','owned-media-test-token',19951,workers=False)
    with TestClient(app,base_url='http://127.0.0.1:19951',headers={'x-forge-token':'owned-media-test-token'}) as client:
        video=fixture_video(tmp_path/'owned.mp4')
        project=post(client,'/api/dubbing/import',{'path':str(video)})['project']
        stem=tone(tmp_path/'owned.wav')
        artifact=post(client,'/api/dubbing/stems/import',{'project_id':project['id'],'path':str(stem),'kind':'music'})['artifact']
        query={'project_id':project['id'],'artifact_id':artifact['id']}
        assert client.get('/api/dubbing/media',params=query).content==stem.read_bytes()
        from forge import queue_api
        monkeypatch.setattr(queue_api,'open_media_output',lambda job,index:{'opened':False,'path':job['outputs'][index]['path']})
        assert post(client,'/api/dubbing/open',query)['path']==str(stem)
        if os.name!='nt':
            assert post(client,'/api/dubbing/open',{**query,'reveal':True})['opened'] is False
        stem.write_bytes(stem.read_bytes()+b'changed owned fixture')
        refused=client.get('/api/dubbing/media',params=query)
        assert refused.status_code==400 and 'changed' in refused.text
        assert client.post('/api/dubbing/open',json=query).status_code==400
        assert video.is_file()


def test_new_endpoints_enforce_existing_offline_entitlement_boundary(tmp_path):
    app=create_app(tmp_path/'data','owned-entitlement-token',19952,workers=False)
    class Denied:
        def allows(self,feature_id): return False
    app.state.entitlements.provider=Denied()
    with TestClient(app,base_url='http://127.0.0.1:19952',headers={'x-forge-token':'owned-entitlement-token'}) as client:
        for route in ['/api/dubbing/state','/api/language-publishing/state']:
            assert client.get(route).status_code==403
        assert not app.state.store.jobs()
