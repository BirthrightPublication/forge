"""V0.7 browser acceptance with a disposable server and explicit synthetic source metadata."""
import argparse
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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from forge import __version__
from forge.app import create_app

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--chromium',default=shutil.which('chromium'))
args=parser.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
checks=[];errors=[]
def check(label,condition=True):
    assert condition,label
    checks.append(label);print('PASS',label,flush=True)

with tempfile.TemporaryDirectory(prefix='forge-v7-browser-') as temp:
    listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    token=secrets.token_urlsafe(32);app=create_app(Path(temp)/'data',token,port,workers=False)
    app.state.update_catalog.busy=False
    app.state.store.set('update_catalog',{'checked':time.time(),'entries':[
        {'id':'application','label':'FORGE','category':'application','installed':__version__,'available':'9.0.0','action':'update','notes':'Synthetic future test release. Never installed.'},
        {'id':'downloads','label':'Downloads','category':'dependencies','installed':'fixture','available':'compatible fixture','action':'repair'},
        {'id':'model:tiny','label':'tiny','category':'models','installed':'','available':'a'*40,'action':''}], 'errors':[]})
    folder=Path(temp)/'exports';folder.mkdir()
    app.state.download_studio.destinations.remember(folder,name='Synthetic exports',pinned=True,default=True)
    class Adapter:
        platform='youtube';capabilities={'discovery':True}
        def inspect(self,url,**kwargs):
            return {'source_id':'UC_SYNTHETIC_CHANNEL','url':'https://www.youtube.com/channel/UC_SYNTHETIC_CHANNEL',
                    'title':'Synthetic Channel — Offline Fixture','reported_count':None,'formats':[{'height':720}],
                    'platform':'youtube','entries':[{'id':'fixture0001','url':'https://www.youtube.com/watch?v=fixture0001',
                        'title':'Synthetic video — no live download','type':'video','duration':60,'upload_date':'20261023','published':time.time()}]}
    app.state.download_studio.adapter=Adapter()
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
    try:
        for _ in range(150):
            if server.started:break
            time.sleep(.1)
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=args.chromium,headless=True,args=['--no-sandbox'])
            context=browser.new_context(viewport={'width':1440,'height':1000},permissions=['clipboard-read','clipboard-write'],timezone_id='America/Los_Angeles')
            page=context.new_page();page.set_default_timeout(12000);page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{port}/#token='+token);page.wait_for_selector('#dl-urls')
            check('Download Studio is the working landing page',page.locator('h1').inner_text()=='Download Studio')
            check('Icon-first sidebar defaults to compact',page.locator('body').evaluate("e=>e.classList.contains('compact')"))
            check('No Overview, Queue or duplicate Environment sidebar entry',page.locator('#sidebar [data-view="overview"],#sidebar [data-view="queue"],#environment-button').count()==0)
            check('Settings occupies workspace navigation',page.locator('#workspace-nav [data-view="settings"]').count()==1)
            check('Header breadcrumb removed',page.locator('.breadcrumb').count()==0)
            page.locator('#collapse').click()
            check('Brand subtitle comes from running version','SCRIPTING ENVIRONMENT ENGINE' in page.locator('.brand-words').inner_text() and page.locator('#brand-version').inner_text().startswith('V'))
            page.locator('#collapse').click()
            page.wait_for_timeout(350)  # Allow the sidebar's width transition to settle.
            settings=page.locator('#workspace-nav [data-view="settings"]');settings.hover()
            page.wait_for_selector('#navigation-tooltip:not([hidden])')
            check('Tooltip names the actual tool outside the sidebar',page.locator('#navigation-tooltip').inner_text()=='Settings' and page.locator('#navigation-tooltip').bounding_box()['x']>=page.locator('#sidebar').bounding_box()['width'])
            sidebar=page.locator('#sidebar').bounding_box();engine=page.locator('.local-mark').bounding_box()
            check('Local Engine anchored at sidebar bottom',sidebar['height']-(engine['y']+engine['height'])<24)
            search=page.locator('.global-search').bounding_box();top=page.locator('.topbar').bounding_box()
            check('Header search is left aligned',search['x']-top['x']<40)
            page.keyboard.press('Control+k');check('Ctrl+K focuses search',page.locator('#search').evaluate('e=>e===document.activeElement'))
            page.locator('#dl-urls').fill('https://www.youtube.com/watch?v=synthetic')
            check('Pinned destinations offered before network inspection',page.locator('#download-directory-list option').count()==1)
            page.locator('#dl-preview').click();page.wait_for_selector('#dl-preview-result strong')
            check('Preview renders explicit source metadata','720p' in page.locator('#dl-preview-result').inner_text())
            page.screenshot(path=str(output/'download-studio.png'),full_page=True)
            page.locator('#black-scripting-launcher').click();page.wait_for_selector('.black-workbench')
            check('Black Scripting mode hides sidebar',not page.locator('#sidebar').is_visible())
            check('Black Scripting has explicit exit state','Exit Black Scripting' in page.locator('#black-scripting-launcher').inner_text())
            page.locator('#black-scripting-launcher').click();page.wait_for_selector('#dl-urls')
            check('Exiting restores Download Studio draft',page.locator('#dl-urls').input_value().endswith('synthetic'))
            page.locator('[data-download-tab="watch"]').click();page.locator('#watch-add').click()
            page.locator('#watch-url').fill('https://www.youtube.com/@synthetic');page.locator('#watch-save').click()
            page.wait_for_selector('.watch-source')
            check('Channel registration displays persistent source','Synthetic Channel' in page.locator('.watch-source').inner_text())
            check('Unreported totals remain unknown','Unknown' in page.locator('.watch-stats').inner_text())
            check('Default future-only registration never backfills',app.state.store.jobs()==[])
            page.screenshot(path=str(output/'channel-watch.png'),full_page=True)
            page.locator('[data-watch-media]').click();page.wait_for_selector('.watch-media tbody tr')
            check('Historical media remains independently reviewable','skipped' in page.locator('.watch-media').inner_text())
            page.evaluate("navigate('addresses')");page.wait_for_selector('#generator-generate');page.locator('#generator-generate').click()
            page.wait_for_selector('.address-record');check('Form-ready default address has six fields',page.locator('.address-field input').count()==6)
            street=page.locator('.address-field input').first.input_value()
            page.get_by_role('button',name='Copy Street Address 1',exact=True).click()
            check('Individual Copy writes exactly the selected field',page.evaluate('navigator.clipboard.readText()')==street)
            page.get_by_role('button',name='Lock City',exact=True).click()
            city=page.locator('.address-field').filter(has=page.locator('label',has_text='City')).locator('input').input_value()
            page.get_by_role('button',name='Regenerate Street Address 1',exact=True).click()
            page.wait_for_function('old=>document.querySelector(".address-field input").value!==old',arg=street)
            check('Single field regeneration preserves locked City',page.locator('.address-field').filter(has=page.locator('label',has_text='City')).locator('input').input_value()==city)
            page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));delete document.hidden")
            check('Switching tabs keeps addresses available for external forms',page.locator('.address-field input').count()==6)
            current=page.locator('.address-field input').first.input_value()
            page.locator('#black-scripting-launcher').click();page.wait_for_selector('.black-workbench')
            page.screenshot(path=str(output/'black-scripting.png'),full_page=True)
            page.locator('#black-scripting-launcher').click();page.wait_for_selector('.address-record')
            check('Mode exit restores address fields and locks',page.locator('.address-field input').first.input_value()==current and page.get_by_role('button',name='Lock City',exact=True).get_attribute('aria-pressed')=='true')
            page.screenshot(path=str(output/'address-fields.png'),full_page=True)
            page.locator('#birthright-updates').click();page.wait_for_selector('.birthright-updates[open]')
            box=page.locator('.birthright-updates').bounding_box()
            check('Updates is a compact right-side panel',box['x']>850 and box['width']<500)
            check('Only actionable updates counted',page.locator('#update-count').inner_text()=='2')
            check('Optional processing batch excludes application','1' in page.locator('#updates-all').inner_text())
            page.screenshot(path=str(output/'update-center.png'),full_page=True)
            page.locator('#birthright-updates').click();check('Header icon toggles Updates closed',not page.locator('.birthright-updates').evaluate('e=>e.open'))
            for width in (1440,900,560):
                page.set_viewport_size({'width':width,'height':1000})
                page.evaluate("navigate('downloads')");page.locator('[data-download-tab="single"]').click();page.wait_for_selector('#dl-urls')
                check('Download layout fits '+str(width),page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                page.locator('#birthright-updates').click();box=page.locator('.birthright-updates').bounding_box()
                check('Update panel fits '+str(width),box['x']>=0 and box['x']+box['width']<=width)
                page.keyboard.press('Escape')
            app.state.manager.maintenance_pending=True
            app.state.store.set('maintenance',{'id':'c'*32,'status':'waiting','requested':time.time(),'message':'Synthetic maintenance waiting for active work.'})
            page.wait_for_selector('#maintenance-banner')
            check('Pending maintenance is visible outside Updates','waiting' in page.locator('#maintenance-banner').inner_text())
            page.locator('#maintenance-banner button').click();page.wait_for_selector('#maintenance-banner',state='detached')
            check('Pending request cancels without removing queued/history data',not app.state.manager.maintenance_pending and app.state.download_studio.media()[0]['status']=='skipped')
            check('No uncaught browser errors',not errors)
            browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=15);app.state.store.close()
        (output/'BROWSER_V7_RESULTS.json').write_text(json.dumps({'version':__version__,'checks':checks,'passed':len(checks),'errors':errors,'platform':'Linux Chromium','source_metadata':'synthetic offline fixture, not live YouTube'},indent=2))
