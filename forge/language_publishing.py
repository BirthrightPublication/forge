"""Reviewed, channel-bound YouTube publishing over FORGE's existing queue.

Normal exports remain local. No render can publish automatically: a short-lived
review capability binds the exact file, channel connection and edited metadata.
Tokens and resumable-upload URLs live only in the Windows DPAPI credential vault.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import tempfile
import time
from urllib.parse import urlencode
import uuid

from fastapi import HTTPException, Response as WebResponse

from .core import VIDEO, absolute_path, json_write, safe_name
from .publishing_oauth import (CAPTIONS, CAPTIONS_SCOPE, CHANNELS, READ_SCOPE, REVOKE, STUDIO,
    TOKEN, UPLOAD_SCOPE, VIDEOS, VIDEO_RESOURCE, CredentialVault, GoogleHTTP, OAuthFlow, PublishingError,
    check_response, normalize_client, sanitize_channels, upload_url)

PROFILE_FIELDS = {'name', 'language', 'platform', 'default_visibility', 'category_id', 'title_format',
    'description_template', 'hashtags', 'tags', 'subtitle_preferences', 'schedule_timezone',
    'voice_profile_id', 'translation_style', 'lip_sync', 'export_directory', 'source_video_id'}
IDENTITY = re.compile(r'^[A-Za-z0-9_-]{1,100}$')
TABLES = {'publishing_profiles', 'publishing_connections', 'publishing_records', 'publishing_approvals'}
SCOPES_NOTE = ('Register a Google Cloud project, enable YouTube Data API v3, configure the consent screen and create a Desktop app OAuth client. '
    'Google may require test-user access, verification or an upload audit. FORGE cannot bypass those requirements. '
    'Export and open YouTube Studio remains available without API credentials.')


@contextmanager
def owned_lock(data, key, *, progress=None, cancel=None, timeout=60):
    from .model_cache import cache_lock
    if not IDENTITY.fullmatch(key):
        raise PublishingError('Invalid publishing transfer identity.')
    folder = Path(data).resolve() / 'publishing' / 'locks'
    path = folder / (key + '.lock')
    if folder.parent.is_symlink() or folder.is_symlink() or path.is_symlink():
        raise PublishingError('The publishing lock location cannot be a symbolic link.')
    def waiting(*args, **kwargs):
        if cancel and cancel():
            raise PublishingError('Upload cancelled while waiting for the previous attempt.', code='upload_cancelled', retryable=True)
        if progress:
            progress(0, 'Waiting for the previous publishing attempt', stage='YouTube transfer lock', progress_measured=False)
    try:
        with cache_lock(path, waiting, timeout=timeout):
            yield
    except RuntimeError as error:
        if str(error).startswith('Timed out waiting for the model cache.'):
            raise PublishingError('The previous publishing attempt is still finishing. Retry after it stops.', code='upload_busy', retryable=True) from None
        raise


def digest_file(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def write_new_json(folder, name, value):
    from .worker import publish
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.forge-publishing-', dir=folder) as temporary:
        stage = Path(temporary) / name
        json_write(stage, value)
        return publish(stage, folder / name)


def fingerprint(path):
    path = Path(path)
    stat = path.stat()
    if not path.is_file() or path.is_symlink() or stat.st_size <= 0:
        raise PublishingError('Choose a nonempty regular video or subtitle file.')
    sha = digest_file(path)
    after = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise PublishingError('The selected file changed while preparing the upload. Review it again.', code='source_changed')
    return {'path': str(path.resolve()), 'name': path.name, 'size': stat.st_size,
            'mtime_ns': stat.st_mtime_ns, 'sha256': sha}


def verify_file(value):
    try:
        path = absolute_path(value['path'])
        current = fingerprint(path)
    except (OSError, KeyError):
        raise PublishingError('The approved media is no longer available. Prepare a new upload review.', code='source_changed') from None
    if any(current[key] != value.get(key) for key in ('path', 'size', 'mtime_ns', 'sha256')):
        raise PublishingError('The approved media changed. Prepare a new upload review before sending it.', code='source_changed')
    return path


def checked_metadata(value, profile):
    if not isinstance(value, dict):
        raise PublishingError('Review the upload metadata before publishing.')
    allowed = {'title', 'description', 'tags', 'category_id', 'language', 'visibility', 'publish_at',
               'contains_synthetic_media', 'made_for_kids', 'source_video_id'}
    if set(value) - allowed:
        raise PublishingError('The upload metadata contains unsupported fields.')
    if not isinstance(value.get('title'), str) or not isinstance(value.get('description', ''), str):
        raise PublishingError('Use reviewed plain-text video title and description fields.')
    title = value['title'].strip()
    description = value.get('description', '')
    if not title or len(title) > 100 or any(char in title for char in '<>\x00'):
        raise PublishingError('Use a reviewed title of 1–100 characters without angle brackets.')
    if len(description.encode('utf-8')) > 5000 or '\x00' in description:
        raise PublishingError('The reviewed description must fit YouTube’s 5,000-byte limit.')
    tags = value.get('tags', profile.get('tags', []))
    if not isinstance(tags, list) or len(tags) > 100 or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 100 for tag in tags):
        raise PublishingError('Use a list of short, nonempty tags.')
    tags = list(dict.fromkeys(tag.strip() for tag in tags))
    if sum(len(tag) + 1 for tag in tags) > 500:
        raise PublishingError('The reviewed tags exceed YouTube’s supported combined length.')
    language = value.get('language', profile.get('language', 'ru'))
    if language != profile.get('language', 'ru'):
        raise PublishingError('The reviewed language must match the publishing profile.')
    visibility = value.get('visibility', profile.get('default_visibility', 'private'))
    if not isinstance(visibility, str) or visibility not in {'private', 'unlisted', 'public'}:
        raise PublishingError('Choose Private, Unlisted or Public visibility.')
    category = str(value.get('category_id', profile.get('category_id', '22')))
    if not re.fullmatch(r'[0-9]{1,3}', category):
        raise PublishingError('Choose a supported numeric YouTube category.')
    publish_at = value.get('publish_at') or ''
    if publish_at:
        try:
            scheduled = datetime.fromisoformat(str(publish_at).replace('Z', '+00:00'))
            if scheduled.tzinfo is None or scheduled.timestamp() <= time.time() + 300:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise PublishingError('Choose a scheduled time with a timezone, at least five minutes in the future.') from None
        if visibility != 'private':
            raise PublishingError('Scheduled YouTube publication must start with Private visibility.')
        publish_at = scheduled.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    for key in ('contains_synthetic_media', 'made_for_kids'):
        if key in value and type(value[key]) is not bool:
            raise PublishingError('Review the audience and synthetic-media disclosure choices.')
    if 'made_for_kids' not in value or 'contains_synthetic_media' not in value:
        raise PublishingError('Explicitly review the audience and synthetic-media disclosure settings.')
    source_video_id = str(value.get('source_video_id', profile.get('source_video_id', '')))
    if source_video_id and not re.fullmatch(r'[A-Za-z0-9_-]{11}', source_video_id):
        raise PublishingError('The English source video ID is invalid.')
    return {'title': title, 'description': description, 'tags': tags, 'category_id': category,
            'language': language, 'visibility': visibility, 'publish_at': publish_at,
            'contains_synthetic_media': value['contains_synthetic_media'], 'made_for_kids': value['made_for_kids'],
            'source_video_id': source_video_id}


class Repository:
    """A worker connection must not invoke Store's application-restart recovery."""
    def __init__(self, store=None, data=None):
        self.owned = store is None
        self.data = Path(data if store is None else store.data).resolve()
        self.lock = threading.RLock() if store is None else store.lock
        if store is None:
            self.db = sqlite3.connect(self.data / 'forge.sqlite3', timeout=30, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
        else:
            self.db = store.db
        with self.lock, self.db:
            for name in sorted(TABLES):
                self.db.execute(f'CREATE TABLE IF NOT EXISTS {name}(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,data TEXT NOT NULL)')

    def get(self, table, key, required=True):
        if table not in TABLES:
            raise PublishingError('Unsupported publishing record type.')
        with self.lock:
            row = self.db.execute(f'SELECT revision,data FROM {table} WHERE id=?', (key,)).fetchone()
        if row is None:
            if required:
                raise PublishingError('The publishing record is no longer available.')
            return None
        result = json.loads(row['data'])
        result['revision'] = row['revision']
        return result

    def list(self, table, limit=200):
        if table not in TABLES:
            raise PublishingError('Unsupported publishing record type.')
        with self.lock:
            rows = self.db.execute(f'SELECT revision,data FROM {table} ORDER BY json_extract(data,\'$.updated\') DESC LIMIT ?', (min(1000, max(1, limit)),)).fetchall()
        return [{**json.loads(row['data']), 'revision': row['revision']} for row in rows]

    def put(self, table, value, expected=None):
        if table not in TABLES:
            raise PublishingError('Unsupported publishing record type.')
        key = value['id']
        if not IDENTITY.fullmatch(key):
            raise PublishingError('Invalid publishing record identity.')
        with self.lock, self.db:
            if not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE')
            current = self.db.execute(f'SELECT revision FROM {table} WHERE id=?', (key,)).fetchone()
            revision = current['revision'] if current else 0
            if expected is not None and expected != revision:
                raise PublishingError('This publishing record changed. Reload it before saving.', code='revision_conflict')
            result = copy.deepcopy(value)
            result.update(revision=revision + 1, updated=time.time())
            result.setdefault('created', result['updated'])
            self.db.execute(f'INSERT OR REPLACE INTO {table} VALUES(?,?,?)', (key, result['revision'], json.dumps(result, ensure_ascii=False)))
        return result

    def patch(self, table, key, fields):
        if table not in TABLES:
            raise PublishingError('Unsupported publishing record type.')
        with self.lock, self.db:
            if not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE')
            current = self.get(table, key)
            current.update(copy.deepcopy(fields))
            return self.put(table, current, current['revision'])

    def close(self):
        if self.owned:
            self.db.close()


class PublishingService:
    def __init__(self, store=None, *, data=None, http=None, vault=None):
        self.store = store
        self.repo = Repository(store, data)
        self.data = self.repo.data
        self.http = http if http is not None else GoogleHTTP()
        self.vault = vault if vault is not None else CredentialVault(self.data)
        self.flows, self.flow_lock = {}, threading.RLock()

    def oauth_state(self):
        configured = self.vault.exists('google-client')
        return {'configured': configured, 'secure_storage': self.vault.available,
                'setup_required': not configured or not self.vault.available,
                'note': SCOPES_NOTE, 'provider': 'Google OAuth 2.0 Desktop application',
                'permissions': [READ_SCOPE, UPLOAD_SCOPE, CAPTIONS_SCOPE],
                'storage': 'Windows DPAPI, current user' if self.vault.available else 'Unavailable; local export remains supported'}

    def _connection(self, profile):
        key = profile.get('connection_id', '')
        if not key:
            raise PublishingError('Connect and confirm the intended Russian YouTube channel first.', code='connection_missing')
        connection = self.repo.get('publishing_connections', key)
        if connection.get('state') != 'connected' or profile.get('connection_generation') != connection.get('generation'):
            raise PublishingError('This channel connection changed. Reconnect and explicitly confirm the intended channel.', code='channel_reverification_required')
        return connection

    def public_profile(self, profile):
        value = {key: copy.deepcopy(profile.get(key)) for key in PROFILE_FIELDS if key in profile}
        for key in ('id', 'revision', 'created', 'updated', 'channel_id', 'channel_name', 'channel_handle'):
            value[key] = profile.get(key, '')
        connection = self.repo.get('publishing_connections', profile.get('connection_id', ''), False)
        value['connected'] = bool(connection and connection.get('state') == 'connected'
                                  and connection.get('generation') == profile.get('connection_generation')
                                  and self.vault.available and self.vault.exists('connection-' + connection['id']))
        value['avatar'] = f'/api/language-publishing/profiles/{profile["id"]}/avatar' if value['connected'] and profile.get('channel_thumbnail') else ''
        return value

    def profiles(self):
        return [self.public_profile(value) for value in self.repo.list('publishing_profiles')]

    def profile(self, key):
        return self.repo.get('publishing_profiles', key)

    def save_profile(self, value, expected_revision=None):
        if not isinstance(value, dict) or len(json.dumps(value, ensure_ascii=False)) > 40000:
            raise PublishingError('Use a bounded language publishing profile.')
        if set(value) - PROFILE_FIELDS - {'id', 'revision', 'created', 'updated', 'channel_id', 'channel_name', 'channel_handle', 'connected', 'avatar'}:
            raise PublishingError('Publishing profiles cannot contain credentials or unsupported settings.')
        key = str(value.get('id') or uuid.uuid4().hex)
        with self.repo.lock:
            previous = self.repo.get('publishing_profiles', key, False)
            if previous and expected_revision is None:
                expected_revision = value.get('revision')
                if expected_revision is None:
                    raise PublishingError('Reload the profile before saving changes.', code='revision_conflict')
            result = copy.deepcopy(previous or {'id': key, 'language': 'ru', 'platform': 'youtube', 'default_visibility': 'private',
                'category_id': '22', 'tags': [], 'hashtags': [], 'subtitle_preferences': {'language': 'ru'},
                'translation_style': 'localized', 'lip_sync': 'off', 'channel_id': '', 'channel_name': '',
                'channel_handle': '', 'connection_id': '', 'connection_generation': 0, 'schedule_timezone': 'Europe/Moscow'})
            if value.get('channel_id', result['channel_id']) != result['channel_id']:
                raise PublishingError('Choose a channel returned by Connect and explicitly confirm its identity.', code='channel_confirmation_required')
            for field in PROFILE_FIELDS:
                if field in value:
                    result[field] = copy.deepcopy(value[field])
            if not isinstance(result.get('name'), str):
                raise PublishingError('Enter a plain-text profile name.')
            result['name'] = result['name'].strip()[:100]
            if not result['name'] or result.get('platform') != 'youtube' or not re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z]{2})?', str(result.get('language', ''))):
                raise PublishingError('Enter a profile name, supported language code and YouTube platform.')
            if not isinstance(result.get('default_visibility'), str) or result['default_visibility'] not in {'private', 'unlisted', 'public'}:
                raise PublishingError('Choose a supported default visibility.')
            for field in ('tags', 'hashtags'):
                values = result.get(field, [])
                if not isinstance(values, list) or len(values) > 100 or any(not isinstance(item, str) or len(item) > 100 or '\x00' in item for item in values):
                    raise PublishingError('Tags and hashtags must be bounded plain-text lists.')
                result[field] = list(dict.fromkeys(item.strip() for item in values if item.strip()))
            subtitles = result.get('subtitle_preferences', {})
            if (not isinstance(subtitles, dict) or set(subtitles) - {'enabled', 'language', 'name', 'format'}
                    or any(not isinstance(item, str) or len(item) > 100 for key, item in subtitles.items() if key != 'enabled')
                    or ('enabled' in subtitles and type(subtitles['enabled']) is not bool)):
                raise PublishingError('Subtitle preferences cannot contain credentials or unsupported settings.')
            if (not isinstance(result.get('translation_style'), str) or result['translation_style'] not in {'faithful', 'localized', 'alternative'}
                    or not isinstance(result.get('lip_sync'), str) or result['lip_sync'] not in {'off', 'automatic', 'selected', 'full'}):
                raise PublishingError('Choose a supported translation style and lip-sync preference.')
            if not isinstance(result.get('category_id'), str) or not re.fullmatch(r'[0-9]{1,3}', result['category_id']):
                raise PublishingError('Choose a numeric YouTube upload category.')
            for field in ('description_template', 'title_format', 'schedule_timezone', 'export_directory', 'voice_profile_id', 'source_video_id'):
                if field in result and (not isinstance(result[field], str) or len(result[field]) > (6000 if field == 'description_template' else 2000)):
                    raise PublishingError('A profile setting is invalid or too long.')
            saved = self.repo.put('publishing_profiles', result, expected_revision)
        return self.public_profile(saved)

    def save_client(self, body):
        if body.get('path'):
            path = absolute_path(str(body['path']))
            if path.suffix.lower() != '.json' or path.stat().st_size > 64000:
                raise PublishingError('Choose the downloaded Google Desktop OAuth JSON, at most 64 KB.')
            try:
                value = json.loads(path.read_text('utf-8-sig'))
            except (ValueError, UnicodeError):
                raise PublishingError('The Google Desktop client JSON is invalid.') from None
        else:
            value = {key: body[key] for key in ('client_id', 'client_secret') if key in body}
        value = normalize_client(value)
        if self.vault.exists('google-client') and self.vault.available:
            current = self.vault.read('google-client')
            if current['client_id'] != value['client_id'] and any(profile.get('connected') for profile in self.profiles()):
                raise PublishingError('Disconnect existing channel profiles before changing the Google OAuth client.', code='oauth_client_changed')
        self.vault.save('google-client', value)
        return self.oauth_state()

    def start_oauth(self, body):
        profile = self.profile(str(body.get('profile_id', '')))
        if 'captions' in body and type(body['captions']) is not bool:
            raise PublishingError('Choose whether to request YouTube management and subtitle permissions.')
        if not self.vault.available:
            raise PublishingError('Secure YouTube connections require Windows. Export locally and open YouTube Studio instead.', code='secure_storage_unavailable')
        client = self.vault.read('google-client')
        if profile.get('connection_id') and body.get('allow_account_change') is not True:
            raise PublishingError('Confirm reconnecting or changing this profile’s Google account first.', code='account_change_confirmation_required')
        with self.flow_lock:
            for key, value in list(self.flows.items()):
                if value['flow'].expires < time.time():
                    value['flow'].close()
                    del self.flows[key]
            if len(self.flows) >= 4:
                raise PublishingError('Finish or close an existing channel connection before starting another.')
            client_id = client['client_id']
            def complete(tokens, channels):
                connection_id = uuid.uuid4().hex
                self.vault.save('connection-' + connection_id, tokens)
                self.repo.put('publishing_connections', {'id': connection_id, 'generation': 1, 'state': 'pending_confirmation',
                    'channels': channels, 'scopes': tokens['scopes'], 'profile_id': profile['id'], 'client_id': client_id})
                return connection_id
            flow = OAuthFlow(client, self.http, complete, captions=body.get('captions', True) is True)
            self.flows[flow.id] = {'flow': flow, 'profile_id': profile['id'], 'profile_revision': profile['revision']}
        return {'flow_id': flow.id, 'authorization_url': flow.authorization_url, 'expires_at': flow.expires}

    def oauth_status(self, flow_id):
        with self.flow_lock:
            value = self.flows.get(flow_id)
            if value is None:
                raise PublishingError('This connection flow is no longer active. Start Connect again.')
            return value['flow'].public()

    def cancel_oauth(self, flow_id):
        with self.flow_lock:
            value = self.flows.pop(flow_id, None)
            if value:
                flow = value['flow']
                flow.close()
                if flow.connection_id and flow.status != 'connected':
                    pending = self.repo.get('publishing_connections', flow.connection_id, False)
                    if pending and pending.get('state') == 'pending_confirmation':
                        pending['state'] = 'cancelled'
                        self.repo.put('publishing_connections', pending)
                        self.vault.remove('connection-' + pending['id'])
        return {'cancelled': True, 'note': 'The pending connection was closed. Confirmed channel profiles were kept.'}

    def confirm_channel(self, profile_id, body):
        if body.get('confirm') is not True:
            raise PublishingError('Explicitly confirm the exact channel name and ID.')
        with self.flow_lock, self.repo.lock:
            pending = self.flows.get(str(body.get('flow_id', '')))
            if not pending or pending['profile_id'] != profile_id:
                raise PublishingError('The connection flow belongs to another profile or has expired.')
            flow = pending['flow']
            if flow.status != 'select_channel' or time.time() >= flow.expires:
                raise PublishingError('Complete Google authorization and choose an available channel first.')
            channel = next((item for item in flow.channels if item['id'] == body.get('channel_id')), None)
            if channel is None:
                raise PublishingError('Choose a channel actually authorized by this Google connection.', code='channel_confirmation_required')
            if len(flow.channels) != 1:
                raise PublishingError('This authorization has an ambiguous channel context. Reconnect and choose the exact Russian Brand channel in Google, or upload manually in YouTube Studio.', code='ambiguous_channel')
            profile = self.profile(profile_id)
            expected = body.get('expected_revision', pending['profile_revision'])
            if profile['revision'] != expected:
                raise PublishingError('This profile changed during connection. Review it before choosing the channel.', code='revision_conflict')
            connection = self.repo.get('publishing_connections', flow.connection_id)
            if connection.get('profile_id') != profile_id:
                raise PublishingError('This connection belongs to another profile.')
            old_connection = profile.get('connection_id')
            profile.update(connection_id=connection['id'], connection_generation=connection['generation'],
                channel_id=channel['id'], channel_name=channel['title'], channel_handle=channel['handle'], channel_thumbnail=channel['thumbnail'])
            connection.update(state='connected', selected_channel_id=channel['id'])
            self.repo.put('publishing_connections', connection, connection['revision'])
            saved = self.repo.put('publishing_profiles', profile, expected)
            if old_connection and old_connection != connection['id']:
                old = self.repo.get('publishing_connections', old_connection, False)
                if old:
                    old.update(state='disconnected', generation=old['generation'] + 1)
                    self.repo.put('publishing_connections', old)
                    self.vault.remove('connection-' + old_connection)
            flow.status, flow.message = 'connected', 'The intended channel is confirmed.'
        return self.public_profile(saved)

    def disconnect(self, profile_id):
        # Local disconnection always succeeds offline. No revocation request may
        # accidentally revoke another profile's connection to the same account.
        with self.repo.lock:
            profile = self.profile(profile_id)
            key = profile.get('connection_id')
            if key:
                connection = self.repo.get('publishing_connections', key, False)
                if connection:
                    connection.update(state='disconnected', generation=connection['generation'] + 1)
                    self.repo.put('publishing_connections', connection)
                self.vault.remove('connection-' + key)
            profile.update(connection_id='', connection_generation=0)
            return self.public_profile(self.repo.put('publishing_profiles', profile))

    def access_token(self, profile):
        connection = self._connection(profile)
        with owned_lock(self.data, 'connection-' + connection['id']):
            return self._access_token_locked(connection)

    def _access_token_locked(self, connection):
        value = self.vault.read('connection-' + connection['id'])
        if value.get('expires_at', 0) <= time.time() + 60:
            if not value.get('refresh_token'):
                raise PublishingError('This Google authorization expired. Reconnect the intended channel.', code='oauth_expired')
            client = self.vault.read('google-client')
            if client['client_id'] != connection.get('client_id'):
                raise PublishingError('This connection belongs to a different Google OAuth client. Reconnect the intended channel.', code='oauth_client_changed')
            response = check_response(self.http.request('POST', TOKEN, headers={'Content-Type': 'application/x-www-form-urlencoded'},
                data=urlencode({'grant_type': 'refresh_token', 'refresh_token': value['refresh_token'], **client}).encode()))
            refreshed = response.json()
            if not refreshed.get('access_token'):
                raise PublishingError('Google did not renew this connection. Reconnect the intended channel.', code='oauth_expired')
            value.update({key: refreshed[key] for key in ('access_token', 'refresh_token', 'scope') if key in refreshed})
            value['expires_at'] = time.time() + min(86400, max(30, float(refreshed.get('expires_in', 3600))))
            self.vault.save('connection-' + connection['id'], value)
        token = value.get('access_token')
        if not isinstance(token, str) or not 1 <= len(token) <= 16384 or not token.isascii() or any(char.isspace() for char in token):
            raise PublishingError('The saved connection is invalid. Reconnect the intended channel.', code='oauth_token_invalid')
        return token, connection

    def verify_channel(self, profile):
        token, connection = self.access_token(profile)
        result = check_response(self.http.request('GET', CHANNELS + '?' + urlencode({'part': 'id,snippet', 'mine': 'true', 'maxResults': 50}),
            headers={'Authorization': 'Bearer ' + token}))
        channels = sanitize_channels(result.json())
        if len(channels) != 1:
            raise PublishingError('Google returned an ambiguous channel context. Reconnect and choose the exact Russian Brand channel before uploading.', code='ambiguous_channel')
        channel = next((item for item in channels if item['id'] == profile.get('channel_id')), None)
        if channel is None or connection.get('selected_channel_id') != profile.get('channel_id'):
            raise PublishingError('The authorized channel no longer matches this profile. Reconnect and confirm the intended Russian channel.', code='wrong_channel')
        return channel, connection

    def prepare(self, body, *, api=True):
        if body.get('approved_metadata') is not True:
            raise PublishingError('Review the Russian metadata, audience and disclosure before preparing an upload.')
        profile = self.profile(str(body.get('profile_id', '')))
        metadata = checked_metadata(body.get('metadata', {}), profile)
        if body.get('source_id') and not isinstance(body['source_id'], str):
            raise PublishingError('Choose a registered video source identity.')
        selected = self.store.source(body['source_id']) if self.store and body.get('source_id') else {'path': body.get('path', '')}
        path = absolute_path(str(selected.get('path', '')))
        if path.suffix.lower() not in VIDEO:
            raise PublishingError('Choose a supported exported video, not an audio-only or project file.')
        from .worker import duration, probe
        try:
            inspected = probe(path, self.store.settings() if self.store is not None else {})
            if not any(stream.get('codec_type') == 'video' for stream in inspected.get('streams', [])) or duration(inspected) <= 0:
                raise ValueError()
        except (OSError, RuntimeError, ValueError):
            raise PublishingError('The selected file is not a verified playable video. Repair or export it before preparing an upload.', code='invalid_video') from None
        if path.stat().st_size > 256 * 1024 ** 3:
            raise PublishingError('The video exceeds YouTube’s supported 256 GB upload limit.', code='video_too_large')
        file = fingerprint(path)
        subtitles = None
        if body.get('subtitles_path'):
            subtitle_path = absolute_path(str(body['subtitles_path']))
            if subtitle_path.suffix.lower() not in {'.srt', '.vtt'} or subtitle_path.stat().st_size > 20 * 1024 * 1024:
                raise PublishingError('Choose a bounded Russian SRT or VTT subtitle file.')
            subtitles = fingerprint(subtitle_path)
        if api:
            channel, connection = self.verify_channel(profile)
            if subtitles and CAPTIONS_SCOPE not in connection.get('scopes', []):
                raise PublishingError('Reconnect with subtitle permission or use manual subtitle upload in YouTube Studio.', code='subtitle_permission_missing')
            if (metadata['visibility'] != 'private' or metadata['publish_at']) and CAPTIONS_SCOPE not in connection.get('scopes', []):
                raise PublishingError('Reconnect with YouTube management permission before changing visibility or scheduling. Private upload or YouTube Studio remains available.', code='publishing_permission_missing')
            existing = [value for value in self.repo.list('publishing_records', 1000)
                        if value.get('channel_id') == channel['id'] and value.get('video_id')
                        and (value.get('snapshot') or {}).get('file', {}).get('sha256') == file['sha256']]
            if existing and body.get('duplicate_confirmed') is not True:
                raise PublishingError('This exact media already has a recorded upload to the channel. Open its history or explicitly confirm another upload.', code='duplicate_upload_review')
        else:
            connection = {'id': '', 'generation': 0}
            channel = {'id': profile.get('channel_id', ''), 'title': profile.get('channel_name') or profile['name']}
        snapshot = {'profile_id': profile['id'], 'profile_revision': profile['revision'], 'connection_id': connection['id'],
            'connection_generation': connection['generation'], 'channel_id': channel['id'], 'channel_name': channel['title'],
            'file': file, 'subtitles': subtitles, 'metadata': metadata, 'project_id': str(body.get('project_id', ''))[:100]}
        review = {**{key: snapshot[key] for key in ('profile_id', 'profile_revision', 'channel_id', 'channel_name', 'metadata', 'project_id')},
                  'file_name': file['name'], 'file_size': file['size'], 'file_sha256': file['sha256'],
                  'subtitle_status': 'Included: ' + subtitles['name'] if subtitles else 'No subtitle upload selected',
                  'api_project_note': SCOPES_NOTE}
        if not api:
            return snapshot, review
        raw = secrets.token_urlsafe(32)
        key = hashlib.sha256(raw.encode()).hexdigest()
        expires = time.time() + 900
        self.repo.put('publishing_approvals', {'id': key, 'expires': expires, 'consumed': False, 'snapshot': snapshot})
        return {'review': review, 'review_token': raw, 'expires_at': expires}

    @staticmethod
    def public_record(value):
        allowed = {'id', 'revision', 'created', 'updated', 'profile_id', 'project_id', 'channel_id', 'actual_channel_id', 'channel_name', 'status',
            'title', 'visibility', 'publish_at', 'file_name', 'file_size', 'uploaded_bytes', 'video_id', 'video_url',
            'caption_id', 'caption_status', 'message', 'job_id', 'uploaded_at', 'published_at', 'contains_synthetic_media'}
        return {key: copy.deepcopy(value[key]) for key in allowed if key in value}

    def history(self, profile_id=None):
        result = []
        for value in self.repo.list('publishing_records', 1000):
            if profile_id is not None and value['profile_id'] != profile_id:
                continue
            if value.get('status') in {'queued', 'uploading'} and value.get('job_id'):
                with self.repo.lock:
                    row = self.repo.db.execute('SELECT status,message FROM jobs WHERE id=?', (value['job_id'],)).fetchone()
                if row and row['status'] in {'failed', 'cancelled', 'interrupted', 'blocked'}:
                    value.update(status=row['status'], message=str(row['message'])[:2000])
                    try:
                        value = self.repo.put('publishing_records', value, value['revision'])
                    except PublishingError as error:
                        if error.code != 'revision_conflict':
                            raise
                        value = self.repo.get('publishing_records', value['id'])
            result.append(self.public_record(value))
        return result

    def enqueue(self, body, manager):
        if body.get('approve') is not True:
            raise PublishingError('Use the explicit Publish or Schedule action after reviewing the upload.')
        raw = str(body.get('review_token', ''))
        if not 30 <= len(raw) <= 200:
            raise PublishingError('Prepare a fresh upload review first.')
        key = hashlib.sha256(raw.encode()).hexdigest()
        approval = self.repo.get('publishing_approvals', key)
        if approval['expires'] <= time.time():
            raise PublishingError('The upload review expired. Review the file, channel and metadata again.', code='review_expired')
        snapshot = approval['snapshot']
        verify_file(snapshot['file'])
        if snapshot.get('subtitles'):
            verify_file(snapshot['subtitles'])
        current = self.profile(snapshot['profile_id'])
        if current['revision'] != snapshot['profile_revision'] or current.get('connection_id') != snapshot['connection_id']:
            raise PublishingError('The channel profile changed after review. Prepare a new upload review.', code='profile_changed')
        self.verify_channel(current)
        # Hash/network work has finished before the scheduler/database locks.
        with manager.lock, self.repo.lock, self.repo.db:
            manager.check_accepting()
            if not self.repo.db.in_transaction:
                self.repo.db.execute('BEGIN IMMEDIATE')
            latest = self.repo.get('publishing_approvals', key)
            current = self.profile(snapshot['profile_id'])
            if current['revision'] != snapshot['profile_revision'] or current.get('connection_generation') != snapshot['connection_generation']:
                raise PublishingError('The channel connection changed after review.', code='profile_changed')
            if latest['consumed']:
                record = self.repo.get('publishing_records', latest['record_id'])
                return record, self.store.job(record['job_id'])
            record_id, job_id, now = uuid.uuid4().hex, uuid.uuid4().hex, time.time()
            folder = self.data / 'publishing' / 'reports'
            metadata = snapshot['metadata']
            payload = {'tool': 'youtube_upload', 'options': {'record_id': record_id, 'profile_id': snapshot['profile_id']},
                'settings': self.store.settings(), 'source': {'path': snapshot['file']['path'], 'name': snapshot['file']['name'], 'size': snapshot['file']['size']},
                'source_stat': {'size': snapshot['file']['size'], 'mtime_ns': snapshot['file']['mtime_ns']},
                'destination': str(folder), 'job_id': job_id, 'priority': 'normal', 'batch_id': record_id}
            record = {'id': record_id, 'revision': 1, 'created': now, 'updated': now, 'profile_id': snapshot['profile_id'],
                'project_id': snapshot['project_id'], 'channel_id': snapshot['channel_id'], 'channel_name': snapshot['channel_name'],
                'status': 'queued', 'title': metadata['title'], 'visibility': metadata['visibility'], 'publish_at': metadata['publish_at'],
                'contains_synthetic_media': metadata['contains_synthetic_media'], 'file_name': snapshot['file']['name'],
                'file_size': snapshot['file']['size'], 'uploaded_bytes': 0, 'job_id': job_id, 'approved_at': now,
                'snapshot': snapshot, 'caption_status': 'pending' if snapshot.get('subtitles') else 'not_requested'}
            latest.update(consumed=True, record_id=record_id)
            self.repo.db.execute('INSERT INTO publishing_records VALUES(?,?,?)', (record_id, 1, json.dumps(record, ensure_ascii=False)))
            self.repo.db.execute('UPDATE publishing_approvals SET revision=revision+1,data=? WHERE id=? AND revision=?',
                                 (json.dumps(latest, ensure_ascii=False), key, latest['revision']))
            self.repo.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)', (job_id, 'youtube_upload', metadata['title'], 'queued', 0,
                'Waiting for a publishing slot; reviewed channel and metadata are frozen.', json.dumps(payload, ensure_ascii=False), '[]', '[]', now, now))
            self.repo.db.execute('INSERT INTO job_meta VALUES(?,?)', (job_id, json.dumps({'priority': 'normal', 'batch_id': record_id,
                'workflow_name': 'Language Publishing', 'resource': 'network', 'stage': 'Awaiting upload', 'progress_measured': False})))
        return record, self.store.job(job_id)

    def manual(self, body):
        snapshot, review = self.prepare(body, api=False)
        profile = self.profile(snapshot['profile_id'])
        folder = absolute_path(str(body.get('directory') or profile.get('export_directory') or self.data / 'publishing' / 'manual'), directory=True, must_exist=False)
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / (safe_name(snapshot['metadata']['title'])[:80] + '-youtube-' + uuid.uuid4().hex[:8] + '.json')
        value = {'schema': 1, 'platform': 'youtube', 'language': snapshot['metadata']['language'], 'review': review,
                 'video_path': snapshot['file']['path'], 'subtitles_path': snapshot['subtitles']['path'] if snapshot['subtitles'] else '',
                 'studio_url': STUDIO, 'instruction': 'Choose the confirmed channel in YouTube Studio, upload this video and review all metadata, audience, disclosure and visibility settings.'}
        destination = write_new_json(folder, destination.name, value)
        return {'files': [{'name': destination.name, 'path': str(destination), 'size': destination.stat().st_size}],
                'studio_url': STUDIO, 'review': review, 'note': 'Local preparation only. No video or metadata was sent to YouTube.'}

    def state(self):
        oauth = self.oauth_state()
        return {'profiles': self.profiles(), 'history': self.history(), 'oauth': oauth,
            'capabilities': {'api_publishing': oauth['secure_storage'] and oauth['configured'], 'manual_fallback': True,
                             'alternate_audio_tracks': False, 'analytics': False},
            'defaults': {'language': 'ru', 'platform': 'youtube', 'default_visibility': 'private', 'translation_style': 'localized', 'lip_sync': 'off'}}

    def avatar(self, profile_id):
        """Cache a bounded public channel avatar without forwarding OAuth data."""
        from io import BytesIO
        from urllib.parse import urlparse
        from urllib.request import Request, build_opener
        from urllib.error import HTTPError, URLError
        from PIL import Image
        from .publishing_oauth import NoRedirects
        profile = self.profile(profile_id)
        self._connection(profile)
        url = str(profile.get('channel_thumbnail', ''))
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or parsed.port not in {None, 443} or parsed.username or parsed.password
                or not parsed.hostname or not (parsed.hostname.endswith('.googleusercontent.com') or parsed.hostname == 'yt3.ggpht.com')):
            raise PublishingError('A supported channel avatar is not available.')
        root = self.data / 'publishing' / 'avatars'
        if root.parent.is_symlink() or root.is_symlink():
            raise PublishingError('The avatar cache cannot be a symbolic link.')
        root.mkdir(parents=True, exist_ok=True)
        target = root / (hashlib.sha256(url.encode()).hexdigest() + '.png')
        if target.is_file() and not target.is_symlink() and 0 < target.stat().st_size < 512 * 1024 and time.time() - target.stat().st_mtime < 86400:
            return target.read_bytes()
        try:
            request = Request(url, headers={'User-Agent': 'Birthright-FORGE/ChannelAvatar', 'Accept': 'image/*'})
            with build_opener(NoRedirects()).open(request, timeout=10) as response:
                raw = response.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise ValueError()
            with Image.open(BytesIO(raw)) as image:
                if image.width > 2048 or image.height > 2048 or image.format not in {'PNG', 'JPEG', 'WEBP'}:
                    raise ValueError()
                image.load()
                converted = image.convert('RGB')
                converted.info.clear()
                converted.thumbnail((128, 128))
                output = BytesIO()
                converted.save(output, 'PNG')
            result = output.getvalue()
        except (OSError, ValueError, HTTPError, URLError, Image.DecompressionBombError):
            raise PublishingError('The channel avatar is temporarily unavailable.') from None
        temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
        with temporary.open('xb') as handle:
            handle.write(result)
        if target.is_symlink():
            temporary.unlink(missing_ok=True)
            raise PublishingError('The avatar cache entry cannot be a symbolic link.')
        os.replace(temporary, target)
        owned = sorted((path for path in root.glob('*.png') if re.fullmatch(r'[a-f0-9]{64}\.png', path.name)
                        and path.is_file() and not path.is_symlink()), key=lambda path: path.stat().st_mtime)
        for old in owned[:-128]:
            old.unlink(missing_ok=True)
        return result

    def close(self):
        with self.flow_lock:
            for value in self.flows.values():
                flow = value['flow']
                flow.close()
                if flow.connection_id and flow.status != 'connected':
                    pending = self.repo.get('publishing_connections', flow.connection_id, False)
                    if pending and pending.get('state') == 'pending_confirmation':
                        pending['state'] = 'expired'
                        self.repo.put('publishing_connections', pending)
                        self.vault.remove('connection-' + pending['id'])
            self.flows.clear()
        self.repo.close()


def _video_body(metadata):
    # Insert privately even for an approved Public/Schedule action. Only release
    # it after the returned video's channel identity has been verified.
    status = {'privacyStatus': 'private', 'selfDeclaredMadeForKids': metadata['made_for_kids'],
              'containsSyntheticMedia': metadata['contains_synthetic_media']}
    return {'snippet': {'title': metadata['title'], 'description': metadata['description'], 'tags': metadata['tags'],
                        'categoryId': metadata['category_id'], 'defaultLanguage': metadata['language'],
                        'defaultAudioLanguage': metadata['language']}, 'status': status}


def _range_offset(response, total):
    value = response.headers.get('range', '')
    if not value:
        return 0
    match = re.fullmatch(r'bytes=0-([0-9]+)', value)
    if not match or int(match.group(1)) + 1 > total:
        raise PublishingError('Google returned an invalid resume position.', code='invalid_resume_position')
    return int(match.group(1)) + 1


def _execute_upload(service, record_id, *, progress=None, cancel=None, chunk_size=4 * 1024 * 1024, job_id=None):
    """Resume an approved video only; never manufacture progress or duplicate it."""
    progress = progress or (lambda *args, **kwargs: None)
    cancel = cancel or (lambda: False)
    if type(chunk_size) is not int or chunk_size < 256 * 1024 or chunk_size % (256 * 1024) or chunk_size > 16 * 1024 * 1024:
        raise PublishingError('Resumable upload chunks must use bounded multiples of 256 KB.')
    record = service.repo.get('publishing_records', record_id)
    snapshot = record.get('snapshot')
    if not snapshot or not record.get('approved_at'):
        raise PublishingError('This transfer has no explicit reviewed publishing approval.', code='approval_missing')
    if record.get('status') in {'uploaded', 'scheduled'} and record.get('video_id'):
        result = service.public_record(record)
        result['message'] = 'This reviewed transfer already completed. No new upload or visibility change was requested.'
        return result
    if job_id is not None:
        with service.repo.lock:
            queued = service.repo.db.execute('SELECT tool,payload FROM jobs WHERE id=?', (job_id,)).fetchone()
        if queued is None or queued['tool'] != 'youtube_upload':
            raise PublishingError('The approved upload has no matching queue record.', code='approval_job_mismatch')
        options = json.loads(queued['payload']).get('options', {})
        if options.get('record_id') != record_id or options.get('profile_id') != record['profile_id']:
            raise PublishingError('The queue record does not match this approved transfer.', code='approval_job_mismatch')
        record = service.repo.patch('publishing_records', record_id, {'job_id': job_id})
    profile = service.profile(record['profile_id'])
    if (profile['revision'] != snapshot['profile_revision'] or profile.get('connection_id') != snapshot['connection_id']
            or profile.get('connection_generation') != snapshot['connection_generation'] or profile.get('channel_id') != snapshot['channel_id']):
        raise PublishingError('The publishing profile or authorized account changed. Review a new upload.', code='profile_changed')
    path = verify_file(snapshot['file'])
    subtitle = verify_file(snapshot['subtitles']) if snapshot.get('subtitles') else None
    channel, connection = service.verify_channel(profile)
    if channel['id'] != record['channel_id']:
        raise PublishingError('The verified channel differs from the reviewed target. Nothing was uploaded.', code='wrong_channel')
    total, started = path.stat().st_size, time.monotonic()
    session_key = 'upload-' + record_id
    metadata = snapshot['metadata']

    def save(**values):
        nonlocal record
        record = service.repo.patch('publishing_records', record_id, values)

    def request(method, url, **kwargs):
        token, _ = service.access_token(profile)
        headers = {'Authorization': 'Bearer ' + token, **kwargs.pop('headers', {})}
        last = None
        for attempt in range(3):
            if cancel():
                raise PublishingError('Upload cancelled. Its approved transfer and any already uploaded video are retained.', code='upload_cancelled', retryable=True)
            try:
                response = service.http.request(method, url, headers=headers, **kwargs)
                if response.status == 429 or 500 <= response.status <= 599:
                    check_response(response)
                return response
            except PublishingError as error:
                last = error
                if not error.retryable or attempt == 2:
                    raise
                delay = max(1 + attempt, getattr(error, 'retry_after', 0))
                if delay > 30:
                    raise
                time.sleep(delay)
        raise last

    try:
        if not record.get('video_id'):
            session = service.vault.read(session_key) if service.vault.exists(session_key) else None
            response = None
            if session:
                if session.get('file_sha256') != snapshot['file']['sha256'] or session.get('channel_id') != record['channel_id']:
                    raise PublishingError('The saved transfer belongs to different reviewed media or another channel.', code='resume_identity_mismatch')
                url = upload_url(session['url'])
                response = request('PUT', url, headers={'Content-Length': '0', 'Content-Range': f'bytes */{total}'}, data=b'')
                if response.status in {200, 201}:
                    pass  # Crash after completion: recover the same video ID.
                elif response.status == 308:
                    session['offset'] = _range_offset(response, total)
                elif response.status in {404, 410}:
                    # A completed upload's expired session cannot safely prove
                    # it did not publish. Require review instead of duplicating.
                    raise PublishingError('The saved upload session expired. Check channel history before preparing another upload; FORGE did not create a duplicate.', code='upload_session_expired')
                else:
                    check_response(response, (308,))
            else:
                response = check_response(request('POST', VIDEOS + '?' + urlencode({'uploadType': 'resumable', 'part': 'snippet,status'}),
                    headers={'Content-Type': 'application/json; charset=utf-8', 'X-Upload-Content-Type': 'video/' + ('mp4' if path.suffix.lower() == '.mp4' else 'x-matroska'),
                             'X-Upload-Content-Length': str(total)}, data=json.dumps(_video_body(metadata), ensure_ascii=False).encode()), (200, 201))
                url = upload_url(response.headers.get('location', ''))
                session = {'url': url, 'offset': 0, 'file_sha256': snapshot['file']['sha256'], 'channel_id': record['channel_id']}
                service.vault.save(session_key, session)
                response = None
            save(status='uploading', message='Uploading to the confirmed channel.', uploaded_bytes=session.get('offset', total) if session else total)
            if response is None or response.status == 308:
                offset = session.get('offset', 0)
                initial_offset = offset
                with path.open('rb') as handle:
                    while offset < total:
                        if cancel():
                            raise PublishingError('Upload cancelled. The saved transfer is retained.', code='upload_cancelled', retryable=True)
                        handle.seek(offset)
                        stat = os.fstat(handle.fileno())
                        if stat.st_size != snapshot['file']['size'] or stat.st_mtime_ns != snapshot['file']['mtime_ns']:
                            raise PublishingError('The approved video changed during upload. The saved transfer is retained and no public release was requested.', code='source_changed')
                        chunk = handle.read(min(chunk_size, total - offset))
                        if not chunk:
                            raise PublishingError('The approved video changed during transfer.', code='source_changed')
                        end = offset + len(chunk) - 1
                        response = request('PUT', session['url'], headers={'Content-Type': 'application/octet-stream',
                            'Content-Length': str(len(chunk)), 'Content-Range': f'bytes {offset}-{end}/{total}'}, data=chunk)
                        if response.status in {200, 201}:
                            offset = total
                        elif response.status == 308:
                            acknowledged = _range_offset(response, total)
                            if acknowledged <= offset or acknowledged > end + 1:
                                raise PublishingError('The upload did not acknowledge a valid byte range. Retry the saved transfer.', code='invalid_resume_position', retryable=True)
                            offset = acknowledged
                        else:
                            check_response(response, (200, 201, 308))
                        session['offset'] = offset
                        service.vault.save(session_key, session)
                        elapsed = max(.001, time.monotonic() - started)
                        speed = max(0, offset - initial_offset) / elapsed
                        save(uploaded_bytes=offset)
                        progress(min(99, 100 * offset / total), 'Uploading reviewed video', stage='YouTube upload',
                                 progress_measured=True, downloaded_bytes=offset, total_bytes=total,
                                 speed_bps=speed, elapsed_seconds=elapsed, eta_seconds=(total - offset) / speed if speed else None,
                                 progress_scope='video_transfer', transfer_stage='upload')
            if response is None or response.status not in {200, 201}:
                # Upload bytes alone are not proof of a completed resource.
                response = check_response(request('PUT', session['url'], headers={'Content-Length': '0', 'Content-Range': f'bytes */{total}'}, data=b''), (200, 201))
            created = response.json()
            video_id = str(created.get('id', ''))
            if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
                raise PublishingError('YouTube did not return a verified video ID. Keep the saved transfer for recovery.', code='video_identity_missing')
            actual_status = created.get('status', {})
            actual_visibility = actual_status.get('privacyStatus', 'private')
            if actual_visibility not in {'private', 'unlisted', 'public'}:
                actual_visibility = 'private'
            save(video_id=video_id, video_url='https://www.youtube.com/watch?v=' + video_id,
                 status='video_uploaded', visibility=actual_visibility,
                 message='YouTube accepted the video; processing and visibility remain controlled by YouTube.', uploaded_bytes=total,
                 uploaded_at=time.time())
        # Required on resumed completion and subtitle/status retries too. An
        # ordinary videos.insert request cannot choose a channel by snippet ID.
        # Resolve the actual resource before uploading subtitles or publishing.
        video = check_response(request('GET', VIDEO_RESOURCE + '?' + urlencode({'part': 'id,snippet,status', 'id': record['video_id']}))).json()
        items = video.get('items') or []
        selected = next((item for item in items if item.get('id') == record['video_id']), None)
        actual_channel = (selected or {}).get('snippet', {}).get('channelId', '')
        if selected is None or actual_channel != record['channel_id']:
            save(status='channel_mismatch', actual_channel_id=str(actual_channel)[:100], visibility='private',
                 message='The uploaded resource did not match the reviewed channel. FORGE kept its upload Private and stopped; review it in YouTube Studio.')
            raise PublishingError('The uploaded resource does not match the reviewed channel. No public release or subtitle upload was requested. Check YouTube Studio.', code='wrong_video_channel')
        save(actual_channel_id=actual_channel)
        verify_file(snapshot['file'])
        if subtitle and not record.get('caption_id'):
            if CAPTIONS_SCOPE not in connection.get('scopes', []):
                raise PublishingError('The video was accepted, but subtitle permission is missing. Reconnect with subtitle permission or use YouTube Studio.', code='subtitle_permission_missing')
            token, _ = service.access_token(profile)
            boundary = 'forge-' + uuid.uuid4().hex
            snippet = json.dumps({'snippet': {'videoId': record['video_id'], 'language': metadata['language'], 'name': 'Russian', 'isDraft': False}}, ensure_ascii=False).encode()
            content = subtitle.read_bytes()
            body = (f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode() + snippet
                + f'\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + content
                + f'\r\n--{boundary}--\r\n'.encode())
            caption = check_response(request('POST', CAPTIONS + '?' + urlencode({'uploadType': 'multipart', 'part': 'snippet'}),
                headers={'Content-Type': f'multipart/related; boundary="{boundary}"'}, data=body), (200, 201)).json()
            caption_id = str(caption.get('id', ''))
            if not caption_id or len(caption_id) > 200:
                raise PublishingError('YouTube did not return a subtitle identity. The uploaded video is retained.', code='caption_identity_missing')
            save(caption_id=caption_id, caption_status='uploaded')
        confirmed_schedule = ''
        if metadata['visibility'] != 'private' or metadata['publish_at']:
            if CAPTIONS_SCOPE not in connection.get('scopes', []):
                raise PublishingError('The uploaded video remains Private. Reconnect with YouTube management permission or finish in Studio.', code='publishing_permission_missing')
            service.verify_channel(profile)
            status_body = {'privacyStatus': metadata['visibility'], 'selfDeclaredMadeForKids': metadata['made_for_kids'],
                           'containsSyntheticMedia': metadata['contains_synthetic_media']}
            if metadata['publish_at']:
                if datetime.fromisoformat(metadata['publish_at'].replace('Z', '+00:00')).timestamp() <= time.time():
                    raise PublishingError('The reviewed schedule elapsed during upload. The video remains Private; choose a new schedule in YouTube Studio.', code='schedule_elapsed')
                status_body['publishAt'] = metadata['publish_at']
            changed = check_response(request('PUT', VIDEO_RESOURCE + '?part=status', headers={'Content-Type': 'application/json; charset=utf-8'},
                data=json.dumps({'id': record['video_id'], 'status': status_body}).encode())).json()
            if changed.get('id') != record['video_id']:
                raise PublishingError('YouTube did not confirm the requested video status. Review the accepted upload in Studio.', code='video_status_unconfirmed')
            actual = changed.get('status') or {}
            actual_visibility = actual.get('privacyStatus', 'private')
            if actual_visibility not in {'private', 'unlisted', 'public'}:
                actual_visibility = 'private'
            confirmed_schedule = str(actual.get('publishAt') or '')
            save(visibility=actual_visibility)
        status = 'scheduled' if metadata['publish_at'] and confirmed_schedule == metadata['publish_at'] else 'uploaded'
        message = ('YouTube accepted the requested schedule; check its processing and publication status in Studio.' if metadata['publish_at'] else
                   'YouTube accepted the video. Processing and actual availability are controlled by YouTube.')
        if record['visibility'] != metadata['visibility']:
            message += ' YouTube returned a different visibility; API project verification or an audit may be required.'
        if metadata['publish_at'] and status != 'scheduled':
            message = 'The video was accepted, but YouTube did not confirm the requested schedule. Review it in YouTube Studio.'
        save(status=status, message=message, publish_at=confirmed_schedule if status == 'scheduled' else '')
        service.vault.remove(session_key)
        return service.public_record(record)
    except PublishingError as error:
        save(status='channel_mismatch' if error.code == 'wrong_video_channel' else
                    'video_uploaded_subtitles_pending' if record.get('video_id') and subtitle and not record.get('caption_id') else
                    'video_uploaded_action_pending' if record.get('video_id') else 'interrupted' if error.retryable else 'failed',
             message=str(error), caption_status='failed' if subtitle and not record.get('caption_id') else record.get('caption_status', 'not_requested'))
        raise


def execute_upload(service, record_id, *, progress=None, cancel=None, chunk_size=4 * 1024 * 1024, job_id=None):
    """One OS-owned transfer lease survives safe retries and expires on process death."""
    with owned_lock(service.data, 'upload-' + str(record_id), progress=progress, cancel=cancel):
        return _execute_upload(service, record_id, progress=progress, cancel=cancel, chunk_size=chunk_size, job_id=job_id)


def worker_upload(payload, progress=None, stage_dir=None):
    service = PublishingService(data=Path(payload['data']))
    try:
        record = execute_upload(service, str(payload.get('options', {}).get('record_id', '')), progress=progress, job_id=payload.get('job_id'))
        folder = Path(stage_dir if stage_dir is not None else payload['destination']).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        report = write_new_json(folder, 'youtube-upload-' + record['id'] + '.json', record)
        return [report], [], {'publishing_record': record}
    finally:
        service.close()


def mount(app, store, manager, destination):
    from .queue import public_job
    service = PublishingService(store)
    app.state.language_publishing = service

    @app.get('/api/language-publishing/state')
    def state():
        return service.state()

    @app.get('/api/language-publishing/profiles')
    def profiles():
        return {'profiles': service.profiles()}

    @app.post('/api/language-publishing/profiles')
    def save_profile(body: dict):
        return {'profile': service.save_profile(body.get('profile', body), body.get('expected_revision'))}

    @app.post('/api/language-publishing/oauth/client')
    def client(body: dict):
        return {'oauth': service.save_client(body)}

    @app.post('/api/language-publishing/oauth/start')
    def start(body: dict):
        return service.start_oauth(body)

    @app.get('/api/language-publishing/oauth/status/{flow_id}')
    def flow_status(flow_id: str):
        return service.oauth_status(flow_id)

    @app.post('/api/language-publishing/oauth/cancel/{flow_id}')
    def cancel_flow(flow_id: str):
        return service.cancel_oauth(flow_id)

    @app.post('/api/language-publishing/profiles/{profile_id}/channel')
    def channel(profile_id: str, body: dict):
        return {'profile': service.confirm_channel(profile_id, body)}

    @app.post('/api/language-publishing/profiles/{profile_id}/disconnect')
    def disconnect(profile_id: str):
        return {'profile': service.disconnect(profile_id)}

    @app.get('/api/language-publishing/profiles/{profile_id}/avatar')
    def avatar(profile_id: str):
        try:
            return WebResponse(service.avatar(profile_id), media_type='image/png')
        except PublishingError:
            raise HTTPException(404, 'Channel avatar is unavailable.') from None

    @app.post('/api/language-publishing/prepare')
    def prepare(body: dict):
        return service.prepare(body)

    @app.post('/api/language-publishing/upload')
    def upload(body: dict):
        record, job = service.enqueue(body, manager)
        return {'record': service.public_record(record), 'job': public_job(job), 'jobs': [public_job(job)]}

    @app.get('/api/language-publishing/history')
    def history(profile_id: str | None = None):
        return {'history': service.history(profile_id)}

    @app.post('/api/language-publishing/manual')
    def manual(body: dict):
        return service.manual(body)

    @app.post('/api/language-publishing/manual/open')
    def open_studio(body: dict):
        if body.get('approve') is not True:
            raise PublishingError('Choose Open YouTube Studio explicitly.')
        import webbrowser
        opened = webbrowser.open(STUDIO)
        return {'opened': bool(opened), 'studio_url': STUDIO}

    return service
