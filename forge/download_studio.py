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
import math
import os
from pathlib import Path
import threading
import time
from typing import Literal
import uuid
from urllib.parse import quote

from pydantic import Field, field_validator, model_validator

from .core import absolute_path, safe_name
from .models import Download, Strict
from .source_probe import CONTENT_TYPES
from .source_watch import ADAPTERS, SourceError, ThumbnailCache, approved_thumbnail_url
from .download_support import diagnostic, diagnostics

DEFAULT_TYPES=['video','short','replay','premiere']
CHANNEL_PRIORITIES=('minimal','low','medium','high','extra','ultra')
PRIORITIES=CHANNEL_PRIORITIES+('critical','normal')
VIDEO_ORDERS=('newest','oldest','most_viewed','least_viewed','longest','shortest')
PRIORITY_ALIASES={'critical':'ultra','normal':'medium'}
ORDER_ALIASES={'views_desc':'most_viewed','views_asc':'least_viewed','duration_desc':'longest','duration_asc':'shortest'}
ACTIVE={'queued','running','cancelling','finalizing'}
TYPE_DIRS={'audio':'Audio','video':'Videos','short':'Shorts','live':'Streams','replay':'Streams','upcoming':'Premieres',
           'premiere':'Premieres','clip':'Clips','podcast':'Podcasts','unclassified':'Unclassified'}


def channel_folder(title,identity):
    """Readable stable identity, bounded for Windows and UTF-8 filesystems."""
    identity=safe_name(identity)
    if len(identity)>80: identity=identity[:60]+'-'+hashlib.sha256(identity.encode()).hexdigest()[:12]
    prefix=safe_name(title.encode('utf-8',errors='replace').decode('utf-8'))
    suffix=' ['+identity+']'
    while len((prefix+suffix).encode('utf-8'))>200: prefix=prefix[:-1]
    return prefix+suffix


def latest_known_upload(previous, entries):
    """Retain the newest reported publication across bounded discovery pages.

    An archive page is a discovery event, not a new publication. Sparse updates
    to the same identity also retain its last reported date. Unknown dates never
    acquire a synthetic timestamp from check/discovery time.
    """
    def published(value):
        return value if type(value) in (int,float) and math.isfinite(value) and value>0 else None

    def dated(value):
        text=str(value or '')
        if len(text)!=8 or not text.isascii() or not text.isdigit(): return None
        try: return datetime.strptime(text,'%Y%m%d').replace(tzinfo=timezone.utc).timestamp()
        except ValueError: return None

    candidates={}
    if isinstance(previous,dict) and previous.get('id'):
        candidates[str(previous['id'])]=dict(previous)
    for entry in entries:
        if not isinstance(entry,dict) or not entry.get('id'): continue
        identity=str(entry['id']);old=candidates.get(identity,{})
        merged={**old,**{key:value for key,value in entry.items() if value not in (None,'')}}
        if published(entry.get('published')) is None and published(old.get('published')) is not None:
            merged['published']=old['published']
        if dated(entry.get('upload_date')) is None and dated(old.get('upload_date')) is not None:
            merged['upload_date']=old['upload_date']
        candidates[identity]=merged
    known=[(published(value.get('published')) or dated(value.get('upload_date')),identity,value)
           for identity,value in candidates.items()]
    known=[value for value in known if value[0] is not None]
    return max(known,key=lambda value:(value[0],value[1]))[2] if known else None


class TypeRule(Strict):
    enabled: bool | None = None
    priority: Literal['minimal','low','medium','high','extra','ultra','critical','normal'] | None = None
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
    priority: Literal['minimal','low','medium','high','extra','ultra','critical','normal'] = 'medium'
    video_order: Literal['newest','oldest','most_viewed','least_viewed','longest','shortest'] = 'newest'
    backfill_order: Literal['newest','oldest','most_viewed','least_viewed','longest','shortest'] = 'newest'
    instant_download: bool = False
    tool: Literal['download_audio','download_video'] = 'download_video'
    types: list[Literal['video','short','live','replay','upcoming','premiere','clip','podcast','unclassified']] = Field(default_factory=lambda:list(DEFAULT_TYPES),max_length=9)
    live_action: Literal['notify','capture','replay','ignore'] = 'replay'
    directory: str = Field('',max_length=2000)
    subfolders: bool = True
    type_subfolders: bool = True
    options: Download = Field(default_factory=lambda:Download(format='mp4'))
    rules: dict[str,TypeRule] = Field(default_factory=dict,max_length=9)
    auto_retry: bool = False

    @field_validator('video_order','backfill_order',mode='before')
    @classmethod
    def order_aliases(cls,value):
        return ORDER_ALIASES.get(value,value) if isinstance(value,str) else value

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
    priority: Literal['minimal','low','medium','high','extra','ultra','critical','normal'] = 'medium'
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
        root=self.preview(path,channel=channel,kind=kind,subfolders=subfolders,type_subfolders=type_subfolders,platform=platform)
        return writable_directory(str(root),create=True)
    def preview(self,path='',*,channel='',kind='',subfolders=False,type_subfolders=False,platform='YouTube'):
        settings=self.snapshot()
        root=absolute_path(path or settings['last'] or settings['default'],directory=True,must_exist=False)
        if subfolders:
            root=root/safe_name(platform)/safe_name(channel or 'Manual')
        if type_subfolders and kind:
            root=root/TYPE_DIRS.get(kind,'Videos')
        return root


class DownloadStudio:
    def __init__(self,store,manager,adapter=None,*,planner=None):
        self.store,self.manager=store,manager
        self.planner=planner or getattr(manager,'download_planner',None)
        self.adapter=adapter or ADAPTERS['youtube']()
        self.thumbnails=ThumbnailCache(store.data)
        self.destinations=Destinations(store)
        self.lock=threading.RLock()
        self.busy=set()
        self.threads=[]
        self.started=False
        with store.lock:
            store.db.executescript('''
            CREATE TABLE IF NOT EXISTS watch_sources (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS watch_media (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS watch_discovery (source_id TEXT NOT NULL, media_id TEXT NOT NULL,
                PRIMARY KEY(source_id,media_id));
            CREATE INDEX IF NOT EXISTS watch_discovery_media ON watch_discovery(media_id);
            CREATE INDEX IF NOT EXISTS watch_media_status ON watch_media(json_extract(data,'$.status'));
            CREATE INDEX IF NOT EXISTS watch_media_published ON watch_media(json_extract(data,'$.published'));
            ''')
            store.db.commit()
        if self.planner:
            self._migrate_sources()

    def _migrate_sources(self):
        """Preserve already approved V0.7 automation and stable archive folders."""
        for source in self.sources():
            previous=source.get('config',{})
            config=WatchConfig.model_validate({**previous,'instant_download':previous.get('instant_download',True)}).model_dump()
            config['priority']=PRIORITY_ALIASES.get(config['priority'],config['priority'])
            if not config['directory']: config['directory']=str(self.destinations.preview())
            source['config']=config
            source.setdefault('handle','')
            self.put('watch_sources',source['id'],source)
            self._configure_planner(source)

    def _configure_planner(self,source):
        if self.planner:
            config=source['config']
            self.planner.configure_source(source['id'],priority=config['priority'],ordering=config.get('video_order','newest'),
                backfill_ordering=config.get('backfill_order','newest'),
                enabled=config['enabled'] and not self.store.get('watch_paused',False)
                    and source.get('status') not in {'offline','access_required','error'},
                auto_retry=config.get('auto_retry',False))

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

    def media(self,source='',limit=200,offset=0,*,status='',search='',order='newest'):
        return self.media_page(source,limit,offset,status=status,search=search,order=order)['media']

    def media_page(self,source='',limit=200,offset=0,*,status='',search='',order='newest'):
        limit=max(1,min(500,limit));offset=max(0,min(1_000_000,offset))
        aliases={'active':('queued','running','cancelling','finalizing','scheduled','downloading'),
                 'completed':('completed','archived'),'downloaded':('completed','archived'),
                 'review_required':('review',),'ready':('ready','approved'),
                 'interrupted':('interrupted','cancelled')}
        selected=aliases.get(status,(status,)) if status and status!='all' else ()
        if selected and any(s not in ACTIVE|{'scheduled','downloading','ready','approved','review','failed','completed','archived','skipped','waiting','interrupted','cancelled','unavailable'} for s in selected):
            raise ValueError('Choose a registered media status filter.')
        order=ORDER_ALIASES.get(order,order)
        published="COALESCE(json_extract(m.data,'$.published'),CAST(strftime('%s',substr(json_extract(m.data,'$.upload_date'),1,4)||'-'||substr(json_extract(m.data,'$.upload_date'),5,2)||'-'||substr(json_extract(m.data,'$.upload_date'),7,2)) AS REAL))"
        sorting={'newest':(published,'DESC'),'oldest':(published,'ASC'),
                 'most_viewed':("json_extract(m.data,'$.view_count')",'DESC'),
                 'least_viewed':("json_extract(m.data,'$.view_count')",'ASC'),
                 'longest':("json_extract(m.data,'$.duration')",'DESC'),
                 'shortest':("json_extract(m.data,'$.duration')",'ASC')}
        if order not in sorting: raise ValueError('Choose a registered video order.')
        expression,direction=sorting[order]
        join='JOIN watch_discovery d ON d.media_id=m.id ' if source else ''
        clauses=[];parameters=[]
        if source: clauses.append('d.source_id=?');parameters.append(source)
        if selected:
            clauses.append("json_extract(m.data,'$.status') IN ("+','.join('?' for _ in selected)+')');parameters.extend(selected)
        if search:
            search=search[:200]
            pattern='%'+search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
            clauses.append("(json_extract(m.data,'$.title') LIKE ? ESCAPE '\\' OR m.id LIKE ? ESCAPE '\\')");parameters.extend((pattern,pattern))
        where='WHERE '+' AND '.join(clauses)+' ' if clauses else ''
        with self.store.lock:
            total=self.store.db.execute('SELECT COUNT(*) FROM watch_media m '+join+where,parameters).fetchone()[0]
            rows=self.store.db.execute('SELECT m.data FROM watch_media m '+join+where+
                f'ORDER BY ({expression}) IS NULL ASC, {expression} {direction}, m.id ASC LIMIT ? OFFSET ?',
                (*parameters,limit,offset)).fetchall()
        values=[]
        for row in rows:
            value=json.loads(row[0])
            value['views']=value.get('view_count')
            if self.planner:
                try:
                    plan=self.planner.get_media(value['key'])
                    value.update(plan_id=plan['id'],plan_state=plan['state'],status=plan['state'])
                    for key in ('job_id','outputs','bytes','execution_seconds','stage_seconds','finished','attempts','views'):
                        if key in plan: value[key]=plan[key]
                except ValueError: pass
            value['thumbnail_preview']=self.thumbnail_preview(value.get('thumbnail',''))
            values.append(value)
        return {'media':values,'total':total,'limit':limit,'offset':offset,'has_more':offset+len(values)<total}

    @staticmethod
    def thumbnail_preview(url):
        return '/api/downloads/thumbnail?url='+quote(url,safe='') if approved_thumbnail_url(url) else ''

    def snapshot(self):
        if not self.planner: self.reconcile()
        sources=[]
        with self.store.lock:
            grouped=self.store.db.execute("SELECT d.source_id,json_extract(m.data,'$.status'),COUNT(*) FROM watch_discovery d JOIN watch_media m ON m.id=d.media_id GROUP BY d.source_id,json_extract(m.data,'$.status')").fetchall()
        source_counts={}
        for source_id,status,count in grouped: source_counts.setdefault(source_id,{})[status]=count
        for source in self.sources():
            public=json.loads(json.dumps(source))
            public['authentication_configured']=bool(public['config']['options'].pop('cookies',''))
            counts=source_counts.get(source['id'],{})
            public['counts']={'discovered':sum(counts.values()),'downloaded':counts.get('completed',0)+counts.get('archived',0),
                              'pending':sum(counts.get(s,0) for s in ACTIVE|{'waiting','ready','approved','scheduled','downloading','interrupted'}),
                              'ready':counts.get('ready',0)+counts.get('approved',0),
                              'active':sum(counts.get(s,0) for s in ACTIVE|{'scheduled','downloading'}),
                              'interrupted':counts.get('interrupted',0),'failed':counts.get('failed',0),
                              'skipped':counts.get('skipped',0),'review':counts.get('review',0),'unavailable':counts.get('unavailable',0)}
            public['busy']=source['id'] in self.busy
            status=source.get('status','ready')
            if self.store.get('watch_paused',False) or not source['config']['enabled'] or self.manager.stop_event.is_set() or self.planner and not self.started:
                status='paused'
            elif public['busy']: status='checking'
            elif status=='ready' and source.get('last_success'): status='monitoring'
            public['monitoring_state']='authentication_required' if status=='access_required' else status
            public['status']=status
            if status in {'paused','access_required','error'}: public['next_check']=None
            public['thumbnail_preview']=self.thumbnail_preview(source.get('thumbnail',''))
            if public.get('latest'):
                public['latest']={**public['latest'],'views':public['latest'].get('view_count'),
                                  'thumbnail_preview':self.thumbnail_preview(public['latest'].get('thumbnail',''))}
            sources.append(public)
        return {'sources':sources,'paused':self.store.get('watch_paused',False),
                'defaults':self.store.get('watch_defaults',WatchConfig().model_dump()),
                'capabilities':self.adapter.capabilities,'content_types':CONTENT_TYPES}

    def register(self,request):
        if len(self.sources())>=1000: raise ValueError('Keep at most 1,000 watched sources.')
        config=request.config.model_dump()
        if self.planner:
            config['priority']=PRIORITY_ALIASES.get(config['priority'],config['priority'])
            if not config['directory']: config['directory']=str(self.destinations.preview())
        result=self.adapter.inspect(request.url,limit=200,cookies=config['options']['cookies'])
        key=self.adapter.platform+':'+result['source_id']
        with self.lock:
            if any(s['id']==key for s in self.sources()):
                raise ValueError('This channel or playlist is already monitored, including renamed handles.')
            folder=channel_folder(result['title'],result['source_id']) if self.planner else safe_name(result['source_id'])
            source={'id':key,'platform':self.adapter.platform,'url':result['url'],'title':result['title'],'handle':result.get('handle',''),
                    'folder':folder,'config':config,'created':time.time(),
                    'last_success':None,'next_check':0,'failures':0,'status':'ready','archive_offset':0}
            self.put('watch_sources',key,source)
            self._configure_planner(source)
            self.ingest(source,result,initial=True)
        return self.snapshot()

    def configure(self,key,changes):
        with self.lock:
            source=self.get('watch_sources',key)
            previous=source['config']
            if 'options' in changes:
                changes={**changes,'options':{**source['config']['options'],**changes['options']}}
            source['config']=WatchConfig.model_validate({**source['config'],**changes}).model_dump()
            if self.planner: source['config']['priority']=PRIORITY_ALIASES.get(source['config']['priority'],source['config']['priority'])
            config=source['config']
            resumed=config['enabled'] and not previous['enabled']
            authentication_changed=config['options'].get('cookies','')!=previous['options'].get('cookies','')
            if not config['enabled']:
                source['status']='paused'
            elif resumed or authentication_changed:
                source.update(status='ready',next_check=time.time())
            elif config['interval_minutes']!=previous['interval_minutes'] and source.get('status') not in {'access_required','error'}:
                next_check=(source.get('last_success') or time.time())+config['interval_minutes']*60
                source['next_check']=max(source.get('next_check',0),next_check) if source.get('status')=='offline' else next_check
            self.put('watch_sources',key,source)
            self._configure_planner(source)
        return self.snapshot()

    def start(self):
        if self.started: return
        self.started=True
        def loop():
            # Immediate asynchronous checks on each launch, then bounded polling.
            startup_due={s['id'] for s in self.sources() if s.get('status') not in {'access_required','error'}}
            while not self.manager.stop_event.is_set():
                if not self.store.get('watch_paused',False):
                    for source in self.sources():
                        if source['config']['enabled'] and source.get('status') not in {'access_required','error'} and (source['id'] in startup_due or source.get('next_check',0)<=time.time()):
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
                              status='offline' if details['retryable'] else 'access_required' if details.get('code')=='authentication' else 'error')
                source['next_check']=time.time()+min(86400,900*2**min(source['failures'],6))
                self.put('watch_sources',key,source)
                self._configure_planner(source)

    def ingest(self,source,result,*,initial=False,archive=False):
        config=source['config'];now=time.time()
        for index,item in enumerate(result['entries']):
            key=self.adapter.platform+':'+item['id']
            try: previous=self.get('watch_media',key)
            except ValueError: previous=None
            value={**(previous or {}),**item,'key':key,'source_id':(previous or {}).get('source_id',source['id']),
                   'discovered':(previous or {}).get('discovered',now),'status':(previous or {}).get('status','review'),
                   'attempts':(previous or {}).get('attempts',0),'priority':(previous or {}).get('priority',config['priority'])}
            # Sparse flat metadata does not erase previously observed values.
            # Unknown fields stay None/empty until the provider reports them.
            for field in ('duration','published','view_count','upload_date','thumbnail','channel_id','channel','handle'):
                if item.get(field) in (None,'') and previous and previous.get(field) not in (None,''):
                    value[field]=previous[field]
            if self.planner: value.setdefault('historical',bool(initial or archive))
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
                value['status']='waiting' if item['type'] in {'upcoming','live'} else 'skipped' if policy=='future' and (not self.planner or config.get('instant_download')) else 'review'
                value['reason']='Initial import: future uploads only.' if policy=='future' else 'Historical media awaits review.'
                self.put('watch_media',key,value)
            transition=previous and previous.get('type') in {'live','upcoming'} and item['type'] not in {'live','upcoming'} and previous.get('status') in {'waiting','review','unavailable'}
            if self.planner:
                if transition and not initial and not archive:
                    value['historical']=False
                    self.put('watch_media',key,value)
                fresh=not previous or transition
                automatic=bool(config.get('instant_download') and eligible and not archive and fresh)
                if fresh:
                    # Enabling Instant later does not approve historical review
                    # rows. Only a fresh discovery/availability event can do so.
                    self._plan_media(value,source,approved=automatic,automatic=True,historical=bool(initial or archive))
                else:
                    # Preserve immutable active/completed options while keeping
                    # newly available metadata and source associations current.
                    self._plan_media(value,source,approved=False,automatic=True,historical=value.get('historical',False))
                continue
            if archive:
                continue  # Historical paging is always review-only.
            if not previous and eligible or transition:
                value['auto_pending']=True
                self.put('watch_media',key,value)
                if config['enabled'] and not self.store.get('watch_paused',False):
                    self.queue_media(key,source=source,automatic=True)
        source.update(title=result['title'],handle=result.get('handle') or source.get('handle',''),thumbnail=result.get('thumbnail') or source.get('thumbnail',''),thumbnail_data=result.get('thumbnail_data') or source.get('thumbnail_data',''),reported_count=result.get('reported_count'),
                      reported_count_scope=result.get('reported_count_scope',''),last_success=now,next_check=now+config['interval_minutes']*60,
                      failures=0,status='monitoring' if config['enabled'] else 'paused',error=None,
                      latest=latest_known_upload(source.get('latest'),result['entries']),has_more=result.get('has_more',False))
        if archive: source['archive_offset']=result.get('offset',0)
        self.put('watch_sources',source['id'],source)
        self._configure_planner(source)

    def _plan_media(self,record,source,*,approved=False,automatic=False,historical=False):
        config=source['config'];kind=record.get('type','unclassified');rule=config['rules'].get(kind,{})
        try: existing=self.planner.get_media(record['key'])
        except ValueError: existing=None
        if existing and existing['approved'] and not approved:
            automatic=existing['automatic']
        enabled=rule.get('enabled') if rule.get('enabled') is not None else kind in config['types']
        unavailable=kind=='upcoming' or kind=='live' and config['live_action']!='capture'
        if kind=='live' and existing and existing['origin']!='channel':
            # A channel's replay-only policy cannot undo an individually
            # requested live capture in the stronger manual priority class.
            unavailable=False
        if unavailable or automatic and not enabled: approved=False
        options=Download.model_validate({**config['options'],**rule.get('options',{}),
            'playlist':False,'thumbnail':False,'capture_live':kind=='live' and config['live_action']=='capture'}).model_dump()
        tool='download_audio' if options['format'] in {'mp3','m4a','flac','wav'} else 'download_video'
        folder=self.destinations.preview(rule.get('directory') or config['directory'],channel=source['folder'],
            kind='audio' if tool=='download_audio' else kind,subfolders=config['subfolders'],type_subfolders=config['type_subfolders'])
        priority=record.get('priority_override') or rule.get('priority') or config['priority']
        payload={'tool':tool,'options':options,'settings':self.store.settings(),'destination':str(folder),'source':None,
                 'url':record['url'],'watch_media':record['key'],'priority':priority,
                 'resume_key':record['key']+hashlib.sha256(json.dumps(options,sort_keys=True).encode()).hexdigest()}
        metadata={**record,'platform':source.get('platform',self.adapter.platform),
                  'auto_retry':config.get('auto_retry',False)}
        if record.get('priority_override') or rule.get('priority'):
            metadata['priority_override']=record.get('priority_override') or rule['priority']
        if unavailable: metadata['eligible']=False
        else: metadata['eligible']=True
        plan=self.planner.upsert(metadata,origin='channel',source_id=source['id'],source_title=source['title'],
            priority=priority,ordering=config.get('video_order','newest'),approved=approved,payload=payload,
            tool=tool,destination=str(folder),automatic=automatic,historical=historical)
        if (record['status']=='skipped' or kind=='live' and config['live_action']=='ignore') and plan['state'] not in {'completed','scheduled','downloading'} and plan['origin']=='channel':
            self.planner.action([plan['id']],'skip')
        return self.get('watch_media',record['key'])

    def queue_media(self,key,*,source=None,automatic=False):
        if self.planner:
            record=self.get('watch_media',key)
            source=source or self.get('watch_sources',record['source_id'])
            if not automatic:
                self.manager.check_accepting()
                if self.manager.require_feature:
                    self.manager.require_feature(source['config']['tool'])
            if record['status'] in {'completed','archived'}: return record
            if automatic and not source['config'].get('instant_download',False): return record
            if not automatic and record['status']=='skipped':
                record['status']='review';self.put('watch_media',key,record)
            value=self._plan_media(record,source,approved=True,automatic=automatic,historical=record.get('historical',False))
            plan=self.planner.get_media(key)
            if plan['state'] in {'failed','interrupted'}:
                self.planner.action([plan['id']],'retry')
                value=self.get('watch_media',key)
            return value
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
        if self.planner:
            return self.planner.reconcile()
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
        if self.planner:
            # Planner owns bounded recovery and active job reconciliation. It
            # never approves review items as a side effect of startup/polling.
            return
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
        if self.planner and body.action in {'priority','skip'}:
            plan=self.planner.get_media(body.key)
            if plan['state']=='completed':
                raise ValueError('Verified completed media remains in download history.')
            if body.action=='priority':
                if plan['state'] in {'completed','scheduled','downloading'}:
                    raise ValueError('Only pending media can change priority; active and completed history is preserved.')
                self.planner.action([plan['id']],'change_priority',priority=body.priority)
            else: self.planner.action([plan['id']],'skip')
            return self.get('watch_media',body.key)
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
        limit=body.get('limit',25);offset=body.get('offset',0)
        if type(limit) is not int or type(offset) is not int or not 1<=limit<=200 or not 0<=offset<=10000:
            raise ValueError('Choose a preview page of 1–200 entries and a valid offset.')
        value=studio.adapter.inspect(body['url'],watch=False,playlist=body.get('playlist') is True,limit=limit,offset=offset,cookies=body.get('cookies',''))
        for item in value.get('entries',[]): item['thumbnail_preview']=studio.thumbnail_preview(item.get('thumbnail',''))
        return value

    @app.get('/api/downloads/thumbnail')
    def thumbnail(url:str=''):
        from fastapi.responses import Response
        raw=studio.thumbnails.get(url)
        return Response(content=raw,status_code=200 if raw else 404,media_type='image/png',
                        headers={'Cache-Control':'private, max-age=600','X-Content-Type-Options':'nosniff'})

    @app.get('/api/downloads/diagnostics')
    def check_downloads(): return diagnostics()

    @app.get('/api/watch')
    def watch(): return studio.snapshot()

    @app.post('/api/watch')
    def add_watch(body:WatchRequest):
        manager.require_feature(body.config.tool)
        return studio.register(body)

    @app.get('/api/watch/media')
    def media(source:str='',limit:int=200,offset:int=0,status:str='',search:str='',order:str='newest'):
        if not studio.planner: studio.reconcile()
        return studio.media_page(source,limit,offset,status=status,search=search,order=order)

    @app.post('/api/watch/approve')
    def approve_review(body:dict):
        source=studio.get('watch_sources',body.get('id',''))
        if not studio.planner: raise ValueError('Persistent download planning is unavailable.')
        manager.check_accepting()
        if manager.require_feature: manager.require_feature(source['config']['tool'])
        return studio.planner.approve_channel_review(source['id'])

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
        store.set('watch_paused',body['paused'])
        for source in studio.sources(): studio._configure_planner(source)
        return {'paused':body['paused']}

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
