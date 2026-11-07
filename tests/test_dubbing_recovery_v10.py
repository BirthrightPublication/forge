"""Real SQLite/filesystem crash recovery, with explicit local state fixtures.

The WAV source is generated PCM silence; translations are synthetic Russian
fixture text. These tests verify durable result safety, never neural inference
or external publishing.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import wave

import pytest

from forge.core import Store
from forge.dubbing_api import DubbingService
from forge import dubbing_recovery as recovery
from forge.dubbing_store import segment_fingerprint, stage_fingerprint


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_manifest(bundle):
    bundle['manifest'].write_text(json.dumps(bundle['result'], ensure_ascii=False), encoding='utf-8')


def service_for(store):
    manager = SimpleNamespace(lock=threading.RLock(), active={})
    return DubbingService(SimpleNamespace(), store, manager, lambda *args: None)


@pytest.fixture
def completed(tmp_path):
    store = Store(tmp_path / 'data')
    store.set('settings', {**store.settings(), 'queue_paused': False})
    service = service_for(store)
    source = tmp_path / 'owned-original.wav'
    with wave.open(str(source), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b'\0\0' * 8000)
    stat = source.stat()
    source_record = {'id': 'owned-source', 'path': str(source), 'name': source.name,
                     'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    project = service.library.save_project({'name': 'Durable local result fixture', 'source': source_record,
        'segments': [{'id': 'segment1', 'start': 0, 'end': 1, 'source_text': 'Owned English state fixture',
                      'text': '', 'reviewed': False}]}, trusted=True)
    run = service.library.save_run({'project_id': project['id'], 'snapshot': project,
                                   'status': 'running', 'results': {}, 'artifacts': []})
    destination = store.data / 'dubbing' / 'projects' / project['id'] / 'runs' / run['id'] / 'dubbing_translate'
    destination.mkdir(parents=True)
    translated = destination / 'russian-translation.json'
    translated.write_text(json.dumps({'text': 'Русский синтетический пример'}, ensure_ascii=False), encoding='utf-8')
    report = destination / 'dubbing-stage.json'
    report.write_text('{"scope":"Local crash recovery state fixture; no inference"}', encoding='utf-8')
    artifact = {'path': str(translated), 'kind': 'translation', 'sha256': digest(translated),
                'size': translated.stat().st_size}
    payload = {'tool': 'dubbing_translate', 'source': source_record,
        'source_stat': {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns},
        'destination': str(destination), 'options': {'run_id': run['id'], 'project_id': project['id']},
        'dubbing': {'project': project, 'run_id': run['id']}, 'settings': store.settings()}
    job = store.add_job('dubbing_translate', 'Durable local translation state fixture', payload)
    run['jobs'] = [job['id']]
    run = service.library.save_run(run)
    store.update_job(job['id'], status='interrupted', message='Service stopped before result collection.')
    result = {'complete': True, 'warnings': [],
        'outputs': [{'path': str(path), 'name': path.name, 'size': path.stat().st_size,
                     'sha256': digest(path)} for path in (translated, report)],
        'dubbing_result': {'schema': 1, 'project_id': project['id'], 'run_id': run['id'],
            'stage': 'dubbing_translate', 'input_revision': project['revision'],
            'stage_settings_hash': stage_fingerprint(project, 'dubbing_translate'), 'elapsed_seconds': 1.25,
            'segment_updates': [{'id': 'segment1', 'expected_input_hash': segment_fingerprint(project['segments'][0]),
                                 'text': 'Русский синтетический пример', 'reviewed': False}],
            'project_updates': {'translation_model': 'explicit-synthetic-state-fixture'}, 'artifacts': [artifact]}}
    manifest = store.data / 'work' / job['id'] / 'result.json'
    manifest.parent.mkdir()
    bundle = {'service': service, 'store': store, 'job': store.job(job['id']), 'project': project, 'run': run,
              'destination': destination, 'translated': translated, 'report': report, 'source': source,
              'source_digest': digest(source), 'manifest': manifest, 'result': result}
    write_manifest(bundle)
    yield bundle
    store.close()


def assert_untouched(bundle, *, status='interrupted'):
    service = bundle['service']
    current = service.library.project(bundle['project']['id'])
    assert current['revision'] == bundle['project']['revision']
    assert current['segments'][0]['text'] == ''
    assert current['artifacts'] == []
    assert service.library.run(bundle['run']['id']).get('results', {}) == {}
    assert service.store.job(bundle['job']['id'])['status'] == status
    assert digest(bundle['source']) == bundle['source_digest']
    assert service.store.get(recovery.RECEIPT_PREFIX + bundle['job']['id']) is None


def test_real_restart_recovers_verified_outputs_and_keeps_automatic_safety_pause(completed):
    old = completed['store']
    old.update_job(completed['job']['id'], status='running')
    old.close()
    reopened = Store(old.data)
    try:
        service = service_for(reopened)
        assert reopened.job(completed['job']['id'])['status'] == 'interrupted'
        assert reopened.settings()['queue_paused'] is True
        assert reopened.get('queue_restart_pause')['automatic'] is True
        report = recovery.recover_completed_results(service)
        assert report['recovered'] == [completed['job']['id']]
        assert not report['invalid']
        saved = service.library.project(completed['project']['id'])
        assert saved['segments'][0]['text'] == 'Русский синтетический пример'
        assert saved['artifacts'][0]['sha256'] == digest(completed['translated'])
        job = reopened.job(completed['job']['id'])
        assert job['status'] == 'completed' and job['progress'] == 100
        assert job['execution_seconds'] == 1.25
        assert len(job['outputs']) == 2 and all(item['sha256'] for item in job['outputs'])
        assert reopened.settings()['queue_paused'] is True
        assert digest(completed['source']) == completed['source_digest']
        assert completed['manifest'].exists()
    finally:
        reopened.close()


def test_recovery_receipt_prevents_reapplying_or_duplicating_saved_artifacts(completed):
    service = completed['service']
    first = recovery.recover_completed_results(service)
    saved = service.library.project(completed['project']['id'])
    run = service.library.run(completed['run']['id'])
    assert first['recovered'] == [completed['job']['id']]
    second = recovery.recover_completed_results(service)
    assert second['already_applied'] == [completed['job']['id']]
    assert not second['recovered']
    assert service.library.project(saved['id'])['revision'] == saved['revision']
    assert len(service.library.project(saved['id'])['artifacts']) == 1
    assert service.library.run(run['id'])['artifacts'] == run['artifacts']


def test_collector_applied_result_before_crash_is_not_applied_twice(completed):
    service = completed['service']
    service.library.apply_result(service.store.job(completed['job']['id']), completed['result'])
    saved = service.library.project(completed['project']['id'])
    run = service.library.run(completed['run']['id'])
    report = recovery.recover_completed_results(service)
    assert report['already_applied'] == [completed['job']['id']]
    assert not report['recovered']
    assert service.store.job(completed['job']['id'])['status'] == 'completed'
    assert service.library.project(saved['id'])['revision'] == saved['revision']
    assert service.library.run(run['id'])['artifacts'] == run['artifacts']


@pytest.mark.parametrize('status', ['failed', 'interrupted'])
def test_changed_output_digest_preserves_failed_or_interrupted_records(completed, status):
    completed['store'].update_job(completed['job']['id'], status=status)
    path = completed['translated']
    raw = path.read_bytes()
    path.write_bytes(b'X' + raw[1:])
    result = recovery.recover_completed_results(completed['service'])
    assert 'SHA-256' in result['invalid'][0]['reason']
    assert_untouched(completed, status=status)


@pytest.mark.parametrize('location', ['output', 'output_parent', 'destination', 'manifest', 'manifest_parent'])
def test_links_in_any_evidence_path_are_rejected(completed, tmp_path, location):
    if location == 'output':
        target = tmp_path / 'external-translation.json'
        completed['translated'].rename(target)
        completed['translated'].symlink_to(target)
    elif location in {'output_parent', 'destination'}:
        original = completed['destination']
        target = tmp_path / 'external-destination'
        original.rename(target)
        original.symlink_to(target, target_is_directory=True)
    elif location == 'manifest':
        target = tmp_path / 'external-result.json'
        completed['manifest'].rename(target)
        completed['manifest'].symlink_to(target)
    else:
        parent = completed['manifest'].parent
        target = tmp_path / 'external-work'
        parent.rename(target)
        parent.symlink_to(target, target_is_directory=True)
    result = recovery.recover_completed_results(completed['service'])
    assert result['invalid'] and not result['recovered']
    assert 'links' in result['invalid'][0]['reason']
    assert_untouched(completed)


@pytest.mark.parametrize('complete', [False, 'true', 1, None])
def test_only_explicit_boolean_complete_result_can_recover(completed, complete):
    completed['result']['complete'] = complete
    write_manifest(completed)
    result = recovery.recover_completed_results(completed['service'])
    assert 'explicitly complete' in result['invalid'][0]['reason']
    assert_untouched(completed)


@pytest.mark.parametrize('intent', ['cancelled', 'cancelling', 'cancel_intent', 'queue_pause', 'job_pause'])
def test_explicit_user_cancel_or_pause_is_preserved(completed, intent):
    store, identity = completed['store'], completed['job']['id']
    if intent in {'cancelled', 'cancelling'}:
        store.update_job(identity, status=intent)
    elif intent == 'cancel_intent':
        store.update_meta(identity, cancel_intent='user')
    elif intent == 'queue_pause':
        store.set('settings', {**store.settings(), 'queue_paused': True})
        store.set('queue_restart_pause', {'automatic': False})
    else:
        store.db.execute('UPDATE job_meta SET value=? WHERE id=?', ('{"pause_intent":"user"}', identity))
        store.db.commit()
    result = recovery.recover_completed_results(completed['service'])
    assert result['skipped'] and not result['recovered'] and not result['invalid']
    assert_untouched(completed, status=intent if intent in {'cancelled', 'cancelling'} else 'interrupted')


@pytest.mark.parametrize('tool', ['youtube_upload', 'dubbing_prepare_upload', 'dubbing_install', 'dubbing_model_test'])
def test_network_install_and_publishing_stages_are_never_replayed(completed, tool, monkeypatch):
    store = completed['store']
    store.db.execute('UPDATE jobs SET tool=? WHERE id=?', (tool, completed['job']['id']))
    store.db.commit()
    monkeypatch.setattr(recovery, '_verify', lambda *args: pytest.fail('Excluded stage must not even read its durable result.'))
    report = recovery.recover_completed_results(completed['service'])
    assert not any(report.values())
    assert_untouched(completed)


def test_hashing_runs_without_queue_or_database_locks_and_user_pause_wins_race(completed, monkeypatch):
    real = recovery._read_verified
    service, store = completed['service'], completed['store']

    def verify(path, **kwargs):
        assert not service.manager.lock._is_owned()
        assert not store.lock._is_owned()
        result = real(path, **kwargs)
        if Path(path) == completed['translated']:
            store.set('settings', {**store.settings(), 'queue_paused': True})
            store.set('queue_restart_pause', {'automatic': False})
        return result

    monkeypatch.setattr(recovery, '_read_verified', verify)
    result = recovery.recover_completed_results(service)
    assert result['skipped'] and not result['invalid']
    assert_untouched(completed)


def test_queue_payload_change_during_hashing_is_not_applied(completed, monkeypatch):
    real = recovery._read_verified
    store = completed['store']

    def verify(path, **kwargs):
        result = real(path, **kwargs)
        if Path(path) == completed['translated']:
            payload = store.job(completed['job']['id'])['payload']
            payload['options']['user_changed_while_verifying'] = True
            store.update_payload(completed['job']['id'], payload)
        return result

    monkeypatch.setattr(recovery, '_read_verified', verify)
    result = recovery.recover_completed_results(completed['service'])
    assert 'changed during recovery' in result['skipped'][0]['reason']
    assert_untouched(completed)


def test_application_and_queue_completion_are_one_rollback_boundary(completed, monkeypatch):
    service = completed['service']
    real = service.library.apply_result

    def fail_after_metadata(job, result):
        real(job, result)
        raise ValueError('Injected local metadata commit failure')

    monkeypatch.setattr(service.library, 'apply_result', fail_after_metadata)
    result = recovery.recover_completed_results(service)
    assert 'Injected local metadata' in result['invalid'][0]['reason']
    assert_untouched(completed)
    assert completed['translated'].exists() and completed['report'].exists()


def test_manual_script_edit_before_recovery_is_kept_as_stale_work(completed):
    service = completed['service']
    current = copy.deepcopy(completed['project'])
    current['segments'][0]['text'] = 'Сохранённая ручная правка'
    current = service.library.save_project(current, current['revision'])
    result = recovery.recover_completed_results(service)
    assert result['recovered'] == [completed['job']['id']]
    final = service.library.project(current['id'])
    assert final['segments'][0]['text'] == 'Сохранённая ручная правка'
    assert final['segments'][0]['manual_edit'] is True
    assert final['revision'] == current['revision']
    assert final['artifacts'][0]['stale'] is True
    run = service.library.run(completed['run']['id'])
    assert run['results']['dubbing_translate']['stale_segments'] == ['segment1']


@pytest.mark.parametrize('invalid', ['outside', 'traversal', 'digest_conflict', 'unhashed_report',
                                     'metadata_path', 'wrong_project', 'wrong_stage', 'publishing_result'])
def test_unowned_or_inconsistent_evidence_fails_closed(completed, tmp_path, invalid):
    result = completed['result']
    if invalid == 'outside':
        outside = tmp_path / 'outside.json'
        outside.write_bytes(completed['translated'].read_bytes())
        result['outputs'][0]['path'] = str(outside)
        result['dubbing_result']['artifacts'][0]['path'] = str(outside)
    elif invalid == 'traversal':
        path = str(completed['destination'] / '..' / completed['destination'].name / completed['translated'].name)
        result['outputs'][0]['path'] = path
        result['dubbing_result']['artifacts'][0]['path'] = path
    elif invalid == 'digest_conflict':
        result['outputs'][0]['sha256'] = 'f' * 64
    elif invalid == 'unhashed_report':
        result['outputs'][1].pop('sha256')
    elif invalid == 'metadata_path':
        result['dubbing_result']['project_updates']['raw_transcript'] = {'path': str(tmp_path / 'unverified.json')}
    elif invalid == 'wrong_project':
        result['dubbing_result']['project_id'] = 'another-project'
    elif invalid == 'wrong_stage':
        result['dubbing_result']['stage'] = 'dubbing_voice'
    else:
        result['publishing_result'] = {'status': 'complete'}
    write_manifest(completed)
    report = recovery.recover_completed_results(completed['service'])
    assert report['invalid'] and not report['recovered']
    assert_untouched(completed)


def test_external_voice_cache_descriptor_does_not_broaden_recovery_boundary(completed, tmp_path):
    cached = tmp_path / 'data' / 'dubbing' / 'cache' / 'voice.wav'
    cached.parent.mkdir(parents=True)
    cached.write_bytes(completed['source'].read_bytes())
    completed['result']['dubbing_result']['artifacts'].append({
        'kind': 'voice_segment', 'path': str(cached), 'size': cached.stat().st_size, 'sha256': digest(cached)})
    write_manifest(completed)
    result = recovery.recover_completed_results(completed['service'])
    assert 'owned published destination output' in result['invalid'][0]['reason']
    assert cached.exists()
    assert_untouched(completed)


def test_startup_recovery_refuses_to_race_an_active_worker(completed):
    completed['service'].manager.active['another-job'] = object()
    with pytest.raises(RuntimeError, match='before queue dispatch'):
        recovery.recover_completed_results(completed['service'])
    assert_untouched(completed)


def test_superseded_failed_attempt_is_never_replayed_over_its_new_retry(completed):
    service, store = completed['service'], completed['store']
    replacement = copy.deepcopy(completed['job']['payload'])
    replacement['retry_of'] = completed['job']['id']
    latest = store.add_job('dubbing_translate', 'Explicit newer retry', replacement)
    run = service.library.run(completed['run']['id'])
    run['jobs'] = [latest['id']]
    service.library.save_run(run)
    result = recovery.recover_completed_results(service)
    assert any('newer retry' in item['reason'] for item in result['skipped'])
    assert_untouched(completed)
    assert store.job(latest['id'])['status'] == 'queued'


def test_recovery_requires_saved_workflow_to_still_own_this_attempt(completed):
    service = completed['service']
    run = service.library.run(completed['run']['id'])
    run['jobs'] = ['another-attempt']
    service.library.save_run(run)
    result = recovery.recover_completed_results(service)
    assert 'different current attempt' in result['skipped'][0]['reason']
    assert_untouched(completed)


@pytest.mark.parametrize('status', ['paused', 'cancelled'])
def test_explicit_workflow_pause_or_cancel_is_preserved(completed, status):
    service = completed['service']
    run = service.library.run(completed['run']['id'])
    run['status'] = status
    service.library.save_run(run)
    result = recovery.recover_completed_results(service)
    assert 'workflow pause or cancellation' in result['skipped'][0]['reason']
    assert_untouched(completed)


def test_saved_artifact_hash_can_verify_legacy_output_record_without_its_own_hash(completed):
    completed['result']['outputs'][0].pop('sha256')
    write_manifest(completed)
    report = recovery.recover_completed_results(completed['service'])
    assert report['recovered'] == [completed['job']['id']]
    output = completed['store'].job(completed['job']['id'])['outputs'][0]
    assert output['sha256'] == digest(completed['translated'])


def test_real_local_metadata_worker_manifest_recovers_without_running_it_twice(completed):
    """Actual worker subprocess/publication; metadata has no inference inputs."""
    service, store = completed['service'], completed['store']
    project = copy.deepcopy(completed['project'])
    project['options']['publishing']['title'] = 'Проверенные русские метаданные'
    project = service.library.save_project(project, project['revision'])
    run = service.library.run(completed['run']['id'])
    run['snapshot'] = project
    service.library.save_run(run)
    job = store.job(completed['job']['id'])
    payload = job['payload']
    payload.update(tool='dubbing_metadata', data=str(store.data))
    payload['dubbing']['project'] = project
    store.update_payload(job['id'], payload)
    store.db.execute('UPDATE jobs SET tool=? WHERE id=?', ('dubbing_metadata', job['id']))
    store.db.commit()
    worker = subprocess.run([sys.executable, '-m', 'forge.worker'], input=json.dumps(payload),
        capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert worker.returncode == 0, worker.stdout + worker.stderr
    actual = json.loads(completed['manifest'].read_text(encoding='utf-8'))
    assert actual['complete'] is True
    assert all(item.get('sha256') for item in actual['outputs'])
    assert actual['dubbing_result']['stage'] == 'dubbing_metadata'
    report = recovery.recover_completed_results(service)
    assert not report['invalid'], report
    assert report['recovered'] == [job['id']]
    saved = service.library.project(project['id'])
    assert saved['metadata']['title'] == 'Проверенные русские метаданные'
    assert store.job(job['id'])['status'] == 'completed'
    assert digest(completed['source']) == completed['source_digest']
    again = recovery.recover_completed_results(service)
    assert again['already_applied'] == [job['id']]
    assert service.library.project(project['id'])['revision'] == saved['revision']
