"""Real Chromium/editor/API acceptance with owned generated local video.

The English/Russian text is an explicit editor fixture. No model inference,
Google authorization, upload, CUDA or lip-sync inference is claimed by this suite.
The real Dubbing API must be mounted; no replacement backend is installed here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

import uvicorn
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from forge import __version__
from forge.app import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chromium', default=shutil.which('chromium') or '')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    checks, errors, screenshots = [], [], []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    with tempfile.TemporaryDirectory(prefix='forge-v10-browser-') as temporary:
        home = Path(temporary)
        original = home / 'Original English fixture.mp4'
        artwork = home / 'Owned geometric source.png'
        image = Image.new('RGB', (640, 360), (28, 38, 40)); drawing = ImageDraw.Draw(image)
        drawing.ellipse((466, 35, 532, 101), fill=(186, 164, 111))
        drawing.polygon([(0, 320), (180, 100), (330, 280), (467, 148), (640, 360)], fill=(71, 92, 87))
        drawing.polygon([(0, 360), (220, 210), (390, 325), (520, 240), (640, 360)], fill=(39, 58, 57))
        image.save(artwork)
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-loop', '1', '-framerate', '10',
                        '-i', str(artwork), '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000:duration=20',
                        '-vf', 'scale=320:180,format=yuv420p', '-t', '20', '-c:v', 'libx264',
                        '-preset', 'ultrafast', '-c:a', 'aac', '-shortest', str(original)], check=True, timeout=60)
        original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        token = secrets.token_urlsafe(30)
        app = create_app(home / 'data', token, port, workers=False)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(port)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def api(route, body=None):
            request = urllib.request.Request(base + '/api/' + route,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'X-Forge-Token': token, 'Content-Type': 'application/json'})
            with opener.open(request, timeout=20) as response:
                return json.load(response)

        def wait(predicate, timeout=12):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                value = predicate()
                if value:
                    return value
                time.sleep(.1)

            try:
                print(json.dumps({'ui_status':page.locator('#dv-status').inner_text(),'browser_errors':errors},ensure_ascii=False))
                page.screenshot(path=str(args.output/'FORGE-V010-Failure.png'),full_page=True)
            except Exception:
                pass
            raise AssertionError('Timed out waiting for verified browser/API state.')

        try:
            wait(lambda: server.started)
            try:
                api('dubbing/state')
            except urllib.error.HTTPError as exc:
                raise RuntimeError('Dubbing API acceptance cannot run: /api/dubbing/state returned HTTP ' + str(exc.code)) from None
            project = api('dubbing/import', {'path': str(original), 'name': 'Synthetic English → Русский'})['project']
            identity = project['id']
            project['segments'] = [{'id': f'fixture-{index:03d}', 'start': index * .5, 'end': index * .5 + .45,
                'speaker': 'speaker1', 'source_text': f'English editor fixture {index + 1}.',
                'text': f'Русский текст для проверки редактора {index + 1}.',
                'reviewed': False, 'manual_edit': False} for index in range(35)]
            project = api('dubbing/projects', {'project': project, 'expected_revision': project['revision']})['project']
            old_video=home/'Earlier synthetic export.mp4';new_video=home/'Current synthetic export.mp4'
            shutil.copyfile(original,old_video);shutil.copyfile(original,new_video)
            library=app.state.dubbing.library
            old_artifact=library.add_artifact(identity,{'path':str(old_video),'sha256':original_hash,'kind':'export_video','stale':True,'preview':False})
            current_artifact=library.add_artifact(identity,{'path':str(new_video),'sha256':original_hash,'kind':'export_video','preview':False})

            def saved():
                return api('dubbing/projects/' + identity).get('project') or api('dubbing/projects/' + identity)

            with sync_playwright() as playwright:
                launch = {'headless': True}
                if args.chromium:
                    launch['executable_path'] = args.chromium
                browser = playwright.chromium.launch(**launch)
                page = browser.new_page(viewport={'width': 1600, 'height': 1100}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base + '/#token=' + token, wait_until='networkidle')
                page.locator('#sidebar [data-view="dubbing"]').click()
                page.locator('#dv-project').select_option(identity)
                page.locator('[data-dv-text="fixture-000"]').wait_for()
                check('Dubbing Studio production navigation', page.locator('#sidebar [data-view="dubbing"]').count() == 1)
                check('Dubbing navigation current state', page.locator('#sidebar [data-view="dubbing"]').get_attribute('aria-current') == 'page')
                check('Side-by-side source and Russian script', page.locator('[data-dv-source="fixture-000"]').count() == page.locator('[data-dv-text="fixture-000"]').count() == 1)
                check('Source transcript read-only', page.locator('[data-dv-source="fixture-000"]').get_attribute('readonly') is not None)
                check('Rational frame rate displayed without NaN', '10.00 fps' in page.locator('.dv-source-facts').inner_text() and 'NaN' not in page.locator('.dv-source-facts').inner_text())
                page.locator('#dv-wave-load').click()
                page.wait_for_function('() => window.ForgeDubbing.snapshot().waveform_points > 0')
                check('Actual bounded waveform loaded', page.evaluate('window.ForgeDubbing.snapshot().waveform_points') == 800)
                check('Long-script DOM pagination bounded', page.locator('.dv-segment').count() == 30)
                page.locator('[data-dv-preview="russian"]').click()
                check('Preview selects latest verified nonstale artifact', current_artifact['id'] in page.locator('#dv-video').get_attribute('src') and old_artifact['id'] not in page.locator('#dv-video').get_attribute('src'))
                page.locator('[data-dv-preview="original"]').click()
                page.locator('[data-dv-select="fixture-000"]').check()
                page.locator('#dv-next').click()
                check('Last page has remaining segments', page.locator('.dv-segment').count() == 5)
                page.locator('[data-dv-select="fixture-034"]').check()
                check('Selections persist across pages', page.locator('#dv-selected-count').inner_text() == '2 selected')
                page.locator('#dv-prev').click()
                check('Prior selected checkbox restored', page.locator('[data-dv-select="fixture-000"]').is_checked())
                text = 'Ручная правка: <img src=x onerror="window.dubbingXSS=true">'
                page.locator('[data-dv-text="fixture-000"]').fill(text)
                page.locator('#dv-save').click()
                wait(lambda: saved()['segments'][0]['text'] == text)
                check('Manual Russian edit persisted through real API', saved()['segments'][0]['text'] == text)
                check('Text edits invalidate review', saved()['segments'][0]['reviewed'] is False)
                check('Untrusted script text not executed', not page.evaluate('window.dubbingXSS === true'))
                rejected_translation = []
                stage_requests = []
                page.on('request', lambda request: stage_requests.append(request.url) if request.url.endswith('/api/dubbing/stage') else None)
                def dismiss_regeneration(dialog):
                    rejected_translation.append(dialog.message)
                    dialog.dismiss()
                page.once('dialog', dismiss_regeneration)
                page.locator('[data-dv-translate="fixture-000"]').click()
                page.wait_for_timeout(150)
                check('Manual translation replacement requires confirmation', bool(rejected_translation) and 'replaced' in rejected_translation[0])
                check('Declined translation leaves text and production unchanged', saved()['segments'][0]['text'] == text and not stage_requests)
                check('Updater flush contract resolves true after saving', page.evaluate('window.ForgeDubbing.flush()') is True)

                delayed = {'used': False}

                def delay_first_save(route):
                    if route.request.method == 'POST' and not delayed['used']:
                        delayed['used'] = True
                        response = route.fetch()
                        time.sleep(1)
                        route.fulfill(response=response)
                    else:
                        route.continue_()

                page.route('**/api/dubbing/projects', delay_first_save)
                page.evaluate("""() => {
                    const editor = document.querySelector('[data-dv-text="fixture-000"]');
                    editor.value = 'First network-delayed edit'; editor.dispatchEvent(new Event('input', {bubbles:true}));
                    setTimeout(() => {editor.value = 'Последняя правка сохранена при медленном ответе'; editor.dispatchEvent(new Event('input', {bubbles:true}));}, 1300);
                }""")
                page.wait_for_timeout(3400)
                wait(lambda: saved()['segments'][0]['text'] == 'Последняя правка сохранена при медленном ответе')
                check('Newer text survives delayed save response', saved()['segments'][0]['text'] == 'Последняя правка сохранена при медленном ответе')
                check('Delayed response did not replace live draft', page.locator('[data-dv-text="fixture-000"]').input_value() == 'Последняя правка сохранена при медленном ответе')
                page.unroute('**/api/dubbing/projects', delay_first_save)

                page.locator('[data-dv-text="fixture-000"]').fill('Локальная правка объединена с независимым изменением')
                remote = saved(); remote['name'] = 'Независимое название проекта'
                api('dubbing/projects', {'project': remote, 'expected_revision': remote['revision']})
                page.locator('#dv-save').click()
                wait(lambda: saved()['segments'][0]['text'] == 'Локальная правка объединена с независимым изменением')
                check('Independent backend change retained during revision merge', saved()['name'] == 'Независимое название проекта')
                check('No unsafe conflict overwrite required', not page.locator('#dialog').is_visible())

                page.get_by_role('tab', name='Timing', exact=True).click()
                page.locator('#dv-max-tempo').fill('1.08'); page.locator('#dv-max-tempo').press('Tab')
                page.locator('#dv-save').click()
                wait(lambda: saved()['options']['timing']['max_tempo'] == 1.08)
                check('Timing options saved with canonical schema', saved()['options']['timing']['max_tempo'] == 1.08)
                page.get_by_role('tab', name='Publish', exact=True).click()
                check('New upload visibility defaults Private', page.locator('#dv-visibility').input_value() == 'private')
                check('Explicit audience decision required', page.locator('#dv-audience').input_value() == '')
                check('Russian publishing timezone offered', page.locator('#dv-publishing-timezone').input_value() == 'Europe/Moscow')
                page.locator('#dv-visibility').select_option('unlisted')
                page.locator('#dv-audience').select_option('false')
                page.locator('#dv-upload-title').fill('Русское название'); page.locator('#dv-upload-title').press('Tab')
                page.locator('#dv-save').click()
                wait(lambda: saved()['options']['publishing']['title'] == 'Русское название')
                check('Audience, title and visibility persisted', saved()['options']['publishing']['made_for_kids'] is False and saved()['options']['publishing']['visibility'] == 'unlisted')
                page.locator('#dv-publish-at').fill('2030-01-02T12:00'); page.locator('#dv-publish-at').press('Tab')
                page.locator('#dv-save').click()
                wait(lambda: saved()['options']['publishing']['schedule'] == '2030-01-02T09:00:00.000Z')
                check('Moscow schedule converted to an aware UTC instant', saved()['options']['publishing']['schedule'] == '2030-01-02T09:00:00.000Z')
                check('Scheduling selects required Private visibility', saved()['options']['publishing']['visibility'] == 'private')

                page.locator('#dv-setup').click()
                page.get_by_role('heading', name='Set Up Dubbing Studio', exact=True).wait_for()
                check('Guided setup opens', page.get_by_role('heading', name='Set Up Dubbing Studio', exact=True).is_visible())
                check('Setup exposes model components before approval', page.locator('[data-dv-component]').count() == len(api('dubbing/state')['models']))
                page.locator('#dialog form button').click()
                page.get_by_role('tab', name='Lip sync', exact=True).click()
                check('Lip sync is opt-in', page.locator('#dv-lip-mode').input_value() == 'off')
                check('Lip preview disabled without installed engine and approval', page.locator('#dv-lip-preview').is_disabled())
                check('Face consent not preapproved', not page.locator('#dv-face-consent').is_checked())
                page.locator('#dv-lip-mode').select_option('selected')
                page.locator('#dv-face-choose').click()
                overlay = page.locator('#dv-face-overlay'); overlay.wait_for()
                box = overlay.bounding_box(); assert box
                page.mouse.move(box['x'] + box['width'] * .3, box['y'] + box['height'] * .15)
                page.mouse.down(); page.mouse.move(box['x'] + box['width'] * .6, box['y'] + box['height'] * .65, steps=6); page.mouse.up()
                page.locator('#dv-save').click()
                wait(lambda: len(saved()['options']['lip_sync'].get('faces', [])) == 1)
                face = saved()['options']['lip_sync']['faces'][0]
                check('Manual face ROI stored as normalized XYWH', face['normalized'] is True and len(face['box']) == 4 and all(0 <= number <= 1 for number in face['box']))
                check('Selecting a face does not infer consent', face['consent'] is False and saved()['options']['lip_sync']['consent'] is False)
                page.get_by_role('tab', name='Voice', exact=True).click()
                page.locator('#dv-my-voice').click()
                check('Voice permission not preapproved', not page.locator('#dv-voice-permission').is_checked())
                check('Voice preparation requires permission', page.locator('#dv-voice-prepare').is_disabled())
                page.locator('#dialog form button').click()

                page.locator('#dv-settings-collapse').click()
                check('Production panel can collapse', not page.locator('.dv-settings').is_visible())
                page.locator('#dv-show-settings').click()
                check('Production panel restored', page.locator('.dv-settings').is_visible())
                page.locator('#dv-panel-resizer').focus(); page.keyboard.press('ArrowLeft')
                page.locator('#dv-save').click()
                wait(lambda: saved()['options']['panels'].get('right_width') is not None)
                check('Panel size persists', saved()['options']['panels']['right_width'] >= 250)

                for width, name in [(1600, 'Desktop'), (1120, 'Compact'), (760, 'Narrow')]:
                    page.set_viewport_size({'width': width, 'height': 1100})
                    page.wait_for_timeout(180)
                    overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth + 2')
                    check('No horizontal overflow at ' + str(width), not overflow)
                    filename = 'FORGE-V010-Dubbing-' + name + '.png'
                    page.screenshot(path=str(args.output / filename), full_page=True)
                    screenshots.append(filename)
                page.set_viewport_size({'width': 1600, 'height': 1100})
                page.get_by_role('tab', name='Lip sync', exact=True).click()
                page.locator('#dv-lip-mode').select_option('off'); page.locator('#dv-save').click()
                page.locator('#dv-create').click()
                page.locator('#dv-plan-black').wait_for()
                page.locator('#dv-plan-black').click()
                page.locator('#black-add-node').wait_for()
                check('Production plan opens an editable linear workflow', page.evaluate('() => blackScript.nodes.length >= 8 && blackScript.nodes.every(node => node.options.project_id)'))
                page.locator('#black-add-node').click()
                check('Black Scripting exposes linear dubbing stage', page.locator('[data-black-tool="dubbing_translate"]').is_visible())
                check('Publishing is preparation-only in linear selector', page.locator('[data-black-tool="youtube_upload"]').count() == 0)
                page.locator('#black-back').click()
                page.locator('#dv-project').wait_for()
                check('Dubbing route restored after Black mode', page.locator('#dv-project').input_value() == identity)
                page.reload(wait_until='networkidle')
                page.locator('[data-dv-text="fixture-000"]').wait_for()
                check('Saved workspace restored after reload', page.locator('[data-dv-text="fixture-000"]').input_value() == 'Локальная правка объединена с независимым изменением')
                page.get_by_role('tab', name='Export', exact=True).click()
                check('Unreviewed script remains available for private backup', not any(segment['reviewed'] for segment in saved()['segments']))
                with page.expect_response(lambda response: response.url.endswith('/api/dubbing/stage') and response.request.method == 'POST') as backup_response:
                    page.locator('#dv-backup').click()
                check('Project backup request accepted before translation review', backup_response.value.ok)
                backup_job = backup_response.value.json()['jobs'][0]
                backup_payload = app.state.store.job(backup_job['id'])['payload']
                check('Project backup queues only its source-inclusive project archive', backup_payload['options']['formats'] == ['project'] and backup_payload['options']['include_source'] is True and backup_job['options_summary']['formats'] == ['project'])
                from forge.dubbing_pipeline import process
                backup_files, _, backup_result = process(backup_payload, home / 'actual-private-backup', home / 'data')
                with zipfile.ZipFile(backup_files[0]) as archive:
                    backup_source = archive.read('media/original.mp4')
                    backup_project = json.loads(archive.read('project.json'))
                check('Actual private backup embeds byte-identical original with unreviewed draft', {Path(path).name for path in backup_files} == {'dubbing-project.zip', 'dubbing-stage.json'} and hashlib.sha256(backup_source).hexdigest() == original_hash and backup_project['backup']['source_embedded'] is True and not any(segment['reviewed'] for segment in backup_project['segments']) and backup_result['project_updates']['exports']['formats'] == ['project'])
                page.locator('#dv-export-wav').uncheck(); page.locator('#dv-export-flac').check()
                page.locator('#dv-export-bilingual').check(); page.locator('#dv-save').click()
                wait(lambda: saved()['options']['export']['separate_audio'] == ['flac'])
                page.locator('#dv-approve').click(); page.locator('#dv-review-confirm').click()
                wait(lambda: all(segment['reviewed'] for segment in saved()['segments']))
                # A known generated tone is an explicit mix fixture, not neural narration.
                mixed_audio = home / 'Owned synthetic narration.wav'
                subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(original), '-vn', '-c:a', 'pcm_s16le', str(mixed_audio)], check=True, timeout=30)
                library.add_artifact(identity, {'path':str(mixed_audio),'sha256':hashlib.sha256(mixed_audio.read_bytes()).hexdigest(),'kind':'mix','preview':False})
                with page.expect_response(lambda response: response.url.endswith('/api/dubbing/stage') and response.request.method == 'POST') as audio_response:
                    page.locator('#dv-export-audio').click()
                check('Explicit audio and text export request accepted', audio_response.value.ok)
                audio_job = audio_response.value.json()['jobs'][0]
                audio_payload = app.state.store.job(audio_job['id'])['payload']
                formats = ['flac', 'srt', 'vtt', 'txt', 'metadata', 'bilingual']
                check('Audio export freezes selected FLAC and bilingual documents without video', audio_payload['options']['formats'] == formats and audio_job['options_summary']['formats'] == formats and 'video' not in audio_payload['options']['formats'])
                audio_files, _, audio_result = process(audio_payload, home / 'actual-audio-document-export', home / 'data')
                check('Actual audio and document export creates FLAC, captions and bilingual files only', {Path(path).name for path in audio_files} == {'russian.flac', 'russian.srt', 'russian.vtt', 'russian-transcript.txt', 'russian-metadata.json', 'english-russian.json', 'dubbing-stage.json'} and audio_result['project_updates']['exports']['formats'] == formats)
                page.locator('#dv-export-flac').uncheck(); page.locator('#dv-export-bilingual').uncheck(); page.locator('#dv-save').click()
                wait(lambda: saved()['options']['export']['separate_audio'] == [])
                with page.expect_response(lambda response: response.url.endswith('/api/dubbing/stage') and response.request.method == 'POST') as fallback_response:
                    page.locator('#dv-export-audio').click()
                check('No selected audio format falls back to WAV and text without video', fallback_response.value.ok and fallback_response.value.json()['jobs'][0]['options_summary']['formats'] == ['wav', 'srt', 'vtt', 'txt', 'metadata'])
                check('Original video byte-identical', hashlib.sha256(original.read_bytes()).hexdigest() == original_hash)
                check('No uncaught browser errors', not errors)
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            app.state.store.close()
    report = {'version': __version__, 'result': 'passed', 'checks': checks, 'passed': len(checks),
              'errors': errors, 'screenshots': screenshots, 'native_windows_execution': False,
              'model_inference_tested': False, 'physical_gpu_tested': False,
              'google_oauth_or_upload_tested': False,
              'scope': 'Actual browser/editor, authenticated Dubbing APIs and persistence; synthetic editor text and generated video.'}
    (args.output / 'BROWSER_V010_RESULTS.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
