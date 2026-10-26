"""Authenticated media planning and persistent, server-owned playlist selections.

Metadata discovery runs outside scheduler/database locks. Browser requests select
stored provider identities; they cannot inject replacement media metadata or URLs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from pydantic import Field

from .core import absolute_path, safe_name
from .download_support import media_url
from .models import Download, Output, Strict

ORDERINGS = {'newest', 'oldest', 'most_viewed', 'least_viewed', 'longest', 'shortest', 'playlist'}
PRIORITIES = {'minimal', 'low', 'medium', 'high', 'extra', 'ultra'}
PREFERENCE_KEYS = {'tab', 'watch_sort', 'watch_direction', 'watch_filter', 'watch_priority',
    'watch_platform', 'watch_view', 'watch_search', 'uploads_view', 'uploads_filter',
    'uploads_order', 'uploads_search', 'plan_filter', 'plan_origin', 'plan_search',
    'plan_source', 'plan_batch'}


def identity(url, *, platform='', item_id=''):
    """Reuse provider IDs across channel, short-link and manual submissions."""
    url = media_url(url)
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be'}:
        video = (parse_qs(parts.query).get('v') or [''])[0]
        if host.endswith('youtu.be'):
            video = parts.path.strip('/').split('/')[0]
        elif parts.path.startswith(('/shorts/', '/live/', '/embed/')):
            video = parts.path.split('/')[2]
        video = item_id or video
        if re.fullmatch(r'[A-Za-z0-9_-]{1,160}', str(video)):
            return {'platform': 'youtube', 'id': str(video), 'url': 'https://www.youtube.com/watch?v=' + str(video)}
    if platform and platform.lower() not in {'generic', 'unknown'} and item_id:
        return {'platform': platform.lower(), 'id': str(item_id), 'url': url}
    canonical = parts._replace(fragment='').geturl()
    return {'platform': 'generic', 'id': hashlib.sha256(canonical.encode()).hexdigest(), 'url': url}


def checked_order(value):
    value = value or 'playlist'
    if value == 'original':
        value = 'playlist'
    if value not in ORDERINGS:
        raise ValueError('Select a supported media ordering rule.')
    return value


def checked_options(tool, value, *, single=True):
    if tool not in {'download_audio', 'download_video'}:
        raise ValueError('Choose an audio or video download.')
    options = Download.model_validate({'format': 'mp4' if tool == 'download_video' else 'mp3', **(value or {})}).model_dump()
    allowed = {'mp3', 'm4a', 'flac', 'wav'} if tool == 'download_audio' else {'mp4', 'mkv'}
    if options['format'] not in allowed:
        raise ValueError('Select an output format suitable for this downloader.')
    if options['cookies']:
        options['cookies'] = str(absolute_path(options['cookies']))
    # Thumbnail previews never authorize exporting image files beside media.
    options['thumbnail'] = False
    if single:
        options['playlist'] = False
    return options


class ImportPreview(Strict):
    url: str = Field(min_length=1, max_length=8000)
    playlist: bool = False
    cookies: str = Field('', max_length=2000)
    limit: int = Field(100, ge=1, le=200)
    offset: int = Field(0, ge=0, le=100000)
    import_id: str = Field('', max_length=100)


class ImportSelection(Strict):
    import_id: str = Field(min_length=1, max_length=100)
    selected_ids: list[str] | None = Field(None, max_length=1000)
    add_selected_ids: list[str] = Field(default_factory=list, max_length=1000)
    remove_selected_ids: list[str] = Field(default_factory=list, max_length=1000)
    select_all_loaded: bool = False
    clear_selection: bool = False
    tool: str | None = None
    options: dict | None = None
    output: Output | None = None
    ordering: str | None = None
    playlist_subfolder: bool | None = None


class ImportSubmit(Strict):
    import_id: str = Field('', max_length=100)
    media_ids: list[str] | None = Field(None, max_length=1000)
    tool: str = 'download_video'
    urls: list[str] = Field(default_factory=list, max_length=1000)
    options: dict | None = None
    output: Output | None = None
    ordering: str | None = None
    playlist_subfolder: bool | None = None


class PlanAction(Strict):
    ids: list[str] = Field(default_factory=list, max_length=1000)
    action: str = Field(min_length=1, max_length=40)
    priority: str | None = None
    ordering: str | None = None
    source_id: str = Field('', max_length=200)


class PlanBatch(Strict):
    ids: list[str] = Field(default_factory=list, max_length=1000)
    name: str = Field('', max_length=160)
    skipped_resolved: bool = False


class SavedOutput(Strict):
    id: str = Field('', max_length=240)
    key: str = Field('', max_length=240)
    index: int = Field(0, ge=0, le=10000)
    reveal: bool = False


class MediaImports:
    def __init__(self, store, studio, planner, manager):
        self.store, self.studio, self.planner, self.manager = store, studio, planner, manager
        self.discovery_slots = threading.BoundedSemaphore(2)
        with store.lock, store.db:
            store.db.executescript('''
                CREATE TABLE IF NOT EXISTS download_imports(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS download_import_entries(
                    import_id TEXT NOT NULL, media_id TEXT NOT NULL, position INTEGER NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL,
                    PRIMARY KEY(import_id, media_id));
                CREATE INDEX IF NOT EXISTS download_import_entries_page
                    ON download_import_entries(import_id,position,media_id);
                CREATE INDEX IF NOT EXISTS download_import_entries_selection
                    ON download_import_entries(import_id,selected,position);
            ''')

    def record(self, import_id):
        with self.store.lock:
            row = self.store.db.execute('SELECT data FROM download_imports WHERE id=?', (import_id,)).fetchone()
        if not row:
            raise ValueError('Saved playlist or media import was not found.')
        return json.loads(row[0])

    def snapshot(self, import_id, *, limit=100, offset=0):
        record = self.record(import_id)
        with self.store.lock:
            rows = self.store.db.execute('SELECT data,selected FROM download_import_entries WHERE import_id=? ORDER BY position,media_id LIMIT ? OFFSET ?',
                                         (import_id, max(1, min(200, limit)), max(0, offset))).fetchall()
            total, selected = self.store.db.execute('SELECT COUNT(*),COALESCE(SUM(selected),0) FROM download_import_entries WHERE import_id=?', (import_id,)).fetchone()
        public = copy.deepcopy(record)
        public['authentication_configured'] = bool(public.pop('cookies', '') or public.get('options', {}).get('cookies'))
        public.setdefault('options', {})['cookies'] = ''
        public.update(import_id=import_id, entries=[{**json.loads(row[0]), 'selected': bool(row[1])} for row in rows],
                      total_loaded=total, selection_count=selected, offset=offset, limit=limit,
                      entries_has_more=offset + len(rows) < total)
        return public

    def recent(self):
        with self.store.lock:
            ids = [row[0] for row in self.store.db.execute("SELECT id FROM download_imports ORDER BY json_extract(data,'$.updated') DESC LIMIT 100")]
        return {'imports': [{key: value for key, value in self.snapshot(key, limit=1).items() if key != 'entries'} for key in ids]}

    def preview(self, request):
        url = media_url(request.url)
        existing = self.record(request.import_id) if request.import_id else None
        with self.store.lock:
            previous_total = self.store.db.execute('SELECT COUNT(*) FROM download_import_entries WHERE import_id=?', (request.import_id,)).fetchone()[0] if existing else 0
        if existing and (existing['url'] != url or existing['playlist'] != request.playlist):
            raise ValueError('A saved import retains its original source and playlist mode.')
        cookies = request.cookies or (existing or {}).get('cookies', '')
        if cookies:
            cookies = str(absolute_path(cookies))
        if not self.discovery_slots.acquire(blocking=False):
            raise ValueError('Two source previews are already running. Try again after one finishes.')
        try:
            result = self.studio.adapter.inspect(url, watch=False, playlist=request.playlist,
                limit=request.limit, offset=request.offset, cookies=cookies)
        finally:
            self.discovery_slots.release()
        import_id = request.import_id or uuid.uuid4().hex
        now = time.time()
        record = existing or {'id': import_id, 'url': url, 'playlist': request.playlist,
            'created': now, 'tool': 'download_video', 'options': Download(format='mp4').model_dump(),
            'output': Output(mode='default').model_dump(), 'ordering': 'playlist', 'playlist_subfolder': False}
        record.update(title=str(result.get('title') or 'Media import')[:400], platform=result.get('platform') or 'generic',
                      cookies=cookies, updated=now, has_more=bool(result.get('has_more')),
                      next_offset=request.offset + request.limit, reported_count=result.get('reported_count'))
        record['formats'] = [{key: value.get(key) for key in ('format_id', 'ext', 'height', 'width', 'vcodec', 'acodec', 'filesize', 'format_note')}
                             for value in result.get('formats', [])[:200] if isinstance(value, dict)]
        entries = []
        for index, value in enumerate(result.get('entries', [])[:600]):
            canonical = identity(value.get('url', ''), platform=record['platform'], item_id=value.get('id', ''))
            media = {**value, **canonical, 'title': str(value.get('title') or canonical['id'])[:400],
                     'position': request.offset + index, 'channel': str(value.get('channel') or value.get('channel_title') or '')[:400]}
            entries.append(media)
        # Complete canonicalization before beginning the transaction.
        with self.store.lock, self.store.db:
            if existing:
                # A slow page cannot erase settings or selections saved while
                # discovery was outside the lock, nor regress its provider cursor.
                latest = self.record(import_id)
                metadata = {key: record[key] for key in ('title', 'platform', 'cookies', 'updated')}
                if record['formats']:
                    metadata['formats'] = record['formats']
                if record['next_offset'] >= latest.get('next_offset', 0):
                    metadata.update(next_offset=record['next_offset'], has_more=record['has_more'], reported_count=record['reported_count'])
                record = {**latest, **metadata}
            self.store.db.execute('INSERT OR REPLACE INTO download_imports VALUES (?,?)', (import_id, json.dumps(record)))
            for media in entries:
                self.store.db.execute('''INSERT INTO download_import_entries VALUES (?,?,?,?,?)
                    ON CONFLICT(import_id,media_id) DO UPDATE SET data=excluded.data''',
                    (import_id, media['id'], media['position'], int(not request.playlist), json.dumps(media)))
        return self.snapshot(import_id, limit=request.limit, offset=previous_total)

    def selection(self, request):
        with self.store.lock, self.store.db:
            # Autosaves can arrive together. Read the latest settings inside the
            # same lock as selection changes and the final merged record write.
            record = self.record(request.import_id)
            if request.tool is not None:
                record['tool'] = request.tool
            if request.options is not None:
                options = {**record.get('options', {}), **request.options}
                if not request.options.get('cookies'):
                    options['cookies'] = record.get('options', {}).get('cookies') or record.get('cookies', '')
                record['options'] = checked_options(record['tool'], options)
            else:
                checked_options(record['tool'], record['options'])
            if request.output is not None:
                record['output'] = request.output.model_dump()
            if request.ordering is not None:
                record['ordering'] = checked_order(request.ordering)
            if request.playlist_subfolder is not None:
                record['playlist_subfolder'] = request.playlist_subfolder
            record['updated'] = time.time()
            groups = [request.selected_ids or [], request.add_selected_ids, request.remove_selected_ids]
            for group in groups:
                for media_id in group:
                    if not self.store.db.execute('SELECT 1 FROM download_import_entries WHERE import_id=? AND media_id=?', (request.import_id, media_id)).fetchone():
                        raise ValueError('Select media from this saved import, not an unknown identity.')
            if request.clear_selection or request.selected_ids is not None:
                self.store.db.execute('UPDATE download_import_entries SET selected=0 WHERE import_id=?', (request.import_id,))
            if request.select_all_loaded:
                self.store.db.execute('UPDATE download_import_entries SET selected=1 WHERE import_id=?', (request.import_id,))
            for group, value in ((request.selected_ids or [], 1), (request.add_selected_ids, 1), (request.remove_selected_ids, 0)):
                self.store.db.executemany('UPDATE download_import_entries SET selected=? WHERE import_id=? AND media_id=?',
                                         [(value, request.import_id, media_id) for media_id in group])
            self.store.db.execute('UPDATE download_imports SET data=? WHERE id=?', (json.dumps(record), request.import_id))
        return self.snapshot(request.import_id)

    def submit(self, request):
        self.manager.check_accepting()
        saved = self.record(request.import_id) if request.import_id else None
        tool = request.tool if request.options is not None or not saved else saved['tool']
        options = copy.deepcopy(request.options if request.options is not None else (saved or {}).get('options', {}))
        if saved and not options.get('cookies'):
            options['cookies'] = saved.get('options', {}).get('cookies') or saved.get('cookies', '')
        options = checked_options(tool, options)
        if self.manager.require_feature:
            self.manager.require_feature(tool)
        output = request.output or Output.model_validate((saved or {}).get('output', {'mode': 'default'}))
        if output.mode == 'source':
            raise ValueError('Media links have no local source folder. Choose a download destination.')
        directories = self.studio.destinations.snapshot()
        selected_root = output.directory if output.mode == 'custom' else directories['last'] or directories['default'] if output.mode == 'last' else directories['default']
        root = absolute_path(selected_root, directory=True, must_exist=False)
        if output.subfolder:
            if safe_name(output.subfolder) != output.subfolder:
                raise ValueError('Use a simple output subfolder name.')
            root = root / output.subfolder
        subfolder = request.playlist_subfolder if request.playlist_subfolder is not None else bool((saved or {}).get('playlist_subfolder'))
        if subfolder and saved and saved['playlist']:
            root = root / safe_name(saved['title'])
        destination = self.studio.destinations.resolve(str(root))
        ordering = checked_order(request.ordering or (saved or {}).get('ordering', 'playlist'))
        if saved:
            with self.store.lock:
                if request.media_ids is None:
                    rows = self.store.db.execute('SELECT data FROM download_import_entries WHERE import_id=? AND selected=1 ORDER BY position,media_id LIMIT 1001', (request.import_id,)).fetchall()
                else:
                    rows = []
                    for media_id in dict.fromkeys(request.media_ids):
                        row = self.store.db.execute('SELECT data FROM download_import_entries WHERE import_id=? AND media_id=?', (request.import_id, media_id)).fetchone()
                        if not row:
                            raise ValueError('Select only entries belonging to this saved import.')
                        rows.append(row)
            if len(rows) > 1000:
                raise ValueError('Add at most 1000 selected entries per planning operation; saved selections are retained.')
            media = [json.loads(row[0]) for row in rows]
            origin = 'manual_playlist' if saved['playlist'] else 'manual_single'
        else:
            if not request.urls:
                raise ValueError('Paste a media URL or select saved playlist entries.')
            if (request.options or {}).get('playlist') or any(urlsplit(url).path.rstrip('/') == '/playlist' for url in request.urls):
                raise ValueError('Preview the playlist and select its entries before adding downloads.')
            media = [{**identity(url), 'title': urlsplit(url).hostname + urlsplit(url).path[:100]} for url in dict.fromkeys(request.urls)]
            origin = 'manual_single'
        if not media:
            raise ValueError('Select at least one media item.')
        requests = []
        for index, item in enumerate(media):
            payload = {'tool': tool, 'source': None, 'url': item['url'], 'options': copy.deepcopy(options),
                       'settings': self.store.settings(), 'destination': str(destination),
                       'media': {key: item.get(key) for key in ('id', 'platform', 'title', 'thumbnail', 'channel', 'url')},
                       'priority': 'normal'}
            if origin == 'manual_playlist':
                payload['playlist_index'] = int(item.get('position', index)) + 1
                payload['media']['position'] = int(item.get('position', index))
            requests.append({'media': item, 'origin': origin, 'source_id': request.import_id,
                'source_title': (saved or {}).get('title', 'Manual download'), 'priority': 'medium',
                'ordering': ordering, 'approved': True, 'payload': payload, 'tool': tool,
                'destination': str(destination), 'automatic': False,
                'playlist_id': request.import_id if origin == 'manual_playlist' else '',
                'position': item.get('position', index)})
        if hasattr(self.planner, 'upsert_many'):
            items = self.planner.upsert_many(requests)
        else:
            items = [self.planner.upsert(**entry) for entry in requests]
        self.studio.destinations.remember(root)
        if saved:
            self.selection(ImportSelection(import_id=request.import_id, tool=tool, options=options,
                output=output, ordering=ordering, playlist_subfolder=subfolder))
        batch = self.planner.frozen_batch([item['id'] for item in items], name=(saved or {}).get('title', 'Manual downloads'))
        return {'items': items, 'plans': items, 'jobs': [], 'batch': batch,
                'progress': self.planner.progress(), 'import_id': request.import_id}


def restore_queue_after_restart(store, planner):
    """Release only an automatically introduced, fully reconciled plan pause."""
    marker = store.get('queue_restart_pause') or {}
    shutdown_resume = store.get('queue_shutdown_resume', False)
    if store.get('queue_pause_explicit', False) or not (marker.get('automatic') or shutdown_resume):
        return
    for job_id in marker.get('interrupted_ids', []):
        try:
            job = store.job(job_id)
            plan_id = job['payload'].get('download_plan_id')
            plan = planner.get(plan_id) if plan_id else {}
        except ValueError:
            return
        if not plan:
            return
    settings = store.settings()
    settings['queue_paused'] = False
    store.set('settings', settings)
    store.set('queue_restart_pause', None)
    store.set('queue_shutdown_resume', False)


def mount(app, store, manager):
    planner = manager.download_planner
    service = MediaImports(store, app.state.download_studio, planner, manager)
    app.state.download_imports = service

    @app.post('/api/downloads/import/preview')
    def preview(body: ImportPreview):
        from .entitlements import require_feature
        require_feature(app, 'download_video')
        return service.preview(body)

    @app.get('/api/downloads/imports')
    def imports():
        return service.recent()

    @app.get('/api/downloads/imports/{id}')
    def imported(id: str, limit: int = 100, offset: int = 0):
        return service.snapshot(id, limit=limit, offset=offset)

    @app.post('/api/downloads/import/selection')
    def selection(body: ImportSelection):
        return service.selection(body)

    @app.post('/api/downloads/import')
    def submit(body: ImportSubmit):
        return service.submit(body)

    @app.get('/api/downloads/plans')
    def plans(state: str = '', source: str = '', origin: str = '', search: str = '', limit: int = 100, offset: int = 0, batch: str = ''):
        return planner.snapshot(state=state, source=source, origin=origin, search=search, limit=limit, offset=offset, batch=batch)

    @app.post('/api/downloads/plans/action')
    def action(body: PlanAction):
        if not body.ids:
            raise ValueError('Select at least one planned media item.')
        options = {}
        if body.priority is not None:
            if body.priority not in PRIORITIES:
                raise ValueError('Select one of the six channel priorities.')
            options['priority'] = body.priority
        if body.ordering is not None:
            options['ordering'] = checked_order(body.ordering)
        return planner.action(body.ids, body.action, **options)

    @app.post('/api/downloads/plans/start')
    def start():
        manager.check_accepting()
        settings = store.settings()
        settings['queue_paused'] = False
        store.set('settings', settings)
        store.set('queue_pause_explicit', False)
        store.set('queue_restart_pause', None)
        store.set('queue_shutdown_resume', False)
        manager.dispatch_once()
        return planner.snapshot()

    @app.post('/api/downloads/plans/batches')
    def batch(body: PlanBatch):
        return planner.frozen_batch(body.ids, name=body.name, skipped_resolved=body.skipped_resolved)

    def open_saved(record, body):
        from .queue_api import open_media_output
        outputs = record.get('outputs', [])
        destination = record.get('destination', '')
        job = {'outputs': outputs, 'payload': {'destination': destination}}
        if not body.reveal:
            return open_media_output(job, body.index)
        if not 0 <= body.index < len(outputs):
            raise ValueError('Saved media output was not found.')
        raw = Path(outputs[body.index]['path'])
        path = raw.resolve()
        if not raw.is_absolute() or not destination or not path.is_file() or not path.is_relative_to(Path(destination).resolve()):
            raise ValueError('The saved media is unavailable at its verified destination.')
        if os.name != 'nt':
            return {'opened': False, 'path': str(path), 'reason': 'Native folder opening is supported on Windows.'}
        from .core import no_window
        subprocess.Popen(['explorer.exe', '/select,', str(path)], **no_window())
        return {'opened': True}

    @app.post('/api/downloads/plans/open')
    def open_plan(body: SavedOutput):
        return open_saved(planner.get(body.id), body)

    @app.post('/api/watch/media/open')
    def open_watch_media(body: SavedOutput):
        return open_saved(app.state.download_studio.get('watch_media', body.key), body)

    @app.get('/api/downloads/preferences')
    @app.get('/api/watch/preferences')
    def preferences():
        return {'preferences': store.get('media_downloads_v9_preferences', {})}

    @app.post('/api/downloads/preferences')
    @app.post('/api/watch/preferences')
    def save_preferences(body: dict):
        values = body.get('preferences', {})
        if not isinstance(values, dict) or not set(values) <= PREFERENCE_KEYS or any(not isinstance(value, str) or len(value) > 500 for value in values.values()):
            raise ValueError('Only supported media-view preferences may be saved.')
        store.set('media_downloads_v9_preferences', {**store.get('media_downloads_v9_preferences', {}), **values})
        return preferences()
