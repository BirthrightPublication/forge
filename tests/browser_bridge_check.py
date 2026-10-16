"""Exercise the UI through an HTTP bridge and real Chromium loopback navigation.

The bridge retains the existing isolated rendering regressions; the separate direct
browser checks use the real served assets, launcher ticket and browser cookies.
Synthetic update inventories below are rendering fixtures, never release packages.
"""
import json, base64, urllib.request, urllib.error, sys, time, argparse, shutil, re, http.cookiejar, threading
from pathlib import Path
from playwright.sync_api import sync_playwright
parser=argparse.ArgumentParser(description='Destructive test settings/fixtures: use only a disposable FORGE runtime.')
parser.add_argument('--runtime',type=Path,required=True,help='Disposable test runtime.json, not production data')
parser.add_argument('--output',type=Path,required=True,help='Dedicated test output/report folder')
parser.add_argument('--chromium',default=shutil.which('chromium'))
parser.add_argument('--allow-test-writes',action='store_true')
args=parser.parse_args()
if not args.allow_test_writes: parser.error('This harness changes test settings and adds jobs. Pass --allow-test-writes for a disposable runtime only.')
ROOT=Path(__file__).resolve().parent.parent
runtime=json.loads(args.runtime.read_text(encoding='utf-8'))
OUT=args.output.resolve();OUT.mkdir(parents=True,exist_ok=True)
SCREENSHOT_VERSION='v'+'.'.join(runtime['version'].split('.')[:2])
RUN_TAG=str(time.time_ns())
results=[]
update_scan_requests=[]
jar=http.cookiejar.CookieJar()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(jar))
transport_lock=threading.RLock()
def check(name,condition=True):
    if not condition: raise AssertionError(name)
    results.append(name);print(time.strftime('%H:%M:%S'),'PASS',name,flush=True)
def transport(url,options):
    assert url.startswith('/api/')
    if url=='/api/application-updates':update_scan_requests.append(url)
    data=base64.b64decode(options['body64']) if 'body64' in options else options.get('body','').encode() if options.get('method')=='POST' else None
    req=urllib.request.Request(f'http://127.0.0.1:{runtime["port"]}'+url,data=data,headers=options.get('headers',{}),method=options.get('method','GET'))
    try:
        with transport_lock:r=opener.open(req,timeout=45)
    except urllib.error.HTTPError as e:r=e
    return {'status':r.status,'headers':dict(r.headers),'body64':base64.b64encode(r.read()).decode()}
# isolated test outputs and no online checks
headers={'X-Forge-Token':runtime['token'],'Content-Type':'application/json'}
st=json.loads(base64.b64decode(transport('/api/state',{'headers':headers})['body64']))
s=st['settings'];s.update(default_output=str(OUT),daily_update_check=False)
transport('/api/settings',{'method':'POST','headers':headers,'body':json.dumps(s)})
transport('/api/queue',{'method':'POST','headers':headers,'body':'{"paused":false}'})
transport('/api/favorites',{'method':'POST','headers':headers,'body':'{"tool":"transcribe","pinned":false}'})
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=args.chromium,headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1920,'height':1080},device_scale_factor=1)
    page.set_default_timeout(8000)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.expose_function('__transport',transport)
    html=(ROOT/'web/index.html').read_text().replace('<script defer src="/assets/app.js"></script>','').replace('<script defer src="/assets/v2.js"></script>','').replace('<script defer src="/assets/v3.js"></script>','').replace('<link rel="icon" href="/assets/forge.ico">','').replace('<link rel="stylesheet" href="/assets/style.css">','')
    page.set_content(html);page.add_style_tag(content=(ROOT/'web/style.css').read_text())
    page.evaluate('''() => {window.fetch=async function(url,options={}) {if(options.body instanceof Blob){const a=new Uint8Array(await options.body.arrayBuffer());options.body64=btoa(Array.from(a,b=>String.fromCharCode(b)).join(''));delete options.body;}const r=await window.__transport(url,options);const a=Uint8Array.from(atob(r.body64),c=>c.charCodeAt(0));return new Response(a,{status:r.status,headers:r.headers});};}''')
    page.add_script_tag(content=(ROOT/'web/app.js').read_text().replace("let token = '', csrf",f"let token = '{runtime['token']}', csrf"))
    page.evaluate('(value)=>{token=value}',runtime['token'])
    page.add_script_tag(content=(ROOT/'web/v2.js').read_text())
    page.add_script_tag(content=(ROOT/'web/v3.js').read_text())
    page.wait_for_timeout(1200)
    if page.locator('.tool-card').count()==0:print('BOOT DEBUG',page.locator('body').inner_text(),errors,flush=True)
    page.wait_for_selector('.tool-card');check('Seven tool cards',page.locator('.tool-card').count()==7)
    page.screenshot(path=str(OUT/f'FORGE-{SCREENSHOT_VERSION}-overview.png'))
    def nav(v):
        if v=='settings':page.locator('#environment-nav').click()
        else:page.locator(f'#sidebar [data-view="{v}"]').first.click()
    for v in ['transcribe','retune','download_audio','download_video','metadata','qr','password','queue','workflows','settings']:
        nav(v);check('Navigation '+v,page.locator('h1').count()==1)
        check('Desktop no overflow '+v,not page.evaluate('document.documentElement.scrollWidth>innerWidth'))
    panel=page.locator('#application-updates-panel')
    check('Application updates loads without an automatic ZIP scan',not update_scan_requests and 'No local package check has been run.' in panel.inner_text())
    check('Application updates initially shows installed version',runtime['version'] in panel.inner_text())
    page.locator('#scan-application-updates').click()
    page.wait_for_function('applicationUpdates!==null&&!applicationUpdateScanning')
    real_inventory=page.evaluate('applicationUpdates')
    check('Manual application update check calls the real API',len(update_scan_requests)==1)
    check('Application update scan shows current version and real build folder',real_inventory['version']==runtime['version'] and real_inventory['build_directory'] in panel.inner_text())
    check('Application update scan records its current local check','Last local check:' in panel.inner_text() and bool(real_inventory['checked']))
    check('Application update scan does not fabricate an available update',bool(real_inventory.get('selected')) or 'No installable newer release was found' in panel.inner_text())
    check('Non-Windows scan explains Windows-only console',real_inventory['platform_supported'] or 'The updater console is available on Windows only.' in panel.inner_text())
    page.screenshot(path=str(OUT/f'FORGE-{SCREENSHOT_VERSION}-environment.png'))
    # Explicit browser rendering fixtures only: no archives are created or installed.
    unsafe_name='TEST FIXTURE <img src=x onerror="window.fixtureExecuted=true">.zip'
    unsafe_error='TEST FIXTURE <script>window.fixtureExecuted=true</script>'
    unsafe_hash='TEST FIXTURE <svg onload="window.fixtureExecuted=true">'
    available={'name':unsafe_name,'status':'available','version':'99.0.0','minimum_from':'0.2.0','sha256':unsafe_hash,'changed_files':2,'new_files':1,'preserved_files':7}
    invalid={'name':'TEST FIXTURE invalid.zip','status':'invalid','error':unsafe_error}
    conflict={'name':'TEST FIXTURE conflict.zip','status':'conflict','error':'TEST FIXTURE conflicting newest builds'}
    fixture=dict(real_inventory,build_directory='B:\\Forge\\Build',platform_supported=True,busy_jobs=0,maintenance=False,interrupted=False,selected=available,candidates=[available,invalid,conflict],rollback={'available':True,'from':'99.0.0','to':runtime['version']},warnings=['TEST FIXTURE: UI rendering only'])
    page.evaluate('(fixture)=>{window.fixtureExecuted=false;applicationUpdates=fixture;renderApplicationUpdates()}',fixture)
    fixture_text=panel.text_content()
    check('Build folder instructions retain Windows separators','B:\\Forge\\Build' in fixture_text)
    check('Available update fixture offers update and rollback review',page.locator('#open-application-update').is_enabled() and page.locator('#open-application-rollback').is_enabled())
    check('Candidate filename is rendered as escaped text',unsafe_name in fixture_text and panel.locator('img').count()==0)
    check('Candidate error is rendered as escaped text',unsafe_error in fixture_text and panel.locator('script').count()==0)
    check('Candidate hash is rendered as escaped text',unsafe_hash in fixture_text and panel.locator('.update-hash svg').count()==0)
    check('Invalid and conflict fixtures show their status',all(status in fixture_text for status in ['invalid','conflict']))
    check('Rollback fixture shows backup versions',f'Rollback available from 99.0.0 to {runtime["version"]}' in fixture_text)
    check('Escaped update fixtures never execute markup',page.evaluate('window.fixtureExecuted===false'))
    page.evaluate('()=>{applicationUpdates.busy_jobs=1;updateApplicationControls()}')
    check('Queued jobs disable both native update actions',page.locator('#open-application-update').is_disabled() and page.locator('#open-application-rollback').is_disabled() and '1 queued or active job' in panel.inner_text())
    page.evaluate('()=>{applicationUpdates.busy_jobs=0;applicationUpdates.maintenance=true;updateApplicationControls()}')
    check('Busy updater console disables both native update actions',page.locator('#open-application-update').is_disabled() and page.locator('#open-application-rollback').is_disabled() and 'already open' in panel.inner_text())
    page.evaluate('()=>{applicationUpdates.maintenance=false;applicationUpdates.interrupted=true;renderApplicationUpdates()}')
    check('Interrupted update requests external recovery and disables both actions','external Update FORGE.bat' in panel.inner_text() and page.locator('#open-application-update').is_disabled() and page.locator('#open-application-rollback').is_disabled())
    page.evaluate('()=>{applicationUpdates.interrupted=false;applicationUpdates.selected=null;applicationUpdates.candidates=applicationUpdates.candidates.filter(c=>c.status!=="available");applicationUpdates.rollback={available:false,error:"TEST FIXTURE no recoverable backup"};renderApplicationUpdates()}')
    check('Conflict inventory cannot enable update or unavailable rollback',page.locator('#open-application-update').is_disabled() and page.locator('#open-application-rollback').is_disabled())
    page.evaluate('()=>{applicationUpdates=null;delete window.fixtureExecuted;renderApplicationUpdates()}')
    page.locator('#scan-application-updates').click();page.wait_for_function('applicationUpdates!==null&&!applicationUpdateScanning')
    check('Synthetic update inventory is cleared by a real re-scan',len(update_scan_requests)==2 and 'TEST FIXTURE' not in panel.text_content())
    # A new page has no URL ticket, token injection or browser storage; the remembered HTTP cookie authorizes it.
    other=browser.new_page(viewport={'width':1366,'height':768});other.on('pageerror',lambda e:errors.append(str(e)));other.expose_function('__transport',transport)
    other.set_content(html);other.add_style_tag(content=(ROOT/'web/style.css').read_text())
    other.evaluate("()=>{window.fetch=async(url,options={})=>{const r=await window.__transport(url,options);const a=Uint8Array.from(atob(r.body64),c=>c.charCodeAt(0));return new Response(a,{status:r.status,headers:r.headers});};}")
    other.add_script_tag(content=(ROOT/'web/app.js').read_text());other.add_script_tag(content=(ROOT/'web/v2.js').read_text());other.add_script_tag(content=(ROOT/'web/v3.js').read_text())
    other.wait_for_selector('.tool-card');check('New tab boot uses remembered cookie without launcher fragment',other.locator('.tool-card').count()==7)
    check('No persistent runtime secret in JS',other.evaluate("token===''&&launchTicket===''"))
    other.close()
    nav('transcribe');page.screenshot(path=str(OUT/f'FORGE-{SCREENSHOT_VERSION}-transcribe.png'))
    page.locator('[name="timestamps"]').select_option('word')
    page.locator('[name="model"]').select_option('base')
    page.once('dialog',lambda d:d.accept('Browser test preset'))
    page.locator('#save-preset').click();page.wait_for_timeout(500)
    nav('qr');nav('transcribe')
    check('Draft settings retained',page.locator('[name="timestamps"]').input_value()=='word')
    page.locator('[name="timestamps"]').select_option('none')
    page.locator('#presets').select_option('Browser test preset')
    check('Saved preset reloads',page.locator('[name="timestamps"]').input_value()=='word')
    page.locator('[name="style"]').select_option('clean')
    page.locator('[name="timestamps"]').select_option('sentence')
    page.locator('[name="formats"][value="csv"]').check()
    check('Advanced transcript controls accept sentence / clean / CSV selections')
    page.locator('#favorite-tool').click();page.wait_for_timeout(600)
    check('Favorite tool persisted',page.evaluate('state.favorites.includes("transcribe")'))
    check('Pinning preserves unsaved tool draft',page.locator('[name="timestamps"]').input_value()=='sentence')
    # Real browser File input -> staging upload -> safe destination handling.
    page.locator('#browser-files').set_input_files({'name':'browser-test.wav','mimeType':'audio/wav','buffer':b'not real audio; upload path test only'})
    page.wait_for_selector('.file-item');check('Browser import labels staging','STAGED' in page.locator('.file-list').inner_text().upper())
    check('Staged source changes destination',page.locator('#out-mode').input_value()=='default')
    # Pasted native source routes via authenticated API, without invoking headless Tk.
    source=OUT/'source.png';from PIL import Image;Image.new('RGB',(20,20),'white').save(source)
    nav('metadata');page.locator('#paste-paths').click();page.locator('#path-list').fill(str(source));page.locator('#paths-submit').click();page.wait_for_selector('.file-item')
    check('Pasted original path keeps source destination',page.locator('#out-mode').input_value()=='source')
    page.locator('#out-mode').select_option('custom');check('Custom directory field appears',page.locator('#out-path').is_visible())
    page.locator('#out-path').fill(str(OUT/'metadata'));page.locator('#enqueue').click();page.wait_for_timeout(800)
    # Presets and workflow snapshots remain independent of polling/saved-tool changes.
    nav('retune');page.locator('[name="format"]').select_option('wav');page.locator('[name="target_hz"]').fill('435')
    page.once('dialog',lambda d:d.accept('Workflow retune preset'));page.locator('#save-preset').click();page.wait_for_timeout(500)
    nav('workflows');page.locator('[data-step-tool="0"]').select_option('retune');page.locator('[data-step-preset="0"]').select_option('Workflow retune preset')
    workflow_name='Browser workflow '+RUN_TAG
    page.locator('[data-step-tool="1"]').select_option('metadata');page.locator('#workflow-name').fill(workflow_name)
    page.locator('#save-workflow').click();page.wait_for_timeout(500)
    page.locator('#load-workflow').select_option(workflow_name);page.wait_for_timeout(2200)
    check('Saved workflow snapshot survives state polling',page.evaluate('workflowSpec().steps[0].options.target_hz===435'))
    check('Saved workflow has copied preset settings',page.locator('[data-step-preset="0"]').input_value()=='__snapshot__')
    # Add a real WAV source to a retune -> metadata chain.
    import wave,math,struct
    wav=OUT/'workflow-tone.wav'
    with wave.open(str(wav),'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(44100)
        f.writeframes(b''.join(struct.pack('<h',int(9000*math.sin(2*math.pi*440*i/44100))) for i in range(44100)))
    page.locator('#paste-paths').click();page.locator('#path-list').fill(str(wav));page.locator('#paths-submit').click();page.wait_for_selector('.file-item')
    page.screenshot(path=str(OUT/f'FORGE-{SCREENSHOT_VERSION}-workflows.png'))
    page.locator('#enqueue').click();page.wait_for_timeout(600);nav('queue')
    page.wait_for_function('(name)=>state.jobs.filter(j=>j.workflow_name===name&&j.status==="completed").length>=2',arg=workflow_name,timeout=20000)
    check('Actual browser workflow completed both linked FFmpeg steps')
    check('Workflow group controls visible',page.locator('[data-batch-retry]').count()>=1)
    # Generate a real queued QR via the UI, then display its actual output.
    nav('qr');page.locator('[name="text"]').fill('FORGE browser integration test')
    qr_name='FORGE Browser QR '+RUN_TAG
    page.locator('[name="name"]').fill(qr_name);page.locator('#enqueue').click();page.wait_for_timeout(500)
    nav('queue')
    page.wait_for_function('(name)=>state.jobs.some(j=>j.label===name&&j.status==="completed")',arg=qr_name,timeout=20000)
    job=page.evaluate('(name)=>state.jobs.find(j=>j.label===name&&j.status==="completed")',qr_name)
    check('QR completed through browser queue',bool(job['outputs']))
    page.evaluate('(id)=>jobDetail(id)',job['id']);page.wait_for_selector('.qr-preview')
    page.wait_for_function('document.querySelector(".qr-preview").naturalWidth>0')
    check('Authorized actual QR preview displayed')
    page.locator('#dialog form button').click()
    check('Original source preserved after UI metadata task',source.is_file())
    nav('password');before=page.evaluate('state.jobs.length');page.locator('#generate-password').click()
    page.wait_for_function('document.querySelector("#secret-output").textContent!=="No password generated."')
    check('Password generated',len(page.locator('#secret-output').inner_text())==24)
    nav('overview');nav('password');check('Password cleared on navigation',page.locator('#secret-output').inner_text()=='No password generated.')
    check('Password did not create a queue record',page.evaluate('state.jobs.length')==before)
    nav('settings');page.locator('[name="gpu_workers"]').fill('1');page.locator('[name="download_workers"]').fill('3');page.locator('#save-settings').click();page.wait_for_timeout(600)
    check('Resource limits saved',page.evaluate('state.settings.download_workers===3'))
    page.locator('#model-scan').click();page.wait_for_timeout(500)
    check('Model inventory rendered',len(page.locator('#model-results').inner_text())>20)
    page.locator('#gpu-diagnostics').click();page.wait_for_timeout(1800)
    check('Read-only diagnostics rendered',len(page.locator('#gpu-results').inner_text())>20)
    # Queue reorder/cancel via UI while dispatch is paused.
    nav('queue');page.locator('#pause-queue').click();page.wait_for_timeout(400)
    nav('qr');page.locator('[name="text"]').fill('Queued A');page.locator('[name="name"]').fill('Queued A');page.locator('#enqueue').click();page.wait_for_timeout(300)
    nav('qr');page.locator('[name="text"]').fill('Queued B');page.locator('[name="name"]').fill('Queued B');page.locator('#enqueue').click();page.wait_for_timeout(300);nav('queue')
    page.locator('[data-direction="up"]').last.click();page.wait_for_timeout(300)
    check('Queue priority controls accepted',page.evaluate('state.jobs.some(j=>j.rank!==undefined)'))
    page.locator('[data-filter="active"]').click();page.locator('#select-visible').click()
    page.once('dialog',lambda d:d.accept());page.locator('#cancel-selected').click();page.wait_for_timeout(500)
    check('Batch cancellation persisted',page.evaluate('state.jobs.filter(j=>j.label.startsWith("Queued ")).every(j=>j.status==="cancelled")'))
    page.locator('#pause-queue').click();page.wait_for_timeout(300);page.locator('[data-filter="all"]').click()
    for width,height in [(1366,768),(900,900),(440,900)]:
        page.set_viewport_size({'width':width,'height':height})
        for v in ['overview','transcribe','queue','workflows','metadata','password','settings']:
            nav(v);check(f'No overflow {width}px {v}',not page.evaluate('document.documentElement.scrollWidth>innerWidth'))
    page.set_viewport_size({'width':1920,'height':1080});nav('queue');page.screenshot(path=str(OUT/f'FORGE-{SCREENSHOT_VERSION}-queue.png'))
    check('No uncaught JavaScript errors',not errors)
    bridge_checks=len(results)
    # Real browser networking: all HTML, CSS, scripts, cookies and fetch calls come
    # from the running service. There are no transport overrides in this context.
    direct_errors=[]
    direct_requests=[]
    direct_context=browser.new_context(viewport={'width':1920,'height':1080})
    direct=direct_context.new_page()
    direct.set_default_timeout(10000)
    direct.on('pageerror',lambda e:direct_errors.append(str(e)))
    direct.on('request',lambda request:direct_requests.append(request.url.split('?')[0]) if '/api/application-updates' in request.url else None)
    ticket_response=transport('/api/launch-ticket',{'method':'POST','headers':headers,'body':'{}'})
    if ticket_response['status']!=200:raise AssertionError('Launcher ticket issuance failed')
    ticket=json.loads(base64.b64decode(ticket_response['body64']))['ticket']
    response=direct.goto(f'http://127.0.0.1:{runtime["port"]}/#launch={ticket}',wait_until='domcontentloaded')
    direct.wait_for_selector('.tool-card')
    check('Direct browser loads served application over loopback',response.status==200 and direct.locator('.tool-card').count()==7)
    check('Direct browser consumes launcher ticket and clears credentials',direct.evaluate("location.hash===''&&token===''&&launchTicket===''") and bool(direct_context.cookies()))
    check('Direct browser loads V0.3 extension from the service',direct.evaluate('typeof scanApplicationUpdates==="function"') and direct.locator('script[src="/assets/v3.js"]').count()==1)
    direct.screenshot(path=str(OUT/f'FORGE-{SCREENSHOT_VERSION}-overview-direct.png'))
    direct.locator('#environment-nav').click()
    direct_panel=direct.locator('#application-updates-panel')
    check('Direct browser settings does not scan release ZIPs automatically',not direct_requests and 'No local package check has been run.' in direct_panel.inner_text())
    with direct.expect_response(lambda response:response.url.endswith('/api/application-updates') and response.request.method=='GET') as scanned:
        direct.locator('#scan-application-updates').click()
    scanned_response=scanned.value
    direct.wait_for_function('applicationUpdates!==null&&!applicationUpdateScanning')
    scanned_inventory=scanned_response.json()
    check('Direct browser authenticates a real local build scan',scanned_response.status==200 and len(direct_requests)==1)
    check('Direct browser shows actual version and build directory',scanned_inventory['version']==runtime['version'] and scanned_inventory['build_directory'] in direct_panel.inner_text())
    check('Direct browser shows accurate Windows console availability',scanned_inventory['platform_supported'] or 'The updater console is available on Windows only.' in direct_panel.inner_text())
    direct.screenshot(path=str(OUT/f'FORGE-{SCREENSHOT_VERSION}-environment-direct.png'))
    second=direct_context.new_page()
    second.on('pageerror',lambda e:direct_errors.append(str(e)))
    second.goto(f'http://127.0.0.1:{runtime["port"]}/',wait_until='domcontentloaded')
    second.wait_for_selector('.tool-card')
    check('Direct second tab authenticates with its remembered browser cookie',second.locator('.tool-card').count()==7 and second.evaluate("location.hash===''&&token===''&&launchTicket===''") )
    second.close()
    direct.locator('#sidebar [data-view="qr"]').first.click()
    direct.locator('[name="text"]').fill('FORGE V0.3 real browser loopback integration')
    direct_qr_name='FORGE Direct Browser QR '+RUN_TAG
    direct.locator('[name="name"]').fill(direct_qr_name)
    direct.locator('#enqueue').click()
    direct.locator('#sidebar [data-view="queue"]').first.click()
    direct.wait_for_function('(name)=>state.jobs.some(j=>j.label===name&&j.status==="completed")',arg=direct_qr_name,timeout=20000)
    direct_job=direct.evaluate('(name)=>state.jobs.find(j=>j.label===name&&j.status==="completed")',direct_qr_name)
    check('Direct browser submits and completes a real QR job',bool(direct_job['outputs']))
    direct.evaluate('(id)=>jobDetail(id)',direct_job['id'])
    direct.wait_for_function('document.querySelector(".qr-preview")?.naturalWidth>0')
    check('Direct browser retrieves an authorized actual QR output preview',direct.locator('.qr-preview').is_visible())
    check('Direct browser and remembered tab have no uncaught JavaScript errors',not direct_errors)
    direct_context.close()
    browser.close()
(OUT/'browser-results.json').write_text(json.dumps({'checks':len(results),'bridge_checks':bridge_checks,'direct_browser_checks':len(results)-bridge_checks,'passed':results,'errors':errors+direct_errors,'transport':'HTTP bridge rendering regression plus real Chromium loopback navigation; no browser networking bypass'},indent=2))
print(f'{len(results)} browser checks passed')
