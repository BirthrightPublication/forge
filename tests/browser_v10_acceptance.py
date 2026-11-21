"""Real browser readiness controls with empty data; no neural/device acceptance."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time

import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from forge.app import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--chromium', default=shutil.which('chromium'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    checks, errors = [], []
    def check(name, condition):
        assert condition, name
        checks.append(name)
    with tempfile.TemporaryDirectory(prefix='forge-local-readiness-') as temporary:
        listener = socket.socket(); listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
        token = secrets.token_urlsafe(30)
        app = create_app(Path(temporary) / 'data', token, port, workers=False)
        settings = app.state.store.settings()
        settings.update(automatic_update_checks=False, daily_update_check=False, automatic_dependency_repairs=False)
        app.state.store.set('settings', settings)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True); thread.start()
        try:
            for _ in range(100):
                if server.started: break
                time.sleep(.1)
            assert server.started
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.chromium, headless=True, args=['--no-sandbox'])
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda error: errors.append(str(error)))
                mutations = []
                page.on('request', lambda request: mutations.append(request.url)
                        if any(part in request.url for part in ('/setup/install', '/oauth/', '/language-publishing/upload')) else None)
                base = f'http://127.0.0.1:{port}'
                page.goto(base + '/#token=' + token)
                page.locator('#sidebar [data-view="dubbing"]').click()
                page.get_by_role('button', name='Verify V0.10 readiness', exact=True).click()
                dialog = page.locator('#forge-v10-readiness')
                dialog.get_by_text('Exact installed test models: Pending', exact=True).wait_for()
                check('Readiness button opens the owner checklist', dialog.is_visible())
                check('Current blocker and next action are explained', 'Current blocker' in dialog.inner_text() and 'Next step' in dialog.inner_text())
                check('GPU discovery is not a passed inference result', 'Physical GPU model execution: Pending' in dialog.inner_text())
                check('Synthetic test consent starts unchecked', not dialog.locator('[data-readiness-consent]').is_checked())
                check('Acceptance cannot run without consent', dialog.locator('[data-readiness-run]').is_disabled())
                dialog.locator('[data-readiness-device]').select_option('cuda')
                check('Owner can choose local CUDA explicitly', dialog.locator('[data-readiness-device]').input_value() == 'cuda')
                dialog.locator('[data-readiness-device]').select_option('cpu')
                dialog.locator('[data-readiness-consent]').check()
                check('Consent enables the local test request', dialog.locator('[data-readiness-run]').is_enabled())
                with page.expect_response(lambda response: response.url.endswith('/api/dubbing/acceptance/run')) as run_response:
                    dialog.locator('[data-readiness-run]').click()
                check('Missing packs are a controlled client blocker, not a server error', run_response.value.status == 400)
                page.wait_for_function('()=>{const p=document.querySelector("[data-readiness-message]");return p&&p.textContent.length>0}')
                check('Missing installed packs produce a controlled blocker', bool(dialog.locator('[data-readiness-message]').inner_text()))
                check('Missing packs do not queue fake success', app.state.store.jobs() == [])
                check('Readiness never installs or uploads', mutations == [])
                check('No listening approval without a generated sample', dialog.locator('[data-readiness-confirm-listening]').count() == 0)
                check('Translation quality stays pending without reviewed actual drafts', 'Owner review of the generated Russian translations: Pending' in dialog.inner_text() and dialog.locator('[data-readiness-confirm-translations]').count() == 0)
                with page.expect_download() as exported:
                    dialog.locator('[data-readiness-export]').click()
                exported.value.save_as(str(args.output / 'readiness-empty.json'))
                report = json.loads((args.output / 'readiness-empty.json').read_text())
                check('Report download is a truthful checklist, not release proof', report['satisfies_release_acceptance'] is False and report['model_evidence'] is None)
                check('Export omits private data-directory paths', str(Path(temporary)) not in json.dumps(report))
                dialog.locator('[data-readiness-close]').click()
                check('Owner can close the checklist', not dialog.is_visible())
                page.get_by_role('button', name='Verify V0.10 readiness', exact=True).click()
                dialog.get_by_role('button', name='Verify Private YouTube upload', exact=True).click()
                publishing = page.locator('#forge-publishing-acceptance')
                publishing.get_by_role('heading', name='Private YouTube verification', exact=True).wait_for()
                check('Fresh installation opens Private YouTube verification without a project', publishing.is_visible() and not dialog.is_visible())
                check('Opening the private wizard does not authorize an OAuth operation or upload', mutations == [] and app.state.store.jobs() == [])
                publishing.get_by_role('button', name='Close verification', exact=True).click()
                check('No uncaught JavaScript errors', errors == [])
                browser.close()
        finally:
            server.should_exit = True; thread.join(timeout=8)
    result = {'status': 'passed', 'passed': len(checks), 'checks': checks, 'errors': errors,
              'model_inference_tested': False, 'physical_gpu_tested': False,
              'scope': 'Actual browser/API readiness controls with empty model data; no actual inference or external account operations.'}
    (args.output / 'BROWSER_LOCAL_ACCEPTANCE.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
