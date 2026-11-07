"""Additive revisioned dubbing projects; worker results never replace newer edits."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from .dubbing_options import DubbingOptions

ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
SHA = re.compile(r'^[0-9a-f]{64}$')
RECORD_KINDS = {'voices','glossaries','presets'}
SEGMENT_FIELDS = ('id','start','end','speaker','source_text','text','reviewed','manual_edit',
                  'voice_profile_id','emotion','speed')
PRIVATE_KEYS = {'access_token','refresh_token','client_secret','password','cookies','credentials','authorization'}
DOWNSTREAM = {
    'dubbing_transcribe':{'dubbing_translate','dubbing_voice','dubbing_align','dubbing_mix','dubbing_lipsync','dubbing_encode','dubbing_export','dubbing_metadata','dubbing_prepare_upload'},
    'dubbing_translate':{'dubbing_voice','dubbing_align','dubbing_mix','dubbing_lipsync','dubbing_encode','dubbing_export','dubbing_metadata','dubbing_prepare_upload'},
    'dubbing_voice':{'dubbing_align','dubbing_mix','dubbing_lipsync','dubbing_encode','dubbing_export','dubbing_prepare_upload'},
    'dubbing_align':{'dubbing_mix','dubbing_lipsync','dubbing_encode','dubbing_export','dubbing_prepare_upload'},
    'dubbing_separate':{'dubbing_mix','dubbing_lipsync','dubbing_encode','dubbing_export','dubbing_prepare_upload'},
    'dubbing_mix':{'dubbing_lipsync','dubbing_encode','dubbing_export','dubbing_prepare_upload'},
    'dubbing_effects':{'dubbing_lipsync','dubbing_encode','dubbing_export','dubbing_prepare_upload'},
    'dubbing_lipsync':{'dubbing_encode','dubbing_export','dubbing_prepare_upload'},
    'dubbing_encode':{'dubbing_export','dubbing_prepare_upload'},
    'dubbing_metadata':{'dubbing_export','dubbing_prepare_upload'},
}


class RevisionConflict(ValueError):
    pass


def canonical(value):
    def numbers(item):
        if isinstance(item,dict): return {key:numbers(child) for key,child in item.items()}
        if isinstance(item,list): return [numbers(child) for child in item]
        if type(item) is float and math.isfinite(item) and item.is_integer(): return int(item)
        return item
    return json.dumps(numbers(value),sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def segment_fingerprint(segment):
    return fingerprint({key:segment[key] for key in SEGMENT_FIELDS if key in segment})


def stage_fingerprint(project, stage):
    stage=stage.removeprefix('dubbing_')
    options=project.get('options',{})
    keys={'transcribe':('asr',),'translate':('translation',),'voice':('tts',),
          'voice_reference':('tts',),'align':('timing',),'separate':('audio',),
          'mix':('audio','timing'),'effects':('audio',),'lipsync':('lip_sync',),'encode':('export',),
          'export':('export',),'metadata':('publishing','translation'),
          'prepare_upload':('publishing',)}.get(stage,())
    value={'options':{key:options.get(key,{}) for key in keys}}
    if stage in {'voice','voice_reference'}:
        value['speakers']=project.get('speakers',{})
    if stage in {'translate','metadata'}:
        value.update(mode=project.get('mode','localized'),glossary_id=project.get('glossary_id',''))
    return fingerprint(value)


def review_fingerprint(project):
    return fingerprint({'source':project.get('source',{}), 'mode':project.get('mode','localized'),
        'segments':[{key:s.get(key) for key in ('id','start','end','speaker','source_text','text','voice_profile_id','emotion','speed')}
                    for s in project.get('segments',[])], 'tts':project.get('options',{}).get('tts',{}),
        'speakers':project.get('speakers',{})})


def stage_input_fingerprint(project,stage):
    value={'settings':stage_fingerprint(project,stage),'source':project.get('source',{}),
           'segments':[{key:row.get(key) for key in SEGMENT_FIELDS if key not in {'reviewed','manual_edit'}}
                       for row in project.get('segments',[])]}
    if stage in {'dubbing_align','dubbing_mix','dubbing_effects','dubbing_lipsync','dubbing_encode','dubbing_export'}:
        value['audio']=[{'id':row['id'],'audio':row.get('audio',{})} for row in project.get('segments',[])]
    return fingerprint(value)


def check_identity(value):
    if not isinstance(value,str) or not ID.fullmatch(value):
        raise ValueError('Use a saved local dubbing identity.')
    return value


def bounded_json(value, maximum=1_800_000):
    def inspect(item,depth=0):
        if depth>24:
            raise ValueError('Dubbing data is nested too deeply.')
        if isinstance(item,dict):
            if any(not isinstance(key,str) or key.lower() in PRIVATE_KEYS for key in item):
                raise ValueError('Credentials belong in the protected publishing connection, not in a dubbing project.')
            for child in item.values(): inspect(child,depth+1)
        elif isinstance(item,list):
            for child in item: inspect(child,depth+1)
    inspect(value)
    text=canonical(value)
    if len(text.encode('utf-8'))>maximum:
        raise ValueError('This dubbing record exceeds the local document size limit.')
    return text


def normalize_segments(values):
    if not isinstance(values,list) or len(values)>10000:
        raise ValueError('A dubbing project supports up to 10,000 speech segments.')
    result=[];seen=set()
    for index,item in enumerate(values):
        if not isinstance(item,dict): raise ValueError('Each dubbing segment must be an object.')
        row=copy.deepcopy(item)
        identity=check_identity(row.get('id') or uuid.uuid4().hex)
        if identity in seen: raise ValueError('Every dubbing segment needs a unique identity.')
        seen.add(identity)
        start,end=row.get('start',0),row.get('end',0)
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 or v>48*3600 for v in (start,end)) or end<=start:
            raise ValueError('Each speech segment needs finite start/end times and a positive duration.')
        for field in ('source_text','text'):
            if not isinstance(row.get(field,''),str) or len(row.get(field,''))>50000:
                raise ValueError('A dubbing segment contains invalid or oversized text.')
        speaker=row.get('speaker') or 'speaker1'
        if not isinstance(speaker,str) or not 1<=len(speaker)<=80:
            raise ValueError('Choose a saved speaker identity.')
        row.update(id=identity,start=float(start),end=float(end),speaker=speaker,
                   source_text=row.get('source_text',''),text=row.get('text',''),
                   reviewed=row.get('reviewed') is True,manual_edit=row.get('manual_edit') is True)
        if row.get('speed') is not None and (type(row['speed']) not in (int,float) or not math.isfinite(row['speed']) or not .5<=row['speed']<=2):
            raise ValueError('Segment pacing must be between 0.5 and 2.')
        result.append(row)
    return result


class DubbingStore:
    def __init__(self,store):
        self.store=store
        self.root=store.data/'dubbing'
        self.root.mkdir(parents=True,exist_ok=True)
        with store.lock:
            store.db.executescript('''
                CREATE TABLE IF NOT EXISTS dubbing_projects(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS dubbing_revisions(project_id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(project_id,revision));
                CREATE TABLE IF NOT EXISTS dubbing_records(kind TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS dubbing_runs(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,data TEXT NOT NULL,updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS dubbing_runs_project ON dubbing_runs(project_id,updated);
                CREATE TABLE IF NOT EXISTS dubbing_artifacts(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,run_id TEXT NOT NULL,data TEXT NOT NULL,created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS dubbing_artifacts_project ON dubbing_artifacts(project_id,created);
            ''')

    @contextmanager
    def transaction(self):
        with self.store.lock:
            name='dubbing_'+uuid.uuid4().hex
            self.store.db.execute('SAVEPOINT '+name)
            try:
                yield
                self.store.db.execute('RELEASE SAVEPOINT '+name)
            except BaseException:
                self.store.db.execute('ROLLBACK TO SAVEPOINT '+name)
                self.store.db.execute('RELEASE SAVEPOINT '+name)
                raise

    def _project(self,identity):
        check_identity(identity)
        row=self.store.db.execute('SELECT data FROM dubbing_projects WHERE id=?',(identity,)).fetchone()
        if not row: raise ValueError('Dubbing project not found.')
        return json.loads(row[0])

    def project(self,identity):
        with self.store.lock:
            value=self._project(identity)
            value['artifacts']=[json.loads(row[0]) for row in self.store.db.execute(
                'SELECT data FROM dubbing_artifacts WHERE project_id=? ORDER BY created DESC LIMIT 1000',(identity,))]
            value['runs']=[json.loads(row[0]) for row in self.store.db.execute(
                'SELECT data FROM dubbing_runs WHERE project_id=? ORDER BY updated DESC LIMIT 30',(identity,))]
            for run in value['runs']:
                run.pop('snapshot',None)
                run.pop('voice_profiles',None)
                run.pop('frozen_inputs',None)
                run['results']={stage:{'job_id':item.get('job_id'),'stale_segments':item.get('stale_segments',[]),
                                       'artifact_count':len(item.get('artifacts',[]))}
                                for stage,item in run.get('results',{}).items()}
            return value

    def projects(self):
        with self.store.lock:
            values=[json.loads(row[0]) for row in self.store.db.execute('SELECT data FROM dubbing_projects ORDER BY updated DESC LIMIT 500')]
        return [{key:value.get(key) for key in ('id','name','revision','created','updated','source','mode','source_language','target_language','status')}
                | {'segment_count':len(value.get('segments',[]))} for value in values]

    def _write_project(self,value,previous=None):
        now=time.time()
        value['revision']=(previous or {}).get('revision',0)+1
        value['updated']=now
        value.setdefault('created',now)
        encoded=bounded_json(value)
        self.store.db.execute('INSERT OR REPLACE INTO dubbing_projects VALUES (?,?,?,?)',
                             (value['id'],value['revision'],encoded,now))
        self.store.db.execute('INSERT INTO dubbing_revisions VALUES (?,?,?,?)',
                             (value['id'],value['revision'],encoded,now))
        return value

    def invalidate_artifacts(self,project_id,stages):
        for row in self.store.db.execute('SELECT id,data FROM dubbing_artifacts WHERE project_id=?',(project_id,)).fetchall():
            value=json.loads(row['data'])
            if value.get('stage') in stages and not value.get('stale'):
                value['stale']=True
                self.store.db.execute('UPDATE dubbing_artifacts SET data=? WHERE id=?',(canonical(value),row['id']))

    def save_project(self,value,expected_revision=None,*,trusted=False):
        if not isinstance(value,dict): raise ValueError('Supply a dubbing project.')
        bounded_json(value)
        identity=check_identity(value.get('id') or uuid.uuid4().hex)
        with self.transaction():
            existing=self.store.db.execute('SELECT data FROM dubbing_projects WHERE id=?',(identity,)).fetchone()
            previous=json.loads(existing[0]) if existing else None
            expected=expected_revision if expected_revision is not None else value.get('revision')
            if previous and expected != previous['revision']:
                raise RevisionConflict('This dubbing project changed. Reload its current revision before saving your edits.')
            if not previous and expected not in (None,0):
                raise RevisionConflict('The selected dubbing project does not exist at that revision.')
            if previous and 'source' in value and value['source']!=previous.get('source'):
                raise ValueError('A dubbing project retains its original source; import another video as a new project.')
            if not previous and not trusted:
                raise ValueError('Import a local video before creating a dubbing project.')
            result=copy.deepcopy(previous or {})
            for key in ('name','mode','options','segments','speakers','glossary_id','publishing'):
                if key in value: result[key]=copy.deepcopy(value[key])
            if trusted:
                for key in ('source','source_metadata','raw_transcript','source_audio','status','source_language','target_language'):
                    if key in value: result[key]=copy.deepcopy(value[key])
            name=result.get('name') or 'Russian version'
            if not isinstance(name,str) or not name.strip() or len(name)>200 or '\x00' in name:
                raise ValueError('Use a short, nonempty project name.')
            if result.get('mode','localized') not in {'faithful','localized','alternative'}:
                raise ValueError('Choose faithful, localized or alternative commentary.')
            result.update(id=identity,name=name.strip(),mode=result.get('mode','localized'),
                          source_language='en',target_language='ru',status=result.get('status','draft'))
            result['options']=DubbingOptions.model_validate(result.get('options',{})).model_dump()
            result['segments']=normalize_segments(result.get('segments',[]))
            result.setdefault('speakers',{'speaker1':{'voice_profile_id':''}})
            if not isinstance(result['speakers'],dict) or len(result['speakers'])>100:
                raise ValueError('Use at most 100 saved speakers.')
            old={row['id']:row for row in (previous or {}).get('segments',[])}
            if not trusted:
                for row in result['segments']:
                    earlier=old.get(row['id'])
                    # Derived state is owned by verified worker results, not by
                    # a browser's stale full-project autosave.
                    for key in ('audio','timing','translation_suggestion','model_revision','generated'):
                        if earlier and key in earlier: row[key]=copy.deepcopy(earlier[key])
                        else: row.pop(key,None)
                    if earlier and any(row.get(k)!=earlier.get(k) for k in ('text','source_text','start','end','speaker','voice_profile_id','emotion','speed')):
                        row['reviewed']=False
                        if row.get('text')!=earlier.get('text'): row['manual_edit']=True
                        if earlier.get('audio') and any(row.get(k)!=earlier.get(k) for k in ('text','speaker','voice_profile_id','emotion','speed')):
                            row['audio']={**earlier['audio'],'stale':True}
                        if earlier.get('timing') and any(row.get(k)!=earlier.get(k) for k in ('start','end')):
                            row['timing']={**earlier['timing'],'stale':True}
                if previous:
                    if review_fingerprint(result)!=review_fingerprint(previous):
                        self.invalidate_artifacts(identity,DOWNSTREAM['dubbing_translate']|{'dubbing_translate'})
                    for stage in ('dubbing_voice','dubbing_align','dubbing_mix','dubbing_lipsync','dubbing_encode','dubbing_metadata'):
                        if stage_fingerprint(result,stage)!=stage_fingerprint(previous,stage):
                            self.invalidate_artifacts(identity,DOWNSTREAM.get(stage,set())|{stage})
                            if stage=='dubbing_voice':
                                for segment in result['segments']:
                                    if segment.get('audio'): segment['audio']={**segment['audio'],'stale':True}
            return copy.deepcopy(self._write_project(result,previous))

    def records(self,kind):
        if kind not in RECORD_KINDS: raise ValueError('Unknown dubbing record library.')
        with self.store.lock:
            return [json.loads(row[0]) for row in self.store.db.execute(
                'SELECT data FROM dubbing_records WHERE kind=? ORDER BY updated DESC LIMIT 1000',(kind,))]

    def record(self,kind,identity):
        if kind not in RECORD_KINDS: raise ValueError('Unknown dubbing record library.')
        check_identity(identity)
        with self.store.lock:
            row=self.store.db.execute('SELECT data FROM dubbing_records WHERE kind=? AND id=?',(kind,identity)).fetchone()
        if not row: raise ValueError('Saved dubbing record not found.')
        return json.loads(row[0])

    def save_record(self,kind,value,expected_revision=None,*,trusted=False):
        if kind not in RECORD_KINDS or not isinstance(value,dict): raise ValueError('Choose a valid local dubbing library.')
        bounded_json(value,500_000)
        identity=check_identity(value.get('id') or uuid.uuid4().hex)
        with self.transaction():
            row=self.store.db.execute('SELECT data FROM dubbing_records WHERE kind=? AND id=?',(kind,identity)).fetchone()
            previous=json.loads(row[0]) if row else {}
            expected=expected_revision if expected_revision is not None else value.get('revision')
            if previous and expected!=previous['revision']:
                raise RevisionConflict('This saved dubbing record changed. Reload before saving.')
            record={**previous,**copy.deepcopy(value),'id':identity,'revision':previous.get('revision',0)+1,
                    'updated':time.time(),'created':previous.get('created',time.time())}
            if not isinstance(record.get('name',''),str) or not 1<=len(record.get('name',''))<=200:
                raise ValueError('Give this dubbing record a short name.')
            if kind=='glossaries':
                entries=record.get('entries',{})
                if isinstance(entries,list):
                    entries={entry['source']:entry['target'] for entry in entries if isinstance(entry,dict) and entry.get('source') and entry.get('target')}
                if not isinstance(entries,dict) or len(entries)>5000 or any(not isinstance(k,str) or not isinstance(v,str) or not 1<=len(k)<=300 or not 1<=len(v)<=1000 for k,v in entries.items()):
                    raise ValueError('Use up to 5,000 nonempty source/target glossary entries.')
                record['entries']=entries
            if kind=='voices':
                if record.get('consent') is not True:
                    raise ValueError('Confirm permission for the identifiable voice reference.')
                if not trusted:
                    for field in ('prepared_reference','audio_quality','sample','original_reference'):
                        if field in value and value[field]!=previous.get(field):
                            raise ValueError('Prepare or test this reference through the voice workflow first.')
                    if record.get('approved') and not record.get('prepared_reference'):
                        raise ValueError('Prepare and review the voice reference before approving it.')
            self.store.db.execute('INSERT OR REPLACE INTO dubbing_records VALUES (?,?,?,?,?)',
                                 (kind,identity,record['revision'],bounded_json(record,500_000),record['updated']))
            return copy.deepcopy(record)

    def delete_record(self,kind,identity):
        self.record(kind,identity)
        with self.transaction():
            if kind=='voices':
                for row in self.store.db.execute('SELECT data FROM dubbing_projects'):
                    project=json.loads(row[0])
                    references=[project.get('options',{}).get('tts',{}).get('voice_profile_id')]
                    references.extend(value.get('voice_profile_id') for value in project.get('speakers',{}).values() if isinstance(value,dict))
                    references.extend(value.get('voice_profile_id') for value in project.get('segments',[]))
                    if identity in references:
                        raise ValueError('This voice is used by a dubbing project. Choose another voice first.')
            self.store.db.execute('DELETE FROM dubbing_records WHERE kind=? AND id=?',(kind,identity))

    def run(self,identity):
        check_identity(identity)
        with self.store.lock:
            row=self.store.db.execute('SELECT data FROM dubbing_runs WHERE id=?',(identity,)).fetchone()
        if not row: raise ValueError('Dubbing workflow not found.')
        return json.loads(row[0])

    def save_run(self,value):
        identity=check_identity(value.get('id') or uuid.uuid4().hex)
        project_id=check_identity(value['project_id'])
        result={**copy.deepcopy(value),'id':identity,'updated':time.time()}
        with self.transaction():
            self._project(project_id)
            self.store.db.execute('INSERT OR REPLACE INTO dubbing_runs VALUES (?,?,?,?)',
                                 (identity,project_id,bounded_json(result,4_000_000),result['updated']))
        return result

    def artifact(self,project_id,identity):
        check_identity(project_id);check_identity(identity)
        with self.store.lock:
            row=self.store.db.execute('SELECT data FROM dubbing_artifacts WHERE project_id=? AND id=?',(project_id,identity)).fetchone()
        if not row: raise ValueError('Saved dubbing media not found.')
        return json.loads(row[0])

    def add_artifact(self,project_id,artifact,run_id=''):
        check_identity(project_id)
        original_path=Path(artifact.get('path',''))
        path=original_path.resolve()
        if not path.is_file() or original_path.is_symlink(): raise ValueError('A dubbing artifact must be a completed local file.')
        digest=artifact.get('sha256','')
        if not SHA.fullmatch(digest): raise ValueError('A completed artifact needs verified SHA-256 evidence.')
        identity=artifact.get('id') or hashlib.sha256((project_id+'\0'+str(artifact.get('kind',''))+'\0'+str(path)+'\0'+digest).encode()).hexdigest()[:32]
        check_identity(identity)
        stat=path.stat()
        result={**copy.deepcopy(artifact),'id':identity,'path':str(path),'name':path.name,
                'size':stat.st_size,'source_stat':{'size':stat.st_size,'mtime_ns':stat.st_mtime_ns},
                'verified':True,'run_id':run_id,'created':time.time()}
        with self.transaction():
            self.store.db.execute('INSERT OR REPLACE INTO dubbing_artifacts VALUES (?,?,?,?,?)',
                                 (identity,project_id,run_id,bounded_json(result,100_000),result['created']))
        return result

    def apply_result(self,job,result):
        """Apply verified stage metadata after publication; hashing stays in workers."""
        meta=copy.deepcopy(result.get('dubbing_result') or {})
        if not meta: return
        payload=job.get('payload',{})
        context=payload.get('dubbing',{})
        project_id=context.get('project',{}).get('id') or meta.get('project_id')
        run_id=context.get('run_id') or meta.get('run_id')
        if meta.get('voice_profile_update'):
            profile=meta['voice_profile_update']
            self.save_record('voices',profile,profile.get('revision'),trusted=True)
        if not project_id or not ID.fullmatch(project_id): return
        with self.transaction():
            found=self.store.db.execute('SELECT data FROM dubbing_projects WHERE id=?',(project_id,)).fetchone()
            if not found: return
            project=json.loads(found[0]);previous=copy.deepcopy(project)
            current={segment['id']:segment for segment in project.get('segments',[])}
            stale=[]
            applied=0
            settings_hash=meta.get('stage_settings_hash')
            settings_match=not settings_hash or settings_hash==stage_fingerprint(project,job['tool'])
            if job['tool']=='dubbing_voice':
                revisions=meta.get('voice_profile_revisions',{})
                for identity,revision in revisions.items():
                    try: current_revision=self.record('voices',identity)['revision']
                    except ValueError: current_revision=None
                    if revision!=current_revision: settings_match=False
            snapshot=context.get('project',{})
            content_matches=not snapshot or stage_input_fingerprint(snapshot,job['tool'])==stage_input_fingerprint(project,job['tool'])
            original_ids={row['id'] for row in snapshot.get('segments',[])}
            updates=meta.get('segment_updates',[])
            if not isinstance(updates,list) or len(updates)>10000: raise ValueError('Invalid dubbing stage result.')
            for update in updates:
                if not isinstance(update,dict) or not update.get('id'): continue
                identity=check_identity(update['id'])
                existing=current.get(identity)
                expected=update.pop('expected_input_hash',None)
                if existing and (not settings_match or not expected or expected!=segment_fingerprint(existing)):
                    stale.append(identity);continue
                if not existing:
                    if job['tool']!='dubbing_transcribe' or not settings_match or identity in original_ids:
                        stale.append(identity);continue
                    current[identity]=update
                else:
                    current[identity]={**existing,**update}
                applied+=1
            if updates:
                project['segments']=normalize_segments(list(current.values()))
            project_updates=meta.get('project_updates',{})
            if settings_match and content_matches and not stale and isinstance(project_updates,dict):
                for key in ('source_audio','raw_transcript','status','metadata','mix','alignment','encoded_video','exports',
                            'transcription','original_audio_reference','voice_generation','lip_sync','upload_preparation',
                            'translation_model','translation_model_revision'):
                    if key in project_updates: project[key]=copy.deepcopy(project_updates[key])
            project['status']='review_required' if job['tool']=='dubbing_translate' and applied else project.get('status','draft')
            if applied or (settings_match and content_matches):
                self.invalidate_artifacts(project_id,DOWNSTREAM.get(job['tool'],set()))
            registered=[]
            for artifact in meta.get('artifacts',[]):
                descriptor={**artifact,'stage':job['tool'],'stage_settings_hash':settings_hash,
                            'stale':not settings_match or not content_matches or bool(stale)}
                if artifact.get('segment_id') and artifact['segment_id'] not in stale and settings_match:
                    descriptor['stale']=False
                registered.append(self.add_artifact(project_id,descriptor,run_id or ''))
            if canonical(project)!=canonical(previous): self._write_project(project,previous)
            if run_id:
                row=self.store.db.execute('SELECT data FROM dubbing_runs WHERE id=?',(run_id,)).fetchone()
                if row:
                    run=json.loads(row[0]);run.setdefault('results',{})[job['tool']]={**meta,'stale_segments':stale,'job_id':job['id']}
                    run.setdefault('artifacts',[]).extend(copy.deepcopy(registered))
                    # Refresh derived stage state while retaining the queued
                    # production recipe. Live controls cannot replace approved
                    # options between workers; explicit review binds new options.
                    saved_options=copy.deepcopy(run.get('snapshot',{}).get('options',project['options']))
                    run['snapshot']=copy.deepcopy(project)
                    run['snapshot']['options']=saved_options
                    run['status']='stale' if stale or not content_matches else 'review_required' if job['tool']=='dubbing_translate' else 'running'
                    if job['tool']=='dubbing_prepare_upload' and not stale and content_matches: run['status']='completed'
                    self.save_run(run)
