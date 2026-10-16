"""Direct Chromium acceptance checks for FORGE V0.4.

Uses a real, already-running disposable service, browser authorization, served
assets and real worker jobs. Never point this harness at an owner's data folder.
Requires --allow-test-writes because it changes settings, adds presets and creates
jobs. It also inserts and cleans 502 explicitly labeled terminal fixtures to check
history retention; those rows do not represent processed media. All generated
inputs and output artifacts stay in the requested test folder.
Domain-specific generator and Black Scripting execution tests are separate.
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sqlite3
import sys
import time
import uuid
import urllib.request
from pathlib import Path

from PIL import Image, PngImagePlugin
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--chromium', default=shutil.which('chromium'))
parser.add_argument('--allow-test-writes', action='store_true')
args = parser.parse_args()
if not args.allow_test_writes:
    parser.error('Use a disposable runtime and explicitly pass --allow-test-writes.')
runtime = json.loads(args.runtime.read_text(encoding='utf-8'))
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
origin = f'http://127.0.0.1:{int(runtime["port"])}'
marker = 'V04 Browser ' + str(time.time_ns())
checks: list[str] = []
errors: list[str] = []
requests: list[str] = []
report = {'kind': 'direct-loopback-browser-acceptance', 'platform': sys.platform,
          'reference_image_comparison': 'Not performed: reference asset was not supplied.',
          'checks': checks, 'browser_errors': errors, 'screenshots': []}
launch_ticket = ''


def check(name: str, condition=True):
    if not condition:
        raise AssertionError(name)
    checks.append(name)
    print('PASS', name, flush=True)


def screenshot(page, name):
    page.evaluate('()=>document.querySelector("#toast")?.classList.add("hidden")')
    page.screenshot(path=str(output / name), full_page=True)
    report['screenshots'].append(name)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=args.chromium, headless=True,
                                         args=['--no-sandbox'])
    context = browser.new_context(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1)
    page = context.new_page()
    page.set_default_timeout(12000)
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: requests.append(request.url.split(origin)[-1])
            if request.url.startswith(origin + '/api/') else None)
    dialog_value = {'prompt': marker + ' preset'}
    page.on('dialog', lambda dialog: dialog.accept(dialog_value['prompt']) if dialog.type == 'prompt' else dialog.accept())

    def api(route, body=None):
        return page.evaluate('([route,body])=>body===null?api(route):api(route,body)', [route, body])

    def nav(tool):
        page.evaluate('(tool)=>navigate(tool)', tool)
        page.wait_for_function('(tool)=>view===tool && !!document.querySelector("h1")', arg=tool)
        if tool in ['identity', 'obfuscation', 'addresses', 'strings']:
            page.wait_for_selector('#generator-options')

    def wait_job(job_id, status):
        page.wait_for_function('async ([id,status])=>{await refresh();return state.jobs.some(j=>j.id===id&&j.status===status)}',
                               arg=[job_id, status], timeout=60000)

    def active_job_ids():
        return page.evaluate('()=>state.jobs.filter(j=>j.status==="queued").map(j=>j.id)')

    try:
        # Match the Windows launcher: the reusable native token stays outside
        # Chromium; only its short-lived one-use launch ticket enters the page.
        request = urllib.request.Request(origin + '/api/launch-ticket', data=b'{}',
                                         headers={'Content-Type': 'application/json', 'X-Forge-Token': runtime['token']})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=15) as response:
            launch_ticket = json.load(response)['ticket']
        # Avoid emitting a ticket-bearing URL if startup fails.
        try:
            page.goto(origin + '/#launch=' + launch_ticket)
        except Exception as error:
            raise RuntimeError('Cannot open the disposable FORGE service; verify the runtime port.') from None
        page.wait_for_selector('.tool-card')
        check('Authorized browser loads the served V0.4 application', page.locator('.version').inner_text().startswith('V0.4'))
        check('Launcher authorization clears temporary ticket and token from the page',
              page.evaluate('()=>token===""&&launchTicket===""&&location.hash===""'))
        check('Overview contains eleven available tools', page.locator('.tool-card').count() == 11)
        check('Black Scripting global launcher remains outside sidebar',
              page.locator('#black-scripting-launcher').is_visible() and page.locator('#sidebar [data-view="workflows"],#sidebar [data-favorite-nav="workflows"]').count() == 0)
        check('FORGE OS has its own seven-tool sidebar group', page.locator('#forge-os-nav [data-view]').count() == 7)
        check('Ordinary startup does not scan release ZIPs', '/api/application-updates' not in requests)
        screenshot(page, 'FORGE-V0.4-overview.png')

        second = context.new_page()
        second.goto(origin)
        second.wait_for_selector('.tool-card')
        check('A second browser tab uses remembered authorization', second.locator('.tool-card').count() == 11)
        second.close()

        tools = ['transcribe', 'retune', 'download_audio', 'download_video', 'metadata', 'qr', 'password',
                 'identity', 'obfuscation', 'addresses', 'strings', 'queue', 'settings']
        for tool in tools:
            nav(tool)
            check('Desktop navigation: ' + tool, page.locator('h1').count() == 1)
            check('Desktop layout fits its viewport: ' + tool,
                  not page.evaluate('document.documentElement.scrollWidth>innerWidth'))
        page.locator('#black-scripting-launcher').click()
        page.wait_for_selector('.black-workbench')
        check('Global launcher opens a full-width empty Black Scripting workbench',
              not page.locator('#sidebar').is_visible() and page.locator('.black-workbench').is_visible())
        screenshot(page, 'FORGE-V0.4-black-scripting-empty.png')

        nav('transcribe')
        model = page.get_by_role('tab', name='Recognition model', exact=True)
        model.click()
        page.select_option('[name="model"]', 'tiny')
        model.focus()
        page.keyboard.press('ArrowRight')
        check('Arrow keys activate the next settings tab', page.get_by_role('tab', name='Language and vocabulary', exact=True).get_attribute('aria-selected') == 'true')
        page.get_by_role('tab', name='Recognition accuracy', exact=True).click()
        page.fill('[name="beam_size"]', '7')
        check('Only the active settings section is visible', page.locator('#tool-options .v4-tab-panel:visible').count() == 1)
        page.locator('#save-preset').click()
        page.wait_for_function('(name)=>state.presets.some(p=>p.tool==="transcribe"&&p.name===name)', arg=dialog_value['prompt'])
        check('Tool preset persists settings from hidden sections',
              page.evaluate('(name)=>{const p=state.presets.find(p=>p.name===name);return p.options.model==="tiny"&&p.options.beam_size===7}', dialog_value['prompt']))
        nav('retune'); nav('transcribe')
        check('Draft settings survive navigation', page.evaluate('()=>draft().options.model==="tiny"&&draft().options.beam_size===7'))
        page.get_by_role('tab', name='Recognition model', exact=True).click()
        page.select_option('[name="model"]', 'base')
        page.select_option('#presets', label=dialog_value['prompt'])
        check('Saved preset restores hidden and visible options', page.evaluate('()=>draft().options.model==="tiny"&&draft().options.beam_size===7'))
        screenshot(page, 'FORGE-V0.4-transcribe.png')

        initial_settings = api('/state')['settings']
        test_settings = {**initial_settings, 'default_output': str(output / 'exports'), 'daily_update_check': False}
        api('/settings', test_settings)
        api('/queue', {'paused': True})
        page.evaluate('()=>refresh(false)')
        nav('qr')
        page.fill('[name="text"]', 'https://example.com/forge-v04-browser-check')
        page.locator('#qr-local-preview').click()
        page.wait_for_selector('#qr-local-preview-area img')
        check('Plain-text QR produces a local image and explicit download link', page.locator('#qr-save-preview').is_visible())
        page.locator('#qr-clear-preview').click()
        check('Clearing a local QR preview removes its downloadable object URL', page.locator('#qr-local-preview-area img').count() == 0 and page.locator('#qr-save-preview').get_attribute('href') is None)
        page.select_option('[name="kind"]', 'wifi')
        page.fill('[name="wifi_ssid"]', 'FORGE test network')
        page.fill('[name="wifi_password"]', 'disposable-test-value')
        jobs_before = len(api('/state')['jobs'])
        page.locator('#enqueue-top').click()
        page.wait_for_selector('#qr-local-preview-area img')
        check('Wi-Fi primary action previews without a persistent queue record', len(api('/state')['jobs']) == jobs_before)
        check('Wi-Fi disables preset saving and exposes its privacy warning',
              page.locator('#save-preset').is_disabled() and 'Wi-Fi codes contain network credentials' in page.locator('.v4-source').inner_text())
        page.select_option('[name="kind"]', 'contact')
        page.fill('[name="contact_name"]', 'Fictional Browser Test')
        page.fill('[name="contact_email"]', 'test@example.com')
        page.locator('#qr-local-preview').click()
        page.wait_for_selector('#qr-local-preview-area img')
        check('Contact QR captures fields in the centered primary input', page.evaluate('()=>draft().options.contact.name==="Fictional Browser Test"'))
        page.locator('#qr-clear-preview').click()
        page.select_option('[name="kind"]', 'text')
        page.fill('[name="text"]', 'V0.4 browser queue test')
        page.get_by_role('tab', name='Output format', exact=True).click()
        page.fill('[name="name"]', marker + ' QR')
        page.locator('#enqueue').click()
        page.wait_for_selector('#queue-list')
        qr_id = next(j['id'] for j in api('/state')['jobs'] if j['label'] == marker + ' QR')
        check('Add to Queue preserves paused dispatch and creates a queued job',
              api('/state')['settings']['queue_paused'] and next(j['status'] for j in api('/state')['jobs'] if j['id'] == qr_id) == 'queued')

        # Browser uploads use real staging; preserve source dimensions and input.
        image = Image.new('RGB', (21, 17), (32, 65, 99))
        metadata = PngImagePlugin.PngInfo(); metadata.add_text('Author', 'Fictional browser fixture')
        stream = io.BytesIO(); image.save(stream, format='PNG', pnginfo=metadata)
        nav('metadata')
        page.locator('#browser-files').set_input_files({'name': marker + '.png', 'mimeType': 'image/png', 'buffer': stream.getvalue()})
        page.wait_for_selector('.file-item')
        check('Browser import visibly distinguishes a staged copy and selects safe output',
              'Staged copy' in page.locator('.file-item').inner_text() and page.locator('#out-mode').input_value() == 'default')
        staged_source = page.evaluate('()=>draft().sources[0].path')
        page.locator('#enqueue').click()
        page.wait_for_selector('#queue-list')
        metadata_id = next(j['id'] for j in api('/state')['jobs'] if j['tool'] == 'metadata' and marker in j['label'])
        check('Multiple independent tools share the Active & Queued table', page.locator('.queue3-row').count() >= 2)
        screenshot(page, 'FORGE-V0.4-queue-active.png')
        page.locator('#select-visible').click()
        check('Select all visible checks every displayed row', page.locator('.job-select:checked').count() == page.locator('.queue3-row').count())
        page.locator('#clear-selection').click()
        check('Clear selection disables bulk record actions', page.locator('.job-select:checked').count() == 0 and page.locator('#remove-selected').is_disabled())
        page.locator(f'[data-select-job="{metadata_id}"]').check()
        with page.expect_response(lambda response: '/api/queue/actions' in response.url):
            page.locator('#up-selected').click()
        check('Selected queued jobs accept priority reordering', not page.locator('#toast').get_attribute('class').endswith('error'))
        page.locator('#clear-selection').click()
        page.locator(f'[data-select-job="{qr_id}"]').check()
        page.locator('#cancel-selected').click()
        wait_job(qr_id, 'cancelled')
        check('Bulk cancellation moves cancelled records out of the active view', page.locator(f'[data-row-job="{qr_id}"]').count() == 0)
        page.locator('[data-filter="history"]').click()
        check('History shows cancelled records', page.locator(f'[data-row-job="{qr_id}"]').is_visible())
        page.locator(f'[data-select-job="{qr_id}"]').check()
        before_retry = set(active_job_ids())
        with page.expect_response(lambda response: '/api/queue/actions' in response.url):
            page.locator('#retry-selected').click()
        page.wait_for_function('(old)=>state.jobs.some(j=>j.status==="queued"&&!old.includes(j.id))', arg=list(before_retry))
        retry_id = next(job for job in active_job_ids() if job not in before_retry)
        check('Circular-arrow Retry creates a fresh queued job', retry_id != qr_id)
        page.locator(f'[data-select-job="{qr_id}"]').check()
        page.locator('#remove-selected').click()
        page.wait_for_function('(id)=>!state.jobs.some(j=>j.id===id)', arg=qr_id)
        check('Remove deletes a selected terminal queue record', page.locator(f'[data-row-job="{qr_id}"]').count() == 0)
        page.locator('[data-filter="failed"]').click()
        check('Failed view has a visible count badge', page.locator('#failed-count').is_visible())
        page.locator('[data-filter="active"]').click()
        page.locator('#resume-queue').click()
        wait_job(metadata_id, 'completed'); wait_job(retry_id, 'completed')
        check('Resume dispatch processes real metadata and QR jobs to completion', True)
        page.locator('[data-filter="history"]').click()
        check('Queue view and minimized counts update together after completion',
              page.evaluate('()=>{const n=state.jobs.filter(j=>j.status==="queued").length;return document.querySelector("#dock-status").innerText.includes(n+" queued")}'))
        screenshot(page, 'FORGE-V0.4-queue-history.png')
        page.locator(f'[data-row-job="{metadata_id}"] [data-job]').click()
        page.wait_for_selector('#dialog[open] .output-item')
        check('Job details expose each output with open and folder actions',
              page.locator('#dialog .output-item').count() > 0 and page.locator('#dialog .output-item button[aria-label^="Open "]').count() == page.locator('#dialog .output-item').count() and page.locator('#dialog .output-item [data-reveal]').count() == page.locator('#dialog .output-item').count())
        check('Metadata output file was published without deleting the input',
              Path(staged_source).is_file() and all(Path(item['path']).is_file() for item in next(j['outputs'] for j in api('/state')['jobs'] if j['id'] == metadata_id)))
        with page.expect_response(lambda response: '/open-output/' in response.url) as opened:
            page.locator('#dialog .output-item button[aria-label^="Open "]').first.click()
        native_result = opened.value.json()
        check('Open file uses the guarded output API and reports platform availability',
              opened.value.ok and isinstance(native_result.get('opened'), bool))
        report['native_output_opened'] = native_result['opened']
        page.locator('#dialog form button').click()
        retry_outputs = next(j['outputs'] for j in api('/state')['jobs'] if j['id'] == retry_id)
        page.locator('#clear-selection').click()
        page.locator(f'[data-select-job="{retry_id}"]').check()
        page.locator('#remove-selected').click()
        page.wait_for_function('(id)=>!state.jobs.some(j=>j.id===id)', arg=retry_id)
        check('Removing a completed queue record preserves its physical output files',
              bool(retry_outputs) and all(Path(item['path']).is_file() for item in retry_outputs))

        # Inspect trustworthy measured-vs-unknown rendering using clearly identified
        # browser-only fixtures; these do not enter the persistent processing queue.
        progress_test = page.evaluate('''()=>{
          const a={label:'Browser-only progress fixture',status:'running',progress:71,progress_measured:false,stage:'Loading model'};
          const b={...a,progress_measured:true,speed:'2x',eta_seconds:20};
          return {unknown:v4ProgressHTML(a),measured:v4ProgressHTML(b)};
        }''')
        check('Unknown engine progress never becomes an invented percentage', '71%' not in progress_test['unknown'] and 'progress max' not in progress_test['unknown'])
        check('Measured engine progress includes real percent and ETA', '71%' in progress_test['measured'] and 'remaining' in progress_test['measured'])
        dock_test = page.evaluate('''()=>{const original=state.jobs;state.jobs=[{id:'browser-only-dock-fixture',tool:'transcribe',label:'Browser-only transcription fixture',status:'running',progress:35,progress_measured:true}];renderDock();const text=document.querySelector('#dock-status').innerText;state.jobs=original;renderDock();return text;}''')
        check('Minimized queue identifies active transcription and measured progress',
              'Browser-only transcription fixture' in dock_test and 'Transcribe' in dock_test and '1 running' in dock_test and '35%' in dock_test)

        nav('settings')
        page.select_option('[name="concurrency_mode"]', 'auto')
        page.select_option('[name="workers"]', '2')
        page.fill('[name="gpu_memory_reserve_mb"]', '1024')
        page.locator('#save-settings').click()
        page.wait_for_function('()=>state.settings.workers===2')
        check('Resource settings persist without dropping existing configuration',
              api('/state')['settings']['workers'] == 2 and api('/state')['settings']['default_output'] == str(output / 'exports'))
        page.get_by_role('tab', name='Application and package updates', exact=True).click()
        check('V0.3 update panel remains available without automatic scanning',
              page.locator('#application-updates-panel').is_visible() and '/api/application-updates' not in requests)
        page.locator('#scan-application-updates').click()
        page.wait_for_function('()=>applicationUpdates!==null||!!applicationUpdateError')
        check('Explicit local update scan still operates', '/api/application-updates' in requests and page.locator('#application-updates-panel').is_visible())
        screenshot(page, 'FORGE-V0.4-environment-updates.png')

        # Explicit terminal browser fixtures exercise the server's history-window
        # projection. They are never dispatched, and do not represent real work.
        data_dir = Path(api('/state')['data_directory']).resolve()
        if data_dir != args.runtime.resolve().parent:
            raise RuntimeError('History fixtures require runtime.json beside the disposable database.')
        fixture_ids = [uuid.uuid4().hex for _ in range(502)]
        fixture_label = marker + ' browser-only old failure fixture'
        now = time.time()
        connection = sqlite3.connect(data_dir / 'forge.sqlite3', timeout=30)
        try:
            fixture_rows = [(job_id, 'qr', fixture_label if i == 0 else marker + ' browser-only terminal fixture',
                             'failed' if i == 0 else 'completed', 0 if i == 0 else 100,
                             'Browser acceptance fixture, not processed media', '{}', '[]', '[]',
                             now - 1000 if i == 0 else now + i, now)
                            for i, job_id in enumerate(fixture_ids)]
            connection.executemany('INSERT INTO jobs (id,tool,label,status,progress,message,payload,outputs,warnings,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)', fixture_rows)
            connection.commit()
            page.evaluate('()=>refresh(false)')
            nav('queue')
            page.locator('[data-filter="failed"]').click()
            check('An older failure remains visible behind more than 500 newer terminal records',
                  page.locator(f'[data-row-job="{fixture_ids[0]}"]').is_visible())
            check('Failed count uses the complete persisted attention count',
                  int(page.locator('#failed-count').inner_text()) == api('/queue/status')['failed_count'])
        finally:
            connection.executemany('DELETE FROM job_meta WHERE id=?', [(job_id,) for job_id in fixture_ids])
            connection.executemany('DELETE FROM jobs WHERE id=?', [(job_id,) for job_id in fixture_ids])
            connection.commit()
            connection.close()
            page.evaluate('()=>refresh(false)')

        for width in [390, 900]:
            page.set_viewport_size({'width': width, 'height': 900})
            for tool in tools + ['workflows']:
                nav(tool)
                page.mouse.move(0, 0)
                page.evaluate('()=>document.activeElement?.blur()')
                check(f'{width}px responsive page width: {tool}', not page.evaluate('document.documentElement.scrollWidth>innerWidth'))
            nav('transcribe')
            screenshot(page, f'FORGE-V0.4-transcribe-{width}px.png')
        check('No browser JavaScript exceptions across the acceptance flow', not errors)
        report['passed'] = len(checks)
        report['status'] = 'passed'
    except Exception as error:
        report['status'] = 'failed'
        report['passed'] = len(checks)
        # Do not write current URLs, requests bodies or generated values to reports.
        report['failure'] = str(error).replace(runtime.get('token', ''), '[redacted]')
        if launch_ticket:
            report['failure'] = report['failure'].replace(launch_ticket, '[redacted]')
        screenshot(page, 'FORGE-V0.4-browser-failure.png')
        raise
    finally:
        (output / 'BROWSER_V0.4_RESULTS.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        browser.close()
