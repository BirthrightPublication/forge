"""Exercise the real updater on a disposable extracted V0.1 installation, not the user's installation.
Pass a prior release ZIP, a current release ZIP, and a NEW disposable output directory.
This validates the Python updater and real application/worker code on the host OS; it does not execute Windows BAT/PowerShell.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('forge_updater',ROOT/'scripts'/'update.py');up=importlib.util.module_from_spec(spec);spec.loader.exec_module(up)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',type=Path,required=True);p.add_argument('--build',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--allow-test-writes',action='store_true');a=p.parse_args()
    if not a.allow_test_writes:p.error('Pass --allow-test-writes for a NEW disposable directory.')
    home=a.output.resolve()
    if home.exists():raise RuntimeError('Output directory already exists. Choose a fresh disposable location.')
    home.mkdir(parents=True);data=home/'test-user-data';builds=home/'Build';builds.mkdir()
    with zipfile.ZipFile(a.base) as z:z.extractall(home)
    root=up.find_installation(home)
    assert up.installed_version(root)=='0.1.0'
    old_hashes={str(f.relative_to(root)):hashlib.sha256(f.read_bytes()).hexdigest() for f in (root/'original_scripts').iterdir() if f.is_file()}
    for relative,content in [('.venv/keep.txt','Private environment'),('tools/keep.txt','User-supplied tools'),('Config/keep.txt','Custom configuration')]:
        path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
    env={**os.environ,'FORGE_DATA_DIR':str(data)}
    def python(code):
        r=subprocess.run([sys.executable,'-c',code],cwd=root,env=env,text=True,capture_output=True,timeout=60)
        if r.returncode:raise RuntimeError(r.stdout+'\n'+r.stderr)
        return r.stdout
    print(python("""from pathlib import Path
import os
from forge.core import Store
s=Store(Path(os.environ['FORGE_DATA_DIR']));s.set('settings',{'workers':3,'queue_paused':False,'default_output':str(s.data/'exports')})
s.set('presets',[{'tool':'transcribe','name':'Keep my preset','options':{'model':'base'},'output':{'mode':'source','directory':'','subfolder':''}}])
j=s.add_job('qr','Preserved V0.1 history',{'tool':'qr','options':{'text':'old history'},'destination':str(s.data/'exports'),'settings':s.settings(),'source':None});s.update_job(j['id'],status='completed');s.close();print('Created V0.1 data with preset and completed job')
"""))
    shutil.copyfile(a.build,builds/'FORGE-v0.2.zip')
    previous=os.environ.get('FORGE_DATA_DIR');os.environ['FORGE_DATA_DIR']=str(data)
    try:
        assert up.main(['--root',str(home),'--yes','--no-launch'])==0
        assert up.installed_version(root)=='0.2.0'
        result=python("""import os,time,json
from pathlib import Path
from fastapi.testclient import TestClient
from forge.app import create_app
app=create_app(Path(os.environ['FORGE_DATA_DIR']),'fixture-token',19765,workers=True)
with TestClient(app,base_url='http://127.0.0.1:19765',headers={'X-Forge-Token':'fixture-token'}) as c:
 state=c.get('/api/state').json();assert state['version']=='0.2.0';assert state['settings']['workers']==3
 assert state['presets'][0]['name']=='Keep my preset';assert any(j['label']=='Preserved V0.1 history' for j in state['jobs'])
 r=c.post('/api/jobs',json={'tool':'qr','options':{'text':'Post-upgrade real worker','name':'Post-upgrade QR'},'output':{'mode':'default'}});assert r.status_code==200,r.text;jid=r.json()['jobs'][0]['id']
 for i in range(100):
  j=next(j for j in c.get('/api/state').json()['jobs'] if j['id']==jid)
  if j['status']=='completed':break
  assert j['status']!='failed',j
  time.sleep(.1)
 assert j['status']=='completed';assert Path(j['outputs'][0]['path']).is_file()
app.state.store.close();print('Preserved old data, loaded new API, completed real QR worker')
""");print(result)
        assert up.main(['--root',str(home),'--rollback','--yes','--no-launch'])==0
        assert up.installed_version(root)=='0.1.0'
        print(python("""import os
from pathlib import Path
from forge.core import Store
s=Store(Path(os.environ['FORGE_DATA_DIR']));assert len(s.jobs())==2;assert s.get('presets')[0]['name']=='Keep my preset';s.close();print('Old code reopened current database after code-only rollback; both history entries remain')
"""))
        for rel,digest in old_hashes.items():assert hashlib.sha256((root/rel).read_bytes()).hexdigest()==digest
        for rel in ['.venv/keep.txt','tools/keep.txt','Config/keep.txt']:assert (root/rel).is_file()
        report={'result':'passed','host':sys.platform,'base_sha256':hashlib.sha256(a.base.read_bytes()).hexdigest(),'candidate_sha256':hashlib.sha256(a.build.read_bytes()).hexdigest(),'original_files_preserved':len(old_hashes),'checks':['Manifest verified','Nested V0.1 installation detected','Full release installed through updater main','Code and database backup created','Settings/preset/history preserved','New API and real QR worker executed','Code-only rollback restored V0.1','V0.1 reopened current database and retained new history','Private environment/tools/config/originals preserved'],'native_windows_execution':False}
        (home/'integration-result.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
    finally:
        if previous is None:os.environ.pop('FORGE_DATA_DIR',None)
        else:os.environ['FORGE_DATA_DIR']=previous
if __name__=='__main__':main()
