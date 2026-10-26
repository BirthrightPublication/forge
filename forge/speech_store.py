"""Additive, revisioned local speech records on FORGE's existing SQLite store.

The service owns no connection or worker manager. Use the existing Store so
opening a second Store cannot interrupt active jobs. Historical render recipes
and revisions are immutable, including after preset renames or draft edits.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
import uuid

from .speech_documents import EMOTIONS, READING_STYLES, identifier

KINDS = {'voices', 'emotions', 'styles', 'effects', 'pronunciations', 'settings', 'directories'}
ALIASES = {'profiles': 'voices', 'voice': 'voices', 'emotion': 'emotions', 'style': 'styles',
           'reading_styles': 'styles', 'pronunciation': 'pronunciations', 'effect': 'effects'}
MAX_RECORD = 2_000_000


class RevisionConflict(ValueError):
    """The draft/preset changed since the caller last read it."""


def _json(value):
    def validate(item, depth=0):
        if depth > 24: raise ValueError('Speech JSON nesting exceeds 24 levels.')
        if isinstance(item, dict):
            if len(item) > 10_000 or any(not isinstance(key, str) or len(key) > 256 for key in item):
                raise ValueError('Invalid speech JSON object keys.')
            for child in item.values(): validate(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 30_000: raise ValueError('Speech JSON list is too large.')
            for child in item: validate(child, depth + 1)
        elif isinstance(item, float):
            if not math.isfinite(item): raise ValueError('Speech JSON cannot contain nonfinite numbers.')
        elif item is not None and not isinstance(item, (str, int, bool)):
            raise ValueError('Speech profiles must be JSON data, never Python/pickle objects.')
    validate(value)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > MAX_RECORD: raise ValueError('Speech record exceeds two megabytes.')
    return encoded


def _kind(kind):
    result = ALIASES.get(kind, kind)
    if result not in KINDS: raise ValueError('Unsupported speech record kind.')
    return result


def _revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('Speech revision must be a nonnegative integer.')
    return value


def _name(value, default):
    name = value.get('name', default)
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise ValueError('Speech record name must contain 1–160 characters.')
    value['name'] = name.strip()


class SpeechStore:
    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.executescript('''
                CREATE TABLE IF NOT EXISTS speech_projects (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS speech_project_revisions (
                    id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(id,revision));
                CREATE TABLE IF NOT EXISTS speech_records (
                    kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    data TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS speech_record_revisions (
                    kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    data TEXT NOT NULL, PRIMARY KEY(kind,id,revision));
                CREATE TABLE IF NOT EXISTS speech_renders (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, segment_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL,
                    UNIQUE(project_id,segment_id,fingerprint));
                CREATE INDEX IF NOT EXISTS speech_render_project ON speech_renders(project_id);
            ''')
            store.db.commit()

    def projects(self):
        with self.store.lock:
            rows = self.store.db.execute('SELECT data FROM speech_projects ORDER BY updated DESC,id').fetchall()
        return [json.loads(row[0]) for row in rows]

    def project(self, id, revision=None):
        identifier(id, 'project ID')
        with self.store.lock:
            if revision is None:
                row = self.store.db.execute('SELECT data FROM speech_projects WHERE id=?', (id,)).fetchone()
            else:
                _revision(revision)
                row = self.store.db.execute('SELECT data FROM speech_project_revisions WHERE id=? AND revision=?', (id, revision)).fetchone()
        if not row: raise ValueError('Speech project not found.')
        return json.loads(row[0])

    def save_project(self, body):
        if not isinstance(body, dict): raise ValueError('Speech project must be a JSON object.')
        _json(body)
        value = copy.deepcopy(body)
        key = identifier(value.get('id') or uuid.uuid4().hex, 'project ID')
        revision = _revision(value.get('revision', 0))
        _name(value, 'Untitled Speech Project')
        with self.store.lock:
            old = self.store.db.execute('SELECT data,revision,created FROM speech_projects WHERE id=?', (key,)).fetchone()
            if old and revision != old['revision'] or not old and revision:
                raise RevisionConflict('Project changed. Reload it before saving; your draft has not been overwritten.')
            if not old and self.store.db.execute('SELECT 1 FROM speech_project_revisions WHERE id=? LIMIT 1', (key,)).fetchone():
                raise ValueError('Deleted project IDs are reserved for their historical revisions; duplicate with a new ID.')
            previous = json.loads(old['data']) if old else {}
            chapters = value.get('chapters')
            if chapters is None:
                chapters = [{'id': previous.get('chapters', [{}])[0].get('id') if previous.get('chapters') else None,
                             'title': value.get('title', 'Chapter 1'), 'text': value.get('text', '')}]
            if not isinstance(chapters, list) or not 1 <= len(chapters) <= 5000:
                raise ValueError('A speech project must have 1–5000 chapters.')
            total, ids = 0, set()
            for index, chapter in enumerate(chapters):
                if not isinstance(chapter, dict): raise ValueError('Chapter must be a JSON object.')
                chapter['id'] = identifier(chapter.get('id') or uuid.uuid4().hex, 'chapter ID')
                if chapter['id'] in ids: raise ValueError('Chapter IDs must be unique.')
                ids.add(chapter['id'])
                chapter.setdefault('title', 'Chapter ' + str(index + 1))
                if not isinstance(chapter['title'], str) or len(chapter['title']) > 300: raise ValueError('Chapter title is too long.')
                if not isinstance(chapter.get('text', ''), str) or '\x00' in chapter.get('text', ''): raise ValueError('Chapter text must be plain text without NUL.')
                chapter.setdefault('text', '')
                total += len(chapter['text'])
            if total > 1_000_000: raise ValueError('Speech project text exceeds one million characters.')
            speakers = value.get('speakers', {})
            if not isinstance(speakers, dict) or len(speakers) > 100: raise ValueError('A project supports at most 100 named speakers.')
            for name, profile in speakers.items():
                identifier(name, 'speaker ID')
                if not isinstance(profile, dict): raise ValueError('Speaker configuration must be an object.')
            processing = value.get('segment_processing', [])
            if not isinstance(processing, list) or len(processing) > 10_000:
                raise ValueError('Per-selection processing must contain at most 10000 ranges.')
            by_chapter = {chapter['id']: chapter for chapter in chapters}
            previous_selections = previous.get('segment_processing', [])
            for selection in processing:
                if not isinstance(selection, dict) or selection.get('chapter_id') not in by_chapter:
                    raise ValueError('Per-selection processing must refer to an existing chapter.')
                start, end = selection.get('start'), selection.get('end')
                if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, end)) or not 0 <= start < end:
                    raise ValueError('Per-selection processing requires a nonempty character range.')
                from .audio_effects import validate_chain
                validate_chain(selection.get('chain', []))
                prior = next((item for item in previous_selections if all(item.get(key) == selection.get(key) for key in ('chapter_id', 'start', 'end', 'chain'))), {})
                selection['id'] = identifier(selection.get('id') or prior.get('id') or uuid.uuid4().hex, 'selection ID')
                current_hash = hashlib.sha256(by_chapter[selection['chapter_id']]['text'][start:end].encode()).hexdigest()
                expected_hash = selection.get('source_sha256') or prior.get('source_sha256') or current_hash
                if not isinstance(expected_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_hash):
                    raise ValueError('Invalid selection source SHA-256.')
                selection['source_sha256'] = expected_hash
                # Draft edits can move ranges beyond current text. Preserve the
                # annotation and flag it for correction; rendering validates
                # source overlap rather than silently applying it elsewhere.
                selection['stale'] = end > len(by_chapter[selection['chapter_id']]['text']) or current_hash != expected_hash
            value.update(id=key, revision=revision + 1, chapters=chapters, speakers=speakers,
                         created=old['created'] if old else time.time(), updated=time.time())
            encoded = _json(value)
            try:
                if old:
                    changed = self.store.db.execute('UPDATE speech_projects SET revision=?,data=?,updated=? WHERE id=? AND revision=?',
                                                    (value['revision'], encoded, value['updated'], key, revision)).rowcount
                    if not changed: raise RevisionConflict('Project changed. Reload before saving.')
                else:
                    self.store.db.execute('INSERT INTO speech_projects VALUES (?,?,?,?,?)',
                                          (key, value['revision'], encoded, value['created'], value['updated']))
                self.store.db.execute('INSERT INTO speech_project_revisions VALUES (?,?,?)', (key, value['revision'], encoded))
                self.store.db.commit()
            except BaseException:
                self.store.db.rollback()
                raise
        return copy.deepcopy(value)

    def delete_project(self, id, revision=None):
        # Explicit project deletion retains historical render recipes/audio;
        # it never unlinks a user file or erases generated segment evidence.
        identifier(id, 'project ID')
        with self.store.lock:
            old = self.store.db.execute('SELECT revision FROM speech_projects WHERE id=?', (id,)).fetchone()
            if not old: raise ValueError('Speech project not found.')
            if revision is not None and _revision(revision) != old[0]: raise RevisionConflict('Project changed. Reload before deleting.')
            self.store.db.execute('DELETE FROM speech_projects WHERE id=?', (id,))
            self.store.db.commit()

    def records(self, kind):
        kind = _kind(kind)
        builtins = EMOTIONS if kind == 'emotions' else READING_STYLES if kind == 'styles' else []
        with self.store.lock:
            rows = self.store.db.execute('SELECT data FROM speech_records WHERE kind=? ORDER BY updated DESC,id', (kind,)).fetchall()
        return copy.deepcopy(builtins) + [json.loads(row[0]) for row in rows]

    def record(self, kind, id, revision=None):
        kind = _kind(kind); identifier(id, 'record ID')
        if revision is None:
            for value in self.records(kind):
                if value['id'] == id: return value
            raise ValueError('Speech record not found.')
        _revision(revision)
        with self.store.lock:
            row = self.store.db.execute('SELECT data FROM speech_record_revisions WHERE kind=? AND id=? AND revision=?', (kind, id, revision)).fetchone()
        if not row: raise ValueError('Speech record revision not found.')
        return json.loads(row[0])

    def save_record(self, kind, body):
        kind = _kind(kind)
        if not isinstance(body, dict): raise ValueError('Speech profile must be a JSON object.')
        _json(body)
        value = copy.deepcopy(body)
        key = identifier(value.get('id') or uuid.uuid4().hex, 'record ID')
        revision = _revision(value.get('revision', 0))
        if any(item['id'] == key for item in (EMOTIONS if kind == 'emotions' else READING_STYLES if kind == 'styles' else [])):
            raise ValueError('Duplicate the built-in preset before editing it.')
        _name(value, 'Untitled ' + kind.rstrip('s'))
        if kind == 'voices':
            nested_options = value.get('options', {})
            if not isinstance(nested_options, dict): raise ValueError('Voice profile options must be a JSON object.')
            if any(value.get(field) for field in ('reference', 'reference_audio', 'reference_source')) and value.get('consent') is not True:
                raise ValueError('Reference voice profiles require explicit permission to use the voice.')
            if any(nested_options.get(field) for field in ('reference', 'reference_audio', 'reference_source')) and nested_options.get('consent') is not True:
                raise ValueError('Reference voice profile options require explicit permission to use the voice.')
            weights = value.get('weights', value.get('blend'))
            if isinstance(weights, (dict, list)) and (weights or value.get('method') == 'blend' or 'weights' in value):
                if value.get('engine', value.get('model', 'kokoro')) not in ('kokoro', 'kokoro-82m'):
                    raise ValueError('Embedding blends are supported only within compatible Kokoro voices.')
                if isinstance(weights, list):
                    from .speech_engines import normalize_blend
                    normalize_blend(weights)
                else:
                    from .speech_engines import normalize_blend
                    normalize_blend([{'voice': voice, 'weight': weight} for voice, weight in weights.items()])
            elif weights is not None and not isinstance(weights, (dict, list)):
                raise ValueError('Voice blend must be a recipe list or weights object.')
            if isinstance(weights, dict):
                if not weights or len(weights) > 32 or any(isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight < 0 for weight in weights.values()) or sum(weights.values()) <= 0:
                    raise ValueError('Voice blends require positive finite total weight and at most 32 sources.')
                if value.get('engine', value.get('model', 'kokoro')) not in ('kokoro', 'kokoro-82m'):
                    raise ValueError('Embedding blends are supported only within compatible Kokoro voices.')
                for source in weights: identifier(source, 'source voice ID')
            nested_blend = nested_options.get('blend', nested_options.get('weights'))
            if nested_blend:
                if nested_options.get('engine', value.get('engine', 'kokoro')) not in ('kokoro', 'kokoro-82m'):
                    raise ValueError('Embedding blends are supported only within compatible Kokoro voices.')
                from .speech_engines import normalize_blend
                if isinstance(nested_blend, dict): nested_blend = [{'voice': voice, 'weight': weight} for voice, weight in nested_blend.items()]
                normalize_blend(nested_blend)
        now = time.time()
        with self.store.lock:
            old = self.store.db.execute('SELECT revision,data FROM speech_records WHERE kind=? AND id=?', (kind, key)).fetchone()
            if old and revision != old['revision'] or not old and revision:
                raise RevisionConflict('Preset changed. Reload it before saving.')
            if not old and self.store.db.execute('SELECT 1 FROM speech_record_revisions WHERE kind=? AND id=? LIMIT 1', (kind, key)).fetchone():
                raise ValueError('Deleted preset IDs are reserved for historical render recipes; duplicate with a new ID.')
            value.update(id=key, revision=revision + 1, kind=kind, builtin=False,
                         created=json.loads(old['data'])['created'] if old else now, updated=now)
            encoded = _json(value)
            try:
                if old:
                    count = self.store.db.execute('UPDATE speech_records SET revision=?,data=?,updated=? WHERE kind=? AND id=? AND revision=?',
                                                  (value['revision'], encoded, now, kind, key, revision)).rowcount
                    if not count: raise RevisionConflict('Preset changed. Reload before saving.')
                else:
                    self.store.db.execute('INSERT INTO speech_records VALUES (?,?,?,?,?)', (kind, key, value['revision'], encoded, now))
                self.store.db.execute('INSERT INTO speech_record_revisions VALUES (?,?,?,?)', (kind, key, value['revision'], encoded))
                self.store.db.commit()
            except BaseException:
                self.store.db.rollback()
                raise
        return copy.deepcopy(value)

    def delete_record(self, kind, id):
        kind = _kind(kind); identifier(id, 'record ID')
        with self.store.lock:
            count = self.store.db.execute('DELETE FROM speech_records WHERE kind=? AND id=?', (kind, id)).rowcount
            self.store.db.commit()
        if not count: raise ValueError('Custom speech record not found; built-in presets cannot be deleted.')

    def record_render(self, project_id, segment_id, body):
        identifier(project_id, 'project ID'); identifier(segment_id, 'segment ID')
        if not isinstance(body, dict): raise ValueError('Render evidence must be an object.')
        value = copy.deepcopy(body)
        # Store the complete actual recipe, including exact engine/model revision,
        # preset snapshot and source content hash, rather than a mutable preset name.
        recipe = value.get('recipe')
        if not isinstance(recipe, dict) or not recipe.get('engine') or not recipe.get('model_revision'):
            raise ValueError('Render evidence requires the engine and exact model revision recipe.')
        if not isinstance(value.get('source_sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', value['source_sha256']):
            raise ValueError('Render evidence requires the source SHA-256.')
        if not isinstance(recipe['model_revision'], str) or not re.fullmatch(r'[a-f0-9]{40}', recipe['model_revision']):
            raise ValueError('Render model revision must be an immutable 40-character commit SHA.')
        fingerprint = hashlib.sha256(_json({'source_sha256': value['source_sha256'], 'recipe': recipe}).encode()).hexdigest()
        key = identifier(value.get('id') or uuid.uuid4().hex, 'render ID')
        value.update(id=key, project_id=project_id, segment_id=segment_id, fingerprint=fingerprint, created=time.time())
        encoded = _json(value)
        with self.store.lock:
            old = self.store.db.execute('SELECT data FROM speech_renders WHERE id=? OR (project_id=? AND segment_id=? AND fingerprint=?)', (key, project_id, segment_id, fingerprint)).fetchone()
            if old:
                previous = json.loads(old[0])
                if previous['fingerprint'] != fingerprint or previous['project_id'] != project_id or previous['segment_id'] != segment_id:
                    raise ValueError('Render evidence is immutable.')
                return previous
            self.store.db.execute('INSERT INTO speech_renders VALUES (?,?,?,?,?,?)', (key, project_id, segment_id, fingerprint, encoded, value['created']))
            self.store.db.commit()
        return value

    def renders(self, project_id):
        identifier(project_id, 'project ID')
        with self.store.lock:
            rows = self.store.db.execute('SELECT data FROM speech_renders WHERE project_id=? ORDER BY created,id', (project_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]
