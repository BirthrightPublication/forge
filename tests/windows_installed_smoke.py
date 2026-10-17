"""Smoke-test the application installed by the exact native updater.

All state and generated outputs must live in a fresh disposable directory under
the host's temporary folder. A Linux run can validate this helper but is never
reported as native Windows acceptance.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application-root', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--expected-version', required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    root = args.application_root.resolve()
    data = args.data_dir.resolve()
    report_path = args.report.resolve()
    temporary = [Path(tempfile.gettempdir()).resolve()]
    if os.environ.get('RUNNER_TEMP'):
        temporary.append(Path(os.environ['RUNNER_TEMP']).resolve())
    for path in (root, data, report_path):
        if not any(path.is_relative_to(parent) and path != parent for parent in temporary):
            parser.error('Application, data and report paths must be inside disposable temporary folders.')
    if data.exists():
        parser.error('The smoke data directory already exists; choose a fresh directory.')
    if not (root / 'forge' / 'app.py').is_file():
        parser.error('Installed application code was not found.')
    checks: list[str] = []
    report = {
        'schema_version': 1,
        'status': 'failed',
        'native_windows_execution': os.name == 'nt',
        'platform': sys.platform,
        'python_version': sys.version,
        'expected_version': args.expected_version,
        'installed_version': None,
        'registry_count': 0,
        'checks': checks,
        'passed': 0,
        'failure': None,
    }

    def check(label: str, condition: bool = True):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS ' + label, flush=True)

    app = None
    try:
        os.environ['FORGE_DATA_DIR'] = str(data)
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(root))
        from fastapi.testclient import TestClient
        from PIL import Image
        import forge
        from forge.app import create_app
        from forge.core import TOOLS

        check('Imports resolve to the updater-installed application', Path(forge.__file__).resolve().is_relative_to(root))
        report['installed_version'] = forge.__version__
        check('Installed Python version matches the package VERSION.txt', forge.__version__ == args.expected_version)
        check('Installed VERSION.txt matches the expected version', (root / 'VERSION.txt').read_text(encoding='utf-8').strip() == args.expected_version)
        report['registry_count'] = len(TOOLS)
        check('Installed registry preserves the existing Studio tools', {
            'transcribe', 'retune', 'download_audio', 'download_video', 'metadata', 'qr',
            'password', 'identity', 'obfuscation', 'addresses', 'strings',
        }.issubset(TOOLS))
        secret = secrets.token_urlsafe(32)
        app = create_app(data, secret, 19765, workers=True)
        settings = app.state.store.settings()
        settings.update(default_output=str(data / 'exports'), daily_update_check=False, queue_paused=False)
        app.state.store.set('settings', settings)
        with TestClient(app, base_url='http://127.0.0.1:19765') as client:
            response = client.get('/api/state')
            check('Unauthenticated state requests are denied', response.status_code in {401, 403})
            client.headers['X-Forge-Token'] = secret
            response = client.get('/api/state')
            check('Authenticated local API starts successfully', response.status_code == 200)
            state = response.json()
            check('Authenticated API exposes the installed version', state['version'] == args.expected_version)
            check('Authenticated API exposes the complete installed tool registry', set(state['tools']) == set(TOOLS))
            check('Development installation retains offline feature access', all(item['enabled'] for item in state['entitlements']['modules']))
            check('Installed browser shell is served', client.get('/').status_code == 200)
            response = client.post('/api/jobs', json={
                'tool': 'qr',
                'options': {'text': 'FORGE native Windows installed-app acceptance', 'name': 'Installed application QR'},
                'output': {'mode': 'default'},
            })
            check('Real QR work is accepted by the installed queue', response.status_code == 200)
            job_id = response.json()['jobs'][0]['id']
            deadline = time.monotonic() + 45
            job = {}
            while time.monotonic() < deadline:
                response = client.get('/api/state')
                if response.status_code != 200:
                    raise AssertionError('State became unavailable during the worker smoke test.')
                job = next(item for item in response.json()['jobs'] if item['id'] == job_id)
                if job['status'] == 'completed':
                    break
                if job['status'] in {'failed', 'cancelled', 'interrupted'}:
                    raise AssertionError('QR worker did not complete: ' + job['status'] + '; ' + job.get('message', ''))
                time.sleep(.15)
            check('Installed worker completes a real queued job', job.get('status') == 'completed')
            check('QR job publishes one output', len(job['outputs']) == 1)
            output = Path(job['outputs'][0]['path']).resolve()
            check('QR output stays inside disposable user data', output.is_relative_to(data))
            check('Published QR has a real PNG signature', output.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
            with Image.open(output) as image:
                image.load()
                check('Published QR contains square image pixels', image.width == image.height and image.width >= 100)
                colors = image.convert('RGB').getcolors(maxcolors=256)
                check('Published QR contains distinct dark and light modules', bool(colors) and len(colors) >= 2)
            response = client.get(f'/api/jobs/{job_id}/outputs/0')
            check('Authenticated output download serves the generated PNG', response.status_code == 200 and response.content == output.read_bytes())
            check('Installed application retains the completed queue history', app.state.store.job(job_id)['status'] == 'completed')
        report['status'] = 'passed'
    except Exception as exc:
        report['failure'] = str(exc)
    finally:
        if app is not None:
            app.state.store.close()
        report['passed'] = len(checks)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    if report['status'] != 'passed':
        print('FAIL ' + str(report['failure']), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
