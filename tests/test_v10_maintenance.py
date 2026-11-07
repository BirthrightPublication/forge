"""Maintenance ownership, approval, recovery and truthful restart regressions.

Process ownership and OS locks are real on the host. Transaction tests label
their restart stubs; native Windows restart acceptance remains a separate gate.
"""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import psutil
import pytest
from fastapi.testclient import TestClient

from scripts import managed_install as installer
from forge import __version__
from forge.app import create_app
from forge.core import Store
from forge.queue import QueueManager
from forge import maintenance, update_catalog, update_manager


@pytest.fixture
def interrupted(tmp_path,monkeypatch):
    root=tmp_path/'Forge';root.mkdir()
    data=tmp_path/'data';(data/'maintenance').mkdir(parents=True)
    identity='a'*32
    task={'id':identity,'root':str(root),'data':str(data),'engines':['transcription'],
          'parent_pid':2147483647,'parent_started':1.0,'port':18991,'queue_paused':True}
    request=data/'maintenance'/(identity+'.request.json')
    request.write_text(json.dumps(task))
    marker=root/'.forge-maintenance.json'
    marker.write_text(json.dumps({'id':identity,'root':str(root),'data':str(data),
                                 'owner_pid':2147483647,'owner_started':1.0}))
    monkeypatch.setattr(installer,'ROOT',root)
    monkeypatch.setattr(maintenance,'ROOT',root)
    return root,data,task,marker


def test_dead_owned_orphan_is_archived_without_changing_application_or_data(interrupted):
    root,data,task,marker=interrupted
    source=root/'app-sentinel.py';source.write_text('Synthetic original source')
    original=data/'original.wav';original.write_bytes(b'Synthetic private media sentinel')
    info=installer.inspect_marker(data)
    assert info['state']=='inactive' and info['safe_to_clear'] is True
    result=installer.clear_orphan_marker(data)
    assert result['state']=='recovered' and result['repairable'] is False and not marker.exists()
    assert source.read_text()=='Synthetic original source'
    assert original.read_bytes()==b'Synthetic private media sentinel'
    assert list((data/'maintenance').glob(task['id']+'.marker-*.json'))


def test_live_exact_process_owner_cannot_be_cleared(interrupted):
    root,data,task,marker=interrupted
    value=json.loads(marker.read_text());value.update(owner_pid=os.getpid(),owner_started=psutil.Process().create_time())
    marker.write_text(json.dumps(value));before=marker.read_bytes()
    assert installer.inspect_marker(data)['state']=='active'
    with pytest.raises(RuntimeError,match='still running'):
        installer.clear_orphan_marker(data)
    assert marker.read_bytes()==before


def test_reused_pid_record_never_kills_the_unrelated_live_process(interrupted):
    root,data,task,marker=interrupted
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])
    try:
        value=json.loads(marker.read_text())
        value.update(owner_pid=child.pid,owner_started=psutil.Process(child.pid).create_time()-100)
        marker.write_text(json.dumps(value))
        installer.clear_orphan_marker(data)
        assert not marker.exists() and child.poll() is None
    finally:
        child.terminate();child.wait(timeout=5)


def test_os_lock_prevents_racing_a_live_installer_even_with_stale_metadata(interrupted):
    _,data,_,marker=interrupted
    before=marker.read_bytes()
    with installer.maintenance_lock():
        assert installer.inspect_marker(data)['state']=='active'
        with pytest.raises(RuntimeError,match='lock'):
            installer.clear_orphan_marker(data)
    assert marker.read_bytes()==before


@pytest.mark.parametrize('kind',['foreign-root','foreign-data','missing-request','wrong-record-id'])
def test_unproven_marker_ownership_is_not_removed(interrupted,kind):
    root,data,task,marker=interrupted
    if kind=='missing-request':
        (data/'maintenance'/(task['id']+'.request.json')).unlink()
    elif kind=='wrong-record-id':
        request=data/'maintenance'/(task['id']+'.request.json')
        value=json.loads(request.read_text());value['id']='b'*32;request.write_text(json.dumps(value))
    else:
        value=json.loads(marker.read_text());value['root' if kind=='foreign-root' else 'data']=str(root/'foreign')
        marker.write_text(json.dumps(value))
    before=marker.read_bytes()
    assert installer.inspect_marker(data)['state']=='unknown'
    with pytest.raises(RuntimeError):installer.clear_orphan_marker(data)
    assert marker.read_bytes()==before


def test_legacy_owner_requires_its_registered_request_and_process_scan(interrupted,monkeypatch):
    _,data,task,marker=interrupted
    marker.write_text(json.dumps({'id':task['id'],'data':str(data)}))
    monkeypatch.setattr(installer,'_legacy_hosts',lambda request:True)
    assert installer.inspect_marker(data)['state']=='active'
    monkeypatch.setattr(installer,'_legacy_hosts',lambda request:False)
    assert installer.inspect_marker(data)['state']=='inactive'


def test_incomplete_environment_recovery_keeps_current_user_state(interrupted,monkeypatch):
    root,data,task,marker=interrupted
    target=root/'.venv';(target/'Scripts').mkdir(parents=True)
    (target/'Scripts/python.exe').write_bytes(b'Synthetic interpreter marker')
    (target/'packages.txt').write_text('broken replacement')
    backup=data/'maintenance'/task['id'];(backup/'.venv/Scripts').mkdir(parents=True)
    (backup/'.venv/Scripts/python.exe').write_bytes(b'Synthetic original interpreter marker')
    (backup/'.venv/packages.txt').write_text('original compatible packages')
    (backup/'transaction.json').write_text(json.dumps({'state':'installing','changed':['.venv'],'saved':['.venv']}))
    (data/'settings.json').write_text('Current user settings')
    info=installer.inspect_marker(data)
    assert info['needs_recovery'] is True and info['safe_to_clear'] is False
    with pytest.raises(RuntimeError):installer.clear_orphan_marker(data)
    monkeypatch.setattr('launcher.alive',lambda data:None)
    installer.recover()
    assert not marker.exists()
    assert (target/'packages.txt').read_text()=='original compatible packages'
    assert (data/'settings.json').read_text()=='Current user settings'
    assert list(backup.glob('.venv-failed-*/packages.txt'))


def test_service_reclaims_safe_orphan_without_losing_queue(interrupted):
    _,data,task,marker=interrupted
    store=Store(data);manager=QueueManager(store)
    app=SimpleNamespace(state=SimpleNamespace(port=18991,shutdown=None))
    service=maintenance.Maintenance(app,store,manager)
    job=store.add_job('qr','Preserved pending job',{'tool':'qr','options':{'text':'Synthetic QR'}})
    store.set('maintenance',{'id':task['id'],'status':'starting'})
    manager.maintenance=True
    try:
        result=service.status()
        assert result['status']=='recovered' and not marker.exists()
        assert manager.maintenance is manager.maintenance_pending is manager.repair_pending is False
        assert store.job(job['id'])['status']=='queued'
    finally:store.close()


def test_wrong_approved_version_cannot_become_restart_success(interrupted,monkeypatch):
    _,data,_,_=interrupted
    monkeypatch.setattr(installer,'validate_import',lambda python:'0.10.0')
    def no_child(*args,**kwargs):raise AssertionError('Wrong-version service must not be launched.')
    monkeypatch.setattr(installer.subprocess,'Popen',no_child)
    with pytest.raises(RuntimeError,match='approved'):
        installer.restart_verified({'data':str(data),'queue_paused':True,'expected_version':'0.9.0'},data/'maintenance/result.json')


def test_failed_scheduler_start_clears_maintenance_gates_without_changing_files(interrupted,monkeypatch):
    _,data,_,marker=interrupted
    marker.unlink()
    store=Store(data);manager=QueueManager(store)
    service=maintenance.Maintenance(SimpleNamespace(state=SimpleNamespace(port=18991,shutdown=None)),store,manager)
    class WindowsOS:
        name='nt'
        def __getattr__(self,name):return getattr(os,name)
    class FailedThread:
        def __init__(self,*args,**kwargs):pass
        def start(self):raise RuntimeError('Synthetic thread resource failure')
        def is_alive(self):return False
    monkeypatch.setattr(maintenance,'os',WindowsOS())
    monkeypatch.setattr(maintenance,'threading',SimpleNamespace(Thread=FailedThread,Event=__import__('threading').Event))
    try:
        with pytest.raises(ValueError,match='could not be scheduled'):
            service.request(['transcription'],approve=True,preserve_queued=True)
        assert manager.maintenance is manager.maintenance_pending is manager.repair_pending is False
        assert store.get('maintenance')['status']=='failed' and not marker.exists()
    finally:store.close()


def test_app_rollback_never_replaces_a_newer_unrelated_working_version(interrupted,monkeypatch):
    root,_,_,_=interrupted
    (root/'VERSION.txt').write_text('0.11.0')
    (root/'forge').mkdir();(root/'forge/__init__.py').write_text("__version__ = '0.11.0'\n")
    (root/'scripts').mkdir()
    shutil.copyfile(Path(installer.__file__).parent/'update.py',root/'scripts/update.py')
    calls=[];monkeypatch.setattr(installer,'run',lambda *args,**kwargs:calls.append(args))
    with pytest.raises(RuntimeError,match='no longer matches'):
        installer.rollback_application({'from':'0.9.0','to':'0.10.0'})
    assert calls==[] and (root/'VERSION.txt').read_text()=='0.11.0'


def test_health_rejects_another_root_and_pid_changes(interrupted,monkeypatch):
    root,_,_,_=interrupted
    runtime={'root':str(root),'pid':123,'version':'0.10.0'}
    values=[{'app':'FORGE','pid':123,'version':'0.10.0'},
            {'version':'0.10.0','application_directory':str(root/'another-installation')}]
    monkeypatch.setattr('launcher.request',lambda *args:values.pop(0))
    with pytest.raises(RuntimeError,match='root'):
        installer.verify_health(runtime,'0.10.0',123)
    values[:]=[{'app':'FORGE','pid':123,'version':'0.10.0'},
               {'version':'0.10.0','application_directory':str(root)},
               {'app':'FORGE','pid':456,'version':'0.10.0'}]
    with pytest.raises(RuntimeError,match='identity changed'):
        installer.verify_health(runtime,'0.10.0',123)


def test_update_preflight_reports_measured_health_work_and_explicit_estimate(interrupted,monkeypatch):
    root,data,_,_=interrupted
    (root/'VERSION.txt').write_text(__version__);(root/'server.py').write_text('# Synthetic source')
    (root/'forge').mkdir();(root/'forge/__init__.py').write_text('__version__ = '+repr(__version__))
    (root/'release-manifest.json').write_text(json.dumps({'files':{'server.py':'unused'}}))
    store=Store(data);manager=QueueManager(store)
    store.add_job('qr','Queued',{'options':{}})
    running=store.add_job('qr','Active',{'options':{}});store.update_job(running['id'],status='running')
    try:
        plan=update_manager.installation_preflight(root,manager,{'available':'v99.0.0','legacy_bytes':1024})
        assert plan['health']['ready'] is True and plan['health']['root']==str(root)
        assert plan['active_jobs']==1 and plan['queued_jobs']==1
        assert plan['download_bytes']==1024 and plan['storage_estimate'] is True
        assert plan['required_storage_bytes']>=512*1048576
        monkeypatch.setattr(update_manager.shutil,'disk_usage',lambda root:SimpleNamespace(free=1024))
        assert update_manager.installation_preflight(root,manager)['storage_ready'] is False
    finally:store.close()


def test_update_approval_binds_the_exact_version_and_preflight_creates_no_jobs(tmp_path,monkeypatch):
    app=create_app(tmp_path/'data','synthetic-maintenance-token',19822,workers=False)
    app.state.store.set('update_catalog',{'entries':[{'id':'application','category':'application','label':'FORGE',
        'action':'update','available':'v99.0.0','bytes':1024,'legacy_bytes':1024}]})
    monkeypatch.setattr(update_catalog,'download_application',lambda *args,**kwargs:pytest.fail('Unapproved version must never download.'))
    try:
        with TestClient(app,base_url='http://127.0.0.1:19822',headers={'X-Forge-Token':'synthetic-maintenance-token'}) as client:
            plan=client.post('/api/updates/preflight',json={'ids':['application']})
            assert plan.status_code==200 and plan.json()['application_version']=='v99.0.0'
            denied=client.post('/api/updates/install',json={'ids':['application'],'consent':True,'expected_version':'v98.0.0'})
            assert denied.status_code==400 and 'exact application version' in denied.json()['detail']
            assert app.state.store.jobs()==[]
            assert client.post('/api/maintenance/recover',json={'consent':1}).status_code==400
    finally:app.state.store.close()


def test_verified_package_storage_check_refuses_before_any_replacement(interrupted,monkeypatch):
    from forge import update_adapter
    from scripts import update
    root,_,_,_=interrupted
    source=root/'server.py';source.write_bytes(b'Synthetic old source')
    monkeypatch.setattr(update,'inspect_release',lambda package:({'version':'0.10.0'},{'server.py':b'Synthetic new source'}))
    monkeypatch.setattr(update_adapter.shutil,'disk_usage',lambda root:SimpleNamespace(free=0))
    with pytest.raises(ValueError,match='free space'):
        update_adapter.check_application_storage(root,root/'Build/approved.zip')
    assert source.read_bytes()==b'Synthetic old source'


def test_dubbing_catalog_is_local_and_routes_installation_to_the_quoted_setup(tmp_path,monkeypatch):
    from forge import dubbing_models
    monkeypatch.setattr(dubbing_models,'catalog',lambda data:[{'id':'translation-en-ru','label':'Russian translation',
        'installed':False,'ready':False,'installation_available':True}])
    monkeypatch.setattr(update_catalog,'read_json',lambda *args:pytest.fail('Local catalog must not contact a publisher.'))
    entries=update_catalog.dubbing_entries(tmp_path)
    assert {entry['category'] for entry in entries}=={'models','dependencies'}
    assert all(entry['setup_action']=='dubbing' and entry['action']=='' for entry in entries)


def test_launcher_uses_controlled_recovery_without_manual_commands(interrupted,monkeypatch):
    import launcher
    root,data,task,marker=interrupted
    (root/'scripts').mkdir();shutil.copyfile(Path(installer.__file__),root/'scripts/managed_install.py')
    backup=data/'maintenance'/task['id'];backup.mkdir()
    (backup/'transaction.json').write_text(json.dumps({'state':'installing','changed':[],'saved':[]}))
    monkeypatch.setattr(launcher,'ROOT',root)
    monkeypatch.setattr(launcher,'alive',lambda data:None)
    calls=[]
    monkeypatch.setattr(launcher.subprocess,'Popen',lambda command,**kwargs:calls.append((command,kwargs)) or SimpleNamespace(pid=123))
    assert launcher.maintenance_startup(data) is True
    command,kwargs=calls[0]
    assert command[2]=='--recover-launch' and Path(command[1])==root/'scripts/managed_install.py'
    request=json.loads(Path(command[3]).read_text())
    assert request['recovery']['id']==task['id'] and request['root']==str(root)
    assert request['parent_pid']==os.getpid() and marker.exists()


def test_failed_fresh_backend_rolls_back_environment_and_never_reports_update_success(interrupted,monkeypatch):
    root,data,task,marker=interrupted
    target=root/'.venv';(target/'Scripts').mkdir(parents=True)
    (target/'Scripts/python.exe').write_bytes(b'Synthetic original interpreter')
    (target/'packages.txt').write_text('Original compatible packages')
    (data/'settings.json').write_text('Current user settings')
    class WindowsOS:
        name='nt'
        def __getattr__(self,name):return getattr(os,name)
    class BaseRuntime:
        prefix=base_prefix='synthetic-base'
        argv=['managed_install.py',str(data/'maintenance'/(task['id']+'.request.json'))]
        def __getattr__(self,name):return getattr(sys,name)
    monkeypatch.setattr(installer,'os',WindowsOS())
    monkeypatch.setattr(installer,'sys',BaseRuntime())
    monkeypatch.setattr(installer,'maintenance_lock',lambda:nullcontext())
    monkeypatch.setattr(installer,'process_started',lambda pid:1.0)
    monkeypatch.setattr(installer,'wait_for_parent',lambda *args:None)
    monkeypatch.setattr(installer,'MaintenanceProgress',lambda *args:SimpleNamespace(start=lambda:None,stop=lambda:None))
    def replace(task):
        backup=data/'maintenance'/task['id'];backup.mkdir()
        shutil.copytree(target,backup/'.venv')
        (target/'packages.txt').write_text('Broken new packages')
        (backup/'transaction.json').write_text(json.dumps({'state':'completed','changed':['.venv'],'saved':['.venv'],'application':None}))
        return {'status':'installed','backup':str(backup),'message':'Disk replacement only; not restart success.'}
    monkeypatch.setattr(installer,'install',replace)
    calls=[]
    def restart(task,result_path,**kwargs):
        calls.append(True)
        if len(calls)==1:
            raise RuntimeError('Synthetic new-backend health failure')
        assert (target/'packages.txt').read_text()=='Original compatible packages'
        task['_verified_backend']={'pid':123,'root':str(root),'backend_verified':True}
        return __version__
    monkeypatch.setattr(installer,'restart_verified',restart)
    assert installer.main()==1
    result=json.loads((data/'maintenance'/(task['id']+'.result.json')).read_text())
    assert result['status']=='failed' and result['rollback_verified'] is True
    assert result['backend_verified'] is True and len(calls)==2
    assert 'restored' in result['message'] and not marker.exists()
    assert (data/'settings.json').read_text()=='Current user settings'


@pytest.mark.skipif(os.name!='nt',reason='Actual Windows process creation-time API acceptance')
def test_native_windows_maintenance_process_creation_matches_the_owned_handle():
    assert installer.process_started(os.getpid())==pytest.approx(psutil.Process().create_time(),abs=.01)
