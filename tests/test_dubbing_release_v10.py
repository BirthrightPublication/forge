"""Release-gate contract fixtures, explicitly not actual model/GPU acceptance.

The passing fixture is synthetic input to the validator only. No production
inventory/evidence is created and no remote Actions run or model is downloaded.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import urllib.error
import zipfile

import pytest

from scripts import check_dubbing_release as gate


TAG, COMMIT, RUN = 'v0.10.0', 'a' * 40, '123456'


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return path


def refresh_manifest(source):
    files = {path.relative_to(source).as_posix(): gate.digest(path) for path in source.rglob('*')
             if path.is_file() and path.name != 'release-manifest.json'}
    dump(source / 'release-manifest.json', {'app': 'FORGE', 'format': 1, 'version': TAG[1:], 'files': files})


@pytest.fixture
def proof(tmp_path):
    source, evidence = tmp_path / 'source', tmp_path / 'acceptance'
    pins = {repository: ('b' if component == 'opus-en-ru' else 'c') * 40
            for component, repository in gate.RECOMMENDED.items()}
    models = source / 'forge' / 'dubbing_models.py'
    models.parent.mkdir(parents=True)
    models.write_text('MODEL_PINS: dict[str, str] = ' + repr(pins) + '\n', encoding='utf-8')
    requirements = source / 'scripts' / 'requirements-dubbing-core.txt'
    requirements.parent.mkdir(parents=True)
    requirements.write_text('# Synthetic validator closure, not installable acceptance.\ntorch==2.8.0\n', encoding='utf-8')
    wheel_name = 'forge/dubbing_wheel_locks/requirements-dubbing-core-windows-x64-py312-cpu.json'
    wheel = {'schema': 1, 'profile': 'requirements-dubbing-core', 'system': 'windows', 'architecture': 'x64',
        'python_version': '3.12', 'device': 'cpu', 'native_windows_execution': True, 'cross_resolved': False,
        'reviewed': True, 'dependency_closure_complete': True, 'requirements_sha256': gate.digest(requirements),
        'download_bytes': 7, 'wheels': [{'name': 'synthetic-1.0-py3-none-any.whl', 'package': 'synthetic',
        'version': '1.0', 'sha256': 'd' * 64, 'size': 7, 'license': 'MIT'}]}
    dump(source / wheel_name, wheel)
    components = {}
    for component, repository in gate.RECOMMENDED.items():
        names = ['config.json', 'model.safetensors', 'LICENSE']
        names += ['source.spm', 'target.spm', 'vocab.json'] if component == 'opus-en-ru' else [
            'generation_config.json', 'tokenizer_config.json', 'speech_tokenizer/config.json', 'speech_tokenizer/model.safetensors']
        components[component] = {'parts': [{'repository': repository, 'revision': pins[repository],
            'license': {'id': 'Apache-2.0', 'reviewed': True, 'commercial_use': True},
            'artifacts': [{'path': name, 'sha256': 'e' * 64, 'size': 11} for name in names]}],
            'runtime': {'requirements': 'scripts/requirements-dubbing-core.txt', 'requirements_sha256': gate.digest(requirements),
                        'wheel_inventory': wheel_name, 'wheel_inventory_sha256': gate.digest(source / wheel_name), 'device': 'cpu'}}
    dump(source / gate.INVENTORY, {'schema': 1, 'license_reviewed': True, 'components': components})
    refresh_manifest(source)
    expected = gate.static_inventory(source, TAG, COMMIT)
    report = dump(evidence / 'reports' / 'acceptance.json', {'fixture': 'SYNTHETIC CONTRACT TEST ONLY; not native/model/GPU proof.'})
    checks = {key: {'status': 'passed', 'evidence': ['reports/acceptance.json']} for key in gate.CHECKS}
    checks['gui_recommended_install'].update(through_forge_gui=True, installed_components=list(gate.RECOMMENDED))
    checks['restart_model_persistence']['verified_after_restart'] = True
    checks['playable_outputs']['decoded_and_probed'] = True
    checks['running_backend_version']['version'] = TAG[1:]
    for key in ('timed_english_asr', 'russian_translation', 'reference_russian_tts'):
        checks[key]['inference_tested'] = True
    checks['timed_english_asr'].update(timings_verified=True, segment_count=3)
    checks['russian_translation']['target_language'] = 'ru'
    checks['reference_russian_tts'].update(language='ru', reference_consent=True, reference_sha256='f' * 64)
    value = {**expected, 'schema': 1, 'app': 'FORGE', 'status': 'passed', 'repository': gate.REPOSITORY,
        'actions_run_id': RUN, 'native_windows_execution': True, 'inference_substituted': False,
        'synthetic_only': False, 'reference_consent': True, 'private_inputs_not_embedded': True,
        'checks': checks, 'physical_gpu': {'status': 'passed', 'evidence': ['reports/acceptance.json'],
            'physical_gpu_tested': True, 'inference_tested': True, 'device_name': 'Explicit synthetic validator device',
            'peak_vram_bytes': 1000},
        'artifacts': [{'path': 'reports/acceptance.json', 'sha256': gate.digest(report), 'size': report.stat().st_size}]}
    path = dump(evidence / 'DUBBING_ACCEPTANCE.json', value)
    return {'source': source, 'path': path, 'value': value, 'expected': expected, 'wheel_name': wheel_name}


def verify(proof):
    return gate.verify_evidence(proof['source'], TAG, COMMIT, proof['path'], run_id=RUN)


def change_inventory(proof, mutate):
    path = proof['source'] / gate.INVENTORY
    value = json.loads(path.read_text(encoding='utf-8'))
    mutate(value)
    dump(path, value)
    refresh_manifest(proof['source'])


def test_passing_contract_requires_all_exact_source_identities_and_real_acceptance_fields(proof):
    result = verify(proof)
    assert result['status'] == 'passed' and result['physical_gpu_tested'] is True
    assert result['acceptance_sha256'] == gate.digest(proof['path'])
    assert result['source_manifest_sha256'] == gate.digest(proof['source'] / 'release-manifest.json')


@pytest.mark.parametrize('tag,expected', [('v0.5.0', False), ('v0.8.0', False), ('v0.9.99', False),
                                        ('v0.10.0', True), ('v0.10.1', True), ('v1.0.0', True)])
def test_gate_applies_only_from_v010_and_never_inherits_old_release_exceptions(tag, expected):
    assert gate.required(tag) is expected
    if not expected:
        assert gate.verify_evidence(Path('/absent'), tag, '', None, run_id='')['status'] == 'not_required'


def test_missing_model_pin_fails_before_any_network_call(proof, monkeypatch, tmp_path):
    (proof['source'] / 'forge' / 'dubbing_models.py').write_text('MODEL_PINS: dict[str, str] = {}\n', encoding='utf-8')
    refresh_manifest(proof['source'])
    monkeypatch.setattr(gate, '_api', lambda *args: pytest.fail('Unverified inventory must fail before network I/O.'))
    with pytest.raises(gate.GateError, match='missing reviewed immutable recommended model pin'):
        gate.download_evidence(proof['source'], TAG, COMMIT, run_id=RUN, destination=tmp_path / 'output', token='synthetic')


def test_missing_production_inventory_never_becomes_an_empty_success(proof):
    (proof['source'] / gate.INVENTORY).unlink()
    refresh_manifest(proof['source'])
    with pytest.raises(gate.GateError, match='missing or oversized required file'):
        gate.static_inventory(proof['source'], TAG, COMMIT)


@pytest.mark.parametrize('fault', ['unreviewed_license', 'noncommercial_license', 'missing_hash', 'missing_tokenizer',
                                 'foreign_pin', 'foreign_wheel_hash', 'missing_native_wheels'])
def test_unverified_model_or_runtime_inventory_is_rejected(proof, fault):
    def mutate(value):
        component = value['components']['qwen3-tts-0.6b-base']
        part = component['parts'][0]
        if fault == 'unreviewed_license': part['license']['reviewed'] = False
        elif fault == 'noncommercial_license': part['license']['commercial_use'] = False
        elif fault == 'missing_hash': part['artifacts'][0].pop('sha256')
        elif fault == 'missing_tokenizer': part['artifacts'] = [item for item in part['artifacts'] if not item['path'].startswith('speech_tokenizer/')]
        elif fault == 'foreign_pin': part['revision'] = 'f' * 40
        elif fault == 'foreign_wheel_hash': component['runtime']['wheel_inventory_sha256'] = '0' * 64
        else: component['runtime']['wheel_inventory'] = 'forge/missing.json'
    change_inventory(proof, mutate)
    with pytest.raises(gate.GateError):
        gate.static_inventory(proof['source'], TAG, COMMIT)


@pytest.mark.parametrize('field,value', [('native_windows_execution', False), ('cross_resolved', True),
                                      ('reviewed', False), ('dependency_closure_complete', False), ('system', 'linux')])
def test_linux_or_cross_resolved_wheels_do_not_satisfy_windows_acceptance(proof, field, value):
    path = proof['source'] / proof['wheel_name']
    wheel = json.loads(path.read_text(encoding='utf-8'))
    wheel[field] = value
    dump(path, wheel)
    change_inventory(proof, lambda inventory: [component['runtime'].update(wheel_inventory_sha256=gate.digest(path))
                                               for component in inventory['components'].values()])
    with pytest.raises(gate.GateError):
        gate.static_inventory(proof['source'], TAG, COMMIT)


def test_missing_external_evidence_is_rejected(proof):
    with pytest.raises(gate.GateError, match='missing DUBBING_ACCEPTANCE'):
        gate.verify_evidence(proof['source'], TAG, COMMIT, None, run_id=RUN)


@pytest.mark.parametrize('field,value', [('source_commit', '9' * 40), ('tag', 'v0.9.0'),
    ('source_manifest_sha256', '9' * 64), ('model_inventory_sha256', '9' * 64),
    ('model_revisions', {}), ('wheel_inventory_sha256', {}), ('repository', 'another/project'),
    ('actions_run_id', '99999'), ('status', 'failed'), ('native_windows_execution', False),
    ('synthetic_only', True), ('inference_substituted', True), ('reference_consent', False), ('private_inputs_not_embedded', False)])
def test_stale_foreign_failed_synthetic_or_unconsented_proof_is_rejected(proof, field, value):
    proof['value'][field] = value
    dump(proof['path'], proof['value'])
    with pytest.raises(gate.GateError):
        verify(proof)


@pytest.mark.parametrize('check', gate.CHECKS)
def test_each_required_real_acceptance_check_must_pass_with_its_report(proof, check):
    proof['value']['checks'][check]['status'] = 'skipped'
    dump(proof['path'], proof['value'])
    with pytest.raises(gate.GateError, match='missing/failed real acceptance check'):
        verify(proof)


@pytest.mark.parametrize('fault', ['untested', 'v08_exception', 'no_inference', 'no_vram'])
def test_physical_gpu_pass_is_required_without_inherited_cpu_release_exception(proof, fault):
    gpu = proof['value']['physical_gpu']
    if fault == 'untested': gpu['status'] = 'not_tested'
    elif fault == 'v08_exception':
        gpu['status'] = 'excepted'
        gpu['exception'] = {'tag': 'v0.8.0', 'approved': True}
    elif fault == 'no_inference': gpu['inference_tested'] = False
    else: gpu['peak_vram_bytes'] = 0
    dump(proof['path'], proof['value'])
    with pytest.raises(gate.GateError):
        verify(proof)


def test_changed_or_missing_report_is_not_a_valid_acceptance_claim(proof):
    (proof['path'].parent / 'reports' / 'acceptance.json').write_text('changed', encoding='utf-8')
    with pytest.raises(gate.GateError, match='differs from its digest'):
        verify(proof)


@pytest.mark.parametrize('fault', ['missing_inference', 'untimed_asr', 'empty_asr', 'wrong_language',
                                 'unowned_reference', 'manual_install', 'wrong_backend', 'unprobed_outputs'])
def test_summary_pass_flags_cannot_replace_specific_real_acceptance(proof, fault):
    checks = proof['value']['checks']
    if fault == 'missing_inference': checks['reference_russian_tts']['inference_tested'] = False
    elif fault == 'untimed_asr': checks['timed_english_asr']['timings_verified'] = False
    elif fault == 'empty_asr': checks['timed_english_asr']['segment_count'] = 0
    elif fault == 'wrong_language': checks['russian_translation']['target_language'] = 'en'
    elif fault == 'unowned_reference': checks['reference_russian_tts']['reference_consent'] = False
    elif fault == 'manual_install': checks['gui_recommended_install']['through_forge_gui'] = False
    elif fault == 'wrong_backend': checks['running_backend_version']['version'] = '0.9.0'
    else: checks['playable_outputs']['decoded_and_probed'] = False
    dump(proof['path'], proof['value'])
    with pytest.raises(gate.GateError):
        verify(proof)


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/drive', 'folder\\escape', 'folder/../escape'])
def test_untrusted_evidence_zip_paths_cannot_escape_extraction(tmp_path, name):
    archive = tmp_path / 'proof.zip'
    with zipfile.ZipFile(archive, 'w') as bundle: bundle.writestr(name, 'bad')
    with pytest.raises(gate.GateError): gate.safe_extract(archive, tmp_path / 'extracted')
    assert not (tmp_path / 'extracted').exists()


def test_evidence_zip_links_are_rejected(tmp_path):
    archive = tmp_path / 'proof.zip'
    entry = zipfile.ZipInfo('linked.json')
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, 'w') as bundle: bundle.writestr(entry, '../other')
    with pytest.raises(gate.GateError, match='linked/special'): gate.safe_extract(archive, tmp_path / 'extracted')


@pytest.mark.parametrize('names', [('same.json', 'SAME.json'), ('folder', 'folder/file.json'), ('folder/file.json', 'folder')])
def test_case_and_file_directory_collisions_are_rejected(tmp_path, names):
    archive = tmp_path / 'proof.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        for name in names: bundle.writestr(name, '{}')
    with pytest.raises(gate.GateError): gate.safe_extract(archive, tmp_path / 'extracted')


def test_extraction_bounds_and_existing_destination_are_enforced(tmp_path, monkeypatch):
    archive = tmp_path / 'proof.zip'
    with zipfile.ZipFile(archive, 'w') as bundle: bundle.writestr('big.json', 'x' * 100)
    monkeypatch.setattr(gate, 'MAX_FILE', 10)
    with pytest.raises(gate.GateError, match='oversized'): gate.safe_extract(archive, tmp_path / 'extracted')
    existing = tmp_path / 'existing'; existing.mkdir()
    with pytest.raises(gate.GateError, match='new owned directory'): gate.safe_extract(archive, existing)


def valid_run():
    return {'status': 'completed', 'conclusion': 'success', 'head_sha': COMMIT,
            'repository': {'full_name': gate.REPOSITORY}, 'head_repository': {'full_name': gate.REPOSITORY}}


@pytest.mark.parametrize('field,value', [('status', 'in_progress'), ('conclusion', 'failure'), ('head_sha', 'f' * 40),
                                      ('repository', {'full_name': 'foreign/repo'}), ('head_repository', {'full_name': 'foreign/fork'})])
def test_external_actions_run_must_be_successful_same_repository_and_exact_commit(field, value):
    run = valid_run(); run[field] = value
    with pytest.raises(gate.GateError): gate.validate_run(run, COMMIT)


def test_download_uses_exact_run_artifact_and_never_forwards_token_to_signed_host(proof, tmp_path, monkeypatch):
    archive = tmp_path / 'fixture.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in proof['path'].parent.rglob('*'):
            if path.is_file(): bundle.write(path, path.relative_to(proof['path'].parent).as_posix())
    raw = archive.read_bytes()
    artifact = {'id': 7, 'name': 'dubbing-acceptance-' + TAG, 'expired': False,
                'size_in_bytes': len(raw), 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}
    paths = []
    def api(path, token):
        paths.append(path); assert token == 'synthetic-token'
        return valid_run() if path == 'actions/runs/' + RUN else {'total_count': 1, 'artifacts': [artifact]}
    monkeypatch.setattr(gate, '_api', api)
    requests = []
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, 'redirect', {'Location': 'https://owned.blob.core.windows.net/proof?signature=not-a-real-secret'}, None)
            assert request.get_header('Authorization') is None
            return io.BytesIO(raw)
    monkeypatch.setattr(gate.urllib.request, 'build_opener', lambda *args: Opener())
    destination = tmp_path / 'downloaded'
    result = gate.download_evidence(proof['source'], TAG, COMMIT, run_id=RUN, destination=destination, token='synthetic-token')
    assert result['status'] == 'passed'
    assert paths == ['actions/runs/' + RUN, 'actions/runs/' + RUN + '/artifacts?per_page=100']
    assert requests[0].get_header('Authorization') == 'Bearer synthetic-token'
    assert all((destination / name).is_file() for name in gate.PROOF_FILES)
    assert result['evidence_archive'] == {'name': 'DUBBING_EVIDENCE.zip',
        'sha256': gate.digest(destination / 'DUBBING_EVIDENCE.zip'),
        'size': (destination / 'DUBBING_EVIDENCE.zip').stat().st_size}
    assert not destination.with_name(destination.name + '.zip').exists()


def test_legacy_download_path_never_requests_new_dubbing_proof(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, '_api', lambda *args: pytest.fail('Pre-V0.10 releases do not acquire a new gate.'))
    result = gate.download_evidence(tmp_path, 'v0.9.0', '', run_id='', destination=tmp_path / 'unused', token='')
    assert result['status'] == 'not_required'


@pytest.mark.parametrize('fault', ['wrong_name', 'expired', 'duplicate', 'missing_digest'])
def test_external_artifact_must_have_fixed_identity_before_download(proof, tmp_path, monkeypatch, fault):
    artifact = {'id': 7, 'name': 'dubbing-acceptance-' + TAG, 'expired': False,
                'size_in_bytes': 100, 'digest': 'sha256:' + 'f' * 64}
    if fault == 'wrong_name': artifact['name'] = 'unrelated-evidence'
    elif fault == 'expired': artifact['expired'] = True
    elif fault == 'missing_digest': artifact.pop('digest')
    artifacts = [artifact, dict(artifact)] if fault == 'duplicate' else [artifact]
    monkeypatch.setattr(gate, '_api', lambda path, token: valid_run() if path == 'actions/runs/' + RUN
                        else {'total_count': len(artifacts), 'artifacts': artifacts})
    monkeypatch.setattr(gate.urllib.request, 'build_opener', lambda *args: pytest.fail('Invalid artifact must fail before download.'))
    with pytest.raises(gate.GateError):
        gate.download_evidence(proof['source'], TAG, COMMIT, run_id=RUN, destination=tmp_path / 'out', token='synthetic')


@pytest.mark.parametrize('workflow,job', [('release.yml', 'publish'), ('windows-release.yml', 'publish_existing')])
def test_both_publication_dags_require_dubbing_gate_success_and_retain_its_assets(workflow, job):
    root = Path(__file__).resolve().parents[1]
    text = (root / '.github' / 'workflows' / workflow).read_text(encoding='utf-8')
    publication = text.split('\n  ' + job + ':\n', 1)[1]
    assert 'dubbing]' in publication.split('if:', 1)[0]
    assert "needs.resolve.outputs.dubbing_required == 'true' && needs.dubbing.result == 'success'" in publication
    assert "needs.resolve.outputs.dubbing_required == 'false'" in publication
    assert 'DUBBING_EVIDENCE.zip' in publication and 'DUBBING_RELEASE_GATE.json' in publication
    assert 'check_dubbing_release.py inventory' in text and 'check_dubbing_release.py download' in text
    assert 'FORGE_DUBBING_ACCEPTANCE_RUN_ID' in text
    if workflow == 'windows-release.yml':
        section = text.split('\n  dubbing:\n')[1].split('\n  publish_existing:\n')[0]
        assert "if: needs.resolve.outputs.build_only == 'false' && needs.resolve.outputs.dubbing_required == 'true'" in section
