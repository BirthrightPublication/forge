"""Real Chromium regressions for maintenance reloads and speech scope requests.

Backend restart identity/model availability are explicit transport fixtures. No
application is replaced and no neural model is installed or impersonated.
"""
import argparse
import copy
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
from forge import __version__
from forge.app import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chromium', default=shutil.which('chromium'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    checks, errors = [], []
    def check(label, condition=True):
        assert condition, label
        checks.append(label)
        print('PASS', label, flush=True)
    with tempfile.TemporaryDirectory(prefix='forge-v10-hotfix-browser-') as temporary:
        listener = socket.socket(); listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        token = secrets.token_urlsafe(32)
        app = create_app(Path(temporary) / 'data', token, port, workers=False)
        settings = app.state.store.settings()
        settings.update(automatic_update_checks=False, daily_update_check=False, automatic_dependency_repairs=False)
        app.state.store.set('settings', settings)
        app.state.store.set('update_catalog', {'entries': [], 'checked': time.time(),
                                             'next_check': time.time() + 86400, 'errors': []})
        def maintenance(version, identity='a' * 32):
            app.state.store.set('maintenance', {'id': identity, 'status': 'completed',
                'version': version, 'requested': time.time() - 100, 'completed': time.time() - 50,
                'message': 'Explicit synthetic completed maintenance fixture.'})
        maintenance('0.1.0')
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                               log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.1)
            assert server.started
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.chromium, headless=True,
                                                       args=['--no-sandbox'])
                context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                page = context.new_page()
                page.set_default_timeout(10000)
                page.on('pageerror', lambda error: errors.append(str(error)))
                navigations = []
                # Count document loads, not same-document history/hash updates.
                page.on('request', lambda request: navigations.append('document')
                        if request.resource_type == 'document' else None)
                base = f'http://127.0.0.1:{port}'
                page.goto(base + '/#token=' + token)
                page.wait_for_selector('#dl-urls')
                page.wait_for_function('()=>state!==null')
                baseline = context.request.get(base + '/api/state').json()
                health = context.request.get(base + '/health').json()
                parts = list(map(int, __version__.split('.')))
                target = '.'.join(map(str, [parts[0], parts[1], parts[2] + 1]))
                mode = {'value': 'actual', 'health_calls': 0}
                def health_route(route):
                    if mode['value'] == 'actual':
                        route.continue_(); return
                    mode['health_calls'] += 1
                    pid = health['pid'] + 1000
                    if mode['value'] == 'unstable-pid':
                        pid += mode['health_calls']
                    route.fulfill(json={'app': 'FORGE', 'version': target, 'pid': pid})
                def state_route(route):
                    if mode['value'] == 'actual':
                        route.continue_(); return
                    value = copy.deepcopy(baseline)
                    value['version'] = target
                    if mode['value'] == 'wrong-root':
                        value['application_directory'] += '/unrelated-installation'
                    route.fulfill(json=value)
                page.route('**/health', health_route)
                page.route('**/api/state', state_route)
                def inspect_updates():
                    if page.locator('.birthright-updates[open]').count():
                        page.locator('#birthright-updates').click()
                    page.locator('#birthright-updates').click()
                    page.wait_for_selector('.birthright-updates[open]')
                draft = 'https://example.invalid/synthetic-unsaved-draft'
                page.locator('#dl-urls').fill(draft)
                before = len(navigations)
                inspect_updates()
                page.wait_for_timeout(11000)
                check('Historical completed maintenance cannot reload the page', len(navigations) == before)
                check('Unsaved work survives multiple former five-second reload periods', page.locator('#dl-urls').input_value() == draft)
                maintenance(target, 'b' * 32)
                inspect_updates(); page.wait_for_timeout(11000)
                check('A future completion record is insufficient without a newer running backend', len(navigations) == before)
                for case in ('wrong-root', 'unstable-pid'):
                    mode.update(value=case, health_calls=0)
                    inspect_updates(); page.wait_for_timeout(600)
                    check('Reload rejects ' + case, len(navigations) == before)
                mode.update(value='verified', health_calls=0)
                inspect_updates()
                for _ in range(100):
                    if len(navigations) > before:
                        break
                    page.wait_for_timeout(100)
                check('A genuinely newer backend with stable PID and matching installation reloads once', len(navigations) == before + 1)
                page.wait_for_selector('#dl-urls')
                for _ in range(3):
                    inspect_updates(); page.wait_for_timeout(150)
                page.wait_for_timeout(11000)
                check('The verified completion cannot cause a reload loop', len(navigations) == before + 1)
                check('Only a nonsecret consumed-update identity is stored', 'forge-token' not in page.evaluate('Object.keys(sessionStorage)'))

                mode['value']='actual'
                app.state.store.set('maintenance',{'id':'c'*32,'status':'recovered','version':__version__,
                    'backend_verified':True,'pid':health['pid'],'message':'Explicit recovered-backend fixture.'})
                inspect_updates();page.wait_for_selector('#open-recovered-workspace')
                check('An older verified recovery offers an explicit Open action without automatic reload',len(navigations)==before+1)
                page.locator('#open-recovered-workspace').click()
                page.wait_for_selector('#dl-urls')
                page.wait_for_function('version=>state!==null&&state.version===version',arg=__version__)
                check('Open restored workspace is a user action and reloads the current backend',len(navigations)==before+2)

                app.state.store.set('update_catalog',{'entries':[
                    {'id':'application','category':'application','label':'FORGE','installed':__version__,
                     'available':target,'action':'update','bytes':1024,'legacy_bytes':2048,
                     'notes':'Synthetic release metadata; no package is downloaded.'},
                    {'id':'dubbing:translation-en-ru','category':'models','label':'Russian translation',
                     'installed':'','available':'Set Up Dubbing Studio','action':'','setup_action':'dubbing'},
                    {'id':'dubbing-runtime','category':'dependencies','label':'Dubbing runtimes',
                     'installed':'','available':'Managed Python','action':'','setup_action':'dubbing'}],
                     'checked':time.time(),'next_check':time.time()+86400,'errors':[]})
                inspect_updates();page.wait_for_selector('[data-update-id="application"]')
                check('Dubbing components link to controlled setup rather than legacy model installation',page.locator('[data-dubbing-setup]').count()==2)
                check('Optional dubbing components do not inflate the update badge',page.locator('#update-count').inner_text()=='1')
                dialogs=[]
                page.once('dialog',lambda dialog:(dialogs.append(dialog.message),dialog.dismiss()))
                page.locator('[data-update-id="application"]').click()
                for _ in range(100):
                    if dialogs:break
                    page.wait_for_timeout(50)
                check('Approval shows exact version, download size, storage, destination and work summary',bool(dialogs) and all(value in dialogs[0] for value in (target,'Download:','Required free storage:','Destination:','Active jobs:')))
                check('Declining the preflight performs no installation or queue mutation',app.state.store.jobs()==[])

                # Explicit save callbacks exercise the real Update Center event
                # handler. A draft conflict must stop before any maintenance API.
                update_requests=[]
                page.on('request',lambda request:update_requests.append(request.url)
                        if '/api/updates/preflight' in request.url or '/api/updates/install' in request.url else None)
                page.evaluate("""()=>{window.hotfixOriginalFlush=ForgeDubbing.flush;
                    window.hotfixDraft='Unsaved Russian text';ForgeDubbing.flush=async()=>false;}""")
                page.locator('[data-update-id="application"]').click()
                page.wait_for_function("()=>document.querySelector('.birthright-updates').innerText.includes('could not be saved')")
                check('A conflicted Dubbing draft blocks application preflight and installation',not update_requests)
                page.evaluate("()=>{ForgeDubbing.flush=async()=>{throw Error('Explicit save conflict fixture');};}")
                page.locator('[data-update-id="application"]').click();page.wait_for_timeout(150)
                check('A rejected Dubbing save leaves the draft and maintenance untouched',not update_requests and page.evaluate('hotfixDraft')=='Unsaved Russian text')
                page.evaluate("""()=>{window.hotfixFlushCount=0;ForgeDubbing.flush=async()=>{
                    hotfixFlushCount++;if(hotfixFlushCount===1){window.hotfixSavedDraft=hotfixDraft;return true;}return false;};}""")
                page.once('dialog',lambda dialog:dialog.accept())
                page.locator('[data-update-id="application"]').click()
                page.wait_for_function('()=>hotfixFlushCount===2')
                check('Update approval flushes again before maintenance and retains saved workspace',
                      len(update_requests)==1 and update_requests[0].endswith('/api/updates/preflight')
                      and page.evaluate('hotfixSavedDraft===hotfixDraft'))
                page.evaluate('()=>{ForgeDubbing.flush=hotfixOriginalFlush;}')

                # Separate fresh page: model availability is a metadata fixture,
                # while project persistence and queue requests use the real API.
                mode['value'] = 'actual'
                maintenance('0.1.0')
                speech = context.request.get(base + '/api/speech/state').json()
                for entry in speech['engines']:
                    if entry['id'] == 'kokoro':
                        entry.update(installed=True, unavailable_reason='')
                for entry in speech['voices']:
                    if entry.get('engine') == 'kokoro':
                        entry.update(installed=True, synthesis_ready=True)
                editor = context.new_page(); editor.set_default_timeout(10000)
                editor.on('pageerror', lambda error: errors.append(str(error)))
                editor.route('**/api/speech/state', lambda route: route.fulfill(json=speech))
                editor.goto(base)
                editor.wait_for_function('()=>state!==null')
                editor.evaluate("navigate('tts')")
                editor.wait_for_selector('#sp-editor')
                editor.locator('#sp-editor').fill('First synthetic chapter.')
                editor.once('dialog', lambda dialog: dialog.accept('Second synthetic chapter'))
                editor.locator('#sp-chapter-add').click()
                editor.locator('#sp-editor').fill('Second synthetic chapter.')
                sent = []
                editor.on('request', lambda request: sent.append(request.post_data_json)
                          if request.method == 'POST' and request.url.endswith(('/api/speech/generate', '/api/speech/preview')) else None)
                def generate(button):
                    count = len(sent)
                    editor.locator(button).click()
                    for _ in range(100):
                        if len(sent) > count:
                            return sent[-1]
                        editor.wait_for_timeout(50)
                    raise AssertionError('Speech request was not sent: ' + button)
                body = generate('#sp-generate')
                check('Generate document submits the saved manuscript without a current-chapter filter', body['scope'] == 'document' and 'text' not in body and 'chapter_id' not in body)
                editor.get_by_text('Export destination and mastering', exact=True).click()
                body = generate('#sp-chapter-generate')
                check('Generate chapter submits the current chapter identity and nonempty text', body['scope'] == 'chapter' and body['chapter_id'] and body['text'] == 'Second synthetic chapter.')
                editor.locator('#sp-editor').evaluate("el=>{el.focus();el.setSelectionRange(0,6);el.dispatchEvent(new Event('select'));}")
                body = generate('#sp-generate-selection')
                check('Generate selection sends selected text with scope selection', body['scope'] == 'selection' and body['text'] == 'Second')
                body = generate('#sp-preview')
                check('Preview does not carry a manuscript chapter filter', body['scope'] == 'preview' and 'chapter_id' not in body and body['text'])
                body = generate('#sp-export')
                check('Export requests the saved project instead of current chapter text', body['scope'] == 'export' and 'text' not in body and 'chapter_id' not in body)
                check('No uncaught JavaScript errors', not errors)
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            app.state.store.close()
    report = {'version': __version__, 'passed': len(checks), 'checks': checks, 'errors': errors,
              'scope': 'Real browser/API, explicit restart-identity and model-availability fixtures; no application replacement or neural inference'}
    (args.output / 'BROWSER_V10_HOTFIX_RESULTS.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
