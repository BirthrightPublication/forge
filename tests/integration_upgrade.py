"""Exercise a real release upgrade and code rollback in a NEW disposable directory.

Pass a prior release ZIP, a candidate release ZIP, and --allow-test-writes.
Versions are read from those releases rather than fixed to one upgrade pair.
This checks Python updater/application behavior on the host OS, not Windows
BAT, PowerShell, native launcher, or browser integration.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('forge_updater', ROOT / 'scripts' / 'update.py')
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_base(archive, destination):
    """Keep even a malformed supplied archive inside the disposable directory."""
    with zipfile.ZipFile(archive) as z:
        seen = set()
        for item in z.infolist():
            name = item.filename.rstrip('/')
            path = PurePosixPath(name)
            mode = item.external_attr >> 16
            if (not name or '\\' in name or ':' in name or path.is_absolute()
                    or '..' in path.parts or str(path) != name
                    or stat.S_ISLNK(mode)
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise RuntimeError('Unsafe base archive entry: ' + item.filename)
            if name.casefold() in seen:
                raise RuntimeError('Duplicate base archive entry: ' + item.filename)
            seen.add(name.casefold())
        z.extractall(destination)


SEED = r"""import json, os
from pathlib import Path
from forge.core import Store
import forge.models as models

data = Path(os.environ['FORGE_DATA_DIR'])
fixture_path = Path(os.environ['FORGE_INTEGRATION_FIXTURE'])
s = Store(data)
settings = {'workers': 3, 'queue_paused': False,
            'default_output': str(data / 'exports'), 'daily_update_check': False}
s.set('settings', {**s.settings(), **settings})
preset = {'tool': 'transcribe', 'name': 'Keep my preset', 'options': {'model': 'base'},
          'output': {'mode': 'source', 'directory': '', 'subfolder': ''}}
s.set('presets', [preset])
workflow = None
favorites = None
if hasattr(models, 'Workflow'):
    workflow = models.Workflow(name='Keep my workflow',
        steps=[{'tool': 'retune', 'options': {'format': 'wav'}}],
        output={'mode': 'default'}).model_dump()
    favorites = ['retune', 'qr']
    s.set('workflows', [workflow])
    s.set('favorites', favorites)
output = data / 'exports' / 'preserved-completed-output.txt'
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(b'Completed before upgrading; never replace this output.\n')
job = s.add_job('qr', 'Preserved baseline history', {
    'tool': 'qr', 'options': {'text': 'old history'}, 'destination': str(output.parent),
    'settings': s.settings(), 'source': None})
s.update_job(job['id'], status='completed', progress=1,
             outputs=[{'name': output.name, 'path': str(output)}])
fixture = {'settings': settings, 'preset': preset, 'workflow': workflow,
           'favorites': favorites, 'old_job_id': job['id'], 'old_output': str(output)}
fixture_path.write_text(json.dumps(fixture), encoding='utf-8')
s.close()
print('Created baseline settings, preset and completed history' +
      (' with workflow/favorites' if workflow else ''))
"""


CHECK_STATE = r"""def check_state(store):
    for key, value in fixture['settings'].items():
        assert store.settings()[key] == value, (key, store.settings()[key], value)
    assert store.get('presets') == [fixture['preset']]
    old = store.job(fixture['old_job_id'])
    assert old['status'] == 'completed'
    assert old['outputs'][0]['path'] == fixture['old_output']
    if fixture['workflow'] is not None:
        assert store.get('workflows') == [fixture['workflow']]
        assert store.get('favorites') == fixture['favorites']
"""


UPGRADED = r"""import hashlib, json, os, time
from pathlib import Path
from fastapi.testclient import TestClient
from forge.app import create_app

fixture_path = Path(os.environ['FORGE_INTEGRATION_FIXTURE'])
fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
""" + CHECK_STATE + r"""
app = create_app(Path(os.environ['FORGE_DATA_DIR']), 'fixture-token', 19765, workers=True)
try:
    with TestClient(app, base_url='http://127.0.0.1:19765',
                    headers={'X-Forge-Token': 'fixture-token'}) as c:
        response = c.get('/api/state')
        assert response.status_code == 200, response.text
        state = response.json()
        assert state['version'] == os.environ['FORGE_EXPECTED_VERSION']
        check_state(app.state.store)
        assert state['presets'] == [fixture['preset']]
        assert any(j['id'] == fixture['old_job_id'] and j['status'] == 'completed'
                   for j in state['jobs'])
        if fixture['workflow'] is not None:
            assert state['workflows'] == [fixture['workflow']]
            assert state['favorites'] == fixture['favorites']
        response = c.post('/api/jobs', json={
            'tool': 'qr', 'options': {'text': 'Post-upgrade real worker', 'name': 'Post-upgrade QR'},
            'output': {'mode': 'default'}})
        assert response.status_code == 200, response.text
        job_id = response.json()['jobs'][0]['id']
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            response = c.get('/api/state')
            assert response.status_code == 200, response.text
            job = next(j for j in response.json()['jobs'] if j['id'] == job_id)
            if job['status'] == 'completed':
                break
            assert job['status'] not in {'failed', 'cancelled', 'interrupted'}, job
            time.sleep(.1)
        assert job['status'] == 'completed', job
        output = Path(job['outputs'][0]['path'])
        assert output.is_file()
        assert output.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
        fixture['new_job_id'] = job_id
        fixture['new_output'] = str(output)
        fixture['new_output_sha256'] = hashlib.sha256(output.read_bytes()).hexdigest()
        fixture_path.write_text(json.dumps(fixture), encoding='utf-8')
finally:
    app.state.store.close()
print('Preserved baseline data, loaded candidate API, completed real QR worker')
"""


ROLLED_BACK = r"""import hashlib, json, os
from pathlib import Path
from forge import __version__
from forge.core import Store

fixture = json.loads(Path(os.environ['FORGE_INTEGRATION_FIXTURE']).read_text(encoding='utf-8'))
assert __version__ == os.environ['FORGE_BASE_VERSION']
""" + CHECK_STATE + r"""
s = Store(Path(os.environ['FORGE_DATA_DIR']))
try:
    check_state(s)
    assert len(s.jobs()) == 2
    new = s.job(fixture['new_job_id'])
    assert new['status'] == 'completed'
    assert new['outputs'][0]['path'] == fixture['new_output']
    output = Path(fixture['new_output'])
    assert hashlib.sha256(output.read_bytes()).hexdigest() == fixture['new_output_sha256']
finally:
    s.close()
print('Baseline code reopened current data; both history entries and new output remain')
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-test-writes', action='store_true')
    args = parser.parse_args()
    if not args.allow_test_writes:
        parser.error('Pass --allow-test-writes for a NEW disposable directory.')
    home = args.output.resolve()
    if home.exists():
        raise RuntimeError('Output directory already exists. Choose a fresh disposable location.')
    base = args.base.resolve()
    candidate = args.build.resolve()
    manifest, _ = up.inspect_release(candidate)
    target_version = manifest['version']
    if not zipfile.is_zipfile(base):
        raise RuntimeError('Base release is not a ZIP archive.')
    home.mkdir(parents=True)
    data = home / 'test-user-data'
    builds = home / 'Build'
    builds.mkdir()
    extract_base(base, home)
    root = up.find_installation(home)
    base_version = up.installed_version(root)
    if up.version(base_version) >= up.version(target_version):
        raise RuntimeError('Candidate must be newer than the supplied baseline.')
    original_code = {p.relative_to(root).as_posix(): digest(p)
                     for p in root.rglob('*') if p.is_file()}
    originals = {rel: sha for rel, sha in original_code.items()
                 if rel.startswith('original_scripts/')}
    markers = {}
    for folder, relative, content in [
        (root, '.venv/keep.txt', b'Private application environment'),
        (root, '.diarization-venv/keep.txt', b'Private speaker environment'),
        (root, 'tools/keep.txt', b'User-supplied tools'),
        (root, 'Config/keep.txt', b'Custom configuration'),
        (root, 'models/keep.bin', b'Installation-local model cache'),
        (root, 'exports/keep.txt', b'Installation-local completed output'),
        (data, 'models/keep.bin', b'Persistent local model cache'),
        (data, 'private-input.txt', b'Original user input; must remain unchanged'),
    ]:
        path = folder / relative
        if path.exists():
            raise RuntimeError('Fixture path unexpectedly exists: ' + str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        markers[path] = digest(path)
    fixture_path = home / 'baseline-fixture.json'
    env = {**os.environ, 'FORGE_DATA_DIR': str(data),
           'FORGE_INTEGRATION_FIXTURE': str(fixture_path),
           'FORGE_EXPECTED_VERSION': target_version, 'FORGE_BASE_VERSION': base_version}

    def python(code):
        result = subprocess.run([sys.executable, '-c', code], cwd=root, env=env,
                                text=True, capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stdout + '\n' + result.stderr)
        print(result.stdout.strip())

    def check_preserved():
        for rel, expected in originals.items():
            assert digest(root / rel) == expected, rel
        for path, expected in markers.items():
            assert digest(path) == expected, str(path)

    python(SEED)
    fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
    old_output = Path(fixture['old_output'])
    markers[old_output] = digest(old_output)
    shutil.copyfile(candidate, builds / ('FORGE-v' + target_version + '.zip'))
    previous = os.environ.get('FORGE_DATA_DIR')
    os.environ['FORGE_DATA_DIR'] = str(data)
    try:
        assert up.main(['--root', str(home), '--yes', '--no-launch']) == 0
        assert up.installed_version(root) == target_version
        backup = Path(json.loads((root / 'update-last.json').read_text())['backup'])
        assert (backup / 'journal.json').is_file()
        assert (backup / 'user-data-snapshot' / 'forge.sqlite3').is_file()
        check_preserved()
        python(UPGRADED)
        fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
        markers[Path(fixture['new_output'])] = fixture['new_output_sha256']
        check_preserved()
        assert up.main(['--root', str(home), '--rollback', '--yes', '--no-launch']) == 0
        assert up.installed_version(root) == base_version
        for rel, expected in original_code.items():
            assert digest(root / rel) == expected, rel
        check_preserved()
        python(ROLLED_BACK)
        checks = [
            'Candidate manifest and source hashes verified',
            'Baseline installation detected at version ' + base_version,
            'Full release installed through updater main at version ' + target_version,
            'Code backup and separate SQLite snapshot created',
            'Baseline settings, preset and completed history preserved',
            'Candidate API and real QR worker executed',
            'Code-only rollback restored original baseline file bytes',
            'Baseline code reopened current database and retained new history/output',
            'Original scripts, private environments, tools, config, model caches, inputs and outputs preserved',
        ]
        if fixture['workflow'] is not None:
            checks.append('Baseline workflow and favorites preserved through upgrade and rollback')
        report = {
            'result': 'passed', 'host': sys.platform,
            'base_version': base_version, 'candidate_version': target_version,
            'base_sha256': digest(base), 'candidate_sha256': digest(candidate),
            'original_files_preserved': len(originals),
            'protected_files_verified': len(markers),
            'baseline_workflow_favorites_tested': fixture['workflow'] is not None,
            'checks': checks, 'native_windows_execution': False,
        }
        (home / 'integration-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, indent=2))
    finally:
        if previous is None:
            os.environ.pop('FORGE_DATA_DIR', None)
        else:
            os.environ['FORGE_DATA_DIR'] = previous


if __name__ == '__main__':
    main()
