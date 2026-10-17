"""Real Chromium acceptance for the V0.5 Media Privacy Studio.

Creates its own disposable local service and fixtures under --output. Exercises
served assets, native-path registration, actual encodes, reports, presets and
Black Scripting. It never reuses an owner's service or data directory. Windows
native dialogs and NVIDIA execution are not exercised by this Linux harness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--chromium', default=shutil.which('chromium'))
parser.add_argument('--allow-test-writes', action='store_true')
args = parser.parse_args()
if not args.allow_test_writes:
    parser.error('Pass --allow-test-writes to create a disposable service and real media outputs.')
if not args.chromium:
    parser.error('Chromium is required.')
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
run = output / ('run-' + uuid.uuid4().hex[:12])
run.mkdir()
data = run / 'data'
data.mkdir()
fixtures = run / 'fixtures'
fixtures.mkdir()
exports = run / 'exports'
exports.mkdir()
checks, errors, requests = [], [], []
report = {'release': '0.5', 'kind': 'real-direct-browser-media-acceptance', 'platform': sys.platform,
          'checks': checks, 'browser_errors': errors, 'screenshots': [], 'limitations': [
              'Native Windows file dialogs were not exercised.',
              'NVIDIA encoding was not exercised.',
              'No claim is made about proprietary recognition systems.']}


def check(name, condition=True):
    if not condition:
        raise AssertionError(name)
    checks.append(name)
    print('PASS', name, flush=True)


def ffmpeg(*arguments):
    subprocess.run([shutil.which('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-y', *arguments],
                   check=True, timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


video = fixtures / 'original.mp4'
audio = fixtures / 'original.wav'
clip = fixtures / 'identifier.wav'
ffmpeg('-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=12:duration=3', '-f', 'lavfi', '-i',
       'sine=frequency=440:sample_rate=44100:duration=3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
       '-c:a', 'aac', '-metadata', 'title=V05 Private source', '-shortest', str(video))
ffmpeg('-i', str(video), '-vn', '-c:a', 'pcm_s16le', str(audio))
ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=44100:duration=0.4', str(clip))
copy_video, copy_audio = fixtures / 'comparison.mp4', fixtures / 'comparison.wav'
shutil.copyfile(video, copy_video)
shutil.copyfile(audio, copy_audio)
original_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in fixtures.iterdir()}
with socket.socket() as reserve:
    reserve.bind(('127.0.0.1', 0))
    port = reserve.getsockname()[1]
origin = f'http://127.0.0.1:{port}'
env = {**os.environ, 'FORGE_DATA_DIR': str(data), 'FORGE_PORT': str(port), 'PYTHONUNBUFFERED': '1'}
log = (run / 'server.log').open('w')
process = subprocess.Popen([sys.executable, str(ROOT / 'server.py')], cwd=ROOT, env=env,
                           stdout=log, stderr=subprocess.STDOUT)
try:
    deadline = time.monotonic() + 40
    while not (data / 'runtime.json').is_file():
        if process.poll() is not None:
            raise RuntimeError('Disposable service exited during startup; inspect its local server.log.')
        if time.monotonic() > deadline:
            raise RuntimeError('Disposable service did not create its runtime file.')
        time.sleep(.1)
    runtime = json.loads((data / 'runtime.json').read_text())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    ticket_request = urllib.request.Request(origin + '/api/launch-ticket', data=b'{}',
                                           headers={'Content-Type': 'application/json', 'X-Forge-Token': runtime['token']})
    for attempt in range(80):
        try:
            with opener.open(ticket_request, timeout=2) as response:
                ticket = json.load(response)['ticket']
            break
        except OSError:
            time.sleep(.1)
    else:
        raise RuntimeError('Disposable service did not accept browser authorization.')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium, headless=True,
                                             args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1)
        page = context.new_page()
        page.set_default_timeout(15000)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url)
                if not request.url.startswith(('blob:', 'data:')) else None)
        prompt_value = {'value': 'V05 browser watermark'}
        page.on('dialog', lambda dialog: dialog.accept(prompt_value['value']) if dialog.type == 'prompt' else dialog.accept())

        def api(route, body=None):
            return page.evaluate('([route,body])=>body===null?api(route):api(route,body)', [route, body])

        def nav(tool):
            page.evaluate('(tool)=>navigate(tool)', tool)
            page.wait_for_function('(tool)=>view===tool&&!!document.querySelector("h1")', arg=tool)

        def register(paths, tool):
            sources = api('/register', {'paths': [str(path) for path in paths]})['sources']
            page.evaluate('([sources,tool])=>addSources(sources,tool)', [sources, tool])
            return sources

        def wait_job(job_id):
            deadline = time.monotonic() + 180
            while True:
                job = next(job for job in api('/state')['jobs'] if job['id'] == job_id)
                if job['status'] in {'completed', 'failed', 'blocked', 'cancelled', 'interrupted'}:
                    break
                if time.monotonic() > deadline:
                    raise AssertionError('Worker did not reach a terminal state: ' + job['tool'])
                page.wait_for_timeout(200)
            page.evaluate('()=>refresh(false)')
            check('Worker completed ' + job['tool'] + (' · ' + job['status'] + ': ' + job['message'] if job['status'] != 'completed' else ''), job['status'] == 'completed')
            return job

        def newest(tool):
            jobs = [job for job in api('/state')['jobs'] if job['tool'] == tool]
            return max(jobs, key=lambda job: job['created'])

        def screenshot(name):
            page.evaluate('()=>document.querySelector("#toast")?.classList.add("hidden")')
            page.screenshot(path=str(output / name), full_page=True)
            report['screenshots'].append(name)

        def settings_tab(name):
            page.get_by_role('tab', name=name, exact=True).click()

        try:
            try:
                page.goto(origin + '/#launch=' + ticket)
            except Exception:
                raise RuntimeError('Cannot open the disposable FORGE service; verify startup in its local server log.') from None
            page.wait_for_selector('.tool-card')
            check('Authorization clears temporary launch credentials',
                  page.evaluate('()=>token===""&&launchTicket===""&&location.hash===""'))
            check('Overview exposes all eighteen tools', page.locator('.tool-card').count() == 18)
            check('Media Privacy has one ordinary sidebar entry',
                  page.locator('#media-privacy-open').count() == 1 and
                  page.locator('#tool-nav [data-view="watermark"],#tool-nav [data-view="provenance"]').count() == 0)
            check('Black Scripting remains outside ordinary sidebar',
                  page.locator('#sidebar [data-view="workflows"]').count() == 0)
            settings = api('/state')['settings']
            settings.update(default_output=str(exports), queue_paused=False, workers=2, cpu_workers=2)
            api('/settings', settings)
            page.evaluate('()=>refresh(false)')
            catalog = api('/media/privacy/catalog')
            ui_defaults = page.evaluate('()=>Object.fromEntries(ForgePrivacy.ids.map(id=>[id,defaults[id]]))')
            for tool, definition in catalog['tools'].items():
                check('UI defaults match validated schema: ' + tool, ui_defaults[tool] == definition['defaults'])
                nav(tool)
                check('Compact existing Studio layout: ' + tool,
                      page.locator('.studio-v4 .v4-source').count() == 1 and page.locator('.v5-tool-nav button').count() == 7)
                check('Every option has an accessible form control: ' + tool,
                      page.evaluate('(keys)=>keys.every(key=>!!document.querySelector(`#tool-options [name="${key}"]`))',
                                    list(definition['schema']['properties'])))
                check('Desktop layout fits: ' + tool, not page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                tabs = page.locator('#tool-options [role="tab"]')
                tabs.first.focus()
                page.keyboard.press('ArrowRight')
                check('Settings tabs support keyboard selection: ' + tool,
                      page.locator('#tool-options [role="tab"][aria-selected="true"]').count() == 1)
            nav('watermark')
            settings_tab('Watermark content')
            page.fill('[name="text"]', 'FORGE · Owner')
            settings_tab('Appearance')
            page.fill('[name="font_size"]', '24')
            settings_tab('Timing and movement')
            page.fill('[name="movement_bounds"]', '0.1, 0.1, 0.9, 0.9')
            page.fill('[name="end_seconds"]', '')
            page.evaluate('()=>capture()')
            captured = page.evaluate('()=>draft().options')
            check('Array and nullable watermark controls preserve their types',
                  captured['movement_bounds'] == [.1, .1, .9, .9] and captured['end_seconds'] is None)
            page.locator('#save-preset').click()
            page.wait_for_function('()=>state.presets.some(p=>p.name==="V05 browser watermark")')
            check('New tool preset saves validated complete options',
                  next(p for p in api('/state')['presets'] if p['name'] == 'V05 browser watermark')['options']['font_size'] == 24)
            sources = register([video], 'watermark')
            settings_tab('Encoding')
            page.select_option('[name="encoder"]', 'cpu')
            page.locator('#v5-watermark-preview').click()
            page.wait_for_selector('.v5-encoded-preview', timeout=90000)
            page.wait_for_function('()=>document.querySelector(".v5-encoded-preview").readyState>=2', timeout=30000)
            check('Watermark preview is a locally encoded playable blob',
                  page.locator('.v5-encoded-preview').get_attribute('src').startswith('blob:'))
            page.locator('.v5-encoded-preview').evaluate('media=>media.currentTime=0.8')
            page.wait_for_function('()=>{const media=document.querySelector(".v5-encoded-preview");return !media.seeking&&media.readyState>=3}')
            screenshot('FORGE-v0.5-watermark-studio.png')
            page.locator('#start-tool').click()
            page.wait_for_selector('#v5-confirm-submit', timeout=30000)
            check('Export submission shows actual encoding disclosures',
                  'encod' in page.locator('.v5-plan-list').inner_text().lower())
            page.locator('#v5-confirm-submit').click()
            page.wait_for_function('()=>view==="queue"')
            marked = wait_job(newest('watermark')['id'])
            check('Watermark export exists independently of preview',
                  any(Path(item['path']).is_file() and item['name'].endswith('.mp4') for item in marked['outputs']))
            marked_path = next(Path(item['path']) for item in marked['outputs'] if item['name'].endswith('.mp4'))

            nav('watermark')
            register([audio], 'watermark')
            settings_tab('Watermark content')
            page.select_option('[name="mode"]', 'audio')
            settings_tab('Audio identifier')
            page.fill('[name="audio_clip_path"]', str(clip))
            page.fill('[name="audio_timestamps"]', '0, 1, 2')
            page.fill('[name="audio_volume"]', '0.5')
            page.locator('#v5-watermark-preview').click()
            page.wait_for_selector('audio.v5-encoded-preview', timeout=90000)
            page.wait_for_function('()=>document.querySelector("audio.v5-encoded-preview").readyState>=2', timeout=30000)
            check('Audio identifier preview is an actual playable encoded audio sample',
                  page.locator('audio.v5-encoded-preview').get_attribute('src').startswith('blob:'))
            page.locator('#start-tool').click()
            page.wait_for_selector('#v5-confirm-submit')
            page.locator('#v5-confirm-submit').click()
            page.wait_for_function('()=>view==="queue"')
            audio_marked = wait_job(newest('watermark')['id'])
            check('Audio watermark controls produce a real separate WAV export',
                  any(item['name'].endswith('.wav') and Path(item['path']).stat().st_size > 1000 for item in audio_marked['outputs']))

            nav('audio_fingerprint')
            register([audio, copy_audio], 'audio_fingerprint')
            check('Audio lab displays original and comparison players', page.locator('#v5-source-players audio').count() == 2)
            settings_tab('Private reports and references')
            page.check('[name="retain_reference"]')
            page.locator('#start-tool').click()
            page.wait_for_function('()=>view==="queue"')
            audio_job = wait_job(newest('audio_fingerprint')['id'])
            api_report = api('/media/privacy/jobs/' + audio_job['id'] + '/report')
            check('Audio comparison reports measurable descriptor agreement',
                  any(item['report'].get('comparisons') for item in api_report['reports']))
            nav('audio_fingerprint')
            page.locator('[data-privacy-report]').first.click()
            page.wait_for_selector('.v5-analysis-images img')
            check('Audio report shows actual spectrogram and frequency visualizations',
                  page.locator('.v5-analysis-images img').count() >= 2)
            check('Comparison explains measurement limitations', 'recognition probability' in page.locator('.v5-report').inner_text())
            screenshot('FORGE-v0.5-audio-analysis.png')
            page.locator('#dialog form button').click()
            references = [Path(item['path']) for item in audio_job['outputs'] if item['name'].endswith('.forge-fingerprint.json')]
            check('Reference retention is explicit and produces a local portable file', len(references) == 1 and len(json.loads(references[0].read_text())['records']) == 2)
            register([audio], 'audio_fingerprint')
            page.locator('.v5-references summary').click()
            page.locator('#v5-add-reference').click()
            page.fill('#v5-reference-paths', str(references[0]))
            page.locator('#v5-register-reference').click()
            page.wait_for_function('()=>draft().reference_sources?.length===1')
            check('Explicit reference import remains separate from source media',
                  page.evaluate('()=>draft().sources.length===1&&draft().reference_sources.length===1'))
            settings_tab('Private reports and references')
            page.uncheck('[name="retain_reference"]')
            page.locator('#start-tool').click()
            page.wait_for_function('()=>view==="queue"')
            reference_job = wait_job(newest('audio_fingerprint')['id'])
            reference_reports = api('/media/privacy/jobs/' + reference_job['id'] + '/report')['reports']
            check('Imported retained fingerprints are compared by the real queued worker',
                  any(len(item['report'].get('comparisons', [])) == 2 and item['report'].get('reference_names') for item in reference_reports))

            nav('video_fingerprint')
            register([video, copy_video], 'video_fingerprint')
            settings_tab('Frame sampling')
            page.fill('[name="selected_times"]', '0.1, 1, 2')
            page.locator('#start-tool').click()
            page.wait_for_function('()=>view==="queue"')
            video_job = wait_job(newest('video_fingerprint')['id'])
            nav('video_fingerprint')
            page.locator('[data-privacy-report]').first.click()
            page.wait_for_selector('.v5-frame-strip img')
            check('Video comparison shows selected frames with timestamps', page.locator('.v5-frame-strip img').count() == 6)
            check('Video report shows sampled agreement timeline', page.locator('.v5-timeline meter').count() > 0)
            screenshot('FORGE-v0.5-video-comparison.png')
            page.locator('#dialog form button').click()

            for tool in ['tracking_audit', 'provenance']:
                nav(tool)
                register([video], tool)
                if tool == 'tracking_audit':
                    settings_tab('Inspection methods')
                    page.check('[name="include_fingerprints"]')
                page.locator('#start-tool').click()
                page.wait_for_function('()=>view==="queue"')
                job = wait_job(newest(tool)['id'])
                nav(tool)
                page.locator('[data-privacy-report]').first.click()
                page.wait_for_selector('.v5-report')
                if tool == 'tracking_audit':
                    check('Unified audit renders requested signal and frame evidence',
                          page.locator('.v5-report .v5-analysis-images img').count() >= 2 and
                          page.locator('.v5-report .v5-frame-strip img').count() > 0)
                check('Inspector presents local evidence and complete details: ' + tool,
                      page.locator('.v5-report details').count() > 0)
                check('Provenance never labels missing credentials as anonymity: ' + tool,
                      'Missing credentials do not establish anonymity' in page.locator('.v5-report').inner_text())
                page.locator('#dialog form button').click()

            nav('privacy_export')
            register([video], 'privacy_export')
            settings_tab('Privacy policy')
            page.select_option('[name="preset"]', 'custom')
            page.select_option('[name="metadata"]', 'selective')
            page.fill('[name="remove_tags"]', 'title, comment')
            settings_tab('Verification and reports')
            page.check('[name="reports"]')
            page.check('[name="compare_audio"]')
            page.locator('#start-tool').click()
            page.wait_for_selector('#v5-confirm-submit')
            check('Privacy export presents stream and provenance disclosure',
                  'provenance' in page.locator('.v5-plan-list').inner_text().lower())
            page.locator('#v5-confirm-submit').click()
            page.wait_for_function('()=>view==="queue"')
            export_job = wait_job(newest('privacy_export')['id'])
            nav('privacy_export')
            page.locator('[data-privacy-report]').first.click()
            check('Verification displays actual before and after metadata',
                  'Metadata verification' in page.locator('.v5-report').inner_text() and
                  'Stream / codec changes' in page.locator('.v5-report').inner_text())
            screenshot('FORGE-v0.5-private-verification.png')
            page.locator('#dialog form button').click()

            nav('privacy_verify')
            register([video, marked_path], 'privacy_verify')
            check('Standalone verification defaults to no saved private reports',
                  not page.locator('[name="reports"]').is_checked())
            page.locator('#start-tool').click()
            page.wait_for_function('()=>view==="queue"')
            verification = wait_job(newest('privacy_verify')['id'])
            check('Disabled reports produce no saved analysis output', verification['outputs'] == [])
            nav('privacy_verify')
            check('UI explains that no JSON report was retained',
                  'no JSON report retained' in page.locator('#v5-recent-results').inner_text())

            nav('workflows')
            page.locator('#black-add-node').click()
            check('Black Scripting offers all seven Media Privacy operations',
                  page.locator('[data-black-tool]').count() >= 10)
            page.locator('[data-black-tool="watermark"]').click()
            settings_tab('Timing and movement')
            page.fill('[name="movement_bounds"]', '0, 0.2, 1, 0.8')
            page.locator('#black-confirm-node').click()
            page.wait_for_selector('[data-black-node]')
            check('Black Scripting snapshots typed watermark arrays',
                  page.evaluate('()=>blackScript.nodes[0].options.movement_bounds') == [0, .2, 1, .8])
            screenshot('FORGE-v0.5-black-scripting.png')

            nav('watermark')
            escaped = page.evaluate('()=>ForgePrivacy.reportHTML({findings:[{category:"<img src=x onerror=alert(1)>",status:"detected",method:"fixture",evidence:"<script>"}]})')
            check('Private report evidence is escaped before insertion', '<img src=x' not in escaped and '&lt;img' in escaped)
            for tool in catalog['tools']:
                page.set_viewport_size({'width': 390, 'height': 844})
                nav(tool)
                check('Mobile layout fits: ' + tool, not page.evaluate('document.documentElement.scrollWidth>innerWidth'))
            screenshot('FORGE-v0.5-mobile.png')
            page.set_viewport_size({'width': 1440, 'height': 1000})
            outsider = browser.new_context()
            denied = outsider.request.get(origin + '/api/media/privacy/sources/' + sources[0]['id'] + '/content')
            check('Registered media preview requires an authorized browser session', denied.status in {401, 403})
            outsider.close()
            check('Browser made no requests to external services',
                  all(url.startswith(origin + '/') for url in requests))
            check('Browser has no uncaught JavaScript errors', not errors)
            check('Every fixture original remains byte-for-byte unchanged',
                  all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in original_hashes.items()))
            report['status'] = 'passed'
        except BaseException as error:
            report['status'] = 'failed'
            report['failure'] = type(error).__name__ + ': ' + str(error).replace(ticket, '[redacted launch ticket]')
            screenshot('FORGE-v0.5-failure.png')
            raise
        finally:
            (output / 'BROWSER_V05_REPORT.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            browser.close()
finally:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    log.close()
print(f'Completed {len(checks)} browser checks.', flush=True)
