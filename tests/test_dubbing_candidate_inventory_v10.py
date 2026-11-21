"""Real candidate identities remain separate from unperformed release acceptance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from forge import dubbing_models as models
from scripts import check_dubbing_release as gate


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = 'forge/dubbing_release_inventory.json'
WHEEL_EVIDENCE = 'docs/V0.10-WHEEL-LICENSE-EVIDENCE.json'


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
    assert candidate['status'] == 'blocked_candidate_inventory'
    assert candidate['license_reviewed'] is False
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
            assert license['reviewed'] is False and license['review_pending'] is True
            assert license['commercial_use'] is None and license['distribution_approved'] is False
            assert license['scope_conflict'] is actual['license_evidence']['scope_conflict']
            assert license['owner_acknowledged'] is False
            assert license['owner_acknowledgement_required'] is license['scope_conflict']
            assert len(license['external_evidence']) == len(actual['external_license_evidence'])
            for notice in license['external_evidence']:
                assert notice['retained_source_file'] == documents[notice['url']]['file']
                assert notice['payload_download_verified'] is True
                assert_retained(notice)
            if license['scope_conflict']:
                assert {item['license_id'] for item in license['external_evidence']} == {'Apache-2.0', 'CC-BY-4.0'}
        runtime = configured['runtime']
        assert runtime['device'] == 'cpu' and runtime['reviewed'] is False
        assert runtime['requirements_sha256'] == sha(runtime['requirements'])
        assert runtime['wheel_inventory_sha256'] == sha(runtime['wheel_inventory'])
        assert runtime['license_evidence_sha256'] == sha(runtime['license_evidence'])


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
        assert lock.get('reviewed') is not True and lock.get('dependency_closure_complete') is not True
        assert lock['model_inference_tested'] is False and lock['physical_gpu_tested'] is False
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


def test_real_candidate_is_blocked_by_model_license_review_gate(tmp_path):
    # A temporary manifest binds the real candidate bytes only to exercise the
    # production validator. It is not a release manifest or acceptance report.
    files = {}
    for name in ('forge/dubbing_models.py', CANDIDATE):
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
        files[name] = gate.digest(destination)
    (tmp_path / 'release-manifest.json').write_text(json.dumps(
        {'app': 'FORGE', 'version': '0.10.0', 'files': files}), 'utf-8')
    with pytest.raises(gate.GateError, match='model/license inventory has not been reviewed'):
        gate.static_inventory(tmp_path, 'v0.10.0', read(CANDIDATE)['source_base_commit'])
