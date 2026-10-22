import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('managed_install_test',ROOT/'scripts/managed_install.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


def fixture(tmp_path,monkeypatch):
    root=tmp_path/'Forge';root.mkdir()
    env=root/'.venv';(env/'Scripts').mkdir(parents=True)
    (env/'Scripts/python.exe').write_bytes(b'synthetic executable placeholder')
    (env/'installed.txt').write_text('original compatible packages')
    data=tmp_path/'data';(data/'maintenance').mkdir(parents=True)
    (data/'models').mkdir();(data/'models/model.bin').write_bytes(b'existing model sentinel')
    (data/'settings.json').write_text('settings sentinel')
    monkeypatch.setattr(installer,'ROOT',root)
    task={'data':str(data),'id':'1'*32,'engines':['transcription']}
    return root,data,env,task


def test_managed_install_preserves_environment_and_user_data(tmp_path,monkeypatch):
    root,data,env,task=fixture(tmp_path,monkeypatch)
    calls=[]
    def run(command,**kwargs):
        calls.append(command)
        if 'install' in command:(env/'installed.txt').write_text('updated compatible packages')
    monkeypatch.setattr(installer,'run',run)
    result=installer.install(task)
    assert result['status']=='completed'
    assert not any('venv' in call for call in calls)
    assert (env/'installed.txt').read_text()=='updated compatible packages'
    assert (Path(result['backup'])/'.venv/installed.txt').read_text()=='original compatible packages'
    assert (data/'models/model.bin').read_bytes()==b'existing model sentinel'
    assert (data/'settings.json').read_text()=='settings sentinel'
    download=next(i for i,c in enumerate(calls) if 'download' in c)
    install=next(i for i,c in enumerate(calls) if 'install' in c)
    assert download<install and '--no-index' in calls[install]


def test_failed_verification_restores_exact_previous_environment(tmp_path,monkeypatch):
    root,data,env,task=fixture(tmp_path,monkeypatch)
    def run(command,**kwargs):
        if 'install' in command:(env/'installed.txt').write_text('broken install')
        if 'check' in command:raise RuntimeError('synthetic dependency conflict')
    monkeypatch.setattr(installer,'run',run)
    with pytest.raises(RuntimeError,match='dependency conflict'):installer.install(task)
    assert (env/'installed.txt').read_text()=='original compatible packages'
    failed=list((data/'maintenance'/task['id']).glob('.venv-failed-*/installed.txt'))
    assert len(failed)==1 and failed[0].read_text()=='broken install'
    assert json.loads((data/'maintenance'/task['id']/'transaction.json').read_text())['state']=='rolled_back'
    assert (data/'models/model.bin').read_bytes()==b'existing model sentinel'


def test_download_failure_never_mutates_existing_environment(tmp_path,monkeypatch):
    root,data,env,task=fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(installer,'run',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('offline')))
    with pytest.raises(RuntimeError,match='offline'):installer.install(task)
    assert (env/'installed.txt').read_text()=='original compatible packages'


def test_interrupted_install_is_recovered_from_durable_journal(tmp_path,monkeypatch):
    root,data,env,task=fixture(tmp_path,monkeypatch)
    def run(command,**kwargs):
        if 'install' in command:
            (env/'installed.txt').write_text('interrupted package replacement')
            raise KeyboardInterrupt('simulate process interruption outside exception rollback')
    monkeypatch.setattr(installer,'run',run)
    with pytest.raises(KeyboardInterrupt):installer.install(task)
    marker=root/'.forge-maintenance.json'
    marker.write_text(json.dumps({'id':task['id'],'data':str(data)}))
    monkeypatch.setattr('launcher.alive',lambda *_:False)
    installer.recover()
    assert not marker.exists()
    assert (env/'installed.txt').read_text()=='original compatible packages'
    assert (data/'settings.json').read_text()=='settings sentinel'
    assert json.loads((data/'maintenance'/task['id']/'transaction.json').read_text())['state']=='rolled_back'
    assert json.loads((data/'maintenance'/(task['id']+'.result.json')).read_text())['status']=='recovered'


def test_recovery_refuses_live_installer_and_keeps_marker(tmp_path,monkeypatch):
    root,data,env,task=fixture(tmp_path,monkeypatch)
    marker=root/'.forge-maintenance.json'
    marker.write_text(json.dumps({'id':task['id'],'data':str(data)}))
    with installer.maintenance_lock():
        with pytest.raises((TimeoutError,RuntimeError),match='(?i)(busy|lock|timeout|timed)'):
            installer.recover()
    assert marker.exists() and (env/'installed.txt').read_text()=='original compatible packages'


def test_incomplete_backup_does_not_displace_existing_environment(tmp_path,monkeypatch):
    root,data,env,task=fixture(tmp_path,monkeypatch)
    backup=data/'maintenance'/task['id'];backup.mkdir()
    with pytest.raises(RuntimeError,match='backup is incomplete'):
        installer.restore_environments(backup,['.venv'],['.venv'])
    assert (env/'installed.txt').read_text()=='original compatible packages'


def test_launcher_rejects_interrupted_maintenance_before_service_start(tmp_path,monkeypatch):
    import launcher
    root,data,env,task=fixture(tmp_path,monkeypatch)
    (root/'.forge-maintenance.json').write_text('{}')
    monkeypatch.setattr(launcher,'ROOT',root)
    with pytest.raises(RuntimeError,match='maintenance'):
        launcher.main()


@pytest.mark.skipif(os.name!='nt',reason='Native Windows process-handle acceptance')
def test_native_wait_for_exact_service_process():
    import psutil
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(1)'])
    started=psutil.Process(child.pid).create_time()
    try:
        installer.wait_for_parent(child.pid,started,timeout=10)
        assert child.poll() is not None
    finally:
        if child.poll() is None:child.terminate()
        child.wait(timeout=5)


@pytest.mark.skipif(os.name!='nt',reason='Native Windows process identity acceptance')
def test_native_wrong_process_identity_is_rejected():
    import psutil
    with pytest.raises(RuntimeError,match='identity'):
        installer.wait_for_parent(os.getpid(),psutil.Process().create_time()-10,timeout=.01)
