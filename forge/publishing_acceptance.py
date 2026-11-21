"""Owner-driven private Google acceptance; local fixtures never become live proof.

Preparation creates owned media and the ordinary publishing review. Upload uses
the existing explicit-consent endpoint. Verification performs read-only Google
readback and exports pseudonymized observations, never credentials or media.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlencode
import uuid

from fastapi.responses import FileResponse

from . import __version__
from .core import ROOT, executable, json_write
from .language_publishing import digest_file, fingerprint, verify_file
from .publishing_oauth import (CAPTIONS_SCOPE, VIDEO_RESOURCE, DPAPIProtector,
                               GoogleHTTP, PublishingError, check_response)

CAPTION_RESOURCE = 'https://www.googleapis.com/youtube/v3/captions'
SESSION = re.compile(r'^[a-f0-9]{32}$')
KV_KEY = 'v10_youtube_private_acceptance'


def source_identity():
    # Shared with native/local model acceptance; an unbound installation is not
    # repaired by asking the owner to type an unverifiable source commit.
    try:
        from scripts.dubbing_cpu_acceptance import source_identity as inspect
        additional = ('forge/language_publishing.py', 'forge/publishing_oauth.py', 'forge/publishing_acceptance.py',
                      'web/dubbing_v10.js', 'web/publishing_acceptance_v10.js')
        value = inspect(additional_files=additional)
        manifest = json.loads((ROOT / 'release-manifest.json').read_text('utf-8'))
        files = value.get('source_files_sha256', {})
        value['verified'] = (set(additional) <= set(files) and value.get('source_checkout_clean') is not False
                             and all(manifest.get('files', {}).get(name) == digest
                                     for name, digest in files.items()))
        value.setdefault('installed_manifest_sha256', value['source_manifest_sha256'])
        if value.get('source_kind') == 'installed_manifest':
            value['source_manifest_sha256'] = manifest.get('package', {}).get('source_manifest_sha256')
        return value
    except (ImportError, OSError, ValueError, RuntimeError, TypeError, subprocess.SubprocessError):
        return {'source_commit': None, 'source_manifest_sha256': None,
                'verified': False, 'reason': 'Install the source-bound acceptance candidate before exporting release evidence.'}


def _identity_ready(identity):
    return bool(re.fullmatch(r'[a-f0-9]{40}', str(identity.get('source_commit') or ''))
                and re.fullmatch(r'[a-f0-9]{64}', str(identity.get('source_manifest_sha256') or ''))
                and identity.get('verified') is True)


def _sha(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


class PublishingAcceptance:
    def __init__(self, service):
        self.service = service
        self.root = service.data / 'publishing' / 'acceptance'

    def _folder(self, identity, *, create=False):
        if not isinstance(identity, str) or not SESSION.fullmatch(identity):
            raise PublishingError('Choose a saved acceptance test.', code='acceptance_missing')
        if self.root.parent.is_symlink() or self.root.is_symlink():
            raise PublishingError('The acceptance folder cannot link outside FORGE data.')
        folder = self.root / identity
        if folder.is_symlink():
            raise PublishingError('The acceptance test folder cannot be a symbolic link.')
        if create:
            folder.mkdir(parents=True, exist_ok=False)
        return folder

    def _session(self, identity):
        path = self._folder(identity) / 'session.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 100_000:
            raise PublishingError('The acceptance test is unavailable.', code='acceptance_missing')
        value = json.loads(path.read_text('utf-8'))
        if value.get('id') != identity:
            raise PublishingError('The acceptance test identity is invalid.')
        return value

    def state(self):
        oauth = self.service.oauth_state()
        profiles = self.service.profiles()
        identity = source_identity()
        sessions = []
        for path in sorted(self.root.glob('*/session.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
            if SESSION.fullmatch(path.parent.name) and not path.is_symlink():
                value = self._session(path.parent.name)
                sessions.append({'id': value['id'], 'created': value['created'], 'profile_id': value['profile_id']})
        return {'profiles': profiles, 'sessions': sessions, 'source_identity': identity,
                'requirements': [
                    {'id': 'windows_secure_storage', 'ready': oauth['secure_storage'],
                     'action': 'Run this candidate on Windows under the account that owns its DPAPI credentials.'},
                    {'id': 'desktop_oauth_client', 'ready': oauth['configured'],
                     'action': 'Connect YouTube using your downloaded Google Desktop OAuth client JSON; never send it in chat.'},
                    {'id': 'confirmed_channel', 'ready': any(p['connected'] for p in profiles),
                     'action': 'Authorize Google and confirm the exact intended channel with subtitle permission.'},
                    {'id': 'source_binding', 'ready': _identity_ready(identity),
                     'action': 'Use the verified acceptance candidate; an arbitrary source hash cannot be typed in.'}],
                'can_prepare': oauth['secure_storage'] and oauth['configured'] and any(p['connected'] for p in profiles)
                               and _identity_ready(identity),
                'note': 'Creates a short generated sample. Nothing uploads until you review the exact channel and approve the Private test.'}

    def prepare(self, body):
        if set(body) != {'profile_id'}:
            raise PublishingError('Choose only the intended channel profile for this Private test.')
        identity = source_identity()
        if not _identity_ready(identity):
            raise PublishingError('The installed source is not verified. Install the source-bound acceptance candidate first.', code='source_unverified')
        profile = self.service.profile(str(body['profile_id']))
        if profile.get('language') != 'ru':
            raise PublishingError('Choose the Russian-language profile for this acceptance test.')
        _, connection = self.service.verify_channel(profile)
        if CAPTIONS_SCOPE not in connection.get('scopes', []):
            raise PublishingError('Reconnect with subtitle permission before the Private acceptance test.', code='subtitle_permission_missing')
        binary = executable('ffmpeg', self.service.store.settings() if self.service.store else {})
        if not binary:
            raise PublishingError('Set up FFmpeg in FORGE Settings before generating the test sample.')
        test_id = uuid.uuid4().hex
        folder = self._folder(test_id, create=True)
        video, subtitles = folder / 'generated-private-test.mp4', folder / 'generated-private-test.srt'
        subprocess.run([str(binary), '-nostdin', '-v', 'error', '-n', '-f', 'lavfi', '-i', 'color=c=0x1b292e:s=320x180:r=15',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000', '-t', '2',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest',
                        '-metadata', 'comment=Generated FORGE Private acceptance ' + test_id, str(video)],
                       check=True, capture_output=True, timeout=45)
        subtitles.write_text('1\n00:00:00,000 --> 00:00:02,000\nРусские субтитры: проверка FORGE.\n', encoding='utf-8')
        prepared = self.service.prepare({'profile_id': profile['id'], 'path': str(video), 'subtitles_path': str(subtitles),
            'project_id': 'acceptance-' + test_id, 'approved_metadata': True,
            'metadata': {'title': 'FORGE private acceptance ' + test_id[:8],
                         'description': 'Generated two-second FORGE verification sample. No private inputs.',
                         'language': profile['language'], 'visibility': 'private', 'publish_at': '',
                         'contains_synthetic_media': True, 'made_for_kids': False, 'tags': ['FORGE verification']}})
        session = {'id': test_id, 'created': time.time(), 'profile_id': profile['id'],
                   'review_digest': _sha(prepared['review_token']), 'file': fingerprint(video),
                   'subtitles': fingerprint(subtitles), 'source_identity': identity}
        json_write(folder / 'session.json', session)
        return {**prepared, 'acceptance_id': test_id, 'preview_url': '/api/language-publishing/acceptance/' + test_id + '/media'}

    def verify(self, identity):
        session = self._session(identity)
        current_identity = source_identity()
        if not _identity_ready(current_identity) or current_identity != session['source_identity']:
            raise PublishingError('The candidate source changed after preparing the test. Prepare a new review.', code='source_changed')
        approval = self.service.repo.get('publishing_approvals', session['review_digest'])
        if not approval.get('consumed'):
            return {'status': 'awaiting_private_review', 'acceptance_id': identity, 'export_ready': False,
                    'message': 'This saved test has no approved upload. Return to preparation for a new review; nothing was uploaded.'}
        record = self.service.repo.get('publishing_records', approval.get('record_id', ''))
        snapshot = record.get('snapshot') or {}
        if (snapshot.get('metadata', {}).get('visibility') != 'private' or snapshot.get('metadata', {}).get('publish_at')
                or snapshot.get('file', {}).get('sha256') != session['file']['sha256']
                or snapshot.get('subtitles', {}).get('sha256') != session['subtitles']['sha256']
                or record.get('project_id') != 'acceptance-' + identity or not record.get('approved_at')):
            raise PublishingError('The accepted upload differs from this generated Private review.', code='acceptance_mismatch')
        verify_file(session['file']); verify_file(session['subtitles'])
        if record.get('status') != 'uploaded' or not record.get('video_id') or not record.get('caption_id'):
            action_required = record.get('status') in {'failed', 'interrupted', 'cancelled', 'blocked', 'channel_mismatch',
                                                      'video_uploaded_subtitles_pending', 'video_uploaded_action_pending'}
            return {'status': 'action_required' if action_required else 'upload_pending', 'acceptance_id': identity, 'export_ready': False,
                    'message': ('Open the existing Processing Queue or Upload history to resolve or retry this saved transfer; do not create another upload.'
                                if action_required else 'Wait for the existing upload and subtitles to finish; do not create another upload.'),
                    'upload_status': record.get('status')}
        profile = self.service.profile(session['profile_id'])
        channel, _ = self.service.verify_channel(profile)
        if channel['id'] != snapshot['channel_id'] or record.get('actual_channel_id') != channel['id']:
            raise PublishingError('The current channel differs from the reviewed test channel.', code='wrong_channel')
        token, _ = self.service.access_token(profile)
        headers = {'Authorization': 'Bearer ' + token}
        response = check_response(self.service.http.request('GET', VIDEO_RESOURCE + '?' + urlencode({
            'part': 'id,snippet,status', 'id': record['video_id']}), headers=headers, timeout=20)).json()
        video = next((v for v in response.get('items', []) if v.get('id') == record['video_id']), {})
        if video.get('snippet', {}).get('channelId') != channel['id']:
            raise PublishingError('Google did not confirm the reviewed video/channel binding.', code='wrong_video_channel')
        status = video.get('status', {})
        if status.get('privacyStatus') != 'private' or status.get('publishAt'):
            raise PublishingError('Google did not confirm that the test remains Private and unscheduled.', code='private_test_visibility')
        response = check_response(self.service.http.request('GET', CAPTION_RESOURCE + '?' + urlencode({
            'part': 'id,snippet', 'videoId': record['video_id']}), headers=headers, timeout=20)).json()
        caption = next((v for v in response.get('items', []) if v.get('id') == record['caption_id']), {})
        snippet = caption.get('snippet', {})
        if (snippet.get('videoId') != record['video_id'] or snippet.get('language') != snapshot['metadata']['language']
                or snippet.get('isDraft') is not False or snippet.get('status') != 'serving'):
            failed = snippet.get('status') == 'failed'
            return {'status': 'action_required' if failed else 'caption_processing', 'acceptance_id': identity, 'export_ready': False,
                    'message': ('Google reported subtitle processing failure. Keep the same Private video and resolve its subtitles in YouTube Studio.'
                                if failed else 'The same video is retained. Wait for Google to serve the uploaded subtitles, then Check again.')}
        native = os.name == 'nt'
        official = type(self.service.http) is GoogleHTTP and type(self.service.vault.protector) is DPAPIProtector
        live = native and official and self.service.vault.available
        receipt = {'schema': 1, 'app': 'FORGE', 'version': __version__, 'status': 'passed' if live else 'protocol_fixture_verified',
            'kind': 'youtube_private_acceptance', 'checked_at': time.time(),
            'source_commit': current_identity['source_commit'], 'source_manifest_sha256': current_identity['source_manifest_sha256'],
            'source_identity': current_identity, 'live_google': live, 'native_windows_execution': native,
            'language': 'ru',
            'explicit_private_review': True, 'channel_verified': True, 'private_visibility_verified': True, 'captions_verified': True,
            'media_sha256': session['file']['sha256'], 'subtitle_sha256': session['subtitles']['sha256'],
            'approved_channel_sha256': _sha(snapshot['channel_id']), 'observed_channel_sha256': _sha(video['snippet']['channelId']),
            'channel_id_sha256': _sha(channel['id']), 'video_id_sha256': _sha(record['video_id']),
            'private_inputs_not_embedded': True, 'credentials_not_embedded': True,
            'note': 'Private generated test only. No public visibility, scheduling, human voice-quality or interrupted-transfer acceptance is claimed.'}
        json_write(self._folder(identity) / 'receipt.json', receipt)
        if live and self.service.store:
            self.service.store.set(KV_KEY, receipt)
        return {'status': receipt['status'], 'acceptance_id': identity, 'export_ready': True,
                'message': 'Google readback verified the Private test and served subtitles.' if live else
                           'Controlled protocol fixture verified; this is not live Google acceptance.', 'receipt': receipt}

    def export(self, identity, body):
        if body != {'approve': True}:
            raise PublishingError('Approve exporting this sanitized acceptance receipt.')
        result = self.verify(identity)
        if not result.get('export_ready'):
            raise PublishingError('Finish the existing private test before exporting its receipt.')
        return {'filename': 'FORGE-V0.10-YOUTUBE-ACCEPTANCE.json', 'receipt': result['receipt']}


def mount(app, service):
    acceptance = PublishingAcceptance(service)
    app.state.publishing_acceptance = acceptance

    @app.get('/api/language-publishing/acceptance/state')
    def state():
        return acceptance.state()

    @app.post('/api/language-publishing/acceptance/prepare')
    def prepare(body: dict):
        return acceptance.prepare(body)

    @app.post('/api/language-publishing/acceptance/{identity}/verify')
    def verify(identity: str):
        return acceptance.verify(identity)

    @app.post('/api/language-publishing/acceptance/{identity}/export')
    def export(identity: str, body: dict):
        return acceptance.export(identity, body)

    @app.get('/api/language-publishing/acceptance/{identity}/media')
    def media(identity: str):
        session = acceptance._session(identity)
        return FileResponse(verify_file(session['file']), media_type='video/mp4')
