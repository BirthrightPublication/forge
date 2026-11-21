"""Scoped CPU license evidence; no binary redistribution or GPU approval."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import pytest

from forge import dubbing_models as models


ROOT = Path(models.__file__).resolve().parents[1]
AUDIT = 'docs/V0.10-RUNTIME-LICENSE-AUDIT.json'
CPU_LOCK = 'forge/dubbing_wheel_locks/requirements-dubbing-core-windows-x64-py312-cpu.json'
CUDA_LOCK = 'forge/dubbing_wheel_locks/requirements-dubbing-core-windows-x64-py312-cuda.json'
IDENTITY_FIELDS = ('package', 'version', 'name', 'sha256', 'size', 'url')


def read(name):
    return json.loads((ROOT / name).read_text('utf-8'))


def sha(name):
    return hashlib.sha256((ROOT / name).read_bytes()).hexdigest()


def normal(name):
    return re.sub('[-_.]+', '-', name).lower()


def checked_reference(reference):
    path = ROOT / reference['path']
    assert path.is_file() and not path.is_symlink()
    assert sha(reference['path']) == reference['sha256']
    if 'size' in reference:
        assert path.stat().st_size == reference['size']
    return read(reference['path'])


def test_runtime_review_has_explicit_source_zip_and_private_local_cpu_scope():
    audit, cpu, cuda = read(AUDIT), read(CPU_LOCK), read(CUDA_LOCK)
    assert audit['schema'] == 1 and audit['kind'] == 'scoped_native_cpu_runtime_license_review'
    assert audit['status'] == 'reviewed_for_source_zip_and_private_local_installation'
    assert audit['reviewed'] is audit['license_reviewed'] is True
    for key in ('distribution_approved', 'binary_redistribution_approved', 'physical_gpu_tested', 'cuda_reviewed'):
        assert audit[key] is False
    scope = audit['scope']
    assert 'source' in scope['application_distribution'] and 'zero dependency wheels' in scope['application_distribution']
    assert 'private local' in scope['runtime_installation'] and 'unchanged' in scope['runtime_installation']
    assert 'Internal/local' in scope['commercial_use'] and 'SaaS/service-bureau' in scope['commercial_use']
    assert 'not approved' in scope['future_bundling'] and 'MIT' in scope['application_license']
    assert cpu['reviewed'] is cpu['license_reviewed'] is True
    assert cpu['model_inference_tested'] is cpu['dependency_closure_complete'] is True
    assert cpu['distribution_approved'] is cpu['physical_gpu_tested'] is False
    assert cpu['license_review_evidence'] == AUDIT and cpu['license_review_evidence_sha256'] == sha(AUDIT)
    assert cuda.get('reviewed') is not True
    # A separate CUDA-license-only appendix may be reviewed without runtime,
    # closure, inference, binary distribution, or physical GPU acceptance.
    assert cuda.get('distribution_approved') is not True and cuda['physical_gpu_tested'] is False
    assert cuda['model_inference_tested'] is False and cuda.get('dependency_closure_complete') is not True


def test_all_sixty_reviewed_package_identities_match_native_install_and_unchanged_publisher_artifacts():
    audit, lock = read(AUDIT), read(CPU_LOCK)
    native = audit['native_runtime_evidence']
    report = checked_reference(native['report'])
    runtime = next(iter(report['runtime_evidence']['environments'].values()))
    observed = checked_reference(native['observed_lock'])
    packages = {normal(row['package']): row for row in audit['packages']}
    wheels = {normal(row['package']): row for row in lock['wheels']}
    installed = {row['package']: row for row in runtime['wheel_artifacts']}
    assert len(packages) == len(wheels) == len(installed) == native['registered_wheel_count'] == 60
    assert set(packages) == set(wheels) == set(installed)
    assert [{key: row[key] for key in IDENTITY_FIELDS} for row in lock['wheels']] == [
        {key: row[key] for key in IDENTITY_FIELDS} for row in observed['wheels']]
    identities = [{key: row[key] for key in IDENTITY_FIELDS} for row in lock['wheels']]
    assert native['wheel_artifact_identity_sha256'] == hashlib.sha256(
        json.dumps(identities, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    for name, package in packages.items():
        wheel, artifact = wheels[name], installed[name]
        assert {key: package[key] for key in IDENTITY_FIELDS} == {key: wheel[key] for key in IDENTITY_FIELDS}
        assert {key: artifact[key] for key in ('version', 'name', 'sha256', 'size')} == {
            key: wheel[key] for key in ('version', 'name', 'sha256', 'size')}
        assert runtime['installed_versions'][name] == wheel['version']
        assert package['reviewed'] is True and package['primary_license'] == wheel['license']
        assert wheel['license_scope'] == 'primary_package'
        assert wheel['bundled_license_scopes'] == package['bundled_license_scopes']
        assert not re.search(r'unknown|see\s+(?:the\s+)?(?:publisher|package)', package['primary_license'], re.I)
        assert 'not a complete binary' in package['primary_label_scope']
        assert not any(row['path'].endswith('.pyc') for row in package['installed_notice_files'])


def test_native_cpu_report_provenance_and_runtime_references_are_exact_and_actual():
    evidence = read(AUDIT)['native_runtime_evidence']
    report = checked_reference(evidence['report'])
    provenance = checked_reference(evidence['provenance'])
    observed = checked_reference(evidence['observed_lock'])
    runtime = next(iter(report['runtime_evidence']['environments'].values()))
    assert report['status'] == 'passed' and report['native_windows_execution'] is True
    assert report['requested_device'] == 'cpu' and report['inference_substituted'] is False
    assert report['physical_gpu_tested'] is report['satisfies_release_acceptance'] is False
    assert evidence['source_commit'] == report['source_commit'] == provenance['source_commit']
    assert evidence['run_id'] == provenance['run_id'] and evidence['artifact_id'] == provenance['artifact_id']
    assert evidence['archive_sha256'] == provenance['archive_sha256']
    assert provenance['files']['DUBBING_MODEL_SMOKE.json']['sha256'] == evidence['report']['sha256']
    assert evidence['notice_sidecar'] == report['runtime_notice_artifact']
    assert evidence['notice_sidecar']['sha256'] == provenance['files']['DUBBING_RUNTIME_NOTICES.json']['sha256']
    assert evidence['notice_sidecar']['size'] == provenance['files']['DUBBING_RUNTIME_NOTICES.json']['size']
    assert evidence['observed_lock']['sha256'] == runtime['native_wheel_lock']['sha256']
    assert observed['wheels'] and evidence['runtime_id'] == runtime['runtime_revision']
    assert evidence['pip_check'] == runtime['pip_check'] and evidence['pip_check']['exit_code'] == 0
    assert evidence['notice_integrity_failures'] == runtime['notice_integrity_failures'] == []
    assert evidence['installed_package_count_including_pip'] == len(runtime['installed_versions']) == 61
    assert report['verified_after_fresh_controller'] is True
    for row in report['components'].values():
        assert row['verification']['status'] == 'passed' and row['inference']['inference_tested'] is True
        assert row['inference']['actual_device'] == 'cpu' and row['inference']['physical_gpu_tested'] is False
        assert row['inference']['runtime_revision'] == evidence['runtime_id']


@pytest.mark.parametrize('package,terms', [
    ('numpy', ('OpenBLAS', 'GCC-exception-3.1', 'LGPL-2.1-or-later')),
    ('scipy', ('OpenBLAS', 'GCC-exception-3.1', 'Qhull')),
    ('av', ('BSD-3-Clause', 'GPL-2.0-or-later', 'FFmpeg', 'x264/x265')),
    ('llvmlite', ('Apache-2.0 WITH LLVM-exception', 'Microsoft')),
    ('soundfile', ('libsndfile', 'LGPL-2.1-or-later', 'LAME/mpg123')),
    ('ctranslate2', ('MIT', 'Intel-Developer-Tools', 'Intel-Simplified', 'NVIDIA-SDK-cuDNN', 'oneDNN')),
])
def test_primary_labels_do_not_replace_separately_reviewed_bundled_terms(package, terms):
    row = next(row for row in read(AUDIT)['packages'] if normal(row['package']) == package)
    text = json.dumps(row['bundled_license_scopes'], ensure_ascii=False)
    assert all(term in text for term in terms)
    assert row['primary_label_scope'] == 'Package primary terms only; not a complete binary SPDX/SBOM expression.'


def test_native_legal_source_report_is_integrity_bound_and_keeps_original_archive_hashes():
    evidence = read(AUDIT)['native_legal_evidence']
    report = checked_reference(evidence['report'])
    provenance = checked_reference(evidence['provenance'])
    assert report['status'] == 'identities_verified_legal_review_pending' and report['errors'] == []
    assert report['source_commit'] == provenance['source_commit'] == evidence['source_commit']
    assert evidence['run_id'] == provenance['run_id'] and evidence['artifact_id'] == provenance['artifact_id']
    assert evidence['archive_sha256'] == provenance['archive_sha256']
    assert evidence['report']['sha256'] == provenance['report_sha256']
    assert evidence['report']['size'] == provenance['report_bytes']
    assert evidence['all_original_artifact_hashes_preserved'] is True
    observed = {row['component']: row for row in report['downloads'] if row['kind'] == 'verified_archive'}
    for archive in evidence['source_archives']:
        row = observed[archive['component']]
        assert (archive['sha256'], archive['size'], archive['url']) == (row['sha256'], row['size'], row['url'])
        assert archive['full_text_rehosted_in_repository'] is False
        assert archive['archive_index'] == row['archive_index']
        assert archive['archive_index']['legal_candidates_complete'] is True
        assert archive['archive_index']['legal_candidate_count'] == len(archive['archive_index']['legal_candidates'])


@pytest.mark.parametrize('component,archive_sha,archive_size,tpp_sha,tpp_size', [
    ('Intel-OpenMP-runtime', 'c351b0f0472c99de1f989001baf07847036158579706cb97cba35be7160f85e4', 3392252,
     '7723b626fe7313c8f8a24143e9bfa008874241aaf54432ea22fd50387c9f1ddf', 71667),
    ('Intel-MKL-legal-source', '0a227354489cc3b5003ae58d984ad10d37ff6dd3f6dbcdda0875251f874cf591', 794943,
     '681067a018380121e39a9ca4cce0325db0f7bf94f620a2407742b07bd78a4850', 48487),
])
def test_original_intel_grants_and_full_tpp_have_explicit_completed_private_review(component, archive_sha, archive_size, tpp_sha, tpp_size):
    audit = read(AUDIT)
    archive = next(row for row in audit['native_legal_evidence']['source_archives'] if row['component'] == component)
    assert archive['sha256'] == archive_sha and archive['size'] == archive_size
    evidence = audit['proprietary_full_term_evidence'][component]
    assert evidence['archive_sha256'] == archive_sha
    documents = {row['member']: row for row in evidence['license_documents']}
    assert {'info/licenses/license.txt', 'info/licenses/tpp.txt'} <= set(documents)
    assert documents['info/licenses/tpp.txt']['sha256'] == tpp_sha
    assert documents['info/licenses/tpp.txt']['size'] == tpp_size
    assert any(row['path'] == 'info/licenses/tpp.txt' and row['kind'] == 'file' and row['size'] == tpp_size
               for row in evidence['legal_candidates'])
    review = evidence['third_party_notice_review']
    assert review['member'] == 'info/licenses/tpp.txt' and review['sha256'] == tpp_sha and review['size'] == tpp_size
    assert review['reviewed'] is review['raw_bytes_unchanged'] is True
    assert review['full_text_rehosted'] is False and review['review_method']
    if component == 'Intel-OpenMP-runtime':
        assert 'windows-1252' in review['review_decoding'] and 'no publisher encoding declaration inferred' in review['review_decoding']


def test_proprietary_terms_are_identity_records_not_rehosted_full_texts_or_private_workspace_paths():
    audit = read(AUDIT)
    text = (ROOT / AUDIT).read_text('utf-8')
    assert '/workspace/' not in text and 'observed_local_file' not in text and 'recovered_text' not in text
    assert audit['nvidia_cudnn_evidence']['distribution_approved'] is False
    assert audit['nvidia_cudnn_evidence']['archive_bytes_verified'] is True
    assert audit['nvidia_cudnn_evidence']['matching_binary_verified'] is True
    proprietary = {(row['size'], row['sha256'])
                   for component in audit['proprietary_full_term_evidence'].values()
                   for row in component['license_documents']}
    proprietary.update((row['size'], row['sha256']) for row in audit['nvidia_cudnn_evidence']['records']
                       if row['kind'] == 'legal_document')
    sizes = {size for size, _ in proprietary}
    # Check the actual tracked application payload inventory, not filenames or
    # fragments that could mistake public source/license links for full bodies.
    for name in read('release-manifest.json')['files']:
        path = ROOT / name
        if path.stat().st_size in sizes:
            assert (path.stat().st_size, sha(name)) not in proprietary
    for record in audit['nvidia_cudnn_evidence']['records']:
        assert 'file' not in record
        if record['kind'] == 'matching_dispatcher_identity_only':
            assert record['executed'] is False and record['retained'] is False


def test_recovered_large_legal_texts_remain_bound_to_actual_native_notice_identities():
    audit = read(AUDIT)
    report = checked_reference(audit['native_runtime_evidence']['report'])
    runtime = next(iter(report['runtime_evidence']['environments'].values()))
    notices = {(row['package'], row['path']): row for row in runtime['notice_files']}
    recovered = audit['exact_recovered_omitted_texts']
    assert {row['package'] for row in recovered} == {'torch', 'onnxruntime', 'soxr'}
    for row in recovered:
        original = notices[(row['package'], row['native_path'])]
        assert (row['size'], row['sha256']) == (original['size'], original['sha256'])
        assert 'recovered_text' not in row
    reconciliation = audit['public_wheel_reconciliation']
    assert reconciliation['wheel_count'] == 59 and reconciliation['mismatches'] == reconciliation['expected_notice_gaps'] == 0
    recovery = audit['torch_notice_recovery']
    assert recovery['matches_native_RECORD_verified_notice'] is True
    assert recovery['whole_wheel_downloaded_here'] is False
    assert recovery['whole_wheel_independently_verified_by_native_installer'] is True
    assert recovery['native_report_sha256'] == audit['native_runtime_evidence']['report']['sha256']
    assert recovery['native_source_commit'] == audit['native_runtime_evidence']['source_commit']
    assert recovery['target_size'] == 506902 and recovery['target_sha256'] == recovery['native_recorded_notice']['sha256']


def test_cuda_license_only_review_preserves_exact_artifacts_and_requires_owner_runtime_acceptance():
    audit, cpu, cuda = read(AUDIT), read(CPU_LOCK), read(CUDA_LOCK)
    appendix = audit['cuda_license_review']
    delta = checked_reference(appendix)
    assert appendix['reviewed'] is cuda['license_reviewed'] is True
    assert appendix['status'] == 'license_only_reviewed_native_gpu_pending'
    for key in ('dependency_closure_complete', 'model_inference_tested', 'physical_gpu_tested', 'binary_redistribution_approved'):
        assert appendix[key] is False
    assert cuda['reviewed'] is cuda['dependency_closure_complete'] is cuda['model_inference_tested'] is cuda['physical_gpu_tested'] is False
    assert cuda['license_review_evidence'] == AUDIT and cuda['license_review_evidence_sha256'] == sha(AUDIT)
    assert delta['cuda_optional_private_install_scope_reviewed'] is delta['source_zip_scope_reviewed'] is True
    assert delta['distribution_approved'] is delta['dependency_closure_complete'] is delta['physical_gpu_tested'] is False
    observed = {row['package']: row for row in delta['cuda_wheels']}
    cpu_artifacts = {tuple(row[key] for key in IDENTITY_FIELDS) for row in cpu['wheels']}
    assert sum(tuple(row[key] for key in IDENTITY_FIELDS) in cpu_artifacts for row in cuda['wheels']) == appendix['shared_cpu_artifact_count'] == 58
    assert set(observed) == {'torch', 'torchaudio'} and appendix['delta_artifact_count'] == 2
    for wheel in cuda['wheels']:
        assert wheel['license_scope'] == 'primary_package'
        assert not re.search(r'unknown|see\s+(?:the\s+)?(?:publisher|package)', wheel['license'], re.I)
        if wheel['package'] in observed:
            proof = observed[wheel['package']]
            assert proof['full_payload_size_and_sha256_verified'] is True
            assert {key: proof[key] for key in IDENTITY_FIELDS} == {key: wheel[key] for key in IDENTITY_FIELDS}
    assert 'mutable' in delta['nvidia_legal_metadata']['identity_scope']


def test_both_torch_variants_match_the_official_intel_binary_and_preserve_separate_sdk_terms():
    audit = read(AUDIT)
    cpu = checked_reference(audit['cpu_torch_openmp_binary_evidence'])
    delta = checked_reference(audit['cuda_license_review'])
    intel = delta['intel_evidence']
    assert cpu['original_fully_verified_before_inspection'] is True
    assert cpu['native_binary_execution'] is cpu['package_installation'] is cpu['model_inference'] is False
    assert intel['version'] == '2025.2.0' and intel['wheel_sha256'] == '1710356ae0db744ca028ed380759a2007548ad1819f743be9d675603cb127377'
    assert intel['full_payload_size_and_sha256_verified'] is True
    assert audit['torch_intel_binary_association']['both_cpu_and_cuda_exact_binary_matches'] is True
    exact = {row['member'].rsplit('/', 1)[-1]: (row['size'], row['sha256']) for row in intel['matching_native_files']}
    assert {row['member'].rsplit('/', 1)[-1]: (row['size'], row['sha256']) for row in cpu['pe_records']} == exact
    assert all(row['exact_match_to_torch_cuda'] is True for row in intel['matching_native_files'])
    assert {(row['size'], row['sha256']) for row in intel['notices']} == {
        (25224, 'febfb69a5b058f55892dc7f341108a9ca29d50e9fe85723a57eebbb826436958'),
        (31578, 'f8ce918fe7311ce279e68380a2e233f8a42b1cd3dda8f4c48d6de97a0255c1d7')}
