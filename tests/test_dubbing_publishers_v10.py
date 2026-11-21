"""Publisher trust boundaries. Fixture metadata never substitutes for inference."""
import copy
import hashlib
import json

import pytest

from forge import dubbing_models as models
from scripts import dubbing_publisher_inventory as inventory
from test_dubbing_models_v10 import managed, install


@pytest.fixture
def publisher(monkeypatch):
    monkeypatch.setattr(models, 'MODEL_LICENSE_NOTICES', {})
    part = copy.deepcopy(models._spec('qwen3-tts-0.6b-base')['parts'][0])
    info = {'id': part['repository'], 'sha': part['revision'], 'cardData': {'license': part['license']},
            'siblings': [{'rfilename': name, 'size': 100, 'blobId': 'a' * 40,
                          **({'lfs': {'sha256': 'b' * 64, 'size': 100}} if name.endswith('.safetensors') else {})}
                         for name in [*part['required_files'], 'README.md']]}
    requests = []
    def fetch(url):
        requests.append(url)
        return copy.deepcopy(info)
    monkeypatch.setattr(models, '_fetch_json', fetch)
    return part, info, requests


def test_exact_publisher_and_nested_tokenizer_identities(publisher):
    part, _, requests = publisher
    value = models.discover_part(part)
    assert requests == [f"https://huggingface.co/api/models/{part['repository']}/revision/{part['revision']}?blobs=true"]
    assert value['revision'] == part['revision']
    assert {'model.safetensors', 'speech_tokenizer/model.safetensors', 'merges.txt', 'vocab.json'} <= {x['name'] for x in value['artifacts']}
    assert all('/resolve/' + part['revision'] + '/' in x['url'] for x in value['artifacts'])
    assert value['bytes'] == 100 * len(value['artifacts'])


def test_explicit_unpinned_discovery_returns_immutable_review_urls(publisher):
    part, info, requests = publisher
    part['revision'] = ''
    value = models.discover_part(part)
    assert '/revision/main?' in requests[0]
    assert value['revision'] == info['sha']
    assert all('/resolve/main/' not in x['url'] for x in value['artifacts'])
    assert 'ready' not in value and 'inference_verified' not in value


@pytest.mark.parametrize('fault', ['publisher_transfer', 'missing_publisher', 'revision', 'missing_revision', 'license',
                                  'missing_card', 'missing_siblings', 'invalid_sibling', 'invalid_name', 'bool_size',
                                  'lfs_size', 'lfs_bool_size', 'invalid_lfs', 'empty_lfs_list', 'missing_digest', 'duplicate', 'case_collision'])
def test_changed_or_unverifiable_publisher_metadata_fails_closed(publisher, fault):
    part, info, _ = publisher
    if fault == 'publisher_transfer': info['id'] = 'new-publisher/same-model'
    elif fault == 'missing_publisher': info['id'] = None
    elif fault == 'revision': info['sha'] = 'c' * 40
    elif fault == 'missing_revision': info['sha'] = None
    elif fault == 'license': info['cardData']['license'] = 'unreviewed-license'
    elif fault == 'missing_card': info['cardData'] = None
    elif fault == 'missing_siblings': info['siblings'] = None
    elif fault == 'invalid_sibling': info['siblings'].append(None)
    elif fault == 'invalid_name': info['siblings'][0]['rfilename'] = 42
    elif fault == 'bool_size': info['siblings'][0]['size'] = True
    elif fault in {'lfs_size', 'lfs_bool_size'}:
        next(x for x in info['siblings'] if 'lfs' in x)['lfs']['size'] = True if fault == 'lfs_bool_size' else 99
    elif fault == 'invalid_lfs': info['siblings'][0]['lfs'] = 'unverifiable'
    elif fault == 'empty_lfs_list': info['siblings'][0]['lfs'] = []
    elif fault == 'missing_digest': info['siblings'][0].pop('blobId')
    elif fault in {'duplicate', 'case_collision'}:
        item = copy.deepcopy(info['siblings'][-1] if fault == 'duplicate' else info['siblings'][0])
        if fault == 'case_collision': item['rfilename'] = 'CONFIG.json'
        info['siblings'].append(item)
    with pytest.raises(models.ComponentUnavailable): models.discover_part(part)


@pytest.mark.parametrize('missing', ['model.safetensors', 'speech_tokenizer/model.safetensors', 'merges.txt',
                                   'vocab.json', 'preprocessor_config.json', 'speech_tokenizer/preprocessor_config.json'])
def test_incomplete_qwen_snapshot_is_not_installable(publisher, missing):
    part, info, _ = publisher
    info['siblings'] = [x for x in info['siblings'] if x['rfilename'] != missing]
    with pytest.raises(models.ComponentUnavailable, match='missing required'): models.discover_part(part)


def test_small_evidence_does_not_download_weights_or_approve_distribution(tmp_path, publisher, monkeypatch):
    _, info, _ = publisher
    for item in info['siblings']:
        if item['rfilename'].endswith('.safetensors'): item['size'] = item['lfs']['size'] = 9_000_000
    path = tmp_path / 'model-card'
    path.write_text('---\nlicense: apache-2.0\n---\nOwned metadata fixture.\n')
    downloads = []
    def download(artifact, cache):
        assert artifact['size'] <= inventory.SMALL_ARTIFACT_LIMIT
        downloads.append(artifact['name'])
        return path
    monkeypatch.setattr(models, '_download', download)
    value = inventory.verify_component('qwen3-tts-0.6b-base', tmp_path / 'cache')
    assert downloads and not any(x.endswith('.safetensors') for x in downloads)
    assert not value['inference_tested'] and not value['installed']
    part = value['parts'][0]
    assert part['license_evidence']['distribution_approved'] is False
    for artifact in part['artifacts']:
        assert artifact['content_download_verified'] is not artifact['name'].endswith('.safetensors')
        if artifact['content_download_verified']: assert artifact['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_downloaded_card_must_match_publisher_license(tmp_path, publisher, monkeypatch):
    path = tmp_path / 'model-card'; path.write_text('---\nlicense: unreviewed-license\n---\n')
    monkeypatch.setattr(models, '_download', lambda *args: path)
    with pytest.raises(models.ComponentUnavailable, match='model card license differs'):
        inventory.verify_component('qwen3-tts-0.6b-base', tmp_path / 'cache')


def test_unpinned_inventory_requires_explicit_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(models, 'MODEL_PINS', {})
    monkeypatch.setattr(models, '_fetch_json', lambda *_: pytest.fail('Implicit main discovery is not authorized.'))
    with pytest.raises(models.ComponentUnavailable, match='--discover'):
        inventory.verify_component('opus-en-ru', tmp_path / 'cache')


def test_saved_inventory_matches_reviewed_pins_and_required_files():
    value = json.loads((models.ROOT / 'forge/dubbing_publisher_inventory.json').read_text('utf-8'))
    assert value['schema'] == 1
    assert {row['component'] for row in value['components']} == set(inventory.DEFAULT_COMPONENTS)
    for component in value['components']:
        assert not component['installed'] and not component['inference_tested']
        for part, descriptor in zip(component['parts'], models._spec(component['component'])['parts'], strict=True):
            assert part['revision'] == models.MODEL_PINS[part['repository']] == descriptor['revision']
            assert set(descriptor['required_files']) <= {x['name'] for x in part['artifacts']}
            assert all(models.SHA256.fullmatch(x['sha256']) for x in part['artifacts'])
            assert part['license_evidence']['distribution_approved'] is False


@pytest.mark.parametrize('change', ['cuda', 'new-runtime'])
def test_existing_cpu_activation_cannot_satisfy_another_approved_profile(managed, monkeypatch, change):
    original, _ = install(managed)
    before_state = models._state_path(managed.data).read_bytes()
    before_receipt = models.resolve(managed.component, managed.data)['receipt']
    if change == 'cuda':
        previous = models.hardware
        monkeypatch.setattr(models, 'hardware', lambda *a, **k: {**previous(*a, **k),
            'gpus': [{'driver_compatible_cu128': True, 'free_vram_mb': 32_000}]})
        device = 'cuda'
    else:
        previous = models._dependency_plan
        def changed_dependencies(*args):
            value = copy.deepcopy(previous(*args))
            value['wheels'][0]['sha256'] = 'c' * 64
            return value
        monkeypatch.setattr(models, '_dependency_plan', changed_dependencies)
        device = 'cpu'
    plan = models.installation_plan([managed.component], managed.data, device=device)
    assert plan['components'][0]['runtime_id'] != original['runtime_revision']
    monkeypatch.setattr(models, '_environment', lambda *a, **k: pytest.fail('A different profile requires reviewed Repair, not cached installation.'))
    with pytest.raises(models.ComponentUnavailable, match='approved device/runtime'):
        models.install(managed.component, managed.data, consent=True, device=device, plan=plan)
    assert models._state_path(managed.data).read_bytes() == before_state
    after = models.resolve(managed.component, managed.data)
    assert after['device'] == 'cpu' and after['runtime_revision'] == original['runtime_revision']
    assert after['receipt'] == before_receipt


def test_external_notices_keep_real_source_scope_and_install_below_notices(monkeypatch):
    part = models._spec('opus-en-ru')['parts'][0]
    notices = models._notice_plan(part)
    assert {item['license_id'] for item in notices} == {'Apache-2.0', 'CC-BY-4.0'}
    assert all(item['origin'] == 'external_license_evidence' and item['name'].startswith('notices/') for item in notices)
    assert all(item['source_revision'] in item['url'] and models.SHA256.fullmatch(item['digest']) for item in notices)
    checkpoint = {**part, 'artifacts': [{'name': 'README.md', 'size': 20, 'digest': 'a' * 40, 'algorithm': 'git-sha1'}], 'bytes': 20}
    monkeypatch.setattr(models, 'discover_part', lambda descriptor: copy.deepcopy(checkpoint))
    planned = models._parts_plan({'parts': [part]})[0]
    assert len(planned['artifacts']) == len(notices) + 1
    assert planned['bytes'] == 20 + sum(item['size'] for item in notices)
    assert planned['license'] == 'apache-2.0' and planned['license_scope_conflict'] is True


@pytest.mark.parametrize('field,value', [('source_revision', 'main'), ('sha256', ''), ('size', True), ('name', 'LICENSE')])
def test_external_notice_identity_cannot_be_mutable_or_invented(field, value):
    part = models._spec('opus-en-ru')['parts'][0]
    part['external_license_notices'][0][field] = value
    with pytest.raises(models.ComponentUnavailable): models._notice_plan(part)


@pytest.mark.parametrize('change', ['new-notice', 'changed-digest'])
def test_changed_required_artifacts_cannot_reuse_old_model_receipt(managed, monkeypatch, change):
    first, _ = install(managed)
    original = models.resolve(managed.component, managed.data)
    state = models._state_path(managed.data).read_bytes()
    artifact = managed.spec['parts'][0]['artifacts'][0]
    if change == 'new-notice':
        managed.spec['parts'][0]['artifacts'].append({**artifact, 'name': 'notices/required-license.txt'})
    else:
        artifact['sha256'] = 'c' * 64
    plan = models.installation_plan([managed.component], managed.data, device='cpu')
    assert plan['components'][0]['runtime_id'] == first['runtime_revision']
    monkeypatch.setattr(models, '_environment', lambda *a, **k: pytest.fail('Missing notice requires explicit Repair.'))
    with pytest.raises(models.ComponentUnavailable, match='Repair'):
        models.install(managed.component, managed.data, consent=True, plan=plan)
    assert models._state_path(managed.data).read_bytes() == state
    assert models.resolve(managed.component, managed.data)['receipt'] == original['receipt']


def test_external_notice_is_in_quote_retained_bytes_and_receipt(managed):
    managed.spec['parts'][0]['external_license_notices'] = [{
        'name': 'notices/fixture-license.txt', 'source_repository': 'owned-fixture/notices',
        'source_revision': 'b' * 40, 'source_path': 'LICENSE', 'scope': 'owned_installer_fixture',
        'license_id': 'MIT', 'size': managed.blob.stat().st_size, 'sha256': models._hash(managed.blob)}]
    result, plan = install(managed)
    selected = plan['components'][0]
    assert selected['license_information'][0]['external_notices'][0]['license_id'] == 'MIT'
    assert selected['license_information'][0]['distribution_approved'] is False
    installed = models.resolve(managed.component, managed.data)
    path = installed['root'] / 'parts/voice/notices/fixture-license.txt'
    assert path.read_bytes() == managed.blob.read_bytes()
    assert installed['receipt']['files']['parts/voice/notices/fixture-license.txt']['sha256'] == models._hash(path)
    assert installed['receipt']['parts'][0]['external_license_evidence'][0]['origin'] == 'external_license_evidence'


@pytest.mark.parametrize('tiny_ready', [False, True])
def test_setup_honors_explicit_project_asr_and_preserves_turbo_default(tmp_path, monkeypatch, tiny_ready):
    checked = []
    monkeypatch.setattr(models, 'hardware', lambda *a: {'application_python_compatible': True,
        'managed_python_available': False, 'ffmpeg': True, 'ffprobe': True})
    monkeypatch.setattr(models, 'catalog', lambda *a: [
        {'id': 'opus-en-ru', 'recommended': True, 'ready': True},
        {'id': 'qwen3-tts-0.6b-base', 'recommended': True, 'ready': True},
        {'id': 'whisper-tiny', 'recommended': False, 'ready': tiny_ready},
        {'id': 'whisper-large-v3-turbo', 'recommended': False, 'ready': False}])
    def legacy(data, model):
        checked.append(model)
        return {'ready': False}
    monkeypatch.setattr(models, 'existing_whisper_state', legacy)
    selected = models.setup_state(tmp_path, asr_model='tiny')
    assert selected['speech_recognition']['model'] == 'tiny' and selected['ready'] is tiny_ready
    assert selected['missing_recommended'] == ([] if tiny_ready else ['whisper-tiny'])
    assert 'whisper-large-v3-turbo' not in selected['recommended_components']
    default = models.setup_state(tmp_path)
    assert default['ready'] is False and default['missing_recommended'] == ['whisper-large-v3-turbo']
    assert checked == ['tiny', 'large-v3-turbo']


def test_setup_retains_existing_compatible_selected_whisper_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(models, 'hardware', lambda *a: {'application_python_compatible': True,
        'managed_python_available': False, 'ffmpeg': True, 'ffprobe': True})
    monkeypatch.setattr(models, 'catalog', lambda *a: [
        {'id': 'opus-en-ru', 'recommended': True, 'ready': True},
        {'id': 'qwen3-tts-0.6b-base', 'recommended': True, 'ready': True},
        {'id': 'whisper-tiny', 'recommended': False, 'ready': False}])
    monkeypatch.setattr(models, 'existing_whisper_state', lambda data, model: {'ready': model == 'tiny', 'model_revision': 'c' * 40})
    monkeypatch.setattr(models, 'install', lambda *a, **k: pytest.fail('Existing compatible Whisper must be reused.'))
    value = models.setup_state(tmp_path, asr_model='tiny')
    assert value['ready'] and value['speech_recognition']['ready']
    assert value['speech_recognition']['isolated_ready'] is False
    assert value['speech_recognition']['model_revision'] == 'c' * 40
    assert value['missing_recommended'] == []


def test_setup_rejects_unregistered_asr_before_inspecting_or_installing(tmp_path, monkeypatch):
    monkeypatch.setattr(models, 'hardware', lambda *a: pytest.fail('Unknown model must fail before hardware or data work.'))
    with pytest.raises(ValueError, match='registered Whisper'):
        models.setup_state(tmp_path, asr_model='../../unknown')
