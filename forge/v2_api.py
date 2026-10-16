from __future__ import annotations
import contextlib
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from .core import ROOT, TOOLS, IMAGES, absolute_path, no_window
from .models import BatchAction, JobOrder, ModelDownload, Workflow, WorkflowRun


def mount(app,store,manager,sessions,destination,token):
    def public(job):
        return {k:v for k,v in job.items() if k!='payload'}

    @app.post('/api/update/prepare')
    def prepare_update(request: Request):
        if not secrets.compare_digest(request.headers.get('x-forge-token',''),token):
            raise HTTPException(403,'Only the native updater can prepare maintenance.')
        with manager.lock:
            if manager.maintenance:
                return {'previous_settings':app.state.pre_update_settings}
            if any(j['status'] in {'running','queued','cancelling','finalizing'} for j in store.jobs(limit=100000)):
                raise ValueError('The queue is not idle. Finish or cancel jobs before updating.')
            app.state.pre_update_settings=store.settings()
            manager.maintenance=True
            store.set('settings',{**app.state.pre_update_settings,'queue_paused':True})
        return {'previous_settings':app.state.pre_update_settings}

    @app.post('/api/update/cancel')
    def cancel_update(request: Request):
        if not secrets.compare_digest(request.headers.get('x-forge-token',''),token):
            raise HTTPException(403,'Only the native updater can cancel maintenance.')
        with manager.lock:
            if manager.maintenance:
                store.set('settings',app.state.pre_update_settings)
            manager.maintenance=False
        return {'ok':True}

    @app.post('/api/launch-ticket')
    def launch_ticket(request: Request):
        if not secrets.compare_digest(request.headers.get('x-forge-token',''),token):
            raise HTTPException(403,'Only the native launcher can issue browser authorization tickets.')
        return {'ticket':sessions.ticket(),'expires_in':120}

    @app.post('/api/session')
    async def authorize(request: Request):
        bearer=secrets.compare_digest(request.headers.get('x-forge-token',''),token)
        body=await request.json()
        if not isinstance(body,dict):
            raise HTTPException(400,'Expected an authorization object.')
        if not bearer and not sessions.consume(body.get('ticket','')):
            raise HTTPException(401,'Open FORGE once from its launcher in this browser profile. The authorization link may have expired.')
        response=JSONResponse({})
        # Reopening the launcher must not invalidate CSRF state in existing tabs.
        item=sessions.lookup(request.cookies.get(sessions.cookie,''))
        if not item:
            item=sessions.create(response)
        response.body=json.dumps({'authorized':True,'csrf':item['csrf'],'expires':item['expires']}).encode()
        response.headers['content-length']=str(len(response.body))
        return response

    @app.get('/api/session')
    def session(request: Request):
        item=sessions.lookup(request.cookies.get(sessions.cookie,''))
        if not item:
            raise HTTPException(401,'Open FORGE once from its launcher to remember this browser. Bookmarks then work in other normal tabs.')
        return {'authorized':True,'csrf':item['csrf'],'expires':item['expires']}

    @app.post('/api/session/forget')
    def forget(request: Request):
        response=JSONResponse({'ok':True})
        sessions.forget(request.cookies.get(sessions.cookie,''),response)
        return response

    @app.post('/api/jobs/{id}/order')
    def order(id: str,body: JobOrder):
        manager.reorder(id,body.direction)
        return {'ok':True}

    @app.post('/api/jobs/batch-action')
    def batch_action(body: BatchAction):
        result=[]
        for id in dict.fromkeys(body.ids):
            try:
                if body.action=='cancel':
                    manager.cancel(id)
                    result.append({'id':id,'ok':True})
                else:
                    result.append({'id':id,'ok':True,'job':public(manager.retry(id))})
            except ValueError as exc:
                result.append({'id':id,'ok':False,'error':str(exc)})
        return {'results':result}

    @app.post('/api/batches/{id}/retry')
    def retry_batch(id: str):
        return {'jobs':[public(j) for j in manager.retry_batch(id)]}

    @app.post('/api/batches/{id}/cancel')
    def cancel_batch(id: str):
        results=[]
        for j in store.jobs(limit=100000):
            if j.get('batch_id')==id and j['status'] in {'queued','running'}:
                try:
                    manager.cancel(j['id'])
                    results.append({'id':j['id'],'ok':True})
                except ValueError as exc:
                    results.append({'id':j['id'],'ok':False,'error':str(exc)})
        return {'results':results}

    @app.post('/api/workflows/save')
    def save_workflow(body: Workflow):
        values=[w for w in store.get('workflows',[]) if w['name']!=body.name]
        values.append(body.model_dump())
        store.set('workflows',values[-100:])
        return {'workflows':values[-100:]}

    @app.post('/api/workflows/run')
    def run_workflow(body: WorkflowRun):
        settings=store.settings()
        plans=[]
        # Validate the entire graph and every destination before creating jobs.
        for id in dict.fromkeys(body.sources):
            source=store.source(id)
            path=absolute_path(source['path'])
            ext=path.suffix.lower()
            for step in body.steps:
                if ext not in TOOLS[step.tool]['accept']:
                    raise ValueError(f'{path.name}: {step.tool} cannot accept the previous step’s {ext} output.')
                if step.tool=='retune':
                    ext='.'+step.options['format']
            folder=destination(body.output,source,settings)
            stat=path.stat()
            plans.append((source,str(folder),{'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}))
        batch=uuid.uuid4().hex
        jobs=[]
        with manager.lock:
            manager.check_accepting()
            for source,folder,snapshot in plans:
                previous=''
                for index,step in enumerate(body.steps,1):
                    payload={'tool':step.tool,'options':step.options,'settings':settings,'destination':folder,
                             'source':source,'source_stat':snapshot,'url':None,'depends_on':previous,'batch_id':batch,
                             'workflow_name':body.name,'step':index,'steps':len(body.steps)}
                    job=store.add_job(step.tool,source['name'],payload)
                    previous=job['id'];jobs.append(public(job))
        if body.output.mode=='custom':
            settings['last_output']=body.output.directory
            store.set('settings',settings)
        return {'jobs':jobs,'batch_id':batch}

    @app.post('/api/metadata/inspect')
    def inspect_metadata(body: dict):
        from .worker import image_metadata,media_metadata,probe
        source=store.source(str(body.get('source','')))
        path=absolute_path(source['path'])
        if path.suffix.lower() not in TOOLS['metadata']['accept']:
            raise ValueError('This file format is not supported by Metadata Studio.')
        result=image_metadata(path) if path.suffix.lower() in IMAGES else media_metadata(probe(path,store.settings()))
        return {'metadata':result,'note':'Displayed only. No metadata was changed. Fields can contain private location or author information.'}

    @app.get('/api/jobs/{id}/media')
    def source_media(id: str):
        job=store.job(id)
        source=job['payload'].get('source')
        if not source:
            raise HTTPException(404,'This job has no source media.')
        path=absolute_path(source['path'])
        if path.suffix.lower() not in TOOLS['transcribe']['accept']:
            raise HTTPException(400,'Not playable media.')
        # Registered source only, no arbitrary path input; supports HTTP Range.
        return FileResponse(path,content_disposition_type='inline')

    @app.post('/api/environment/diagnostics')
    def diagnostics():
        interpreter=Path(sys.executable).with_name('python.exe') if os.name=='nt' else Path(sys.executable)
        try:
            p=subprocess.run([str(interpreter),str(ROOT/'scripts'/'diagnostics.py')],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             encoding='utf-8',errors='replace',timeout=40,env={**os.environ,'PYTHONUTF8':'1'},**no_window())
        except subprocess.TimeoutExpired:
            raise ValueError('Diagnostics timed out. No runtime, driver or model was modified.')
        if p.returncode:
            raise ValueError('Diagnostics failed: '+p.stderr[-1000:])
        result=json.loads(p.stdout)
        store.set('diagnostics',result)
        return result

    @app.get('/api/environment/models')
    def models():
        values=[]
        for name in ('tiny','base','small','medium','large-v3','large-v3-turbo'):
            folders=list((store.data/'models').glob('models--*--faster-whisper-'+name))
            snapshots=[]
            for folder in folders:
                snapshots.extend(p for p in (folder/'snapshots').glob('*') if (p/'model.bin').is_file() and (p/'config.json').is_file() and (p/'tokenizer.json').is_file())
            size=0
            for folder in folders:
                with contextlib.suppress(OSError):
                    size+=sum(p.stat().st_size for p in (folder/'blobs').glob('*') if p.is_file())
            values.append({'model':name,'cached':bool(snapshots),'bytes':size,'note':'Cache presence only; not an inference test.'})
        return {'models':values,'directory':str(store.data/'models')}

    @app.post('/api/environment/models/download')
    def download_model(body: ModelDownload):
        if not body.consent:
            raise ValueError('Explicit model-download consent is required.')
        if importlib.util.find_spec('faster_whisper') is None:
            raise ValueError('Install the transcription engine first.')
        with manager.lock:
            manager.check_accepting()
            payload={'tool':'model_download','options':body.model_dump(),'settings':store.settings(),
                     'destination':str(store.data/'models'),'source':None,'url':None,'batch_id':uuid.uuid4().hex}
            job=store.add_job('model_download','Download '+body.model,payload)
        return public(job)

    @app.post('/api/favorites')
    def favorites(body: dict):
        tool=body.get('tool')
        if tool not in TOOLS:raise ValueError('Unknown tool.')
        values=store.get('favorites',[])
        values=[v for v in values if v!=tool]
        if body.get('pinned') is True:values.append(tool)
        store.set('favorites',values)
        return {'favorites':values}

    @app.post('/api/environment/reveal')
    def reveal_installation():
        if os.name!='nt':return {'opened':False,'path':str(ROOT)}
        subprocess.Popen(['explorer.exe',str(ROOT)],**no_window())
        return {'opened':True,'path':str(ROOT)}

    @app.post('/api/environment/install')
    def install(body: dict):
        engine=body.get('engine')
        if engine not in {'transcription','downloads','speakers'}:raise ValueError('Unknown optional engine.')
        if os.name!='nt':return {'started':False,'message':'The guided installer is Windows-only. Installer scripts are in the application folder.'}
        with manager.lock:
            if any(j['status'] in {'running','queued','cancelling','finalizing'} for j in store.jobs(limit=100000)):
                raise ValueError('Finish or cancel queued/active jobs before installing packages.')
            if engine=='speakers':
                command=['powershell.exe','-NoProfile','-NoExit','-ExecutionPolicy','Bypass','-File',str(ROOT/'scripts'/'install_speakers.ps1'),'-WaitForStop','1']
            else:
                command=['powershell.exe','-NoProfile','-NoExit','-ExecutionPolicy','Bypass','-File',str(ROOT/'scripts'/'install_optional.ps1'),'-Engine',engine,'-WaitForStop','1']
            subprocess.Popen(command,cwd=ROOT,creationflags=subprocess.CREATE_NEW_CONSOLE)
            if app.state.shutdown:app.state.shutdown()
        return {'started':True,'message':'FORGE is stopping. Confirm the installation in the Windows console, then reopen FORGE when it completes.'}
