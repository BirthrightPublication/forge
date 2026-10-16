"""Exercise Black Scripting through Chromium and the real local HTTP service.

This script owns a newly created temporary runtime, never a production data path.
It tests real FFmpeg media jobs and preserves reports/screenshots under --output.
Run with the development requirements, Chromium and FFmpeg installed:

    python tests/browser_black_scripting_check.py --output /tmp/forge-evidence --allow-test-writes

Windows native file-dialog and CUDA behavior are outside this browser check.
"""
from pathlib import Path
import argparse, json, math, os, shutil, socket, struct, subprocess, sys, tempfile, time, uuid, wave
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser(description='Direct Chromium Black Scripting integration checks; creates its own disposable FORGE runtime.')
parser.add_argument('--output', type=Path, required=True, help='Evidence parent folder; a unique test-run subdirectory is created.')
parser.add_argument('--chromium', default=shutil.which('chromium'), help='Optional local Chromium executable; otherwise Playwright uses its installed browser.')
parser.add_argument('--allow-test-writes', action='store_true', help='Allow creating test presets/jobs in the self-owned disposable runtime.')
args = parser.parse_args()
if not args.allow_test_writes:
    parser.error('Pass --allow-test-writes to authorize jobs and presets in this harness-owned disposable runtime.')
OUT = args.output.resolve() / ('black-scripting-' + uuid.uuid4().hex[:12])
OUT.mkdir(parents=True, exist_ok=False)
scratch = tempfile.TemporaryDirectory(prefix='forge-black-scripting-')
DATA = Path(scratch.name) / 'data'
# The socket reservation finds an unused loopback port; FORGE still performs its
# own exclusive bind and fails safely if another process takes it first.
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
service_log = open(Path(scratch.name) / 'service.log', 'w+', encoding='utf-8')
process = subprocess.Popen([sys.executable, str(ROOT / 'server.py')], cwd=ROOT,
                           env={**os.environ, 'FORGE_DATA_DIR': str(DATA), 'FORGE_PORT': str(port)},
                           stdout=service_log, stderr=subprocess.STDOUT)
checks=[];errors=[];failed_assets=[]
def check(name, condition=True):
    assert condition,name
    checks.append(name);print('PASS',name,flush=True)
try:
    for _ in range(100):
        if (DATA/'runtime.json').exists():break
        if process.poll() is not None:
            service_log.seek(0)
            raise AssertionError('Disposable FORGE server did not start: ' + service_log.read())
        time.sleep(.1)
    runtime=json.loads((DATA/'runtime.json').read_text())
    base=f'http://127.0.0.1:{port}'
    audio=OUT/'source.wav'
    with wave.open(str(audio),'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(44100)
        f.writeframes(b''.join(struct.pack('<h',int(13000*math.sin(2*math.pi*440*i/44100))) for i in range(22050)))
    original=audio.read_bytes()
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=args.chromium,headless=True,args=['--no-sandbox'])
        context=browser.new_context(viewport={'width':1600,'height':1000},accept_downloads=True)
        page=context.new_page();page.set_default_timeout(10000)
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('response',lambda response:failed_assets.append(response.url) if '/assets/' in response.url and response.status>=400 else None)
        def api(route,body=None):
            method=context.request.get if body is None else context.request.post
            response=method(base+'/api'+route,headers={'X-Forge-Token':runtime['token']},**({'data':body} if body is not None else {}))
            assert response.ok,(route,response.status,response.text())
            return response.json()
        settings=api('/state')['settings'];settings['default_output']=str(OUT/'outputs');settings['queue_paused']=False;api('/settings',settings)
        api('/presets',{'tool':'retune','name':'432 lossless','options':{'target_hz':432,'format':'flac'},'output':{'mode':'source'}})
        ticket=api('/launch-ticket',{})['ticket']
        page.goto(base+'/#launch='+ticket)
        page.wait_for_selector('.tool-card')
        page.locator('#black-scripting-launcher').click()
        page.wait_for_selector('.black-plus')
        check('Initial workbench empty',page.locator('[data-black-node]').count()==0)
        check('Black Scripting absent from sidebar',page.locator('#sidebar [data-view="workflows"]').count()==0)
        check('Dedicated full width',page.locator('#sidebar').is_hidden())
        page.screenshot(path=str(OUT/'black-scripting-empty.png'),full_page=True)
        page.locator('#paste-paths').click();page.locator('#path-list').fill(str(audio));page.locator('#paths-submit').click()
        page.locator('#file-list .file-item').wait_for()
        check('Original path source selected','Original path' in page.locator('#file-list').inner_text())
        page.locator('#black-name').fill('Production chain')
        page.locator('#out-mode').select_option('custom');page.locator('#out-path').fill(str(OUT/'outputs'))
        page.locator('#black-add-node').click();page.locator('[data-black-tool="retune"]').click()
        page.locator('#black-tool-preset').select_option(label='432 lossless')
        check('Tool preset copied',page.evaluate('blackEditor.node.options.target_hz')==432 and page.evaluate('blackEditor.node.options.format')=='flac')
        page.locator('#black-confirm-node').click();page.locator('[data-black-node="0"]').wait_for()
        page.locator('#black-add-node').click();page.locator('[data-black-tool="metadata"]').click();page.locator('#black-confirm-node').click()
        page.wait_for_function('() => blackScript.nodes.length === 2 && blackEditor === null')
        check('Two operation chain confirmed',page.locator('[data-black-node]').count()==2)
        page.locator('[data-black-node="1"]').click();page.locator('#black-duplicate-node').click()
        page.wait_for_function('() => blackScript.nodes.length === 3')
        check('Duplicate node',page.locator('[data-black-node]').count()==3)
        page.locator('#black-toggle-node').click();page.locator('#black-confirm-node').click()
        page.wait_for_function('() => blackEditor === null')
        check('Disable node',page.evaluate('blackScript.nodes[2].enabled') is False)
        page.locator('[data-black-node="2"]').click();page.locator('#black-toggle-node').click();page.locator('#black-confirm-node').click()
        page.wait_for_function('() => blackEditor === null')
        check('Enable node',page.evaluate('blackScript.nodes[2].enabled') is True)
        page.locator('[data-black-node="2"]').click();page.locator('#black-remove-node').click()
        check('Remove node',page.locator('[data-black-node]').count()==2)
        page.locator('[data-black-node="1"]').click();page.locator('#black-left').click();page.locator('#black-confirm-node').click()
        page.wait_for_function('() => blackEditor === null')
        check('Reorder left',page.evaluate('blackScript.nodes[0].tool')=='metadata')
        page.locator('[data-black-node="0"]').click();page.locator('#black-right').click();page.locator('#black-confirm-node').click()
        page.wait_for_function('() => blackEditor === null')
        check('Reorder right',page.evaluate('blackScript.nodes[0].tool')=='retune')
        api('/presets',{'tool':'retune','name':'432 lossless','options':{'target_hz':444,'format':'mp3'},'output':{'mode':'source'}})
        page.locator('[data-black-node="0"]').click()
        check('Preset snapshot stable after preset edit',page.locator('#black-node-options [name="target_hz"]').input_value()=='432')
        page.locator('#black-node-options [name="target_hz"]').fill('431');page.locator('#black-confirm-node').click()
        page.wait_for_function('() => blackEditor === null')
        check('Edit node settings',page.evaluate('blackScript.nodes[0].options.target_hz')==431)
        page.locator('[data-black-node="0"]').click();page.locator('#black-node-options [name="target_hz"]').fill('432');page.locator('#black-confirm-node').click();page.wait_for_function('() => blackEditor === null')
        page.locator('#black-save').click();page.wait_for_function('() => blackSaved.length === 1')
        first_id=page.evaluate('blackScript.id')
        page.once('dialog',lambda dialog:dialog.accept('Renamed chain'));page.locator('#black-rename').click()
        page.wait_for_function('() => blackSaved[0].name === "Renamed chain"')
        check('Save and rename script',page.evaluate('blackSaved[0].id')==first_id)
        page.once('dialog',lambda dialog:dialog.accept('Copied chain'));page.locator('#black-copy').click()
        page.wait_for_function('() => blackSaved.length === 2')
        check('Duplicate script',page.evaluate('blackScript.id')!=first_id)
        page.locator('#black-presets').select_option(first_id)
        check('Reload saved script',page.locator('#black-name').input_value()=='Renamed chain')
        with page.expect_download() as download_info:page.locator('#black-export').click()
        exported=OUT/'exported.forge-script.json';download_info.value.save_as(exported)
        doc=json.loads(exported.read_text());check('Export safe JSON',doc['schema_version']==1 and 'sources' not in doc)
        page.locator('#black-import-file').set_input_files(str(exported));page.wait_for_function('() => blackSaved.length === 3')
        check('Import creates additional preset',page.evaluate('blackScript.id')!=first_id)
        page.locator('#toast').wait_for(state='hidden',timeout=10000)
        page.screenshot(path=str(OUT/'black-scripting-chain.png'),full_page=True)
        page.locator('[data-black-node="0"]').click();page.screenshot(path=str(OUT/'black-scripting-settings.png'),full_page=True)
        page.locator('#black-cancel-node').click()
        page.set_viewport_size({'width':390,'height':844})
        check('Mobile no document overflow',page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        page.screenshot(path=str(OUT/'black-scripting-mobile.png'),full_page=True)
        page.set_viewport_size({'width':1600,'height':1000})
        page.locator('#black-preview').click();page.locator('#black-plan-queue').wait_for()
        check('Preview shows dependencies and formats','2 queued operations' in page.locator('#dialog-content').inner_text() and 'published output' in page.locator('.black-plan').inner_text())
        check('Preview creates no jobs',not api('/state')['jobs'])
        page.screenshot(path=str(OUT/'black-scripting-plan.png'))
        page.locator('#black-plan-queue').click();page.wait_for_selector('#queue-list')
        for _ in range(200):
            jobs=api('/state')['jobs']
            if len(jobs)==2 and all(job['status']=='completed' for job in jobs):break
            if any(job['status'] in ['failed','blocked','interrupted'] for job in jobs):raise AssertionError(jobs)
            time.sleep(.1)
        check('Actual media chain completed through UI',len(jobs)==2 and all(job['status']=='completed' for job in jobs))
        check('Original bytes preserved',audio.read_bytes()==original)
        check('No JavaScript runtime errors',not errors)
        check('All app assets served',not failed_assets)
        context.close();browser.close()
    result={'passed':True,'checks':checks,'errors':errors,'failed_assets':failed_assets,'screenshots':[p.name for p in OUT.glob('*.png')]}
    (OUT/'browser-results.json').write_text(json.dumps(result,indent=2), encoding='utf-8')
    print('Evidence: ' + str(OUT), flush=True)
except Exception as exc:
    (OUT/'browser-results.json').write_text(json.dumps({'passed':False,'checks':checks,'failure':str(exc),'errors':errors,'failed_assets':failed_assets},indent=2))
    raise
finally:
    process.terminate()
    try:process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    service_log.close()
    scratch.cleanup()
