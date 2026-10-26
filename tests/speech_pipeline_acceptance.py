"""Opt-in actual neural speech through FORGE's API, queue and audiobook worker.

Uses only the new disposable model folder populated by the native acceptance job.
It refuses a pre-existing FORGE database and never opens an owner installation.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
import secrets
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def run(data,output):
    data,output=data.resolve(),output.resolve()
    if not data.is_dir() or (data/'forge.sqlite3').exists() or data.is_relative_to(ROOT) or output.exists() or output.is_relative_to(data):
        raise ValueError('Use a disposable model-only directory without an existing FORGE database and a new separate evidence directory.')
    marker=data/'.forge-acceptance-owned.json'
    if not marker.is_file() or json.loads(marker.read_text()).get('purpose')!='FORGE disposable speech acceptance':
        raise ValueError('The model directory must be created by the disposable speech acceptance harness.')
    from fastapi.testclient import TestClient
    from forge.app import create_app
    output.mkdir(parents=True)
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1')
    token=secrets.token_urlsafe(32)
    app=create_app(data,token,19819,workers=True)
    settings=app.state.store.settings()
    settings.update(workers=1,cpu_workers=1,automatic_update_checks=False,daily_update_check=False)
    app.state.store.set('settings',settings)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True,timeout=10).strip()
    receipt={'source_commit':commit,'platform':sys.platform,
             'model_inference_tested':True,'device':'cpu','checks':[]}
    try:
        with TestClient(app,base_url='http://127.0.0.1:19819',headers={'X-Forge-Token':token}) as client:
            def post(path,body):
                response=client.post('/api/speech/'+path,json=body)
                if response.status_code!=200:
                    raise AssertionError('Speech API rejected '+path+': '+response.text[:1000])
                return response.json()

            def render(project,name):
                job=post('generate',{'project_id':project['id'],'options':{'name':name},
                         'output':{'mode':'custom','directory':str(data/'pipeline-output')}})['job']
                deadline=time.monotonic()+900
                while time.monotonic()<deadline:
                    value=app.state.store.job(job['id'])
                    if value['status']=='completed':
                        break
                    if value['status'] in {'failed','cancelled','blocked','interrupted'}:
                        raise AssertionError('Actual queued speech failed: '+value['message'])
                    time.sleep(.2)
                else:
                    raise TimeoutError('Actual queued speech exceeded its acceptance deadline.')
                report_path=next(Path(x['path']) for x in value['outputs'] if x['name'].endswith('_speech-render.json'))
                report=json.loads(report_path.read_text())
                for index,item in enumerate(value['outputs']):
                    path=Path(item['path'])
                    if path.suffix=='.wav':
                        response=client.get('/api/speech/audio',params={'job':job['id'],'index':index})
                        assert response.status_code==200 and response.content==path.read_bytes()
                        assert path.stat().st_size>1000 and path.stat().st_size<10000000
                        shutil.copyfile(path,output/path.name)
                return report

            project=post('projects',{'name':'Synthetic queued speech acceptance',
                         'chapters':[{'id':'first','title':'First','text':'A quiet passage ends here.'},
                                     {'id':'second','title':'Second','text':'A second passage follows.'}],
                         'options':{'engine':'kokoro','voice':'am_onyx','device':'cpu','format':'wav',
                                    'master_effects':[{'type':'gain','params':{'db':-3}}]}})['project']
            first=render(project,'pipeline-first')
            assert first['segments'] and all(not x['reused'] for x in first['segments'])
            assert first['master_effects'][0]['params']['db']==-3
            project['chapters'][1]['text']='Another quiet passage follows.'
            project=post('projects',project)['project']
            edited=render(project,'pipeline-edited')
            assert any(x['chapter_id']=='first' and x['reused'] for x in edited['segments'])
            assert any(x['chapter_id']=='second' and not x['reused'] for x in edited['segments'])
            revisions=sorted({x['inference']['model_revision'] for x in edited['segments']})
            assert revisions and all(re.fullmatch('[a-f0-9]{40}',x) for x in revisions)
            receipt.update(status='passed',model_revisions=revisions,first_segments=len(first['segments']),
                           edited_segments=len(edited['segments']),reused_segments=sum(x['reused'] for x in edited['segments']),
                           checks=['Actual Onyx through authenticated API and Universal Queue',
                                   'Generated output served by authenticated playback endpoint',
                                   'Master processing and original/processed output publication',
                                   'Changed chapter preserves unchanged verified speech segments'])
            (output/'PIPELINE_ACCEPTANCE.json').write_text(json.dumps(receipt,indent=2)+'\n')
            print('::notice title=Actual queued speech acceptance::'+json.dumps(receipt),flush=True)
    finally:
        app.state.store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        run(args.data,args.output)
    except Exception as error:
        message=str(error).replace('%','%25').replace('\r','%0D').replace('\n','%0A')
        print('::error title=Real queued speech acceptance::'+message)
        raise
