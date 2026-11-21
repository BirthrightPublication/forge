"""Actual tagged-media import/export plumbing; translation boundary is synthetic."""
import copy
from pathlib import Path
import subprocess
import zipfile

from fastapi.testclient import TestClient
import pytest

from forge.app import create_app
from forge import dubbing_models, dubbing_pipeline
from test_dubbing_queue_v10 import fixture_video, post, wait_for_jobs


def test_imported_english_tags_reach_metadata_translation_and_survive_saves(tmp_path,monkeypatch):
    app=create_app(tmp_path/'data','owned-metadata-token',19953,workers=False)
    with TestClient(app,base_url='http://127.0.0.1:19953',headers={'x-forge-token':'owned-metadata-token'}) as client:
        original=fixture_video(tmp_path/'owned-original.mp4')
        tagged=tmp_path/'owned-tagged.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(original),'-c','copy',
            '-metadata','title=Owned English title','-metadata','description=Owned English description',str(tagged)],check=True)
        project=post(client,'/api/dubbing/import',{'path':str(tagged)})['project']
        assert project['source_metadata']['title']=='Owned English title'
        assert project['source']['metadata']==project['source_metadata']
        changed=copy.deepcopy(project);changed['name']='Edited local name';changed['source_metadata']={'title':'Browser cannot replace original tags'}
        saved=post(client,'/api/dubbing/projects',{'project':changed,'expected_revision':project['revision']})['project']
        assert saved['source_metadata']==project['source_metadata']
        assert client.post('/api/dubbing/stage',json={'project_id':project['id'],'stage':'metadata'}).status_code==400
        assert not app.state.store.jobs()
        calls=[]
        def synthetic_translation(component,data,request,progress=None):
            calls.append(copy.deepcopy(request))
            assert component=='opus-en-ru'
            return {'model_revision':'b'*40,'segments':[{**row,'text':'Русский '+row['source_text']} for row in request['segments']]}
        monkeypatch.setattr(dubbing_models,'invoke',synthetic_translation)
        # Both newly imported and earlier saved projects feed the same source
        # metadata. This exercises plumbing only, not neural translation.
        for index,project_value in enumerate([saved,{key:value for key,value in saved.items() if key!='source_metadata'}]):
            _,_,result=dubbing_pipeline.process({'tool':'dubbing_metadata','options':{},'settings':{},
                'source':saved['source'],'dubbing':{'project':project_value,'run_id':'owned-run',
                    'model_revisions':{'opus-en-ru':'b'*40}}},tmp_path/('stage-'+str(index)),tmp_path/'data')
            assert result['project_updates']['metadata']['title']=='Русский Owned English title'
            assert result['project_updates']['metadata']['description']=='Русский Owned English description'
        assert calls[0]['segments'][0]['source_text']=='Owned English title'
        assert calls[0]['model_revision']=='b'*40


def test_unreviewed_project_backup_runs_without_models_or_prior_render(tmp_path):
    app=create_app(tmp_path/'data','owned-backup-token',19954,workers=True)
    with TestClient(app,base_url='http://127.0.0.1:19954',headers={'x-forge-token':'owned-backup-token'}) as client:
        source=fixture_video(tmp_path/'owned.mp4');before=source.read_bytes()
        project=post(client,'/api/dubbing/import',{'path':str(source)})['project']
        result=post(client,'/api/dubbing/stage',{'project_id':project['id'],'stage':'export','formats':['project'],'include_source':True})
        identities=[row['id'] for row in result['jobs']]
        wait_for_jobs(app,identities)
        job=app.state.store.job(identities[0])
        assert job['status']=='completed',job['message']
        archives=[Path(item['path']) for item in job['outputs'] if Path(item['path']).suffix=='.zip']
        assert len(archives)==1
        with zipfile.ZipFile(archives[0]) as archive:
            assert 'project.json' in archive.namelist()
            assert archive.read('media/original.mp4')==before
            assert not any('token' in name.lower() or 'environment' in name.lower() for name in archive.namelist())
        assert source.read_bytes()==before
        assert job['payload']['options']['formats']==['project']


@pytest.mark.parametrize('extra',[{'formats':['exe']},{'formats':[]},{'arbitrary_filter':'bad'},{'formats':['project'],'stage':'mix'}])
def test_export_request_rejects_unknown_or_unbounded_overrides(tmp_path,extra):
    app=create_app(tmp_path/'data','owned-export-validation-token',19955,workers=False)
    with TestClient(app,base_url='http://127.0.0.1:19955',headers={'x-forge-token':'owned-export-validation-token'}) as client:
        project=post(client,'/api/dubbing/import',{'path':str(fixture_video(tmp_path/'owned.mp4'))})['project']
        response=client.post('/api/dubbing/stage',json={'project_id':project['id'],'stage':'export',**extra})
        assert response.status_code==400,response.text
        assert not app.state.store.jobs()
