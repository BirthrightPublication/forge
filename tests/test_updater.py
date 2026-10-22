from __future__ import annotations
import hashlib
import importlib.util
import json
import os
import py_compile
import sqlite3
import stat
import subprocess
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


def test_managed_source_update_preserves_installed_github_recovery_updater(tmp_path):
    root,data=install(tmp_path)
    external=b'@echo off\r\nrem existing GitHub recovery entrypoint\r\n'
    powershell=b'# existing owner supplied PowerShell updater\r\n'
    (root/'Update FORGE.bat').write_bytes(external)
    (root/'Update-Application.ps1').write_bytes(powershell)
    package=archive(root/'Build/new.zip')
    backup=u.apply_release(root,package,data)
    assert (root/'Update FORGE.bat').read_bytes()==external
    assert (root/'Update-Application.ps1').read_bytes()==powershell
    u.restore_snapshot(root,backup)
    assert (root/'Update FORGE.bat').read_bytes()==external
    assert (root/'Update-Application.ps1').read_bytes()==powershell


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


def tree_bytes(folder):
    return {str(p.relative_to(folder)):p.read_bytes() for p in folder.rglob('*') if p.is_file()}


def interrupted_update(root,data,*,paused=False):
    candidate=archive(root/'Build'/'FORGE-v0.2.zip')
    backup=u.apply_release(root,candidate,data)
    journal=u.read_json((backup/'journal.json').read_text());journal['state']='installing'
    u.write_json(backup/'journal.json',journal)
    u.write_json(root/'.forge-update-state.json',{'backup':str(backup),'state':'installing'})
    u.write_json(root/'.forge-updating',{'format':1,'root':str(root),'data':str(data),'queue_paused':paused})
    with sqlite3.connect(data/'forge.sqlite3') as db:
        db.execute("UPDATE kv SET value=? WHERE key='settings'",(json.dumps({'workers':2,'custom':'retain','queue_paused':True}),))
    return backup


@pytest.mark.parametrize('manifest',[None,[],42,{'app':'FORGE','format':True}, {'app':'FORGE','format':1,'version':'0.2.0','files':[]}, 'not an object'])
def test_malformed_manifest_is_rejected_without_blocking_valid_candidates(tmp_path,manifest):
    good=archive(tmp_path/'FORGE-v0.2.zip')
    with zipfile.ZipFile(tmp_path/'FORGE-v999.zip','w') as z:
        z.writestr('release-manifest.json',json.dumps(manifest))
    assert u.select_release(tmp_path,'0.1.0')==good
    with pytest.raises(u.UpdateError):u.inspect_release(tmp_path/'FORGE-v999.zip')


def test_selection_skips_incompatible_newest_and_reports_base_requirement(tmp_path):
    good=archive(tmp_path/'FORGE-v0.2.zip')
    archive(tmp_path/'FORGE-v0.3.zip','0.3.0',minimum='0.2.0')
    assert u.select_release(tmp_path,'0.1.0')==good
    good.unlink()
    with pytest.raises(u.UpdateError,match='Requires base 0.2.0'):u.select_release(tmp_path,'0.1.0')


def test_all_latest_duplicate_archives_are_compared(tmp_path):
    archive(tmp_path/'FORGE-v0.2-a.zip',extra={'README.md':b'different first archive'})
    second=archive(tmp_path/'FORGE-v0.2-b.zip')
    (tmp_path/'FORGE-v0.2-c.zip').write_bytes(second.read_bytes())
    with pytest.raises(u.UpdateError,match='Two different'):u.select_release(tmp_path,'0.1.0')


def test_release_plan_counts_files_without_writing(tmp_path):
    root,data=install(tmp_path)
    candidate=archive(root/'Build'/'FORGE-v0.2.zip',extra={'forge/new.py':b'# new'})
    before=tree_bytes(root);before_data=tree_bytes(data)
    plan=u.plan_release(root,candidate)
    assert plan['version']=='0.2.0' and plan['current_version']=='0.1.0'
    assert plan['changed_files']==1 and plan['new_files']==2 and plan['preserved_files']==1
    assert plan['sha256']==hashlib.sha256(candidate.read_bytes()).hexdigest()
    assert plan['total_files']==plan['changed_files']+plan['new_files']+plan['unchanged_files']+plan['preserved_files']
    assert tree_bytes(root)==before and tree_bytes(data)==before_data


@pytest.mark.parametrize('ver,minimum,message',[('0.1.0','0.1.0','not newer'),('0.3.0','0.2.0','requires base')])
def test_explicit_dry_run_rejects_ineligible_release_before_stopping(tmp_path,monkeypatch,ver,minimum,message):
    root,data=install(tmp_path);candidate=archive(root/'Build'/'FORGE-v-explicit.zip',ver,minimum=minimum)
    monkeypatch.setattr(u,'local_data',lambda:data)
    monkeypatch.setattr(u,'stop_idle',lambda *_:pytest.fail('Dry run stopped the service'))
    before=tree_bytes(root);before_data=tree_bytes(data)
    with pytest.raises(u.UpdateError,match=message):u.main(['--root',str(root),'--zip',str(candidate),'--dry-run','--yes'])
    assert tree_bytes(root)==before and tree_bytes(data)==before_data


def test_interrupted_dry_run_verifies_backup_without_any_mutation(tmp_path,monkeypatch):
    root,data=install(tmp_path);interrupted_update(root,data)
    before=tree_bytes(root);before_data=tree_bytes(data)
    monkeypatch.setattr(u,'local_data',lambda:data)
    monkeypatch.setattr(u,'stop_idle',lambda *_:pytest.fail('Dry run stopped the service'))
    assert u.main(['--root',str(root),'--dry-run','--yes','--no-launch'])==0
    assert tree_bytes(root)==before and tree_bytes(data)==before_data


@pytest.mark.parametrize('paused',[False,True])
def test_interrupted_recovery_clears_both_markers_and_restores_queue_preference(tmp_path,monkeypatch,paused):
    root,data=install(tmp_path);interrupted_update(root,data,paused=paused)
    monkeypatch.setattr(u,'local_data',lambda:data)
    assert u.main(['--root',str(root),'--yes','--no-launch'])==0
    assert u.installed_version(root)=='0.1.0'
    assert not (root/'.forge-update-state.json').exists() and not (root/'.forge-updating').exists()
    with sqlite3.connect(data/'forge.sqlite3') as db:
        settings=json.loads(db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0])
    assert settings['queue_paused'] is paused and settings['custom']=='retain'


def test_prepare_metadata_overrides_marker_snapshot_after_response_loss(tmp_path,monkeypatch):
    root,data=install(tmp_path);interrupted_update(root,data,paused=False)
    with sqlite3.connect(data/'forge.sqlite3') as db:
        db.execute('INSERT INTO kv VALUES (?,?)',('update_maintenance',json.dumps({'root':str(root),'queue_paused':True})))
    monkeypatch.setattr(u,'local_data',lambda:data)
    assert u.main(['--root',str(root),'--yes','--no-launch'])==0
    with sqlite3.connect(data/'forge.sqlite3') as db:
        assert json.loads(db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0])['queue_paused'] is True
        assert db.execute("SELECT value FROM kv WHERE key='update_maintenance'").fetchone() is None


def test_orphan_marker_dry_run_and_recovery_preserve_queue_preference(tmp_path,monkeypatch):
    root,data=install(tmp_path)
    u.write_maintenance_marker(root,data,{'queue_paused':False})
    with sqlite3.connect(data/'forge.sqlite3') as db:
        db.execute("UPDATE kv SET value=? WHERE key='settings'",(json.dumps({'queue_paused':True,'custom':'retain'}),))
    monkeypatch.setattr(u,'local_data',lambda:data)
    before=tree_bytes(root);before_data=tree_bytes(data)
    assert u.main(['--root',str(root),'--dry-run','--yes','--no-launch'])==0
    assert tree_bytes(root)==before and tree_bytes(data)==before_data
    with u.file_lock(data/'service.lock'):
        with pytest.raises(u.UpdateError,match='lock'):u.main(['--root',str(root),'--yes','--no-launch'])
    assert (root/'.forge-updating').exists()
    assert u.main(['--root',str(root),'--yes','--no-launch'])==0
    assert not (root/'.forge-updating').exists()
    with sqlite3.connect(data/'forge.sqlite3') as db:
        assert json.loads(db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0])=={'queue_paused':False,'custom':'retain'}


def test_legacy_orphan_marker_is_recovered_without_guessing_pause_state(tmp_path,monkeypatch):
    root,data=install(tmp_path)
    (root/'.forge-updating').write_text('An application update is in progress. Do not start FORGE.\n')
    with sqlite3.connect(data/'forge.sqlite3') as db:
        db.execute("UPDATE kv SET value=? WHERE key='settings'",(json.dumps({'queue_paused':True}),))
    monkeypatch.setattr(u,'local_data',lambda:data)
    assert u.main(['--root',str(root),'--yes','--no-launch'])==0
    with sqlite3.connect(data/'forge.sqlite3') as db:
        assert json.loads(db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0])['queue_paused'] is True
    assert not (root/'.forge-updating').exists()


def test_inspect_backup_is_read_only_and_rejects_tampered_version_or_content(tmp_path):
    root,data=install(tmp_path);candidate=archive(root/'Build'/'FORGE-v0.2.zip');backup=u.apply_release(root,candidate,data)
    before=tree_bytes(root)
    assert u.inspect_backup(root,backup)['state']=='completed'
    assert tree_bytes(root)==before
    journal=u.read_json((backup/'journal.json').read_text());journal['from']='0.0.9'
    u.write_json(backup/'journal.json',journal)
    with pytest.raises(u.UpdateError,match='version'):u.inspect_backup(root,backup)
    journal['from']='0.1.0';u.write_json(backup/'journal.json',journal)
    (backup/'code'/'forge/__init__.py').write_text('corrupted backup')
    with pytest.raises(u.UpdateError,match='integrity'):u.inspect_backup(root,backup)


def test_rollback_dry_run_rejects_corrupt_backup_before_stopping(tmp_path,monkeypatch):
    root,data=install(tmp_path);candidate=archive(root/'Build'/'FORGE-v0.2.zip');backup=u.apply_release(root,candidate,data)
    (backup/'code'/'forge/__init__.py').write_text('corrupted backup')
    monkeypatch.setattr(u,'local_data',lambda:data)
    monkeypatch.setattr(u,'stop_idle',lambda *_:pytest.fail('Invalid backup stopped the service'))
    before=tree_bytes(root);before_data=tree_bytes(data)
    with pytest.raises(u.UpdateError,match='integrity'):u.main(['--root',str(root),'--rollback','--dry-run','--yes'])
    assert tree_bytes(root)==before and tree_bytes(data)==before_data


def test_interrupted_manual_rollback_retains_recoverable_journal(tmp_path,monkeypatch):
    root,data=install(tmp_path);candidate=archive(root/'Build'/'FORGE-v0.2.zip');backup=u.apply_release(root,candidate,data)
    monkeypatch.setattr(u,'local_data',lambda:data)
    original=u.restore_snapshot
    def interrupted(*_):
        (root/'forge/__init__.py').write_text('__version__="0.1.0"\n')
        raise OSError('Injected locked file during rollback')
    monkeypatch.setattr(u,'restore_snapshot',interrupted)
    with pytest.raises(OSError,match='Injected'):u.main(['--root',str(root),'--rollback','--yes','--no-launch'])
    assert (root/'.forge-updating').exists()
    assert u.read_json((root/'.forge-update-state.json').read_text())['backup']==str(backup)
    monkeypatch.setattr(u,'restore_snapshot',original)
    assert u.main(['--root',str(root),'--yes','--no-launch'])==0
    assert u.installed_version(root)=='0.1.0'
    assert not (root/'.forge-updating').exists() and not (root/'.forge-update-state.json').exists()


@pytest.mark.parametrize('name',['forge/a?.py','forge/a*.py','forge/a|b.py','forge/a<b.py','forge/a\x01b.py'])
def test_rejects_windows_invalid_filename_characters(name):
    with pytest.raises(u.UpdateError):u.safe_relative(name)


def test_selection_uses_target_preflight_and_skips_newest_directory_conflict(tmp_path):
    root,data=install(tmp_path)
    good=archive(root/'Build'/'FORGE-v0.2.zip')
    archive(root/'Build'/'FORGE-v0.3.zip','0.3.0',extra={'forge/conflict.py':b'# new file'})
    (root/'forge/conflict.py').mkdir()
    before=tree_bytes(root);before_data=tree_bytes(data)
    assert u.select_release(root/'Build','0.1.0',root=root)==good
    assert tree_bytes(root)==before and tree_bytes(data)==before_data


def test_selection_skips_symlink_archive_even_when_it_is_newest(tmp_path):
    root,data=install(tmp_path)
    good=archive(root/'Build'/'FORGE-v0.2.zip')
    newer=archive(tmp_path/'newer.zip','0.3.0')
    (root/'Build'/'FORGE-v0.3.zip').symlink_to(newer)
    assert u.select_release(root/'Build','0.1.0',root=root)==good


def test_explicit_symlink_dry_run_is_rejected_without_stopping_or_writing(tmp_path,monkeypatch):
    root,data=install(tmp_path)
    candidate=archive(tmp_path/'candidate.zip')
    link=root/'Build'/'FORGE-v0.2.zip';link.symlink_to(candidate)
    monkeypatch.setattr(u,'local_data',lambda:data)
    monkeypatch.setattr(u,'stop_idle',lambda *_:pytest.fail('Linked archive stopped service'))
    before=tree_bytes(root);before_data=tree_bytes(data)
    with pytest.raises(u.UpdateError,match='links and junctions'):
        u.main(['--root',str(root),'--zip',str(link),'--dry-run','--yes'])
    assert tree_bytes(root)==before and tree_bytes(data)==before_data and link.is_symlink()


def test_same_timestamp_same_size_bytecode_is_invalidated_on_upgrade_and_rollback(tmp_path,monkeypatch):
    root,data=install(tmp_path);source=root/'forge/__init__.py'
    stamp=1700000000
    os.utime(source,(stamp,stamp))
    py_compile.compile(str(source),doraise=True,invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    cache=source.parent/'__pycache__'
    foreign=cache/'__init__.cpython-311.opt-1.pyc';foreign.write_bytes(b'foreign interpreter cache')
    pypy=cache/'__init__.pypy39-pp73.pyc';pypy.write_bytes(b'other interpreter cache')
    unrelated=cache/'__init__.other.cpython-312.pyc';unrelated.write_bytes(b'different source cache')
    legacy=source.with_suffix('.pyc');legacy.write_bytes(b'legacy bytecode')
    original_write=u.atomic_write
    def preserve_timestamp(path,content):
        original_write(path,content)
        if path==source:os.utime(path,(stamp,stamp))
    monkeypatch.setattr(u,'atomic_write',preserve_timestamp)
    def imported():
        return subprocess.check_output([sys.executable,'-c','import forge; print(forge.__version__)'],cwd=root,text=True).strip()
    assert imported()=='0.1.0'
    candidate=archive(root/'Build'/'FORGE-v0.2.zip')
    backup=u.apply_release(root,candidate,data,verify=lambda _:None)
    assert int(source.stat().st_mtime)==stamp and len(source.read_bytes())==len(code('0.1.0')['forge/__init__.py'])
    assert imported()=='0.2.0'
    assert not foreign.exists() and not pypy.exists() and not legacy.exists()
    assert unrelated.read_bytes()==b'different source cache'
    u.restore_snapshot(root,backup)
    assert int(source.stat().st_mtime)==stamp and imported()=='0.1.0'
    assert unrelated.read_bytes()==b'different source cache'


@pytest.mark.parametrize('linked_directory',[True,False])
def test_linked_bytecode_caches_are_rejected_before_application_changes(tmp_path,linked_directory):
    root,data=install(tmp_path);outside=tmp_path/'outside';outside.mkdir()
    original=outside/'__init__.cpython-312.pyc';original.write_bytes(b'keep external bytecode')
    cache=root/'forge/__pycache__'
    if linked_directory:cache.symlink_to(outside,target_is_directory=True)
    else:
        cache.mkdir();(cache/original.name).symlink_to(original)
    candidate=archive(root/'Build'/'FORGE-v0.2.zip')
    before=tree_bytes(root)
    with pytest.raises(u.UpdateError,match='linked bytecode'):u.apply_release(root,candidate,data)
    assert tree_bytes(root)==before and original.read_bytes()==b'keep external bytecode'
    assert not (root/'Backups').exists()


def test_smoke_rejects_imported_version_that_disagrees_with_source(tmp_path,monkeypatch):
    root,data=install(tmp_path)
    if os.name=='nt':
        private=root/'.venv/Scripts/python.exe';private.parent.mkdir();private.touch()
    class Result:
        returncode=0
        stdout='0.0.9\n'
        stderr=''
    monkeypatch.setattr(u.subprocess,'run',lambda *_,**__:Result())
    with pytest.raises(u.UpdateError,match='expected 0.1.0, imported 0.0.9'):u.smoke(root)
