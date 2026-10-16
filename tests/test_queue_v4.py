"""Real worker integration plus explicitly mocked hardware/Windows admission tests."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from forge import queue_api, resources
from forge.core import Store
from forge.queue import QueueManager
from test_forge import client, submit_qr, tone, wait_job
from test_v2 import authorize, register


def synthetic_metrics(free=24000, available=True):
    """Simulation only: these values are never claimed as physical GPU results."""
    return {'cpu_percent': 10, 'cpu_count': 8, 'memory_available_mb': 16000,
            'memory_total_mb': 32000, 'disk_free_mb': 100000,
            'gpu': {'available': available, 'source': 'mocked-test-metrics',
                    'devices': [{'index': 0, 'name': 'SIMULATED', 'total_mb': 24000, 'free_mb': free}] if available else []}}


@pytest.fixture
def simulated_queue(tmp_path, monkeypatch):
    store = Store(tmp_path / 'data')
    settings = store.settings()
    settings.update(concurrency_mode='manual', workers=5, cpu_workers=5,
                    gpu_workers=5, download_workers=5, disk_workers=5)
    store.set('settings', settings)
    queue = QueueManager(store)
    metrics = synthetic_metrics()
    monkeypatch.setattr(queue.monitor, 'sample', lambda data: metrics)
    launched = []
    def launch(job):
        queue.active[job['id']] = object()
        store.update_job(job['id'], status='running')
        launched.append(job)
    monkeypatch.setattr(queue, 'launch', launch)
    yield store, queue, metrics, launched
    queue.active.clear()
    store.close()


def add(store, tool='qr', options=None, **payload):
    return store.add_job(tool, tool, {'tool': tool, 'options': options or {}, **payload})


def settings(store, **values):
    state = store.settings()
    state.update(values)
    store.set('settings', state)


def test_simulated_five_worker_cap_including_legacy_values(simulated_queue):
    store, queue, _, launched = simulated_queue
    settings(store, workers=99, cpu_workers=99)
    for _ in range(12):
        add(store)
    queue.dispatch_once()
    assert len(launched) == 5
    assert len(store.jobs(status='queued')) == 7
    assert queue.status()['effective_workers'] == 5


def test_simulated_independent_resource_limits_allow_five_jobs(simulated_queue):
    store, queue, _, launched = simulated_queue
    settings(store, cpu_workers=2, gpu_workers=1, download_workers=1, disk_workers=2)
    for tool, options in [('qr', {}), ('qr', {}), ('qr', {}),
                          ('transcribe', {'model': 'small'}), ('download_audio', {}), ('metadata', {})]:
        add(store, tool, options)
    queue.dispatch_once()
    assert len(launched) == 5
    assert queue.status()['usage'] == {'cpu': 2, 'gpu': 1, 'network': 1, 'disk': 2}


def test_simulated_gpu_same_cycle_budgets_and_duplicate_models(simulated_queue):
    store, queue, metrics, launched = simulated_queue
    metrics['gpu']['devices'][0]['free_mb'] = 10000
    first = add(store, 'transcribe', {'model': 'large-v3'})
    duplicate = add(store, 'transcribe', {'model': 'large-v3'})
    medium = add(store, 'transcribe', {'model': 'medium'})
    cpu = add(store)
    queue.dispatch_once()
    assert [job['id'] for job in launched] == [first['id'], cpu['id']]
    assert 'duplicate model' in store.job(duplicate['id'])['message']
    assert 'GPU memory' in store.job(medium['id'])['message']
    assert queue.status()['gpu_budget']['kind'] == 'conservative_estimate'


def test_simulated_gpu_pressure_delays_new_work_without_killing(simulated_queue):
    store, queue, metrics, launched = simulated_queue
    first = add(store, 'transcribe', {'model': 'medium'})
    queue.dispatch_once()
    metrics['gpu']['devices'][0]['free_mb'] = 1000
    waiting = add(store, 'transcribe', {'model': 'small'})
    network = add(store, 'download_audio')
    queue.dispatch_once()
    assert store.job(first['id'])['status'] == 'running'
    assert store.job(waiting['id'])['status'] == 'queued'
    assert store.job(network['id'])['status'] == 'running'
    metrics['gpu']['devices'][0]['free_mb'] = 24000
    queue.dispatch_once()
    assert store.job(waiting['id'])['status'] == 'running'


def test_simulated_unavailable_gpu_telemetry_serializes_inference(simulated_queue):
    store, queue, metrics, launched = simulated_queue
    metrics['gpu'] = synthetic_metrics(available=False)['gpu']
    add(store, 'transcribe', {'model': 'tiny'})
    add(store, 'transcribe', {'model': 'base'})
    add(store)
    queue.dispatch_once()
    assert [job['tool'] for job in launched] == ['transcribe', 'qr']
    assert queue.status()['metrics']['gpu']['devices'] == []


def test_simulated_auto_pressure_reduces_admission_without_stopping_active(simulated_queue):
    store, queue, metrics, launched = simulated_queue
    settings(store, concurrency_mode='auto')
    for _ in range(5):
        add(store)
    queue.dispatch_once()
    metrics['memory_available_mb'] = 100
    waiting = add(store)
    queue.dispatch_once()
    assert len(launched) == 5
    assert queue.status()['effective_workers'] == 1
    assert all(store.job(job['id'])['status'] == 'running' for job in launched)
    assert store.job(waiting['id'])['status'] == 'queued'


def test_simulated_model_cache_download_exclusion(simulated_queue):
    store, queue, _, launched = simulated_queue
    add(store, 'model_download', {'model': 'tiny'})
    add(store, 'transcribe', {'model': 'small', 'device': 'cpu'})
    add(store)
    queue.dispatch_once()
    assert [job['tool'] for job in launched] == ['model_download', 'qr']


def test_simulated_rank_dependencies_and_missing_parent_do_not_stall_queue(simulated_queue):
    store, queue, _, launched = simulated_queue
    settings(store, workers=1)
    low = add(store)
    parent = add(store, 'retune')
    child = add(store, 'metadata', depends_on=parent['id'])
    missing = add(store, 'metadata', depends_on='missing')
    queue.bulk_action([child['id'], missing['id'], parent['id']], 'first')
    queue.dispatch_once()
    assert launched[0]['id'] == parent['id']
    assert store.job(child['id'])['status'] == 'queued'
    assert store.job(missing['id'])['status'] == 'blocked'
    assert store.job(low['id'])['status'] == 'queued'


def test_dependency_source_is_registered_once_while_waiting_for_resource(simulated_queue, tmp_path):
    store, queue, _, launched = simulated_queue
    settings(store, workers=1)
    running = add(store)
    queue.dispatch_once()
    path = tmp_path / 'published.wav'; path.write_bytes(b'parent media')
    parent = add(store, 'retune')
    store.update_job(parent['id'], status='completed', outputs=[{'path': str(path)}])
    child = add(store, 'metadata', depends_on=parent['id'])
    for _ in range(3):
        queue.dispatch_once()
    assert store.job(child['id'])['status'] == 'queued'
    assert store.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 1
    queue.active.pop(running['id'])
    store.update_job(running['id'], status='completed')
    queue.dispatch_once()
    assert launched[-1]['payload']['source']['path'] == str(path)


def test_simulated_auto_mode_respects_explicit_ceiling(simulated_queue):
    store, queue, _, launched = simulated_queue
    settings(store, concurrency_mode='auto', workers=2)
    for _ in range(5):
        add(store)
    queue.dispatch_once()
    assert len(launched) == 2


def test_simulated_output_drive_pressure_uses_destination_volume(simulated_queue, tmp_path, monkeypatch):
    store, queue, metrics, launched = simulated_queue
    output = tmp_path / 'exports'; output.mkdir()
    job = add(store, 'retune', destination=str(output))
    calls = []
    def disk_usage(path):
        calls.append(path)
        return SimpleNamespace(free=10 * 1048576)
    monkeypatch.setattr(resources.psutil, 'disk_usage', disk_usage)
    queue.dispatch_once()
    assert not launched and calls == [str(output)]
    assert 'output drive' in store.job(job['id'])['message']
    assert metrics['disk_free_mb'] == 100000


def test_bulk_order_preserves_selected_relative_order(client):
    store, queue = client.app.state.store, client.app.state.manager
    jobs = [add(store) for _ in range(5)]
    a, b, c, d, e = [job['id'] for job in jobs]
    queue.bulk_action([c, b], 'up')
    assert [j['id'] for j in store.jobs(status='queued')] == [b, c, a, d, e]
    queue.bulk_action([c, b], 'last')
    assert [j['id'] for j in store.jobs(status='queued')] == [a, d, e, b, c]
    queue.bulk_action([b, c], 'up')
    assert [j['id'] for j in store.jobs(status='queued')] == [a, d, b, c, e]
    queue.bulk_action([b, c], 'down')
    assert [j['id'] for j in store.jobs(status='queued')] == [a, d, e, b, c]


def test_bulk_remove_references_and_preserves_all_files(client, tmp_path):
    store = client.app.state.store
    source = tmp_path / 'original.wav'; source.write_bytes(b'original')
    output = tmp_path / 'finished.wav'; output.write_bytes(b'finished')
    parent = add(store, 'retune', source={'path': str(source)})
    child = add(store, 'metadata', depends_on=parent['id'])
    store.update_job(parent['id'], status='completed', outputs=[{'path': str(output)}])
    store.update_job(child['id'], status='failed')
    response = client.post('/api/queue/actions', json={'ids': [parent['id']], 'action': 'remove'})
    assert response.status_code == 400
    assert len(store.jobs()) == 2
    response = client.post('/api/queue/actions', json={'ids': [child['id'], parent['id'], child['id']], 'action': 'remove'})
    assert response.status_code == 200, response.text
    assert not store.jobs()
    assert store.db.execute('SELECT COUNT(*) FROM job_meta').fetchone()[0] == 0
    assert source.read_bytes() == b'original' and output.read_bytes() == b'finished'


def test_bulk_rejects_live_terminal_race_and_invalid_selection_atomically(client):
    store, queue = client.app.state.store, client.app.state.manager
    queued = add(store)
    terminal = add(store)
    store.update_job(terminal['id'], status='completed')
    queue.active[terminal['id']] = object()
    try:
        assert client.post('/api/queue/actions', json={'ids': [terminal['id']], 'action': 'remove'}).status_code == 400
    finally:
        queue.active.clear()
    for ids in ([terminal['id'], queued['id']], [terminal['id'], 'missing']):
        assert client.post('/api/queue/actions', json={'ids': ids, 'action': 'remove'}).status_code == 400
    assert len(store.jobs()) == 2
    assert client.post('/api/queue/actions', json={'ids': [queued['id'], terminal['id']], 'action': 'cancel'}).status_code == 400
    assert store.job(queued['id'])['status'] == 'queued'


def test_bulk_retry_validates_sources_and_preserves_selected_dependencies(client, tmp_path):
    store = client.app.state.store
    missing = add(store, source={'path': str(tmp_path / 'missing.wav')})
    parent = add(store, 'retune')
    child = add(store, 'metadata', depends_on=parent['id'])
    for job in (missing, parent, child):
        store.update_job(job['id'], status='failed')
    assert client.post('/api/queue/actions', json={'ids': [parent['id'], missing['id']], 'action': 'retry'}).status_code == 400
    assert len(store.jobs()) == 3
    result = client.post('/api/queue/actions', json={'ids': [child['id'], parent['id']], 'action': 'retry'})
    assert result.status_code == 200, result.text
    first, second = result.json()['jobs']
    assert second['depends_on'] == first['id']
    assert first['batch_id'] == second['batch_id']
    assert store.job(child['id'])['payload']['depends_on'] == parent['id']


def test_queue_views_counts_and_metadata_never_expose_sensitive_options(client):
    store = client.app.state.store
    job = add(store, 'transcribe', {'model': 'tiny', 'device': 'cpu', 'formats': ['txt'], 'vocabulary': 'PRIVATE-VOCAB', 'cookies': 'PRIVATE-COOKIE', 'text': 'PRIVATE-QR'})
    for state in ('completed', 'failed', 'blocked', 'interrupted', 'cancelled'):
        historical = add(store)
        store.update_job(historical['id'], status=state)
    state = client.get('/api/queue/status').json()
    assert state['failed_count'] == 3 and state['queued_count'] == 1
    active = client.get('/api/queue/jobs').json()
    assert [j['id'] for j in active['jobs']] == [job['id']]
    assert client.get('/api/queue/jobs?view=failed').json()['total'] == 3
    assert client.get('/api/queue/jobs?view=history').json()['total'] == 2
    detail = client.get('/api/queue/jobs/' + job['id'])
    assert detail.json()['options_summary']['model'] == 'tiny'
    assert detail.json()['progress_measured'] is False
    assert 'payload' not in detail.json()
    assert not any(secret in detail.text for secret in ('PRIVATE-VOCAB', 'PRIVATE-COOKIE', 'PRIVATE-QR'))


def test_state_retains_all_failed_and_active_records_beyond_history_window(client):
    store = client.app.state.store
    statuses = ['failed', 'blocked', 'interrupted', 'queued', 'running', 'finalizing', 'cancelling']
    rows = [(f'old-{index}', 'qr', status, status, 0, 'test', '{}', '[]', '[]', float(index), float(index))
            for index, status in enumerate(statuses)]
    rows += [(f'completed-{index}', 'qr', 'old completed', 'completed', 100, 'test', '{}', '[]', '[]', 100. + index, 100. + index)
             for index in range(510)]
    with store.lock, store.db:
        store.db.executemany('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)', rows)
    response = client.get('/api/state')
    assert response.status_code == 200, response.text
    state = response.json()
    ids = {job['id'] for job in state['jobs']}
    assert {f'old-{index}' for index in range(len(statuses))} <= ids
    assert len(state['jobs']) == 507 and 'completed-0' not in ids
    assert state['scheduler']['failed_count'] == 3
    assert sum(job['status'] in {'failed', 'blocked', 'interrupted'} for job in state['jobs']) == 3


@pytest.mark.parametrize('route', ['single', 'legacy_bulk', 'bulk', 'batch'])
def test_retry_routes_honor_injected_entitlement_provider(client, route):
    class QRProvider:
        provider_id = 'test.queue-retry'
        mode = 'test'
        def allows(self, feature_id):
            return feature_id == 'birthright.forge.qr'
    store = client.app.state.store
    client.app.state.entitlements.provider = QRProvider()
    allowed = add(store, 'qr', batch_id='allowed')
    denied = add(store, 'retune', batch_id='denied')
    for job in (allowed, denied):
        store.update_job(job['id'], status='failed')
    if route == 'single':
        url, body = '/api/jobs/' + denied['id'] + '/retry', {}
    elif route == 'batch':
        url, body = '/api/batches/denied/retry', {}
    else:
        url = '/api/jobs/batch-action' if route == 'legacy_bulk' else '/api/queue/actions'
        body = {'ids': [allowed['id'], denied['id']], 'action': 'retry'}
    response = client.post(url, json=body)
    assert response.status_code == 403, response.text
    assert len(store.jobs()) == 2


def test_queue_new_routes_obey_existing_auth_and_csrf(client):
    csrf = authorize(client).json()['csrf']
    assert client.get('/api/queue/status').status_code == 200
    job = add(client.app.state.store)
    body = {'ids': [job['id']], 'action': 'cancel'}
    assert client.post('/api/queue/actions', json=body).status_code == 403
    assert client.post('/api/queue/actions', json=body, headers={'X-Forge-CSRF': csrf}).status_code == 200
    client.cookies.clear()
    assert client.get('/api/queue/status').status_code == 401


def test_simulated_nvidia_query_is_fixed_and_measurements_are_parsed(monkeypatch):
    calls = []
    monkeypatch.delenv('CUDA_VISIBLE_DEVICES', raising=False)
    monkeypatch.setattr(resources.shutil, 'which', lambda name: '/trusted/nvidia-smi')
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout='0, GPU-mocked-uuid, SIMULATED NVIDIA, 24000, 18000\n')
    monkeypatch.setattr(resources.subprocess, 'run', run)
    result = resources.ResourceMonitor.nvidia()
    assert result['available'] and result['source'] == 'nvidia-smi'
    assert result['devices'][0]['free_mb'] == 18000
    assert calls[0][0] == ['/trusted/nvidia-smi', '--query-gpu=index,uuid,name,memory.total,memory.free', '--format=csv,noheader,nounits']
    assert 'shell' not in calls[0][1] and calls[0][1]['timeout'] == 2
    monkeypatch.setattr(resources.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='0, GPU-mocked-uuid, broken, N/A, N/A'))
    assert resources.ResourceMonitor.nvidia()['available'] is False


def test_simulated_multi_gpu_requires_unambiguous_uuid_mapping(monkeypatch):
    monkeypatch.delenv('CUDA_VISIBLE_DEVICES', raising=False)
    monkeypatch.setattr(resources.shutil, 'which', lambda name: '/trusted/nvidia-smi')
    monkeypatch.setattr(resources.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        stdout='0, GPU-mock-one, SIMULATED ONE, 24000, 18000\n1, GPU-mock-two, SIMULATED TWO, 8000, 4000\n'))
    assert resources.ResourceMonitor.nvidia()['available'] is False
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', 'GPU-mock-two')
    result = resources.ResourceMonitor.nvidia()
    assert result['available'] is True and result['admission_device_index'] == 1


def test_simulated_windows_native_open_only_dispatches_registered_media(tmp_path, monkeypatch):
    path = tmp_path / 'safe & ordinary.png'; path.write_bytes(b'fixture')
    job = {'outputs': [{'path': str(path)}], 'payload': {'destination': str(tmp_path)}}
    calls = []
    monkeypatch.setattr(queue_api, 'windows_supported', lambda: True)
    monkeypatch.setattr(queue_api.os, 'startfile', lambda *args: calls.append(args), raising=False)
    assert queue_api.open_media_output(job, 0) == {'opened': True}
    assert calls == [(str(path), 'open')]
    for suffix in ('.exe', '.BAT', '.ps1', '.lnk', '.url', '.html', '.svg'):
        bad = tmp_path / ('unsafe' + suffix); bad.write_bytes(b'fixture')
        with pytest.raises(ValueError):
            queue_api.open_media_output({'outputs': [{'path': str(bad)}], 'payload': job['payload']}, 0)
    with pytest.raises(ValueError):
        queue_api.open_media_output(job, -1)
    outside = tmp_path.parent / 'outside.png'; outside.write_bytes(b'fixture')
    linked = tmp_path / 'linked.png'; linked.symlink_to(outside)
    with pytest.raises(ValueError):
        queue_api.open_media_output({'outputs': [{'path': str(linked)}], 'payload': job['payload']}, 0)
    assert len(calls) == 1


def test_simulated_windows_opens_real_plain_transcripts_and_rejects_extension_links(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(queue_api, 'windows_supported', lambda: True)
    monkeypatch.setattr(queue_api.os, 'startfile', lambda *args: calls.append(args), raising=False)
    for name, contents in [('transcript.txt', 'Local transcript text'),
                           ('transcript.json', '{"segments": []}'),
                           ('transcript.srt', '1\n00:00:00,000 --> 00:00:01,000\nHello\n'),
                           ('transcript.vtt', 'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n')]:
        path = tmp_path / name
        path.write_text(contents, encoding='utf-8')
        job = {'outputs': [{'path': str(path)}], 'payload': {'destination': str(tmp_path)}}
        assert queue_api.open_media_output(job, 0) == {'opened': True}
        assert calls[-1] == (str(path), 'open')
        assert path.read_text(encoding='utf-8') == contents
    executable = tmp_path / 'program.exe'; executable.write_bytes(b'not executed')
    linked = tmp_path / 'transcript-link.txt'; linked.symlink_to(executable)
    with pytest.raises(ValueError, match='Native Open supports'):
        queue_api.open_media_output({'outputs': [{'path': str(linked)}], 'payload': {'destination': str(tmp_path)}}, 0)
    assert len(calls) == 4


def test_real_simultaneous_qr_and_media_workers(client, tmp_path, monkeypatch):
    """Actual Python workers + FFmpeg + QR generation; no hardware mocks."""
    store, queue = client.app.state.store, client.app.state.manager
    settings(store, concurrency_mode='manual', workers=5, cpu_workers=5, disk_workers=2)
    source = tone(tmp_path / 'voice.wav', seconds=3)
    original = source.read_bytes()
    response = client.post('/api/jobs', json={'tool': 'retune', 'sources': [register(client, source)],
                           'options': {'format': 'wav'}, 'output': {'mode': 'custom', 'directory': str(tmp_path / 'out')}})
    assert response.status_code == 200, response.text
    media = response.json()['jobs'][0]
    jobs = [media] + [submit_qr(client, tmp_path / 'out', name=f'Concurrent {index}') for index in range(4)]
    overlap = []
    real_launch = queue.launch
    def observe(job):
        real_launch(job)
        overlap.append([store.job(id)['tool'] for id in queue.active])
    monkeypatch.setattr(queue, 'launch', observe)
    queue.dispatch_once()
    completed = [wait_job(client, job['id']) for job in jobs]
    assert all(job['status'] == 'completed' for job in completed), completed
    assert any('retune' in tools and 'qr' in tools for tools in overlap)
    assert max(map(len, overlap)) >= 2
    assert source.read_bytes() == original
    assert all(Path(job['outputs'][0]['path']).is_file() for job in completed)


def test_real_worker_failure_retry_and_cancellation(client, tmp_path):
    store, queue = client.app.state.store, client.app.state.manager
    source = tone(tmp_path / 'voice.wav', seconds=.1)
    response = client.post('/api/jobs', json={'tool': 'retune', 'sources': [register(client, source)],
                           'options': {'format': 'wav'}, 'output': {'mode': 'custom', 'directory': str(tmp_path / 'out')}})
    assert response.status_code == 200, response.text
    failed_id = response.json()['jobs'][0]['id']
    tone(source, seconds=.2)
    queue.dispatch_once()
    assert wait_job(client, failed_id)['status'] == 'failed'
    queue.threads[-1].join(timeout=5)
    retried = client.post('/api/jobs/' + failed_id + '/retry', json={})
    assert retried.status_code == 200, retried.text
    queue.dispatch_once()
    assert wait_job(client, retried.json()['id'])['status'] == 'completed'
    qr = submit_qr(client, tmp_path / 'out', name='Cancel then retry')
    with queue.lock:
        queue.launch(store.job(qr['id']))
        queue.cancel(qr['id'])
    assert wait_job(client, qr['id'])['status'] == 'cancelled'
    for thread in queue.threads:
        thread.join(timeout=5)
    retry = queue.retry(qr['id'])
    queue.dispatch_once()
    assert wait_job(client, retry['id'])['status'] == 'completed'


def test_restart_preserves_queue_rank_and_pauses_interrupted_work(tmp_path):
    data = tmp_path / 'restart-data'
    store = Store(data)
    running = add(store)
    first, last = add(store), add(store)
    QueueManager(store).reorder(last['id'], 'first')
    store.update_job(running['id'], status='running')
    store.close()
    recovered = Store(data)
    try:
        assert recovered.job(running['id'])['status'] == 'interrupted'
        assert recovered.settings()['queue_paused'] is True
        assert [job['id'] for job in recovered.jobs(status='queued')] == [last['id'], first['id']]
        retried = QueueManager(recovered).retry(running['id'])
        assert retried['status'] == 'queued' and retried['id'] != running['id']
    finally:
        recovered.close()
