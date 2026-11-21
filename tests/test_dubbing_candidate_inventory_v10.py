"""Real candidate identities remain separate from unperformed release acceptance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from forge import dubbing_models as models
from scripts import check_dubbing_release as gate
from scripts import package_windows_release as package


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = 'forge/dubbing_release_inventory.json'
WHEEL_EVIDENCE = 'docs/V0.10-WHEEL-LICENSE-EVIDENCE.json'
CPU_CLOSURE = 'docs/validation/v0.10/native-cpu-3b1e7c5.dependency-closure.json'


def read(name):
    return json.loads((ROOT / name).read_text('utf-8'))


def sha(name):
    return hashlib.sha256((ROOT / name).read_bytes()).hexdigest()


def assert_retained(document):
    path = document['retained_source_file']
    assert path.startswith('docs/model-notices/')
    assert (ROOT / path).stat().st_size == document['size']
    assert sha(path) == document['sha256']


def test_candidate_matches_fresh_publisher_inventory_and_real_notice_sources():
    candidate = read(CANDIDATE)
    publisher = read('forge/dubbing_publisher_inventory.json')
    legal = read('docs/V0.10-PUBLISHER-LICENSES.json')
    documents = {item['url']: item for item in legal['evidence']}
    assert candidate['schema'] == 1
    assert candidate['status'] == 'license_reviewed_acceptance_pending'
    assert candidate['license_reviewed'] is True
    assert candidate['distribution_approved'] is False
    assert candidate['publisher_inventory_sha256'] == sha(candidate['publisher_inventory'])
    assert candidate['license_evidence_sha256'] == sha(candidate['license_evidence'])
    assert set(candidate['components']) == set(gate.RECOMMENDED)
    for observed in publisher['components']:
        if observed['component'] not in gate.RECOMMENDED:
            continue
        configured = candidate['components'][observed['component']]
        for part, actual in zip(configured['parts'], observed['parts'], strict=True):
            assert part['repository'] == actual['repository']
            assert part['revision'] == actual['revision'] == models.MODEL_PINS[part['repository']]
            expected = {item['name']: item for item in [*actual['artifacts'], *actual['external_license_evidence']]}
            assert {item['path'] for item in part['artifacts']} == set(expected)
            for artifact in part['artifacts']:
                observed_artifact = expected[artifact['path']]
                for key in ('sha256', 'size', 'url'):
                    assert artifact[key] == observed_artifact[key]
                assert artifact['payload_download_verified'] is observed_artifact['content_download_verified']
                if artifact['origin'] == 'checkpoint_snapshot':
                    assert '/resolve/' + part['revision'] + '/' in artifact['url']
                    assert artifact['source_repository'] == part['repository']
                    assert artifact['source_revision'] == part['revision']
                else:
                    assert artifact['origin'] == 'external_license_evidence'
                    assert artifact['path'].startswith('notices/')
                    assert artifact['url'].startswith('https://raw.githubusercontent.com/')
                    assert '/' + artifact['source_revision'] + '/' in artifact['url']
            license = part['license']
            assert license['id'] == actual['license']
            assert license['reviewed'] is True and license['review_pending'] is False
            assert license['commercial_use'] is True and license['distribution_approved'] is False
            assert license['declared_spdx_id'] == 'Apache-2.0' and license['scope_review_completed'] is True
            assert license['scope_conflict'] is actual['license_evidence']['scope_conflict']
            assert 'owner_acknowledged' not in license
            assert 'owner_acknowledgement_required' not in license
            assert len(license['external_evidence']) == len(actual['external_license_evidence'])
            for notice in license['external_evidence']:
                assert notice['retained_source_file'] == documents[notice['url']]['file']
                assert notice['payload_download_verified'] is True
                assert_retained(notice)
            if license['scope_conflict']:
                assert {item['license_id'] for item in license['external_evidence']} == {'Apache-2.0', 'CC-BY-4.0'}
                assert license['notice_obligation_expression'] == 'Apache-2.0 AND CC-BY-4.0'
                assert {item['id'] for item in license['applied_terms']} == {'Apache-2.0', 'CC-BY-4.0'}
            else:
                assert {item['id'] for item in license['applied_terms']} == {'Apache-2.0'}
            assert license['attribution_notice'] == legal['application_attribution_notice']
        runtime = configured['runtime']
        assert runtime['device'] == 'cpu' and runtime['reviewed'] is True
        assert runtime['requirements_sha256'] == sha(runtime['requirements'])
        assert runtime['wheel_inventory_sha256'] == sha(runtime['wheel_inventory'])
        assert runtime['license_evidence_sha256'] == sha(runtime['license_evidence'])
        assert runtime['dependency_closure_evidence'] == CPU_CLOSURE
        assert runtime['dependency_closure_evidence_sha256'] == sha(CPU_CLOSURE)
        assert runtime['observed_cpu_runtime_id'] == read(CPU_CLOSURE)['runtime_id']
        assert runtime['observed_cpu_source_commit'] == read(CPU_CLOSURE)['source_commit']
        assert runtime['dependency_closure_complete'] is True and runtime['model_inference_tested'] is True


def test_scoped_model_review_delivers_attribution_terms_and_unchanged_lineage_in_windows_payload():
    legal = read('docs/V0.10-PUBLISHER-LICENSES.json')
    assert legal['model_license_scopes_reviewed'] is True
    assert legal['status'] == 'model_and_cpu_runtime_scopes_reviewed'
    assert legal['distribution_approved'] is False
    assert legal['opus']['scope_reviewed'] is legal['qwen']['scope_reviewed'] is True
    assert legal['opus']['commercial_use_permitted_subject_to_terms'] is legal['qwen']['commercial_use_permitted_subject_to_terms'] is True
    assert legal['opus']['checkpoint_card_declared_license'] == 'Apache-2.0'
    assert legal['opus']['original_pretrained_model_terms'] == 'CC-BY-4.0'
    assert legal['opus']['source_code_terms_separate'] == 'MIT'
    notice = legal['application_attribution_notice']
    assert (ROOT / notice['file']).stat().st_size == notice['size']
    assert sha(notice['file']) == notice['sha256']
    text = (ROOT / notice['file']).read_text('utf-8')
    for expected in ('bb09c99d180016eac6819df3dae68edb1690fdee', 'opus-2020-02-11.zip',
                     '5d83992436eae1d760afd27aff78a71d676296fc', 'CC-BY-4.0',
                     'Copyright 2026 Alibaba Cloud', 'downloads and installs',
                     'unchanged', 'endorsement', 'FORGE code'):
        assert expected in text
    delivery = legal['notice_delivery']
    assert delivery['packaging_policy_checked'] is True and delivery['normal_updater_payload'] is True
    assert delivery['exact_release_archive_verified'] is False and delivery['no_model_weights_bundled'] is True
    assert set(delivery['source_and_windows_payload_files']) == {notice['file'], *[item['file'] for item in legal['evidence']]}
    for document in [notice, *legal['evidence']]:
        assert sha(document['file']) == document['sha256']
        assert (ROOT / document['file']).stat().st_size == document['size']
        assert str(package.payload_path(document['file'])) == document['file']


def test_primary_wheel_license_labels_are_bound_to_exact_wheels_and_retained_text():
    evidence = read(WHEEL_EVIDENCE)
    assert evidence['native_execution_performed_by_this_review'] is False
    assert evidence['dependency_closure_approved'] is False
    assert evidence['distribution_approved'] is False
    assert sum(item['wheel_size'] for item in evidence['records']) == evidence['wheel_bytes_verified']
    records = {item['wheel_sha256']: item for item in evidence['records']}
    upstream = {item['package']: item for item in evidence['upstream_notice_evidence']}
    assert len(records) == 8
    for record in records.values():
        assert record['native_windows_execution'] is False and record['installed'] is False
        assert record['native_execution_or_distribution_approval'] is False
        assert models.SHA256.fullmatch(record['wheel_sha256'])
        assert record['wheel_size'] > 0 and record['url'].startswith('https://')
        if record['full_license_absent_from_wheel']:
            assert record['package'] in {'tokenizers', 'sentencepiece'}
            assert not record['license_files']
            source = upstream[record['package']]
            assert source['version'] == record['version']
            assert source['license'] == record['primary_license_identified']
            assert '/' + source['source_revision'] + '/' in source['url']
            assert_retained(source)
        else:
            assert record['license_files']
            for notice in record['license_files']:
                assert_retained(notice)
    for source in upstream.values():
        for notice in source.get('supplemental_source_notices', []):
            if notice.get('full_license_text') is False:
                assert 'retained_source_file' not in notice
                continue
            assert '/' + source['source_revision'] + '/' in notice['url']
            assert_retained(notice)
    for descriptor in evidence['updated_wheel_locks']:
        assert descriptor['sha256'] == sha(descriptor['path'])
        assert descriptor['review_flags_promoted'] is False
        lock = read(descriptor['path'])
        assert lock['native_windows_execution'] is True and lock['cross_resolved'] is False
        assert lock['physical_gpu_tested'] is False
        if lock['device'] == 'cpu':
            assert lock['reviewed'] is True and lock['license_reviewed'] is True
            assert lock['dependency_closure_complete'] is True and lock['model_inference_tested'] is True
            assert lock['dependency_closure_evidence'] == descriptor['external_execution_evidence'] == CPU_CLOSURE
            assert lock['dependency_closure_evidence_sha256'] == descriptor['external_execution_evidence_sha256'] == sha(CPU_CLOSURE)
        else:
            assert lock.get('reviewed') is not True
            assert lock.get('dependency_closure_complete') is not True and lock['model_inference_tested'] is False
            assert 'external_execution_evidence' not in descriptor
        changes = {item['wheel_sha256']: item for item in descriptor['changes']}
        assert len(changes) == 7
        for wheel in lock['wheels']:
            if wheel['sha256'] not in changes:
                continue
            record, change = records[wheel['sha256']], changes[wheel['sha256']]
            assert wheel['name'] == record['wheel'] and wheel['version'] == record['version']
            assert wheel['size'] == record['wheel_size']
            assert change['previous_license'] == 'See publisher LICENSE'
            assert wheel['license'] == change['license'] == record['primary_license_identified']


def test_cpu_closure_binds_real_native_report_source_wheels_and_runtime_recipe():
    closure = read(CPU_CLOSURE)
    report = read(closure['report']['path'])
    provenance = read(closure['report_provenance']['path'])
    observed = closure['observed_wheel_lock']
    lock = read(observed['path'])
    assert closure['kind'] == 'native_cpu_dependency_closure' and closure['status'] == 'verified'
    assert closure['source_commit'] == report['source_commit'] == provenance['source_commit'] == '3b1e7c5f0d625ede7403b9c1d1b069c93481498f'
    assert closure['run_id'] == provenance['run_id'] == 44854764936
    assert closure['report']['sha256'] == provenance['report_sha256'] == sha(closure['report']['path'])
    assert closure['report']['size'] == provenance['report_bytes'] == (ROOT / closure['report']['path']).stat().st_size
    assert closure['report_provenance']['sha256'] == sha(closure['report_provenance']['path'])
    assert closure['retained_actions_archive']['sha256'] == provenance['archive_sha256'] == 'a0427acd9be621b3bb8ce6e197e5c1b00aa8902ab103f6844571eab6938ef7f1'
    assert report['status'] == 'passed' and report['native_windows_execution'] is True
    assert report['source_checkout_clean'] is True and report['inference_substituted'] is False
    assert closure['source_manifest_sha256'] == report['source_manifest_sha256']
    assert closure['source_files_sha256'] == report['source_files_sha256']
    assert set(closure['source_files_sha256']) == {
        'forge/dubbing_models.py', 'scripts/dubbing_runtime.py', 'scripts/dubbing_cpu_acceptance.py'}
    assert all(models.SHA256.fullmatch(value) for value in closure['source_files_sha256'].values())
    assert observed['source_commit'] == closure['source_commit']
    assert observed['wheel_count'] == len(lock['wheels']) == 60
    assert observed['download_bytes'] == lock['download_bytes'] == sum(item['size'] for item in lock['wheels'])
    audit = read('docs/V0.10-RUNTIME-LICENSE-AUDIT.json')
    original_lock = read(audit['native_runtime_evidence']['observed_lock']['path'])
    assert observed['canonical_wheels_sha256'] == hashlib.sha256(models._canonical(original_lock['wheels'])).hexdigest()
    fields = ('package', 'version', 'name', 'sha256', 'size', 'url')
    assert [[row[key] for key in fields] for row in lock['wheels']] == [[row[key] for key in fields] for row in original_lock['wheels']]
    # Reconstruct the exact observed lock by undoing only this later evidence
    # annotation. Wheel bytes, resolver metadata, and all CUDA flags stay fixed.
    original = dict(original_lock)
    for key in ('dependency_closure_complete', 'reviewed', 'dependency_closure_evidence',
                'dependency_closure_evidence_sha256', 'model_inference_evidence', 'model_inference_evidence_sha256'):
        original.pop(key)
    original['model_inference_tested'] = False
    original_bytes = (json.dumps(original, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
    assert observed['sha256'] == hashlib.sha256(original_bytes).hexdigest() == '870b40584f90451550005ce30d9369ad5ba573648e397a3270633403de41fd5c'
    recipe = {'requirements': lock['requirements_sha256'],
              'wheels': [[item['package'], item['version'], item['sha256']] for item in lock['wheels']],
              'sources': models._spec('opus-en-ru')['sources'], 'device': 'cpu', 'python': report['platform']['python']}
    assert recipe == closure['runtime_recipe']
    assert closure['requirements']['sha256'] == lock['requirements_sha256'] == sha(closure['requirements']['path'])
    assert closure['runtime_recipe_sha256'] == hashlib.sha256(models._canonical(recipe)).hexdigest()
    assert closure['runtime_id'] == 'dubbing-core-' + closure['runtime_recipe_sha256'][:16]
    for component, row in report['components'].items():
        assert models._spec(component)['sources'] == recipe['sources']
        assert row['verification']['status'] == 'passed' and row['inference']['inference_tested'] is True
        assert row['inference']['actual_device'] == 'cpu' and row['inference']['physical_gpu_tested'] is False
        assert row['verification']['runtime_revision'] == row['inference']['runtime_revision'] == closure['runtime_id']
        assert report['installed_identities'][component]['runtime_revision'] == closure['runtime_id']
        assert row['verification']['model_revision'] == models.MODEL_PINS[models._spec(component)['parts'][0]['repository']]
    assert sum(row['verification']['files_verified'] for row in report['components'].values()) == closure['model_files_and_notices_verified'] == 30
    assert closure['verified_after_fresh_controller'] is report['verified_after_fresh_controller'] is True
    assert closure['dependency_closure_complete'] is closure['model_inference_tested'] is True
    assert closure['reviewed'] is closure['distribution_approved'] is closure['physical_gpu_tested'] is False
    assert closure['raw_pip_stdout_retained'] is closure['installed_environment_receipt_retained'] is False
    assert closure['satisfies_release_acceptance'] is report['satisfies_release_acceptance'] is False
    assert lock['model_inference_evidence'] == closure['report']['path']
    assert lock['model_inference_evidence_sha256'] == closure['report']['sha256']
    linked = read(WHEEL_EVIDENCE)['native_cpu_dependency_closure']
    assert linked['sha256'] == sha(CPU_CLOSURE) and linked['path'] == CPU_CLOSURE
    assert linked['observed_lock_sha256'] == observed['sha256']
    assert linked['canonical_wheels_sha256'] == observed['canonical_wheels_sha256']
    assert linked['runtime_id'] == closure['runtime_id'] and linked['reviewed'] is False


def test_reviewed_real_candidate_still_requires_separate_complete_release_acceptance(tmp_path):
    # This temporary manifest exercises the real reviewed inventory. It is not
    # a release artifact or a replacement for native/hardware/account evidence.
    files = {}
    names = {'forge/dubbing_models.py', CANDIDATE}
    candidate = read(CANDIDATE)
    audit_name = candidate['runtime_license_review']['path']
    names.add(audit_name)
    pending = [read(audit_name)]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key in ('path', 'file', 'retained_source_file'):
                name = node.get(key)
                if isinstance(name, str) and name.startswith('docs/'):
                    names.add(name)
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    for component in candidate['components'].values():
        names.update((component['runtime']['requirements'], component['runtime']['wheel_inventory']))
        for part in component['parts']:
            names.add(part['license']['attribution_notice']['file'])
            names.update(item['retained_source_file'] for item in part['license']['external_evidence'])
    for name in names:
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
        files[name] = gate.digest(destination)
    (tmp_path / 'release-manifest.json').write_text(json.dumps(
        {'app': 'FORGE', 'version': '0.10.0', 'files': files}), 'utf-8')
    commit = candidate['source_base_commit']
    assert gate.static_inventory(tmp_path, 'v0.10.0', commit)['status'] == 'inventory_verified'
    with pytest.raises(gate.GateError, match='missing DUBBING_ACCEPTANCE.json'):
        gate.verify_evidence(tmp_path, 'v0.10.0', commit, None, run_id='1')
