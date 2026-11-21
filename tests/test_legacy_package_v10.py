"""Real local legacy packaging/update contracts, never model or live OAuth proof."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from forge import dubbing_acceptance, publishing_acceptance
from forge.core import Store
from scripts import dubbing_cpu_acceptance, package_release, update


ROOT = Path(__file__).resolve().parents[1]
EXTRA_SOURCE = ('forge/dubbing_acceptance.py', 'forge/worker.py', 'forge/language_publishing.py',
                'forge/publishing_oauth.py', 'forge/publishing_acceptance.py',
                'web/dubbing_v10.js', 'web/publishing_acceptance_v10.js')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def commit_source(root, message='Source-bound legacy package fixture'):
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=FORGE test',
                    '-c', 'user.email=fixture@example.invalid', 'commit', '--quiet', '-m', message], check=True)
    return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / 'source'; root.mkdir()
    files = {'forge/__init__.py': b'__version__ = "0.10.0"\n', 'VERSION.txt': b'0.10.0\n',
             'forge/app.py': b'# Source identity integration fixture; no server execution.\n',
             'launcher.py': b'# Source fixture launcher.\n', 'server.py': b'# Source fixture server.\n',
             'web/app.js': b'// Source fixture application.\n', 'web/index.html': b'<title>FORGE fixture</title>',
             'Update FORGE.bat': (ROOT / 'Update FORGE.bat').read_bytes(),
             'original_scripts/owner.py': b'# Published original; installed original must survive.\n'}
    files.update({name: (ROOT / name).read_bytes() for name in (*dubbing_cpu_acceptance.SOURCE_FILES, *EXTRA_SOURCE)})
    for name, raw in files.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    manifest = {'app': 'FORGE', 'format': 1, 'version': '0.10.0', 'minimum_from': '0.1.0',
                'files': {name: digest(raw) for name, raw in files.items()}}
    (root / 'release-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    monkeypatch.setattr(package_release, 'ROOT', root)
    return root


def git_source(source):
    subprocess.run(['git', 'init', '--quiet', str(source)], check=True)
    subprocess.run(['git', '-C', str(source), 'config', 'core.autocrlf', 'false'], check=True)
    return commit_source(source)


def previous_install(tmp_path):
    root = tmp_path / 'installed'; root.mkdir()
    old = {'forge/__init__.py': b'__version__ = "0.9.3"\n', 'VERSION.txt': b'0.9.3\n',
           'release-manifest.json': b'{"app":"FORGE","format":1,"version":"0.9.3","files":{}}',
           'Update FORGE.bat': b'@echo off\r\nrem Preserve installed external updater wrapper.\r\n',
           'Update-Application.ps1': b'# Preserve installed external PowerShell updater.\r\n',
           'original_scripts/owner.py': b'# Preserve owner original script.\n',
           '.venv/Scripts/owner-environment.txt': b'Preserve existing environment.',
           'models/owner-model.bin': b'Preserve existing model bytes.'}
    for name, raw in old.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    data = tmp_path / 'UserData'
    store = Store(data)
    try:
        store.set('settings', {'queue_paused': True, 'workers': 2, 'owner_marker': 'preserve'})
        store.set('presets', [{'name': 'Owner preset', 'tool': 'transcribe', 'options': {'model': 'tiny'}}])
        store.add_job('qr', 'Preserve queued owner work', {'tool': 'qr', 'options': {'text': 'Owned synthetic fixture'}})
    finally:
        store.close()
    return root, data, old, (data / 'forge.sqlite3').read_bytes()


def test_normal_legacy_upgrade_enables_source_bound_readiness_and_private_acceptance_and_rolls_back(
        source, tmp_path, monkeypatch):
    commit = git_source(source)
    original = (source / 'release-manifest.json').read_bytes()
    archive = package_release.build(tmp_path / 'Build')
    manifest, payload = update.inspect_release(archive)
    assert archive.name == 'FORGE-v0.10.zip'
    with zipfile.ZipFile(archive) as zipped:
        assert all(name.startswith('FORGE-v0.10/') for name in zipped.namelist())
    assert manifest['package']['source_commit'] == commit
    assert manifest['package']['source_manifest_sha256'] == digest(original)
    assert archive.with_suffix('.zip.sha256').read_text().split()[0] == digest(archive.read_bytes())
    assert set(payload) == set(manifest['files']) | {'release-manifest.json'}

    installed, data, old, database = previous_install(tmp_path)
    backup = update.apply_release(installed, archive, data)
    assert not (installed / '.git').exists() and update.installed_version(installed) == '0.10.0'
    monkeypatch.setattr(dubbing_cpu_acceptance, 'ROOT', installed)
    monkeypatch.setattr(publishing_acceptance, 'ROOT', installed)
    identity = dubbing_cpu_acceptance.source_identity()
    assert identity['source_kind'] == 'installed_manifest' and identity['source_commit'] == commit
    assert identity['source_manifest_sha256'] == digest(original)
    assert dubbing_acceptance._source()['source_commit'] == commit
    publishing = publishing_acceptance.source_identity()
    assert publishing_acceptance._identity_ready(publishing)
    assert publishing['source_commit'] == commit and publishing['source_manifest_sha256'] == digest(original)
    preserved = {name: raw for name, raw in old.items() if name not in {
        'forge/__init__.py', 'VERSION.txt', 'release-manifest.json'}}
    assert all((installed / name).read_bytes() == raw for name, raw in preserved.items())
    assert (data / 'forge.sqlite3').read_bytes() == database
    assert (backup / 'user-data-snapshot/forge.sqlite3').is_file()

    # Installed code edits cannot retain this source-bound readiness approval.
    runtime = installed / 'scripts/dubbing_runtime.py'
    runtime.write_bytes(runtime.read_bytes() + b'\n# Changed installed runtime fixture.\n')
    with pytest.raises(dubbing_cpu_acceptance.AcceptanceBlocked, match='adapter bytes'):
        dubbing_acceptance._source()
    assert not publishing_acceptance._identity_ready(publishing_acceptance.source_identity())

    update.restore_snapshot(installed, backup)
    assert update.installed_version(installed) == '0.9.3'
    assert all((installed / name).read_bytes() == raw for name, raw in old.items())
    assert not (installed / 'scripts/dubbing_runtime.py').exists()
    assert (data / 'forge.sqlite3').read_bytes() == database


def test_no_git_legacy_build_never_invents_source_identity(source, tmp_path):
    archive = package_release.build(tmp_path / 'Build')
    manifest, _ = update.inspect_release(archive)
    assert 'package' not in manifest
    with pytest.raises(dubbing_cpu_acceptance.AcceptanceBlocked, match='original source manifest identity'):
        dubbing_cpu_acceptance.source_identity(source)


@pytest.mark.parametrize('fault', ['uncommitted_manifest', 'stale_payload', 'unlisted_payload'])
def test_legacy_source_binding_rejects_uncommitted_or_incomplete_identity(source, tmp_path, fault):
    git_source(source)
    if fault == 'uncommitted_manifest':
        path = source / 'release-manifest.json'
        manifest = json.loads(path.read_text()); manifest['note'] = 'Uncommitted source fixture.'
        path.write_text(json.dumps(manifest), encoding='utf-8')
    else:
        path = source / ('forge/app.py' if fault == 'stale_payload' else 'forge/unlisted.py')
        path.write_bytes(b'# Source byte missing from the committed inventory.\n')
        commit_source(source, 'Intentionally stale inventory fixture')
    with pytest.raises(ValueError, match='Commit the verified source manifest|complete legacy application payload'):
        package_release.build(tmp_path / 'Build')
    assert not (tmp_path / 'Build').exists()
