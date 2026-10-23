"""Persistent destinations and YouTube Source Watch, using FORGE's existing worker queue.

Only additive SQLite tables are created. A media record and its queue job are
committed in the same transaction, so a restart cannot duplicate that download.
Network discovery runs outside database/scheduler locks and never at startup's
request boundary. Provider failures never discard the last successful snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Literal
import uuid

from pydantic import Field, model_validator

from .core import absolute_path, safe_name
from .models import Download, Strict
from .source_probe import CONTENT_TYPES
from .source_watch import ADAPTERS, SourceError
from .download_support import diagnostic, diagnostics

DEFAULT_TYPES=['video','short','replay','premiere']
PRIORITIES=('critical','high','normal','low')
ACTIVE={'queued','running','cancelling','finalizing'}
TYPE_DIRS={'audio':'Audio','video':'Videos','short':'Shorts','live':'Streams','replay':'Streams','upcoming':'Premieres',
           'premiere':'Premieres','clip':'Clips','podcast':'Podcasts','unclassified':'Unclassified'}


class TypeRule(Strict):
    enabled: bool | None = None
    priority: Literal['critical','high','normal','low'] | None = None
    directory: str = Field('',max_length=2000)
    options: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def safe_options(self):
        if 'cookies' in self.options or 'capture_live' in self.options or 'playlist' in self.options:
            raise ValueError('Authentication, live capture and playlists are controlled at source level.')
        Download.model_validate(self.options)
        return self


class WatchConfig(Strict):
    enabled: bool = True
    interval_minutes: int = Field(60,ge=15,le=1440)
    initial: Literal['future','latest','after','review'] = 'future'
    latest: int = Field(10,ge=1,le=200)
    after: str = Field('',max_length=10)
    priority: Literal['critical','high','normal','low'] = 'normal'
    tool: Literal['download_audio','download_video'] = 'download_video'
    types: list[Literal['video','short','live','replay','upcoming','premiere','clip','podcast','unclassified']] = Field(default_factory=lambda:list(DEFAULT_TYPES),max_length=9)
    live_action: Literal['notify','capture','replay','ignore'] = 'replay'
    directory: str = Field('',max_length=2000)
    subfolders: bool = True
    type_subfolders: bool = True
    options: Download = Field(default_factory=lambda:Download(format='mp4'))
    rules: dict[str,TypeRule] = Field(default_factory=dict,max_length=9)
    auto_retry: bool = False

    @model_validator(mode='after')
    def coherent(self):
        if self.after:
            datetime.strptime(self.after,'%Y-%m-%d')
        if self.initial=='after' and not self.after:
            raise ValueError('Select a publication date for the initial import.')
        if set(self.rules)-set(CONTENT_TYPES):
            raise ValueError('Unknown content type rule.')
        if self.options.playlist:
            raise ValueError('Watched media is queued individually, never as a nested playlist.')
        return self


class WatchRequest(Strict):
    url: str = Field(min_length=1,max_length=8000)
    config: WatchConfig = Field(default_factory=WatchConfig)


class DestinationRequest(Strict):
    path: str = Field(min_length=1,max_length=2000)
    name: str = Field('',max_length=80)
    pinned: bool = False
    default: bool = False
    create: bool = False


class MediaAction(Strict):
    key: str = Field(min_length=1,max_length=200)
    action: Literal['download','retry','skip','priority','archived']
    priority: Literal['critical','high','normal','low'] = 'normal'
    path: str = Field('',max_length=2000)


def writable_directory(value, *, create=False):
    path=absolute_path(value,directory=True,must_exist=False)
    if create: path.mkdir(parents=True,exist_ok=True)
    if not path.is_dir(): raise ValueError('The selected directory is unavailable. Create it or choose another folder.')
    probe=path/('.forge-write-test-'+uuid.uuid4().hex)
    try:
        with probe.open('xb'): pass
    finally:
        probe.unlink(missing_ok=True)
    return path.resolve()


class Destinations:
    def __init__(self,store): self.store=store
    def snapshot(self):
        return {'default':self.store.get('download_default',str(Path.home()/'Downloads'/'FORGE')),
                'last':self.store.get('download_last',''),'records':self.store.get('download_directories',[])}
    def remember(self,path,*,name=None,pinned=None,default=False):
        path=str(Path(path).resolve())
        with self.store.lock:
            values=self.store.get('download_directories',[])
            previous=next((v for v in values if os.path.normcase(v['path'])==os.path.normcase(path)),{})
            record={'path':path,'name':name if name is not None else previous.get('name',''),
                    'pinned':pinned if pinned is not None else previous.get('pinned',False),'used':time.time()}
            remaining=[v for v in values if v['path']!=previous.get('path')]
            pins=[v for v in remaining if v.get('pinned')]
            if len(pins)+(1 if record['pinned'] else 0)>50:
                raise ValueError('Keep at most 50 pinned download destinations.')
            recent=[v for v in remaining if not v.get('pinned')][:29]
            self.store.set('download_directories',[record]+pins+recent)
            self.store.set('download_last',path)
            if default: self.store.set('download_default',path)
        return self.snapshot()
    def resolve(self,path='',*,channel='',kind='',subfolders=False,type_subfolders=False,platform='YouTube'):
        settings=self.snapshot()
        root=absolute_path(path or settings['last'] or settings['default'],directory=True,must_exist=False)
        if subfolders:
            root=root/safe_name(platform)/safe_name(channel or 'Manual')
        if type_subfolders and kind:
            root=root/TYPE_DIRS.get(kind,'Videos')
        return writable_directory(str(root),create=True)


class DownloadStudio:
    def __init__(self,store,manager,adapter=None):
        self.store,self.manager=store,manager
        self.adapter=adapter or ADAPTERS['youtube']()
        self.destinations=Destinations(store)
        self.lock=threading.RLock()
        self.busy=set()
        self.threads=[]
        with store.lock:
            store.db.executescript('''
            CREATE TABLE IF NOT EXISTS watch_sources (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS watch_media (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS watch_discovery (source_id TEXT NOT NULL, media_id TEXT NOT NULL,
                PRIMARY KEY(source_id,media_id));
            CREATE INDEX IF NOT EXISTS watch_discovery_media ON watch_discovery(media_id);
            ''')
            store.db.commit()

    def get(self,table,key):
        with self.store.lock:
            row=self.store.db.execute(f'SELECT data FROM {table} WHERE id=?',(key,)).fetchone()
        if not row: raise ValueError('Saved source or media record was not found.')
        return json.loads(row[0])

    def put(self,table,key,value):
        with self.store.lock:
            self.store.db.execute(f'INSERT OR REPLACE INTO {table} VALUES (?,?)',(key,json.dumps(value)))
            self.store.db.commit()

    def sources(self):
        with self.store.lock:
            return [json.loads(r[0]) for r in self.store.db.execute('SELECT data FROM watch_sources')]

    def media(self,source='',limit=200,offset=0):
        with self.store.lock:
            rows=self.store.db.execute('SELECT m.data FROM watch_media m '+
                ('JOIN watch_discovery d ON d.media_id=m.id WHERE d.source_id=? ' if source else '')+
                "ORDER BY json_extract(m.data,'$.discovered') DESC LIMIT ? OFFSET ?",
                ((source,) if source else ())+(max(1,min(500,limit)),max(0,offset))).fetchall()
        return [json.loads(r[0]) for r in rows]

    def snapshot(self):
        self.reconcile()
        sources=[]
        for source in self.sources():
            public=json.loads(json.dumps(source))
            public['authentication_configured']=bool(public['config']['options'].pop('cookies',''))
            with self.store.lock:
                counts=self.store.db.execute("SELECT json_extract(m.data,'$.status'),COUNT(*) FROM watch_media m JOIN watch_discovery d ON d.media_id=m.id WHERE d.source_id=? GROUP BY json_extract(m.data,'$.status')",(source['id'],)).fetchall()
            counts=dict(counts)
            public['counts']={'discovered':sum(counts.values()),'downloaded':counts.get('completed',0)+counts.get('archived',0),
                              'pending':sum(counts.get(s,0) for s in ACTIVE|{'waiting'}),'failed':counts.get('failed',0),'skipped':counts.get('skipped',0),'review':counts.get('review',0)}
            public['busy']=source['id'] in self.busy
            sources.append(public)
        return {'sources':sources,'paused':self.store.get('watch_paused',False),
                'defaults':self.store.get('watch_defaults',WatchConfig().model_dump()),
                'capabilities':self.adapter.capabilities,'content_types':CONTENT_TYPES}

    def register(self,request):
        if len(self.sources())>=100: raise ValueError('Keep at most 100 watched sources.')
        config=request.config.model_dump()
        result=self.adapter.inspect(request.url,limit=200,cookies=config['options']['cookies'])
        key=self.adapter.platform+':'+result['source_id']
        with self.lock:
            if any(s['id']==key for s in self.sources()):
                raise ValueError('This channel or playlist is already monitored, including renamed handles.')
            source={'id':key,'platform':self.adapter.platform,'url':result['url'],'title':result['title'],
                    'folder':safe_name(result['source_id']),'config':config,'created':time.time(),
                    'last_success':None,'next_check':0,'failures':0,'status':'ready','archive_offset':0}
            self.put('watch_sources',key,source)
            self.ingest(source,result,initial=True)
        return self.snapshot()

    def configure(self,key,changes):
        with self.lock:
            source=self.get('watch_sources',key)
            if 'options' in changes:
                changes={**changes,'options':{**source['config']['options'],**changes['options']}}
            source['config']=WatchConfig.model_validate({**source['config'],**changes}).model_dump()
            source['next_check']=time.time()
            source['status']='ready' if source['config']['enabled'] else 'paused'
            self.put('watch_sources',key,source)
        return self.snapshot()

    def start(self):
        def loop():
            # Immediate asynchronous checks on each launch, then bounded polling.
            startup_due={s['id'] for s in self.sources() if s.get('status') not in {'offline','access_required'}}
            while not self.manager.stop_event.is_set():
                if not self.store.get('watch_paused',False):
                    for source in self.sources():
                        if source['config']['enabled'] and source.get('status')!='access_required' and (source['id'] in startup_due or source.get('next_check',0)<=time.time()):
                            if self.schedule(source['id']).get('scheduled'): startup_due.discard(source['id'])
                try:
                    self.reconcile()
                    if not self.store.get('watch_paused',False):
                        self.retry_due()
                except Exception:
                    logging.getLogger(__name__).warning('Channel Watch deferred a local reconciliation; saved records are retained.')
                if self.manager.stop_event.wait(15): break
        thread=threading.Thread(target=loop,daemon=True,name='forge-channel-watch')
        self.threads.append(thread);thread.start()

    def schedule(self,key,*,archive=False,manual=False):
        with self.lock:
            if self.manager.stop_event.is_set(): return {'scheduled':False}
            source=self.get('watch_sources',key)
            if key in self.busy: return {'scheduled':False,'busy':True}
            if len(self.busy)>=2: return {'scheduled':False,'busy':True}
            if manual and time.time()-source.get('last_attempt',0)<15:
                return {'scheduled':False,'message':'A source check just ran. Please wait a few seconds.'}
            self.busy.add(key)
        def check():
            try: self.check_once(key,archive=archive)
            finally:
                with self.lock: self.busy.discard(key)
        self.threads=[t for t in self.threads if t.is_alive()]
        thread=threading.Thread(target=check,daemon=True,name='forge-source-check')
        self.threads.append(thread);thread.start()
        return {'scheduled':True}

    def check_once(self,key,*,archive=False):
        with self.lock:
            source=self.get('watch_sources',key)
            source['last_attempt']=time.time();self.put('watch_sources',key,source)
        try:
            offset=source.get('archive_offset',0)+200 if archive else 0
            result=self.adapter.inspect(source['url'],limit=200,offset=offset,cookies=source['config']['options']['cookies'])
            if self.adapter.platform+':'+result['source_id'] != key:
                raise SourceError({'code':'identity','message':'The source identity changed. Review the saved channel before continuing.','retryable':False})
            if self.manager.stop_event.is_set(): return
            with self.lock:
                # Re-read configuration: a user can pause/edit while HTTP is pending.
                source=self.get('watch_sources',key)
                self.ingest(source,result,archive=archive)
        except Exception as error:
            if self.manager.stop_event.is_set(): return
            details=error.details if isinstance(error,SourceError) else diagnostic(error)
            with self.lock:
                source=self.get('watch_sources',key)
                source.update(failures=source.get('failures',0)+1,error=details,
                              status='offline' if details['retryable'] else 'access_required')
                source['next_check']=time.time()+min(86400,900*2**min(source['failures'],6))
                self.put('watch_sources',key,source)

    def ingest(self,source,result,*,initial=False,archive=False):
        config=source['config'];now=time.time()
        for index,item in enumerate(result['entries']):
            key=self.adapter.platform+':'+item['id']
            try: previous=self.get('watch_media',key)
            except ValueError: previous=None
            value={**(previous or {}),**item,'key':key,'source_id':(previous or {}).get('source_id',source['id']),
                   'discovered':(previous or {}).get('discovered',now),'status':(previous or {}).get('status','review'),
                   'attempts':(previous or {}).get('attempts',0),'priority':(previous or {}).get('priority',config['priority'])}
            self.put('watch_media',key,value)
            with self.store.lock:
                self.store.db.execute('INSERT OR IGNORE INTO watch_discovery VALUES (?,?)',(source['id'],key));self.store.db.commit()
            policy=config['initial']
            eligible=not initial or policy=='latest' and index<config['latest']
            if initial and policy=='after':
                date=item.get('upload_date','')
                if not date and item.get('published'):
                    date=datetime.fromtimestamp(item['published'],timezone.utc).strftime('%Y%m%d')
                eligible=bool(date and date>=config['after'].replace('-',''))
            if initial and not eligible and not previous:
                value['status']='waiting' if item['type'] in {'upcoming','live'} else 'skipped' if policy=='future' else 'review'
                value['reason']='Initial import: future uploads only.' if policy=='future' else 'Historical media awaits review.'
                self.put('watch_media',key,value)
            transition=previous and previous.get('type') in {'live','upcoming'} and item['type'] not in {'live','upcoming'} and previous.get('status') in {'waiting','review'}
            if archive:
                continue  # Historical paging is always review-only.
            if not previous and eligible or transition:
                value['auto_pending']=True
                self.put('watch_media',key,value)
                if config['enabled'] and not self.store.get('watch_paused',False):
                    self.queue_media(key,source=source,automatic=True)
        source.update(title=result['title'],thumbnail=result.get('thumbnail',''),thumbnail_data=result.get('thumbnail_data',''),reported_count=result.get('reported_count'),
                      reported_count_scope=result.get('reported_count_scope',''),last_success=now,next_check=now+config['interval_minutes']*60,
                      failures=0,status='monitoring' if config['enabled'] else 'paused',error=None,
                      latest=result['entries'][0] if result['entries'] else source.get('latest'),has_more=result.get('has_more',False))
        if archive: source['archive_offset']=result.get('offset',0)
        self.put('watch_sources',source['id'],source)

    def queue_media(self,key,*,source=None,automatic=False):
        with self.lock, self.manager.lock:
            record=self.get('watch_media',key)
            if automatic and (self.manager.maintenance or self.manager.maintenance_pending):
                record.update(status='waiting',auto_pending=True,reason='Waiting for visible maintenance to complete or be cancelled.')
                self.put('watch_media',key,record);return record
            self.manager.check_accepting()
            if record['status'] in ACTIVE|{'completed','archived'}: return record
            source=source or self.get('watch_sources',record['source_id'])
            config=source['config'];kind=record['type'];rule=config['rules'].get(kind,{})
            enabled=rule.get('enabled') if rule.get('enabled') is not None else kind in config['types']
            if kind=='upcoming' or kind=='live' and config['live_action']!='capture':
                record.update(auto_pending=False,status='skipped' if kind=='live' and config['live_action']=='ignore' else 'waiting',reason='Tracked until a downloadable replay is available.' if config['live_action']=='replay' else 'Notification only; no automatic live capture.')
                self.put('watch_media',key,record);return record
            if automatic and not enabled:
                record.update(status='review',auto_pending=False,reason='Content type awaits manual review or is disabled.')
                self.put('watch_media',key,record);return record
            options=Download.model_validate({**config['options'],**rule.get('options',{}),'playlist':False,'capture_live':kind=='live' and config['live_action']=='capture'}).model_dump()
            tool='download_audio' if options['format'] in {'mp3','m4a','flac','wav'} else 'download_video'
            if self.manager.require_feature: self.manager.require_feature(tool)
            folder=self.destinations.resolve(rule.get('directory') or config['directory'],channel=source['folder'],kind='audio' if tool=='download_audio' else kind,subfolders=config['subfolders'],type_subfolders=config['type_subfolders'])
            priority=record.get('priority_override') or rule.get('priority') or config['priority']
            job_id=uuid.uuid4().hex;now=time.time()
            payload={'tool':tool,'options':options,'settings':self.store.settings(),'destination':str(folder),'source':None,
                     'url':record['url'],'job_id':job_id,'watch_media':key,'priority':priority,
                     'resume_key':key+hashlib.sha256(json.dumps(options,sort_keys=True).encode()).hexdigest()}
            record.update(source_id=source['id'],job_id=job_id,status='queued',priority=priority,attempts=record['attempts']+1,destination=str(folder),reason='Queued for download.',auto_pending=False)
            with self.store.lock:
                try:
                    self.store.db.execute('BEGIN IMMEDIATE')
                    self.store.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)',(job_id,tool,record['title'],'queued',0,'Waiting for a processing slot.',json.dumps(payload),'[]','[]',now,now))
                    self.store.db.execute('INSERT INTO job_meta VALUES (?,?)',(job_id,json.dumps({'priority':priority})))
                    self.store.db.execute('UPDATE watch_media SET data=? WHERE id=?',(json.dumps(record),key))
                    self.store.db.commit()
                except BaseException:
                    self.store.db.rollback();raise
            return record

    def reconcile(self):
        with self.lock:
            with self.store.lock:
                rows=self.store.db.execute("SELECT data FROM watch_media WHERE json_extract(data,'$.status') IN ('queued','running','cancelling','finalizing','failed')").fetchall()
            for row in rows:
                value=json.loads(row[0]);job_id=value.get('job_id')
                if not job_id: continue
                try: job=self.store.job(job_id)
                except ValueError:
                    value.update(status='failed',reason='Queue record removed; discovered media is preserved.');self.put('watch_media',value['key'],value);continue
                if job['status'] in ACTIVE:
                    if value['status']!=job['status']:
                        value['status']=job['status'];self.put('watch_media',value['key'],value)
                    continue
                if value.get('reconciled_job')==job_id: continue
                completed=job['status']=='completed' and bool(job['outputs'])
                outputs=[]
                for output in job['outputs']:
                    p=Path(output['path'])
                    try:
                        valid=p.is_file() and p.stat().st_size==output['size'] and output['size']>0
                        if valid: outputs.append({**output,'sha256':file_digest(p)})
                    except OSError:
                        valid=False
                    completed &= valid
                value.update(status='completed' if completed else 'skipped' if job['status']=='cancelled' else 'failed',
                             reason=job['message'],outputs=outputs,execution_seconds=job.get('execution_seconds'),
                             stage_seconds=job.get('stage_seconds',{}),bytes=sum(o['size'] for o in outputs),
                             finished=time.time(),reconciled_job=job_id)
                self.put('watch_media',value['key'],value)

    def retry_due(self):
        # Only explicitly enabled retries, transient classified errors, at most
        # three total attempts. Access-control errors never enter this path.
        with self.store.lock:
            rows=self.store.db.execute("SELECT data FROM watch_media WHERE json_extract(data,'$.auto_pending')=1 OR (json_extract(data,'$.status')='failed' AND json_extract(data,'$.attempts')<3)").fetchall()
        for row in rows:
            record=json.loads(row[0])
            if record.get('auto_pending'):
                source=self.get('watch_sources',record['source_id'])
                if source['config']['enabled']:
                    try: self.queue_media(record['key'],automatic=True)
                    except ValueError: pass
                continue
            if record['status']!='failed' or record['attempts']>=3 or time.time()<record.get('finished',time.time())+900*2**record['attempts']: continue
            source=self.get('watch_sources',record['source_id'])
            if source['config']['enabled'] and source['config']['auto_retry'] and diagnostic(record.get('reason',''))['retryable']:
                try: self.queue_media(record['key'],automatic=True)
                except ValueError: pass

    def action(self,body):
        if body.action in {'download','retry'}: return self.queue_media(body.key)
        with self.lock,self.manager.lock:
            value=self.get('watch_media',body.key)
            if body.action=='priority':
                value['priority_override']=value['priority']=body.priority
                if value.get('job_id') and value['status']=='queued': self.store.update_meta(value['job_id'],priority=body.priority)
            elif body.action=='skip':
                if value['status'] in ACTIVE: raise ValueError('Cancel active/queued work in Processing Queue before skipping this record.')
                value.update(status='skipped',auto_pending=False,reason='Skipped permanently by the user.')
            elif body.action=='archived':
                if value['status'] in ACTIVE: raise ValueError('Finish or cancel the pending job first.')
                path=absolute_path(body.path)
                known={o.get('sha256') for o in value.get('outputs',[]) if o.get('sha256')}
                if not known or file_digest(path) not in known:
                    raise ValueError('The selected file must match a previously verified download hash for this media. Filename matching alone is insufficient.')
                value.update(status='archived',archived_path=str(path),reason='Previously verified media matched by SHA-256.')
            self.put('watch_media',body.key,value)
            return value


def file_digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''): value.update(chunk)
    return value.hexdigest()


def mount(app,store,manager):
    studio=DownloadStudio(store,manager);app.state.download_studio=studio

    @app.get('/api/downloads/directories')
    def directories(): return studio.destinations.snapshot()

    @app.post('/api/downloads/directories')
    def save_directory(body:DestinationRequest):
        path=writable_directory(body.path,create=body.create)
        return studio.destinations.remember(path,name=body.name,pinned=body.pinned,default=body.default)

    @app.post('/api/downloads/directories/validate')
    def validate_directory(body:DestinationRequest):
        return {'available':True,'path':str(writable_directory(body.path,create=body.create))}

    @app.post('/api/downloads/directories/open')
    def open_directory(body:DestinationRequest):
        path=absolute_path(body.path,directory=True)
        if os.name!='nt': return {'opened':False,'path':str(path)}
        os.startfile(str(path),'open');return {'opened':True}

    @app.post('/api/downloads/preview')
    def preview(body:dict):
        from .entitlements import require_feature
        require_feature(app,'download_video')
        if not isinstance(body.get('url'),str): raise ValueError('Paste a media URL.')
        return studio.adapter.inspect(body['url'],watch=False,playlist=body.get('playlist') is True,limit=25,cookies=body.get('cookies',''))

    @app.get('/api/downloads/diagnostics')
    def check_downloads(): return diagnostics()

    @app.get('/api/watch')
    def watch(): return studio.snapshot()

    @app.post('/api/watch')
    def add_watch(body:WatchRequest):
        manager.require_feature(body.config.tool)
        return studio.register(body)

    @app.get('/api/watch/media')
    def media(source:str='',limit:int=200,offset:int=0):
        studio.reconcile();return {'media':studio.media(source,limit,offset)}

    @app.post('/api/watch/configure')
    def configure(body:dict): return studio.configure(body.get('id',''),body.get('config',{}))

    @app.post('/api/watch/defaults')
    def defaults(body:WatchConfig):
        value=body.model_dump();value['options']['cookies']=''
        store.set('watch_defaults',value);return {'defaults':value}

    @app.post('/api/watch/refresh')
    def refresh(body:dict): return studio.schedule(body.get('id',''),archive=body.get('archive') is True,manual=True)

    @app.post('/api/watch/pause')
    def pause(body:dict):
        if not isinstance(body.get('paused'),bool): raise ValueError('Choose pause or resume.')
        store.set('watch_paused',body['paused']);return {'paused':body['paused']}

    @app.post('/api/watch/media/action')
    def action(body:MediaAction): return studio.action(body)

    @app.post('/api/queue/priority')
    def priority(body:dict):
        if body.get('priority') not in PRIORITIES: raise ValueError('Choose a registered priority.')
        with manager.lock:
            job=store.job(body.get('id',''))
            if job['status']!='queued': raise ValueError('Only waiting jobs can change priority.')
            store.update_meta(job['id'],priority=body['priority'])
        return {'priority':body['priority']}

    return studio
