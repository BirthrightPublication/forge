"""Persistent planning tests; no public platform access or simulated downloads.

State-machine fixtures are explicit. Completion cases use real tiny WAV media and
FFprobe, so a declared successful job or an arbitrary renamed file cannot count.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import threading
import time
import wave

import pytest

from forge.core import Store
from forge.download_plans import DownloadPlanner, PRIORITIES


@pytest.fixture
def planner(tmp_path):
    store = Store(tmp_path / 'data')
    service = DownloadPlanner(store)
    yield service
    service.close()
    store.close()


def add(planner, identity, *, source='', origin='channel', priority='medium', ordering='newest', approved=True,
        published=None, views=None, duration=None, position=0, automatic=False, extra=None):
    media = {'id': identity, 'title': identity, 'url': 'https://www.youtube.com/watch?v=' + identity,
             'published': published, 'views': views, 'duration': duration, **(extra or {})}
    payload = {'tool': 'download_video', 'url': media['url'], 'destination': str(planner.store.data / 'exports'),
               'options': {'format': 'mp4', 'cookies': '/private/local-cookies.txt'}}
    return planner.upsert(media, source_id=source, source_title=source, origin=origin, priority=priority,
                          ordering=ordering, approved=approved, payload=payload, tool='download_video',
                          playlist_id=source if origin == 'manual_playlist' else '', position=position, automatic=automatic)


def admit(planner):
    return planner.admit_one(lambda candidate: True)


def tiny_wav(directory, name='verified.wav'):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 16000, 8000, 'NONE', 'not compressed'))
        audio.writeframes(b'\x00\x01' * 8000)
    return {'path': str(path), 'name': path.name, 'size': path.stat().st_size,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def finish(planner, job, output):
    planner.store.update_job(job['id'], status='completed', progress=100, outputs=[output])
    planner.reconcile(force=True)
    assert planner.wait_for_verifications(timeout=10)


def watch_tables(store, media, source=None):
    source = source or {'id': 'youtube:channel', 'title': 'Preserved channel', 'folder': 'stable',
                        'config': {'priority': 'normal', 'options': {'format': 'mp4'},
                                   'directory': str(store.data / 'exports'), 'enabled': True}}
    store.db.executescript('''CREATE TABLE IF NOT EXISTS watch_sources(id TEXT PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS watch_media(id TEXT PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS watch_discovery(source_id TEXT,media_id TEXT,PRIMARY KEY(source_id,media_id));''')
    store.db.execute('INSERT OR REPLACE INTO watch_sources VALUES (?,?)', (source['id'], json.dumps(source)))
    store.db.execute('INSERT OR REPLACE INTO watch_media VALUES (?,?)', (media['key'], json.dumps(media)))
    store.db.execute('INSERT OR REPLACE INTO watch_discovery VALUES (?,?)', (source['id'], media['key']))
    store.db.commit()


def test_discovery_and_approval_stay_outside_processing_queue(planner):
    row = add(planner, 'review', approved=False)
    assert row['state'] == 'review' and planner.store.jobs() == []
    assert admit(planner) is None
    planner.action([row['id']], 'approve')
    assert planner.get(row['id'])['state'] == 'ready' and planner.store.jobs() == []
    assert planner.admit_one(lambda _: False) is None and planner.store.jobs() == []
    job = admit(planner)
    assert job['payload']['download_plan_id'] == row['id']
    assert planner.get(row['id'])['state'] == 'scheduled'
    assert admit(planner) is None and len(planner.store.jobs()) == 1
    public = json.dumps(planner.snapshot())
    assert 'local-cookies' not in public and 'cookies' not in public


def test_global_classes_and_six_channel_priority_levels(planner):
    for priority in PRIORITIES:
        add(planner, priority, source=priority, priority=priority)
    add(planner, 'playlist', origin='manual_playlist', source='playlist')
    add(planner, 'manual', origin='manual_single')
    assert [admit(planner)['label'] for _ in range(8)] == ['manual', 'playlist', *reversed(PRIORITIES)]


def test_same_priority_fair_round_robin_respects_metadata_and_display(planner):
    for channel in ('A', 'B'):
        planner.configure_source(channel, priority='high', ordering='most_viewed')
        for index in (1, 3, 2):
            add(planner, channel + str(index), source=channel, priority='high', ordering='most_viewed', views=index)
    displayed = [row['media_id'] for row in planner.snapshot()['items']]
    assert displayed == ['A3', 'B3', 'A2', 'B2', 'A1', 'B1']
    assert [admit(planner)['label'] for _ in range(6)] == displayed


@pytest.mark.parametrize('ordering,field,expected', [
    ('newest', 'published', ['3', '2', '1', 'unknown']), ('oldest', 'published', ['1', '2', '3', 'unknown']),
    ('most_viewed', 'views', ['3', '2', '1', 'unknown']), ('least_viewed', 'views', ['1', '2', '3', 'unknown']),
    ('longest', 'duration', ['3', '2', '1', 'unknown']), ('shortest', 'duration', ['1', '2', '3', 'unknown']),
])
def test_channel_ordering_unknowns_and_manual_playlist_ordering(planner, ordering, field, expected):
    for origin in ('channel', 'manual_playlist'):
        source = origin
        planner.configure_source(source, ordering=ordering)
        for index in ('unknown', '2', '1', '3'):
            add(planner, origin + index, source=source, origin=origin, ordering=ordering,
                **{field: None if index == 'unknown' else int(index)})
    expected_full = ['manual_playlist' + item for item in expected] + ['channel' + item for item in expected]
    assert [row['media_id'] for row in planner.snapshot()['items']] == expected_full
    assert [admit(planner)['label'] for _ in range(8)] == expected_full


def test_explicit_playlist_move_swaps_real_waiting_order_without_touching_active(planner):
    for index in (1, 3, 2):
        add(planner, str(index), source='playlist', origin='manual_playlist', ordering='most_viewed', views=index, position=index)
    rows = planner.snapshot()['items']
    planner.action([rows[1]['id']], 'up')
    displayed = [row['media_id'] for row in planner.snapshot()['items']]
    assert displayed == ['2', '3', '1']
    first = admit(planner)
    with pytest.raises(ValueError, match='Active'):
        planner.action([first['plan_id']], 'down')
    assert [first['label'], admit(planner)['label'], admit(planner)['label']] == displayed


def test_priority_change_reorders_pending_without_altering_active_snapshot(planner):
    planner.configure_source('A', priority='low')
    a = add(planner, 'A', source='A', priority='low')
    add(planner, 'B', source='B', priority='high')
    planner.configure_source('A', priority='ultra')
    first = admit(planner)
    assert first['label'] == 'A' and first['channel_priority'] == 'ultra'
    planner.configure_source('A', priority='minimal')
    assert planner.get(a['id'])['channel_priority'] == 'ultra'
    assert planner.store.job(first['id'])['channel_priority'] == 'ultra'


def test_manual_request_reuses_and_upgrades_watched_plan(planner):
    channel = add(planner, 'same', source='A', approved=False)
    manual = add(planner, 'same', origin='manual_single', approved=True)
    assert manual['id'] == channel['id'] and manual['origin'] == 'manual_single'
    assert planner.snapshot()['total'] == 1
    assert admit(planner)['label'] == 'same' and admit(planner) is None
    active = add(planner, 'same', source='A', origin='channel', extra={'eligible': False})
    assert active['state'] == 'scheduled' and active['origin'] == 'manual_single'


def test_explicit_channel_approval_survives_automatic_refresh_and_pause(planner):
    planner.configure_source('A', enabled=True)
    row = add(planner, 'review', source='A', approved=False, automatic=True)
    planner.approve_channel_review('A')
    add(planner, 'review', source='A', approved=False, automatic=True)
    planner.configure_source('A', enabled=False)
    assert planner.get(row['id'])['automatic'] is False
    assert admit(planner) is not None


def test_available_transition_clears_historical_and_does_not_release_review(planner):
    first = planner.upsert({'id': 'live', 'url': 'https://example.invalid/live', 'eligible': False},
                           source_id='A', historical=True, approved=False)
    assert first['state'] == 'unavailable'
    later = planner.upsert({'id': 'live', 'url': 'https://example.invalid/live', 'eligible': True},
                           source_id='A', historical=False, approved=False)
    assert later['state'] == 'review' and later['historical'] is False
    assert admit(planner) is None


def test_upsert_many_rolls_back_prior_items_on_later_validation_failure(planner):
    requests = [{'media': {'id': 'valid', 'url': 'https://example.invalid/a'}, 'approved': True},
                {'media': {'id': '../invalid'}, 'approved': True}]
    with pytest.raises(ValueError):
        planner.upsert_many(requests)
    assert planner.progress()['total'] == 0 and planner.store.jobs() == []


def test_two_connections_cannot_admit_duplicate_job(planner):
    add(planner, 'concurrent', origin='manual_single')
    other_store = Store(planner.store.data)
    other = DownloadPlanner(other_store)
    gate = threading.Barrier(2)
    def claim(service):
        gate.wait()
        return admit(service)
    try:
        with ThreadPoolExecutor(max_workers=2) as threads:
            results = list(threads.map(claim, (planner, other)))
        assert sum(result is not None for result in results) == 1
        assert len(planner.store.jobs()) == 1
    finally:
        other.close(); other_store.close()


@pytest.mark.skipif(not shutil.which('ffprobe'), reason='Real FFprobe required')
def test_completion_requires_actual_verified_media_and_batches_freeze_denominator(planner):
    valid = add(planner, 'valid', origin='manual_single')
    review = add(planner, 'review', approved=False)
    batch = planner.frozen_batch([valid['id'], review['id']], skipped_resolved=False)
    first = admit(planner)
    assert planner.progress()['percent'] == 0
    output = tiny_wav(Path(first['payload']['destination']))
    finish(planner, first, output)
    assert planner.progress(batch['id'])['percent'] == 50
    add(planner, 'later', approved=False)
    assert planner.progress(batch['id'])['total'] == 2 and planner.progress()['total'] == 3
    planner.action([review['id']], 'skip')
    assert planner.progress(batch['id'])['percent'] == 50
    resolved = planner.frozen_batch([valid['id'], review['id']], skipped_resolved=True)
    assert planner.progress(resolved['id'])['percent'] == 100
    planner.action([valid['id']], 'remove')
    assert Path(output['path']).is_file()
    assert planner.progress(batch['id'])['total'] == 2
    assert planner.progress(batch['id'])['percent'] == 50
    assert planner.progress(resolved['id'])['percent'] == 100


@pytest.mark.skipif(not shutil.which('ffprobe'), reason='Real FFprobe required')
@pytest.mark.parametrize('invalid', ['missing', 'fake', 'empty'])
def test_a_success_status_or_fake_extension_cannot_count_as_completion(planner, invalid):
    add(planner, 'bad', origin='manual_single')
    job = admit(planner)
    directory = Path(job['payload']['destination']); directory.mkdir()
    path = directory / 'bad.mp4'
    if invalid != 'missing':
        path.write_bytes(b'not real video' if invalid == 'fake' else b'')
    planner.store.update_job(job['id'], status='completed', outputs=[{'path': str(path), 'size': 14 if invalid == 'missing' else path.stat().st_size}])
    planner.reconcile(force=True); assert planner.wait_for_verifications()
    assert planner.progress()['completed'] == 0 and planner.progress()['percent'] == 0
    assert planner.get(job['plan_id'])['state'] == 'failed'


def test_restart_recovery_and_manual_cancel_intent_do_not_duplicate_or_override_user_pause(planner):
    first = add(planner, 'interrupted', origin='manual_single')
    job = admit(planner)
    planner.store.update_job(job['id'], status='interrupted')
    assert planner.recover() == 1
    retry = admit(planner)
    assert retry['id'] != job['id'] and retry['payload']['resume_key'] == job['payload']['resume_key']
    assert retry['payload']['resume_from_job'] == job['id']
    # Root QueueManager records cancel_intent before a process can be interrupted.
    with planner.store.lock, planner.store.db:
        meta = json.loads(planner.store.db.execute('SELECT value FROM job_meta WHERE id=?', (retry['id'],)).fetchone()[0])
        meta['cancel_intent'] = 'user'
        planner.store.db.execute('UPDATE job_meta SET value=? WHERE id=?', (json.dumps(meta), retry['id']))
    planner.store.update_job(retry['id'], status='interrupted')
    planner.reconcile(force=True); planner.recover()
    assert planner.get(first['id'])['paused'] is True and admit(planner) is None


@pytest.mark.skipif(not shutil.which('ffprobe'), reason='Real FFprobe required')
def test_published_full_result_recovers_after_power_loss_without_new_job(planner):
    row = add(planner, 'powerloss', origin='manual_single')
    job = admit(planner)
    output = tiny_wav(Path(job['payload']['destination']))
    directory = planner.store.data / 'work' / job['id']; directory.mkdir(parents=True)
    (directory / 'result.json').write_text(json.dumps({'complete': True, 'outputs': [output], 'warnings': []}))
    planner.store.update_job(job['id'], status='interrupted')
    planner.recover(); assert admit(planner) is None
    assert planner.wait_for_verifications()
    assert planner.get(row['id'])['state'] == 'completed'
    assert planner.store.job(job['id'])['status'] == 'completed' and len(planner.store.jobs()) == 1


def test_legacy_queued_job_migrates_atomically_and_preserves_id_resume_and_black_graph(planner):
    media = {'key': 'youtube:old', 'id': 'old', 'source_id': 'youtube:channel', 'url': 'https://www.youtube.com/watch?v=old',
             'title': 'Old', 'status': 'queued', 'attempts': 1, 'outputs': []}
    watch_tables(planner.store, media)
    payload = {'tool': 'download_video', 'options': {'format': 'mp4'}, 'url': media['url'], 'destination': str(planner.store.data / 'exports'),
               'watch_media': media['key'], 'resume_key': 'legacy-explicit-key'}
    job = planner.store.add_job('download_video', 'Old', payload)
    parent = planner.store.add_job('download_video', 'Black parent', {'url': 'https://www.youtube.com/watch?v=black', 'options': {}, 'destination': str(planner.store.data), 'workflow_name': 'Preserved'})
    child = planner.store.add_job('transcribe', 'Black child', {'depends_on': parent['id']})
    planner.migrate_legacy()
    item = planner.get_media(media['key'])
    assert item['job_id'] == job['id'] and planner.store.job(job['id'])['status'] == 'planned'
    assert planner.store.job(parent['id'])['status'] == 'queued' and planner.store.job(child['id'])['payload']['depends_on'] == parent['id']
    admitted = admit(planner)
    assert admitted['id'] == job['id'] and admitted['payload']['resume_key'] == 'legacy-explicit-key'
    assert planner.migrate_legacy() == 0


@pytest.mark.skipif(not shutil.which('ffprobe'), reason='Real FFprobe required')
@pytest.mark.parametrize('outputs_kind', ['empty', 'missing', 'fake', 'valid'])
def test_legacy_completed_claims_require_real_output_verification(planner, outputs_kind):
    outputs = []
    directory = planner.store.data / 'exports'; directory.mkdir()
    if outputs_kind == 'valid':
        outputs = [tiny_wav(directory)]
    elif outputs_kind in {'missing', 'fake'}:
        path = directory / 'past.mp4'
        if outputs_kind == 'fake': path.write_bytes(b'not real media')
        outputs = [{'path': str(path), 'size': 14}]
    media = {'key': 'youtube:legacydone', 'id': 'legacydone', 'source_id': 'youtube:channel', 'url': 'https://www.youtube.com/watch?v=legacydone',
             'title': 'Past', 'status': 'completed', 'attempts': 1, 'outputs': outputs}
    watch_tables(planner.store, media)
    planner.migrate_legacy()
    assert planner.progress()['completed'] == 0
    assert planner.wait_for_verifications()
    assert planner.progress()['completed'] == (1 if outputs_kind == 'valid' else 0)
    assert planner.progress()['percent'] == (100 if outputs_kind == 'valid' else 0)


def test_pagination_is_bounded_and_does_not_fabricate_ranks_for_reviews(planner):
    for index in range(225):
        add(planner, 'media' + str(index), source='A', approved=False)
    first = planner.snapshot(limit=999)
    assert len(first['items']) == 200 and first['total'] == 225
    assert all(row['planned_order'] is None for row in first['items'])
    second = planner.snapshot(offset=200)
    assert len(second['items']) == 25 and not ({row['id'] for row in first['items']} & {row['id'] for row in second['items']})
    assert planner.progress()['percent'] == 0


def test_empty_progress_and_removed_plan_never_delete_media(planner):
    assert planner.progress()['percent'] is None
    row = add(planner, 'remove', approved=False)
    source = planner.store.data / 'owner.txt'; source.write_text('keep')
    planner.action([row['id']], 'remove')
    assert source.read_text() == 'keep' and planner.progress()['total'] == 0


def test_standalone_legacy_manual_batch_moves_to_planning_but_referenced_parent_does_not(planner):
    payload = {'tool': 'download_video', 'url': 'https://www.youtube.com/watch?v=legacy_single', 'options': {'format': 'mp4'},
               'destination': str(planner.store.data / 'exports'), 'batch_id': 'ordinary-ui-batch'}
    old = planner.store.add_job('download_video', 'Manual legacy', payload)
    parent = planner.store.add_job('download_video', 'Referenced parent', {**payload, 'url': 'https://www.youtube.com/watch?v=referenced'})
    child = planner.store.add_job('metadata', 'Dependent child', {'depends_on': parent['id']})
    assert planner.migrate_legacy() == 1
    assert planner.store.job(old['id'])['status'] == 'planned'
    assert planner.store.job(parent['id'])['status'] == 'queued'
    assert planner.store.job(child['id'])['payload']['depends_on'] == parent['id']
    row = planner.get_media('youtube:legacy_single')
    assert row['origin'] == 'manual_single'
    assert admit(planner)['id'] == old['id']
    assert planner.payload_for(row['id'])['resume_key'] == old['id']


def test_source_config_retry_change_affects_failed_offline_rows_but_auth_never_auto_retries(planner, monkeypatch):
    import forge.download_plans as implementation
    now = time.time()
    monkeypatch.setattr(implementation.time, 'time', lambda: now)
    for identity, retryable in [('authentication', False), ('network', True)]:
        add(planner, identity, source='A', extra={'auto_retry': False})
        job = admit(planner)
        planner.store.update_job(job['id'], status='failed', message='network timeout')
        with planner.store.lock, planner.store.db:
            meta = json.loads(planner.store.db.execute('SELECT value FROM job_meta WHERE id=?', (job['id'],)).fetchone()[0])
            meta.update(error_code='authentication' if not retryable else 'network', error_retryable=retryable)
            planner.store.db.execute('UPDATE job_meta SET value=? WHERE id=?', (json.dumps(meta), job['id']))
        planner.reconcile(force=True)
    planner.configure_source('A', auto_retry=True)
    now += 5000
    assert planner.retry_due() == 1
    assert planner.get_media('youtube:authentication')['state'] == 'failed'
    assert planner.get_media('youtube:network')['state'] == 'ready'


@pytest.mark.skipif(not shutil.which('ffprobe'), reason='Real FFprobe required')
def test_verification_respects_disk_lanes_and_reserves_next_free_lane(planner):
    from forge.queue import QueueManager
    manager = QueueManager(planner.store); planner.manager = manager
    settings = planner.store.settings(); settings['disk_workers'] = 1; planner.store.set('settings', settings)
    row = add(planner, 'verify', origin='manual_single'); job = admit(planner)
    output = tiny_wav(Path(job['payload']['destination']))
    busy = planner.store.add_job('metadata', 'Other disk work', {'options': {}})
    manager.active[busy['id']] = object()
    planner.store.update_job(job['id'], status='completed', outputs=[output])
    planner.reconcile(force=True)
    assert planner.verification_count == 0 and planner.verification_reservations == 1
    assert planner.get(row['id'])['state'] == 'scheduled'
    manager.active.clear()
    assert planner.wait_for_verifications()
    assert planner.get(row['id'])['state'] == 'completed'
    assert planner.verification_count == 0


def test_shared_media_retains_both_sources_and_stronger_pending_priority(planner):
    planner.configure_source('A', priority='low')
    planner.configure_source('B', priority='ultra')
    first = add(planner, 'shared', source='A', priority='low', approved=False)
    second = add(planner, 'shared', source='B', priority='ultra', approved=False)
    assert first['id'] == second['id']
    assert planner.get(first['id'])['channel_priority'] == 'ultra'
    assert planner.snapshot(source='A')['total'] == planner.snapshot(source='B')['total'] == 1
    planner.approve_channel_review('A')
    assert admit(planner)['label'] == 'shared' and admit(planner) is None


@pytest.mark.skipif(__import__('os').name == 'nt', reason='POSIX executable fixture; native Windows shutdown has separate acceptance')
def test_close_kills_only_owned_verification_process_and_finishes_promptly(planner, monkeypatch, tmp_path):
    import sys
    import forge.download_plans as implementation
    slow = tmp_path / 'bounded-probe'
    slow.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(60)\n')
    slow.chmod(0o700)
    monkeypatch.setattr(implementation, 'executable', lambda *args: str(slow))
    add(planner, 'closing', origin='manual_single')
    job = admit(planner); output = tiny_wav(Path(job['payload']['destination']))
    planner.store.update_job(job['id'], status='completed', outputs=[output])
    planner.reconcile(force=True)
    deadline = time.monotonic() + 3
    while not planner._verification_processes and time.monotonic() < deadline:
        time.sleep(.01)
    processes = list(planner._verification_processes)
    assert processes
    started = time.monotonic(); planner.close()
    assert time.monotonic() - started < 3
    assert all(process.poll() is not None for process in processes)
    assert planner.get(job['plan_id'])['state'] != 'completed'


def test_large_live_plan_batch_snapshot_is_frozen_without_loading_all_items_or_jobs(planner):
    for start in range(0, 10001, 1000):
        planner.upsert_many([{'media': {'id': 'large_' + str(index), 'url': 'https://www.youtube.com/watch?v=large_' + str(index)},
                              'source_id': 'Large source', 'approved': False}
                             for index in range(start, min(start + 1000, 10001))])
    batch = planner.frozen_batch([], name='Explicit whole-plan snapshot')
    assert batch['progress']['total'] == 10001 and batch['progress']['percent'] == 0
    assert len(planner.snapshot(limit=200)['items']) == 200
    assert planner.store.jobs() == []
    add(planner, 'later_discovery', approved=False)
    assert planner.progress(batch['id'])['total'] == 10001
    assert planner.progress()['total'] == 10002


def test_preview_simulates_turns_advanced_by_higher_item_priority_overrides(planner):
    planner.configure_source('A', priority='low'); planner.configure_source('B', priority='low')
    add(planner, 'A_ultra', source='A', priority='ultra', extra={'priority_override': 'ultra'})
    add(planner, 'A_low', source='A', priority='low')
    add(planner, 'B_low', source='B', priority='low')
    displayed = [item['media_id'] for item in planner.snapshot()['items']]
    assert displayed == ['A_ultra', 'B_low', 'A_low']
    assert [admit(planner)['label'] for _ in range(3)] == displayed


def test_playlist_group_anchor_and_custom_order_remain_consistent_across_admissions(planner):
    add(planner, 'P_old', origin='manual_playlist', source='P', position=1)
    add(planner, 'Q_one', origin='manual_playlist', source='Q', position=1)
    add(planner, 'P_later', origin='manual_playlist', source='P', position=2)
    displayed = [item['media_id'] for item in planner.snapshot()['items']]
    assert displayed == ['P_old', 'P_later', 'Q_one']
    assert [admit(planner)['label'] for _ in range(3)] == displayed
