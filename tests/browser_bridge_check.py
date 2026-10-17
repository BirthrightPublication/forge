"""Browser UI exercise against the real local HTTP API through a transport bridge.
Used because this sandbox's managed Chromium blocks all URL navigation.
No security policy is changed. This is not a real browser-to-loopback network test.
"""
import json, base64, urllib.request, urllib.error, sys, time, argparse, shutil
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
results=[]
def check(name,condition=True):
    if not condition: raise AssertionError(name)
    results.append(name);print('PASS',name,flush=True)
def transport(url,options):
    assert url.startswith('/api/')
    data=base64.b64decode(options['body64']) if 'body64' in options else options.get('body','').encode() if options.get('method')=='POST' else None
    req=urllib.request.Request(f'http://127.0.0.1:{runtime["port"]}'+url,data=data,headers=options.get('headers',{}),method=options.get('method','GET'))
    try:r=urllib.request.urlopen(req,timeout=30)
    except urllib.error.HTTPError as e:r=e
    return {'status':r.status,'headers':dict(r.headers),'body64':base64.b64encode(r.read()).decode()}
# isolated test outputs and no online checks
headers={'X-Forge-Token':runtime['token'],'Content-Type':'application/json'}
st=json.loads(base64.b64decode(transport('/api/state',{'headers':headers})['body64']))
s=st['settings'];s.update(default_output=str(OUT),daily_update_check=False)
transport('/api/settings',{'method':'POST','headers':headers,'body':json.dumps(s)})
transport('/api/queue',{'method':'POST','headers':headers,'body':'{"paused":false}'})
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=args.chromium,headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1920,'height':1080},device_scale_factor=1)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.expose_function('__transport',transport)
    html=(ROOT/'web/index.html').read_text().replace('<script defer src="/assets/app.js"></script>','').replace('<link rel="icon" href="/assets/forge.ico">','').replace('<link rel="stylesheet" href="/assets/style.css">','')
    page.set_content(html);page.add_style_tag(content=(ROOT/'web/style.css').read_text())
    page.evaluate('''() => {window.fetch=async function(url,options={}) {if(options.body instanceof Blob){const a=new Uint8Array(await options.body.arrayBuffer());options.body64=btoa(Array.from(a,b=>String.fromCharCode(b)).join(''));delete options.body;}const r=await window.__transport(url,options);const a=Uint8Array.from(atob(r.body64),c=>c.charCodeAt(0));return new Response(a,{status:r.status,headers:r.headers});};}''')
    page.add_script_tag(content=(ROOT/'web/app.js').read_text().replace("let token = '';",f"let token = '{runtime['token']}';"))
    page.wait_for_selector('.tool-card');check('Seven tool cards',page.locator('.tool-card').count()==7)
    page.screenshot(path=str(OUT/'FORGE-v0.1-overview.png'))
    def nav(v):
        if v=='settings':page.locator('#environment-nav').click()
        else:page.locator(f'#sidebar [data-view="{v}"]').click()
    for v in ['transcribe','retune','download_audio','download_video','metadata','qr','password','queue','settings']:
        nav(v);check('Navigation '+v,page.locator('h1').count()==1)
        check('Desktop no overflow '+v,not page.evaluate('document.documentElement.scrollWidth>innerWidth'))
    nav('transcribe');page.screenshot(path=str(OUT/'FORGE-v0.1-transcribe.png'))
    page.locator('[name="timestamps"]').select_option('word')
    page.locator('[name="model"]').select_option('base')
    page.once('dialog',lambda d:d.accept('Browser test preset'))
    page.locator('#save-preset').click();page.wait_for_timeout(500)
    nav('qr');nav('transcribe')
    check('Draft settings retained',page.locator('[name="timestamps"]').input_value()=='word')
    page.locator('[name="timestamps"]').select_option('none')
    page.locator('#presets').select_option('Browser test preset')
    check('Saved preset reloads',page.locator('[name="timestamps"]').input_value()=='word')
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
    # Generate a real queued QR via the UI, then display its actual output.
    nav('qr');page.locator('[name="text"]').fill('FORGE browser integration test')
    page.locator('[name="name"]').fill('FORGE Browser QR');page.locator('#enqueue').click();page.wait_for_timeout(500)
    nav('queue')
    page.wait_for_function('state.jobs.some(j=>j.label==="FORGE Browser QR"&&j.status==="completed")',timeout=20000)
    job=page.evaluate('state.jobs.find(j=>j.label==="FORGE Browser QR"&&j.status==="completed")')
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
    for width,height in [(1366,768),(900,900),(440,900)]:
        page.set_viewport_size({'width':width,'height':height})
        for v in ['overview','transcribe','queue','password','settings']:
            nav(v);check(f'No overflow {width}px {v}',not page.evaluate('document.documentElement.scrollWidth>innerWidth'))
    page.set_viewport_size({'width':1920,'height':1080});nav('queue');page.screenshot(path=str(OUT/'FORGE-v0.1-queue.png'))
    check('No uncaught JavaScript errors',not errors)
    browser.close()
(OUT/'browser-results.json').write_text(json.dumps({'checks':len(results),'passed':results,'errors':errors,'transport':'HTTP bridge; sandbox browser navigation blocked'},indent=2))
print(f'{len(results)} browser checks passed')
