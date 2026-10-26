"""Real authenticated planning APIs with explicitly simulated provider metadata."""
import copy
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from forge.app import create_app
from forge.download_plan_api import identity


class FixtureMetadata:
    """No requests to YouTube; only canonical metadata boundaries are simulated."""
    def __init__(self):
        self.calls = []

    def inspect(self, url, *, watch=False, playlist=False, limit=100, offset=0, cookies=''):
        self.calls.append((url, playlist, offset, cookies))
        items = [{'id': 'media%06d' % number, 'url': 'https://www.youtube.com/watch?v=media%06d' % number,
                  'title': 'Synthetic playlist item %d' % number, 'type': 'video', 'duration': 10 + number,
                  'views': number * 100, 'published': 1700000000 + number} for number in range(8)]
        return {'platform': 'youtube', 'title': 'Synthetic persisted playlist', 'source_id': 'fixture-playlist',
                'entries': items[offset:offset + limit], 'has_more': offset + limit < len(items),
                'offset': offset, 'limit': limit, 'formats': [], 'reported_count': len(items)}


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / 'data', 'synthetic-v9-token', 19879, workers=False)
    adapter = FixtureMetadata()
    app.state.download_studio.adapter = adapter
    with TestClient(app, base_url='http://127.0.0.1:19879', headers={'X-Forge-Token': 'synthetic-v9-token'}) as client:
        yield client, app, adapter
    app.state.store.close()


def post(client, route, body):
    response = client.post('/api/' + route, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def preview(client, **extra):
    return post(client, 'downloads/import/preview', {'url': 'https://www.youtube.com/playlist?list=synthetic-playlist',
                'playlist': True, 'limit': 3, **extra})


def test_manual_alias_deduplicates_shortlinks_and_does_not_allocate_worker(api, tmp_path):
    client, app, adapter = api
    request = {'tool': 'download_video', 'urls': ['https://youtu.be/synthetic01',
               'https://www.youtube.com/watch?v=synthetic01&list=another-list'],
               'options': {'format': 'mp4'}, 'output': {'mode': 'custom', 'directory': str(tmp_path / 'manual')}}
    result = post(client, 'jobs', request)
    assert not result['jobs'] and not app.state.store.jobs() and not adapter.calls
    items = client.get('/api/downloads/plans').json()['items']
    assert len(items) == 1 and items[0]['origin'] == 'manual_single' and items[0]['state'] == 'ready'
    assert app.state.download_planner.payload_for(items[0]['id'])['options']['playlist'] is False
    assert Path(items[0]['destination']) == tmp_path / 'manual'
    assert not (tmp_path / 'manual' / 'YouTube').exists()


def test_playlist_selection_delta_pagination_auth_redaction_and_canonical_submission(api, tmp_path):
    client, app, adapter = api
    cookie = tmp_path / 'private-cookies.txt'
    cookie.write_text('# Netscape HTTP Cookie File\n')
    first = preview(client, cookies=str(cookie))
    key = first['import_id']
    assert first['selection_count'] == 0 and first['total_loaded'] == 3
    selected = post(client, 'downloads/import/selection', {'import_id': key, 'add_selected_ids': ['media000000']})
    assert selected['selection_count'] == 1
    second = preview(client, import_id=key, offset=3)
    assert second['total_loaded'] == 6 and second['selection_count'] == 1 and second['next_offset'] == 6
    selected = post(client, 'downloads/import/selection', {'import_id': key, 'add_selected_ids': ['media000004'],
                        'tool': 'download_video', 'options': {'format': 'mp4'},
                        'output': {'mode': 'custom', 'directory': str(tmp_path / 'playlist')}, 'ordering': 'original'})
    assert selected['selection_count'] == 2
    assert str(cookie) not in json.dumps(client.get('/api/downloads/imports/' + key).json())
    assert str(cookie) not in json.dumps(client.get('/api/downloads/imports').json())
    assert all(call[3] == str(cookie) for call in adapter.calls)
    bad = client.post('/api/downloads/import', json={'import_id': key, 'media_ids': ['unknown'], 'options': {'format': 'mp4'}})
    assert bad.status_code == 400 and not app.state.store.jobs() and app.state.download_planner.snapshot()['total'] == 0
    result = post(client, 'downloads/import', {'import_id': key})
    assert {item['media_id'] for item in result['items']} == {'media000000', 'media000004'}
    assert all(item['origin'] == 'manual_playlist' and item['state'] == 'ready' for item in result['items'])
    for item in result['items']:
        payload = app.state.download_planner.payload_for(item['id'])
        assert payload['options']['cookies'] == str(cookie) and payload['options']['playlist'] is False
        assert Path(payload['destination']) == tmp_path / 'playlist'
    assert not app.state.store.jobs()
    assert str(cookie) not in json.dumps(client.get('/api/downloads/plans').json())


def test_unknown_selection_is_atomic_and_new_preview_pages_do_not_select_themselves(api):
    client, _, _ = api
    record = preview(client)
    key = record['import_id']
    post(client, 'downloads/import/selection', {'import_id': key, 'select_all_loaded': True})
    response = client.post('/api/downloads/import/selection', json={'import_id': key,
        'clear_selection': True, 'add_selected_ids': ['not-an-import-member']})
    assert response.status_code == 400
    assert client.get('/api/downloads/imports/' + key).json()['selection_count'] == 3
    next_page = preview(client, import_id=key, offset=3)
    assert next_page['selection_count'] == 3 and not any(item['selected'] for item in next_page['entries'])


def test_playlist_subfolder_is_opt_in_and_restored_selected_settings_survive_restart(api, tmp_path):
    client, app, _ = api
    key = preview(client)['import_id']
    post(client, 'downloads/import/selection', {'import_id': key, 'add_selected_ids': ['media000001'],
        'output': {'mode': 'custom', 'directory': str(tmp_path / 'chosen')}, 'ordering': 'longest', 'playlist_subfolder': True})
    # A second service instance uses the same persistent DB without starting workers.
    other = create_app(app.state.store.data, 'second-local-token', 19878, workers=False)
    with TestClient(other, base_url='http://127.0.0.1:19878', headers={'X-Forge-Token': 'second-local-token'}) as resumed:
        restored = resumed.get('/api/downloads/imports/' + key).json()
        assert restored['selection_count'] == 1 and restored['ordering'] == 'longest' and restored['playlist_subfolder']
        result = post(resumed, 'downloads/import', {'import_id': key})
        assert Path(result['items'][0]['destination']).parent == tmp_path / 'chosen'
        assert 'Synthetic persisted playlist' in result['items'][0]['destination']
    other.state.store.close()


def test_preferences_are_bounded_shared_aliases_and_never_accept_credentials(api):
    client, _, _ = api
    post(client, 'downloads/preferences', {'preferences': {'tab': 'watch', 'watch_sort': 'name', 'watch_view': 'grid'}})
    assert client.get('/api/watch/preferences').json()['preferences']['watch_view'] == 'grid'
    assert client.post('/api/downloads/preferences', json={'preferences': {'cookies': 'private'}}).status_code == 400
    assert client.post('/api/watch/preferences', json={'preferences': {'watch_search': {'unexpected': 'object'}}}).status_code == 400
    assert client.get('/api/downloads/preferences').json()['preferences']['tab'] == 'watch'


def test_start_all_approved_leaves_review_unapproved_and_unknown_browser_metadata_is_rejected(api, tmp_path, monkeypatch):
    client, app, _ = api
    planner = app.state.download_planner
    review = planner.upsert({'platform': 'youtube', 'id': 'review00001', 'url': 'https://www.youtube.com/watch?v=review00001'}, approved=False)
    called = []
    monkeypatch.setattr(app.state.manager, 'dispatch_once', lambda: called.append(True))
    post(client, 'downloads/plans/start', {})
    assert called and planner.get(review['id'])['state'] == 'review' and not planner.get(review['id'])['approved']
    response = client.post('/api/downloads/import', json={'tool': 'download_video', 'entries': [{'url': 'http://untrusted.invalid'}]})
    assert response.status_code == 422 and planner.snapshot()['total'] == 1


def test_provider_identity_does_not_mix_distinct_generic_urls():
    assert identity('https://youtu.be/example0011')['id'] == identity('https://www.youtube.com/shorts/example0011')['id']
    assert identity('https://a.invalid/file.mp4')['id'] != identity('https://b.invalid/file.mp4')['id']
