"""Owner bootstrap state isolation; native Windows execution is a separate gate."""
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from forge.core import Store
from scripts import verification_launch as verification
from scripts import package_windows_release, update


def test_fresh_verification_has_separate_defaults_and_retains_subsequent_choices(tmp_path, monkeypatch):
    normal = tmp_path / 'normal-data'; normal.mkdir()
    original = normal / 'settings.json'; original.write_text('untouched original settings')
    monkeypatch.setenv('FORGE_DATA_DIR', str(normal))
    monkeypatch.setenv('FORGE_PORT', '8765')
    root = tmp_path / 'candidate'; root.mkdir()
    expected = root / 'Build' / 'V0.10-Verification' / 'Data'
    assert verification.initialize(root) == expected
    assert os.environ['FORGE_DATA_DIR'] == str(expected)
    assert os.environ['FORGE_PORT'] == '18765'
    store = Store(expected)
    settings = store.settings()
    assert settings['automatic_update_checks'] is False and settings['automatic_dependency_repairs'] is False
    assert settings['default_output'] == str(expected.parent / 'Exports')
    settings.update(workers=3, automatic_update_checks=True)
    store.set('settings', settings); store.close()
    verification.initialize(root)
    store = Store(expected)
    assert store.settings()['workers'] == 3 and store.settings()['automatic_update_checks'] is True
    store.close()
    assert original.read_text() == 'untouched original settings'


def test_existing_unmarked_verification_database_is_not_recovered_or_reset(tmp_path, monkeypatch):
    monkeypatch.setenv('FORGE_DATA_DIR', 'old-default');monkeypatch.setenv('FORGE_PORT','8765')
    data = tmp_path / 'Build' / 'V0.10-Verification' / 'Data'
    store = Store(data)
    settings = store.settings();settings['workers'] = 4;store.set('settings', settings)
    job = store.add_job('qr', 'Existing owned work', {})
    store.update_job(job['id'], status='running'); store.close()
    verification.initialize(tmp_path)
    with sqlite3.connect(data / 'forge.sqlite3') as db:
        assert db.execute('SELECT status FROM jobs WHERE id=?',(job['id'],)).fetchone()[0] == 'running'
        assert json.loads(db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0])['workers'] == 4
    assert json.loads((data / 'verification-bootstrap.json').read_text())['initial_defaults_applied'] is False


def test_linked_build_cannot_redirect_bootstrap_into_existing_data(tmp_path, monkeypatch):
    outside = tmp_path / 'existing';outside.mkdir()
    candidate = tmp_path / 'candidate';candidate.mkdir()
    try:
        (candidate / 'Build').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('This platform cannot create the test directory symlink.')
    with pytest.raises(RuntimeError, match='linked existing data'):
        verification.initialize(candidate)
    assert list(outside.iterdir()) == []


def test_main_uses_official_launcher_without_registering_forwarded_sources(monkeypatch):
    import launcher
    calls=[]
    monkeypatch.setattr(verification, 'initialize', lambda: calls.append('isolated'))
    monkeypatch.setattr(launcher, 'main', lambda: calls.append(sys.argv[:]))
    monkeypatch.setattr(sys, 'argv', ['verification_launch.py','some-private-file.mp4'])
    verification.main()
    assert calls == ['isolated', [str(verification.ROOT / 'launcher.py')]]
    assert sys.argv == ['verification_launch.py','some-private-file.mp4']


def test_bootstrap_is_optional_packaged_source_and_skips_shortcuts():
    name = 'Run V0.10 Verification.bat'
    assert name not in update.ROOT_FILES and name not in package_windows_release.ROOT_FILES
    assert 'scripts' in update.APP_DIRS and 'scripts' in package_windows_release.APP_DIRS
    assert update.safe_relative('scripts/' + name)
    body = (verification.ROOT / 'scripts' / name).read_text()
    assert 'setlocal DisableDelayedExpansion' in body
    assert 'Build\\V0.10-Verification\\Data' in body and 'FORGE_PORT=18765' in body
    assert 'if not exist "%FORGE_VERIFICATION_ROOT%\\.venv\\Scripts\\python.exe"' in body
    assert '-NoShortcuts' in body and 'verification_launch.py' in body
    assert 'LOCALAPPDATA' not in body and '%*' not in body
