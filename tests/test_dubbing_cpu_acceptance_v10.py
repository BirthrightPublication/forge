"""Model acceptance trust contracts. No synthetic fixture is neural/GPU proof."""
from __future__ import annotations

import hashlib
import base64
import csv
import io
import json
from pathlib import Path
import math
import os
import shutil
import struct
import subprocess
import sys
import venv
import wave

import pytest

from scripts import dubbing_cpu_acceptance as acceptance


def packaged_source(tmp_path, *, commit='a' * 40):
    root = tmp_path / 'application'; root.mkdir()
    files = {}
    for name in (*acceptance.SOURCE_FILES, 'forge/owned-extra.py'):
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'# Owned source binding fixture, never executed.\n')
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {'app': 'FORGE', 'version': '0.10.0', 'format': 1,
                'package': {'source_commit': commit, 'source_manifest_sha256': 'b' * 64}, 'files': files}
    (root / 'release-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return root


def test_packaged_identity_uses_real_commit_and_binds_adapter_and_extra_bytes(tmp_path):
    root = packaged_source(tmp_path)
    value = acceptance.source_identity(root, additional_files=('forge/owned-extra.py',))
    assert value['source_commit'] == 'a' * 40 and value['source_kind'] == 'installed_manifest'
    assert value['source_manifest_sha256'] == 'b' * 64
    assert value['installed_manifest_sha256'] == acceptance.digest(root / 'release-manifest.json')
    assert 'forge/owned-extra.py' in value['source_files_sha256']
    (root / 'forge/owned-extra.py').write_bytes(b'Changed source; reject stale acceptance.')
    with pytest.raises(acceptance.AcceptanceBlocked, match='adapter bytes'):
        acceptance.source_identity(root, additional_files=('forge/owned-extra.py',))


@pytest.mark.parametrize('commit', ['', 'v0.10.0', '0.10.0', None, 'a' * 39])
def test_app_version_or_missing_metadata_never_substitutes_for_source_commit(tmp_path, commit):
    root = packaged_source(tmp_path, commit=commit)
    with pytest.raises(acceptance.AcceptanceBlocked, match='exact source commit'):
        acceptance.source_identity(root)


def test_source_identity_rejects_extra_path_escape(tmp_path):
    root = packaged_source(tmp_path)
    with pytest.raises(acceptance.AcceptanceBlocked, match='application-relative'):
        acceptance.source_identity(root, additional_files=('../outside.py',))


def git_source(tmp_path):
    root = packaged_source(tmp_path)
    subprocess.run(['git', 'init', '--quiet', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=FORGE test',
                    '-c', 'user.email=fixture@example.invalid', '-c', 'core.autocrlf=false',
                    'commit', '--quiet', '-m', 'Owned source identity fixture'], check=True)
    return root


def test_git_identity_binds_actual_adapter_bytes_and_committed_manifest(tmp_path):
    root = git_source(tmp_path)
    identity = acceptance.source_identity(root)
    assert identity['source_kind'] == 'git' and identity['source_checkout_clean'] is True
    changed = root / acceptance.SOURCE_FILES[0]
    changed.write_bytes(b'# Changed uncommitted adapter\n')
    with pytest.raises(acceptance.AcceptanceBlocked, match='adapter bytes'):
        acceptance.source_identity(root)
    # Updating only the local manifest cannot attach changed bytes to HEAD.
    manifest_path = root / 'release-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['files'][acceptance.SOURCE_FILES[0]] = acceptance.digest(changed)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(acceptance.AcceptanceBlocked, match='committed identity'):
        acceptance.source_identity(root)


def test_clean_committed_adapter_with_stale_manifest_cannot_supply_acceptance(tmp_path):
    root = git_source(tmp_path)
    (root / acceptance.SOURCE_FILES[0]).write_bytes(b'# Changed committed adapter\n')
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=FORGE test',
                    '-c', 'user.email=fixture@example.invalid', 'commit', '--quiet', '-am',
                    'Intentionally stale manifest fixture'], check=True)
    with pytest.raises(acceptance.AcceptanceBlocked, match='adapter bytes'):
        acceptance.source_identity(root)


def test_cpu_and_gpu_claims_require_matching_actual_inference_metrics():
    base = {'inference_tested': True, 'actual_device': 'cpu', 'physical_gpu_tested': False,
            'model_revision': 'a' * 40, 'runtime_revision': 'owned-runtime', 'elapsed_seconds': 1.0}
    assert acceptance._metrics(base, 'cpu')['physical_gpu_tested'] is False
    for changed in [{'inference_tested': False}, {'actual_device': 'cuda'}, {'physical_gpu_tested': True}]:
        with pytest.raises(acceptance.AcceptanceBlocked):
            acceptance._metrics({**base, **changed}, 'cpu')
    gpu = {**base, 'actual_device': 'cuda', 'physical_gpu_tested': True}
    with pytest.raises(acceptance.AcceptanceBlocked, match='incomplete'):
        acceptance._metrics(gpu, 'cuda')
    gpu.update(gpu_name='Owned schema fixture; not hardware', gpu_capability=[12, 0],
               peak_vram_bytes=4096, peak_reserved_vram_bytes=8192)
    assert acceptance._metrics(gpu, 'cuda')['peak_vram_bytes'] == 4096
    with pytest.raises(acceptance.AcceptanceBlocked, match='positive actual'):
        acceptance._metrics({**gpu, 'peak_vram_bytes': 0}, 'cuda')


@pytest.mark.skipif(os.name == 'nt', reason='Linux must not impersonate native Windows.')
def test_native_requirement_fails_before_any_install_or_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance.models, 'installation_plan', lambda *a, **k: pytest.fail('No installation from a wrong native platform.'))
    result = acceptance.run_acceptance(tmp_path / 'data', tmp_path / 'evidence', approve_install=True, require_native_windows=True)
    assert result['status'] == 'blocked' and result['native_windows_execution'] is False
    assert result['physical_gpu_tested'] is False and result['satisfies_release_acceptance'] is False
    assert 'native Windows' in result['error']
    assert json.loads((tmp_path / 'evidence' / acceptance.REPORT_NAME).read_text()) == result


def test_missing_models_never_trigger_unapproved_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, 'source_identity', lambda **kwargs: {'source_commit': 'a' * 40})
    monkeypatch.setattr(acceptance.shutil, 'which', lambda _: 'owned-media-tool')
    monkeypatch.setattr(acceptance.models, 'resolve', lambda *a, **k: (_ for _ in ()).throw(acceptance.models.ComponentUnavailable('Install the exact approved pack first.')))
    monkeypatch.setattr(acceptance.models, 'installation_plan', lambda *a, **k: pytest.fail('No implicit installation consent.'))
    result = acceptance.run_acceptance(tmp_path / 'data', tmp_path / 'evidence')
    assert result['status'] == 'blocked' and result['components'] == {}
    assert result['controller_environment_unchanged'] is True


def test_frozen_source_rejection_happens_before_model_access(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, 'source_identity', lambda **kwargs: {'source_commit': 'b' * 40})
    monkeypatch.setattr(acceptance.shutil, 'which', lambda _: 'owned-media-tool')
    monkeypatch.setattr(acceptance, 'installed_identities', lambda *a, **k: pytest.fail('Do not read models after a changed source.'))
    result = acceptance.run_acceptance(tmp_path / 'data', tmp_path / 'evidence', expected_source={'source_commit': 'a' * 40})
    assert result['status'] == 'blocked' and 'source changed' in result['error']


def test_device_mismatch_does_not_become_a_cached_gpu_success(monkeypatch):
    monkeypatch.setattr(acceptance.models, 'resolve', lambda *a: {'device': 'cpu', 'model_revision': 'a' * 40, 'runtime_revision': 'owned-cpu-runtime'})
    with pytest.raises(acceptance.AcceptanceBlocked, match='selected device profile'):
        acceptance.installed_identities(Path('/owned-generated-test-data'), 'cuda')


def test_existing_evidence_is_never_overwritten(tmp_path):
    output = tmp_path / 'evidence'; output.mkdir()
    sentinel = output / acceptance.REPORT_NAME; sentinel.write_text('Retain original evidence.')
    with pytest.raises(FileExistsError):
        acceptance.run_acceptance(tmp_path / 'data', output)
    assert sentinel.read_text() == 'Retain original evidence.'


def test_subprocess_failure_keeps_actionable_cause_but_redacts_private_context(tmp_path):
    data = tmp_path / 'private-model-data'
    error = subprocess.CalledProcessError(1, ['owned-fixture'], stderr=(
        'No installed English SAPI voice. ' + str(data) + ' token=owned-private-value https://example.invalid/private'))
    detail = acceptance._safe_error(error, data, tmp_path / 'evidence')
    assert 'No installed English SAPI voice' in detail
    assert str(data) not in detail and 'owned-private-value' not in detail and 'example.invalid' not in detail


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='Real FFmpeg decoding is required.')
def test_real_audio_decode_is_finite_nonsilent_and_preserves_input(tmp_path):
    target = tmp_path / 'owned-tone.wav'
    with wave.open(str(target), 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
        stream.writeframes(b''.join(struct.pack('<h', int(5000 * math.sin(2 * math.pi * 440 * i / 24000))) for i in range(24000)))
    before = target.read_bytes()
    actual = acceptance.decode_audio(target)
    assert actual['decoded_and_probed'] and actual['finite_samples'] and actual['duration'] == 1
    assert target.read_bytes() == before
    with wave.open(str(target), 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
        stream.writeframes(b'\0' * 48000)
    with pytest.raises(acceptance.AcceptanceBlocked, match='silent'):
        acceptance.decode_audio(target)


@pytest.fixture
def metadata_runtime(tmp_path):
    """An actual empty local venv; fake package metadata is never inference proof."""
    root = tmp_path / 'isolated-runtime'
    venv.EnvBuilder(with_pip=False, symlinks=os.name != 'nt').create(root)
    python = root / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    site = Path(subprocess.check_output([str(python), '-I', '-c',
        'import sysconfig;print(sysconfig.get_path("purelib"))'], text=True).strip())
    return root, python, site


def metadata_distribution(site, *, version='1.2.3', notices=None, unsafe_record=None, unhashed=False):
    info = site / 'owned_package-1.2.3.dist-info'; info.mkdir(parents=True)
    (info / 'METADATA').write_text('Metadata-Version: 2.4\nName: Owned_Package\nVersion: ' + version +
        '\nLicense: Test fixture declaration only\nLicense-Expression: MIT\n', encoding='utf-8')
    rows = []
    for name, raw in (notices or {}).items():
        path = info / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        sha = '' if unhashed else 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b'=').decode()
        rows.append([path.relative_to(site).as_posix(), sha, '' if unhashed else str(len(raw))])
    if unsafe_record:
        rows.append([unsafe_record, '', ''])
    stream = io.StringIO(); csv.writer(stream).writerows(rows)
    (info / 'RECORD').write_text(stream.getvalue(), encoding='utf-8')
    return info


def metadata_probe(runtime, *, version='1.2.3', python_version=None):
    root, python, _ = runtime
    return acceptance._bounded_command([str(python), '-I', '-c', acceptance.RUNTIME_METADATA_PROBE],
        payload={'environment': str(root), 'python_version': python_version or '.'.join(map(str, sys.version_info[:3])),
                 'expected_versions': {'owned-package': version}}, timeout=20)


def test_real_isolated_metadata_probe_verifies_recorded_nested_license_text(metadata_runtime):
    raw = b'Owned public license fixture. This is not a legal approval.\n'
    metadata_distribution(metadata_runtime[2], notices={'licenses/BSD-3-Clause.txt': raw})
    result = metadata_probe(metadata_runtime)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed['installed_versions'] == {'owned-package': '1.2.3'}
    notice = observed['notices'][0]
    assert notice['path'] == 'owned_package-1.2.3.dist-info/licenses/BSD-3-Clause.txt'
    assert notice['integrity'] == 'record_verified' and notice['text'] == raw.decode()
    assert notice['sha256'] == hashlib.sha256(raw).hexdigest() and notice['size'] == len(raw)
    assert str(metadata_runtime[0]).encode() not in result.stdout
    assert observed['packages'][0]['license_expression_declaration']['text'] == 'MIT'


@pytest.mark.parametrize('filename', ['ThirdPartyNotices.txt', 'THIRD_PARTY_NOTICES.txt',
                                    'third-party-licenses.txt', 'COPYRIGHT', 'AUTHORS.md'])
def test_compound_third_party_notices_outside_license_directories_are_record_verified(metadata_runtime, filename):
    raw = b'Public compiled third party attribution fixture.\n'
    metadata_distribution(metadata_runtime[2], notices={filename: raw})
    result = metadata_probe(metadata_runtime)
    assert result.returncode == 0, result.stderr
    notices = json.loads(result.stdout)['notices']
    assert len(notices) == 1
    assert notices[0]['path'].endswith('/' + filename)
    assert notices[0]['integrity'] == 'record_verified' and notices[0]['text'] == raw.decode()
    assert notices[0]['sha256'] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize('change', ['installed_version', 'python_marker'])
def test_runtime_probe_refuses_mismatching_exact_versions(metadata_runtime, change):
    metadata_distribution(metadata_runtime[2], version='9.9.9' if change == 'installed_version' else '1.2.3')
    result = metadata_probe(metadata_runtime, python_version='3.12.0' if change == 'python_marker' else None)
    assert result.returncode != 0
    assert b'version' in result.stderr and result.stdout == b''


def test_missing_full_license_files_remain_explicitly_pending(metadata_runtime):
    metadata_distribution(metadata_runtime[2])
    result = metadata_probe(metadata_runtime)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed['notices'] == []
    assert observed['packages_without_recorded_notice_files'] == ['owned-package']
    assert observed['packages'][0]['notice_file_count'] == 0


@pytest.mark.parametrize('unhashed', [False, True])
def test_unverified_or_modified_notice_never_exports_local_file_content(metadata_runtime, unhashed):
    info = metadata_distribution(metadata_runtime[2], notices={'LICENSE': b'Original public fixture.\n'}, unhashed=unhashed)
    if not unhashed:
        (info / 'LICENSE').write_bytes(b'PRIVATE LOCAL REPLACEMENT MUST NOT BE EXPORTED')
    result = metadata_probe(metadata_runtime)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    notice = observed['notices'][0]
    assert 'text' not in notice
    assert notice['integrity'] == ('pending' if unhashed else 'failed')
    assert b'PRIVATE LOCAL REPLACEMENT' not in result.stdout
    assert bool(observed['notice_integrity_failures']) is not unhashed


def test_record_path_escape_never_reads_or_exports_external_notice(metadata_runtime, tmp_path):
    secret = tmp_path / 'LICENSE'; secret.write_bytes(b'PRIVATE OUTSIDE RECORD CONTENT')
    metadata_distribution(metadata_runtime[2], unsafe_record='../../../LICENSE')
    result = metadata_probe(metadata_runtime)
    assert result.returncode != 0 and b'unsafe package-relative' in result.stderr
    assert b'PRIVATE OUTSIDE' not in result.stdout
    assert secret.read_bytes() == b'PRIVATE OUTSIDE RECORD CONTENT'


def test_notice_text_limit_retains_exact_hash_and_omission_reason(metadata_runtime):
    raw = b'A' * (256 * 1024 + 1)
    metadata_distribution(metadata_runtime[2], notices={'LICENSE': raw})
    result = metadata_probe(metadata_runtime)
    assert result.returncode == 0, result.stderr
    notice = json.loads(result.stdout)['notices'][0]
    assert notice['sha256'] == hashlib.sha256(raw).hexdigest()
    assert notice['integrity'] == 'record_verified' and notice['text_omission'] == 'per_file_text_bound'
    assert 'text' not in notice


def test_missing_recorded_notice_is_not_silently_filtered_by_metadata(metadata_runtime):
    info = metadata_distribution(metadata_runtime[2], notices={'LICENSE': b'Original recorded public fixture.'})
    (info / 'LICENSE').unlink()
    result = metadata_probe(metadata_runtime)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed['notices'][0]['integrity'] == 'missing'
    assert observed['notices'][0]['text_omission'] == 'recorded_notice_missing'
    assert observed['notice_integrity_failures'] and 'text' not in observed['notices'][0]


def test_linked_notice_cannot_export_an_external_file(metadata_runtime, tmp_path):
    info = metadata_distribution(metadata_runtime[2], notices={'LICENSE': b'Original public fixture.'})
    private = tmp_path / 'private-record'; private.write_bytes(b'PRIVATE LINK TARGET')
    (info / 'LICENSE').unlink()
    try:
        (info / 'LICENSE').symlink_to(private)
    except OSError:
        pytest.skip('This local account cannot create symlinks; path escape is tested independently.')
    result = metadata_probe(metadata_runtime)
    assert result.returncode != 0 and b'escapes its runtime' in result.stderr
    assert b'PRIVATE LINK TARGET' not in result.stdout


def runtime_plan_fixture(tmp_path, monkeypatch):
    root = tmp_path / 'application'; requirements = root / 'scripts/requirements-fixture.txt'
    requirements.parent.mkdir(parents=True); requirements.write_text('owned-package==1.2.3\n')
    lock_path = root / 'forge/locks/native.json'; lock_path.parent.mkdir(parents=True)
    lock_path.write_text('{"owned fixture": true}')
    env = tmp_path / 'data/environments/owned-runtime'; env.mkdir(parents=True)
    wheel = {'package': 'Owned_Package', 'version': '1.2.3', 'name': 'owned_package-1.2.3-py3-none-any.whl',
             'sha256': 'a' * 64, 'size': 123}
    marker = {'schema': 1, 'id': env.name, 'python': '.'.join(map(str, sys.version_info[:3])), 'device': 'cpu',
              'import_tested': True, 'requirements_sha256': acceptance.digest(requirements),
              'dependency_wheels': [wheel], 'sources': []}
    marker_path = env / 'forge-dubbing-runtime.json'; marker_path.write_text(json.dumps(marker))
    selected = {'environment': env, 'python': env / 'bin/python', 'runtime_revision': env.name, 'device': 'cpu'}
    monkeypatch.setattr(acceptance.models, 'ROOT', root)
    monkeypatch.setattr(acceptance.models, 'resolve', lambda *a, **k: selected.copy())
    monkeypatch.setattr(acceptance.models, '_spec', lambda _: {'requirements': 'scripts/requirements-fixture.txt'})
    monkeypatch.setattr(acceptance.models, '_wheel_lock_path', lambda *a: lock_path)
    monkeypatch.setattr(acceptance.models, '_load_wheel_lock', lambda *a: {'wheels': [wheel.copy()]})
    return env, marker, selected


@pytest.mark.parametrize('change', ['requirements_sha256', 'device', 'wheel_hash', 'duplicate_wheel'])
def test_runtime_audit_marker_must_match_actual_requirements_and_native_lock(tmp_path, monkeypatch, change):
    env, marker, _ = runtime_plan_fixture(tmp_path, monkeypatch)
    if change == 'wheel_hash':
        marker['dependency_wheels'][0] = {**marker['dependency_wheels'][0], 'sha256': 'b' * 64}
    elif change == 'duplicate_wheel':
        marker['dependency_wheels'].append(marker['dependency_wheels'][0].copy())
    else:
        marker[change] = 'cuda' if change == 'device' else 'b' * 64
    (env / 'forge-dubbing-runtime.json').write_text(json.dumps(marker))
    with pytest.raises(acceptance.AcceptanceBlocked, match='marker|wheel|duplicated'):
        acceptance._runtime_audit_plan('opus-en-ru', tmp_path / 'data')


def test_shared_runtime_is_audited_once_and_pip_check_failure_cannot_pass(tmp_path, monkeypatch):
    runtime_plan_fixture(tmp_path, monkeypatch)
    calls = []
    def command(arguments, **kwargs):
        calls.append(arguments)
        observed = {'notices': [], 'notice_integrity_failures': [], 'installed_versions': {'owned-package': '1.2.3'},
                    'packages': [], 'packages_without_recorded_notice_files': ['owned-package']}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(observed).encode() if '-c' in arguments else b'No broken requirements found.\n', b'')
    monkeypatch.setattr(acceptance, '_bounded_command', command)
    evidence, texts = acceptance.collect_runtime_evidence(tmp_path / 'data')
    assert len(calls) == 2 and calls[0][1] == '-I' and calls[1][-3:] == ['-m', 'pip', 'check']
    assert evidence['environment_count'] == 1 and evidence['legal_review_status'] == 'pending' and texts == []
    row = evidence['environments']['owned-runtime']
    assert row['components'] == list(acceptance.COMPONENTS)
    assert row['pip_check']['exit_code'] == 0 and row['full_notice_review_status'] == 'pending'
    assert row['wheel_artifacts'][0]['package'] == 'owned-package'
    monkeypatch.setattr(acceptance, '_bounded_command', lambda arguments, **kwargs:
        subprocess.CompletedProcess(arguments, 1 if '-m' in arguments else 0,
            b'PRIVATE PIP CONTEXT' if '-m' in arguments else json.dumps({'notices': []}).encode(), b''))
    with pytest.raises(acceptance.AcceptanceBlocked, match='pip check failed') as error:
        acceptance.collect_runtime_evidence(tmp_path / 'data')
    assert 'PRIVATE PIP CONTEXT' not in str(error.value)


def test_runtime_probe_output_and_time_are_bounded():
    with pytest.raises(acceptance.AcceptanceBlocked, match='bounded output'):
        acceptance._bounded_command([sys.executable, '-I', '-c', 'print("x" * 100000)'], limit=1024)
    with pytest.raises(acceptance.AcceptanceBlocked, match='execution bound'):
        acceptance._bounded_command([sys.executable, '-I', '-c', 'import time;time.sleep(1)'], timeout=.05)


def test_notice_sidecar_is_explicit_bounded_and_never_overwritten(tmp_path):
    records = [{'runtime_revision': 'owned-runtime', 'package': 'owned-package', 'path': 'licenses/LICENSE',
                'size': 200000, 'sha256': 'a' * 64, 'integrity': 'record_verified', 'text': 'A' * 200000}
               for _ in range(12)]
    result = acceptance.write_runtime_notices(tmp_path, records)
    path = tmp_path / acceptance.RUNTIME_NOTICES_NAME
    assert result['size'] <= 2 * 1024 * 1024 and result['sha256'] == acceptance.digest(path)
    assert result['included_texts'] < len(records) and result['omitted_texts'] > 0
    assert json.loads(path.read_text())['legal_review_status'] == 'pending'
    with pytest.raises(FileExistsError):
        acceptance.write_runtime_notices(tmp_path, records)


@pytest.mark.parametrize('change', ['unverified', 'oversized'])
def test_notice_sidecar_refuses_unverified_or_oversized_text(tmp_path, change):
    record = {'integrity': 'pending' if change == 'unverified' else 'record_verified',
              'text': 'Fixture text' if change == 'unverified' else 'A' * (256 * 1024 + 1)}
    with pytest.raises(acceptance.AcceptanceBlocked, match='bounded RECORD-verified'):
        acceptance.write_runtime_notices(tmp_path, [record])
    assert not (tmp_path / acceptance.RUNTIME_NOTICES_NAME).exists()
