"""Chromium acceptance using a disposable service, synthetic update metadata and QR jobs."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from forge.core import Store
from forge import __version__

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--chromium',default=shutil.which('chromium'))
args=parser.parse_args()
output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
data=output/'data';exports=output/'exports';exports.mkdir()
store=Store(data);settings=store.settings();settings['automatic_update_checks']=False;store.set('settings',settings)
entries=[{'id':'application','label':'FORGE','category':'application','installed':'0.6.0','available':'9.0.0','action':'update','published':'2026-10-22T00:00:00Z','notes':'Synthetic browser test release; never installed.'},
         {'id':'transcription','label':'Transcription engine','category':'dependencies','installed':'Synthetic older engine','available':'Compatible test fixture','action':'repair'},
         {'id':'model:tiny','label':'tiny','category':'models','installed':'a'*40,'available':'b'*40,'action':'update','revisions':[]},
         {'id':'gpu-runtime','label':'GPU runtime','category':'gpu','installed':'Check runtime','available':'CUDA 12 / cuDNN 9','action':''}]
store.set('update_catalog',{'entries':entries,'checked':time.time(),'next_check':time.time()+86400,'errors':[]});store.close()
with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
environment={**os.environ,'FORGE_DATA_DIR':str(data),'FORGE_PORT':str(port),'HF_HUB_OFFLINE':'1'}
log=(output/'server.log').open('w')
server=subprocess.Popen([sys.executable,str(ROOT/'server.py')],cwd=ROOT,env=environment,stdout=log,stderr=log)
checks=[];errors=[]
def check(name,condition=True):
    assert condition,name
    checks.append(name);print('PASS',name,flush=True)
try:
    for _ in range(100):
        if (data/'runtime.json').exists():break
        if server.poll() is not None:raise RuntimeError('Disposable service exited')
        time.sleep(.1)
    runtime=json.loads((data/'runtime.json').read_text())
    base=f'http://127.0.0.1:{port}'
    def request(route,body=None):
        req=urllib.request.Request(base+'/api'+route,data=json.dumps(body).encode() if body is not None else None,
                                   headers={'X-Forge-Token':runtime['token'],'Content-Type':'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=10) as response:return json.load(response)
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(executable_path=args.chromium,headless=True,args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':1440,'height':1000});page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(base+'/#token='+runtime['token']);page.wait_for_selector('#birthright-updates')
        page.wait_for_function('version=>state!==null&&state.version===version',arg=__version__)
        check('Brand reads boxed F plus ORGE',page.locator('.brand-mark').inner_text()=='F' and page.locator('.brand-words strong').inner_text()=='ORGE')
        check('Accessible FORGE brand retained','FORGE' in page.locator('.brand').get_attribute('aria-label'))
        check('Visible Ctrl+K label removed',page.locator('kbd').count()==0)
        page.keyboard.press('Control+k');check('Ctrl+K still focuses search',page.locator('#search').evaluate('(el)=>el===document.activeElement'))
        check('Badge counts actionable updates only',page.locator('#update-count').inner_text()=='3')
        page.locator('#birthright-updates').click();page.wait_for_selector('.birthright-updates[open]')
        check('Birthright panel shows current and available version',__version__ in page.locator('.birthright-updates').inner_text() and '9.0.0' in page.locator('.birthright-updates').inner_text())
        check('Changelog and release date are available',page.get_by_text('Changelog',exact=True).count()==1 and page.locator('.birthright-update-row').first.locator('small').count()>=2)
        check('Compatible batch excludes the separately approved application','2' in page.locator('#updates-all').inner_text())
        page.once('dialog',lambda dialog:dialog.dismiss());page.locator('#updates-all').click()
        check('Declining installation does not create jobs or approvals',request('/state')['jobs']==[] and not request('/maintenance').get('id'))
        page.locator('#updates-later').click();check('Later closes updates',not page.locator('.birthright-updates').evaluate('(el)=>el.open'))
        page.evaluate("navigate('transcribe')")
        page.locator('[name=model]').select_option('base',force=True);page.locator('[name=device]').select_option('cpu',force=True)
        page.get_by_role('tab',name='Recognition accuracy',exact=True).click()
        page.locator('[name=beam_size]').fill('7')
        page.get_by_role('tab',name='Language and vocabulary',exact=True).click()
        page.locator('[name=vocabulary]').fill('PRIVATE TEST VOCABULARY')
        page.wait_for_function("()=>state!==null&&draft('transcribe').options.model==='base'")
        page.wait_for_timeout(500)
        prefs=request('/preferences')['tools']['transcribe']['options']
        check('Working model, device and accuracy save automatically',prefs['model']=='base' and prefs['device']=='cpu' and prefs['beam_size']==7)
        check('Vocabulary content is excluded from working preferences','vocabulary' not in prefs)
        check('First-download checkbox removed',page.locator('[name=allow_model_download]').count()==0)
        page.reload();page.wait_for_function('()=>state!==null');page.evaluate("navigate('transcribe')")
        check('Working settings survive browser reload',page.locator('[name=model]').input_value()=='base' and page.locator('[name=device]').input_value()=='cpu')
        check('Sensitive vocabulary does not return after reload',page.locator('[name=vocabulary]').input_value()=='')
        page.locator('#reset-working-settings').click();page.wait_for_timeout(400)
        check('Reset restores defaults',page.locator('[name=model]').input_value()=='large-v3')
        page.evaluate("navigate('password')");page.locator('[name=length]').fill('40',force=True);page.wait_for_timeout(400)
        check('Password policy persists without generated values',request('/preferences')['tools']['password']['options']['length']==40)
        page.evaluate("navigate('strings')");page.wait_for_selector('#generator-generate');page.locator('[name=length]').fill('48',force=True);page.wait_for_timeout(400)
        check('Generator configuration also persists',request('/preferences')['tools']['strings']['options']['length']==48)
        page.evaluate("navigate('queue')")
        check('Queue explanatory footer removed',page.locator('.v4-queue-note').count()==0)
        created=request('/jobs',{'tool':'qr','options':{'text':'Synthetic V0.6 duration fixture','name':'duration'},'output':{'mode':'custom','directory':str(exports)}})
        for _ in range(100):
            jobs=request('/state')['jobs']
            if jobs and jobs[0]['status'] in ('completed','failed'):break
            time.sleep(.1)
        job=jobs[0]
        check('Actual QR worker stores measured execution duration',job['status']=='completed' and job['execution_seconds']>=0 and job['finished']>=job['started'])
        page.evaluate('refresh()');page.wait_for_function("()=>state.jobs.some(j=>j.status==='completed')")
        page.evaluate("filter='history';renderQueue()")
        check('Completed queue row displays duration','Completed ·' in page.locator('#queue-list').inner_text())
        check('Sidebar remains scrollable without a visible scrollbar',page.locator('.nav-scroll').evaluate('(el)=>getComputedStyle(el).overflowY==="auto"&&getComputedStyle(el).scrollbarWidth==="none"'))
        page.locator('#birthright-updates').click();page.screenshot(path=str(output/'FORGE-V0.6-updates.png'))
        page.locator('#updates-later').click();page.evaluate("navigate('transcribe')");page.screenshot(path=str(output/'FORGE-V0.6-transcribe.png'))
        for width in (1440,900,560):
            page.set_viewport_size({'width':width,'height':1000});page.locator('#birthright-updates').click()
            check('Updates fit viewport '+str(width),page.locator('.birthright-updates').evaluate('(el)=>el.getBoundingClientRect().right<=innerWidth&&el.getBoundingClientRect().left>=0'))
            page.locator('#updates-later').click()
        check('No uncaught browser errors',not errors)
        browser.close()
    (output/'BROWSER_V6_RESULTS.json').write_text(json.dumps({'checks':checks,'errors':errors,'limitations':['Synthetic update metadata; no real application installation in this browser test.','No physical CUDA hardware.']},indent=2))
finally:
    server.terminate()
    try:server.wait(timeout=15)
    except subprocess.TimeoutExpired:server.kill();server.wait()
    log.close()
