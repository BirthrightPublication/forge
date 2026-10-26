"""Local authenticated speech workspace over the existing queue and data store."""
from pathlib import Path
import copy
import json
import uuid

from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse

from .core import absolute_path, safe_name
from .entitlements import require_feature
from .models import Output
from .queue import public_job
from .speech_options import SpeechOptions, EffectsOptions


def mount(app, store, manager, destination):
    from .speech_store import SpeechStore, RevisionConflict
    from .speech_documents import compile_document, EMOTIONS, READING_STYLES
    from . import speech_engines, audio_effects, bark_asr
    library = SpeechStore(store)
    app.state.speech = library

    @app.exception_handler(RevisionConflict)
    async def revision_conflict(request,exc):
        return JSONResponse({'detail':str(exc)},status_code=409)

    def enqueue(tool, options, body, *, source=None, extra=None):
        require_feature(app,'transcribe' if tool=='speech_install' and options.get('engine')=='bark' else 'tts' if tool=='speech_install' else tool)
        settings = store.settings()
        spec = Output.model_validate(body.get('output') or {'mode':'custom','directory':str(store.data/'speech'/'exports')})
        folder = destination(spec,source,settings)
        payload = {'tool':tool,'options':options,'settings':settings,'destination':str(folder),
                   'source':source,'batch_id':uuid.uuid4().hex,'priority':'normal',**(extra or {})}
        if source:
            stat=Path(source['path']).stat()
            payload['source_stat']={'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
        with manager.lock:
            manager.check_accepting()
            job=store.add_job(tool,options.get('name') or 'Speech production',payload)
        return {'job':public_job(job),'jobs':[public_job(job)]}

    @app.get('/api/speech/state')
    def state():
        require_feature(app,'tts')
        engines=speech_engines.catalog(store.data)
        voices=[]
        for entry in engines:
            voices.extend(speech_engines.voices(store.data,entry['id']))
        return {'engines':engines,'voices':voices,'projects':library.projects(),
                'profiles':library.records('voices'),'emotions':library.records('emotions'),
                'styles':library.records('styles'),'effects':audio_effects.schema(),
                'settings':store.get('speech_settings',{}),'bark':bark_asr.catalog(store.data)}

    @app.get('/api/speech/projects')
    def projects():
        return {'projects':library.projects()}

    @app.get('/api/speech/projects/{project_id}')
    def project(project_id:str):
        return library.project(project_id)

    @app.post('/api/speech/projects')
    def save_project(body:dict):
        require_feature(app,'tts')
        value=copy.deepcopy(body.get('project',body))
        options=value.setdefault('options',{})
        for key in ('engine','voice','master_chain'):
            if key in value:
                options['master_effects' if key=='master_chain' else key]=value.pop(key)
        return {'project':library.save_project(value)}

    @app.get('/api/speech/records/{kind}')
    def records(kind:str):
        return {'records':library.records(kind)}

    @app.post('/api/speech/records/{kind}')
    def save_record(kind:str,body:dict):
        return {'record':library.save_record(kind,body.get('record',body))}

    @app.delete('/api/speech/records/{kind}/{record_id}')
    def delete_record(kind:str,record_id:str):
        library.delete_record(kind,record_id)
        return {'ok':True}

    @app.post('/api/speech/settings')
    def settings(body:dict):
        allowed={'preview_text','default_voice','favorites','pinned','recent','panel_width','panel_collapsed'}
        if set(body)-allowed or len(json.dumps(body))>20000:
            raise ValueError('Unsupported speech settings.')
        with store.lock:
            values=store.get('speech_settings',{})
            values.update(body)
            store.set('speech_settings',values)
        return {'settings':values}

    @app.post('/api/speech/parse')
    def parse(body:dict):
        text=body.get('text','')
        if not isinstance(text,str) or len(text)>500000:
            raise ValueError('A speech document must contain at most 500,000 characters.')
        options=body.get('options',body)
        options=dict(options)
        if isinstance(options.get('style'),str):
            options['style']=library.record('styles',options['style'])
        return compile_document(text,**{k:options[k] for k in ('speaker','emotion','style','pronunciation','pauses') if k in options})

    @app.post('/api/speech/import')
    def import_document(body:dict):
        from .speech_books import import_document
        return import_document(absolute_path(str(body.get('path',''))))

    def generate(body,preview=False):
        value=dict(body.get('options',{}))
        project_id=body.get('project_id') or value.get('project_id')
        snapshot=library.project(project_id) if project_id else None
        if snapshot:
            value={**snapshot.get('options',{}),**value,'project_id':project_id}
            for key in ('speakers','pronunciation','pauses','segment_processing'):
                if key in snapshot and key not in value:
                    value[key]=copy.deepcopy(snapshot[key])
            if 'master_chain' in snapshot and 'master_effects' not in value:
                value['master_effects']=copy.deepcopy(snapshot['master_chain'])
        if 'text' in body:
            value['text']=body['text']
        scope='preview' if preview else body.get('scope',value.get('scope','document'))
        if scope=='export':
            value['assemble_only']=True
            scope='document'
        if scope=='chapters':
            value['chapter_ids']=body.get('chapter_ids',value.get('chapter_ids',[]))
            scope='document'
        if body.get('chapter_id'):
            value['chapter_id']=body['chapter_id']
        value['scope']=scope
        if preview and len(value.get('text',''))>1000:
            raise ValueError('Voice previews are limited to 1,000 characters.')
        options=SpeechOptions.model_validate(value).model_dump()
        profiles={p['id']:p for p in library.records('voices')}
        def expand_voice(value):
            result=copy.deepcopy(value)
            profile=profiles.get(result.get('voice'))
            if profile:
                selected=profile.get('options',profile)
                fields=('engine','voice','blend','model_revision','voice_effects','task','instructions','reference_audio','reference_text','consent','language','speed','attribute_speed','attribute_pitch','attribute_volume','dialect','emotion')
                result.update({k:copy.deepcopy(selected[k]) for k in fields if k in selected})
                for key in ('pitch','volume'):
                    if isinstance(selected.get(key),(int,float)) and not isinstance(selected[key],bool):
                        result[key]=selected[key]
                result['voice_revision']=profile['revision']
                if result['voice']==profile['id']:
                    raise ValueError('Custom voice must retain an original voice ID and blend recipe.')
            return result,profile
        options,profile=expand_voice(options)
        options['speakers']={key:expand_voice(value)[0] for key,value in options['speakers'].items()}
        if snapshot:
            snapshot['speakers']=copy.deepcopy(options['speakers'])
        if isinstance(options['style'],str):
            options['style']=library.record('styles',options['style'])
        expressions={}
        for item in library.records('emotions'):
            if not item.get('revision'):
                continue
            item=copy.deepcopy(item)
            if item.get('method')=='effects' and not item.get('chain'):
                chain=[]
                if item.get('pitch'):
                    chain.append({'type':'pitch','params':{'semitones':float(item['pitch'])}})
                if item.get('speed',1)!=1:
                    chain.append({'type':'pitch','params':{'tempo':float(item['speed'])}})
                if item.get('volume'):
                    chain.append({'type':'gain','params':{'db':float(item['volume'])}})
                item['chain']=chain
            expressions[item['id']]=item
        options['emotion_profiles']={**expressions,**options['emotion_profiles']}
        options=SpeechOptions.model_validate(options).model_dump()
        if not options['text'] and not snapshot:
            raise ValueError('Enter text or select a saved speech project.')
        # Validate marker syntax before persisting a production job.
        compile_document(options['text'],style=options['style'],pronunciation=options['pronunciation'],pauses=options['pauses'])
        return enqueue('tts',options,body,extra={'speech_project':snapshot,'speech_profile':profile})

    @app.post('/api/speech/generate')
    def generate_document(body:dict):
        return generate(body)

    @app.post('/api/speech/preview')
    def preview(body:dict):
        return generate(body,True)

    @app.post('/api/speech/install')
    def install(body:dict):
        if body.get('consent') is not True:
            raise ValueError('Approve this model and environment installation first.')
        engine=body.get('engine','')
        valid={entry['id'] for entry in speech_engines.catalog(store.data)}|{'bark'}
        if engine not in valid:
            raise ValueError('Unknown speech engine.')
        options={k:body[k] for k in ('engine','model','revision','device') if k in body}
        options.update(consent=True,name='Install '+engine)
        return enqueue('speech_install',options,body)

    @app.get('/api/speech/effects-schema')
    def effects_schema():
        return {'effects':audio_effects.schema()}

    @app.post('/api/speech/effects')
    def effects(body:dict):
        source=store.source(body.get('source_id',''))
        options=EffectsOptions.model_validate(body.get('options',{})).model_dump()
        if body.get('preview'):
            options['preview']=True
            seconds=float(body.get('preview_seconds',10))
            if not 0<seconds<=30:
                raise ValueError('Effects previews must be between zero and 30 seconds.')
            measured=audio_effects.waveform(absolute_path(source['path']),store.settings(),max_points=16)
            start=float((options.get('region') or {}).get('start',0))
            options['region']={'start':start,'end':min(measured['duration'],start+seconds)}
        audio_effects.validate_chain(options['chain'])
        return enqueue('audio_effects',options,body,source=source)

    @app.post('/api/speech/waveform')
    def waveform(body:dict):
        source=store.source(body.get('source_id',''))
        return audio_effects.waveform(absolute_path(source['path']),store.settings())

    @app.get('/api/speech/audio')
    def audio(job:str,index:int=0):
        selected=store.job(job)
        if index<0 or index>=len(selected['outputs']):
            raise HTTPException(404,'Audio output is not available.')
        path=Path(selected['outputs'][index]['path'])
        if path.suffix.lower() not in {'.wav','.mp3','.flac','.opus','.m4b','.m4a','.ogg'} or not path.is_file():
            raise HTTPException(404,'Audio output is not available.')
        return FileResponse(path)

    @app.get('/api/speech/source-audio')
    def source_audio(source_id:str):
        source=store.source(source_id)
        path=absolute_path(source['path'])
        if path.suffix.lower() not in {'.wav','.mp3','.flac','.opus','.m4a','.ogg','.m4b'}:
            raise HTTPException(400,'Select playable audio for original comparison.')
        return FileResponse(path)

    @app.post('/api/speech/models/remove')
    def remove_model(body:dict):
        if body.get('consent') is not True:
            raise ValueError('Confirm removing the selected model first.')
        with manager.lock:
            if manager.active:
                raise ValueError('Finish active jobs before removing a speech model.')
            return speech_engines.remove(body.get('engine',''),store.data,revision=body.get('revision',''),consent=True)
