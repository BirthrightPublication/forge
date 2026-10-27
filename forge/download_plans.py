"""Persistent media planning, separate from executable Universal Queue jobs.

All admission is transactional and bounded. Metadata/thumbnail discovery belongs to
adapters; this module never performs network requests. Final media verification
runs outside the database/scheduler critical section before a conditional commit.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid

from .core import MEDIA, executable, no_window

PRIORITIES = ('minimal', 'low', 'medium', 'high', 'extra', 'ultra')
ORDERINGS = ('newest', 'oldest', 'most_viewed', 'least_viewed', 'longest', 'shortest', 'playlist')
STATES = ('review', 'ready', 'scheduled', 'downloading', 'interrupted', 'failed', 'completed', 'skipped', 'unavailable')
ORIGINS = {'channel': 1, 'manual_playlist': 2, 'manual_single': 3}
ACTIVE = {'scheduled', 'downloading'}
PENDING = {'review', 'ready', 'interrupted', 'failed', 'unavailable'}
JOB_ACTIVE = {'queued', 'running', 'finalizing', 'cancelling'}
_PRIORITY_ALIASES = {'normal': 'medium', 'critical': 'ultra'}
_ORDER_ALIASES = {'original': 'playlist', 'newest_first': 'newest', 'oldest_first': 'oldest', 'views_desc': 'most_viewed',
                  'views_asc': 'least_viewed', 'duration_desc': 'longest', 'duration_asc': 'shortest'}
_METADATA = {'title', 'url', 'thumbnail', 'type', 'published', 'upload_date', 'duration', 'views',
             'view_count', 'channel_id', 'channel', 'availability', 'handle'}


def priority_name(value):
    value = _PRIORITY_ALIASES.get(str(value).lower(), str(value).lower())
    if value not in PRIORITIES:
        raise ValueError('Choose Minimal, Low, Medium, High, Extra or Ultra channel priority.')
    return value


def order_name(value):
    value = _ORDER_ALIASES.get(str(value).lower(), str(value).lower())
    if value not in ORDERINGS:
        raise ValueError('Choose a supported video ordering rule.')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _number(value, *, zero=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value if value >= 0 and (zero or value > 0) else None


def _identity(media):
    key = str(media.get('key') or '')
    platform = str(media.get('platform') or (key.split(':', 1)[0] if ':' in key else 'youtube')).lower()
    identity = str(media.get('media_id') or media.get('id') or (key.split(':', 1)[1] if ':' in key else ''))
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', platform) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', identity):
        raise ValueError('Media planning requires a stable platform and media identity.')
    return platform + ':' + identity, platform, identity


def _metadata(media):
    value = {key: media[key] for key in _METADATA if key in media and isinstance(media[key], (str, int, float, type(None)))}
    value['title'] = str(value.get('title') or media.get('media_id') or media.get('id') or 'Media')[:400]
    value['url'] = str(value.get('url') or '')[:8000]
    value['published'] = _number(value.get('published'), zero=False)
    date = str(value.get('upload_date') or '')[:8]
    if value['published'] is None and re.fullmatch(r'\d{8}', date):
        try:
            value['published'] = datetime.strptime(date, '%Y%m%d').replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    value['upload_date'] = date
    value['duration'] = _number(value.get('duration'), zero=False)
    value['views'] = _number(value.get('views', value.get('view_count')))
    value.pop('view_count', None)
    # This is a display reference, never permission to fetch arbitrary hosts.
    thumbnail = str(value.get('thumbnail') or '')[:4000]
    try:
        parts = urlsplit(thumbnail)
        allowed = parts.scheme == 'https' and parts.hostname in {
            'i.ytimg.com', 'i9.ytimg.com', 'img.youtube.com', 'yt3.ggpht.com', 'yt3.googleusercontent.com'}
        allowed = allowed and not parts.username and not parts.password and parts.port in {None, 443}
    except ValueError:
        allowed = False
    value['thumbnail'] = thumbnail if allowed else ''
    for key in ('duration', 'views', 'published'):
        value[key] = _number(value.get(key), zero=key == 'views')
    return value


def _digest(path, stopped=None):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            if stopped is not None and stopped.is_set():
                raise InterruptedError('Verification stopped with FORGE.')
            value.update(block)
    return value.hexdigest()


class DownloadPlanner:
    def __init__(self, store, manager=None):
        self.store, self.manager, self.studio = store, manager, None
        self._last_reconcile = 0.0
        self._reconcile_lock = threading.RLock()
        self._verification_lock = threading.RLock()
        self._verification_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='forge-media-verification')
        self._verifications = {}
        self._verification_waiting = 0
        self._verification_processes = set()
        self._closed = threading.Event()
        with store.lock:
            store.db.executescript('''
                CREATE TABLE IF NOT EXISTS download_plan_items (
                    id TEXT PRIMARY KEY, media_key TEXT NOT NULL UNIQUE, platform TEXT NOT NULL,
                    media_id TEXT NOT NULL, origin TEXT NOT NULL, class_rank INTEGER NOT NULL,
                    source_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, approved INTEGER NOT NULL,
                    channel_priority INTEGER NOT NULL DEFAULT 2, ordering TEXT NOT NULL DEFAULT 'newest',
                    historical INTEGER NOT NULL DEFAULT 0, automatic INTEGER NOT NULL DEFAULT 0,
                    position REAL NOT NULL DEFAULT 0, boost INTEGER NOT NULL DEFAULT 0,
                    published REAL, views REAL, duration REAL, job_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
                    resume_allowed INTEGER NOT NULL DEFAULT 1, removed INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS download_plan_job ON download_plan_items(job_id) WHERE job_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS download_plan_dispatch ON download_plan_items(state,approved,removed,class_rank DESC,channel_priority DESC,next_retry);
                CREATE INDEX IF NOT EXISTS download_plan_source ON download_plan_items(source_id,state,removed);
                CREATE INDEX IF NOT EXISTS download_plan_newest ON download_plan_items(source_id,state,published DESC);
                CREATE INDEX IF NOT EXISTS download_plan_views ON download_plan_items(source_id,state,views DESC);
                CREATE INDEX IF NOT EXISTS download_plan_duration ON download_plan_items(source_id,state,duration);
                CREATE TABLE IF NOT EXISTS download_plan_sources (
                    plan_id TEXT NOT NULL, source_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(plan_id,source_id)
                );
                CREATE INDEX IF NOT EXISTS download_plan_source_items ON download_plan_sources(source_id,plan_id);
                CREATE TABLE IF NOT EXISTS download_plan_channels (
                    source_id TEXT PRIMARY KEY, priority INTEGER NOT NULL DEFAULT 2,
                    ordering TEXT NOT NULL DEFAULT 'newest', backfill_ordering TEXT NOT NULL DEFAULT 'newest',
                    enabled INTEGER NOT NULL DEFAULT 1, last_turn INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS download_plan_playlists (id TEXT PRIMARY KEY,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS download_plan_batches (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, skipped_resolved INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS download_plan_batch_members (
                    batch_id TEXT NOT NULL, plan_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    PRIMARY KEY(batch_id,plan_id)
                );
            ''')
            store.db.execute("INSERT OR IGNORE INTO download_plan_playlists SELECT COALESCE(NULLIF(json_extract(data,'$.playlist_id'),''),NULLIF(source_id,''),'playlist'),MIN(created) FROM download_plan_items WHERE origin='manual_playlist' GROUP BY 1")
            store.db.commit()
        self.migrate_legacy()

    @contextmanager
    def _transaction(self):
        with self.store.lock:
            nested = self.store.db.in_transaction
            savepoint = 'download_plan_' + uuid.uuid4().hex
            self.store.db.execute('SAVEPOINT ' + savepoint if nested else 'BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                if nested:
                    self.store.db.execute('ROLLBACK TO ' + savepoint)
                    self.store.db.execute('RELEASE ' + savepoint)
                else:
                    self.store.db.rollback()
                raise
            else:
                if nested:
                    self.store.db.execute('RELEASE ' + savepoint)
                else:
                    self.store.db.commit()

    def upsert_many(self, requests):
        if not isinstance(requests, list) or not 1 <= len(requests) <= 1000:
            raise ValueError('Submit between one and 1,000 selected media items.')
        with self._transaction():
            return [self.upsert(**request) for request in requests]

    def _row(self, identity):
        row = self.store.db.execute('SELECT * FROM download_plan_items WHERE id=? OR media_key=?', (identity, identity)).fetchone()
        if row is None:
            raise ValueError('Planned media was not found.')
        return dict(row)

    @staticmethod
    def _public(row):
        data = json.loads(row['data'])
        result = {key: data.get(key) for key in _METADATA if key != 'view_count'}
        result.update({key: row[key] for key in ('id', 'platform', 'media_id', 'origin', 'source_id', 'state',
                                                'ordering', 'position', 'job_id', 'attempts', 'created', 'updated')})
        result.update(key=row['media_key'], channel_priority=PRIORITIES[row['channel_priority']],
                      approved=bool(row['approved']), automatic=bool(row['automatic']),
                      historical=bool(row['historical']), paused=not bool(row['resume_allowed']),
                      source_title=data.get('source_title', ''), destination=data.get('destination', ''),
                      outputs=data.get('outputs', []), bytes=data.get('bytes', 0), reason=data.get('reason', ''),
                      execution_seconds=data.get('execution_seconds'), stage_seconds=data.get('stage_seconds', {}),
                      finished=data.get('finished'), planned_order=data.get('planned_order'),
                      next_retry=row['next_retry'])
        return result

    def get(self, identity):
        with self.store.lock:
            return self._public(self._row(identity))

    def get_media(self, key):
        return self.get(key)

    def payload_for(self, identity):
        with self.store.lock:
            return copy.deepcopy(json.loads(self._row(identity)['payload']))

    def _associate(self, identity, source_id, title=''):
        if source_id:
            self.store.db.execute('INSERT INTO download_plan_sources VALUES (?,?,?) ON CONFLICT(plan_id,source_id) DO UPDATE SET title=excluded.title',
                                  (identity, source_id, title[:400]))

    def upsert(self, media, *, origin='channel', source_id='', source_title='', priority='medium', ordering='newest',
               approved=False, payload=None, tool='download_video', destination='', automatic=False,
               playlist_id='', position=0, historical=False):
        if origin not in ORIGINS:
            raise ValueError('Choose an individual, playlist or watched-channel plan.')
        if tool not in {'download_audio', 'download_video'}:
            raise ValueError('Only media downloads belong to download planning.')
        key, platform, identity = _identity(media)
        rank, priority, ordering = ORIGINS[origin], priority_name(priority), order_name(ordering)
        if isinstance(position, bool) or not isinstance(position, (int, float)) or not math.isfinite(position) or abs(position) > 1e12:
            raise ValueError('Choose a finite planned position.')
        metadata = _metadata(media)
        if 'priority_override' in media and media['priority_override']:
            metadata['priority_override'] = priority_name(media['priority_override'])
        if isinstance(media.get('eligible'), bool):
            metadata['eligible'] = media['eligible']
        template = copy.deepcopy(payload or {})
        template.setdefault('tool', tool)
        template.setdefault('url', metadata['url'])
        if destination:
            template['destination'] = str(destination)
        now = time.time()
        with self._transaction():
            if origin == 'manual_playlist':
                self.store.db.execute('INSERT OR IGNORE INTO download_plan_playlists VALUES (?,?)', (playlist_id or source_id or 'playlist', now))
            existing = self.store.db.execute('SELECT * FROM download_plan_items WHERE media_key=?', (key,)).fetchone()
            if existing:
                row = dict(existing)
                data = json.loads(row['data'])
                # Sparse/lower-rank discoveries cannot overwrite explicit manual policy.
                policy_keys = {'eligible', 'priority_override'}
                policy_allowed = row['state'] not in ACTIVE | {'completed'} and rank >= row['class_rank']
                data.update({k: v for k, v in metadata.items() if v is not None and v != '' and (policy_allowed or k not in policy_keys)})
                if policy_allowed and isinstance(media.get('auto_retry'), bool):
                    data['auto_retry'] = media['auto_retry']
                self._associate(row['id'], source_id, source_title)
                pending = row['state'] not in ACTIVE | {'completed'}
                stronger = rank > row['class_rank']
                if pending and (stronger or (rank == row['class_rank'] and source_id == row['source_id'])):
                    old_payload = json.loads(row['payload'])
                    if payload is not None:
                        template.setdefault('watch_media', old_payload.get('watch_media'))
                        row['payload'] = _json(template)
                    row.update(origin=origin, class_rank=rank, ordering=ordering,
                               position=float(position), automatic=int(automatic) if approved else row['automatic'], historical=0 if row['state'] == 'unavailable' and metadata.get('eligible') is True and not historical else int(historical or row['historical']))
                    if source_id:
                        row['source_id'] = source_id
                    data['source_title'] = source_title or data.get('source_title', '')
                    data['destination'] = template.get('destination') or data.get('destination', '')
                if pending and approved:
                    row.update(approved=1, state='ready', next_retry=0, resume_allowed=1, removed=0)
                    data['reason'] = 'Approved and waiting for scheduler capacity.'
                if pending and row['state'] != 'skipped' and data.get('eligible') is False:
                    row.update(state='unavailable', approved=0)
                    data['reason'] = 'Tracked media is not currently available for an eligible download.'
                elif row['state'] == 'unavailable' and data.get('eligible') is True:
                    row['state'] = 'ready' if approved else 'review'
                row.update(data=_json(data), updated=now, published=data.get('published'),
                           views=data.get('views'), duration=data.get('duration'))
                self._save(row)
                self._refresh_channels([row['id']])
                return self._public(self._row(row['id']))
            identity = uuid.uuid4().hex
            metadata.update(source_title=source_title[:400], destination=template.get('destination', ''),
                            playlist_id=playlist_id[:200], reason='Approved and waiting for scheduler capacity.' if approved else 'Review required.',
                            outputs=[], bytes=0, auto_retry=bool(media.get('auto_retry', False)))
            row = dict(id=identity, media_key=key, platform=platform, media_id=key.split(':', 1)[1], origin=origin,
                       class_rank=rank, source_id=source_id, state='unavailable' if metadata.get('eligible') is False else 'ready' if approved else 'review', approved=int(approved and metadata.get('eligible') is not False),
                       channel_priority=PRIORITIES.index(priority), ordering=ordering, historical=int(historical),
                       automatic=int(automatic), position=float(position), boost=0, published=metadata.get('published'),
                       views=metadata.get('views'), duration=metadata.get('duration'), job_id=None, attempts=0,
                       next_retry=0, resume_allowed=1, removed=0, created=now, updated=now,
                       data=_json(metadata), payload=_json(template))
            self._save(row, insert=True)
            self._associate(identity, source_id, source_title)
            if origin == 'channel':
                self.store.db.execute('INSERT OR IGNORE INTO download_plan_channels(source_id,priority,ordering,backfill_ordering) VALUES (?,?,?,?)',
                                      (source_id, PRIORITIES.index(priority), ordering, ordering))
            if origin == 'channel':
                self._refresh_channels([identity])
            return self._public(self._row(identity))

    def _save(self, row, insert=False):
        keys = tuple(key for key in row if not key.startswith('_'))
        if insert:
            self.store.db.execute('INSERT INTO download_plan_items (' + ','.join(keys) + ') VALUES (' + ','.join('?' for _ in keys) + ')', tuple(row[key] for key in keys))
        else:
            keys = tuple(key for key in row if key != 'id' and not key.startswith('_'))
            self.store.db.execute('UPDATE download_plan_items SET ' + ','.join(key + '=?' for key in keys) + ' WHERE id=?',
                                  tuple(row[key] for key in keys) + (row['id'],))
        self._sync_watch_locked(row)

    def _refresh_channels(self, identities=None):
        restriction = ''
        args = []
        if identities is not None:
            if not identities:
                return
            restriction = ' AND p.id IN (' + ','.join('?' for _ in identities) + ')'
            args = identities
        rows = self.store.db.execute('''SELECT p.id,c.source_id,c.priority,c.ordering,c.backfill_ordering,s.title,
                ROW_NUMBER() OVER (PARTITION BY p.id ORDER BY c.enabled DESC,c.priority DESC,c.source_id) AS n
            FROM download_plan_items p JOIN download_plan_sources s ON s.plan_id=p.id
            JOIN download_plan_channels c ON c.source_id=s.source_id
            WHERE p.origin='channel' AND p.state IN ('review','ready','interrupted','failed','unavailable')''' + restriction,
            args).fetchall()
        for value in rows:
            if value['n'] != 1:
                continue
            row = self._row(value['id'])
            data = json.loads(row['data']); data['source_title'] = value['title']
            row.update(source_id=value['source_id'], channel_priority=PRIORITIES.index(data['priority_override']) if data.get('priority_override') else value['priority'],
                       ordering=data.get('ordering_override') or (value['backfill_ordering'] if row['historical'] else value['ordering']), data=_json(data))
            self._save(row)

    def configure_source(self, source_id, *, priority=None, ordering=None, backfill_ordering=None, enabled=None, auto_retry=None):
        if not isinstance(source_id, str) or not source_id or len(source_id) > 240:
            raise ValueError('Choose a saved source.')
        with self._transaction():
            self.store.db.execute('INSERT OR IGNORE INTO download_plan_channels(source_id) VALUES (?)', (source_id,))
            values = {}
            if priority is not None:
                values['priority'] = PRIORITIES.index(priority_name(priority))
            if ordering is not None:
                values['ordering'] = order_name(ordering)
            if backfill_ordering is not None:
                values['backfill_ordering'] = order_name(backfill_ordering)
            if enabled is not None:
                if not isinstance(enabled, bool):
                    raise ValueError('Choose whether source scheduling is enabled.')
                values['enabled'] = int(enabled)
            if values:
                self.store.db.execute('UPDATE download_plan_channels SET ' + ','.join(k + '=?' for k in values) + ' WHERE source_id=?',
                                      tuple(values.values()) + (source_id,))
            identities = [row[0] for row in self.store.db.execute('SELECT plan_id FROM download_plan_sources WHERE source_id=?', (source_id,))]
            self._refresh_channels(identities)
            if auto_retry is not None:
                if not isinstance(auto_retry, bool):
                    raise ValueError('Choose whether automatic retries are enabled.')
                self.store.db.execute("UPDATE download_plan_items SET data=json_set(data,'$.auto_retry',json(?)) WHERE origin='channel' AND state IN ('review','ready','failed','interrupted','unavailable') AND id IN (SELECT plan_id FROM download_plan_sources WHERE source_id=?)", ('true' if auto_retry else 'false', source_id))
        return dict(self.store.db.execute('SELECT * FROM download_plan_channels WHERE source_id=?', (source_id,)).fetchone())

    def _where(self, state='outstanding', source='', origin='', search='', batch='', include_removed=False):
        clauses, args = ([] if include_removed else ['p.removed=0']), []
        aliases = {'review_required': 'review', 'approved': 'ready', 'running': 'downloading'}
        state = aliases.get(state, state)
        if state in {'', 'outstanding'}:
            clauses.append("p.state NOT IN ('completed','skipped')")
        elif state == 'active':
            clauses.append("p.state IN ('scheduled','downloading')")
        elif state != 'all':
            if state not in STATES:
                raise ValueError('Choose a supported plan state.')
            clauses.append('p.state=?'); args.append(state)
        if source:
            clauses.append('EXISTS(SELECT 1 FROM download_plan_sources s WHERE s.plan_id=p.id AND s.source_id=?)'); args.append(source)
        origin = {'manual': 'manual_single', 'playlist': 'manual_playlist'}.get(origin, origin)
        if origin:
            if origin not in ORIGINS:
                raise ValueError('Choose a supported plan origin.')
            clauses.append('p.origin=?'); args.append(origin)
        if search:
            if len(search) > 300:
                raise ValueError('Keep searches under 300 characters.')
            clauses.append("(json_extract(p.data,'$.title') LIKE ? ESCAPE '\\' OR json_extract(p.data,'$.source_title') LIKE ? ESCAPE '\\' OR p.media_key LIKE ? ESCAPE '\\')")
            term = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            args.extend([term] * 3)
        if batch:
            clauses.append('EXISTS(SELECT 1 FROM download_plan_batch_members b WHERE b.plan_id=p.id AND b.batch_id=?)'); args.append(batch)
        return ' AND '.join(clauses) or '1', args

    def _preview_turns(self):
        turns = {row['source_id']: row['last_turn'] for row in self.store.db.execute('SELECT source_id,last_turn FROM download_plan_channels')}
        clock = max(turns.values(), default=0)
        groups = {}
        for row in self.store.db.execute("""SELECT p.source_id,p.channel_priority,COUNT(*) AS n
            FROM download_plan_items p LEFT JOIN download_plan_channels c ON c.source_id=p.source_id
            WHERE p.origin='channel' AND p.state='ready' AND p.approved=1 AND p.removed=0
              AND p.resume_allowed=1 AND p.next_retry<=? AND (p.automatic=0 OR COALESCE(c.enabled,1)=1)
            GROUP BY p.source_id,p.channel_priority""", (time.time(),)):
            groups.setdefault(row['channel_priority'], []).append((row['source_id'], row['n']))
        result = []
        for priority in reversed(range(len(PRIORITIES))):
            sources = sorted(groups.get(priority, []), key=lambda value: (turns.get(value[0], 0), value[0]))
            for source, _ in sources:
                result.append([source, priority, turns.get(source, 0)])
            # Closed-form final turns for deterministic round-robin drain. This
            # matters when one source has item overrides in several priorities.
            for index, (source, count) in enumerate(sources):
                prior_rounds = sum(min(size, count - 1) for _, size in sources)
                last_round = sum(size >= count for _, size in sources[:index + 1])
                turns[source] = clock + prior_rounds + last_round
            clock += sum(size for _, size in sources)
        return _json(result)

    def _ranked_query(self, where, virtual_turns=False):
        item_order = """p.boost DESC,
            CASE p.ordering WHEN 'newest' THEN p.published IS NULL WHEN 'oldest' THEN p.published IS NULL
              WHEN 'most_viewed' THEN p.views IS NULL WHEN 'least_viewed' THEN p.views IS NULL
              WHEN 'longest' THEN p.duration IS NULL WHEN 'shortest' THEN p.duration IS NULL ELSE 0 END,
            CASE p.ordering WHEN 'newest' THEN -p.published WHEN 'oldest' THEN p.published
              WHEN 'most_viewed' THEN -p.views WHEN 'least_viewed' THEN p.views
              WHEN 'longest' THEN -p.duration WHEN 'shortest' THEN p.duration ELSE p.position END,
            p.created,p.id"""
        prefix = "WITH preview_turns AS (SELECT json_extract(value,'$[0]') AS source_id,json_extract(value,'$[1]') AS priority,json_extract(value,'$[2]') AS turn FROM json_each(?))," if virtual_turns else 'WITH'
        turn = 'COALESCE(t.turn,c.last_turn,0)' if virtual_turns else 'COALESCE(c.last_turn,0)'
        turn_join = ' LEFT JOIN preview_turns t ON t.source_id=p.source_id AND t.priority=p.channel_priority' if virtual_turns else ''
        return prefix + " ranked AS (SELECT p.*," + turn + """ AS _turn,
            COALESCE(l.created,p.created) AS _group_created,
            CASE WHEN p.origin='manual_playlist' THEN COALESCE(NULLIF(json_extract(p.data,'$.playlist_id'),''),NULLIF(p.source_id,''),'playlist') ELSE '' END AS _group_key,
            ROW_NUMBER() OVER(PARTITION BY p.origin,CASE WHEN p.origin='channel' THEN p.channel_priority ELSE 0 END,
                CASE WHEN p.origin='channel' THEN p.source_id WHEN p.origin='manual_playlist' THEN COALESCE(NULLIF(json_extract(p.data,'$.playlist_id'),''),NULLIF(p.source_id,''),'playlist') ELSE p.id END,(p.state='ready')
                ORDER BY """ + item_order + """) AS _source_n
            FROM download_plan_items p LEFT JOIN download_plan_channels c ON c.source_id=p.source_id
            LEFT JOIN download_plan_playlists l ON l.id=COALESCE(NULLIF(json_extract(p.data,'$.playlist_id'),''),NULLIF(p.source_id,''),'playlist')
            """ + turn_join + ' WHERE ' + where + ')'

    @staticmethod
    def _global_order():
        return """class_rank DESC,
            CASE WHEN origin='channel' THEN channel_priority ELSE 0 END DESC,
            CASE WHEN origin='manual_playlist' THEN _group_created ELSE 0 END,
            CASE WHEN origin='channel' THEN _source_n ELSE 0 END,
            CASE WHEN origin='channel' THEN _turn ELSE 0 END,
            CASE WHEN origin='channel' THEN source_id WHEN origin='manual_playlist' THEN _group_key ELSE '' END,
            CASE WHEN origin='manual_playlist' THEN _source_n ELSE 0 END,
            CASE WHEN origin!='channel' THEN boost ELSE 0 END DESC,position,created,id"""

    def snapshot(self, state='outstanding', source='', origin='', search='', limit=100, offset=0, batch=''):
        limit, offset = max(1, min(200, int(limit))), max(0, int(offset))
        with self.store.lock:
            where, args = self._where(state, source, origin, search, batch)
            total = self.store.db.execute('SELECT COUNT(*) FROM download_plan_items p WHERE ' + where, args).fetchone()[0]
            preview = self._preview_turns()
            rows = self.store.db.execute(self._ranked_query(where, virtual_turns=True) + ' SELECT * FROM ranked ORDER BY ' + self._global_order() + ' LIMIT ? OFFSET ?',
                                        [preview] + args + [limit, offset]).fetchall()
            items = [self._public(dict(row)) for row in rows]
            ready_ids = [item['id'] for item in items if item['state'] == 'ready']
            ranks = {}
            if ready_ids:
                ready_where = "p.state='ready' AND p.approved=1 AND p.removed=0 AND p.resume_allowed=1 AND p.next_retry<=? AND (p.origin!='channel' OR p.automatic=0 OR COALESCE(c.enabled,1)=1)"
                query = self._ranked_query(ready_where, virtual_turns=True) + ', ordered AS (SELECT id,ROW_NUMBER() OVER(ORDER BY ' + self._global_order() + ') AS planned_order FROM ranked) SELECT id,planned_order FROM ordered WHERE id IN (' + ','.join('?' for _ in ready_ids) + ')'
                ranks = dict(self.store.db.execute(query, [preview, time.time()] + ready_ids).fetchall())
            for item in items:
                item['planned_order'] = ranks.get(item['id'])
            batches = [dict(row) for row in self.store.db.execute('SELECT * FROM download_plan_batches ORDER BY created DESC LIMIT 100')]
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset,
                'progress': self.progress(batch=batch), 'batches': batches,
                'priorities': list(PRIORITIES), 'orderings': list(ORDERINGS)}

    def progress(self, batch=''):
        with self.store.lock:
            if batch:
                record = self.store.db.execute('SELECT * FROM download_plan_batches WHERE id=?', (batch,)).fetchone()
                if record is None:
                    raise ValueError('Saved batch was not found.')
                where, args = self._where('all', batch=batch, include_removed=True)
                skipped_resolved = bool(record['skipped_resolved'])
            else:
                where, args = self._where('all')
                skipped_resolved = False
            counts = dict(self.store.db.execute('SELECT p.state,COUNT(*) FROM download_plan_items p WHERE ' + where + ' GROUP BY p.state', args).fetchall())
        total = sum(counts.values()); completed = counts.get('completed', 0); skipped = counts.get('skipped', 0)
        resolved = completed + (skipped if skipped_resolved else 0)
        return {'total': total, 'completed': completed, 'active': sum(counts.get(s, 0) for s in ACTIVE),
                'ready': counts.get('ready', 0), 'review': counts.get('review', 0), 'awaiting_review': counts.get('review', 0),
                'failed': counts.get('failed', 0), 'interrupted': counts.get('interrupted', 0),
                'unavailable': counts.get('unavailable', 0), 'skipped': skipped, 'resolved': resolved,
                'remaining': total - resolved, 'percent': (min(99.99, round(100 * resolved / total, 2)) if resolved < total else 100.0) if total else None,
                'skipped_resolved': skipped_resolved, 'empty': total == 0}

    def frozen_batch(self, ids, name='', skipped_resolved=False):
        if not isinstance(ids, list) or len(ids) > 10000:
            raise ValueError('Select at most 10,000 explicit planned IDs, or snapshot the full live plan.')
        identity = uuid.uuid4().hex
        with self._transaction():
            identities = list(dict.fromkeys(self._row(value)['id'] for value in ids))
            if not identities and self.store.db.execute('SELECT 1 FROM download_plan_items WHERE removed=0 LIMIT 1').fetchone() is None:
                raise ValueError('There are no planned items to snapshot.')
            self.store.db.execute('INSERT INTO download_plan_batches VALUES (?,?,?,?)', (identity, str(name).strip()[:120] or 'Download batch', int(bool(skipped_resolved)), time.time()))
            if identities:
                self.store.db.executemany('INSERT INTO download_plan_batch_members VALUES (?,?,?)', [(identity, item, index) for index, item in enumerate(identities)])
            else:
                self.store.db.execute('INSERT INTO download_plan_batch_members SELECT ?,id,ROW_NUMBER() OVER(ORDER BY created,id)-1 FROM download_plan_items WHERE removed=0', (identity,))
        return {'id': identity, 'name': str(name).strip()[:120] or 'Download batch', 'progress': self.progress(identity)}

    def _sync_watch_locked(self, row):
        if not self._has_table('watch_media'):
            return
        old = self.store.db.execute('SELECT data FROM watch_media WHERE id=?', (row['media_key'],)).fetchone()
        if old is None:
            return
        record, data = json.loads(old[0]), json.loads(row['data'])
        record.update(plan_id=row['id'], status={'scheduled': 'queued', 'downloading': 'running'}.get(row['state'], row['state']),
                      job_id=row['job_id'], attempts=row['attempts'], destination=data.get('destination', ''),
                      outputs=data.get('outputs', []), reason=data.get('reason', ''), auto_pending=False,
                      priority=PRIORITIES[row['channel_priority']], execution_seconds=data.get('execution_seconds'),
                      stage_seconds=data.get('stage_seconds', {}), bytes=data.get('bytes', 0), finished=data.get('finished'))
        self.store.db.execute('UPDATE watch_media SET data=? WHERE id=?', (_json(record), row['media_key']))

    def _has_table(self, name):
        return self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def approve_channel_review(self, source_id):
        now = time.time()
        with self._transaction():
            changed = self.store.db.execute("""UPDATE download_plan_items SET state='ready',approved=1,
                automatic=0,resume_allowed=1,updated=?,data=json_set(data,'$.reason','Channel review items explicitly approved.')
                WHERE removed=0 AND state='review' AND id IN
                (SELECT plan_id FROM download_plan_sources WHERE source_id=?)""", (now, source_id)).rowcount
            self._sync_bulk_ready(now, source_id)
        return {'changed': changed, 'progress': self.progress()}

    def _sync_bulk_ready(self, updated, source_id=None):
        if not self._has_table('watch_media'):
            return
        where = "p.updated=? AND p.state='ready'"
        args = [updated]
        if source_id is not None:
            where += ' AND EXISTS(SELECT 1 FROM download_plan_sources s WHERE s.plan_id=p.id AND s.source_id=?)'
            args.append(source_id)
        self.store.db.execute("""UPDATE watch_media SET data=json_set(data,'$.status','ready',
            '$.auto_pending',json('false'),'$.reason','Explicitly approved and waiting for scheduler capacity.')
            WHERE id IN (SELECT p.media_key FROM download_plan_items p WHERE """ + where + ')', args)

    def action(self, ids, action, **options):
        aliases = {'approve_selected': 'approve', 'start_selected': 'start', 'start_all': 'start',
                   'retry_failed': 'retry', 'skip_selected': 'skip', 'remove_selected': 'remove',
                   'change_priority': 'priority', 'change_ordering': 'ordering', 'order': 'ordering', 'now': 'download_now'}
        action = aliases.get(action, action)
        if action not in {'approve', 'start', 'retry', 'skip', 'remove', 'download_now', 'priority', 'ordering', 'first', 'up', 'down', 'pause', 'unavailable'}:
            raise ValueError('Choose a supported planning action.')
        if not isinstance(ids, list) or len(ids) > 1000 or any(not isinstance(value, str) or len(value) > 240 for value in ids):
            raise ValueError('Select up to 1,000 planned items.')
        with self._transaction():
            if not ids:
                if action not in {'start', 'retry'}:
                    raise ValueError('Select at least one planned item.')
                states = "approved=1 AND state IN ('ready','interrupted')" if action == 'start' else "state='failed'"
                now = time.time()
                changed = self.store.db.execute("UPDATE download_plan_items SET state='ready',approved=1,resume_allowed=1,automatic=0,next_retry=0,updated=?,data=json_set(data,'$.reason','Explicitly approved and waiting for scheduler capacity.') WHERE removed=0 AND COALESCE(json_extract(data,'$.eligible'),1)!=0 AND " + states, (now,)).rowcount
                self._sync_bulk_ready(now)
                return {'changed': changed, 'ids': [], 'items': [], 'progress': self.progress()}
            else:
                rows = [self._row(value) for value in dict.fromkeys(ids)]
            rows = [dict(row) for row in rows]
            if action in {'skip', 'remove', 'priority', 'ordering', 'first', 'up', 'down'} and any(row['state'] in ACTIVE for row in rows):
                raise ValueError('Active transfers keep their snapshot. Cancel them in Processing Queue before changing this plan.')
            if action in {'approve', 'start', 'retry', 'download_now', 'skip', 'priority', 'ordering', 'first', 'up', 'down', 'pause'} and any(row['state'] == 'completed' for row in rows):
                raise ValueError('Completed media is already verified; this action does not create duplicate outputs.')
            if action in {'up', 'down'} and len(rows) != 1:
                raise ValueError('Move one pending item at a time.')
            now = time.time()
            changed = []
            for row in rows:
                data = json.loads(row['data'])
                if action in {'approve', 'start', 'retry', 'download_now'}:
                    if row['state'] in ACTIVE:
                        continue
                    if data.get('eligible') is False:
                        raise ValueError('This media is not yet eligible for downloading.')
                    row.update(approved=1, state='ready', resume_allowed=1, removed=0, next_retry=0, automatic=0)
                    data['reason'] = 'Explicitly approved and waiting for scheduler capacity.'
                    if action == 'download_now':
                        data.setdefault('original_origin', row['origin'])
                        row.update(origin='manual_single', class_rank=3, boost=1)
                elif action == 'skip' or action == 'remove':
                    row.update(state='completed' if row['state'] == 'completed' else 'skipped', resume_allowed=0, removed=int(action == 'remove'))
                    data['reason'] = 'Removed from planning; saved media and queue history were kept.' if action == 'remove' else 'Explicitly skipped by the user.'
                    if row['job_id']:
                        self.store.db.execute("UPDATE jobs SET status='cancelled',message=?,updated=? WHERE id=? AND status='planned'",
                                              (data['reason'], now, row['job_id']))
                elif action == 'unavailable':
                    if row['state'] in ACTIVE or row['state'] == 'completed':
                        continue
                    row.update(state='unavailable', approved=0)
                    data.update(eligible=False, reason='Tracked media is not currently available for an eligible download.')
                elif action == 'pause':
                    if row['state'] in ACTIVE:
                        raise ValueError('Pause or cancel the running transfer in Processing Queue.')
                    row.update(state='interrupted', resume_allowed=0)
                    data['reason'] = 'Paused by the user.'
                elif action == 'priority':
                    selected = priority_name(options.get('priority', 'medium'))
                    row['channel_priority'] = PRIORITIES.index(selected)
                    data['priority_override'] = selected
                elif action == 'ordering':
                    selected = order_name(options.get('ordering', options.get('order', 'newest')))
                    row['ordering'] = selected; data['ordering_override'] = selected
                elif action in {'up', 'down'}:
                    changed.extend(self._move_pending(row, -1 if action == 'up' else 1))
                    continue
                elif action == 'first':
                    row.update(boost=1, position=now * -1)
                row.update(updated=now, data=_json(data))
                self._save(row); changed.append(row['id'])
        return {'changed': len(changed), 'ids': changed, 'progress': self.progress(),
                'items': [self.get(value) for value in changed[:200]]}

    def _move_pending(self, row, direction):
        if row['origin'] == 'channel':
            raise ValueError('Use channel video ordering or Download Now to change watched-channel scheduling.')
        where = "p.origin=? AND p.state IN ('review','ready','failed','interrupted','unavailable') AND p.removed=0"
        args = [row['origin']]
        if row['origin'] == 'manual_playlist':
            group = json.loads(row['data']).get('playlist_id') or row['source_id'] or 'playlist'
            where += " AND COALESCE(NULLIF(json_extract(p.data,'$.playlist_id'),''),NULLIF(p.source_id,''),'playlist')=?"
            args.append(group)
        siblings = [dict(value) for value in self.store.db.execute(self._ranked_query(where) + ' SELECT * FROM ranked ORDER BY ' + self._global_order(), args)]
        index = next(index for index, item in enumerate(siblings) if item['id'] == row['id'])
        target = index + direction
        if not 0 <= target < len(siblings):
            return []
        siblings[index], siblings[target] = siblings[target], siblings[index]
        changed = []
        for position, item in enumerate(siblings):
            data = json.loads(item['data'])
            if item['origin'] == 'manual_playlist':
                item['ordering'] = 'playlist'; data['ordering_override'] = 'playlist'
            item.update(position=position, boost=0, updated=time.time(), data=_json(data))
            self._save(item); changed.append(item['id'])
        return changed

    @staticmethod
    def _order_sql(ordering):
        field = {'newest': 'published', 'oldest': 'published', 'most_viewed': 'views', 'least_viewed': 'views',
                 'longest': 'duration', 'shortest': 'duration'}.get(ordering)
        if field is None:
            return 'position ASC,created ASC,id ASC'
        direction = 'DESC' if ordering in {'newest', 'most_viewed', 'longest'} else 'ASC'
        return f'({field} IS NULL) ASC,{field} {direction},created ASC,id ASC'

    def _candidates(self, now):
        """At most one head per channel, with indexed per-channel metadata order."""
        base = "state='ready' AND approved=1 AND removed=0 AND resume_allowed=1 AND next_retry<=?"
        singles = self.store.db.execute('SELECT * FROM download_plan_items WHERE ' + base +
                                       " AND origin='manual_single' ORDER BY boost DESC,position,created,id LIMIT 128", (now,)).fetchall()
        for row in singles:
            yield dict(row)
        playlists = self.store.db.execute(self._ranked_query(base + " AND p.origin='manual_playlist'") + ' SELECT * FROM ranked ORDER BY ' + self._global_order() + ' LIMIT 128', (now,)).fetchall()
        for row in playlists:
            yield dict(row)
        channels = self.store.db.execute('''SELECT p.source_id,p.channel_priority,COALESCE(c.last_turn,0) AS turn
            FROM download_plan_items p LEFT JOIN download_plan_channels c ON c.source_id=p.source_id
            WHERE p.state='ready' AND p.approved=1 AND p.removed=0 AND p.resume_allowed=1 AND p.next_retry<=?
              AND p.origin='channel' AND (p.automatic=0 OR COALESCE(c.enabled,1)=1)
            GROUP BY p.source_id,p.channel_priority ORDER BY p.channel_priority DESC,turn,p.source_id LIMIT 1000''', (now,)).fetchall()
        for channel in channels:
            # Per-item explicit ordering is uncommon; evaluate the bounded source
            # head using a SQL CASE rather than materializing its historical list.
            rows = self.store.db.execute('SELECT * FROM download_plan_items WHERE ' + base +
                                        " AND origin='channel' AND source_id=? AND channel_priority=? AND "
                                        "(automatic=0 OR COALESCE((SELECT enabled FROM download_plan_channels WHERE source_id=?),1)=1) "
                                        "ORDER BY boost DESC,"
                                        "CASE ordering WHEN 'newest' THEN published IS NULL WHEN 'oldest' THEN published IS NULL "
                                        "WHEN 'most_viewed' THEN views IS NULL WHEN 'least_viewed' THEN views IS NULL "
                                        "WHEN 'longest' THEN duration IS NULL WHEN 'shortest' THEN duration IS NULL ELSE 0 END,"
                                        "CASE ordering WHEN 'newest' THEN -published WHEN 'oldest' THEN published "
                                        "WHEN 'most_viewed' THEN -views WHEN 'least_viewed' THEN views "
                                        "WHEN 'longest' THEN -duration WHEN 'shortest' THEN duration ELSE position END,created,id LIMIT 1",
                                        (now, channel['source_id'], channel['channel_priority'], channel['source_id'])).fetchall()
            for row in rows:
                yield dict(row)

    def _candidate_job(self, row):
        payload = json.loads(row['payload'])
        data = json.loads(row['data'])
        if not payload.get('url') or not payload.get('destination'):
            return None
        job_id = row['job_id']
        old = self.store.db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone() if job_id else None
        if old is None or old['status'] != 'planned':
            job_id = uuid.uuid4().hex
        payload.update(job_id=job_id, download_plan_id=row['id'], plan_id=row['id'])
        payload['resume_key'] = payload.get('resume_key') or payload.get('watch_media') or row.get('job_id') or row['id']
        if row.get('job_id') and row['job_id'] != job_id:
            payload['resume_from_job'] = row['job_id']
        payload.setdefault('tool', 'download_video')
        payload.setdefault('settings', self.store.settings())
        payload['media_preview'] = {key: data.get(key) for key in ('title', 'thumbnail', 'url', 'source_title', 'duration', 'published')}
        if self._has_table('watch_media') and self.store.db.execute('SELECT 1 FROM watch_media WHERE id=?', (row['media_key'],)).fetchone():
            payload['watch_media'] = row['media_key']
        now = time.time()
        return {'id': job_id, 'tool': payload['tool'], 'label': data.get('title', row['media_id']),
                'status': 'queued', 'progress': 0, 'message': 'Admitted by the download planner.', 'payload': payload,
                'outputs': [], 'warnings': [], 'created': now, 'updated': now}

    def admit_one(self, can_admit=None):
        """Create one job only after real resource admission; caller immediately launches it.

        QueueManager holds its lock around this call and launch. The transaction
        additionally prevents duplicate claims from another connection/process.
        """
        with self.store.lock:
            try:
                self.store.db.execute('BEGIN IMMEDIATE')
                for row in self._candidates(time.time()):
                    candidate = self._candidate_job(row)
                    if candidate is None:
                        continue
                    if can_admit is not None and not can_admit(candidate):
                        continue
                    if self.manager is not None:
                        self.manager.check_accepting()
                        if self.manager.stop_event.is_set():
                            self.store.db.rollback(); return None
                        if self.manager.require_feature:
                            self.manager.require_feature(candidate['tool'])
                    job_id = candidate['id']; now = time.time()
                    # SQL insertion deliberately does not call Store.add_job,
                    # whose internal commit would split the admission transaction.
                    prior = self.store.db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
                    if prior is not None:
                        if prior['status'] != 'planned':
                            raise ValueError('The previous queue claim is no longer available.')
                        self.store.db.execute("UPDATE jobs SET status='queued',progress=0,message=?,payload=?,updated=? WHERE id=?",
                                              (candidate['message'], _json(candidate['payload']), now, job_id))
                    else:
                        self.store.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                              (job_id, candidate['tool'], candidate['label'], 'queued', 0, candidate['message'],
                                               _json(candidate['payload']), '[]', '[]', now, now))
                    old_meta = self.store.db.execute('SELECT value FROM job_meta WHERE id=?', (job_id,)).fetchone()
                    meta = json.loads(old_meta[0]) if old_meta else {}
                    legacy_priority = 'critical' if row['class_rank'] == 3 else 'high' if row['class_rank'] == 2 else {
                        5: 'critical', 4: 'high', 3: 'high', 2: 'normal', 1: 'low', 0: 'low'}[row['channel_priority']]
                    meta.update(priority=legacy_priority, plan_id=row['id'], channel_priority=PRIORITIES[row['channel_priority']],
                                media_preview=candidate['payload']['media_preview'])
                    self.store.db.execute('INSERT OR REPLACE INTO job_meta VALUES (?,?)', (job_id, _json(meta)))
                    row.update(state='scheduled', job_id=job_id, attempts=row['attempts'] + 1, updated=now, payload=_json(candidate['payload']))
                    data = json.loads(row['data']); data['reason'] = 'Admitted to Processing Queue.'
                    row['data'] = _json(data); self._save(row); self._sync_watch_locked(row)
                    if row['origin'] == 'channel':
                        turn = self.store.db.execute('SELECT COALESCE(MAX(last_turn),0)+1 FROM download_plan_channels').fetchone()[0]
                        self.store.db.execute('UPDATE download_plan_channels SET last_turn=? WHERE source_id=?', (turn, row['source_id']))
                    self.store.db.commit()
                    return self.store.job(job_id)
                self.store.db.commit()
                return None
            except BaseException:
                self.store.db.rollback(); raise

    def verify_outputs(self, job):
        """Verify actual local media, without holding persistence/scheduler locks."""
        if self._closed.is_set():
            return [], 'Verification stopped with FORGE.'
        binary = executable('ffprobe', self.store.settings())
        if not binary:
            return [], 'FFprobe is unavailable; completed media could not be verified.'
        result = []
        for output in job.get('outputs', []):
            path = Path(str(output.get('path', '')))
            if path.suffix.lower() not in MEDIA:
                continue
            try:
                destination = job.get('payload', {}).get('destination')
                if destination and not path.resolve().is_relative_to(Path(destination).resolve()):
                    return [], 'Published media escaped the selected destination.'
                stat = path.stat()
                if not path.is_file() or stat.st_size <= 0 or stat.st_size != output.get('size'):
                    return [], 'Published media is missing or its size changed.'
                with subprocess.Popen([binary, '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', str(path)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **no_window()) as process:
                    with self._verification_lock:
                        self._verification_processes.add(process)
                    try:
                        if self._closed.is_set():
                            process.kill()
                        stdout, _ = process.communicate(timeout=20)
                    except BaseException:
                        process.kill(); process.communicate()
                        raise
                    finally:
                        with self._verification_lock:
                            self._verification_processes.discard(process)
                info = json.loads(stdout) if process.returncode == 0 else {}
                if not any(stream.get('codec_type') in {'audio', 'video'} for stream in info.get('streams', [])):
                    return [], 'Published output is not a verified playable media file.'
                digest = _digest(path, self._closed)
                expected = output.get('sha256')
                if expected and expected != digest:
                    return [], 'Published output hash changed after verification.'
                if path.stat().st_size != stat.st_size or path.stat().st_mtime_ns != stat.st_mtime_ns:
                    return [], 'Published media changed during verification.'
                result.append({**output, 'sha256': digest, 'verified': True})
            except (OSError, ValueError, subprocess.SubprocessError):
                return [], 'Published media could not be verified.'
        return (result, '') if result else ([], 'No verified audio/video output was published.')

    def _recovery_result(self, job):
        path = self.store.data / 'work' / job['id'] / 'result.json'
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                return None
            result = json.loads(path.read_text('utf-8'))
            if result.get('complete') is True and isinstance(result.get('outputs'), list):
                return {**job, 'outputs': result['outputs'], 'warnings': result.get('warnings', [])}
        except (OSError, ValueError):
            pass
        return None

    @property
    def verification_count(self):
        # Called under manager.lock too: never take _reconcile_lock here.
        return len(self._verifications)

    @property
    def verification_reservations(self):
        # Reserve a free disk lane for a waiting check, preventing a continuously
        # replenished download backlog from starving final verification forever.
        return min(2, len(self._verifications) + int(bool(self._verification_waiting)))

    def _queue_verification(self, row, job, recovered=None):
        if row['id'] in self._verifications or self._closed.is_set():
            return False
        with self.manager.lock if self.manager is not None else nullcontext():
            if len(self._verifications) >= 2:
                self._verification_waiting += 1
                return False
            if self.manager is not None:
                from .resources import required_resources
                if self.manager.stop_event.is_set():
                    return False
                disk_used = sum('disk' in required_resources(self.store.job(identity)) for identity in list(self.manager.active))
                if disk_used + len(self._verifications) >= self.store.settings()['disk_workers']:
                    self._verification_waiting += 1
                    return False
            self._verifications[row['id']] = (dict(row), job, recovered,
                                             self._verification_pool.submit(self.verify_outputs, recovered or job))
            return True

    def _collect_verifications(self):
        for identity, (row, job, recovered, future) in list(self._verifications.items()):
            if not future.done():
                continue
            del self._verifications[identity]
            if self._closed.is_set():
                continue
            try:
                outputs, reason = future.result()
            except Exception:
                outputs, reason = [], 'Published media could not be verified.'
            self._finish_state(row, 'completed' if outputs else 'failed',
                               'Verified downloaded media.' if outputs else reason,
                               outputs=outputs if outputs else None, job=job, recover_job=recovered)

    def reconcile(self, limit=200, force=False):
        """Schedule/poll at most two local verifications; never block dispatcher I/O."""
        if self._closed.is_set():
            return 0
        with self._reconcile_lock:
            now = time.monotonic()
            if not force and now - self._last_reconcile < 1:
                return 0
            self._last_reconcile = now
            self._collect_verifications()
            self._verification_waiting = 0
            with self.store.lock:
                rows = [dict(row) for row in self.store.db.execute("SELECT * FROM download_plan_items WHERE (state IN ('scheduled','downloading') OR (state='interrupted' AND COALESCE(json_extract(data,'$.reconciled_job'),'')!=COALESCE(job_id,'')) OR json_extract(data,'$.pending_legacy_verification')=1) ORDER BY updated LIMIT ?", (max(1, min(1000, limit)),))]
            for row in rows:
                if row['id'] in self._verifications:
                    continue
                data = json.loads(row['data'])
                if data.get('pending_legacy_verification'):
                    job = {'id': row['job_id'] or 'legacy-' + row['id'], 'outputs': data.get('outputs', []),
                           'payload': json.loads(row['payload']), 'execution_seconds': data.get('execution_seconds'),
                           'stage_seconds': data.get('stage_seconds', {}), 'finished': data.get('finished')}
                    self._queue_verification(row, job)
                    continue
                if not row['job_id']:
                    continue
                try:
                    job = self.store.job(row['job_id'])
                except ValueError:
                    self._finish_state(row, 'failed', 'Queue record was removed; the saved plan is retained.')
                    continue
                if job['status'] in JOB_ACTIVE:
                    state = 'downloading' if job['status'] in {'running', 'finalizing', 'cancelling'} else 'scheduled'
                    if row['state'] != state:
                        self._finish_state(row, state, job.get('message', ''))
                    continue
                if job['status'] == 'planned':
                    continue
                recovered = self._recovery_result(job) if job['status'] in {'interrupted', 'cancelled'} else None
                if job['status'] == 'completed' or recovered is not None:
                    self._queue_verification(row, job, recovered)
                elif job['status'] == 'cancelled':
                    self._finish_state(row, 'interrupted', 'Transfer cancelled by the user; explicitly retry to continue.', job=job, resume_allowed=False)
                elif job['status'] == 'interrupted':
                    self._finish_state(row, 'interrupted', 'Transfer interrupted; supported partial data was retained.', job=job,
                                       resume_allowed=False if job.get('cancel_intent') == 'user' else None)
                else:
                    self._finish_state(row, 'failed', job.get('message') or 'Transfer did not complete.', job=job)
            self._collect_verifications()
            self.retry_due()
            return len(rows)

    def wait_for_verifications(self, timeout=10):
        """Deterministic test/recovery helper; ordinary API snapshots never wait."""
        deadline = time.monotonic() + max(0, timeout)
        while time.monotonic() < deadline:
            self.reconcile(force=True)
            with self._reconcile_lock:
                if not self._verifications and not self._verification_waiting:
                    return True
            if self._closed.wait(.02):
                break
        return False

    def close(self):
        self._closed.set()
        with self._verification_lock:
            for process in list(self._verification_processes):
                try:
                    process.kill()
                except OSError:
                    pass
        self._verification_pool.shutdown(wait=True, cancel_futures=True)

    stop = close

    def _finish_state(self, snapshot, state, reason, *, outputs=None, job=None, resume_allowed=None, recover_job=None):
        with self._transaction():
            row = self._row(snapshot['id'])
            if row['job_id'] != snapshot['job_id'] or row['state'] == 'completed':
                return
            data = json.loads(row['data']); data['reason'] = str(reason)[:2000]
            if state in {'completed', 'failed'}:
                data.pop('pending_legacy_verification', None)
            if job is not None:
                data.update(reconciled_job=job['id'], execution_seconds=job.get('execution_seconds'), stage_seconds=job.get('stage_seconds', {}),
                            finished=job.get('finished') or time.time())
                if isinstance(job.get('error_retryable'), bool):
                    data['error_retryable'] = job['error_retryable']
                    data['error_code'] = str(job.get('error_code') or '')[:100]
            if outputs is not None:
                data.update(outputs=outputs, bytes=sum(item['size'] for item in outputs), completion_verified=True)
            row.update(state=state, updated=time.time(), data=_json(data))
            if resume_allowed is not None:
                row['resume_allowed'] = int(resume_allowed)
            self._save(row)
            if recover_job is not None and outputs:
                self.store.db.execute("UPDATE jobs SET status='completed',progress=100,message=?,outputs=?,warnings=?,updated=? WHERE id=? AND status='interrupted'",
                                      ('Recovered and verified previously published output.', _json(outputs), _json(recover_job.get('warnings', [])), time.time(), row['job_id']))

    def recover(self):
        """Re-admit approved interruptions without changing explicit user pauses."""
        self.reconcile(limit=1000, force=True)
        now = time.time()
        changed = 0
        with self._transaction():
            rows = self.store.db.execute("SELECT * FROM download_plan_items WHERE state='interrupted' AND approved=1 AND resume_allowed=1 AND removed=0").fetchall()
            for value in rows:
                row = dict(value); data = json.loads(row['data'])
                if row['id'] in self._verifications or data.get('pending_legacy_verification') or (row.get('job_id') and self._recovery_result({'id': row['job_id']}) is not None):
                    continue
                data['reason'] = 'Recovered approved work; partial transfer state will be reused when supported.'
                row.update(state='ready', data=_json(data), updated=now)
                self._save(row); changed += 1
        return changed

    def retry_due(self):
        """Only approved transient failures with explicit auto-retry and a small bound."""
        from .download_support import diagnostic
        now = time.time()
        with self._transaction():
            rows = self.store.db.execute("SELECT * FROM download_plan_items WHERE state='failed' AND approved=1 AND removed=0 AND attempts<3 AND json_extract(data,'$.auto_retry')=1").fetchall()
            changed = 0
            for value in rows:
                row = dict(value); data = json.loads(row['data'])
                finished = data.get('finished', row['updated'])
                retryable = data.get('error_retryable') if isinstance(data.get('error_retryable'), bool) else diagnostic(data.get('reason', ''))['retryable']
                if not data.get('auto_retry') or not retryable:
                    continue
                due = finished + min(3600, 60 * 2 ** row['attempts'])
                if now < due:
                    continue
                row.update(state='ready', next_retry=0, updated=now)
                data['reason'] = 'Retrying an approved transient transfer failure.'; row['data'] = _json(data)
                self._save(row); changed += 1
        return changed

    def _legacy_payload(self, media, source):
        from .core import safe_name
        from .models import Download
        config = source.get('config', {})
        options = Download.model_validate({**config.get('options', {}), 'playlist': False}).model_dump()
        tool = 'download_audio' if options['format'] in {'mp3', 'm4a', 'flac', 'wav'} else 'download_video'
        root = config.get('directory') or self.store.get('download_last') or self.store.get('download_default') or str(Path.home() / 'Downloads' / 'FORGE')
        historical_destination = media.get('destination')
        if not historical_destination and media.get('status') in {'completed', 'archived'}:
            parents = {str(Path(item['path']).parent) for item in media.get('outputs', []) if isinstance(item, dict) and item.get('path') and Path(item['path']).suffix.lower() in MEDIA}
            historical_destination = next(iter(parents)) if len(parents) == 1 else None
        destination = Path(historical_destination or root)
        if not historical_destination and config.get('subfolders', True):
            destination /= 'YouTube'; destination /= safe_name(source.get('folder') or source.get('id', 'Channel'))
        if not historical_destination and config.get('type_subfolders', True):
            from .download_studio import TYPE_DIRS
            destination /= TYPE_DIRS.get('audio' if tool == 'download_audio' else media.get('type'), 'Videos')
        return {'tool': tool, 'url': media.get('url', ''), 'source': None, 'options': options,
                'settings': self.store.settings(), 'destination': str(destination), 'watch_media': media.get('key'),
                'resume_key': media.get('key', '') + hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()}

    def _graph_member(self, payload, job_id):
        return bool(payload.get('depends_on') or payload.get('workflow_name') or payload.get('steps') or
                    self.store.db.execute("SELECT 1 FROM jobs WHERE id!=? AND json_extract(payload,'$.depends_on')=? LIMIT 1", (job_id, job_id)).fetchone())

    def _migrate_watch(self, key):
        if self.store.db.execute('SELECT 1 FROM download_plan_items WHERE media_key=?', (key,)).fetchone():
            return False
        media = json.loads(self.store.db.execute('SELECT data FROM watch_media WHERE id=?', (key,)).fetchone()[0])
        source_row = self.store.db.execute('SELECT data FROM watch_sources WHERE id=?', (media.get('source_id', ''),)).fetchone()
        source = json.loads(source_row[0]) if source_row else {'id': media.get('source_id', ''), 'title': '', 'config': {}}
        config = source.get('config', {})
        job_row = self.store.db.execute('SELECT * FROM jobs WHERE id=?', (media.get('job_id'),)).fetchone() if media.get('job_id') else None
        job = dict(job_row) if job_row else None
        payload = json.loads(job['payload']) if job else self._legacy_payload(media, source)
        old_state = media.get('status', 'review')
        approved = old_state in {'queued', 'running', 'cancelling', 'finalizing', 'waiting', 'interrupted'} or bool(media.get('auto_pending'))
        item = self.upsert(media, origin='channel', source_id=source.get('id', ''), source_title=source.get('title', ''),
                           priority=media.get('priority_override') or media.get('priority') or config.get('priority', 'medium'),
                           ordering=config.get('video_order', 'newest'), approved=approved, payload=payload,
                           tool=payload['tool'], destination=payload.get('destination', ''), automatic=approved,
                           historical=old_state == 'review')
        row = self._row(item['id']); data = json.loads(row['data'])
        state = {'archived': 'completed', 'completed': 'completed', 'skipped': 'skipped', 'failed': 'failed',
                 'unavailable': 'unavailable', 'interrupted': 'interrupted', 'review': 'review'}.get(old_state, 'ready' if approved else 'review')
        if job:
            payload['resume_key'] = payload.get('resume_key') or payload.get('watch_media') or job['id']
            row['payload'] = _json(payload)
            if job['status'] == 'queued' and not self._graph_member(payload, job['id']):
                state = 'ready'; payload.update(download_plan_id=row['id'], plan_id=row['id'])
                self.store.db.execute("UPDATE jobs SET status='planned',payload=?,updated=? WHERE id=? AND status='queued'", (_json(payload), time.time(), job['id']))
            elif job['status'] in JOB_ACTIVE:
                state = 'scheduled' if job['status'] == 'queued' else 'downloading'
            elif job['status'] == 'completed':
                state = 'completed'
            elif job['status'] == 'interrupted':
                state = 'interrupted'
            row['job_id'] = job['id']
        if state == 'completed':
            state = 'unavailable'
            data['pending_legacy_verification'] = True
        data.update(outputs=media.get('outputs', []), bytes=media.get('bytes', 0), reason=media.get('reason', ''),
                    execution_seconds=media.get('execution_seconds'), stage_seconds=media.get('stage_seconds', {}),
                    finished=media.get('finished'), completion_verified=False)
        row.update(state=state, data=_json(data), attempts=int(media.get('attempts') or 0), approved=int(approved or state in ACTIVE | {'completed'}))
        self._save(row)
        if self._has_table('watch_discovery'):
            for linked in self.store.db.execute('SELECT source_id FROM watch_discovery WHERE media_id=?', (key,)).fetchall():
                self._associate(row['id'], linked['source_id'], source.get('title', ''))
        return True

    def _migrate_manual(self, job):
        from urllib.parse import parse_qs
        payload = json.loads(job['payload'])
        if payload.get('download_plan_id') or payload.get('watch_media') or self._graph_member(payload, job['id']):
            return False
        url = str(payload.get('url', ''))
        if not url:
            return False
        parsed = urlsplit(url)
        playlist = bool(payload.get('options', {}).get('playlist'))
        media_id = ''
        if not playlist and parsed.hostname in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'}:
            media_id = parsed.path.strip('/').split('/')[-1] if parsed.hostname == 'youtu.be' or parsed.path.startswith(('/shorts/', '/live/')) else parse_qs(parsed.query).get('v', [''])[0]
        platform = 'youtube' if media_id else 'legacy'
        media_id = media_id or hashlib.sha256(url.encode()).hexdigest()
        key = platform + ':' + media_id
        existing = self.store.db.execute('SELECT * FROM download_plan_items WHERE media_key=?', (key,)).fetchone()
        if existing and existing['state'] in ACTIVE | {'completed'}:
            self.store.db.execute("UPDATE jobs SET status='cancelled',message='This media is already represented by an active or verified plan; no duplicate transfer was created.',updated=? WHERE id=? AND status='queued'", (time.time(), job['id']))
            return False
        media = {'id': media_id, 'platform': platform, 'title': job['label'], 'url': url}
        payload['resume_key'] = payload.get('resume_key') or payload.get('watch_media') or job['id']
        item = self.upsert(media, origin='manual_playlist' if playlist else 'manual_single', approved=True,
                           payload=payload, tool=job['tool'], destination=payload.get('destination', ''),
                           playlist_id=('legacy:' + media_id) if playlist else '')
        row = self._row(item['id'])
        if row['job_id'] and row['job_id'] != job['id']:
            self.store.db.execute("UPDATE jobs SET status='cancelled',message='A manual request reused this planned media identity; the pending duplicate was consolidated.',updated=? WHERE id=? AND status='planned'", (time.time(), row['job_id']))
        row.update(job_id=job['id'], state='interrupted' if job['status'] == 'interrupted' else 'ready')
        payload.update(download_plan_id=row['id'], plan_id=row['id']); row['payload'] = _json(payload)
        self.store.db.execute("UPDATE jobs SET status='planned',payload=?,updated=? WHERE id=? AND status='queued'", (_json(payload), time.time(), job['id']))
        self._save(row)
        return True

    def migrate_legacy(self):
        """Page additive migration; each page is atomic, IDs and graph rows survive."""
        count = 0
        with self.store.lock:
            has_watch = self._has_table('watch_media') and self._has_table('watch_sources')
            sources = self.store.db.execute('SELECT data FROM watch_sources').fetchall() if has_watch else []
        for value in sources:
            source = json.loads(value[0]); config = source.get('config', {})
            self.configure_source(source['id'], priority=config.get('priority', 'medium'),
                                  ordering=config.get('video_order', 'newest'),
                                  backfill_ordering=config.get('backfill_order', config.get('video_order', 'newest')),
                                  enabled=bool(config.get('enabled', True)))
        cursor = ''
        while has_watch:
            with self._transaction():
                page = self.store.db.execute('SELECT id FROM watch_media WHERE id>? ORDER BY id LIMIT 500', (cursor,)).fetchall()
                for value in page:
                    count += self._migrate_watch(value['id'])
                if page:
                    cursor = page[-1]['id']
            if len(page) < 500:
                break
        cursor = ''
        while True:
            with self._transaction():
                page = self.store.db.execute("SELECT * FROM jobs WHERE status IN ('queued','interrupted') AND tool IN ('download_audio','download_video') AND id>? ORDER BY id LIMIT 500", (cursor,)).fetchall()
                for job in page:
                    count += self._migrate_manual(dict(job))
                if page:
                    cursor = page[-1]['id']
            if len(page) < 500:
                break
        return count
