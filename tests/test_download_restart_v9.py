"""Persisted crash-state fixtures; real worker termination is tested separately.

These cases reopen actual SQLite/app instances and never launch public downloads.
"""
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from forge.app import create_app


def application(data):
    return create_app(data, 'synthetic-restart-token', 19889, workers=False)


def close(app):
    app.state.manager.stop()
    app.state.store.close()


def planned(app, *, identity='restart0001', approved=True):
    root = app.state.store.data / 'exports'
    root.mkdir(exist_ok=True)
    return app.state.download_planner.upsert({'id': identity, 'platform': 'youtube',
        'url': 'https://www.youtube.com/watch?v=' + identity, 'title': 'Synthetic restart fixture'},
        origin='manual_single', approved=approved, destination=str(root),
        payload={'tool': 'download_video', 'url': 'https://www.youtube.com/watch?v=' + identity,
                 'options': {'format': 'mp4'}, 'destination': str(root), 'settings': app.state.store.settings()})


@pytest.mark.parametrize('explicit_pause,unrelated_interruption,expected_pause',
                         [(False, False, False), (True, False, True), (False, True, True)])
def test_restart_reapproves_only_owned_plans_and_preserves_user_or_other_work_safety_pause(tmp_path, explicit_pause, unrelated_interruption, expected_pause):
    data = tmp_path / 'data'
    first = application(data)
    item = planned(first)
    review = planned(first, identity='review00001', approved=False)
    job = first.state.download_planner.admit_one(lambda candidate: True)
    stable_key = job['payload']['resume_key']
    first.state.store.update_job(job['id'], status='running')
    if explicit_pause:
        settings = first.state.store.settings(); settings['queue_paused'] = True
        first.state.store.set('settings', settings)
        first.state.store.set('queue_pause_explicit', True)
    if unrelated_interruption:
        other = first.state.store.add_job('qr', 'Separate interrupted QR fixture',
            {'tool': 'qr', 'options': {}, 'destination': str(data / 'exports')})
        first.state.store.update_job(other['id'], status='running')
    close(first)
    reopened = application(data)
    try:
        restored = reopened.state.download_planner.get(item['id'])
        assert restored['state'] == 'ready' and restored['approved'] and not restored['paused']
        assert reopened.state.download_planner.get(review['id'])['state'] == 'review'
        assert reopened.state.store.settings()['queue_paused'] is expected_pause
        # No processing record is allocated by merely recovering an approval.
        assert not reopened.state.store.jobs(status='queued')
        next_job = reopened.state.download_planner.admit_one(lambda candidate: True)
        assert next_job['id'] != job['id']
        assert next_job['payload']['resume_key'] == stable_key
        assert next_job['payload']['resume_from_job'] == job['id']
        assert reopened.state.store.job(job['id'])['status'] == 'interrupted'
    finally:
        close(reopened)


def test_cancel_intent_survives_crash_without_resuming_cancelled_plan(tmp_path):
    data = tmp_path / 'data'
    first = application(data)
    item = planned(first)
    job = first.state.download_planner.admit_one(lambda candidate: True)
    first.state.store.update_job(job['id'], status='cancelling')
    first.state.store.update_meta(job['id'], cancel_intent='user')
    close(first)
    reopened = application(data)
    try:
        saved = reopened.state.download_planner.get(item['id'])
        assert saved['state'] == 'interrupted' and saved['paused']
        assert reopened.state.download_planner.admit_one(lambda candidate: True) is None
        assert reopened.state.store.settings()['queue_paused'] is False
    finally:
        close(reopened)


def test_controlled_shutdown_transient_pause_does_not_disable_approved_plan_recovery(tmp_path):
    data = tmp_path / 'data'
    first = application(data)
    item = planned(first)
    with TestClient(first, base_url='http://127.0.0.1:19889', headers={'X-Forge-Token': 'synthetic-restart-token'}) as client:
        assert client.post('/api/shutdown').status_code == 200
        assert first.state.store.settings()['queue_paused'] is True
    first.state.store.close()
    reopened = application(data)
    try:
        assert reopened.state.store.settings()['queue_paused'] is False
        assert reopened.state.download_planner.get(item['id'])['state'] == 'ready'
        assert not reopened.state.store.jobs()
    finally:
        close(reopened)


def test_queue_retry_returns_to_plan_and_keeps_original_processing_history(tmp_path):
    app = application(tmp_path / 'data')
    item = planned(app)
    original = app.state.download_planner.admit_one(lambda candidate: True)
    app.state.store.update_job(original['id'], status='failed', message='Synthetic interrupted transfer')
    app.state.download_planner.reconcile(force=True)
    with TestClient(app, base_url='http://127.0.0.1:19889', headers={'X-Forge-Token': 'synthetic-restart-token'}) as client:
        response = client.post('/api/jobs/' + original['id'] + '/retry', json={})
        assert response.status_code == 200, response.text
        assert response.json()['plan']['id'] == item['id'] and response.json()['queued'] is False
        assert app.state.store.job(original['id'])['status'] == 'failed'
        assert len(app.state.store.jobs()) == 1 and not app.state.store.jobs(status='queued')
        assert app.state.download_planner.get(item['id'])['state'] == 'ready'
    app.state.store.close()
