"""Capability-checked local TTS adapters; model weights never enter the application ZIP.

The web service imports this lightweight module without importing torch. Inference
and model installation run in explicitly selected, isolated environments.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from .core import ROOT, json_write, no_window

MING_CODE_REVISION = '028c1eace193e7acdb256453da89b6e9207e052b'
LANGUAGES = {'a': ('en','American'), 'b': ('en','British'), 'e': ('es','Spanish'),
             'f': ('fr','French'), 'h': ('hi','Indian'), 'i': ('it','Italian'),
             'j': ('ja','Japanese'), 'p': ('pt','Brazilian'), 'z': ('zh','Chinese')}
EMOTIONS = {'happy':'高兴','sad':'悲伤','angry':'愤怒','fearful':'恐惧',
            'disgusted':'厌恶','surprised':'惊讶','neutral':'中性'}
EVENTS = ['Breathing','Cough','Sigh','Laughter','Surprise-ah','Surprise-oh',
          'Dissatisfaction-hnn','Uhm','Confirmation-en','Question-ei','Question-ah',
          'Question-en','Question-oh','Question-huh']
MING_TASKS = {
 'IP': ['voice'], 'TTS': ['reference_audio','reference_text','language','consent'],
 'FREEFORM': ['instructions'], 'VOICE_DESIGN': ['instructions'],
 'EMOTION': ['reference_audio','consent','emotion'],
 'BASIC': ['reference_audio','consent','attribute_speed','attribute_pitch','attribute_volume'],
 'MULTILINGUAL': ['reference_audio','reference_text','language','consent'],
 'DIALECT': ['reference_audio','consent','dialect'],
}
SPECS = {
 'kokoro': {'id':'kokoro','label':'Kokoro-82M','repository':'hexgrad/Kokoro-82M',
            'approved_revision':'f3ff3571791e39611d31c381e3a41a3af07b4987',
            'license':'Apache-2.0 (model weights); separate phonemizer/runtime obligations',
            'requirements':'scripts/requirements-speech-kokoro.txt', 'planning_memory_mb':1800,
            'languages':list(dict.fromkeys(v[0] for v in LANGUAGES.values())),
            'tasks':['TTS'], 'capabilities':{'blend':True,'native_emotion':False,'instructions':False,
                        'reference':False,'voice_design':False,'paralinguistic':False,'offline':True}},
 'ming': {'id':'ming','label':'Ming-3.0-tts-0.5b','repository':'inclusionAI/Ming-3.0-tts-0.5b',
          'approved_revision':'8d66b70a00f05ad183fa8a863b4ce609fc83cbc0',
          'license':'MIT upstream code; verify checkpoint LICENSE and NOTICE before distribution',
          'requirements':'scripts/requirements-speech-ming.txt', 'planning_memory_mb':6500,
          'languages':['en','zh','de','es','fr','ko','ja'], 'tasks':list(MING_TASKS),
          'capabilities':{'blend':False,'native_emotion':True,'instructions':True,'reference':True,
                          'voice_design':True,'paralinguistic':True,'offline':True}},
 'ming-tiny': {'id':'ming-tiny','label':'Ming-3.0-tts-tiny (advanced)',
               'repository':'inclusionAI/Ming-3.0-tts-tiny','license':'Review upstream checkpoint notices',
               'languages':['en','zh','de','es','fr','ko','ja'],'tasks':[],
               'planning_memory_mb':22000,'capabilities':{'blend':False,'podcast':False,'offline':True},
               'unavailable_reason':'The official Tiny reference runtime requires Python 3.10.15, CUDA 11.8 and Triton. A verified native Windows runtime is not available; it is not installed automatically.'},
 'qwen3-tts': {'id':'qwen3-tts','label':'Qwen3-TTS (adapter extension)', 'languages':[], 'tasks':[],
               'planning_memory_mb':0,'capabilities':{},
               'unavailable_reason':'No verified isolated runtime is registered in this release.'},
}
REVISION = re.compile(r'^[a-f0-9]{40}$')
VOICE_ID = re.compile(r'^[abefhijpz][fm]_[a-zA-Z0-9_-]{1,80}$')


def _read(path):
    try:
        value=json.loads(Path(path).read_text('utf-8'))
        return value if isinstance(value,dict) else {}
    except (OSError, ValueError):
        return {}


def _root(data):
    return Path(data)/'models'/'speech'


def _selected(data, engine):
    value=_read(_root(data)/'selected.json').get(engine,'')
    return value if isinstance(value,str) and REVISION.fullmatch(value) else ''


def model_path(data, engine, revision=''):
    if engine not in SPECS or engine not in {'kokoro','ming'}:
        raise ValueError('Choose an available speech engine.')
    revision=revision or _selected(data,engine)
    if not REVISION.fullmatch(revision):
        raise RuntimeError('Install and verify the selected speech model first.')
    path=_root(data)/engine/revision
    receipt=_read(path/'forge-receipt.json')
    if receipt.get('revision')!=revision or receipt.get('repository')!=SPECS[engine]['repository'] or not receipt.get('files'):
        raise RuntimeError('The selected speech model has no verified installation receipt. Repair it in Models.')
    return path


def environment_python(data,engine,runtime_revision=''):
    if engine not in {'kokoro','ming'}:
        raise ValueError('No verified runtime for this engine.')
    root=environment_path(data,engine,runtime_revision=runtime_revision)
    return root/('Scripts/python.exe' if os.name=='nt' else 'bin/python')


def environment_path(data,engine,device=None,runtime_revision=''):
    spec=SPECS[engine]
    if runtime_revision:
        if not re.fullmatch('speech-'+re.escape(engine)+r'-[a-f0-9]{16}',runtime_revision):
            raise ValueError('Invalid isolated runtime revision.')
        return Path(data)/'environments'/runtime_revision
    selected=_read(Path(data)/'environments'/'speech-selected.json').get(engine,'') if device is None else ''
    if selected:
        return environment_path(data,engine,runtime_revision=selected)
    device=device or 'cpu'
    if device not in {'cpu','cuda'}:raise ValueError('Install either the CPU or approved CUDA runtime.')
    fingerprint=hashlib.sha256((ROOT/spec['requirements']).read_bytes()+
                              (MING_CODE_REVISION if engine=='ming' else '').encode()+device.encode()).hexdigest()[:16]
    return Path(data)/'environments'/('speech-'+engine+'-'+fingerprint)


def catalog(data):
    result=[]
    for engine,spec in SPECS.items():
        item=copy.deepcopy(spec);revision=_selected(data,engine)
        try:
            path=model_path(data,engine,revision)
            receipt=_read(path/'forge-receipt.json')
        except (ValueError,RuntimeError):
            path=None;receipt={}
        python=environment_python(data,engine) if engine in {'kokoro','ming'} else None
        runtime_ready=bool(python and python.is_file() and _read(python.parent.parent/'forge-runtime.json').get('import_tested'))
        item.update(installed=bool(path and runtime_ready), model_revision=revision,
                    model_installed=bool(path),runtime_installed=runtime_ready,
                    download_bytes=receipt.get('bytes'), artifact_count=len(receipt.get('files',{})),
                    task_controls=MING_TASKS if engine=='ming' else {'TTS':['voice','speed','blend','language']},
                    emotions=list(EMOTIONS) if engine=='ming' else [],
                    paralinguistic_tags=EVENTS if engine=='ming' else [],
                    inference_verified=False, windows_model_verified=False)
        item['runtime_device']=_read(python.parent.parent/'forge-runtime.json').get('device','') if runtime_ready else ''
        item['runtime_revision']=python.parent.parent.name if runtime_ready else ''
        result.append(item)
    return result


def _voice(voice_id,engine,revision='',installed=False,**extra):
    language,accent=LANGUAGES.get(voice_id[:1],('en' if voice_id.startswith(('US_','UK_')) else 'zh','Not specified'))
    return {'id':voice_id,'name':voice_id.split('_',1)[-1].replace('_',' ').title() if engine=='kokoro' else voice_id,
            'engine':engine,'model_revision':revision,'language':language,'accent':accent,
            'gender':'Male' if engine=='kokoro' and voice_id[1:2]=='m' else 'Female' if engine=='kokoro' and voice_id[1:2]=='f' else 'Not specified',
            'pitch':'Not specified','origin':'Not specified','installed':installed,
            'expression':'Audio effects only' if engine=='kokoro' else 'Task-specific native expression',
            **extra}


def _voice_readiness(data,voice):
    runtime=_read(environment_path(data,voice['engine'])/'forge-runtime.json')
    frontend=runtime.get('language_frontends',{})
    language=voice['language']
    available=bool(voice['installed'] and runtime.get('import_tested'))
    reason='Install the isolated model and runtime first.'
    if voice['engine']=='kokoro' and runtime.get('import_tested'):
        if language=='en':
            available &= bool(frontend.get('en'))
            if not frontend.get('en'):reason='Repair the isolated English phonemizer before synthesis.'
        elif language in {'ja','zh'}:
            available &= bool(frontend.get(language))
            if not frontend.get(language):reason='Repair the isolated '+language+' phonemizer before synthesis.'
        elif language!='en':
            supported=bool(frontend.get(language,frontend.get('espeak',False)))
            available &= supported
            if not supported:reason='Repair the isolated language frontend for this voice.'
    voice['synthesis_ready']=available
    voice['availability_reason']='' if available else reason
    return voice


def voices(data,engine=None):
    result=[]
    if engine in (None,'kokoro'):
        revision=_selected(data,'kokoro')
        try:
            path=model_path(data,'kokoro',revision)
            receipt=_read(path/'forge-receipt.json')
            names=sorted(Path(name).stem for name in receipt.get('files',{}) if name.startswith('voices/') and name.endswith('.pt') and VOICE_ID.fullmatch(Path(name).stem))
        except RuntimeError:
            names=['am_onyx'];path=None
        for name in names:
            result.append(_voice_readiness(data,_voice(name,'kokoro',revision,bool(path))))
    if engine in (None,'ming'):
        revision=_selected(data,'ming')
        source=environment_path(data,'ming')/'upstream'/'cookbooks'/'instruction_en.md'
        if source.is_file():
            names=parse_ming_voice_catalog(source.read_text('utf-8'))
        else:
            names=[{'id':n,'gender':'Male' if n in {'US_James','UK_George'} else 'Female'} for n in ['US_Emma','US_James','UK_George','UK_Charlotte']]
        for item in names:
            key=item.pop('id');result.append(_voice_readiness(data,_voice(key,'ming',revision,bool(revision),**item)))
    return result


def parse_ming_voice_catalog(text):
    """Provider-documented IDs only; no identity/ethnicity inferred from audio."""
    voices={};mode='';gender_column=None
    for line in text.splitlines():
        if line.startswith('### '):
            mode='variants' if 'emotion variants' in line and 'without' not in line else 'plain'
        if not line.startswith('|'):continue
        columns=[part.strip() for part in line.strip('|').split('|')]
        if len(columns)<2 or columns[0].startswith((':','-')):continue
        if columns[0].lower() in {'ip','voice','base voice'}:
            gender_column=next((i for i,c in enumerate(columns) if c=='Gender'),None);continue
        if gender_column is None:continue
        gender=columns[gender_column] if len(columns)>gender_column else 'Not specified'
        if mode=='variants':
            names=re.findall(r'`([^`]+)`',columns[-1])
        else:names=[columns[0]]
        for name in names:
            if name and len(name)<=150 and not name.startswith((':','-')):
                voices[name]={'id':name,'gender':gender if gender not in {'-','—'} else 'Not specified',
                              'description':columns[4] if len(columns)>4 else ''}
    return list(voices.values())


def normalize_blend(blend):
    if not isinstance(blend,list) or not 1<=len(blend)<=16:
        raise ValueError('Blend requires between one and sixteen compatible Kokoro voices.')
    seen=set();values=[]
    for entry in blend:
        if not isinstance(entry,dict) or set(entry)-{'voice','weight'}:raise ValueError('Invalid voice blend entry.')
        voice=entry.get('voice','');weight=entry.get('weight')
        if not isinstance(voice,str) or not VOICE_ID.fullmatch(voice) or voice in seen:raise ValueError('Choose distinct original Kokoro voices.')
        if isinstance(weight,bool) or not isinstance(weight,(int,float)) or not math.isfinite(weight) or not 0<=weight<=100:raise ValueError('Voice weights must be finite values from 0 to 100.')
        seen.add(voice);values.append({'voice':voice,'weight':float(weight)})
    languages={LANGUAGES[v['voice'][0]][0] for v in values}
    if len(languages)!=1:raise ValueError('Blend voices must share a compatible language.')
    total=sum(v['weight'] for v in values)
    if total<=0:raise ValueError('At least one voice must have a positive weight.')
    return [{'voice':v['voice'],'weight':v['weight']/total} for v in values]


def validate_options(options):
    if not isinstance(options,dict):raise ValueError('Speech options must be an object.')
    value=copy.deepcopy(options)
    engine=value.get('engine','kokoro');value['engine']=engine
    if engine not in {'kokoro','ming'}:raise ValueError(SPECS.get(engine,{}).get('unavailable_reason','Unknown speech engine.'))
    device=value.get('device','cpu')
    if device not in {'auto','cpu','cuda'}:raise ValueError('Choose Auto, CPU or CUDA.')
    value['device']=device
    revision=value.get('model_revision','')
    if revision and (not isinstance(revision,str) or not REVISION.fullmatch(revision)):raise ValueError('Use an immutable model revision.')
    seed=value.get('seed',1234)
    if isinstance(seed,bool) or not isinstance(seed,int) or not 0<=seed<2**32:raise ValueError('Seed must be an unsigned 32-bit integer.')
    value['seed']=seed
    if engine=='kokoro':
        for field in ['instructions','emotion','reference_audio','reference_text','dialect','attribute_speed','attribute_pitch','attribute_volume']:
            if value.get(field) and not (field=='emotion' and value[field]=='neutral'):raise ValueError(f'Kokoro does not support native {field}; use a separately labeled audio effects chain.')
        value['voice']=value.get('voice','am_onyx');value['task']='TTS'
        if not VOICE_ID.fullmatch(value['voice']):raise ValueError('Choose an original installed Kokoro voice.')
        speed=value.get('speed',1)
        if isinstance(speed,bool) or not isinstance(speed,(int,float)) or not math.isfinite(speed) or not .5<=speed<=2:raise ValueError('Kokoro speed must be between 0.5 and 2.')
        value['speed']=float(speed)
        if value.get('blend'):
            value['blend']=normalize_blend(value['blend'])
            if LANGUAGES[value['blend'][0]['voice'][0]][0]!=LANGUAGES[value['voice'][0]][0]:
                raise ValueError('Selected voice and blended source voices must share the phonemizer language.')
        language=LANGUAGES[value['voice'][0]][0]
        if value.get('language') and value['language']!=language:raise ValueError('Voice language and selected language differ.')
        value['language']=language
    else:
        task=str(value.get('task','IP')).upper()
        task={'VOICE':'IP','BUILTIN':'IP','CLONE':'TTS','DESIGN':'VOICE_DESIGN'}.get(task,task)
        value['task']=task
        if task not in MING_TASKS:raise ValueError('Choose a documented Ming task mode.')
        if value.get('blend'):raise ValueError('Ming voice embeddings cannot be mixed with Kokoro or other engines.')
        if value.get('speed',1)!=1:raise ValueError('Ming speaking attributes require BASIC mode; a numeric Kokoro speed is not supported.')
        controlled={'voice','instructions','emotion','reference_audio','reference_text','dialect','attribute_speed','attribute_pitch','attribute_volume'}
        for field in controlled-set(MING_TASKS[task]):
            if value.get(field) and not (field=='emotion' and value[field]=='neutral'):raise ValueError(f'Ming {task} does not accept {field}.')
        for field in set(MING_TASKS[task])-{'consent','language'}:
            if not isinstance(value.get(field),str) or not value[field].strip():raise ValueError(f'Ming {task} requires {field}.')
        if 'reference_audio' in MING_TASKS[task]:
            if value.get('consent') is not True:raise ValueError('Confirm permission to use this reference voice before synthesis.')
            ref=Path(value['reference_audio']).expanduser()
            if not ref.is_absolute() or ref.suffix.lower() not in {'.wav','.flac','.mp3','.ogg','.m4a'}:raise ValueError('Choose a local reference audio file, not a Python/pickle voice pack.')
        if task=='EMOTION' and value['emotion'] not in EMOTIONS:raise ValueError('Choose a documented native Ming emotion.')
        if task=='BASIC':
            for field,allowed in [('attribute_speed',{'slow','medium','fast'}),('attribute_pitch',{'low','medium','high'}),('attribute_volume',{'low','medium','high'})]:
                if value[field] not in allowed:raise ValueError('Invalid native speech attribute.')
        if task=='DIALECT' and value['dialect'] not in {'chuanyu','cantonese'}:raise ValueError('Choose a documented dialect.')
        if task in {'TTS','MULTILINGUAL'}:
            language=value.get('language','en')
            allowed={'de','es','fr','ko','ja'} if task=='MULTILINGUAL' else {'en','zh'}
            if language not in allowed:raise ValueError('Choose a language compatible with this task mode.')
            value['language']=language
    return value


def resolve_options(options,data):
    value=validate_options(options)
    path=model_path(data,value['engine'],value.get('model_revision',''))
    value['model_revision']=path.name
    runtime=environment_path(data,value['engine'],runtime_revision=value.get('runtime_revision',''))
    value['runtime_revision']=runtime.name
    return value


def synthesize(text,options,data,output,progress=lambda *args,**kwargs:None):
    value=resolve_options(options,data)
    if not isinstance(text,str) or not text.strip() or len(text)>20000:raise ValueError('Supply nonempty speech text of at most 20,000 characters per segment.')
    if '[[' in text or ']]' in text:raise ValueError('Structured editor markers must be parsed before synthesis.')
    tags=re.findall(r'\[([^\[\]]+)\]',text)
    if tags and not (value['engine']=='ming' and value['task']=='TTS' and all(t in EVENTS for t in tags)):
        raise ValueError('This task does not support the supplied inline vocal event tags.')
    python=environment_python(data,value['engine'],value.get('runtime_revision',''))
    if not python.is_file() or not _read(python.parent.parent/'forge-runtime.json').get('import_tested'):raise RuntimeError('Install the isolated speech runtime in Models first.')
    output=Path(output).resolve()
    if output.exists():raise ValueError('Speech output must be a new file.')
    output.parent.mkdir(parents=True,exist_ok=True)
    request=output.with_name(output.name+'.'+uuid.uuid4().hex+'.request.json')
    response=request.with_suffix('.result.json')
    json_write(request,{'text':text,'options':value,'data':str(Path(data).resolve()),'output':str(output),'response':str(response)})
    progress(0,'Synthesizing speech locally',stage='Speech synthesis')
    try:
        result=subprocess.run([str(python),str(ROOT/'scripts'/'speech_runtime.py'),'synthesize','--request',str(request)],
                              capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=3600,
                              env={**os.environ,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','DO_NOT_TRACK':'1','HF_HUB_DISABLE_TELEMETRY':'1'},**no_window())
        metadata=_read(response)
        if result.returncode or not output.is_file() or not metadata.get('success'):
            raise RuntimeError('Local speech synthesis failed. '+metadata.get('error','Check the installed runtime, selected model and device.'))
        return metadata
    finally:
        request.unlink(missing_ok=True);response.unlink(missing_ok=True)


def install(engine,data,consent=False,revision='',progress=lambda *args,**kwargs:None,device='cpu'):
    from .speech_runtime import install_engine
    return install_engine(engine,Path(data),consent=consent,revision=revision,progress=progress,device=device)


def remove(engine,data,revision='',consent=False):
    """Quarantine an explicitly chosen unused model; environments and voices stay recoverable."""
    from .model_cache import cache_lock
    if consent is not True:raise ValueError('Confirm removing this model from the installed catalog.')
    path=model_path(data,engine,revision)
    with cache_lock(_root(data)/('.remove-'+engine+'.lock')):
        destination=_root(data)/'removed'/(engine+'-'+path.name+'-'+uuid.uuid4().hex)
        destination.parent.mkdir(parents=True,exist_ok=True)
        path.rename(destination)
        with cache_lock(_root(data)/'.selected.lock'):
            selected=_read(_root(data)/'selected.json')
            if selected.get(engine)==path.name:selected.pop(engine,None)
            json_write(_root(data)/'selected.json',selected)
    return {'engine':engine,'revision':path.name,'removed':True,'recoverable':True}
