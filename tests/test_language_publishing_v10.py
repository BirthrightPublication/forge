"""Publishing contracts over an owned HTTP fixture, never a live Google upload."""
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
import pytest

from forge.app import create_app
from forge.core import Store
from forge.language_publishing import PublishingService, Repository, checked_metadata, execute_upload, mount
from forge.publishing_oauth import (CAPTIONS_SCOPE, CredentialVault, DPAPIProtector, GoogleHTTP,
                                  PublishingError, Response, allowed_url, check_response, upload_url)

RUSSIAN = 'UC' + 'R' * 22
ENGLISH = 'UC' + 'E' * 22
VIDEO_ID = 'RuVideo0001'
ACCESS = 'synthetic-access-token-not-real'
REFRESH = 'synthetic-refresh-token-not-real'
CLIENT = {'installed': {'client_id': 'synthetic-client.apps.googleusercontent.com',
                        'client_secret': 'synthetic-desktop-secret-not-real'}}


class TestProtector:
    __test__ = False
    available = True

    def __init__(self):
        self.cipher = AESGCM(AESGCM.generate_key(bit_length=256))

    def protect(self, data):
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, data, b'explicit-owned-test-fixture')

    def unprotect(self, data):
        return self.cipher.decrypt(data[:12], data[12:], b'explicit-owned-test-fixture')


class FixtureGoogle:
    """Actual local HTTP ranges/metadata; no Google requests leave the test."""
    def __init__(self):
        self.channels = [RUSSIAN]
        self.sessions = {}
        self.insert_calls = 0
        self.caption_calls = 0
        self.token_calls = []
        self.token_gate = None
        self.token_entered = threading.Event()
        self.chunk_ranges = []
        self.requests = []
        self.location_override = None
        self.fail_chunks = 0
        self.fail_caption = False
        self.response_visibility = None
        self.video_channel_override = None
        self.status_updates = []
        self.stop_after_ack = None
        self.lock = threading.RLock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _body(self):
                return self.rfile.read(int(self.headers.get('Content-Length') or 0))

            def _send(self, status, body=None, **headers):
                raw = json.dumps(body or {}).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = self._body()
                with outer.lock:
                    outer.requests.append(('POST', self.path, dict(self.headers)))
                    if self.path == '/token':
                        values = parse_qs(body.decode())
                        outer.token_calls.append(values)
                        outer.token_entered.set()
                        if outer.token_gate is not None:
                            assert outer.token_gate.wait(5)
                        return self._send(200, {'access_token': ACCESS, 'refresh_token': REFRESH,
                            'expires_in': 3600, 'token_type': 'Bearer'})
                    if self.path.startswith('/upload/youtube/v3/videos'):
                        assert self.headers.get('Authorization') == 'Bearer ' + ACCESS
                        metadata = json.loads(body)
                        outer.insert_calls += 1
                        key = str(outer.insert_calls)
                        outer.sessions[key] = {'bytes': bytearray(), 'total': int(self.headers['X-Upload-Content-Length']),
                                               'metadata': metadata, 'complete': False}
                        location = outer.location_override or 'https://www.googleapis.com/upload/youtube/v3/videos?upload_id=' + key
                        return self._send(200, {}, Location=location)
                    if self.path.startswith('/upload/youtube/v3/captions'):
                        assert self.headers.get('Authorization') == 'Bearer ' + ACCESS
                        outer.caption_calls += 1
                        if outer.fail_caption:
                            return self._send(403, {'error': {'errors': [{'reason': 'insufficientPermissions'}]}})
                        assert 'Русские'.encode() in body
                        return self._send(200, {'id': 'synthetic-caption-id'})
                return self._send(404)

            def do_GET(self):
                with outer.lock:
                    outer.requests.append(('GET', self.path, dict(self.headers)))
                    if self.path.startswith('/youtube/v3/channels'):
                        assert self.headers.get('Authorization') == 'Bearer ' + ACCESS
                        return self._send(200, {'items': [{'id': key, 'snippet': {'title': 'Russian fixture' if key == RUSSIAN else 'English fixture',
                            'customUrl': '@fixture', 'thumbnails': {'default': {'url': 'https://yt3.ggpht.com/synthetic-avatar'}}}} for key in outer.channels]})
                    if self.path.startswith('/youtube/v3/videos'):
                        return self._send(200, {'items': [{'id': VIDEO_ID, 'snippet': {'channelId': outer.video_channel_override or RUSSIAN},
                            'status': {'privacyStatus': outer.response_visibility or 'private'}}]})
                return self._send(404)

            def do_PUT(self):
                body = self._body()
                values = parse_qs(urlparse(self.path).query)
                with outer.lock:
                    outer.requests.append(('PUT', self.path, dict(self.headers)))
                    assert self.headers.get('Authorization') == 'Bearer ' + ACCESS
                    if self.path.startswith('/youtube/v3/videos'):
                        update = json.loads(body)
                        outer.status_updates.append(update)
                        status = copy.deepcopy(update['status'])
                        if outer.response_visibility:
                            status['privacyStatus'] = outer.response_visibility
                        return self._send(200, {'id': update['id'], 'status': status})
                    session = outer.sessions[values['upload_id'][0]]
                    content_range = self.headers.get('Content-Range')
                    if content_range.startswith('bytes */'):
                        if session['complete']:
                            return self._send(200, {'id': VIDEO_ID, 'status': {'privacyStatus': outer.response_visibility or session['metadata']['status']['privacyStatus']}})
                        headers = {'Range': 'bytes=0-' + str(len(session['bytes']) - 1)} if session['bytes'] else {}
                        return self._send(308, {}, **headers)
                    if outer.fail_chunks:
                        outer.fail_chunks -= 1
                        return self._send(503, {'error': {'errors': [{'reason': 'backendError'}]}})
                    match = re.fullmatch(r'bytes ([0-9]+)-([0-9]+)/([0-9]+)', content_range)
                    assert match
                    start, end, total = map(int, match.groups())
                    assert start == len(session['bytes']) and end - start + 1 == len(body) and total == session['total']
                    outer.chunk_ranges.append((start, end, total))
                    session['bytes'].extend(body)
                    if len(session['bytes']) == total:
                        session['complete'] = True
                        return self._send(200, {'id': VIDEO_ID, 'status': {'privacyStatus': outer.response_visibility or session['metadata']['status']['privacyStatus']}})
                    return self._send(308, {}, Range='bytes=0-' + str(end))

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class LocalHTTP(GoogleHTTP):
    """Explicit test-only transport maps allowlisted endpoints to owned localhost."""
    def __init__(self, fixture):
        self.fixture = fixture
        self.opener = build_opener(ProxyHandler({}))

    def request(self, method, url, *, headers=None, data=None, timeout=45):
        assert allowed_url(url), 'Credentials must never reach an unapproved destination'
        value = urlparse(url)
        local = f'http://127.0.0.1:{self.fixture.server.server_port}' + value.path + ('?' + value.query if value.query else '')
        try:
            result = self.opener.open(Request(local, data=data, headers=headers or {}, method=method), timeout=timeout)
        except HTTPError as error:
            return Response(error.code, {k.lower(): v for k, v in error.headers.items()}, error.read())
        with result:
            return Response(result.status, {k.lower(): v for k, v in result.headers.items()}, result.read())


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / 'data', 'synthetic-publishing-token', 19931, workers=False)
    if not hasattr(app.state, 'language_publishing'):
        mount(app, app.state.store, app.state.manager, lambda *args: tmp_path / 'exports')
    service = app.state.language_publishing
    fixture = FixtureGoogle()
    service.http = LocalHTTP(fixture)
    service.vault = CredentialVault(app.state.store.data, TestProtector())
    with TestClient(app, base_url='http://127.0.0.1:19931', headers={'X-Forge-Token': 'synthetic-publishing-token'}) as client:
        yield client, app, service, fixture
    service.close()
    fixture.close()
    app.state.store.close()


def post(client, path, body):
    result = client.post('/api/language-publishing/' + path, json=body)
    assert result.status_code == 200, result.text
    return result.json()


def connected(api, *, captions=True, profile_name='Russian channel'):
    client, app, service, fixture = api
    profile = post(client, 'profiles', {'profile': {'name': profile_name}})['profile']
    post(client, 'oauth/client', CLIENT['installed'])
    started = post(client, 'oauth/start', {'profile_id': profile['id'], 'captions': captions})
    flow = service.flows[started['flow_id']]['flow']
    auth = parse_qs(urlparse(started['authorization_url']).query)
    assert auth['code_challenge_method'] == ['S256'] and auth['prompt'] == ['consent select_account']
    assert auth['redirect_uri'] == [flow.redirect_uri]
    callback = flow.redirect_uri + '?' + urlencode({'code': 'synthetic-code-not-real', 'state': flow.state})
    with build_opener(ProxyHandler({})).open(callback, timeout=5) as response:
        assert response.status == 200
    status = client.get('/api/language-publishing/oauth/status/' + flow.id).json()
    assert status['status'] == 'select_channel'
    assert status['channels'][0]['id'] == RUSSIAN
    profile = post(client, 'profiles/' + profile['id'] + '/channel', {'flow_id': flow.id,
                   'channel_id': RUSSIAN, 'confirm': True, 'expected_revision': profile['revision']})['profile']
    assert profile['connected'] and profile['channel_id'] == RUSSIAN
    return profile


def metadata(**extra):
    return {'title': 'Русские комментарии', 'description': 'Проверенный русский текст.', 'tags': ['Русский'],
            'visibility': 'private', 'language': 'ru', 'contains_synthetic_media': True, 'made_for_kids': False, **extra}


def make_video(path):
    binary = shutil.which('ffmpeg')
    assert binary, 'The native media prerequisites are required for this video validation suite.'
    subprocess.run([binary, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=128x72:r=10',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000', '-t', '1',
                    '-c:v', 'mpeg4', '-c:a', 'aac', '-y', str(path)], check=True, capture_output=True, timeout=30)
    # A legal MP4 free box gives the protocol fixture several actual byte ranges
    # without pretending generated test video is a neural/model acceptance case.
    with path.open('ab') as handle:
        handle.write(struct.pack('>I4s', 1024 * 1024 + 8, b'free'))
        label = b'Explicit owned HTTP fixture.'
        handle.write((label * (1024 * 1024 // len(label) + 1))[:1024 * 1024])
    return path


def review(api, profile, tmp_path, **extra):
    client, app, service, fixture = api
    path = tmp_path / 'fixture-export.mp4'
    make_video(path)
    body = {'profile_id': profile['id'], 'path': str(path), 'approved_metadata': True, 'metadata': metadata(), **extra}
    return post(client, 'prepare', body), path, body


def test_profile_is_additive_revisioned_and_has_no_credential_fields(api):
    client, app, service, _ = api
    app.state.store.set('presets', [{'name': 'existing preserved preset'}])
    profile = post(client, 'profiles', {'profile': {'name': 'Russian channel'}})['profile']
    assert profile['default_visibility'] == 'private' and profile['language'] == 'ru'
    assert profile['schedule_timezone'] == 'Europe/Moscow'
    changed = post(client, 'profiles', {'profile': {**profile, 'name': 'Renamed'}})['profile']
    assert changed['revision'] == 2
    assert client.post('/api/language-publishing/profiles', json={'profile': profile}).status_code == 400
    assert client.post('/api/language-publishing/profiles', json={'profile': {'name': 'bad', 'refresh_token': 'must-not-save'}}).status_code == 400
    assert client.post('/api/language-publishing/profiles', json={'profile': {'name': 'bad', 'channel_id': ENGLISH}}).status_code == 400
    assert app.state.store.get('presets') == [{'name': 'existing preserved preset'}]
    assert not app.state.store.jobs()


@pytest.mark.parametrize('extra', [{'tags': [{'refresh_token': REFRESH}]},
    {'subtitle_preferences': {'client_secret': CLIENT['installed']['client_secret']}}, {'name': {'token': ACCESS}}])
def test_profile_nested_options_cannot_smuggle_connection_credentials(api, extra):
    client, app, service, fixture = api
    result = client.post('/api/language-publishing/profiles', json={'profile': {'name': 'Russian', **extra}})
    assert result.status_code == 400
    assert not service.repo.list('publishing_profiles')


def test_oauth_loopback_pkce_state_confirmation_and_encrypted_storage(api):
    client, app, service, fixture = api
    profile = connected(api)
    public = json.dumps(client.get('/api/language-publishing/state').json())
    assert all(secret not in public for secret in (ACCESS, REFRESH, CLIENT['installed']['client_secret']))
    assert profile['avatar'].startswith('/api/language-publishing/profiles/')
    for path in service.vault.root.iterdir():
        assert all(secret.encode() not in path.read_bytes() for secret in (ACCESS, REFRESH, CLIENT['installed']['client_secret']))
    with service.repo.lock:
        dumps = '\n'.join(str(row[0]) for row in service.repo.db.execute('SELECT data FROM publishing_connections'))
    assert ACCESS not in dumps and REFRESH not in dumps and CLIENT['installed']['client_secret'] not in dumps
    assert fixture.token_calls[0]['code_verifier'][0]
    assert not app.state.store.jobs()
    failed = client.post('/api/language-publishing/oauth/start', json={'profile_id': profile['id']})
    assert failed.status_code == 400 and 'confirm' in failed.text.lower()


def test_wrong_oauth_state_does_not_consume_legitimate_flow_and_channel_cannot_be_forged(api):
    client, app, service, fixture = api
    profile = post(client, 'profiles', {'profile': {'name': 'Russian'}})['profile']
    post(client, 'oauth/client', CLIENT['installed'])
    started = post(client, 'oauth/start', {'profile_id': profile['id']})
    flow = service.flows[started['flow_id']]['flow']
    with pytest.raises(HTTPError) as error:
        build_opener(ProxyHandler({})).open(flow.redirect_uri + '?code=owned&state=wrong', timeout=5)
    assert error.value.code == 400 and not fixture.token_calls and not flow.used
    with build_opener(ProxyHandler({})).open(flow.redirect_uri + '?' + urlencode({'code': 'owned', 'state': flow.state}), timeout=5) as response:
        assert response.status == 200
    forged = client.post('/api/language-publishing/profiles/' + profile['id'] + '/channel', json={'flow_id': flow.id, 'channel_id': ENGLISH, 'confirm': True})
    assert forged.status_code == 400
    assert not service.profile(profile['id'])['channel_id']
    assert not service.profiles()[0]['connected']


def test_pending_oauth_connection_can_be_cancelled_without_waiting_for_expiry(api):
    client, app, service, fixture = api
    profile = post(client, 'profiles', {'profile': {'name': 'Russian'}})['profile']
    post(client, 'oauth/client', CLIENT['installed'])
    flow_id = post(client, 'oauth/start', {'profile_id': profile['id']})['flow_id']
    flow = service.flows[flow_id]['flow']
    assert flow.thread.is_alive()
    assert post(client, 'oauth/cancel/' + flow_id, {})['cancelled'] is True
    assert not flow.thread.is_alive() and flow_id not in service.flows
    assert not fixture.token_calls


def test_cancel_during_token_exchange_cannot_store_a_late_connection(api):
    client, app, service, fixture = api
    profile = post(client, 'profiles', {'profile': {'name': 'Russian'}})['profile']
    post(client, 'oauth/client', CLIENT['installed'])
    flow_id = post(client, 'oauth/start', {'profile_id': profile['id']})['flow_id']
    flow = service.flows[flow_id]['flow']
    fixture.token_gate = threading.Event()
    def callback():
        try:
            with build_opener(ProxyHandler({})).open(flow.redirect_uri + '?' + urlencode({'code': 'owned', 'state': flow.state}), timeout=8) as response:
                return response.status
        except HTTPError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(callback)
        try:
            assert fixture.token_entered.wait(5)
            assert post(client, 'oauth/cancel/' + flow_id, {})['cancelled']
        finally:
            fixture.token_gate.set()
        assert future.result(timeout=5) == 400
    assert not service.repo.list('publishing_connections')
    assert not list(service.vault.root.glob('connection-*'))
    assert not any(item[0] == 'GET' and item[1].startswith('/youtube/v3/channels') for item in fixture.requests)


def test_review_binds_file_channel_metadata_and_duplicate_clicks_are_atomic(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    prepared, path, body = review(api, profile, tmp_path)
    assert prepared['review']['channel_id'] == RUSSIAN and prepared['review']['file_size'] == path.stat().st_size
    assert prepared['review']['metadata']['visibility'] == 'private'
    assert prepared['review']['metadata']['contains_synthetic_media'] is True
    assert not fixture.insert_calls and not app.state.store.jobs()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post('/api/language-publishing/upload', json={'review_token': prepared['review_token'], 'approve': True}), range(2)))
    assert all(item.status_code == 200 for item in responses), [item.text for item in responses]
    assert responses[0].json()['job']['id'] == responses[1].json()['job']['id']
    assert len(app.state.store.jobs()) == 1
    payload = app.state.store.jobs()[0]['payload']
    assert set(payload['options']) == {'record_id', 'profile_id'}
    assert all(secret not in json.dumps(payload) for secret in (ACCESS, REFRESH, CLIENT['installed']['client_secret']))
    assert not fixture.insert_calls


def test_modified_reviewed_file_and_changed_profile_are_rejected(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    prepared, path, body = review(api, profile, tmp_path)
    path.write_bytes(path.read_bytes() + b'changed after review')
    assert client.post('/api/language-publishing/upload', json={'review_token': prepared['review_token'], 'approve': True}).status_code == 400
    assert not app.state.store.jobs() and not fixture.insert_calls
    prepared = post(client, 'prepare', body)
    post(client, 'profiles', {'profile': {**profile, 'name': 'Changed after review'}})
    assert client.post('/api/language-publishing/upload', json={'review_token': prepared['review_token'], 'approve': True}).status_code == 400
    assert not app.state.store.jobs()


def test_wrong_channel_and_explicit_upload_approval_are_required(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    prepared, path, body = review(api, profile, tmp_path)
    assert client.post('/api/language-publishing/upload', json={'review_token': prepared['review_token']}).status_code == 400
    fixture.channels = [ENGLISH]
    assert client.post('/api/language-publishing/upload', json={'review_token': prepared['review_token'], 'approve': True}).status_code == 400
    assert not fixture.insert_calls and not app.state.store.jobs()


def test_ambiguous_brand_channel_context_is_not_treated_as_selectable_upload_target(api):
    client, app, service, fixture = api
    fixture.channels = [RUSSIAN, ENGLISH]
    profile = post(client, 'profiles', {'profile': {'name': 'Russian target'}})['profile']
    post(client, 'oauth/client', CLIENT['installed'])
    flow_id = post(client, 'oauth/start', {'profile_id': profile['id']})['flow_id']
    flow = service.flows[flow_id]['flow']
    with build_opener(ProxyHandler({})).open(flow.redirect_uri + '?' + urlencode({'code': 'owned', 'state': flow.state}), timeout=5):
        pass
    result = client.post('/api/language-publishing/profiles/' + profile['id'] + '/channel',
        json={'flow_id': flow_id, 'channel_id': RUSSIAN, 'confirm': True})
    assert result.status_code == 400 and 'ambiguous' in result.text
    assert not service.public_profile(service.profile(profile['id']))['connected']
    assert not fixture.insert_calls


def test_public_upload_is_private_until_actual_video_channel_is_verified(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    prepared, path, body = review(api, profile, tmp_path, metadata=metadata(visibility='public'))
    queued = post(client, 'upload', {'review_token': prepared['review_token'], 'approve': True})
    done = execute_upload(service, queued['record']['id'])
    assert fixture.sessions['1']['metadata']['status']['privacyStatus'] == 'private'
    assert fixture.status_updates[0]['status']['privacyStatus'] == 'public'
    assert done['visibility'] == 'public'
    lookup = next(index for index, value in enumerate(fixture.requests) if value[0] == 'GET' and value[1].startswith('/youtube/v3/videos'))
    publish = next(index for index, value in enumerate(fixture.requests) if value[0] == 'PUT' and value[1].startswith('/youtube/v3/videos'))
    assert lookup < publish


def test_completed_record_replay_cannot_republish_or_change_current_visibility(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    prepared, path, body = review(api, profile, tmp_path, metadata=metadata(visibility='public'))
    queued = post(client, 'upload', {'review_token': prepared['review_token'], 'approve': True})
    completed = execute_upload(service, queued['record']['id'])
    previous = len(fixture.requests)
    # Simulate the owner later setting the video Private in YouTube Studio.
    fixture.response_visibility = 'private'
    replay = execute_upload(service, queued['record']['id'])
    assert replay['video_id'] == completed['video_id'] and 'No new upload' in replay['message']
    assert len(fixture.requests) == previous and fixture.insert_calls == 1 and len(fixture.status_updates) == 1


def test_two_worker_attempts_share_one_os_owned_transfer_lease(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    prepared, path, body = review(api, profile, tmp_path)
    queued = post(client, 'upload', {'review_token': prepared['review_token'], 'approve': True})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute_upload(service, queued['record']['id'], chunk_size=256 * 1024), range(2)))
    assert {item['video_id'] for item in results} == {VIDEO_ID}
    assert fixture.insert_calls == 1 and bytes(fixture.sessions['1']['bytes']) == path.read_bytes()


def test_new_queue_retry_id_is_bound_before_live_history_reconciliation(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    prepared, path, body = review(api, profile, tmp_path)
    queued = post(client, 'upload', {'review_token': prepared['review_token'], 'approve': True})
    first = app.state.store.job(queued['job']['id'])
    app.state.store.update_job(first['id'], status='interrupted', message='Explicit owned fixture interruption')
    payload = copy.deepcopy(first['payload'])
    retry = app.state.store.add_job('youtube_upload', 'Fixture retry', payload)
    app.state.store.update_job(retry['id'], status='running')
    observations = []
    execute_upload(service, queued['record']['id'], job_id=retry['id'],
                   progress=lambda *args, **kwargs: observations.append(service.history()[0]), chunk_size=256 * 1024)
    assert observations and all(item['job_id'] == retry['id'] and item['status'] == 'uploading' for item in observations)
    assert service.history()[0]['status'] == 'uploaded'


def test_separate_database_connections_cannot_both_consume_one_publish_review(api, tmp_path):
    from types import SimpleNamespace
    client, app, service, fixture = api
    profile = connected(api)
    prepared, path, body = review(api, profile, tmp_path)
    second_store = Store(service.data)
    second = PublishingService(second_store, http=service.http, vault=service.vault)
    manager = SimpleNamespace(lock=threading.RLock(), check_accepting=lambda: None)
    request = {'review_token': prepared['review_token'], 'approve': True}
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(service.enqueue, request, app.state.manager), pool.submit(second.enqueue, request, manager)]
            results = [future.result(timeout=10) for future in futures]
        assert len({job['id'] for _, job in results}) == 1
        assert len(app.state.store.jobs()) == 1
    finally:
        second.close()
        second_store.close()


def test_separate_connection_revision_check_cannot_overwrite_newer_profile(api):
    client, app, service, fixture = api
    profile = post(client, 'profiles', {'profile': {'name': 'Russian'}})['profile']
    second = PublishingService(data=service.data, http=service.http, vault=service.vault)
    original = service.profile(profile['id'])
    def save(repo, name):
        try:
            return repo.put('publishing_profiles', {**original, 'name': name}, expected=original['revision'])
        except PublishingError as error:
            return error
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(save, service.repo, 'First'), pool.submit(save, second.repo, 'Second')]
            results = [future.result(timeout=10) for future in futures]
        assert sum(isinstance(result, PublishingError) for result in results) == 1
        assert service.profile(profile['id'])['revision'] == 2
    finally:
        second.close()


def test_returned_wrong_video_channel_stays_private_and_cannot_receive_subtitles_or_publication(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    prepared, path, body = review(api, profile, tmp_path, metadata=metadata(visibility='public'))
    queued = post(client, 'upload', {'review_token': prepared['review_token'], 'approve': True})
    fixture.video_channel_override = ENGLISH
    with pytest.raises(PublishingError, match='does not match'):
        execute_upload(service, queued['record']['id'])
    assert fixture.sessions['1']['metadata']['status']['privacyStatus'] == 'private'
    assert not fixture.status_updates and not fixture.caption_calls
    record = service.history()[0]
    assert record['status'] == 'channel_mismatch' and record['actual_channel_id'] == ENGLISH
    assert record['video_id'] == VIDEO_ID and record['visibility'] == 'private'


def test_limited_oauth_connection_can_upload_privately_but_does_not_claim_status_management(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api, captions=False)
    prepared, path, body = review(api, profile, tmp_path)
    body['metadata'] = metadata(visibility='unlisted')
    result = client.post('/api/language-publishing/prepare', json=body)
    assert result.status_code == 400 and 'management permission' in result.text
    assert not fixture.insert_calls


def test_real_http_upload_byte_progress_private_metadata_subtitles_and_restart_history(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    subtitle = tmp_path / 'ru.srt'
    subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\nРусские субтитры\n', encoding='utf-8')
    prepared, path, body = review(api, profile, tmp_path, subtitles_path=str(subtitle))
    queued = post(client, 'upload', {'review_token': prepared['review_token'], 'approve': True})
    samples = []
    record = execute_upload(service, queued['record']['id'], progress=lambda value, message, **extra: samples.append((value, extra)), chunk_size=256 * 1024)
    assert record['video_id'] == VIDEO_ID and record['status'] == 'uploaded' and record['visibility'] == 'private'
    assert record['caption_status'] == 'uploaded' and fixture.caption_calls == 1 and fixture.insert_calls == 1
    assert bytes(fixture.sessions['1']['bytes']) == path.read_bytes()
    uploaded = fixture.sessions['1']['metadata']
    assert uploaded['snippet']['title'] == metadata()['title'] and uploaded['snippet']['defaultAudioLanguage'] == 'ru'
    assert uploaded['status'] == {'privacyStatus': 'private', 'selfDeclaredMadeForKids': False, 'containsSyntheticMedia': True}
    assert samples and all(0 < value <= 99 for value, _ in samples)
    counts = [extra['downloaded_bytes'] for _, extra in samples]
    assert counts == sorted(counts) and counts[-1] == path.stat().st_size
    assert any(0 < number < path.stat().st_size for number in counts)
    second = PublishingService(data=service.data, http=service.http, vault=service.vault)
    assert second.history()[0]['video_id'] == VIDEO_ID
    assert second.history()[0]['caption_id'] == 'synthetic-caption-id'
    second.close()
    assert all(secret not in json.dumps(service.history()) for secret in (ACCESS, REFRESH, CLIENT['installed']['client_secret']))
    assert path.read_bytes() == bytes(fixture.sessions['1']['bytes'])


def test_interrupted_transfer_resumes_verified_acknowledged_range_without_duplicate_video(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    prepared, path, body = review(api, profile, tmp_path)
    queued = post(client, 'upload', {'review_token': prepared['review_token'], 'approve': True})
    cancel = threading.Event()
    def sample(value, message, **extra):
        cancel.set()
    with pytest.raises(PublishingError, match='cancelled'):
        execute_upload(service, queued['record']['id'], progress=sample, cancel=cancel.is_set, chunk_size=256 * 1024)
    acknowledged = len(fixture.sessions['1']['bytes'])
    assert 0 < acknowledged < path.stat().st_size
    assert service.history()[0]['status'] == 'interrupted'
    samples = []
    done = execute_upload(service, queued['record']['id'], progress=lambda value, message, **extra: samples.append(extra), chunk_size=256 * 1024)
    assert done['video_id'] == VIDEO_ID and fixture.insert_calls == 1
    assert any(start == acknowledged for start, _, _ in fixture.chunk_ranges)
    assert bytes(fixture.sessions['1']['bytes']) == path.read_bytes()
    assert samples[0]['downloaded_bytes'] > acknowledged


def test_crash_after_remote_completion_recovers_same_video_id(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    prepared, path, body = review(api, profile, tmp_path)
    queued = post(client, 'upload', {'review_token': prepared['review_token'], 'approve': True})
    record_id = queued['record']['id']
    record = service.repo.get('publishing_records', record_id)
    fixture.sessions['1'] = {'bytes': bytearray(path.read_bytes()), 'total': path.stat().st_size,
                             'metadata': {'status': {'privacyStatus': 'private'}}, 'complete': True}
    fixture.insert_calls = 1
    service.vault.save('upload-' + record_id, {'url': 'https://www.googleapis.com/upload/youtube/v3/videos?upload_id=1',
        'file_sha256': record['snapshot']['file']['sha256'], 'channel_id': RUSSIAN, 'offset': path.stat().st_size})
    result = execute_upload(service, record_id)
    assert result['video_id'] == VIDEO_ID and fixture.insert_calls == 1 and not fixture.chunk_ranges


def test_subtitle_failure_preserves_uploaded_video_and_retry_does_not_upload_again(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    subtitle = tmp_path / 'ru.vtt'
    subtitle.write_text('WEBVTT\n\n00:00.000 --> 00:01.000\nРусские субтитры\n')
    prepared, path, body = review(api, profile, tmp_path, subtitles_path=str(subtitle))
    queued = post(client, 'upload', {'review_token': prepared['review_token'], 'approve': True})
    fixture.fail_caption = True
    with pytest.raises(PublishingError):
        execute_upload(service, queued['record']['id'], chunk_size=256 * 1024)
    assert service.history()[0]['video_id'] == VIDEO_ID and service.history()[0]['status'] == 'video_uploaded_subtitles_pending'
    fixture.fail_caption = False
    result = execute_upload(service, queued['record']['id'])
    assert result['caption_status'] == 'uploaded' and fixture.insert_calls == 1


def test_untrusted_resumable_location_receives_no_bearer_or_video(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    prepared, path, body = review(api, profile, tmp_path)
    queued = post(client, 'upload', {'review_token': prepared['review_token'], 'approve': True})
    fixture.location_override = 'https://example.invalid/steal-token'
    with pytest.raises(PublishingError, match='unsupported upload'):
        execute_upload(service, queued['record']['id'])
    assert not fixture.chunk_ranges
    assert not service.vault.exists('upload-' + queued['record']['id'])


def test_schedule_visibility_disclosure_and_utf8_metadata_constraints(api):
    client, app, service, fixture = api
    profile = post(client, 'profiles', {'profile': {'name': 'Russian'}})['profile']
    when = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert checked_metadata(metadata(publish_at=when), profile)['publish_at'].endswith('Z')
    for values in [metadata(publish_at=when, visibility='public'), metadata(publish_at='2020-01-01T00:00:00Z'),
                   metadata(description='Я' * 3000), {'title': 'No audience review'}]:
        with pytest.raises(PublishingError):
            checked_metadata(values, profile)


def test_disconnect_invalidates_prepared_upload_and_does_not_change_other_profile(api, tmp_path):
    client, app, service, fixture = api
    profile = connected(api)
    other = connected(api, profile_name='Another Russian account')
    prepared, path, body = review(api, profile, tmp_path)
    post(client, 'profiles/' + profile['id'] + '/disconnect', {})
    assert not service.public_profile(service.profile(profile['id']))['connected']
    assert service.public_profile(service.profile(other['id']))['connected']
    assert client.post('/api/language-publishing/upload', json={'review_token': prepared['review_token'], 'approve': True}).status_code == 400
    assert not fixture.insert_calls


def test_manual_export_fallback_needs_no_oauth_and_never_sends_media(api, tmp_path):
    client, app, service, fixture = api
    profile = post(client, 'profiles', {'profile': {'name': 'Manual Russian'}})['profile']
    path = tmp_path / 'local.mp4'
    make_video(path)
    original = path.read_bytes()
    result = post(client, 'manual', {'profile_id': profile['id'], 'path': str(path), 'metadata': metadata(),
                                  'approved_metadata': True, 'directory': str(tmp_path / 'manual')})
    saved = json.loads(Path(result['files'][0]['path']).read_text())
    assert saved['review']['metadata']['title'] == metadata()['title']
    assert result['studio_url'] == 'https://studio.youtube.com/'
    assert not fixture.requests and not app.state.store.jobs()
    assert path.read_bytes() == original


def test_worker_repository_does_not_interrupt_live_application_jobs(api):
    client, app, service, fixture = api
    job = app.state.store.add_job('qr', 'Synthetic running job', {'options': {}, 'tool': 'qr'})
    app.state.store.update_job(job['id'], status='running')
    other = Repository(data=service.data)
    other.close()
    assert app.state.store.job(job['id'])['status'] == 'running'


def test_public_avatar_is_sanitized_cached_and_never_receives_bearer(api, monkeypatch):
    from io import BytesIO
    from PIL import Image
    import urllib.request
    client, app, service, fixture = api
    profile = connected(api)
    png = BytesIO()
    Image.new('RGB', (200, 100), 'gold').save(png, 'PNG')
    calls = []
    class OwnedAvatarResponse(BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
    class AvatarTransport:
        def open(self, request, timeout):
            calls.append(request)
            assert not request.has_header('Authorization')
            assert request.full_url == 'https://yt3.ggpht.com/synthetic-avatar'
            return OwnedAvatarResponse(png.getvalue())
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: AvatarTransport())
    first = client.get(profile['avatar'])
    second = client.get(profile['avatar'])
    assert first.status_code == second.status_code == 200
    with Image.open(BytesIO(first.content)) as image:
        assert image.format == 'PNG' and image.size == (128, 64)
    assert len(calls) == 1 and first.content == second.content
    saved = service.profile(profile['id'])
    saved['channel_thumbnail'] = 'http://127.0.0.1/private'
    service.repo.put('publishing_profiles', saved)
    assert client.get(profile['avatar']).status_code == 404 and len(calls) == 1


def test_api_routes_remain_authenticated_and_csrf_guard_is_not_weakened(api):
    client, app, service, fixture = api
    result = client.get('/api/language-publishing/state', headers={'X-Forge-Token': ''})
    assert result.status_code == 401
    assert client.post('/api/language-publishing/profiles', json={'name': 'Bad origin'}, headers={'Origin': 'https://example.invalid'}).status_code == 403
    assert client.get('/api/language-publishing/state', headers={'Host': 'example.invalid'}).status_code == 403


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows DPAPI requires native Windows')
def test_native_windows_dpapi_roundtrip_hides_credentials(tmp_path):
    vault = CredentialVault(tmp_path)
    value = {'access_token': ACCESS, 'refresh_token': REFRESH}
    vault.save('native-fixture', value)
    assert ACCESS.encode() not in next(vault.root.iterdir()).read_bytes()
    assert vault.read('native-fixture') == value
    vault.remove('native-fixture')
    assert not vault.exists('native-fixture')


def test_non_windows_does_not_silently_store_plaintext_credentials(tmp_path):
    if os.name == 'nt':
        pytest.skip('Non-Windows refusal is covered on non-Windows hosts')
    vault = CredentialVault(tmp_path)
    assert not vault.available
    with pytest.raises(PublishingError, match='Windows'):
        vault.save('forbidden-plaintext', {'access_token': ACCESS})
    assert not vault.root.exists()


@pytest.mark.parametrize('url', ['http://www.googleapis.com/upload/youtube/v3/videos',
    'https://www.googleapis.com.evil.invalid/upload/youtube/v3/videos',
    'https://oauth2.googleapis.com/upload/youtube/v3/videos', 'https://www.googleapis.com/other/path',
    'https://name:secret@www.googleapis.com/upload/youtube/v3/videos'])
def test_upload_destination_is_strictly_allowlisted(url):
    with pytest.raises(PublishingError):
        upload_url(url)


def test_error_diagnostics_do_not_echo_provider_tokens_or_private_response_body():
    raw = json.dumps({'error': {'message': 'must not echo ' + ACCESS, 'errors': [{'reason': 'insufficientPermissions'}]}}).encode()
    with pytest.raises(PublishingError) as error:
        check_response(Response(403, {}, raw))
    assert 'permission' in str(error.value).lower() and ACCESS not in str(error.value)
