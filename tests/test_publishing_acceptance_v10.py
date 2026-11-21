"""Private-acceptance contracts over owned HTTP fixtures, never live Google."""
import copy
import json
import os
from urllib.parse import urlparse

import pytest

from forge import publishing_acceptance as acceptance
from forge.publishing_oauth import Response
from test_language_publishing_v10 import (api, connected, post, execute_upload,
                                         ACCESS, REFRESH, CLIENT, RUSSIAN, ENGLISH, VIDEO_ID)


@pytest.mark.parametrize('fault', [None, 'dirty_checkout', 'changed_source_file', 'unlisted_source_file', 'invalid_manifest'])
def test_source_identity_rechecks_inventory_and_preserves_original_package_binding(tmp_path, monkeypatch, fault):
    from scripts import dubbing_cpu_acceptance
    files = {name: '4' * 64 for name in [
        'forge/language_publishing.py', 'forge/publishing_oauth.py', 'forge/publishing_acceptance.py',
        'web/dubbing_v10.js', 'web/publishing_acceptance_v10.js']}
    inspected = {'source_commit': '1' * 40, 'source_manifest_sha256': '2' * 64,
                 'installed_manifest_sha256': '3' * 64, 'source_files_sha256': files,
                 'source_kind': 'installed_manifest', 'source_checkout_clean': None}
    manifest = {'files': files.copy(), 'package': {'source_manifest_sha256': '2' * 64}}
    if fault == 'dirty_checkout':
        inspected['source_checkout_clean'] = False
    elif fault == 'changed_source_file':
        manifest['files']['forge/publishing_acceptance.py'] = '5' * 64
    elif fault == 'unlisted_source_file':
        del manifest['files']['web/publishing_acceptance_v10.js']
    (tmp_path / 'release-manifest.json').write_text('{' if fault == 'invalid_manifest' else json.dumps(manifest))
    monkeypatch.setattr(acceptance, 'ROOT', tmp_path)
    monkeypatch.setattr(dubbing_cpu_acceptance, 'source_identity', lambda **kwargs: copy.deepcopy(inspected))
    result = acceptance.source_identity()
    assert acceptance._identity_ready(result) is (fault is None)
    if fault is None:
        assert result['source_manifest_sha256'] == '2' * 64
        assert result['installed_manifest_sha256'] == '3' * 64


@pytest.fixture
def prepared(api, monkeypatch):
    client, app, service, fixture = api
    identity = {'source_commit': '1' * 40, 'source_manifest_sha256': '2' * 64,
                'verified': True, 'source_kind': 'owned-protocol-fixture'}
    monkeypatch.setattr(acceptance, 'source_identity', lambda: copy.deepcopy(identity))
    if not hasattr(app.state, 'publishing_acceptance'):
        acceptance.mount(app, service)
    profile = connected(api)
    original = service.http
    class CaptionReadback:
        status = 'serving'
        language = 'ru'
        draft = False
        def request(self, method, url, **kwargs):
            if method == 'GET' and urlparse(url).path == '/youtube/v3/captions':
                assert kwargs['headers']['Authorization'] == 'Bearer ' + ACCESS
                value = {'items': [{'id': 'synthetic-caption-id', 'snippet': {
                    'videoId': VIDEO_ID, 'language': self.language, 'isDraft': self.draft, 'status': self.status}}]}
                return Response(200, {}, json.dumps(value).encode())
            return original.request(method, url, **kwargs)
    transport = CaptionReadback()
    service.http = transport
    response = post(client, 'acceptance/prepare', {'profile_id': profile['id']})
    return client, app, service, fixture, profile, identity, transport, response


def upload(prepared):
    client, app, service, fixture, profile, identity, transport, response = prepared
    queued = post(client, 'upload', {'review_token': response['review_token'], 'approve': True})
    record = queued['record']
    execute_upload(service, record['id'], chunk_size=256 * 1024, job_id=record['job_id'])
    return service.repo.get('publishing_records', record['id'])


def test_preparing_generated_media_does_not_upload_or_consume_approval(prepared):
    client, app, service, fixture, _, _, _, response = prepared
    assert fixture.insert_calls == fixture.caption_calls == 0
    review = response['review']
    assert review['channel_id'] == RUSSIAN
    assert review['metadata']['visibility'] == 'private' and review['metadata']['publish_at'] == ''
    assert review['metadata']['contains_synthetic_media'] is True
    assert client.get(response['preview_url']).headers['content-type'] == 'video/mp4'
    result = post(client, 'acceptance/' + response['acceptance_id'] + '/verify', {})
    assert result['status'] == 'awaiting_private_review' and not result['export_ready']
    assert fixture.insert_calls == 0 and not app.state.store.jobs()
    assert client.post('/api/language-publishing/upload', json={'review_token': response['review_token']}).status_code == 400


def test_real_fixture_upload_and_readback_export_never_claim_live_acceptance(prepared):
    client, app, service, fixture, _, _, _, response = prepared
    record = upload(prepared)
    result = post(client, 'acceptance/' + response['acceptance_id'] + '/verify', {})
    receipt = result['receipt']
    assert result['export_ready'] and receipt['status'] == 'protocol_fixture_verified'
    assert receipt['live_google'] is False
    assert receipt['native_windows_execution'] is (os.name == 'nt')
    assert receipt['approved_channel_sha256'] == receipt['observed_channel_sha256']
    assert receipt['media_sha256'] == record['snapshot']['file']['sha256']
    assert receipt['channel_verified'] and receipt['private_visibility_verified'] and receipt['captions_verified']
    assert app.state.store.get(acceptance.KV_KEY) is None
    assert client.post('/api/language-publishing/acceptance/' + response['acceptance_id'] + '/export', json={}).status_code == 400
    exported = post(client, 'acceptance/' + response['acceptance_id'] + '/export', {'approve': True})
    text = json.dumps(exported['receipt'])
    for private in [ACCESS, REFRESH, CLIENT['installed']['client_secret'], RUSSIAN, VIDEO_ID, str(service.data), 'upload_id=']:
        assert private not in text
    assert fixture.insert_calls == fixture.caption_calls == 1
    assert fixture.status_updates == []  # No public visibility or scheduling write.
    # Recheck/export only reads the accepted video; it cannot upload a duplicate.
    post(client, 'acceptance/' + response['acceptance_id'] + '/verify', {})
    assert fixture.insert_calls == fixture.caption_calls == 1


@pytest.mark.parametrize('fault', ['public', 'wrong_video_channel', 'changed_source', 'changed_media', 'review_mismatch'])
def test_stale_foreign_or_nonprivate_proof_cannot_export(prepared, monkeypatch, fault):
    client, app, service, fixture, _, identity, _, response = prepared
    record = upload(prepared)
    if fault == 'public':
        fixture.response_visibility = 'public'
    elif fault == 'wrong_video_channel':
        fixture.video_channel_override = ENGLISH
    elif fault == 'changed_source':
        monkeypatch.setattr(acceptance, 'source_identity', lambda: {**identity, 'source_commit': '3' * 40})
    elif fault == 'changed_media':
        path = app.state.publishing_acceptance._folder(response['acceptance_id']) / 'generated-private-test.mp4'
        path.write_bytes(path.read_bytes() + b'owned modified fixture')
    elif fault == 'review_mismatch':
        record['snapshot']['metadata']['visibility'] = 'public'
        service.repo.put('publishing_records', record, record['revision'])
    result = client.post('/api/language-publishing/acceptance/' + response['acceptance_id'] + '/verify', json={})
    assert result.status_code == 400
    assert not (app.state.publishing_acceptance._folder(response['acceptance_id']) / 'receipt.json').exists()
    assert app.state.store.get(acceptance.KV_KEY) is None


@pytest.mark.parametrize('status,language,draft', [('syncing','ru',False), ('failed','ru',False), ('serving','en',False), ('serving','ru',True)])
def test_unserved_or_wrong_subtitle_cannot_satisfy_acceptance(prepared, status, language, draft):
    client, app, _, fixture, _, _, transport, response = prepared
    upload(prepared)
    transport.status, transport.language, transport.draft = status, language, draft
    result = post(client, 'acceptance/' + response['acceptance_id'] + '/verify', {})
    assert result['export_ready'] is False
    assert result['status'] in {'caption_processing', 'action_required'}
    assert fixture.insert_calls == 1 and app.state.store.get(acceptance.KV_KEY) is None


def test_prepare_cannot_override_generated_file_visibility_or_schedule(prepared):
    client, app, service, fixture, profile, _, _, _ = prepared
    for extra in [{'path':'unrelated.mp4'}, {'visibility':'public'}, {'publish_at':'2099-01-01T00:00:00Z'}]:
        result = client.post('/api/language-publishing/acceptance/prepare', json={'profile_id': profile['id'], **extra})
        assert result.status_code == 400
    assert fixture.insert_calls == 0


def test_source_binding_and_existing_auth_guards_remain_required(prepared, monkeypatch):
    client, app, service, fixture, profile, _, _, _ = prepared
    monkeypatch.setattr(acceptance, 'source_identity', lambda: {'verified': False})
    state = client.get('/api/language-publishing/acceptance/state').json()
    assert state['can_prepare'] is False
    assert client.post('/api/language-publishing/acceptance/prepare', json={'profile_id': profile['id']}).status_code == 400
    assert client.get('/api/language-publishing/acceptance/state', headers={'X-Forge-Token':''}).status_code == 401
    assert client.post('/api/language-publishing/acceptance/prepare', json={'profile_id': profile['id']}, headers={'Origin':'https://example.invalid'}).status_code == 403
    assert fixture.insert_calls == 0
