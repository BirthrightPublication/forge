"""Real SQLite graph retries; source bytes are state-only owned fixtures."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge.core import Store
from forge.dubbing_store import DubbingStore
from forge.queue import QueueManager


@pytest.fixture
def queue(tmp_path):
    store = Store(tmp_path / 'data')
    manager = QueueManager(store)
    library = DubbingStore(store)
    manager.dubbing = SimpleNamespace(library=library)
    source = tmp_path / 'owned-state-source.mp4'
    source.write_bytes(b'Explicit state-only source; not claimed as playable or model inference.')
    stat = source.stat()
    project = library.save_project({'name': 'State retry fixture', 'source': {'path': str(source),
        'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}, 'segments': [{'id': 'one', 'start': 0, 'end': 1,
        'source_text': 'Saved original', 'text': 'Сохранённая правка', 'reviewed': True, 'manual_edit': True}]}, trusted=True)
    manager.fixture_project = project
    yield manager
    manager.stop(); store.close()


def chain(queue, statuses=('completed', 'failed', 'blocked', 'queued')):
    project = queue.fixture_project
    run = queue.dubbing.library.save_run({'id': 'saved-run', 'project_id': project['id'], 'snapshot': project,
        'frozen_inputs': {'voice_profiles': {}, 'glossary': {'entries': {'Alice': 'Алиса'}},
                          'model_revisions': {'opus-en-ru': 'a' * 40}}, 'jobs': [], 'status': 'failed',
        'review_approved': False, 'approved_hash': '', 'results': {'dubbing_transcribe': {'job_id': 'original-result', 'artifacts': ['kept-cache']}}})
    tools = ('dubbing_transcribe', 'dubbing_translate', 'dubbing_review', 'dubbing_voice')
    jobs = []; parent = ''
    for index, (tool, status) in enumerate(zip(tools, statuses), 1):
        payload = {'tool': tool, 'source': project['source'], 'source_stat': {key: project['source'][key] for key in ('size', 'mtime_ns')},
            'options': {'run_id': run['id'], 'project_id': project['id']},
            'dubbing': {'run_id': run['id'], 'project': copy.deepcopy(project), 'review_approved': False},
            'settings': queue.store.settings(), 'destination': str(queue.store.data / 'saved-cache' / tool),
            'depends_on': parent, 'batch_id': run['id'], 'step': index, 'steps': len(statuses)}
        job = queue.store.add_job(tool, tool, payload)
        queue.store.update_job(job['id'], status=status, message='Original ' + status)
        queue.store.update_meta(job['id'], dubbing_project_id=project['id'], dubbing_run_id=run['id'])
        jobs.append(queue.store.job(job['id'])); parent = job['id']
    run['jobs'] = [job['id'] for job in jobs]
    queue.dubbing.library.save_run(run)
    return jobs


def snapshot(queue):
    return {table: [list(row) for row in queue.store.db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
            for table in ('jobs', 'job_meta', 'dubbing_runs', 'dubbing_projects', 'dubbing_revisions', 'kv')}


def test_single_failed_stage_rebinds_pending_review_and_descendants_without_replaying_cache(queue):
    jobs = chain(queue)
    before = queue.dubbing.library.run('saved-run')
    historical = queue.store.job(jobs[1]['id'])
    completed = queue.store.job(jobs[0]['id'])
    retry = queue.retry(jobs[1]['id'])
    run = queue.dubbing.library.run('saved-run')
    assert run['jobs'] == [jobs[0]['id'], retry['id'], jobs[2]['id'], jobs[3]['id']]
    assert run['frozen_inputs'] == before['frozen_inputs'] and run['snapshot'] == before['snapshot']
    assert run['results'] == before['results'] and run['review_approved'] is False
    assert queue.store.job(jobs[0]['id']) == completed
    assert queue.store.job(jobs[1]['id']) == historical
    assert retry['payload']['depends_on'] == jobs[0]['id']
    assert retry['payload']['retry_of'] == jobs[1]['id'] and retry['payload']['attempt'] == 2
    review = queue.store.job(jobs[2]['id'])
    assert review['status'] == 'queued' and review['payload']['depends_on'] == retry['id']
    assert review['payload']['dubbing']['review_approved'] is False
    assert queue.store.job(jobs[3]['id'])['payload']['depends_on'] == jobs[2]['id']
    assert run['attempt_history'][0]['from'] == jobs[1]['id']


def test_batch_retry_skips_completed_and_preserves_queued_ids_then_retries_only_current_attempts(queue):
    jobs = chain(queue)
    retried = queue.retry_batch('saved-run')
    assert [job['tool'] for job in retried] == ['dubbing_translate', 'dubbing_review']
    run = queue.dubbing.library.run('saved-run')
    assert run['jobs'][0] == jobs[0]['id'] and run['jobs'][-1] == jobs[3]['id']
    assert queue.store.job(jobs[3]['id'])['payload']['depends_on'] == retried[1]['id']
    queue.store.update_job(retried[0]['id'], status='failed')
    # Pending retry/review stages keep their identity; old failed attempts are
    # history and never become a second parallel translation.
    again = queue.retry_batch('saved-run')
    assert len(again) == 1 and again[0]['tool'] == 'dubbing_translate'
    assert again[0]['payload']['attempt'] == 3
    assert queue.store.job(retried[1]['id'])['payload']['depends_on'] == again[0]['id']
    assert queue.dubbing.library.run('saved-run')['jobs'][0] == jobs[0]['id']
    with pytest.raises(ValueError, match='already replaced'):
        queue.retry(jobs[1]['id'])


def test_validate_only_does_not_write_retry_or_rebind_anything(queue):
    jobs = chain(queue); before = snapshot(queue)
    plan = queue._retry_selection([jobs[1]], validate_only=True)
    assert len(plan) == 1 and snapshot(queue) == before


def test_retried_parent_cannot_bypass_real_dubbing_review_gate(queue):
    from forge.dubbing_api import DubbingService
    service = DubbingService(SimpleNamespace(), queue.store, queue, lambda *args: None)
    jobs = chain(queue)
    parent = queue.retry(jobs[1]['id'])
    queue.store.update_job(parent['id'], status='completed')
    gate = queue.store.job(jobs[2]['id'])
    assert service.prepare_job(gate) is False
    assert queue.store.job(gate['id'])['status'] == 'queued'
    assert 'Review and approve' in queue.store.job(gate['id'])['message']
    assert service.library.run('saved-run')['review_approved'] is False


@pytest.mark.parametrize('problem', ['missing-parent', 'unfinished-parent', 'changed-source', 'active', 'active-descendant', 'feature', 'cycle'])
def test_invalid_graph_source_active_or_feature_rejects_without_partial_writes(queue, problem):
    jobs = chain(queue)
    if problem == 'missing-parent':
        payload = jobs[1]['payload']; payload['depends_on'] = 'missing-parent'
        queue.store.update_payload(jobs[1]['id'], payload)
    elif problem == 'unfinished-parent':
        queue.store.update_job(jobs[0]['id'], status='failed')
    elif problem == 'changed-source':
        Path(queue.fixture_project['source']['path']).write_bytes(b'Changed after import')
    elif problem == 'active':
        queue.active[jobs[1]['id']] = object()
    elif problem == 'active-descendant':
        queue.store.update_job(jobs[3]['id'], status='running')
    elif problem == 'feature':
        def deny(tool):
            if tool == 'dubbing_review': raise ValueError('Explicit feature rejection')
        queue.require_feature = deny
    elif problem == 'cycle':
        payload = jobs[1]['payload']; payload['depends_on'] = jobs[1]['id']
        queue.store.update_payload(jobs[1]['id'], payload)
    before = snapshot(queue)
    try:
        with pytest.raises(ValueError): queue.retry(jobs[1]['id'])
        assert snapshot(queue) == before
    finally:
        queue.active.clear()


def test_injected_run_write_failure_rolls_back_clones_edges_and_metadata(queue, monkeypatch):
    jobs = chain(queue); before = snapshot(queue)
    def fail(value): raise RuntimeError('Injected transaction failure')
    monkeypatch.setattr(queue.dubbing.library, 'save_run', fail)
    with pytest.raises(RuntimeError, match='transaction failure'):
        queue.retry(jobs[1]['id'])
    assert snapshot(queue) == before


def test_failed_child_can_retry_after_parent_attempt_was_replaced(queue):
    jobs = chain(queue, ('completed', 'failed', 'failed', 'queued'))
    parent = queue.retry(jobs[1]['id'])
    queue.store.update_job(parent['id'], status='completed')
    child = queue.retry(jobs[2]['id'])
    assert child['payload']['depends_on'] == parent['id']
    assert queue.store.job(jobs[3]['id'])['payload']['depends_on'] == child['id']


def test_install_chain_retry_rebinds_approved_quote_heads_without_redownloading_completed_components(queue):
    jobs = []; parent = ''; token = 'owned-install-plan'
    quote = {'id': token, 'components': [{'component': 'one'}, {'component': 'two'}, {'component': 'three'}],
             'plan_hash': 'a' * 64, 'jobs': []}
    for index, status in enumerate(('completed', 'failed', 'blocked')):
        job = queue.store.add_job('dubbing_install', 'Synthetic install state', {'tool': 'dubbing_install',
            'options': {'component': str(index), 'consent': True}, 'dubbing_install_plan': copy.deepcopy(quote),
            'settings': queue.store.settings(), 'depends_on': parent, 'batch_id': token, 'step': index + 1, 'steps': 3})
        queue.store.update_job(job['id'], status=status)
        jobs.append(queue.store.job(job['id'])); parent = job['id']
    quote['jobs'] = [job['id'] for job in jobs]
    queue.store.set('dubbing_setup_plans', {token: quote})
    retried = queue.retry_batch(token)
    assert len(retried) == 2
    saved = queue.store.get('dubbing_setup_plans')[token]
    assert saved['jobs'] == [jobs[0]['id'], retried[0]['id'], retried[1]['id']]
    assert saved['plan_hash'] == quote['plan_hash'] and saved['components'] == quote['components']
    assert retried[0]['payload']['depends_on'] == jobs[0]['id']
    assert retried[1]['payload']['depends_on'] == retried[0]['id']
    assert queue.store.job(jobs[1]['id'])['status'] == 'failed'


def test_standalone_component_retry_and_mixed_tools_have_no_corrupting_generic_fallback(queue):
    job = queue.store.add_job('dubbing_model_test', 'Verify owned state fixture', {'tool': 'dubbing_model_test', 'options': {'component': 'opus-en-ru'}})
    queue.store.update_job(job['id'], status='failed')
    retry = queue.retry(job['id'])
    assert retry['payload']['retry_of'] == job['id']
    generic = queue.store.add_job('qr', 'Generic existing history', {'tool': 'qr', 'options': {'text': 'fixture'}})
    queue.store.update_job(generic['id'], status='failed')
    queue.store.update_job(retry['id'], status='failed')
    before = snapshot(queue)
    with pytest.raises(ValueError, match='separately'):
        queue.bulk_action([retry['id'], generic['id']], 'retry')
    assert snapshot(queue) == before


def test_completed_dubbing_stage_is_not_reexecuted_and_completed_batch_is_noop(queue):
    jobs = chain(queue, ('completed', 'completed', 'completed', 'completed'))
    before = snapshot(queue)
    with pytest.raises(ValueError, match='completed stages'):
        queue.retry(jobs[0]['id'])
    assert queue.retry_batch('saved-run') == [] and snapshot(queue) == before
