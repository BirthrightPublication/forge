"""Local readiness contracts with explicit synthetic receipts, not GPU proof."""
import copy
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from forge import dubbing_acceptance as acceptance, dubbing_models, worker
from forge.core import Store, json_write


def identities(device='cpu'):
    return {name: {'model_revision': str(index + 1) * 40, 'runtime_revision': 'dubbing-core-' + str(index + 1) * 16,
                   'device': 'cpu' if name == 'whisper-tiny' else device}
            for index, name in enumerate(acceptance.COMPONENTS)}


class SyntheticRunner:
    """Never executes a model; these receipts exercise readiness trust checks."""
    def __init__(self):
        self.source = {'source_commit': 'a' * 40, 'source_manifest_sha256': 'b' * 64,
                       'source_files_sha256': {'forge/dubbing_models.py': 'c' * 64}}
        self.device = 'cpu'
        self.calls = []

    def source_identity(self, **kwargs):
        return copy.deepcopy(self.source)

    def installed_identities(self, data, device):
        if device != self.device:
            raise ValueError('Install the approved device profile first.')
        return identities(device)

    def run_acceptance(self, data, output, **kwargs):
        self.calls.append(kwargs)
        assert kwargs['approve_install'] is False
        output.mkdir()
        sample = output / 'Russian-acceptance.wav'
        sample.write_bytes(b'Owned fixture sample, not actual speech.')
        report = synthetic_report(self, kwargs['device'], sample.read_bytes())
        return report


def synthetic_report(runner, device, sample=b'Owned fixture sample, not actual speech.'):
    rows = {}
    selected = identities(device)
    for component, value in selected.items():
        gpu = value['device'] == 'cuda'
        inference = {**value, 'actual_device': value['device'], 'inference_tested': True,
                     'physical_gpu_tested': gpu, 'timings_verified': True}
        inference.pop('device')
        if gpu:
            inference.update(gpu_name='Owned synthetic GPU receipt', gpu_capability=[12, 0], peak_vram_bytes=10)
        if component.startswith('qwen'):
            inference['media'] = {'sha256': hashlib.sha256(sample).hexdigest(), 'duration': 1.0}
        if component == 'opus-en-ru':
            inference['segments'] = [{'id': 'owned-' + str(index), 'source_text': text,
                                      'text': 'Синтетический черновик ' + str(index)}
                                     for index, text in enumerate(acceptance.TRANSLATION_INPUTS, start=1)]
        rows[component] = {'verification': {'status': 'passed', 'files_verified': 1}, 'inference': inference}
    return {**copy.deepcopy(runner.source), 'schema': 1, 'kind': 'dubbing_model_smoke', 'status': 'passed',
            'inference_substituted': False, 'private_inputs_used': False, 'native_windows_execution': False,
            'physical_gpu_tested': device == 'cuda', 'components': rows, 'installed_identities': selected}


@pytest.fixture
def service(tmp_path, monkeypatch):
    runner = SyntheticRunner()
    monkeypatch.setattr(acceptance, '_runner', lambda: runner)
    monkeypatch.setattr(dubbing_models, 'catalog', lambda data: [{'id': name, 'ready': True, **row}
                       for name, row in identities(runner.device).items()])
    data = tmp_path / 'data'; store = Store(data)
    manager = SimpleNamespace(lock=threading.RLock())
    def simple_job(tool, options, extra):
        job = store.add_job(tool, 'Owned local verification fixture', {'tool': tool, 'options': options, **extra})
        return {'job': job, 'jobs': [job]}
    app = SimpleNamespace(state=SimpleNamespace(dubbing=SimpleNamespace(simple_job=simple_job)))
    value = acceptance.LocalAcceptance(app, store, manager)
    yield value, runner, store
    store.close()


def finish(service, runner, store, tmp_path, device='cpu'):
    runner.device = device
    result = service.run({'device': device, 'consent': True})
    job = store.job(result['job']['id'])
    path = store.data / 'reports' / job['id']; path.mkdir(parents=True)
    files, _, _ = acceptance.run_worker({**job['payload'], 'settings': {}}, path, store.data)
    store.update_job(job['id'], status='completed', outputs=[{'path': str(file)} for file in files])
    return store.job(job['id'])


@pytest.mark.parametrize('body', [{}, {'device': 'cpu', 'consent': False}, {'device': 'auto', 'consent': True},
                                  {'device': 'cpu', 'consent': True, 'approve_install': True}])
def test_no_local_test_is_queued_without_the_exact_owner_consent(service, body):
    value, _, store = service
    with pytest.raises(ValueError):
        value.run(body)
    assert store.jobs() == []


def test_model_queue_freezes_source_device_and_runtime_and_deduplicates(service):
    value, runner, store = service
    first = value.run({'device': 'cpu', 'consent': True})
    second = value.run({'device': 'cpu', 'consent': True})
    assert len(store.jobs()) == 1 and second['already_queued']
    payload = store.job(first['job']['id'])['payload']
    assert payload['dubbing_acceptance_run']['source'] == runner.source
    assert payload['dubbing_acceptance_run']['models'] == identities()
    assert payload['options']['action'] == 'test' and payload['options']['consent'] is True
    with pytest.raises(ValueError, match='device profile'):
        value.run({'device': 'cuda', 'consent': True})


def test_dirty_git_source_refuses_queue_before_model_access_and_clean_candidate_is_accepted(service, monkeypatch):
    value, runner, store = service
    runner.source.update(source_kind='git', source_checkout_clean=False)
    accesses = []
    def installed(data, device):
        accesses.append(device)
        return identities(device)
    monkeypatch.setattr(runner, 'installed_identities', installed)
    with pytest.raises(ValueError, match='Preserve the edits'):
        value.run({'device': 'cpu', 'consent': True})
    assert accesses == [] and store.jobs() == []
    runner.source['source_checkout_clean'] = True
    result = value.run({'device': 'cpu', 'consent': True})
    assert accesses == ['cpu'] and len(store.jobs()) == 1
    assert store.job(result['job']['id'])['payload']['dubbing_acceptance_run']['source']['source_checkout_clean'] is True


def test_gpu_discovery_or_missing_receipts_never_make_acceptance_pass(service, monkeypatch):
    value, runner, store = service
    monkeypatch.setattr(dubbing_models, 'hardware', lambda *_: {'gpus': [{'name': 'RTX 5080'}], 'cuda_available': True})
    rows = {row['id']: row['status'] for row in value.state()['checks']}
    assert rows['installed_models'] == 'passed'
    assert rows['actual_inference'] == rows['physical_gpu'] == rows['native_windows'] == 'pending'
    assert value.state()['satisfies_release_acceptance'] is False
    store.set('v10_youtube_private_acceptance', {'status': 'protocol_fixture_verified', 'live_google': True,
              'source_identity': runner.source, 'private_visibility_verified': True, 'channel_verified': True,
              'captions_verified': True, 'explicit_private_review': True})
    assert next(row for row in value.state()['checks'] if row['id'] == 'youtube_private')['status'] == 'pending'


def test_current_model_receipt_does_not_imply_gpu_native_or_owner_listening(service, tmp_path):
    value, runner, store = service
    job = finish(value, runner, store, tmp_path)
    rows = {row['id']: row['status'] for row in value.state()['checks']}
    assert rows['actual_inference'] == 'passed'
    assert rows['physical_gpu'] == rows['native_windows'] == rows['human_listening'] == 'pending'
    assert value.state()['sample_url'].startswith('/api/jobs/' + job['id'])
    state = value.listen({'job_id': job['id'], 'approve': True})
    assert next(row for row in state['checks'] if row['id'] == 'human_listening')['status'] == 'passed'
    assert next(row for row in state['checks'] if row['id'] == 'human_translation')['status'] == 'pending'
    runner.source['source_files_sha256']['forge/dubbing_models.py'] = 'd' * 64
    assert value.state()['receipt_current'] is False
    assert next(row for row in value.state()['checks'] if row['id'] == 'human_listening')['status'] == 'pending'


def test_translation_quality_requires_its_own_exact_report_bound_owner_review(service, tmp_path):
    value, runner, store = service
    job = finish(value, runner, store, tmp_path)
    state = value.state()
    assert state['translation_review_available'] is True
    assert [row['english'] for row in state['translation_pairs']] == list(acceptance.TRANSLATION_INPUTS)
    assert next(row for row in state['checks'] if row['id'] == 'human_translation')['status'] == 'pending'
    with pytest.raises(ValueError, match='Current verified'):
        value.translation_review({'job_id': 'another-job', 'approve': True})
    with pytest.raises(ValueError, match='Explicitly'):
        value.translation_review({'job_id': job['id'], 'approve': True, 'text': 'browser replacement'})
    state = value.translation_review({'job_id': job['id'], 'approve': True})
    assert next(row for row in state['checks'] if row['id'] == 'human_translation')['status'] == 'passed'
    assert next(row for row in state['checks'] if row['id'] == 'human_listening')['status'] == 'pending'
    assert value.export()['translation_review']['confirmed'] is True
    assert 'Синтетический черновик' not in json.dumps(value.export(), ensure_ascii=False)
    state = value.translation_review({'job_id': job['id'], 'approve': False})
    assert next(row for row in state['checks'] if row['id'] == 'human_translation')['status'] == 'pending'
    value.translation_review({'job_id': job['id'], 'approve': True})
    runner.source['source_commit'] = 'f' * 40
    assert value.state()['translation_pairs'] == []
    assert value.export()['translation_review']['confirmed'] is False
    with pytest.raises(ValueError, match='Current verified'):
        value.translation_review({'job_id': job['id'], 'approve': True})


@pytest.mark.parametrize('fault', ['private_source', 'wrong_id', 'oversized_output', 'control_character'])
def test_translation_review_only_exposes_bounded_exact_synthetic_pairs(service, fault):
    _, runner, _ = service
    report = synthetic_report(runner, 'cpu')
    row = report['components']['opus-en-ru']['inference']['segments'][0]
    if fault == 'private_source': row['source_text'] = 'A private manuscript is never displayed.'
    elif fault == 'wrong_id': row['id'] = 'private-project-segment'
    elif fault == 'oversized_output': row['text'] = 'я' * 2001
    else: row['text'] = 'Русский\x00черновик'
    receipt = {'source': runner.source, 'models': identities(), 'device': 'cpu', 'report': report}
    assert acceptance._translation_pairs(receipt) == []


@pytest.mark.parametrize('fault', ['summary_only', 'wrong_model', 'wrong_runtime', 'no_vram', 'cpu_claims_gpu', 'missing_timings'])
def test_per_component_real_device_facts_are_required_even_with_summary_pass(service, fault):
    _, runner, _ = service
    device = 'cpu' if fault == 'cpu_claims_gpu' else 'cuda'
    report = synthetic_report(runner, device)
    if fault == 'summary_only': report['components'] = {}
    elif fault == 'wrong_model': report['components']['opus-en-ru']['inference']['model_revision'] = 'f' * 40
    elif fault == 'wrong_runtime': report['components']['qwen3-tts-0.6b-base']['inference']['runtime_revision'] = 'other'
    elif fault == 'no_vram': report['components']['qwen3-tts-0.6b-base']['inference']['peak_vram_bytes'] = 0
    elif fault == 'cpu_claims_gpu': report['components']['opus-en-ru']['inference']['physical_gpu_tested'] = True
    else: report['components']['whisper-tiny']['inference']['timings_verified'] = False
    receipt = {'source': runner.source, 'models': identities(device), 'device': device, 'report': report}
    assert acceptance._facts(receipt) == (False, False)


def test_cuda_metrics_count_only_for_current_matching_cuda_models(service, tmp_path):
    value, runner, store = service
    finish(value, runner, store, tmp_path, device='cuda')
    assert next(row for row in value.state()['checks'] if row['id'] == 'physical_gpu')['status'] == 'passed'
    runner.device = 'cpu'
    assert next(row for row in value.state()['checks'] if row['id'] == 'physical_gpu')['status'] == 'pending'


def test_changed_sample_cannot_receive_owner_quality_approval(service, tmp_path):
    value, runner, store = service
    job = finish(value, runner, store, tmp_path)
    Path(job['outputs'][1]['path']).write_bytes(b'Changed unrelated sample')
    assert value.state()['sample_url'] is None
    with pytest.raises(ValueError, match='unavailable'):
        value.listen({'job_id': job['id'], 'approve': True})


def test_sanitized_export_omits_diagnostics_private_text_paths_tokens_and_media(service, tmp_path):
    value, runner, store = service
    job = finish(value, runner, store, tmp_path)
    path = Path(job['outputs'][0]['path']); receipt = json.loads(path.read_text())
    receipt['report']['access_token'] = 'owned-secret-fixture'
    receipt['report']['error'] = '/private/owner/path'
    receipt['report']['components']['opus-en-ru']['inference']['segments'] = [{'source_text': 'private manuscript'}]
    receipt['report_sha256'] = acceptance._digest(receipt['report']); json_write(path, receipt)
    exported = json.dumps(value.export())
    assert all(text not in exported for text in ['owned-secret-fixture', '/private/', 'private manuscript', str(store.data)])
    assert value.export()['satisfies_release_acceptance'] is False


def test_changed_source_or_models_block_before_worker_invokes_any_model(service, tmp_path):
    value, runner, store = service
    result = value.run({'device': 'cpu', 'consent': True}); job = store.job(result['job']['id'])
    runner.source['source_commit'] = 'd' * 40
    with pytest.raises(ValueError, match='changed after approval'):
        acceptance.run_worker(job['payload'], tmp_path, store.data)
    assert runner.calls == []


def test_worker_hook_uses_only_the_private_server_owned_acceptance_snapshot(service, tmp_path, monkeypatch):
    value, runner, store = service
    queued = value.run({'device': 'cpu', 'consent': True}); job = store.job(queued['job']['id'])
    output = tmp_path / 'stage'; output.mkdir()
    files, warnings, meta = worker.dubbing_component({**job['payload'], 'settings': {}}, output, store.data)
    assert len(files) == 2 and warnings == [] and meta == {}
    assert len(runner.calls) == 1 and runner.calls[0]['approve_install'] is False
    assert runner.calls[0]['expected_source'] == runner.source


def test_authenticated_api_uses_existing_queue_and_refuses_client_request_injection(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from forge.app import create_app
    runner = SyntheticRunner()
    monkeypatch.setattr(acceptance, '_runner', lambda: runner)
    monkeypatch.setattr(dubbing_models, 'catalog', lambda data: [{'id': name, 'ready': True, **row}
                       for name, row in identities().items()])
    app = create_app(tmp_path / 'data', 'owned-readiness-api-token', 19978, workers=False)
    settings = app.state.store.settings(); settings.update(automatic_update_checks=False, automatic_dependency_repairs=False)
    app.state.store.set('settings', settings)
    with TestClient(app, base_url='http://127.0.0.1:19978') as client:
        assert client.get('/api/dubbing/acceptance/state').status_code in {401, 403}
        headers = {'x-forge-token': 'owned-readiness-api-token'}
        response = client.get('/api/dubbing/acceptance/state', headers=headers)
        assert response.status_code == 200 and response.json()['satisfies_release_acceptance'] is False
        assert client.post('/api/dubbing/acceptance/translation-review', headers=headers,
                           json={'job_id': 'missing', 'approve': True}).status_code == 400
        forbidden = client.post('/api/dubbing/acceptance/run', json={'device': 'cpu', 'consent': True,
            'source': '/private/input.wav'}, headers=headers)
        assert forbidden.status_code == 400 and app.state.store.jobs() == []
        queued = client.post('/api/dubbing/acceptance/run', json={'device': 'cpu', 'consent': True}, headers=headers)
        assert queued.status_code == 200
        job = app.state.store.job(queued.json()['job']['id'])
        assert job['tool'] == 'dubbing_model_test' and job['payload']['options']['action'] == 'test'
        assert job['payload']['dubbing_acceptance_run']['models'] == identities()
        exported = client.get('/api/dubbing/acceptance/export', headers=headers)
        assert exported.status_code == 200 and 'attachment' in exported.headers['content-disposition']
        assert str(tmp_path) not in exported.text and exported.json()['model_evidence'] is None
