"""Authenticated project editing and staged dubbing over FORGE's existing queue."""
from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .core import MEDIA, VIDEO, absolute_path, executable, json_write, no_window, safe_name
from .dubbing_options import DubbingJobOptions, PIPELINE_STAGES, STAGE_TITLES
from .dubbing_store import (DubbingStore, RevisionConflict, bounded_json, canonical,
                            fingerprint, review_fingerprint)
from .queue import public_job

POST_REVIEW={'dubbing_voice','dubbing_align','dubbing_separate','dubbing_mix','dubbing_effects','dubbing_lipsync','dubbing_encode','dubbing_export'}
ALIASES={name.removeprefix('dubbing_'):name for name in STAGE_TITLES}


def media_info(path,settings):
    binary=executable('ffprobe',settings)
    if not binary: raise ValueError('Audio processing needs FFprobe. Open Updates and install the compatible media utilities.')
    result=subprocess.run([binary,'-v','error','-protocol_whitelist','file,pipe','-show_format','-show_streams','-of','json',str(path)],
                          capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30,**no_window())
    if result.returncode: raise ValueError('The selected media could not be decoded: '+result.stderr[-1000:])
    value=json.loads(result.stdout)
    duration=float(value.get('format',{}).get('duration') or max([float(s.get('duration') or 0) for s in value.get('streams',[])]+[0]))
    if not 0<duration<=48*3600: raise ValueError('Choose playable media with a duration of up to 48 hours.')
    return value,duration


def file_hash(path):
    before=path.stat();digest=hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''): digest.update(chunk)
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise ValueError('The selected media changed during verification. Select it again.')
    return digest.hexdigest()


class DubbingService:
    def __init__(self,app,store,manager,destination):
        self.app,self.store,self.manager,self.destination=app,store,manager,destination
        self.library=DubbingStore(store)
        manager.dubbing=self

    def models(self):
        from . import dubbing_models
        return dubbing_models

    def state(self):
        setup=self.models().setup_state(self.store.data,self.store.settings())
        return {'projects':self.library.projects(),'models':setup.get('models',[]),'setup':setup,
                'voices':self.library.records('voices'),'glossaries':self.library.records('glossaries'),
                'presets':[self.preset(),*self.library.records('presets')],
                'settings':self.store.get('dubbing_settings',{}),'stages':STAGE_TITLES,
                'capabilities':{'local_first':True,'review_required':True,'timed_asr':'whisper',
                                'voice_reference_required':True,'automatic_public_upload':False,
                                'physical_gpu_tested':False,'lip_sync_optional':True}}

    @staticmethod
    def preset():
        from .dubbing_options import DubbingOptions
        return {'id':'builtin-russian-commentary','name':'Russian Commentary','builtin':True,
                'mode':'localized','options':DubbingOptions().model_dump()}

    def import_video(self,body):
        source=self.store.source(body['source_id']) if body.get('source_id') else self.store.register(absolute_path(str(body.get('path',''))))
        path=absolute_path(source['path'])
        if path.suffix.lower() not in VIDEO: raise ValueError('Choose a local video for Dubbing Studio.')
        info,duration=media_info(path,self.store.settings())
        videos=[s for s in info.get('streams',[]) if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic')]
        if not videos or not any(s.get('codec_type')=='audio' for s in info.get('streams',[])):
            raise ValueError('Dubbing requires a video with a playable source-audio stream.')
        video=videos[0];stat=path.stat()
        source={**source,'path':str(path),'size':stat.st_size,'mtime_ns':stat.st_mtime_ns,
                'duration':duration,'width':video.get('width'),'height':video.get('height'),
                'fps':video.get('avg_frame_rate') or video.get('r_frame_rate'),
                'metadata':{key:str(value)[:5000] for key,value in info.get('format',{}).get('tags',{}).items()
                            if key.lower() in {'title','description','comment','artist','date','copyright'}}}
        value=self.library.save_project({'name':str(body.get('name') or path.stem+' · Russian')[:200],
                                        'source':source,'source_metadata':source['metadata'],'mode':'localized'},trusted=True)
        return {'project':self.library.project(value['id'])}

    def _project(self,identity):
        # Worker snapshots exclude UI history and duplicated artifact inventories.
        with self.store.lock: return self.library._project(identity)

    def _stage_list(self,body,project):
        stages=body.get('stages')
        if stages is None:
            stages=list(PIPELINE_STAGES)
            audio=project['options']['audio'];lip=project['options']['lip_sync']
            if audio.get('separation')!='off': stages.insert(stages.index('dubbing_mix'),'dubbing_separate')
            if lip.get('mode')!='off': stages.insert(stages.index('dubbing_encode'),'dubbing_lipsync')
        if not isinstance(stages,list) or not 1<=len(stages)<=24: raise ValueError('Select a bounded linear dubbing workflow.')
        stages=[ALIASES.get(str(stage),str(stage)) for stage in stages]
        if len(stages)!=len(set(stages)) or any(stage not in STAGE_TITLES or stage in {'dubbing_install','dubbing_model_test','dubbing_voice_reference'} for stage in stages):
            raise ValueError('Use registered production stages once in their linear order.')
        if 'dubbing_voice' in stages and 'dubbing_review' in stages and stages.index('dubbing_voice')<stages.index('dubbing_review'):
            raise ValueError('Review translated text before generating its final narration.')
        return stages

    def _check_source(self,project):
        source=project.get('source',{});path=absolute_path(source.get('path',''));stat=path.stat()
        if stat.st_size!=source.get('size') or stat.st_mtime_ns!=source.get('mtime_ns'):
            raise ValueError('The original video changed after import. Import it as a new project; existing work is retained.')
        return path

    def _selection(self,project,body):
        selected=body.get('segment_ids')
        if selected is None: return None
        if not isinstance(selected,list) or not selected or len(selected)>10000 or len(set(selected))!=len(selected):
            raise ValueError('Select one or more unique saved speech segments.')
        known={row['id'] for row in project.get('segments',[])}
        if not set(selected)<=known: raise ValueError('Select segments belonging to this saved project.')
        return selected

    def _voices(self,project):
        ids={project['options']['tts'].get('voice_profile_id')}
        ids.update(value.get('voice_profile_id') for value in project.get('speakers',{}).values() if isinstance(value,dict))
        ids.update(value.get('voice_profile_id') for value in project.get('segments',[]))
        return {identity:self.library.record('voices',identity) for identity in ids if identity}

    def frozen_inputs(self,project):
        profiles=self._voices(project)
        glossary=self.library.record('glossaries',project['glossary_id']) if project.get('glossary_id') else {'entries':{}}
        models={item['id']:item.get('model_revision','') for item in self.models().catalog(self.store.data) if item.get('ready')}
        asr=project['options']['asr']
        existing=self.models().existing_whisper_state(self.store.data,asr['model'])
        if existing.get('ready') and existing.get('model_revision'):
            models.setdefault('whisper-'+asr['model'],existing['model_revision'])
        return {'voice_profiles':profiles,'glossary':glossary,'model_revisions':models}

    def _context(self,project,run_id,segment_ids=None,*,approved=False,preview=False,inputs=None):
        self._check_source(project)
        public=self.library.project(project['id'])
        artifacts=[item for item in public['artifacts'] if not item.get('stale')]
        # Producers select the latest compatible kind from the end.
        artifacts.sort(key=lambda item:item.get('created',0))
        inputs=copy.deepcopy(inputs if inputs is not None else self.frozen_inputs(project))
        snapshot=copy.deepcopy(project)
        if preview:
            for row in snapshot['segments']:
                if segment_ids is None or row['id'] in segment_ids: row['reviewed']=True
        return {'project':snapshot,'run_id':run_id,'segment_ids':segment_ids,'artifacts':artifacts,
                'voice_profiles':inputs['voice_profiles'],'glossary':inputs['glossary'],'model_revisions':inputs['model_revisions'],'review_approved':approved,
                'preview_approved':preview,'physical_gpu_tested':False}

    def plan(self,body):
        project=self._project(body.get('project_id',''));self._check_source(project)
        stages=self._stage_list(body,project);selected=self._selection(project,body)
        if 'dubbing_lipsync' in stages:
            from .dubbing_visual import validate_options
            validate_options(project['options']['lip_sync'])
        token=secrets.token_hex(32)
        value={'project_id':project['id'],'revision':project['revision'],'project_hash':fingerprint(project),
               'stages':stages,'segment_ids':selected,'preview':body.get('preview') is True,
               'replace_manual_text':body.get('replace_manual_text') is True,'created':time.time(),'expires':time.time()+3600}
        value['frozen_inputs']=self.frozen_inputs(project)
        with self.library.transaction():
            plans=self.store.get('dubbing_plans',{})
            plans={key:item for key,item in plans.items() if item.get('expires',0)>time.time()}
            if len(plans)>=64: plans=dict(list(plans.items())[-63:])
            plans[token]=value
            self.store.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',('dubbing_plans',canonical(plans)))
        return {'token':token,'plan_token':token,'plan':{**{key:item for key,item in value.items() if key!='frozen_inputs'},'name':project['name'],
                'steps':[{'stage':stage,'title':STAGE_TITLES[stage],'review_gate':stage=='dubbing_review'} for stage in stages]},
                'review_required':True,'automatic_upload':False}

    def _ready(self,project,stages,*,preview=False):
        catalog={item['id']:item for item in self.models().catalog(self.store.data)}
        needs=[]
        original_metadata=project.get('source_metadata') or project.get('source',{}).get('metadata') or {}
        if 'dubbing_translate' in stages or ('dubbing_metadata' in stages and any(original_metadata.get(key) for key in ('title','description','tags','hashtags','chapters','chapter_headings'))):
            needs.append(project['options']['translation']['engine'])
        profiles=self._voices(project) if 'dubbing_voice' in stages else {}
        if 'dubbing_voice' in stages:
            if not profiles: raise ValueError('Prepare and approve a permitted voice reference with Use My Voice first.')
            needs.extend(profile.get('model') or profile.get('engine') or project['options']['tts']['engine'] for profile in profiles.values())
        if 'dubbing_lipsync' in stages and project['options']['lip_sync']['mode']!='off': needs.append(project['options']['lip_sync']['engine'])
        if 'dubbing_separate' in stages and project['options']['audio'].get('separation')=='hdemucs': needs.append('hdemucs-high')
        missing=[identity for identity in needs if not catalog.get(identity,{}).get('ready')]
        if missing: raise ValueError('Set Up Dubbing Studio first: install or repair '+', '.join(missing)+'. Existing engines and projects are preserved.')
        if 'dubbing_transcribe' in stages:
            model=project['options']['asr']['model']
            if not self.models().existing_whisper_state(self.store.data,model).get('ready') and not catalog.get('whisper-'+model,{}).get('ready'):
                raise ValueError('Speech recognition needs a compatible installed Whisper model. Choose Install Recommended or Repair in Dubbing Studio.')
        if 'dubbing_voice' in stages:
            if any(p.get('consent') is not True or (p.get('approved') is not True and not preview) for p in profiles.values()):
                raise ValueError('Listen to and approve the selected permitted voice references first.')

    def enqueue(self,project,stages,*,segment_ids=None,preview=False,replace_manual_text=False,run_id=None,frozen_inputs=None,stage_options=None,workflow_name=None):
        self.manager.check_accepting()
        self._ready(project,stages,preview=preview)
        identity=run_id or uuid.uuid4().hex
        reviewed=bool(project.get('segments')) and all(row.get('reviewed') and row.get('text','').strip() for row in project['segments'])
        approved=reviewed and 'dubbing_review' not in stages and not preview
        production_stages=[stage for stage in stages if not (stage=='dubbing_export' and (stage_options or {}).get(stage,{}).get('formats')==['project'])]
        if any(stage in POST_REVIEW for stage in production_stages) and 'dubbing_review' not in stages and not approved and not preview:
            raise ValueError('Review the Russian script before starting final narration or rendering. Preview does not approve the production script.')
        now=time.time();jobs=[];previous='';settings=self.store.settings()
        run={'id':identity,'project_id':project['id'],'snapshot':project,'stages':stages,'segment_ids':segment_ids,
             'preview':preview,'status':'queued','jobs':[],'created':now,'review_approved':approved,
             'approved_hash':review_fingerprint(project) if approved else '', 'replace_manual_text':replace_manual_text}
        run['stage_options']=copy.deepcopy(stage_options or {})
        inputs=copy.deepcopy(frozen_inputs if frozen_inputs is not None else self.frozen_inputs(project))
        context=self._context(project,identity,segment_ids,approved=approved,preview=preview,inputs=inputs)
        run['frozen_inputs']=inputs
        run['approved_voice_revisions']={key:value.get('revision') for key,value in inputs['voice_profiles'].items()} if approved else {}
        with self.manager.lock,self.library.transaction():
            self.manager.check_accepting()
            if self._project(project['id'])['revision']!=project['revision']:
                raise RevisionConflict('The dubbing project changed while preparing its workflow. Review the new version first.')
            for index,stage in enumerate(stages,1):
                job_id=uuid.uuid4().hex
                output=absolute_path(project['options']['export']['directory'],directory=True,must_exist=False) if stage=='dubbing_export' and project['options']['export']['directory'] else self.store.data/'dubbing'/'projects'/project['id']/'runs'/identity/stage
                if not output.is_absolute(): raise ValueError('Choose a complete local export directory.')
                options=DubbingJobOptions(project_id=project['id'],run_id=identity,stage=stage,segment_ids=segment_ids,
                    preview=preview,replace_manual_text=replace_manual_text,name=STAGE_TITLES[stage]).model_dump()
                options=DubbingJobOptions.model_validate({**options,**(stage_options or {}).get(stage,{}),
                    'project_id':project['id'],'run_id':identity,'stage':stage}).model_dump()
                payload={'job_id':job_id,'tool':stage,'options':options,'source':project['source'],
                         'source_stat':{key:project['source'][key] for key in ('size','mtime_ns')},
                         'destination':str(output.resolve()),'settings':settings,'dubbing':context,
                         'depends_on':previous,'batch_id':identity,'workflow_name':workflow_name or project['name'],'step':index,'steps':len(stages),'priority':'normal'}
                message='Awaiting translation review.' if stage=='dubbing_review' else 'Waiting for the previous dubbing stage.' if previous else 'Waiting for processing capacity.'
                self.store.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (job_id,stage,project['name']+' · '+STAGE_TITLES[stage],'queued',0,message,canonical(payload),'[]','[]',now,now))
                meta={'batch_id':identity,'workflow_name':project['name'],'depends_on':previous,'step':index,'steps':len(stages),
                      'dubbing_project_id':project['id'],'dubbing_run_id':identity,'stage':'Translation review' if stage=='dubbing_review' else 'Awaiting dispatch'}
                self.store.db.execute('INSERT INTO job_meta VALUES (?,?)',(job_id,canonical(meta)))
                run['jobs'].append(job_id);jobs.append(job_id);previous=job_id
            self.library.save_run(run)
        return {'run':self.library.run(identity),'jobs':[public_job(self.store.job(j)) for j in jobs]}

    def black_preview(self,script,active,plans):
        if any(not node.tool.startswith('dubbing_') and node.tool!='audio_effects' for node in active):
            raise ValueError('A dubbing script uses one linear set of dubbing stages and optional Audio Effects. Run other media operations as a separate script.')
        identities={node.options.get('project_id') for node in active if node.tool.startswith('dubbing_')}
        if len(identities)!=1 or not next(iter(identities),None):
            raise ValueError('Choose the same saved Dubbing Studio project for this linear production script.')
        if len(plans)!=1: raise ValueError('Choose the original video of one saved dubbing project for this script.')
        project=self._project(next(iter(identities)))
        if Path(plans[0]['source']['path']).resolve()!=Path(project['source']['path']).resolve():
            raise ValueError('The Black Scripting source must be this dubbing project’s original video.')
        snapshot={'project_id':project['id'],'revision':project['revision'],'project_hash':fingerprint(project),
                  'inputs_hash':fingerprint(self.frozen_inputs(project))}
        plans[0]['dubbing_snapshot']=snapshot
        return snapshot

    def black_run(self,script,active,plans):
        self.black_preview(script,active,plans)
        snapshot=plans[0]['dubbing_snapshot'];project=self._project(snapshot['project_id'])
        stages=[];overrides={}
        for node in active:
            if node.tool=='audio_effects':
                from .audio_effects import validate_chain
                chain=node.options.get('chain',[]);validate_chain(chain)
                stage='dubbing_effects';values={'effects':chain}
            else:
                stage=node.tool;values={key:value for key,value in node.options.items()
                                       if key in {'segment_ids','effects','replace_manual_text'}}
            if stage in stages: raise ValueError('Use each persistent dubbing stage once per linear workflow.')
            if stage in {'dubbing_install','dubbing_model_test','dubbing_voice_reference'}:
                raise ValueError('Set up model components and permitted references in Dubbing Studio before production.')
            if values.get('segment_ids') is not None: self._selection(project,values)
            stages.append(stage);overrides[stage]=values
        target=plans[0]['destination']
        if project['options']['export']['directory']!=target:
            project['options']['export']['directory']=target
            project=self.library.save_project(project,project['revision'],trusted=True)
        result=self.enqueue(project,stages,stage_options=overrides,workflow_name=script.name)
        result['batch_id']=result['run']['id']
        return result

    def start(self,body):
        token=body.get('plan_token') or body.get('token')
        with self.manager.lock,self.library.transaction():
            plans=self.store.get('dubbing_plans',{});plan=plans.get(token)
            if not plan or plan.get('expires',0)<time.time(): raise ValueError('Preview the current dubbing workflow before starting it.')
            if plan.get('run_id'):
                run=self.library.run(plan['run_id'])
                return {'run':run,'jobs':[public_job(self.store.job(j)) for j in run['jobs']]}
            project=self._project(plan['project_id'])
            if fingerprint(project)!=plan['project_hash']: raise RevisionConflict('The project changed after the workflow preview. Preview it again.')
            current_inputs=self.frozen_inputs(project)
            if fingerprint(current_inputs)!=fingerprint(plan['frozen_inputs']):
                raise RevisionConflict('A voice, glossary or model changed after the workflow preview. Preview the current inputs first.')
            result=self.enqueue(project,plan['stages'],segment_ids=plan['segment_ids'],preview=plan['preview'],replace_manual_text=plan['replace_manual_text'],frozen_inputs=plan['frozen_inputs'])
            plan['run_id']=result['run']['id'];plans[token]=plan
            self.store.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',('dubbing_plans',canonical(plans)))
        return result

    def prepare_job(self,job):
        if not job['tool'].startswith('dubbing_'): return None
        if job['tool'] in {'dubbing_install','dubbing_model_test','dubbing_voice_reference'}:
            parent_id=job['payload'].get('depends_on')
            if not parent_id: return True
            parent=self.store.job(parent_id)
            if parent['status'] in {'failed','cancelled','blocked','interrupted'}:
                self.store.update_job(job['id'],status='blocked',message='The preceding approved component installation needs review or retry.')
                return False
            return parent['status']=='completed'
        payload=job['payload'];run_id=payload.get('options',{}).get('run_id') or payload.get('dubbing',{}).get('run_id')
        if not run_id:
            self.store.update_job(job['id'],status='blocked',message='Open the dubbing project and prepare its reviewed linear workflow first.')
            return False
        run=self.library.run(run_id);project=self._project(run['project_id'])
        parent_id=payload.get('depends_on')
        if parent_id:
            parent=self.store.job(parent_id)
            if parent['status'] in {'failed','cancelled','blocked','interrupted'}:
                self.store.update_job(job['id'],status='blocked',message='An earlier dubbing stage needs review or retry. Verified completed work was retained.')
                return False
            if parent['status']!='completed': return False
        snapshot=run.get('snapshot',project)
        def production_options(value):
            return {key:item for key,item in value.get('options',{}).items() if key not in {'panels','publishing'}}
        if canonical(production_options(snapshot))!=canonical(production_options(project)):
            message='Production settings changed after approval. Review and plan the updated workflow; completed work is retained.'
            if job.get('message')!=message: self.store.update_job(job['id'],message=message)
            return False
        live_voices=self._voices(project)
        profiles_match={key:value.get('revision') for key,value in live_voices.items()}==run.get('approved_voice_revisions',{})
        approved=run.get('review_approved') and run.get('approved_hash')==review_fingerprint(project) and profiles_match
        if job['tool']=='dubbing_review':
            if approved:
                self.store.update_job(job['id'],status='completed',progress=100,message='Russian script reviewed; approved snapshot retained.')
            elif job.get('message')!='Review and approve the Russian translation in Dubbing Studio.':
                self.store.update_job(job['id'],message='Review and approve the Russian translation in Dubbing Studio.')
            return False
        backup_only=job['tool']=='dubbing_export' and run.get('stage_options',{}).get(job['tool'],{}).get('formats')==['project']
        if job['tool'] in POST_REVIEW and not backup_only and not run.get('preview') and not approved:
            message='The script or voice changed. Review the current Russian translation before continuing.'
            if job.get('message')!=message: self.store.update_job(job['id'],message=message)
            return False
        current=copy.deepcopy(snapshot)
        overrides=run.get('stage_options',{}).get(job['tool'],{})
        selected=overrides.get('segment_ids',run.get('segment_ids'))
        context=self._context(current,run_id,selected,approved=approved,preview=run.get('preview') is True,inputs=run.get('frozen_inputs'))
        context['replace_manual_text']=overrides.get('replace_manual_text',run.get('replace_manual_text')) is True
        source_stat={key:current['source'][key] for key in ('size','mtime_ns')}
        if canonical(payload.get('dubbing',{}))!=canonical(context) or payload.get('source_stat')!=source_stat:
            payload['dubbing']=context;payload['source']=current['source'];payload['source_stat']=source_stat
            self.store.update_payload(job['id'],payload)
        return True

    def review(self,body):
        if body.get('approve') is not True: raise ValueError('Approve the reviewed Russian script explicitly.')
        with self.manager.lock,self.library.transaction():
            project=self._project(body.get('project_id',''))
            if body.get('expected_revision')!=project['revision']: raise RevisionConflict('Reload the current translated script before approving it.')
            selected=self._selection(project,body)
            rows=[row for row in project['segments'] if selected is None or row['id'] in selected]
            if not rows or any(not row.get('text','').strip() for row in rows): raise ValueError('Review nonempty Russian text before approving it.')
            for row in rows: row['reviewed']=True
            project=self.library.save_project(project,project['revision'],trusted=True)
            complete=all(row.get('reviewed') and row.get('text','').strip() for row in project['segments'])
            run_id=body.get('run_id')
            candidates=[self.library.run(run_id)] if run_id else [json.loads(row[0]) for row in self.store.db.execute('SELECT data FROM dubbing_runs WHERE project_id=?',(project['id'],))]
            for run in candidates:
                if run['project_id']!=project['id']: raise ValueError('Choose a workflow from this dubbing project.')
                run.update(review_approved=complete,approved_hash=review_fingerprint(project) if complete else '',snapshot=project,status='approved' if complete else 'review_required')
                if complete:
                    run['frozen_inputs']=self.frozen_inputs(project)
                    run['approved_voice_revisions']={key:value.get('revision') for key,value in run['frozen_inputs']['voice_profiles'].items()}
                run.setdefault('review_history',[]).append({'revision':project['revision'],'time':time.time(),'segment_ids':selected,'complete':complete})
                self.library.save_run(run)
        return {'project':self.library.project(project['id']),'approved':complete}

    def simple_job(self,tool,options,*,source=None,extra=None,folder=None):
        values=DubbingJobOptions.model_validate(options).model_dump()
        dest=folder or self.store.data/'dubbing'/'reports'/uuid.uuid4().hex
        payload={'tool':tool,'options':values,'settings':self.store.settings(),'destination':str(dest),
                 'source':source,'batch_id':uuid.uuid4().hex,**(extra or {})}
        if source:
            stat=Path(source['path']).stat();payload['source_stat']={'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
        with self.manager.lock:
            self.manager.check_accepting();job=self.store.add_job(tool,values.get('name') or STAGE_TITLES[tool],payload)
        return {'job':public_job(job),'jobs':[public_job(job)]}

    def setup_plan(self,body):
        components=body.get('components')
        if not components: components=self.models().setup_state(self.store.data,self.store.settings()).get('missing_recommended',[])
        if not components: return {'plan':{'components':[],'download_bytes':0,'required_storage_bytes':0,'destination':str(self.store.data/'dubbing')},'ready':True,'token':None}
        action='repair' if body.get('repair') is True else body.get('action','install')
        try:
            quote=self.models().installation_plan(components,self.store.data,device=body.get('device','auto'),settings=self.store.settings(),action=action)
        except RuntimeError as error: raise ValueError('Dubbing setup cannot prepare a verified download plan: '+str(error)) from error
        token=secrets.token_hex(32)
        with self.library.transaction():
            plans=self.store.get('dubbing_setup_plans',{})
            plans={key:value for key,value in plans.items() if value.get('expires',0)>time.time()}
            if len(plans)>31: plans=dict(list(plans.items())[-31:])
            plans[token]=quote
            self.store.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',('dubbing_setup_plans',json.dumps(plans,ensure_ascii=False,allow_nan=False)))
        return {'plan':quote,'token':token,'approval_required':True}

    def setup_install(self,body):
        if body.get('consent') is not True: raise ValueError('Review and approve the quoted components, size and destination first.')
        token=body.get('token') or body.get('plan_token')
        with self.manager.lock,self.library.transaction():
            plans=self.store.get('dubbing_setup_plans',{});quote=plans.get(token)
            if not quote or quote.get('expires',0)<time.time(): raise ValueError('Prepare a fresh compatible installation quote before approving downloads.')
            if quote.get('jobs'): return {'jobs':[public_job(self.store.job(j)) for j in quote['jobs']]}
            self.manager.check_accepting();now=time.time();previous='';jobs=[]
            for index,component in enumerate(quote['components'],1):
                identity=uuid.uuid4().hex
                options=DubbingJobOptions(component=component['component'],consent=True,action=quote['action'],device=component['device'],name=component['label']).model_dump()
                payload={'job_id':identity,'tool':'dubbing_install','options':options,'dubbing_install_plan':quote,
                         'destination':str(self.store.data/'dubbing'/'setup-reports'/token/component['component']),
                         'settings':self.store.settings(),'depends_on':previous,'batch_id':token,'step':index,'steps':len(quote['components'])}
                self.store.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)',(identity,'dubbing_install','Install '+component['label'],'queued',0,'Approved compatible component installation.',json.dumps(payload,ensure_ascii=False,allow_nan=False),'[]','[]',now,now))
                self.store.db.execute('INSERT INTO job_meta VALUES (?,?)',(identity,canonical({'depends_on':previous,'batch_id':token,'step':index,'steps':len(quote['components'])})))
                jobs.append(identity);previous=identity
            # Keep the immutable quote in each job; job IDs are outside its plan hash.
            quote={**quote,'jobs':jobs};plans[token]=quote
            self.store.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',('dubbing_setup_plans',json.dumps(plans,ensure_ascii=False,allow_nan=False)))
        return {'jobs':[public_job(self.store.job(j)) for j in jobs]}


def mount(app,store,manager,destination):
    service=DubbingService(app,store,manager,destination)
    app.state.dubbing=service
    library=service.library

    @app.exception_handler(RevisionConflict)
    async def revision_conflict(request,error): return JSONResponse({'detail':str(error)},status_code=409)

    @app.get('/api/dubbing/state')
    def state(): return service.state()

    @app.get('/api/dubbing/projects')
    def projects(): return {'projects':library.projects()}

    @app.get('/api/dubbing/projects/{identity}')
    def project(identity:str): return library.project(identity)

    @app.post('/api/dubbing/projects')
    def save_project(body:dict):
        value=body.get('project',body)
        saved=library.save_project(value,body.get('expected_revision'))
        return {'project':library.project(saved['id'])}

    @app.post('/api/dubbing/import')
    def import_video(body:dict): return service.import_video(body)

    @app.get('/api/dubbing/records/{kind}')
    def records(kind:str): return {'records':library.records(kind)}

    @app.post('/api/dubbing/records/{kind}')
    def save_record(kind:str,body:dict): return {'record':library.save_record(kind,body.get('record',body),body.get('expected_revision'))}

    @app.delete('/api/dubbing/records/{kind}/{identity}')
    def delete_record(kind:str,identity:str):
        library.delete_record(kind,identity);return {'ok':True}

    @app.post('/api/dubbing/settings')
    def settings(body:dict):
        allowed={'last_project','last_panel','default_voice','default_glossary','panel_widths'}
        if set(body)-allowed: raise ValueError('Unsupported dubbing preference.')
        bounded_json(body,20000)
        with store.lock:
            value=store.get('dubbing_settings',{});value.update(body);store.set('dubbing_settings',value)
        return {'settings':value}

    @app.post('/api/dubbing/plan')
    def plan(body:dict): return service.plan(body)

    @app.post('/api/dubbing/run')
    def run(body:dict): return service.start(body)

    @app.post('/api/dubbing/stage')
    def stage(body:dict):
        allowed={'project_id','stage','segment_ids','preview','replace_manual_text','formats','include_source','preview_start','preview_seconds'}
        if set(body)-allowed: raise ValueError('Unsupported dubbing stage options.')
        tool=ALIASES.get(str(body.get('stage','')),body.get('stage',''))
        if tool not in STAGE_TITLES or tool in {'dubbing_install','dubbing_model_test','dubbing_voice_reference','dubbing_review'}:
            raise ValueError('Choose a registered dubbing production stage.')
        project=service._project(body.get('project_id',''))
        selected=service._selection(project,body)
        preview=body.get('preview') is True
        if preview and tool not in {'dubbing_voice','dubbing_lipsync','dubbing_align','dubbing_mix','dubbing_encode'}:
            raise ValueError('Choose a supported bounded preview operation.')
        overrides={key:body[key] for key in ('formats','include_source','preview_start','preview_seconds') if key in body}
        if any(key in overrides for key in ('formats','include_source')) and tool!='dubbing_export':
            raise ValueError('Export formats apply only to the export stage.')
        checked=DubbingJobOptions.model_validate(overrides)
        if tool=='dubbing_lipsync' and preview and not 5<=checked.preview_seconds<=10:
            raise ValueError('Lip-sync previews must be between five and ten seconds.')
        result=service.enqueue(project,[tool],segment_ids=selected,preview=preview,replace_manual_text=body.get('replace_manual_text') is True,
                               stage_options={tool:overrides})
        return result

    @app.post('/api/dubbing/review')
    def review(body:dict): return service.review(body)

    @app.post('/api/dubbing/setup/plan')
    def setup_plan(body:dict): return service.setup_plan(body)

    @app.post('/api/dubbing/setup/install')
    def setup_install(body:dict): return service.setup_install(body)

    @app.post('/api/dubbing/models/action')
    def model_action(body:dict):
        action=body.get('action');component=body.get('component','')
        if action=='repair': return service.setup_plan({'components':[component],'action':'repair','device':body.get('device','auto')})
        if action not in {'verify','test','compatibility','remove_unused','move'}: raise ValueError('Choose a supported model-management action.')
        if action in {'remove_unused','move'}:
            if body.get('consent') is not True: raise ValueError('Approve the selected model-storage change first.')
            with manager.lock:
                if manager.active: raise ValueError('Finish active processing before moving or removing managed model files.')
        options={key:body[key] for key in ('component','device','destination','consent','voice_profile_id') if key in body}
        options.update(action=action,name=action.replace('_',' ').title()+' '+component)
        extra={}
        if body.get('voice_profile_id'):
            profile=library.record('voices',body['voice_profile_id'])
            if not profile.get('consent') or not profile.get('approved'): raise ValueError('Approve the prepared reference before testing its voice.')
            extra['dubbing_voice_profile']=profile
        return service.simple_job('dubbing_model_test',options,extra=extra)

    @app.post('/api/dubbing/voices/prepare')
    def prepare_voice(body:dict):
        if body.get('consent') is not True: raise ValueError('Confirm that you own or have permission to use this identifiable voice reference.')
        source=store.source(body['source_id']) if body.get('source_id') else store.register(absolute_path(str(body.get('path',''))))
        if Path(source['path']).suffix.lower() not in MEDIA: raise ValueError('Choose playable local speech audio or video.')
        media_info(Path(source['path']),store.settings())
        identity=uuid.uuid4().hex
        options={key:body[key] for key in ('name','description','engine','reference_text','reference_start','reference_end') if key in body}
        options.update(voice_profile_id=identity,consent=True,name=options.get('name') or 'My Voice')
        context={'project':{'id':'voice-reference-'+identity,'name':options['name'],'options':{},'segments':[]},'voice_consent':True}
        return service.simple_job('dubbing_voice_reference',options,source=source,extra={'dubbing':context},folder=store.data/'dubbing'/'voices'/identity)

    @app.post('/api/dubbing/voices/{identity}/approve')
    def approve_voice(identity:str,body:dict):
        if body.get('consent') is not True: raise ValueError('Approve this permitted voice reference explicitly.')
        profile=library.record('voices',identity)
        if body.get('expected_revision')!=profile['revision']: raise RevisionConflict('The voice reference changed. Review the current sample first.')
        reference=profile.get('prepared_reference') or {};path=absolute_path(reference.get('path',''))
        if file_hash(path)!=reference.get('sha256'): raise ValueError('The prepared reference changed. Prepare and listen to it again.')
        profile['approved']=True
        return {'record':library.save_record('voices',profile,profile['revision'])}

    @app.get('/api/dubbing/voices/{identity}/audio')
    def voice_audio(identity:str,kind:str='reference'):
        profile=library.record('voices',identity)
        if not profile.get('consent'): raise HTTPException(403,'Voice permission is required.')
        value=profile.get('prepared_reference' if kind=='reference' else 'sample',{})
        if kind not in {'reference','sample'} or not value: raise HTTPException(404,'The selected voice sample is not available yet.')
        return FileResponse(absolute_path(value.get('path','')))

    @app.post('/api/dubbing/stems/import')
    def import_stem(body:dict):
        project=service._project(body.get('project_id',''));kind=body.get('kind')
        if kind not in {'background','music','vocals'}: raise ValueError('Choose a background, music or isolated-vocals stem.')
        path=absolute_path(str(body.get('path','')))
        if path.suffix.lower() not in MEDIA-VIDEO: raise ValueError('Choose a playable local audio stem.')
        info,duration=media_info(path,store.settings())
        if not any(row.get('codec_type')=='audio' for row in info.get('streams',[])): raise ValueError('This stem has no audio stream.')
        artifact=library.add_artifact(project['id'],{'kind':kind,'path':str(path),'sha256':file_hash(path),'duration':duration,'user_selected':True})
        return {'artifact':artifact,'project':library.project(project['id'])}

    def saved_media(project_id,artifact_id):
        project=service._project(project_id)
        if artifact_id=='source': return service._check_source(project)
        artifact=library.artifact(project_id,artifact_id)
        raw=Path(artifact['path'])
        if raw.is_symlink(): raise ValueError('The saved media was replaced. Render or import it again.')
        path=absolute_path(str(raw));stat=path.stat()
        evidence=artifact.get('source_stat',{})
        if evidence and (stat.st_size!=evidence.get('size') or stat.st_mtime_ns!=evidence.get('mtime_ns')):
            raise ValueError('The saved media changed after verification. Render or import it again.')
        return path

    @app.get('/api/dubbing/media')
    def media(project_id:str,artifact_id:str='source'):
        path=saved_media(project_id,artifact_id)
        return FileResponse(path,media_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')

    @app.post('/api/dubbing/waveform')
    def waveform(body:dict):
        path=saved_media(body.get('project_id',''),body.get('artifact_id','source'))
        points=body.get('max_points',800)
        if type(points) is not int or not 16<=points<=1600: raise ValueError('Choose between 16 and 1,600 waveform points.')
        stat=path.stat();key=fingerprint({'path':str(path),'size':stat.st_size,'mtime':stat.st_mtime_ns,'points':points})
        cache=store.data/'dubbing'/'waveforms'/(key+'.json')
        if cache.is_file(): return json.loads(cache.read_text('utf-8'))
        from .dubbing_media import waveform as measure_waveform
        result=measure_waveform(path,store.settings(),max_points=points)
        json_write(cache,result);return result

    @app.post('/api/dubbing/open')
    def open_media(body:dict):
        from .queue_api import open_media_output
        path=saved_media(body.get('project_id',''),body.get('artifact_id','source'))
        project=service._project(body['project_id'])
        if body.get('reveal') is True:
            if os.name!='nt':
                return {'opened':False,'path':str(path),'reason':'Native folder opening is supported on Windows.'}
            subprocess.Popen(['explorer.exe','/select,',str(path)],**no_window())
            return {'opened':True}
        return open_media_output({'status':'completed','outputs':[{'path':str(path)}],
                                  'payload':{'destination':str(path.parent),'source':project['source']}},
                                 0)

    return service
