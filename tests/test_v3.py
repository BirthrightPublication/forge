from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge import update_manager as updates
from test_forge import client, TOKEN
from test_updater import archive, install, u
from test_v2 import authorize


def test_inventory_is_read_only_and_reports_rejected_compatible_and_old_builds(tmp_path):
    root, data = install(tmp_path)
    archive(root / 'Build' / 'FORGE-v0.1.zip', '0.1.0')
    good = archive(root / 'Build' / 'FORGE-v0.3.zip', '0.3.0')
    archive(root / 'Build' / 'FORGE-v0.4.zip', '0.4.0', minimum='0.2.0')
    (root / 'Build' / 'FORGE-v0.5.zip').write_bytes(b'broken download')
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = updates.inventory(root)
    candidates = {item['name']: item for item in result['candidates']}
    assert result['selected']['version'] == '0.3.0'
    assert result['selected']['sha256'] == hashlib.sha256(good.read_bytes()).hexdigest()
    assert result['selected']['changed_files'] > 0
    assert candidates['FORGE-v0.1.zip']['status'] == 'current'
    assert candidates['FORGE-v0.4.zip']['status'] == 'incompatible'
    assert candidates['FORGE-v0.5.zip']['status'] == 'invalid'
    assert not result['rollback']['available']
    assert before == {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_inventory_conflicting_third_newest_copy_blocks_selection(tmp_path):
    root, _ = install(tmp_path)
    good = archive(root / 'Build' / 'FORGE-v0.3-b.zip', '0.3.0')
    (root / 'Build' / 'FORGE-v0.3-c.zip').write_bytes(good.read_bytes())
    archive(root / 'Build' / 'FORGE-v0.3-a.zip', '0.3.0', extra={'README.md': b'conflicting build'})
    result = updates.inventory(root)
    assert result['selected'] is None
    assert {item['status'] for item in result['candidates']} == {'conflict'}


def test_inventory_checks_registered_backup_and_detects_tampering(tmp_path):
    root, data = install(tmp_path)
    build = archive(root / 'Build' / 'FORGE-v0.3.zip', '0.3.0')
    backup = u.apply_release(root, build, data)
    result = updates.inventory(root)
    assert result['rollback']['available']
    assert result['rollback']['from'] == '0.3.0'
    assert result['rollback']['to'] == '0.1.0'
    (backup / 'code' / 'forge' / '__init__.py').write_text('tampered')
    result = updates.inventory(root)
    assert not result['rollback']['available']
    assert 'integrity' in result['rollback']['error'].lower()


def test_nested_install_uses_established_parent_builds(tmp_path, monkeypatch):
    monkeypatch.delenv('FORGE_UPDATE_HOME', raising=False)
    root, _ = install(tmp_path)
    nested = root / 'FORGE-v0.1'
    nested.mkdir()
    for name in ('forge', 'server.py', 'launcher.py', 'web'):
        (root / name).rename(nested / name)
    result = updates.inventory(nested)
    assert result['home'] == str(root)
    assert result['build_directory'] == str(root / 'Build')
    monkeypatch.setenv('FORGE_UPDATE_HOME', str(tmp_path))
    # Parent contains one Forge child with no application at that level.
    with pytest.raises((ValueError, updates.updater.UpdateError)):
        updates.update_home(nested)


def test_inspection_requires_auth_and_serves_new_asset(client):
    assert client.get('/assets/v3.js').status_code == 200
    response = authorize(client)
    assert client.get('/api/application-updates').status_code == 200
    assert client.post('/api/application-updates/open', json={'action':'update','consent':True}).status_code == 403
    client.cookies.clear()
    assert client.get('/api/application-updates').status_code == 401


def test_open_console_requires_explicit_consent_and_no_command_arguments(client, monkeypatch):
    calls = []
    snapshot = {'selected': {'version': '0.4.0'}, 'rollback': {'available':True}, 'home': '/fixed/home', 'interrupted':False}
    monkeypatch.setattr(updates, 'inventory', lambda root: snapshot)
    monkeypatch.setattr(updates, 'open_console', lambda *args: calls.append(args) or SimpleNamespace(poll=lambda: None))
    for body in ({}, {'action':'update','consent':False}, {'action':'run','consent':True}, {'action':[],'consent':True}, {'action':{},'consent':True}, {'action':'update','consent':True,'path':'evil.bat'}):
        assert client.post('/api/application-updates/open', json=body).status_code == 400
    assert not calls
    assert client.post('/api/application-updates/open', json={'action':'update','consent':True}).status_code == 200
    assert calls[0][1:] == (Path('/fixed/home'), 'update')
    assert client.post('/api/application-updates/open', json={'action':'update','consent':True}).status_code == 409
    assert len(calls) == 1


def test_open_console_refuses_busy_queue_and_rechecks_disk(client, monkeypatch):
    calls = []
    monkeypatch.setattr(updates, 'open_console', lambda *args: calls.append(args))
    monkeypatch.setattr(updates, 'inventory', lambda root: {'selected':None,'interrupted':False,'rollback':{'available':False,'error':'No backup.'}})
    assert client.post('/api/application-updates/open', json={'action':'update','consent':True}).status_code == 400
    assert client.post('/api/application-updates/open', json={'action':'rollback','consent':True}).status_code == 400
    store = client.app.state.store
    store.add_job('qr', 'Queued work', {'tool':'qr'})
    assert client.post('/api/application-updates/open', json={'action':'update','consent':True}).status_code == 400
    assert not calls


def test_browser_console_action_requires_csrf(client, monkeypatch):
    calls = []
    monkeypatch.setattr(updates, 'inventory', lambda root: {'selected':True,'interrupted':False,'home':'/fixed','rollback':{'available':False}})
    monkeypatch.setattr(updates, 'open_console', lambda *args: calls.append(args) or SimpleNamespace(poll=lambda: 0))
    csrf = authorize(client).json()['csrf']
    response = client.post('/api/application-updates/open', json={'action':'update','consent':True}, headers={'X-Forge-CSRF':csrf})
    assert response.status_code == 200
    assert len(calls) == 1


def test_native_console_launch_is_fixed_and_keeps_final_confirmation(tmp_path, monkeypatch):
    root = tmp_path / 'App'; script = root / 'scripts' / 'update.py'; script.parent.mkdir(parents=True);script.write_text('# trusted local updater')
    interpreter = tmp_path / 'python.exe';interpreter.write_bytes(b'python fixture')
    calls = []
    monkeypatch.setattr(updates, 'windows_supported', lambda: True)
    monkeypatch.setattr(updates.sys, 'executable', str(interpreter))
    monkeypatch.setattr(updates.subprocess, 'CREATE_NEW_CONSOLE', 16, raising=False)
    monkeypatch.setattr(updates.subprocess, 'Popen', lambda *args, **kwargs: calls.append((args,kwargs)))
    updates.open_console(root, root, 'rollback')
    command = calls[0][0][0]
    assert command == [str(interpreter), str(script), '--root', str(root), '--pause-on-exit', '--rollback']
    assert '--yes' not in command and 'shell' not in calls[0][1]


def test_prepare_persists_prior_pause_before_shutdown_and_cancel_clears_it(client):
    store = client.app.state.store
    assert client.post('/api/update/prepare', json={}).status_code == 200
    assert store.get('update_maintenance')['queue_paused'] is False
    assert store.settings()['queue_paused'] is True
    assert client.post('/api/update/cancel', json={}).status_code == 200
    assert store.get('update_maintenance') is None
    assert store.settings()['queue_paused'] is False
