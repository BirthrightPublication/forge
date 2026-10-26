"""Populated V0.8 candidate -> V0.9 upgrade and code-only rollback.

Every write is restricted to a NEW disposable directory. The baseline is the
supplied V0.8 development candidate, not a claimed published/stable release.
Actual updater, API, QR and PNG privacy processing run on the host OS. YouTube
metadata is an explicit local fixture and external network access is forbidden.
This does not execute the external PowerShell updater or neural speech models.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('forge_upgrade_integration', ROOT / 'tests/integration_upgrade.py')
original = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(original)
up = original.up

NETWORK_GUARD = r"""
import socket
_getaddrinfo, _connect = socket.getaddrinfo, socket.socket.connect
def _local_resolution(host, *args, **kwargs):
    if host not in {'127.0.0.1', '::1', 'localhost', None}:
        raise AssertionError('External network access is forbidden in this local upgrade fixture.')
    return _getaddrinfo(host, *args, **kwargs)
def _local_connection(sock, address):
    if isinstance(address, tuple) and address[0] not in {'127.0.0.1', '::1', 'localhost'}:
        raise AssertionError('External network access is forbidden in this local upgrade fixture.')
    return _connect(sock, address)
socket.getaddrinfo, socket.socket.connect = _local_resolution, _local_connection
"""

SEED_V8 = original.SEED + NETWORK_GUARD + r"""
import math, struct, wave
from fastapi.testclient import TestClient
from forge.app import create_app
from forge.models import Download
from forge.download_studio import WatchConfig

fixture = json.loads(fixture_path.read_text('utf-8'))
store = Store(data)
settings = {**store.settings(), 'automatic_update_checks': False, 'daily_update_check': False}
store.set('settings', settings)
store.set('watch_paused', True)
fixture['settings'].update(automatic_update_checks=False)
store.close()
app = create_app(data, 'upgrade-fixture-token', 19766, workers=False)
try:
    with TestClient(app, base_url='http://127.0.0.1:19766', headers={'X-Forge-Token': 'upgrade-fixture-token'}) as client:
        def post(path, body):
            response = client.post(path, json=body)
            assert response.status_code == 200, response.text
            return response.json()
        fixture['v8_project'] = post('/api/speech/projects', {'name': 'Preserved V0.8 manuscript',
            'chapters': [{'id': 'chapter-one', 'title': 'One', 'text': 'Original synthetic manuscript.'},
                         {'id': 'chapter-two', 'title': 'Two', 'text': 'A second saved chapter.'}],
            'options': {'engine': 'kokoro', 'voice': 'am_onyx', 'format': 'wav'}})['project']
        fixture['v8_project']['chapters'][1]['text'] = 'A revised second chapter retained in project history.'
        fixture['v8_project'] = post('/api/speech/projects', fixture['v8_project'])['project']
        fixture['v8_voice'] = post('/api/speech/records/voices', {'name': 'Preserved original Onyx favorite',
            'engine': 'kokoro', 'voice': 'am_onyx', 'favorite': True})['record']
        fixture['v8_blend'] = post('/api/speech/records/voices', {'name': 'Preserved compatible blend recipe',
            'engine': 'kokoro', 'blend': [{'voice': 'am_onyx', 'weight': 70}, {'voice': 'am_fenrir', 'weight': 30}],
            'favorite': True})['record']
        fixture['v8_effects'] = post('/api/speech/records/effects', {'name': 'Preserved gain preset',
            'chain': [{'type': 'gain', 'params': {'db': -3}}], 'favorite': True})['record']
        fixture['speech_settings'] = post('/api/speech/settings', {'default_voice': 'am_onyx',
            'favorites': ['am_onyx'], 'pinned': [fixture['v8_voice']['id']], 'preview_text': 'Local synthetic preview.'})['settings']
        archive = data / 'selected-archive-root'; archive.mkdir()
        post('/api/downloads/directories', {'path': str(archive), 'name': 'Preserved archive', 'pinned': True, 'default': True})
        fixture['download_destination'] = str(archive)
        options = Download(format='wav', embed_metadata=False).model_dump()
        config = WatchConfig(initial='review', priority='critical', directory=str(archive),
                             tool='download_audio', options=options).model_dump()
        source_id = 'youtube:UCV08UpgradeFixture'
        channel = {'id': source_id, 'platform': 'youtube', 'url': 'https://www.youtube.com/channel/UCV08UpgradeFixture',
            'title': 'Synthetic V0.8 watched channel', 'folder': 'UCV08UpgradeFixture', 'config': config,
            'created': 1000, 'last_success': 1001, 'next_check': 0, 'failures': 0, 'status': 'ready', 'archive_offset': 0}
        app.state.download_studio.put('watch_sources', source_id, channel)
        downloaded = archive / 'YouTube' / channel['folder'] / 'Audio' / 'preserved.wav'
        downloaded.parent.mkdir(parents=True)
        with wave.open(str(downloaded), 'wb') as stream:
            stream.setparams((1, 2, 16000, 4000, 'NONE', 'not compressed'))
            stream.writeframes(b''.join(struct.pack('<h', round(7000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(4000)))
        stat = downloaded.stat()
        output = {'path': str(downloaded), 'name': downloaded.name, 'size': stat.st_size,
            'sha256': hashlib.sha256(downloaded.read_bytes()).hexdigest(), 'source_stat': {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}}
        for identity, status in [('baseline-done', 'completed'), ('baseline-review', 'review'), ('baseline-pending', 'queued')]:
            key = 'youtube:' + identity
            media = {'id': identity, 'key': key, 'source_id': source_id, 'url': 'https://www.youtube.com/watch?v=' + identity,
                'title': 'Synthetic ' + identity, 'type': 'video', 'published': 1000, 'upload_date': '19700101',
                'duration': .25, 'thumbnail': '', 'availability': 'public', 'status': status,
                'discovered': 1000, 'attempts': int(status != 'review'), 'priority': 'critical'}
            app.state.download_studio.put('watch_media', key, media)
            if status != 'review':
                body = {'tool': 'download_audio', 'url': media['url'], 'source': None, 'options': options,
                    'settings': app.state.store.settings(), 'destination': str(downloaded.parent), 'watch_media': key, 'priority': 'critical'}
                job = app.state.store.add_job('download_audio', media['title'], body)
                media['job_id'] = job['id']
                fixture['legacy_' + status + '_job'] = job['id']
                if status == 'completed':
                    app.state.store.update_job(job['id'], status='completed', progress=100, outputs=[output])
                    media.update(outputs=[output], bytes=output['size'], verified=[output['sha256']], finished=1002)
            app.state.download_studio.put('watch_media', key, media)
            with app.state.store.lock, app.state.store.db:
                app.state.store.db.execute('INSERT INTO watch_discovery VALUES (?,?)', (source_id, key))
        fixture['legacy_source'] = channel
        fixture['legacy_completed_output'] = output
        fixture['legacy_pending_key'] = 'youtube:baseline-pending'
        partial = data / 'download-cache' / hashlib.sha256(fixture['legacy_pending_key'].encode()).hexdigest() / 'legacy.wav.part'
        partial.parent.mkdir(parents=True); partial.write_bytes(b'Synthetic legacy partial transfer state; retain unchanged.')
        fixture['legacy_partial'] = {'path': str(partial), 'sha256': hashlib.sha256(partial.read_bytes()).hexdigest()}
        fixture_path.write_text(json.dumps(fixture), encoding='utf-8')
finally:
    app.state.store.close()
print('Seeded V0.8 speech revisions/profiles/effects, watched media, review, pending job and partial state without network.')
"""

SNAPSHOT = r"""
def table_snapshot(store):
    names = [row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
             if row[0].startswith(('download_plan_', 'download_import')) or row[0] in {
                 'watch_sources', 'watch_media', 'watch_discovery', 'speech_projects', 'speech_project_revisions', 'speech_records'}]
    return {name: {'columns': [row[1] for row in store.db.execute('PRAGMA table_info("' + name + '")')],
                   'rows': [list(row) for row in store.db.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]}
            for name in names}
"""

UPGRADED_V9 = NETWORK_GUARD + r"""
import copy, hashlib, json, os, time
from pathlib import Path
from PIL import Image
from fastapi.testclient import TestClient
from forge.app import create_app
from forge.source_watch import YouTubeAdapter

data = Path(os.environ['FORGE_DATA_DIR'])
fixture_path = Path(os.environ['FORGE_INTEGRATION_FIXTURE'])
fixture = json.loads(fixture_path.read_text('utf-8'))
""" + original.CHECK_STATE + SNAPSHOT + r"""
class LocalMetadataFixture(YouTubeAdapter):
    def inspect(self, url, **kwargs):
        playlist = kwargs.get('playlist', False)
        ids = ['playlist-a', 'playlist-b', 'playlist-c'] if playlist else ['watch-a', 'watch-b', 'playlist-a']
        return {'platform': 'youtube', 'source_id': 'PLV09UpgradeFixture' if playlist else 'UCV09UpgradeFixture',
            'url': url, 'title': 'Synthetic selected playlist' if playlist else 'Synthetic V0.9 watched channel',
            'thumbnail': '', 'thumbnail_data': '', 'reported_count': None, 'reported_count_scope': 'Unknown fixture count',
            'entries': [{'id': value, 'url': 'https://www.youtube.com/watch?v=' + value, 'title': 'Synthetic ' + value,
                         'type': 'video', 'published': 2000 + index, 'duration': 10 + index, 'view_count': None,
                         'upload_date': '19700101', 'thumbnail': '', 'availability': 'public'} for index, value in enumerate(ids)],
            'offset': 0, 'limit': 100, 'has_more': False, 'checked': time.time()}

def open_candidate():
    app = create_app(data, 'upgrade-fixture-token', 19766, workers=False)
    app.state.download_studio.adapter = LocalMetadataFixture()
    return app

app = open_candidate()
try:
    with TestClient(app, base_url='http://127.0.0.1:19766', headers={'X-Forge-Token': 'upgrade-fixture-token'}) as client:
        def post(path, body):
            response = client.post(path, json=body)
            assert response.status_code == 200, response.text
            return response.json()
        state = client.get('/api/state').json()
        assert state['version'] == os.environ['FORGE_EXPECTED_VERSION']
        check_state(app.state.store)
        assert app.state.store.get('download_default') == fixture['download_destination']
        assert app.state.store.get('speech_settings') == fixture['speech_settings']
        app.state.download_planner.wait_for_verifications()
        app.state.download_planner.reconcile(force=True)
        migrated = app.state.download_planner.get_media('youtube:baseline-review')
        assert migrated['state'] == 'review' and not migrated['approved']
        migrated_pending = app.state.download_planner.get_media(fixture['legacy_pending_key'])
        assert migrated_pending['state'] == 'ready' and migrated_pending['job_id'] == fixture['legacy_queued_job']
        assert app.state.store.job(fixture['legacy_queued_job'])['status'] == 'planned'
        assert app.state.store.job(fixture['legacy_queued_job'])['payload']['resume_key'] == fixture['legacy_pending_key']
        completed = app.state.download_planner.get_media('youtube:baseline-done')
        assert completed['state'] == 'completed' and completed['outputs'][0]['path'] == fixture['legacy_completed_output']['path']
        legacy_source = app.state.download_studio.get('watch_sources', fixture['legacy_source']['id'])
        assert legacy_source['folder'] == fixture['legacy_source']['folder'] and legacy_source['config']['priority'] == 'ultra'
        assert legacy_source['config']['instant_download'] is True
        fixture['migrated_pending_plan'] = migrated_pending['id']
        before = app.state.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        preview = post('/api/downloads/import/preview', {'url': 'https://www.youtube.com/playlist?list=PLV09UpgradeFixture', 'playlist': True})
        imported = preview['import_id']
        output = data / 'selected-manual-root'; output.mkdir()
        selection = post('/api/downloads/import/selection', {'import_id': imported, 'selected_ids': ['playlist-a', 'playlist-b'],
            'tool': 'download_video', 'options': {'format': 'mp4', 'filename_style': 'playlist_index'},
            'output': {'mode': 'custom', 'directory': str(output)}, 'ordering': 'playlist'})
        assert selection['selection_count'] == 2
        planned = post('/api/downloads/import', {'import_id': imported})
        assert not planned['jobs'] and len(planned['plans']) == 2
        fixture['v9_import'] = imported
        fixture['v9_playlist_plans'] = [item['id'] for item in planned['plans']]
        fixture['v9_batch'] = planned['batch']['id']
        assert app.state.download_planner.progress(fixture['v9_batch'])['total'] == 2
        channel_root = data / 'selected-channel-root'; channel_root.mkdir()
        watch = post('/api/watch', {'url': 'https://www.youtube.com/channel/UCV09UpgradeFixture', 'config': {
            'initial': 'review', 'instant_download': False, 'priority': 'ultra', 'video_order': 'most_viewed',
            'directory': str(channel_root), 'options': {'format': 'mp4'}}})
        channel_id = 'youtube:UCV09UpgradeFixture'
        fixture['v9_source'] = channel_id
        assert app.state.download_planner.get_media('youtube:watch-a')['state'] == 'review'
        assert app.state.store.db.execute("SELECT COUNT(*) FROM download_plan_items WHERE media_key='youtube:playlist-a'").fetchone()[0] == 1
        post('/api/watch/configure', {'id': channel_id, 'config': {'instant_download': True}})
        assert app.state.download_planner.get_media('youtube:watch-a')['state'] == 'review'
        assert app.state.download_planner.get_media('youtube:watch-b')['state'] == 'review'
        source = app.state.download_studio.get('watch_sources', channel_id)
        fresh = LocalMetadataFixture().inspect(source['url'])
        fresh['entries'].insert(0, {'id': 'watch-new', 'url': 'https://www.youtube.com/watch?v=watch-new',
            'title': 'Synthetic fresh upload', 'type': 'video', 'published': time.time(), 'duration': 12,
            'view_count': None, 'upload_date': '20261026', 'thumbnail': '', 'availability': 'public'})
        app.state.download_studio.ingest(source, fresh)
        assert app.state.download_planner.get_media('youtube:watch-new')['state'] == 'ready'
        skipped = app.state.download_planner.get_media('youtube:watch-b')
        post('/api/downloads/plans/action', {'ids': [skipped['id']], 'action': 'skip'})
        assert app.state.download_planner.get_media('youtube:watch-b')['state'] == 'skipped'
        assert app.state.download_planner.progress(fixture['v9_batch'])['total'] == 2
        assert app.state.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == before
        manual = post('/api/downloads/import', {'urls': ['https://www.youtube.com/watch?v=failed-fixture'],
            'tool': 'download_video', 'options': {'format': 'mp4'}, 'output': {'mode': 'custom', 'directory': str(output)}})['plans'][0]
        admitted = app.state.download_planner.admit_one(lambda job: job['payload'].get('download_plan_id') == manual['id'])
        assert admitted is not None
        app.state.store.update_job(admitted['id'], status='failed', message='Synthetic prior authentication failure; no network request was made.')
        app.state.store.update_meta(admitted['id'], error_code='authentication', error_retryable=False)
        app.state.download_planner.reconcile(force=True)
        assert app.state.download_planner.get(manual['id'])['state'] == 'failed'
        fixture['v9_failed_job'] = admitted['id']
        preferences = {'tab': 'priority', 'watch_sort': 'priority', 'watch_direction': 'desc', 'watch_view': 'grid'}
        assert post('/api/downloads/preferences', {'preferences': preferences})['preferences'] == preferences
        fixture['v9_preferences'] = preferences
        fixture['v9_new_project'] = post('/api/speech/projects', {'name': 'Speech draft saved after V0.9 upgrade',
            'text': 'An additional synthetic manuscript survives rollback.'})['project']
        def run_worker(body):
            response = post('/api/jobs', body)
            job = response['jobs'][0]
            app.state.manager.launch(app.state.store.job(job['id']))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                value = app.state.store.job(job['id'])
                if value['status'] == 'completed':
                    return value
                assert value['status'] not in {'failed', 'cancelled', 'interrupted', 'blocked'}, value
                time.sleep(.1)
            raise AssertionError('Real QR/privacy worker did not finish.')
        qr = run_worker({'tool': 'qr', 'options': {'text': 'Synthetic V0.9 upgrade worker', 'name': 'V0.9 upgrade QR'}, 'output': {'mode': 'default'}})
        assert Path(qr['outputs'][0]['path']).read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
        registered = post('/api/register', {'paths': [fixture['privacy_source']]})['sources'][0]['id']
        privacy = run_worker({'tool': 'privacy_export', 'sources': [registered], 'options': {'preset': 'advanced', 'reports': True}, 'output': {'mode': 'default'}})
        cleaned = next(Path(item['path']) for item in privacy['outputs'] if item['name'].endswith('.png'))
        with Image.open(cleaned) as image, Image.open(fixture['privacy_source']) as original_image:
            assert image.size == original_image.size and image.tobytes() == original_image.tobytes()
            assert 'Author' not in image.info
        fixture['real_worker_jobs'] = [qr['id'], privacy['id']]
        fixture['new_outputs'] = [{'path': item['path'], 'sha256': hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()}
                                  for job in (qr, privacy) for item in job['outputs']]
        check_state(app.state.store)
        fixture_path.write_text(json.dumps(fixture), encoding='utf-8')
finally:
    app.state.store.close()

app = open_candidate()
try:
    with TestClient(app, base_url='http://127.0.0.1:19766', headers={'X-Forge-Token': 'upgrade-fixture-token'}) as client:
        app.state.download_planner.wait_for_verifications()
        check_state(app.state.store)
        imported = client.get('/api/downloads/imports/' + fixture['v9_import']).json()
        assert {item['id'] for item in imported['entries'] if item['selected']} == {'playlist-a', 'playlist-b'}
        assert app.state.download_planner.progress(fixture['v9_batch'])['total'] == 2
        assert app.state.download_planner.get_media('youtube:watch-a')['state'] == 'review'
        assert app.state.download_planner.get_media('youtube:watch-new')['state'] == 'ready'
        assert app.state.download_planner.get_media('youtube:watch-b')['state'] == 'skipped'
        assert app.state.store.get('media_downloads_v9_preferences') == fixture['v9_preferences']
finally:
    app.state.store.close()
# Capture after lifecycle shutdown, with no scheduler or verification writer left.
from forge.core import Store
store = Store(data)
try:
    snapshot = table_snapshot(store)
    assert snapshot['download_plan_items']['rows'] and snapshot['download_import_entries']['rows']
    fixture['v9_table_snapshot'] = snapshot
    fixture_path.write_text(json.dumps(fixture), encoding='utf-8')
finally:
    store.close()
print('V0.9 migration, planning/import/batch persistence, offline restart and actual QR/privacy workers passed.')
"""

ROLLED_BACK_V8 = NETWORK_GUARD + r"""
import hashlib, json, os
from pathlib import Path
from fastapi.testclient import TestClient
from forge.app import create_app

data = Path(os.environ['FORGE_DATA_DIR'])
fixture = json.loads(Path(os.environ['FORGE_INTEGRATION_FIXTURE']).read_text('utf-8'))
""" + original.CHECK_STATE + SNAPSHOT + r"""
app = create_app(data, 'upgrade-fixture-token', 19766, workers=False)
try:
    with TestClient(app, base_url='http://127.0.0.1:19766', headers={'X-Forge-Token': 'upgrade-fixture-token'}) as client:
        response = client.get('/api/state')
        assert response.status_code == 200 and response.json()['version'] == os.environ['FORGE_BASE_VERSION']
        check_state(app.state.store)
        assert app.state.store.get('speech_settings') == fixture['speech_settings']
        assert client.get('/api/speech/projects/' + fixture['v9_new_project']['id']).json() == fixture['v9_new_project']
        assert app.state.store.get('media_downloads_v9_preferences') == fixture['v9_preferences']
        assert table_snapshot(app.state.store) == fixture['v9_table_snapshot']
        for job in fixture['real_worker_jobs']:
            assert app.state.store.job(job)['status'] == 'completed'
        for output in fixture['new_outputs']:
            assert hashlib.sha256(Path(output['path']).read_bytes()).hexdigest() == output['sha256']
finally:
    app.state.store.close()
print('V0.8 API reopened the V0.9 database; raw plan/import/watch/speech rows and all media remain unchanged.')
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-test-writes', action='store_true')
    args = parser.parse_args(argv)
    if not args.allow_test_writes:
        parser.error('Pass --allow-test-writes only for a NEW disposable directory.')
    home = args.output.resolve()
    if home.exists():
        raise RuntimeError('Output directory already exists; existing installations are never reused.')
    if any((parent / 'VERSION.txt').is_file() and (parent / 'forge/app.py').is_file() for parent in home.parents):
        raise RuntimeError('Keep the disposable test outside any existing FORGE source or installation.')
    base, candidate = args.base.resolve(), args.build.resolve()
    base_manifest, _ = up.inspect_release(base)
    candidate_manifest, _ = up.inspect_release(candidate)
    if up.version(base_manifest['version'])[:2] != (0, 8) or up.version(candidate_manifest['version'])[:2] != (0, 9):
        raise RuntimeError('This fixture requires a V0.8 baseline candidate and a newer V0.9 candidate.')
    home.mkdir(parents=True)
    data, builds = home / 'test-user-data', home / 'Build'
    builds.mkdir()
    original.extract_base(base, home)
    installation = up.find_installation(home)
    fixtures = ROOT / 'tests/fixtures/windows_updater'
    shutil.copyfile(fixtures / 'Update-Application.ps1', installation / 'Update-Application.ps1')
    shutil.copyfile(fixtures / 'Update FORGE.bat', installation / 'Update FORGE.bat')
    original_code = {path.relative_to(installation).as_posix(): original.digest(path)
                     for path in installation.rglob('*') if path.is_file()}
    protected = {}
    for folder, relative, content in [
        (installation, '.venv/preserve.txt', b'Synthetic private Python environment'),
        (installation, '.diarization-venv/preserve.txt', b'Synthetic speaker environment'),
        (installation, 'models/preserve.bin', b'Synthetic installation model cache'),
        (installation, 'tools/preserve.txt', b'Synthetic user utility'),
        (installation, 'Config/preserve.txt', b'Synthetic private configuration'),
        (installation, 'exports/preserve.txt', b'Synthetic existing export'),
        (data, 'environments/speech-kokoro/preserve.txt', b'Synthetic immutable Kokoro environment'),
        (data, 'environments/speech-ming/preserve.txt', b'Synthetic immutable Ming environment'),
        (data, 'models/speech/preserve.bin', b'Synthetic speech/model cache'),
        (data, 'presets/preserve.json', b'{"fixture": "preserve"}'),
        (data, 'private-input.txt', b'Synthetic original input; never overwrite'),
    ]:
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise RuntimeError('Disposable fixture unexpectedly already exists.')
        path.write_bytes(content)
        protected[path] = original.digest(path)
    for relative in ('Update FORGE.bat', 'Update-Application.ps1'):
        protected[installation / relative] = original.digest(installation / relative)
    fixture_path = home / 'fixture.json'
    env = {**os.environ, 'FORGE_DATA_DIR': str(data), 'FORGE_INTEGRATION_FIXTURE': str(fixture_path),
           'FORGE_BASE_VERSION': base_manifest['version'], 'FORGE_EXPECTED_VERSION': candidate_manifest['version'],
           'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
           'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1'}
    def execute(command, *, timeout=180):
        result = subprocess.run(command, cwd=installation, env=env, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=timeout)
        with (home / 'integration.log').open('a', encoding='utf-8') as log:
            log.write(result.stdout + '\n' + result.stderr + '\n')
        if result.returncode:
            raise RuntimeError(result.stdout + '\n' + result.stderr)
        print(result.stdout.strip())
    def preserved():
        for path, expected in protected.items():
            assert original.digest(path) == expected, str(path)
    execute([sys.executable, '-c', SEED_V8])
    fixture = json.loads(fixture_path.read_text('utf-8'))
    for path, expected in [(Path(fixture['old_output']), original.digest(Path(fixture['old_output']))),
                           (Path(fixture['privacy_source']), fixture['privacy_source_sha256']),
                           (Path(fixture['legacy_completed_output']['path']), fixture['legacy_completed_output']['sha256']),
                           (Path(fixture['legacy_partial']['path']), fixture['legacy_partial']['sha256'])]:
        protected[path] = expected
    shutil.copyfile(candidate, builds / ('FORGE-v' + candidate_manifest['version'] + '.zip'))
    execute([sys.executable, 'scripts/update.py', '--root', str(home), '--yes', '--no-launch'])
    assert up.installed_version(installation) == candidate_manifest['version']
    backup = Path(json.loads((installation / 'update-last.json').read_text('utf-8'))['backup'])
    assert (backup / 'journal.json').is_file() and (backup / 'user-data-snapshot/forge.sqlite3').is_file()
    preserved()
    execute([sys.executable, '-c', UPGRADED_V9])
    fixture = json.loads(fixture_path.read_text('utf-8'))
    for output in fixture['new_outputs']:
        protected[Path(output['path'])] = output['sha256']
    preserved()
    execute([sys.executable, 'scripts/update.py', '--root', str(home), '--rollback', '--yes', '--no-launch'])
    assert up.installed_version(installation) == base_manifest['version']
    for relative, expected in original_code.items():
        assert original.digest(installation / relative) == expected, relative
    preserved()
    execute([sys.executable, '-c', ROLLED_BACK_V8])
    snapshot = fixture['v9_table_snapshot']
    report = {'result': 'passed', 'host': sys.platform, 'native_windows_execution': os.name == 'nt',
        'external_powershell_updater_executed': False, 'neural_models_executed': False, 'live_youtube_tested': False,
        'metadata_scope': 'Explicit local fixture; external network blocked',
        'base_version': base_manifest['version'], 'base_description': 'Supplied V0.8 development candidate; no stable publication claim',
        'candidate_version': candidate_manifest['version'], 'base_sha256': original.digest(base),
        'candidate_sha256': original.digest(candidate), 'protected_files_verified': len(protected),
        'original_scripts_preserved': sum(path.startswith('original_scripts/') for path in original_code),
        'table_rows_preserved': {name: len(value['rows']) for name, value in snapshot.items()},
        'table_snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        'checks': ['Verified source manifests and actual installed updater CLI upgrade/rollback',
                   'Populated V0.8 settings, workflows, presets, speech revisions/profiles/effects and watched media preserved',
                   'Legacy review stays outside queue; queued watched job retains ID and compatible partial-cache identity',
                   'Manual playlist selections, deduplicated channel associations and frozen batch denominator persist',
                   'Instant Download toggle preserves backlog review and approves only fresh fixture discoveries',
                   'Plans remain separate from jobs until explicit resource admission',
                   'Actual QR/PNG privacy workers complete with original pixels/input preserved',
                   'V0.9 restart retains imported selection, plans, source settings, preferences and batch membership',
                   'Code-only rollback restores baseline bytes and reopens V0.8 API against the current database',
                   'Raw V0.9 plan/import/watch/speech table rows remain byte-identical after code rollback',
                   'External updater files, environments, caches, partials and original/published files remain unchanged']}
    (home / 'integration-result.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return report


if __name__ == '__main__':
    main()
