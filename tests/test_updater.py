from __future__ import annotations
import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import sys
import zipfile
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('forge_release_updater',ROOT/'scripts'/'update.py')
u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)


def code(ver='0.2.0'):
    return {'forge/__init__.py':f'__version__ = "{ver}"\n'.encode(),'forge/app.py':b'# new application\n',
            'launcher.py':b'# launcher\n','server.py':b'# server\n','web/app.js':b'// browser\n','web/index.html':b'<h1>FORGE</h1>',
            'Update FORGE.bat':b'@echo off\necho update\n','original_scripts/reference.bat':b'published original\n'}


def archive(path,ver='0.2.0',extra=None,overrides=None,prefix='FORGE-v0.2/',minimum='0.1.0'):
    contents=code(ver);contents.update(extra or {})
    manifest={'app':'FORGE','format':1,'version':ver,'minimum_from':minimum,'files':{k:hashlib.sha256(v).hexdigest() for k,v in contents.items()}}
    actual={**contents,**(overrides or {})}
    with zipfile.ZipFile(path,'w') as z:
        z.writestr(prefix+'release-manifest.json',json.dumps(manifest))
        for k,v in actual.items():z.writestr(prefix+k,v)
    return path


def install(tmp_path):
    root=tmp_path/'Forge';root.mkdir()
    for k,v in code('0.1.0').items():
        p=root/k;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(v)
    (root/'Build').mkdir();(root/'.venv').mkdir();(root/'.venv'/'local-only.txt').write_text('environment')
    (root/'tools').mkdir();(root/'tools'/'ffmpeg.exe').write_bytes(b'USER TOOL')
    (root/'original_scripts'/'reference.bat').write_text('user original untouched')
    data=tmp_path/'UserData';data.mkdir()
    with sqlite3.connect(data/'forge.sqlite3') as db:
        db.execute('CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)')
        db.execute('INSERT INTO kv VALUES (?,?)',('settings',json.dumps({'workers':2,'custom':'retain'})))
        db.execute('CREATE TABLE jobs (status TEXT)')
    (data/'models').mkdir();(data/'models'/'weights.bin').write_bytes(b'large local model')
    return root,data


@pytest.mark.parametrize('name',['../launcher.py','/launcher.py','forge/../../secret','forge\\worker.py','forge/a:stream','Build/a.py','.venv/a.py','tools/a.py','forge/data/x','forge/CON.py','forge/name.','unregistered.py'])
def test_rejects_unsafe_or_protected_paths(name):
    with pytest.raises(u.UpdateError):u.safe_relative(name)


def test_verify_complete_release_and_bootstrap(tmp_path):
    p=archive(tmp_path/'FORGE-v0.2.zip');manifest,files=u.inspect_release(p)
    assert manifest['version']=='0.2.0' and files['server.py']==code()['server.py']
    payload=(ROOT/'Update FORGE.bat').read_text().split('# === FORGE PYTHON PAYLOAD ===',1)[1]
    assert payload.lstrip()==(ROOT/'scripts/update.py').read_text().lstrip()
    compile(payload,'embedded updater','exec')


def test_checksum_failure_and_missing_files(tmp_path):
    p=archive(tmp_path/'broken.zip',overrides={'server.py':b'tampered'})
    with pytest.raises(u.UpdateError,match='Integrity'):u.inspect_release(p)
    p=archive(tmp_path/'missing.zip')
    with zipfile.ZipFile(p) as z:content={i.filename:z.read(i) for i in z.infolist() if not i.filename.endswith('server.py')}
    with zipfile.ZipFile(p,'w') as z:
        for k,v in content.items():z.writestr(k,v)
    with pytest.raises(u.UpdateError,match='missing'):u.inspect_release(p)


def test_rejects_manifest_version_mismatch(tmp_path):
    p=archive(tmp_path/'bad.zip',extra={'forge/__init__.py':b'__version__="1.0.0"'})
    with pytest.raises(u.UpdateError,match='version'):u.inspect_release(p)


def test_duplicate_case_and_symlink_rejected(tmp_path):
    p=archive(tmp_path/'case.zip',extra={'forge/APP.py':b'pass'})
    with pytest.raises(u.UpdateError,match='case-insensitive'):u.inspect_release(p)
    p=archive(tmp_path/'link.zip')
    with zipfile.ZipFile(p,'a') as z:
        i=zipfile.ZipInfo('FORGE-v0.2/forge/link.py');i.create_system=3;i.external_attr=(stat.S_IFLNK|0o777)<<16
        z.writestr(i,'/etc/passwd')
    with pytest.raises(u.UpdateError,match='Links'):u.inspect_release(p)


def test_wrong_project_bad_python_and_unlisted_entry(tmp_path):
    p=archive(tmp_path/'syntax.zip',extra={'forge/bad.py':b'def ???'})
    with pytest.raises(u.UpdateError,match='compilation'):u.inspect_release(p)
    p=archive(tmp_path/'extra.zip')
    with zipfile.ZipFile(p,'a') as z:z.writestr('FORGE-v0.2/forge/unknown.py','pass')
    with pytest.raises(u.UpdateError,match='Unlisted'):u.inspect_release(p)


def test_semantic_build_selection_and_conflicting_duplicates(tmp_path):
    folder=tmp_path/'Build';folder.mkdir()
    a=archive(folder/'FORGE-v0.2.zip','0.2.0')
    b=archive(folder/'FORGE-v0.10.zip','0.10.0')
    os.utime(a,(time:=9999999999,time))
    assert u.select_release(folder,'0.1.0')==b
    archive(folder/'FORGE-v0.10-other.zip','0.10.0',extra={'README.md':b'different content'})
    with pytest.raises(u.UpdateError,match='Two different'):u.select_release(folder,'0.1.0')


def test_apply_backup_preserves_user_data_tools_originals_and_venv(tmp_path):
    root,data=install(tmp_path);p=archive(root/'Build'/'FORGE-v0.2.zip')
    original=(root/'original_scripts'/'reference.bat').read_bytes()
    backup=u.apply_release(root,p,data)
    assert u.installed_version(root)=='0.2.0'
    assert (backup/'journal.json').is_file()
    with sqlite3.connect(backup/'user-data-snapshot'/'forge.sqlite3') as db:
        assert db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0].find('retain')>=0
    assert (root/'original_scripts'/'reference.bat').read_bytes()==original
    assert (root/'.venv'/'local-only.txt').read_text()=='environment'
    assert (root/'tools'/'ffmpeg.exe').read_bytes()==b'USER TOOL'
    assert (data/'models'/'weights.bin').read_bytes()==b'large local model'
    assert not (root/'.forge-update-state.json').exists()
    u.restore_snapshot(root,backup)
    assert u.installed_version(root)=='0.1.0'
    assert (root/'original_scripts'/'reference.bat').read_bytes()==original


def test_automatic_rollback_on_failed_smoke(tmp_path):
    root,data=install(tmp_path);p=archive(root/'Build'/'FORGE-v0.2.zip',extra={'forge/new.py':b'# new'})
    initial={str(f.relative_to(root)):f.read_bytes() for f in root.rglob('*') if f.is_file() and 'Build' not in f.parts}
    def fail(_):raise RuntimeError('Injected startup failure')
    with pytest.raises(RuntimeError,match='Injected'):u.apply_release(root,p,data,verify=fail)
    assert u.installed_version(root)=='0.1.0' and not (root/'forge/new.py').exists()
    for rel,content in initial.items():assert (root/rel).read_bytes()==content
    assert not (root/'.forge-update-state.json').exists()


def test_reject_target_symlink_without_changes(tmp_path):
    root,data=install(tmp_path);p=archive(root/'Build'/'FORGE-v0.2.zip')
    external=tmp_path/'other.py';external.write_text('untouched')
    (root/'forge/app.py').unlink();(root/'forge/app.py').symlink_to(external)
    with pytest.raises(u.UpdateError,match='link'):u.apply_release(root,p,data)
    assert u.installed_version(root)=='0.1.0' and external.read_text()=='untouched'


def test_corrupted_backup_rejected_before_any_restore(tmp_path):
    root,data=install(tmp_path);p=archive(root/'Build'/'FORGE-v0.2.zip')
    backup=u.apply_release(root,p,data)
    (backup/'code'/'forge/__init__.py').write_text('corrupt')
    with pytest.raises(u.UpdateError,match='integrity'):u.restore_snapshot(root,backup)
    assert u.installed_version(root)=='0.2.0'


def test_existing_nested_installation_resolution(tmp_path):
    home=tmp_path/'Forge';home.mkdir();child=home/'FORGE-v0.1';child.mkdir()
    (child/'forge').mkdir();(child/'server.py').touch();(child/'forge/__init__.py').write_text('__version__="0.1.0"')
    assert u.find_installation(home)==child
    second=home/'FORGE-v-other';second.mkdir();(second/'forge').mkdir();(second/'server.py').touch();(second/'forge/__init__.py').touch()
    with pytest.raises(u.UpdateError,match='Multiple'):u.find_installation(home)


def test_dry_run_no_mutation_and_repeated_downgrade_refusal(tmp_path,monkeypatch):
    root,data=install(tmp_path);p=archive(root/'Build'/'FORGE-v0.2.zip')
    monkeypatch.setattr(u,'local_data',lambda:data)
    assert u.main(['--root',str(root),'--dry-run','--no-launch','--yes'])==0
    assert u.installed_version(root)=='0.1.0' and not (root/'Backups').exists()
    u.apply_release(root,p,data)
    with pytest.raises(u.UpdateError,match='not newer'):u.apply_release(root,p,data)


def test_interactive_rollback_uses_single_lock(tmp_path,monkeypatch):
    root,data=install(tmp_path);p=archive(root/'Build'/'FORGE-v0.2.zip');u.apply_release(root,p,data)
    archive(root/'Build'/'FORGE-v0.3.zip','0.3.0')
    monkeypatch.setattr(u,'local_data',lambda:data);answers=iter(['r','y'])
    monkeypatch.setattr('builtins.input',lambda *_:next(answers))
    assert u.main(['--root',str(root),'--no-launch'])==0
    assert u.installed_version(root)=='0.1.0'


def test_idle_check_includes_jobs_outside_visible_state(tmp_path,monkeypatch):
    root,data=install(tmp_path)
    with sqlite3.connect(data/'forge.sqlite3') as db:db.execute('INSERT INTO jobs VALUES (?)',('queued',))
    monkeypatch.setattr(u,'running',lambda _ : ({'root':str(root)}, {'jobs':[], 'settings':{}}))
    with pytest.raises(u.UpdateError,match='not idle'):u.stop_idle(data,root)


def test_old_zip_without_manifest_is_not_an_update(tmp_path):
    p=tmp_path/'FORGE-v0.1.zip'
    with zipfile.ZipFile(p,'w') as z:z.writestr('FORGE-v0.1/server.py','pass')
    with pytest.raises(u.UpdateError,match='manifest'):u.inspect_release(p)
