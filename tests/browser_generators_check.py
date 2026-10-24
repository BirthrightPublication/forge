"""Real Chromium acceptance checks for the FORGE generator workspace.

This harness creates its own temporary data directory and loopback service. It
cannot target a production runtime. Only the requested JSON report and screenshots
survive; generated values and downloaded test exports are never included in them.

Example:
    .venv/bin/python tests/browser_generators_check.py --output /tmp/forge-generators-report
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from forge import __version__  # noqa: E402
from forge.app import create_app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Directory for the results JSON and cleared-value screenshots.')
    parser.add_argument('--chromium', default=shutil.which('chromium'), help='Chromium executable; defaults to PATH or Playwright browser.')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    checks: list[str] = []
    report = {'kind': 'direct-loopback-generator-browser-acceptance', 'version': __version__,
              'platform': sys.platform, 'data_isolation': 'Harness-owned temporary directory; removed on exit.',
              'timer_validation': 'Chromium virtual clock advances the actual application timer by 61 seconds.',
              'reference_image_comparison': 'Not performed by this functional acceptance harness.',
              'checks': checks, 'screenshots': [], 'status': 'running'}
    token = secrets.token_urlsafe(32)
    sensitive_values: list[str] = []
    browser_errors: list[str] = []
    console_messages: list[str] = []
    browser = None
    app = None
    server = None
    thread = None

    def check(name, condition=True):
        report['current_check'] = name
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS', name, flush=True)

    def screenshot(page, name):
        # Screenshots only show cleared results, never generated passwords.
        page.evaluate('window.ForgeGenerators.clear()')
        page.screenshot(path=str(output / name), full_page=True)
        report['screenshots'].append(name)

    with tempfile.TemporaryDirectory(prefix='forge-generator-browser-') as temporary:
        data = Path(temporary) / 'data'
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        try:
            app = create_app(data, token, port, workers=False)
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 15
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.025)
            check('Harness starts its own isolated loopback service', server.started)

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.chromium, headless=True, args=['--no-sandbox'])
                context = browser.new_context(viewport={'width': 1440, 'height': 980}, device_scale_factor=1)
                page = context.new_page()
                page.set_default_timeout(12000)
                page.on('pageerror', lambda error: browser_errors.append(str(error)))
                page.on('console', lambda message: console_messages.append(message.text))
                unauthenticated = playwright.request.new_context(base_url=origin)
                check('Unauthenticated generation is rejected', unauthenticated.post('/api/generators/generate', data={'tool': 'strings'}).status == 401)
                unauthenticated.dispose()
                page.goto(origin + '/#token=' + token)
                page.wait_for_function('()=>state!==null');page.evaluate("navigate('overview')");page.wait_for_selector('.tool-card')
                check('Launcher authorization is removed from the browser URL', '#' not in page.url and token not in page.url)
                check('All four generator tools are present in the actual navigation',
                      all(page.locator(f'#sidebar [data-view="{tool}"]').count() for tool in ['identity', 'obfuscation', 'addresses', 'strings']))
                check('Authorized browser still requires CSRF on generation',
                      context.request.post(origin + '/api/generators/generate', data={'tool': 'strings'}).status == 403)
                check('Cross-origin generation is rejected', context.request.post(origin + '/api/generators/generate',
                      headers={'Origin': 'https://example.test'}, data={'tool': 'strings'}).status == 403)
                initial_jobs = context.request.get(origin + '/api/state').json()['jobs']
                bodies = {}

                def nav(tool):
                    page.locator(f'#sidebar [data-view="{tool}"]').first.click()
                    page.wait_for_selector('#generator-generate')

                def generate(tool):
                    with page.expect_response(lambda response: response.url == origin + '/api/generators/generate') as pending:
                        page.locator('#generator-generate').click()
                    response = pending.value
                    check(tool + ': actual generator request succeeds without caching', response.status == 200 and response.headers.get('cache-control') == 'no-store')
                    body = response.json()
                    page.wait_for_selector('.generator-values li')
                    check(tool + ': displayed values match the real API batch', (page.locator('.address-field input').first.input_value()==body['rows'][0]['street'] if tool=='addresses' else page.locator('.generator-values li').all_text_contents() == [row['value'] for row in body['rows']]))
                    return body

                def export_current(format):
                    with page.expect_download() as pending:
                        page.locator(f'[data-generator-export="{format}"]').click()
                    download = pending.value
                    check('Browser ' + format.upper() + ' export has the requested filename', download.suggested_filename.endswith('.' + format))
                    text = Path(download.path()).read_text(encoding='utf-8')
                    download.delete()
                    return text

                for tool in ['identity', 'obfuscation', 'addresses', 'strings']:
                    nav(tool)
                    check(tool + ': settings use accessible shared tabs', page.locator('#generator-options [role="tab"]').count() >= 2)
                    tabs = page.locator('#generator-options [role="tab"]')
                    tabs.first.focus()
                    tabs.first.press('ArrowRight')
                    check(tool + ': arrow keys select and focus the next settings tab', tabs.nth(1).get_attribute('aria-selected') == 'true' and tabs.nth(1).evaluate('(element)=>element===document.activeElement'))
                    body = generate(tool)
                    bodies[tool] = body
                    check(tool + ': default batch size and no persistence', body['count'] == (1 if tool=='addresses' else 10) and body['persisted'] is False)
                    if tool != 'strings':
                        check(tool + ': synthetic data is explicitly marked fictional', body['fictional'] and all(row['fictional'] for row in body['rows']))
                    else:
                        sensitive_values.extend(row['value'] for row in body['rows'])
                    check(tool + ': JSON download preserves the current batch', json.loads(export_current('json')) == body)
                    page.locator('#generator-clear').click()
                    check(tool + ': explicit clear removes values and disables export', page.locator('.generator-values li').count() == 0 and page.locator('[data-generator-export="json"]').is_disabled())

                # TXT and CSV serialize the existing result, including secret values,
                # only after an explicit browser download action. Temp files are removed.
                body = generate('strings')
                sensitive_values.extend(row['value'] for row in body['rows'])
                csv_rows = list(csv.DictReader(io.StringIO(export_current('csv'))))
                check('CSV exports every row and escapes formula-leading generated values',
                      len(csv_rows) == body['count'] and all(csv_row['value'] == row['value'] or csv_row['value'] == "'" + row['value'] for csv_row, row in zip(csv_rows, body['rows'])))
                check('TXT export matches the current displayed values', export_current('txt').splitlines() == [row['value'] for row in body['rows']])
                check('CSV serializer escapes spreadsheet formulas and leading whitespace', page.evaluate('''()=>['=1+2','+1','-1','@SUM(A1)','  =1','\ttext'].every(value=>ForgeGenerators.csvCell(value).startsWith('"\\\''))'''))
                check('Generated values never enter browser storage', page.evaluate('''values=>{
                    const stored=JSON.stringify({...localStorage})+JSON.stringify({...sessionStorage});
                    return values.every(value=>!stored.includes(value));
                }''', sensitive_values))

                page.evaluate("navigate('overview')")
                nav('strings')
                check('Leaving and reopening a generator clears the batch', page.locator('.generator-values li').count() == 0)
                page.locator('[role="tab"][aria-label="Alphabet"]').click()
                page.locator('[name="alphabet"]').select_option('custom')
                page.locator('[name="custom_chars"]').fill('abcXYZ37')
                custom = generate('strings')
                sensitive_values.extend(row['value'] for row in custom['rows'])
                check('Custom alphabet and exclusions are enforced by the real service', all(set(row['value']) <= set('abcXYZ37') for row in custom['rows']))

                preset_title = 'Generator test <img src=x onerror=alert(1)>'
                page.once('dialog', lambda dialog: dialog.accept(preset_title))
                page.locator('#generator-save').click()
                page.wait_for_function('(title)=>[...document.querySelector("#generator-presets").options].some(option=>option.textContent===title)', arg=preset_title)
                check('Preset names render as text without injecting HTML', page.locator('.heading-actions img').count() == 0)
                saved_presets = context.request.get(origin + '/api/generators/presets').json()['presets']
                check('Preset API stores options and title without result rows', len(saved_presets) == 1 and set(saved_presets[0]) == {'id', 'name', 'tool', 'options', 'schema_version'} and saved_presets[0]['options']['custom_chars'] == 'abcXYZ37')
                preset_id = saved_presets[0]['id']
                page.locator('[name="custom_chars"]').fill('different')
                page.locator('#generator-presets').select_option('')
                page.locator('#generator-presets').select_option(preset_id)
                page.wait_for_selector('#generator-generate')
                page.wait_for_function('()=>document.querySelector("[name=custom_chars]")?.value==="abcXYZ37"')
                check('Loading a preset restores its validated character policy', page.locator('[name="custom_chars"]').input_value() == 'abcXYZ37')
                page.once('dialog', lambda dialog: dialog.accept('Renamed generator test'))
                page.locator('#generator-rename').click()
                page.wait_for_function('()=>document.querySelector("#generator-presets")?.selectedOptions[0]?.textContent==="Renamed generator test"')
                check('Preset rename preserves its identifier', page.locator('#generator-presets').input_value() == preset_id)
                page.locator('#generator-delete').click()
                page.wait_for_function('()=>document.querySelector("#generator-presets")?.options.length===1')
                check('Preset removal persists through the real API', context.request.get(origin + '/api/generators/presets').json()['presets'] == [])

                # HTML-significant values must still be displayed through textContent.
                page.locator('[name="custom_chars"]').fill('<>&"\'')
                markup = generate('strings')
                check('Generated markup characters are rendered as text', all(set(row['value']) <= set('<>&"\'') for row in markup['rows']) and page.locator('#generator-output li *').count() == 0)

                page.clock.install()
                page.locator('[name="custom_chars"]').fill('abcXYZ37')
                generate('strings')
                page.clock.fast_forward(61000)
                check('Application timer clears values after sixty seconds', page.locator('.generator-values li').count() == 0 and '60 seconds' in page.locator('#generator-output').inner_text())
                generate('strings')
                # Headless Chromium does not consistently hide background tabs;
                # dispatch the visibility event with document.hidden set explicitly.
                page.evaluate('''()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});
                    document.dispatchEvent(new Event('visibilitychange'));delete document.hidden;}''')
                check('Hidden-tab event clears values (simulated visibility transition)', page.locator('.generator-values li').count() == 0)
                generate('strings')
                page.locator('#search').fill('identity')
                page.wait_for_function('()=>state!==null');page.evaluate("navigate('overview')");page.wait_for_selector('.tool-card')
                nav('strings')
                check('Global search clears generated values', page.locator('.generator-values li').count() == 0)

                invalid = page.evaluate('''async()=>{
                    const response=await fetch('/api/generators/generate',{method:'POST',headers:{'Content-Type':'application/json','X-Forge-CSRF':csrf},
                    body:JSON.stringify({tool:'strings',options:{count:1001}})});return response.status;
                }''')
                check('Batch sizes above the server limit are rejected', invalid == 422)
                check('Generators create no queue or history records', context.request.get(origin + '/api/state').json()['jobs'] == initial_jobs)
                check('Generated secrets are absent from every service data file',
                      all(value.encode() not in path.read_bytes() for path in data.rglob('*') if path.is_file() for value in sensitive_values))
                check('Generated secrets are absent from browser console output', all(value not in message for value in sensitive_values for message in console_messages))

                screenshot(page, 'FORGE-V0.4-generators-desktop.png')
                for width in (768, 390):
                    page.set_viewport_size({'width': width, 'height': 900})
                    for tool in ['identity', 'obfuscation', 'addresses', 'strings']:
                        nav(tool)
                        check(f'{width}px layout has no horizontal page overflow: {tool}', not page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    screenshot(page, f'FORGE-V0.4-generators-{width}px.png')
                check('No JavaScript exceptions during the generator acceptance flow', not browser_errors)
                report['status'] = 'passed'
                browser.close()
                browser = None
        except Exception as error:
            report['status'] = 'failed'
            message = str(error).replace(token, '[redacted]')
            for value in sensitive_values:
                message = message.replace(value, '[generated value redacted]')
            report['failure'] = type(error).__name__ + ': ' + message
            print('FAIL', type(error).__name__, report.get('current_check', 'startup'), flush=True)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if server:
                server.should_exit = True
            if thread:
                thread.join(timeout=10)
            listener.close()
            if app:
                app.state.store.close()
            report['passed'] = len(checks)
            report['browser_error_count'] = len(browser_errors)
            report.pop('current_check', None)
            report_path = output / 'BROWSER_GENERATORS_RESULTS.json'
            report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(f'{report["status"].upper()}: {len(checks)} checks; report: {report_path}', flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
