"""Cross-tool queue, immutable dependencies, and real V0.5 media chains."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from forge import resources
from forge.queue import public_job
from test_forge import client, live, tone, wait_job
from test_black_scripting import definition, preview, register
from test_queue_v4 import add, settings, simulated_queue, synthetic_metrics


def snapshot(path):
    stat = path.stat()
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def terminal(client, id, timeout=35):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        job = client.app.state.store.job(id)
        if job['status'] in {'completed', 'failed', 'cancelled', 'interrupted', 'blocked'}:
            return job
        time.sleep(.08)
    raise AssertionError('Queue chain timed out')


@pytest.mark.parametrize('tool', sorted(resources.MEDIA_ANALYSIS))
def test_inspection_dependency_passes_media_without_report_copy(client, tmp_path, tool):
    store, queue = client.app.state.store, client.app.state.manager
    source = tone(tmp_path / 'original.wav', seconds=.1)
    selected = store.register(source)
    expected = snapshot(source)
    parent = add(store, tool, source=selected, source_stat=expected,
                 original_source=selected, original_source_stat=expected)
    report = tmp_path / 'PRIVATE-report.json'
    report.write_text('{"private": "report is not media"}')
    store.update_job(parent['id'], status='completed', outputs=[{'path': str(report)}])
    child = add(store, 'metadata', depends_on=parent['id'])
    assert queue.prepare_dependency(child)
    payload = store.job(child['id'])['payload']
    assert payload['source']['path'] == str(source)
    assert payload['source_stat'] == expected
    assert payload['original_source'] == selected
    assert payload['original_source_stat'] == expected
    assert source.read_bytes()[:4] == b'RIFF'
    source.write_bytes(b'changed input')
    assert not queue.prepare_dependency(store.job(child['id']))
    assert store.job(child['id'])['status'] == 'blocked'


def test_transformed_output_change_blocks_next_step(client, tmp_path):
    store, queue = client.app.state.store, client.app.state.manager
    output = tone(tmp_path / 'published.wav', seconds=.1)
    parent = add(store, 'metadata')
    store.update_job(parent['id'], status='completed', outputs=[{'path': str(output), 'source_stat': snapshot(output)}])
    child = add(store, 'privacy_export', depends_on=parent['id'])
    output.write_bytes(b'changed publication')
    assert not queue.prepare_dependency(child)
    assert store.job(child['id'])['status'] == 'blocked'


def test_encoder_budget_is_distinct_and_shared_with_model_admission(simulated_queue):
    store, queue, metrics, launched = simulated_queue
    settings(store, workers=5, gpu_workers=5, cpu_workers=5, disk_workers=5)
    encoder = add(store, 'watermark', {'mode': 'video', 'encoder': 'nvenc'})
    duplicate = add(store, 'watermark', {'mode': 'both', 'encoder': 'auto'})
    inference = add(store, 'transcribe', {'model': 'tiny'})
    metrics['gpu']['devices'][0]['free_mb'] = 3000
    queue.dispatch_once()
    assert [job['id'] for job in launched] == [encoder['id'], inference['id']]
    assert 'encoder lane' in store.job(duplicate['id'])['message']
    state = queue.status()
    assert state['gpu_budget']['encoder_memory_mb'] == 768
    assert state['gpu_budget']['reserved_for_active_mb'] == 768 + 700
    assert resources.required_resources(encoder) == {'cpu', 'gpu', 'disk'}
    assert resources.required_resources(add(store, 'watermark', {'mode': 'audio'})) == {'cpu', 'disk'}
    assert resources.required_resources(add(store, 'privacy_verify')) == {'cpu', 'disk'}


def test_new_tool_public_queue_summary_omits_private_content(client):
    job = add(client.app.state.store, 'watermark', {'text': 'PRIVATE-WATERMARK', 'image_path': '/PRIVATE-ASSET', 'encoder': 'cpu', 'mode': 'video'})
    result = public_job(job)
    assert result['options_summary'] == {'encoder': 'cpu', 'mode': 'video'}
    assert 'PRIVATE' not in json.dumps(result)


def test_black_preview_new_ops_reports_transition(client, tmp_path):
    source = tone(tmp_path / 'voice.wav', seconds=.15)
    script = definition({'tool': 'tracking_audit', 'options': {'reports': False}},
                        {'tool': 'privacy_export', 'options': {'reports': False, 'format': 'flac'}},
                        {'tool': 'privacy_verify', 'options': {'reports': False}})
    body, plan = preview(client, script, [register(client, source)])
    steps = plan['sources'][0]['steps']
    assert steps[0]['passthrough'] is True
    assert steps[1]['output_formats'] == ['.flac']
    assert steps[2]['input_format'] == '.flac'
    result = client.post('/api/black-scripting/run', json=body)
    assert result.status_code == 200, result.text
    jobs = result.json()['jobs']
    final = client.app.state.store.job(jobs[-1]['id'])['payload']
    assert final['chain_operations'] == ['tracking_audit', 'privacy_export']
    assert final['original_source']['path'] == str(source)


def test_real_audit_clean_export_verify_chain_and_retry_without_reports(live, tmp_path):
    source = tone(tmp_path / 'original.wav', seconds=.3)
    original = source.read_bytes()
    script = definition({'tool': 'tracking_audit', 'options': {'reports': False}},
                        {'tool': 'metadata', 'options': {'audit_report': False}},
                        {'tool': 'privacy_export', 'options': {'reports': False}},
                        {'tool': 'privacy_verify', 'options': {'reports': False}})
    body, _ = preview(live, script, [register(live, source)])
    response = live.post('/api/black-scripting/run', json=body)
    assert response.status_code == 200, response.text
    jobs = response.json()['jobs']
    final = terminal(live, jobs[-1]['id'])
    assert final['status'] == 'completed', final['message']
    completed = [live.app.state.store.job(job['id']) for job in jobs]
    assert completed[0]['outputs'] == [] and completed[-1]['outputs'] == []
    assert all(job['status'] == 'completed' for job in completed)
    assert final['payload']['source']['path'] == completed[-2]['outputs'][0]['path']
    assert final['payload']['original_source']['path'] == str(source)
    assert source.read_bytes() == original
    assert not list(tmp_path.glob('*PRIVATE*'))
    assert all('analysis_result' not in job['payload'] for job in completed)
    retry = live.post('/api/batches/' + response.json()['batch_id'] + '/retry', json={})
    assert retry.status_code == 200, retry.text
    rerun = retry.json()['jobs']
    repeated = terminal(live, rerun[-1]['id'])
    assert repeated['status'] == 'completed', repeated
    assert repeated['payload']['original_source']['path'] == str(source)
    assert repeated['payload']['source']['path'] != final['payload']['source']['path']
    assert source.read_bytes() == original


def test_real_reports_reference_collision_resolved_media_filename(live, tmp_path):
    source = tone(tmp_path / 'report-source.wav', seconds=.2)
    request = {'tool': 'privacy_export', 'sources': [register(live, source)],
               'options': {'reports': True}, 'output': {'mode': 'source'}}
    first = live.post('/api/jobs', json=request)
    assert first.status_code == 200, first.text
    initial = terminal(live, first.json()['jobs'][0]['id'])
    assert initial['status'] == 'completed', initial['message']
    second = live.post('/api/jobs', json=request)
    assert second.status_code == 200, second.text
    job = terminal(live, second.json()['jobs'][0]['id'])
    assert job['status'] == 'completed', job['message']
    media = next(Path(output['path']) for output in job['outputs'] if Path(output['path']).suffix == '.wav')
    report = next(Path(output['path']) for output in job['outputs'] if Path(output['path']).suffix == '.json')
    payload = json.loads(report.read_text())
    assert '(1)' in media.name
    assert payload['final_output_location'] == str(media)
    assert payload['output']['filename'] == media.name
    html = next(Path(output['path']) for output in job['outputs'] if Path(output['path']).suffix == '.html')
    assert media.name in html.read_text()
    assert '.work/' not in json.dumps(payload)


def test_black_preview_token_detects_changed_watermark_asset(client, tmp_path):
    source = tone(tmp_path / 'source.wav', seconds=.4)
    clip = tone(tmp_path / 'watermark.wav', seconds=.05)
    script = definition({'tool': 'watermark', 'options': {'mode': 'audio', 'format': 'wav', 'audio_clip_path': str(clip)}})
    body, plan = preview(client, script, [register(client, source)])
    assert plan['sources'][0]['steps'][0]['output_formats'] == ['.wav']
    tone(clip, seconds=.1)
    response = client.post('/api/black-scripting/run', json=body)
    assert response.status_code == 400, response.text
    assert 'Preview' in response.json()['detail']
    assert not client.app.state.store.jobs()


def test_real_watermark_verify_chain_preserves_original_and_asset(live, tmp_path):
    source = tone(tmp_path / 'source.wav', seconds=.4)
    clip = tone(tmp_path / 'marker.wav', seconds=.05)
    original, asset = source.read_bytes(), clip.read_bytes()
    script = definition({'tool': 'watermark', 'options': {'mode': 'audio', 'format': 'wav', 'audio_clip_path': str(clip), 'audio_volume': .6}},
                        {'tool': 'privacy_verify', 'options': {'reports': True, 'compare_audio': True}})
    body, _ = preview(live, script, [register(live, source)])
    response = live.post('/api/black-scripting/run', json=body)
    assert response.status_code == 200, response.text
    first, second = response.json()['jobs']
    result = terminal(live, second['id'])
    assert result['status'] == 'completed', result['message']
    transformed = live.app.state.store.job(first['id'])
    assert Path(transformed['outputs'][0]['path']).read_bytes() != original
    assert result['payload']['source']['path'] == transformed['outputs'][0]['path']
    assert result['payload']['original_source']['path'] == str(source)
    assert result['payload']['chain_operations'] == ['watermark']
    assert source.read_bytes() == original and clip.read_bytes() == asset


def test_real_analysis_worker_cancel_then_retry(client, tmp_path):
    store, queue = client.app.state.store, client.app.state.manager
    source = tone(tmp_path / 'cancel.wav', seconds=1)
    response = client.post('/api/jobs', json={'tool': 'tracking_audit', 'sources': [register(client, source)],
                           'options': {'reports': False}, 'output': {'mode': 'source'}})
    assert response.status_code == 200, response.text
    id = response.json()['jobs'][0]['id']
    with queue.lock:
        queue.launch(store.job(id))
        queue.cancel(id)
    assert terminal(client, id)['status'] == 'cancelled'
    for thread in queue.threads:
        thread.join(timeout=5)
    retry = queue.retry(id)
    queue.dispatch_once()
    result = terminal(client, retry['id'])
    assert result['status'] == 'completed', result['message']
    assert result['outputs'] == []
