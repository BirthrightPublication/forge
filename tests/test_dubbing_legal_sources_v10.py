"""Synthetic legal-source verifier fixtures; never native or license approval."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tarfile
from types import SimpleNamespace
import urllib.error
import zipfile

import pytest

from scripts import dubbing_legal_sources as legal


def sha(data):
    return hashlib.sha256(data).hexdigest()


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()


def descriptor(data, **changes):
    return {'url': 'https://publisher.example.test/immutable-source.tar.gz',
            'sha256': sha(data), 'expected_size': len(data), 'max_bytes': len(data), **changes}


@pytest.fixture
def opener(monkeypatch):
    """An ordinary urllib response fixture; no network or TLS replacement."""
    state = SimpleNamespace(data=b'Owned public-source fixture.', headers={}, requests=[], chunks=[],
                            read_error_at=None, on_read=None)

    class Response(io.BytesIO):
        def __init__(self):
            super().__init__(state.data)
            self.headers = state.headers

        def read(self, limit=-1):
            if state.on_read is not None:
                state.on_read()
            if state.read_error_at is not None and self.tell() >= state.read_error_at:
                raise OSError('Owned synthetic transfer interruption.')
            value = super().read(min(limit, 7) if limit >= 0 else 7)
            state.chunks.append(len(value))
            return value

    class Opener:
        def open(self, request, timeout):
            state.requests.append((request, timeout))
            return Response()

    monkeypatch.setattr(legal.urllib.request, 'build_opener', lambda *args: Opener())
    return state


def zip_fixture(path, members):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, value in members:
            archive.writestr(name, value)
    return path


def tar_fixture(path, members):
    with tarfile.open(path, 'w:gz') as archive:
        for name, data in members:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return path


def test_immutable_identity_checks_sha_size_and_git_blob_without_executing_text():
    data = b'raise AssertionError("Fixture text must never be executed")\n'
    expected = {'expected_size': len(data), 'expected_sha256': sha(data), 'expected_git_blob_sha1': blob(data)}
    assert legal.identity(data, expected) == {'size': len(data), 'sha256': sha(data), 'git_blob_sha1': blob(data)}


@pytest.mark.parametrize('fault', [
    {'expected_size': True}, {'expected_size': 1}, {'expected_sha256': 'f' * 64},
    {'expected_sha256': 'mutable-main'}, {'expected_git_blob_sha1': 'a' * 40},
    {'expected_git_blob_sha1': 'main'},
])
def test_mismatched_immutable_evidence_is_rejected(fault):
    data = b'Synthetic source identity.'
    with pytest.raises(ValueError):
        legal.identity(data, {'expected_size': len(data), 'expected_sha256': sha(data), **fault})


@pytest.mark.parametrize('value', ['', '.', '..', '../LICENSE', '/LICENSE', 'C:/LICENSE',
                                  'vendor/../LICENSE', 'vendor/./LICENSE', 'vendor//LICENSE',
                                  'vendor\\LICENSE', 'vendor/LICENSE\x00'])
def test_unsafe_legal_evidence_paths_are_rejected(value):
    with pytest.raises(ValueError):
        legal.relative(value)


def test_safe_legal_evidence_path_keeps_exact_spelling():
    assert str(legal.relative('release/COPYING.LGPLv3')) == 'release/COPYING.LGPLv3'


@pytest.mark.parametrize('value', ['http://publisher.example.test/source', 'file:///tmp/source',
                                  'https://user:password@publisher.example.test/source',
                                  'https://publisher.example.test/source#fragment',
                                  'https://publisher.example.test/with space'])
def test_downloads_require_plain_https_without_embedded_credentials(value):
    with pytest.raises(ValueError):
        legal.safe_url(value)


def test_verified_download_is_bounded_and_preserves_original_bytes(tmp_path, opener):
    target = tmp_path / 'source.verified-archive'
    opener.headers = {'Content-Length': str(len(opener.data))}
    budget = {'limit': 128, 'used': 0}
    result = legal.download(descriptor(opener.data), target, budget)
    assert target.read_bytes() == opener.data
    assert result['sha256'] == sha(opener.data) and result['size'] == len(opener.data)
    assert result['independent_expected_size_available'] is True
    assert budget['used'] == len(opener.data) and opener.requests
    assert opener.requests[0][0].full_url == 'https://publisher.example.test/immutable-source.tar.gz'


def test_unpublished_archive_size_does_not_weaken_exact_hash_check(tmp_path, opener):
    result = legal.download(descriptor(opener.data, expected_size=None, max_bytes=128),
                            tmp_path / 'source', {'limit': 128, 'used': 0})
    assert result['independent_expected_size_available'] is False
    assert result['expected_size'] is None and result['sha256'] == sha(opener.data)


@pytest.mark.parametrize('fault', ['sha', 'size', 'content_length', 'empty'])
def test_corrupt_or_truncated_download_never_becomes_verified(tmp_path, opener, fault):
    item = descriptor(opener.data)
    if fault == 'sha':
        item['sha256'] = 'f' * 64
    elif fault == 'size':
        item['expected_size'] += 1
        item['max_bytes'] += 1
    elif fault == 'content_length':
        opener.headers = {'Content-Length': str(len(opener.data) + 1)}
    else:
        opener.data = b''
    target = tmp_path / 'must-not-be-verified'
    with pytest.raises(ValueError):
        legal.download(item, target, {'limit': 128, 'used': 0})
    assert not target.exists()


@pytest.mark.parametrize('fault', ['artifact_cap', 'remaining_budget', 'missing_sha'])
def test_invalid_download_plan_is_rejected_before_network(tmp_path, opener, fault):
    item, budget = descriptor(opener.data), {'limit': 128, 'used': 0}
    if fault == 'artifact_cap':
        item['max_bytes'] = 129
    elif fault == 'remaining_budget':
        budget['used'] = 127
    else:
        item.pop('sha256')
    with pytest.raises(ValueError):
        legal.download(item, tmp_path / 'blocked', budget)
    assert not opener.requests


@pytest.mark.parametrize('fault', ['artifact_stream', 'total_stream'])
def test_streamed_bytes_cannot_overrun_an_unpublished_size_allowance(tmp_path, opener, fault):
    item = descriptor(opener.data, expected_size=None, max_bytes=128)
    budget = {'limit': 128, 'used': 0}
    if fault == 'artifact_stream':
        item['max_bytes'] = 8
    else:
        budget['used'] = 125
    with pytest.raises(ValueError):
        legal.download(item, tmp_path / 'blocked', budget)
    assert not (tmp_path / 'blocked').exists()


def test_zip_retains_legal_text_and_hashes_required_dll_without_retaining_or_executing_it(tmp_path):
    dll = b'EXPLICITLY SYNTHETIC DLL CONTENT, NOT EXECUTABLE'
    archive = zip_fixture(tmp_path / 'source.zip', [
        ('vendor/bin/cudnn64_9.dll', dll), ('vendor/LICENSE', b'Owned license fixture.'),
        ('vendor/EULA.txt', b'Owned EULA fixture.'),
        ('vendor/host-code.py', b'raise AssertionError("Must not execute")'),
    ])
    required = {'path_suffix': '/bin/cudnn64_9.dll', 'expected_size': len(dll),
                'expected_sha256': sha(dll), 'retain': False}
    item = {'component': 'synthetic', 'format': 'zip', 'collect_legal_members': True,
            'required_members': [required]}
    output = tmp_path / 'legal'
    records = legal.collect_archive(archive, item, output)
    assert {row['member'] for row in records} == {'vendor/bin/cudnn64_9.dll', 'vendor/LICENSE', 'vendor/EULA.txt'}
    dll_record = next(row for row in records if row['member'].endswith('.dll'))
    assert dll_record['retained'] is False and dll_record['sha256'] == sha(dll)
    assert not list(output.rglob('*.dll')) and not list(output.rglob('*.py'))
    assert (output / 'LICENSE').read_bytes() == b'Owned license fixture.'
    assert (output / 'EULA.txt').read_bytes() == b'Owned EULA fixture.'


def test_required_nonretained_dll_hash_is_mandatory(tmp_path):
    data = b'Synthetic mismatched DLL.'
    archive = zip_fixture(tmp_path / 'source.zip', [('vendor/bin/cudnn64_9.dll', data)])
    required = {'path_suffix': '/bin/cudnn64_9.dll', 'expected_size': len(data),
                'expected_sha256': 'f' * 64, 'retain': False}
    with pytest.raises(ValueError, match='SHA-256'):
        legal.collect_archive(archive, {'component': 'fixture', 'format': 'zip', 'required_members': [required]}, tmp_path / 'legal')
    assert not list(tmp_path.rglob('*.dll'))


def test_tar_legal_member_matches_immutable_git_blob_and_exact_bytes(tmp_path):
    data = b'Owned complete legal-source fixture.\n'
    archive = tar_fixture(tmp_path / 'source.tar.gz', [('release/COPYING', data)])
    required = {'path': 'COPYING', 'expected_size': len(data), 'expected_sha256': sha(data),
                'expected_git_blob_sha1': blob(data)}
    output = tmp_path / 'legal'
    records = legal.collect_archive(archive, {'component': 'fixture', 'format': 'tar', 'required_members': [required]}, output)
    assert len(records) == 1 and records[0]['sha256'] == sha(data)
    assert (output / 'COPYING').read_bytes() == data


@pytest.mark.parametrize('kind', ['tar', 'zip'])
def test_missing_required_full_terms_cannot_be_reported_complete(tmp_path, kind):
    build = tar_fixture if kind == 'tar' else zip_fixture
    archive = build(tmp_path / ('source.' + kind), [('release/README', b'No legal text.')])
    with pytest.raises(ValueError, match='missing'):
        legal.collect_archive(archive, {'component': 'fixture', 'format': kind,
                              'required_members': [{'path': 'COPYING'}]}, tmp_path / 'legal')


@pytest.mark.parametrize('kind', ['tar', 'zip'])
def test_archive_path_traversal_is_rejected_before_writing_outside_output(tmp_path, kind):
    build = tar_fixture if kind == 'tar' else zip_fixture
    archive = build(tmp_path / ('source.' + kind), [('../LICENSE', b'Unsafe synthetic member.')])
    with pytest.raises(ValueError):
        legal.collect_archive(archive, {'component': 'fixture', 'format': kind, 'collect_legal_members': True}, tmp_path / 'legal')
    assert not (tmp_path.parent / 'LICENSE').exists()


def test_duplicate_required_archive_identity_is_rejected(tmp_path):
    archive = zip_fixture(tmp_path / 'source.zip', [('first/LICENSE', b'One.'), ('second/LICENSE', b'Two.')])
    with pytest.raises(ValueError):
        legal.collect_archive(archive, {'component': 'fixture', 'format': 'zip',
                              'required_members': [{'path': 'LICENSE'}]}, tmp_path / 'legal')


def test_legal_text_has_per_member_and_total_bounds(tmp_path, monkeypatch):
    archive = zip_fixture(tmp_path / 'source.zip', [('release/LICENSE', b'12345'), ('release/NOTICE', b'67890')])
    item = {'component': 'fixture', 'format': 'zip', 'collect_legal_members': True}
    monkeypatch.setattr(legal, 'MAX_NOTICE', 4)
    with pytest.raises(ValueError):
        legal.collect_archive(archive, item, tmp_path / 'member-bound')
    monkeypatch.setattr(legal, 'MAX_NOTICE', 8)
    monkeypatch.setattr(legal, 'MAX_NOTICES', 8)
    with pytest.raises(ValueError):
        legal.collect_archive(archive, item, tmp_path / 'total-bound')


@pytest.mark.parametrize('names', [('one/LICENSE', 'two/LICENSE'), ('release/LICENSE', 'release/license')])
def test_retained_legal_destinations_cannot_overwrite_or_case_collide(tmp_path, names):
    archive = zip_fixture(tmp_path / 'source.zip', [(names[0], b'First.'), (names[1], b'Second.')])
    with pytest.raises(ValueError):
        legal.collect_archive(archive, {'component': 'fixture', 'format': 'zip', 'collect_legal_members': True}, tmp_path / 'legal')


def test_zip_symbolic_link_is_not_a_full_license_text(tmp_path):
    archive = tmp_path / 'linked.zip'
    info = zipfile.ZipInfo('release/LICENSE')
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, 'w') as zipped:
        zipped.writestr(info, b'../real-license')
    with pytest.raises(ValueError):
        legal.collect_archive(archive, {'component': 'fixture', 'format': 'zip', 'collect_legal_members': True}, tmp_path / 'legal')


def test_nontext_required_member_is_hashed_but_never_presented_as_full_legal_text(tmp_path):
    data = b'\x00\xffEXPLICIT SYNTHETIC BINARY'
    archive = zip_fixture(tmp_path / 'source.zip', [('release/LICENSE', data)])
    required = {'path': 'LICENSE', 'expected_size': len(data), 'expected_sha256': sha(data)}
    output = tmp_path / 'legal'
    records = legal.collect_archive(archive, {'component': 'fixture', 'format': 'zip',
                                  'required_members': [required]}, output)
    assert len(records) == 1 and records[0]['sha256'] == sha(data)
    assert records[0]['retained'] is False and records[0]['text_omission'] == 'not_utf8_plain_text'
    assert not (output / 'LICENSE').exists()


@pytest.mark.parametrize('text', ['{"schema":1,"schema":1}', '{"schema":1,"limit":NaN}',
                                  '{"schema":1,"limit":Infinity}', '[]'])
def test_source_inventory_json_cannot_hide_duplicate_or_nonfinite_values(tmp_path, text):
    path = tmp_path / 'inventory.json'
    path.write_text(text, 'utf-8')
    with pytest.raises(ValueError):
        legal.read_json(path)


@pytest.fixture
def source(tmp_path, monkeypatch):
    """Exact synthetic source envelope; never an actual git acceptance claim."""
    root = tmp_path / 'source'
    script = root / 'scripts/dubbing_legal_sources.py'
    inventory = root / 'forge/dubbing_legal_sources_inventory.json'
    script.parent.mkdir(parents=True)
    inventory.parent.mkdir()
    (root / '.git').mkdir()
    script.write_text('# Synthetic verifier source identity fixture.\n', 'utf-8')
    inventory.write_text(json.dumps({'schema': 1, 'kind': 'synthetic_verifier_inventory',
                                    'immutable_assets': [], 'source_archives': [],
                                    'total_download_limit': 1024}), 'utf-8')
    paths = {'scripts/dubbing_legal_sources.py': sha(script.read_bytes()),
             'forge/dubbing_legal_sources_inventory.json': sha(inventory.read_bytes())}
    manifest = root / 'release-manifest.json'
    manifest.write_text(json.dumps({'app': 'FORGE', 'format': 1, 'version': '0.10.0', 'files': paths}, sort_keys=True), 'utf-8')
    value = SimpleNamespace(root=root, script=script, inventory=inventory, manifest=manifest,
                            committed_manifest=manifest.read_bytes(), paths=paths, commit='a' * 40,
                            dirty='', requests=[])

    def git(command, **kwargs):
        value.requests.append(command)
        if 'rev-parse' in command and 'HEAD' in command:
            result = (value.commit + '\n').encode('ascii')
        elif 'rev-parse' in command and '--show-toplevel' in command:
            result = (str(root) + '\n').encode('utf-8')
        elif 'status' in command:
            result = value.dirty.encode('utf-8')
        elif 'show' in command and str(command[-1]).endswith(':release-manifest.json'):
            result = value.committed_manifest
        else:
            pytest.fail('Unexpected source verification command: ' + repr(command))
        return result.decode('utf-8') if kwargs.get('text') or kwargs.get('encoding') else result

    monkeypatch.setattr(legal.subprocess, 'check_output', git)
    return value


def test_source_identity_binds_exact_registered_inventory_and_tracked_manifest(source):
    result = legal.source_identity(source.root, source.inventory)
    assert result == {'source_commit': source.commit, 'source_manifest_sha256': sha(source.committed_manifest),
                      'source_files_sha256': source.paths, 'source_kind': 'git', 'source_checkout_clean': True}
    assert source.requests


def test_source_identity_keeps_dirty_state_explicit_without_inventing_clean_evidence(source):
    source.dirty = ' M scripts/dubbing_legal_sources.py\n'
    assert legal.source_identity(source.root, source.inventory)['source_checkout_clean'] is False


@pytest.mark.parametrize('change', ['script', 'inventory', 'manifest', 'foreign_manifest'])
def test_source_identity_refuses_changed_or_foreign_source(source, tmp_path, change):
    inventory = source.inventory
    if change == 'script':
        source.script.write_text('# Unreviewed synthetic script change.\n', 'utf-8')
    elif change == 'inventory':
        source.inventory.write_text('{"schema": 1, "changed": true}', 'utf-8')
    elif change == 'manifest':
        source.manifest.write_text(source.manifest.read_text().replace('0.10.0', '0.10.1'), 'utf-8')
    else:
        inventory = tmp_path / 'other-inventory.json'
        inventory.write_bytes(source.inventory.read_bytes())
    with pytest.raises(ValueError):
        legal.source_identity(source.root, inventory)


def test_source_identity_rejects_linked_tracked_source_files(source, tmp_path):
    target = tmp_path / 'same-bytes.py'
    target.write_bytes(source.script.read_bytes())
    source.script.unlink()
    try:
        source.script.symlink_to(target)
    except OSError:
        pytest.skip('This platform cannot create the symbolic-link integrity fixture.')
    with pytest.raises(ValueError):
        legal.source_identity(source.root, source.inventory)


def test_metadata_only_report_never_claims_license_installation_or_gpu_acceptance(tmp_path, monkeypatch):
    manifest = tmp_path / 'synthetic-inventory.json'
    manifest.write_text(json.dumps({'schema': 1, 'kind': 'synthetic_fixture', 'immutable_assets': [],
                                    'source_archives': [], 'total_download_limit': 1024}), 'utf-8')
    identity = {'source_commit': 'a' * 40, 'source_manifest_sha256': 'b' * 64,
                'source_files_sha256': {'scripts/dubbing_legal_sources.py': 'c' * 64,
                                        'forge/dubbing_legal_sources_inventory.json': 'd' * 64},
                'source_kind': 'git', 'source_checkout_clean': True}
    monkeypatch.setattr(legal, 'source_identity', lambda *args: dict(identity))
    monkeypatch.setattr(legal.urllib.request, 'build_opener', lambda *args: pytest.fail('Empty metadata fixture must not download anything.'))
    output = tmp_path / 'report'
    monkeypatch.setattr(sys, 'argv', ['dubbing_legal_sources.py', '--manifest', str(manifest),
                                    '--output', str(output), '--metadata-only', '--require-clean-source'])
    assert legal.main() == 0
    report = json.loads((output / 'PUBLIC_LEGAL_SOURCE_REPORT.json').read_text('utf-8'))
    assert report['status'] == 'partial_individual_sources_only'
    assert report['archive_review_skipped'] is True and report['downloaded_bytes'] == 0
    assert report['manifest_sha256'] == sha(manifest.read_bytes())
    for key in ('native_execution_performed', 'native_binary_execution_performed', 'package_installation_performed',
                'model_inference_performed', 'license_reviewed', 'distribution_approved', 'physical_gpu_tested'):
        assert report[key] is False
    assert report['source_commit'] == identity['source_commit']
    assert report['source_files_sha256'] == identity['source_files_sha256']


@pytest.fixture
def cli(tmp_path, monkeypatch):
    manifest = tmp_path / 'synthetic-inventory.json'
    value = {'schema': 1, 'kind': 'synthetic_fixture', 'immutable_assets': [],
             'source_archives': [], 'total_download_limit': 1024}
    manifest.write_text(json.dumps(value), 'utf-8')
    identity = {'source_commit': 'a' * 40, 'source_manifest_sha256': 'b' * 64,
                'source_files_sha256': {'scripts/dubbing_legal_sources.py': 'c' * 64,
                                        'forge/dubbing_legal_sources_inventory.json': 'd' * 64},
                'source_kind': 'git', 'source_checkout_clean': True}
    output = tmp_path / 'report'
    monkeypatch.setattr(sys, 'argv', ['dubbing_legal_sources.py', '--manifest', str(manifest),
                                    '--output', str(output), '--metadata-only', '--require-clean-source'])
    monkeypatch.setattr(legal.urllib.request, 'build_opener', lambda *args: pytest.fail('No real network is permitted in a contract fixture.'))
    return SimpleNamespace(manifest=manifest, value=value, output=output, identity=identity)


def test_native_job_clean_source_requirement_prevents_network_or_output_creation(cli, monkeypatch):
    monkeypatch.setattr(legal, 'source_identity', lambda *args: {**cli.identity, 'source_checkout_clean': False})
    with pytest.raises(SystemExit) as error:
        legal.main()
    assert error.value.code == 2 and not cli.output.exists()


@pytest.mark.parametrize('failure', ['changed', 'unavailable'])
def test_source_changed_during_collection_retains_blocked_report(cli, monkeypatch, failure):
    calls = []
    def source_identity(*args):
        calls.append(args)
        if len(calls) == 1:
            return dict(cli.identity)
        if failure == 'unavailable':
            raise ValueError('Sensitive local source path that must not be emitted.')
        return {**cli.identity, 'source_commit': 'e' * 40}
    monkeypatch.setattr(legal, 'source_identity', source_identity)
    assert legal.main() == 2
    text = (cli.output / 'PUBLIC_LEGAL_SOURCE_REPORT.json').read_text('utf-8')
    report = json.loads(text)
    assert len(calls) == 2 and report['status'] == 'blocked'
    assert any(error.get('source_identity') == 'changed_or_unavailable' for error in report['errors'])
    assert 'Sensitive local source path' not in text
    assert report['license_reviewed'] is report['distribution_approved'] is False


def test_external_transfer_errors_are_sanitized_and_never_waive_integrity(cli, monkeypatch):
    data = b'Public immutable fixture.'
    cli.value['immutable_assets'] = [{**descriptor(data), 'save_as': 'COPYING'}]
    cli.manifest.write_text(json.dumps(cli.value), 'utf-8')
    monkeypatch.setattr(legal, 'source_identity', lambda *args: dict(cli.identity))
    class Opener:
        def open(self, *args, **kwargs):
            raise OSError('Private /workspace/owner/video.wav and authorization-token-fixture must be hidden.')
    monkeypatch.setattr(legal.urllib.request, 'build_opener', lambda *args: Opener())
    assert legal.main() == 2
    text = (cli.output / 'PUBLIC_LEGAL_SOURCE_REPORT.json').read_text('utf-8')
    report = json.loads(text)
    assert report['status'] == 'blocked' and report['errors'][0]['error'] == 'OSError'
    assert 'Private /workspace/owner' not in text and 'authorization-token-fixture' not in text
    assert report['downloads'] == [] and report['license_reviewed'] is False


@pytest.mark.parametrize('corrupt', [False, True])
def test_whole_archive_integrity_precedes_legal_extraction_and_never_approves_licenses(cli, opener, monkeypatch, tmp_path, corrupt):
    archive = zip_fixture(tmp_path / 'owned-fixture.zip', [('vendor/LICENSE', b'Owned full legal text fixture.')])
    opener.data = archive.read_bytes()
    item = {**descriptor(opener.data), 'component': 'fixture', 'format': 'zip',
            'required_members': [{'path': 'LICENSE'}]}
    if corrupt:
        item['sha256'] = 'f' * 64
        monkeypatch.setattr(legal, 'collect_archive', lambda *args: pytest.fail('An unverified archive must never be inspected.'))
    cli.value['source_archives'] = [item]
    cli.manifest.write_text(json.dumps(cli.value), 'utf-8')
    monkeypatch.setattr(legal, 'source_identity', lambda *args: dict(cli.identity))
    monkeypatch.setattr(sys, 'argv', [value for value in sys.argv if value != '--metadata-only'])
    assert legal.main() == (2 if corrupt else 0)
    report = json.loads((cli.output / 'PUBLIC_LEGAL_SOURCE_REPORT.json').read_text('utf-8'))
    assert report['archive_review_skipped'] is False
    assert report['license_reviewed'] is report['distribution_approved'] is False
    assert report['native_binary_execution_performed'] is report['model_inference_performed'] is False
    if corrupt:
        assert report['status'] == 'blocked' and not report['downloads']
        assert not (cli.output / 'public_source_texts').exists()
    else:
        assert report['status'] == 'identities_verified_legal_review_pending'
        assert report['downloads'][0]['documents'][0]['sha256'] == sha(b'Owned full legal text fixture.')


def test_manifest_cannot_silently_enlarge_default_large_artifact_download_allowance(cli, monkeypatch):
    cli.value.update(total_download_limit=680 * 1024 * 1024,
                     source_archives=[{'component': 'large-explicit-fixture', 'format': 'zip',
                                       'url': 'https://publisher.example.test/large-public-fixture.zip',
                                       'sha256': 'a' * 64, 'expected_size': 669629858,
                                       'max_bytes': 669629858, 'required_members': []}])
    cli.manifest.write_text(json.dumps(cli.value), 'utf-8')
    monkeypatch.setattr(legal, 'source_identity', lambda *args: dict(cli.identity))
    monkeypatch.setattr(sys, 'argv', [value for value in sys.argv if value != '--metadata-only'])
    assert legal.main() == 2
    report = json.loads((cli.output / 'PUBLIC_LEGAL_SOURCE_REPORT.json').read_text('utf-8'))
    assert report['download_limit_bytes'] == legal.DEFAULT_LIMIT
    assert report['downloaded_bytes'] == 0 and report['status'] == 'blocked'
    assert report['errors'][0]['error'] == 'ValueError'


@pytest.mark.parametrize('fault', ['sha', 'truncated', 'read_error', 'git_blob'])
@pytest.mark.parametrize('explicit_staging', [False, True])
def test_failed_transfers_never_expose_unverified_bytes_in_public_source_tree(tmp_path, opener, fault, explicit_staging):
    public = tmp_path / 'evidence/public_source_texts'
    target = public / 'immutable/LICENSE'
    item = descriptor(opener.data)
    if fault == 'sha':
        item['sha256'] = 'f' * 64
    elif fault == 'truncated':
        opener.data = opener.data[:-3]
    elif fault == 'read_error':
        opener.read_error_at = 7
    else:
        item['git_blob_sha1'] = 'f' * 40
    def check_public_tree():
        assert not [path for path in public.rglob('*') if path.is_file()], 'Transfer bytes appeared in public upload tree before complete identity verification.'
    opener.on_read = check_public_tree
    kwargs = {'staging': tmp_path / 'evidence/.transfer-staging'} if explicit_staging else {}
    with pytest.raises((ValueError, OSError)):
        legal.download(item, target, {'limit': 128, 'used': 0}, **kwargs)
    check_public_tree()
    assert not target.exists() and not list(public.rglob('*.part'))


@pytest.mark.parametrize('fault', [
    {'sha256': None}, {'sha256': True}, {'sha256': 'not-a-sha256'},
    {'git_blob_sha1': True}, {'git_blob_sha1': 'a' * 39}, {'git_blob_sha1': 'G' * 40},
    {'expected_git_blob_sha1': 'main'}, {'expected_size': True}, {'expected_size': 0},
    {'expected_size': -1}, {'expected_size': '12'},
])
def test_invalid_expected_hash_git_blob_or_size_prevents_any_network(tmp_path, opener, fault):
    target = tmp_path / 'public_source_texts/immutable/LICENSE'
    with pytest.raises(ValueError):
        legal.download(descriptor(opener.data, **fault), target, {'limit': 128, 'used': 0})
    assert not opener.requests and not target.exists()
    assert not [path for path in target.parent.rglob('*') if path.is_file()]


def test_existing_public_destination_is_preserved_without_new_download(tmp_path, opener):
    target = tmp_path / 'public_source_texts/immutable/LICENSE'
    target.parent.mkdir(parents=True)
    original = b'Existing verified legal evidence must be preserved.'
    target.write_bytes(original)
    with pytest.raises(ValueError):
        legal.download(descriptor(opener.data), target, {'limit': 128, 'used': 0})
    assert target.read_bytes() == original and not opener.requests


@pytest.mark.parametrize('fault', ['sha', 'truncated', 'read_error', 'git_blob'])
def test_main_never_uploads_unverified_immutable_asset_or_partial_bytes(cli, opener, monkeypatch, fault):
    item = {**descriptor(opener.data), 'save_as': 'LICENSE'}
    if fault == 'sha':
        item['sha256'] = 'f' * 64
    elif fault == 'truncated':
        opener.data = opener.data[:-3]
    elif fault == 'read_error':
        opener.read_error_at = 7
    else:
        item['git_blob_sha1'] = 'f' * 40
    cli.value['immutable_assets'] = [item]
    cli.manifest.write_text(json.dumps(cli.value), 'utf-8')
    monkeypatch.setattr(legal, 'source_identity', lambda *args: dict(cli.identity))
    public = cli.output / 'public_source_texts'
    def check_public_tree():
        assert not [path for path in public.rglob('*') if path.is_file()]
    opener.on_read = check_public_tree
    assert legal.main() == 2
    check_public_tree()
    report = json.loads((cli.output / 'PUBLIC_LEGAL_SOURCE_REPORT.json').read_text('utf-8'))
    assert report['status'] == 'blocked' and report['downloads'] == []
    assert report['license_reviewed'] is report['distribution_approved'] is False


@pytest.mark.parametrize('with_git_blob', [False, True])
def test_verified_utf8_source_with_control_bytes_is_omitted_without_identity_failure(tmp_path, opener, with_git_blob):
    # Synthetic reproduction of the native pkg.py finding: valid UTF-8, 7652
    # bytes, and an ESC byte at offset4410. This is not the upstream artifact.
    data = bytearray(b'# Explicit synthetic source fixture.\n'.ljust(7652, b' '))
    data[4410] = 0x1B
    opener.data = bytes(data)
    item = descriptor(opener.data)
    if with_git_blob:
        item['git_blob_sha1'] = blob(opener.data)
    public = tmp_path / 'public_source_texts'
    target = public / 'immutable/pkg.py'
    result = legal.download(item, target, {'limit': 8192, 'used': 0})
    assert result['identity_verified'] is True and result['retained'] is False
    assert result['sha256'] == sha(opener.data) and result['size'] == len(opener.data)
    assert result['text_omission'] == 'non_text_control_bytes'
    assert not target.exists() and not [path for path in public.rglob('*') if path.is_file()]


def test_main_records_verified_hash_only_source_omission_without_reading_missing_public_file(cli, opener, monkeypatch):
    opener.data = b'# Explicit synthetic source with an ANSI escape:\x1b[31mred\x1b[0m\n'
    cli.value['immutable_assets'] = [{**descriptor(opener.data), 'git_blob_sha1': blob(opener.data), 'save_as': 'pkg.py'}]
    cli.manifest.write_text(json.dumps(cli.value), 'utf-8')
    monkeypatch.setattr(legal, 'source_identity', lambda *args: dict(cli.identity))
    monkeypatch.setattr(sys, 'argv', [value for value in sys.argv if value != '--metadata-only'])
    assert legal.main() == 0
    report = json.loads((cli.output / 'PUBLIC_LEGAL_SOURCE_REPORT.json').read_text('utf-8'))
    assert report['status'] == 'identities_verified_legal_review_pending' and report['errors'] == []
    assert len(report['downloads']) == 1
    result = report['downloads'][0]
    assert result['identity_verified'] is True and result['retained'] is False
    assert result['text_omission'] == 'non_text_control_bytes'
    assert result['sha256'] == sha(opener.data) and result['size'] == len(opener.data)
    assert 'path' not in result and 'retained_path' not in result
    assert not [path for path in (cli.output / 'public_source_texts').rglob('*') if path.is_file()]
    assert report['license_reviewed'] is report['distribution_approved'] is False


@pytest.mark.parametrize('status', [403, 404])
def test_http_error_exposes_numeric_status_without_signed_url_path_or_server_message(cli, monkeypatch, status):
    data = b'Public immutable fixture.'
    cli.value['immutable_assets'] = [{**descriptor(data), 'save_as': 'LICENSE'}]
    cli.manifest.write_text(json.dumps(cli.value), 'utf-8')
    monkeypatch.setattr(legal, 'source_identity', lambda *args: dict(cli.identity))
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError(
                'https://publisher.example.test/source?X-Amz-Signature=synthetic-secret-value',
                status, 'Synthetic private path /private/owner/source and untrusted server detail', {}, None)
    monkeypatch.setattr(legal.urllib.request, 'build_opener', lambda *args: Opener())
    assert legal.main() == 2
    text = (cli.output / 'PUBLIC_LEGAL_SOURCE_REPORT.json').read_text('utf-8')
    report = json.loads(text)
    error = report['errors'][0]
    assert error['error'] == 'HTTPError' and error['reason_code'] == 'http_error'
    assert type(error['http_status']) is int and error['http_status'] == status
    for private in ('X-Amz-Signature', 'synthetic-secret-value', '/private/owner/source', 'untrusted server detail'):
        assert private not in text


def test_external_value_error_cannot_impersonate_a_controlled_integrity_reason(cli, monkeypatch):
    data = b'Public immutable fixture.'
    cli.value['immutable_assets'] = [{**descriptor(data), 'save_as': 'LICENSE'}]
    cli.manifest.write_text(json.dumps(cli.value), 'utf-8')
    monkeypatch.setattr(legal, 'source_identity', lambda *args: dict(cli.identity))
    class Opener:
        def open(self, *args, **kwargs):
            raise ValueError('artifact_sha256_mismatch: /private/owner/source https://publisher.example.test/?token=synthetic-secret-value')
    monkeypatch.setattr(legal.urllib.request, 'build_opener', lambda *args: Opener())
    assert legal.main() == 2
    text = (cli.output / 'PUBLIC_LEGAL_SOURCE_REPORT.json').read_text('utf-8')
    error = json.loads(text)['errors'][0]
    assert error['error'] == 'ValueError' and error['reason_code'] == 'external_value_error'
    for private in ('artifact_sha256_mismatch:', '/private/owner/source', 'synthetic-secret-value'):
        assert private not in text


@pytest.mark.parametrize('fault,code', [('invalid_sha', 'expected_sha256_invalid'),
                                      ('wrong_sha', 'artifact_sha256_mismatch'),
                                      ('wrong_size', 'artifact_size_mismatch'),
                                      ('wrong_git_blob', 'git_blob_mismatch')])
def test_internal_integrity_errors_keep_typed_reason_codes_without_changing_error_class(cli, opener, monkeypatch, fault, code):
    item = {**descriptor(opener.data), 'save_as': 'LICENSE'}
    if fault == 'invalid_sha':
        item['sha256'] = 'malformed'
    elif fault == 'wrong_sha':
        item['sha256'] = 'f' * 64
    elif fault == 'wrong_size':
        opener.data = opener.data[:-3]
    else:
        item['git_blob_sha1'] = 'f' * 40
    cli.value['immutable_assets'] = [item]
    cli.manifest.write_text(json.dumps(cli.value), 'utf-8')
    monkeypatch.setattr(legal, 'source_identity', lambda *args: dict(cli.identity))
    assert legal.main() == 2
    error = json.loads((cli.output / 'PUBLIC_LEGAL_SOURCE_REPORT.json').read_text('utf-8'))['errors'][0]
    assert error['error'] == 'ValueError' and error['reason_code'] == code
    if fault == 'invalid_sha':
        assert not opener.requests


@pytest.fixture
def fallback_sources(monkeypatch):
    """Explicit source alternatives serve fixtures only; no real network."""
    state = SimpleNamespace(primary='https://publisher.example.test/source.tar.gz',
                            alternative='https://mirror.example.test/source.tar.gz',
                            routes={}, requests=[], on_read=None)
    class Response(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.headers = {'Content-Length': str(len(data))}
        def read(self, limit=-1):
            if state.on_read is not None:
                state.on_read()
            return super().read(limit)
    class Opener:
        def open(self, request, timeout):
            state.requests.append(request.full_url)
            assert request.full_url in state.routes, 'Unregistered synthetic alternative was requested.'
            value = state.routes[request.full_url]
            if isinstance(value, BaseException):
                raise value
            return Response(value)
    monkeypatch.setattr(legal.urllib.request, 'build_opener', lambda *args: Opener())
    return state


@pytest.mark.parametrize('first_failure,reason', [('http404', 'http_error'),
                                                ('wrong_sha', 'artifact_sha256_mismatch')])
def test_explicit_alternative_preserves_exact_identity_and_publication_guards(tmp_path, fallback_sources, monkeypatch, first_failure, reason):
    state = fallback_sources
    data = b'Identical verified public source bytes.'
    if first_failure == 'http404':
        primary = urllib.error.HTTPError(
            'https://redirect.example.test/?signature=synthetic-hidden-value', 404,
            'Synthetic private /private/source path and server message', {}, None)
    else:
        primary = b'X' + data[1:]
    state.routes = {state.primary: primary, state.alternative: data}
    item = descriptor(data, url=state.primary, alternative_urls=[state.alternative], git_blob_sha1=blob(data))
    public = tmp_path / 'public_source_texts'
    target = public / 'immutable/LICENSE'
    def check_public():
        assert not [path for path in public.rglob('*') if path.is_file()]
    state.on_read = check_public
    seen = []
    original = legal._download_one
    def inspect_identity(attempt, *args, **kwargs):
        seen.append({key: attempt[key] for key in ('sha256', 'expected_size', 'git_blob_sha1')})
        return original(attempt, *args, **kwargs)
    monkeypatch.setattr(legal, '_download_one', inspect_identity)
    budget = {'limit': 256, 'used': 0}
    result = legal.download(item, target, budget)
    expected = {key: item[key] for key in ('sha256', 'expected_size', 'git_blob_sha1')}
    assert seen == [expected, expected]
    assert state.requests == [state.primary, state.alternative]
    assert target.read_bytes() == data and result['sha256'] == sha(data)
    assert result['identity_verified'] is True and result['retained'] is True
    attempts = result['source_attempts']
    assert [attempt['source_index'] for attempt in attempts] == [0, 1]
    assert attempts[0]['reason_code'] == reason
    if first_failure == 'http404':
        assert attempts[0]['http_status'] == 404
    assert attempts[1]['status'] == 'identity_verified'
    text = json.dumps(attempts)
    for private in ('redirect.example.test', 'synthetic-hidden-value', '/private/source', 'server message'):
        assert private not in text
    assert budget['used'] == len(data) * (2 if first_failure == 'wrong_sha' else 1)


def test_all_explicit_sources_failed_reports_only_bounded_safe_attempts(tmp_path, fallback_sources):
    state = fallback_sources
    for url, status in ((state.primary, 403), (state.alternative, 404)):
        state.routes[url] = urllib.error.HTTPError(
            'https://redirect.example.test/private?signature=synthetic-secret-value', status,
            'Private path /private/source and untrusted reason', {}, None)
    item = descriptor(b'Public source fixture.', url=state.primary, alternative_urls=[state.alternative])
    target = tmp_path / 'public_source_texts/immutable/LICENSE'
    with pytest.raises(legal.EvidenceError) as failure:
        legal.download(item, target, {'limit': 256, 'used': 0})
    result = legal.error_record(failure.value)
    assert result['reason_code'] == 'all_sources_failed' and result['error'] == 'ValueError'
    attempts = result['source_attempts']
    assert [attempt['source_index'] for attempt in attempts] == [0, 1]
    assert [attempt['http_status'] for attempt in attempts] == [403, 404]
    assert all(type(attempt['http_status']) is int and attempt['reason_code'] == 'http_error' for attempt in attempts)
    text = json.dumps(result)
    for private in ('signature=', 'synthetic-secret-value', 'redirect.example.test', '/private/source', 'untrusted reason'):
        assert private not in text
    assert not target.exists()


@pytest.mark.parametrize('alternatives', [
    ['http://mirror.example.test/source'], ['https://user:password@mirror.example.test/source'],
    ['https://mirror.example.test/source#fragment'], [None],
    ['https://mirror.example.test/source'] * 6, 'https://mirror.example.test/source',
])
def test_every_alternative_is_preflighted_before_any_network(tmp_path, fallback_sources, alternatives):
    state = fallback_sources
    item = descriptor(b'Public fixture.', url=state.primary, alternative_urls=alternatives)
    with pytest.raises(ValueError):
        legal.download(item, tmp_path / 'LICENSE', {'limit': 256, 'used': 0})
    assert state.requests == []


@pytest.mark.parametrize('fault', ['invalid_sha', 'invalid_size', 'existing_destination'])
def test_alternatives_never_retry_invalid_metadata_or_replace_existing_evidence(tmp_path, fallback_sources, fault):
    state = fallback_sources
    item = descriptor(b'Public fixture.', url=state.primary, alternative_urls=[state.alternative])
    target = tmp_path / 'LICENSE'
    if fault == 'invalid_sha':
        item['sha256'] = 'invalid'
    elif fault == 'invalid_size':
        item['expected_size'] = 0
    else:
        target.write_bytes(b'Preexisting evidence remains unchanged.')
    with pytest.raises(ValueError):
        legal.download(item, target, {'limit': 256, 'used': 0})
    assert state.requests == []
    if fault == 'existing_destination':
        assert target.read_bytes() == b'Preexisting evidence remains unchanged.'


def test_alternative_transfer_cannot_reset_shared_budget_after_failed_bytes(tmp_path, fallback_sources):
    state = fallback_sources
    data = b'Exact immutable public source fixture.'
    state.routes = {state.primary: b'X' + data[1:], state.alternative: data}
    item = descriptor(data, url=state.primary, alternative_urls=[state.alternative])
    # The rejected primary consumes real bytes; the fallback no longer fits.
    budget = {'limit': 2 * len(data) + 2, 'used': 3}
    target = tmp_path / 'public_source_texts/immutable/LICENSE'
    with pytest.raises(ValueError):
        legal.download(item, target, budget)
    assert budget['used'] == 3 + len(data)
    assert state.requests == [state.primary] and not target.exists()


@pytest.mark.parametrize('name', ['release/tpp.txt', 'release/third-party-programs.txt', 'release/AUTHORS',
                                  'release/redist.txt', 'release/redist-notices.txt', 'release/LICENCE.txt',
                                  'release/licenses/component.spdx',
                                  'release/third-party-notices/component.txt',
                                  'release/third_party_notices/component.spdx'])
def test_public_legal_document_detection_includes_vendor_notice_names_and_directories(name):
    assert legal.legal_member(name) is True


@pytest.mark.parametrize('name', ['release/README.md', 'release/configure', 'release/bin/ordinary.dll',
                                  'release/src/component.cpp'])
def test_ordinary_archive_files_are_not_assumed_legal_documents(name):
    assert legal.legal_member(name) is False


def test_collects_tpp_authors_redist_and_nested_spdx_without_assuming_unknown_required_paths(tmp_path):
    members = [('vendor/tpp.txt', b'Owned third-party program fixture.'),
               ('vendor/third-party-programs.txt', b'Owned additional vendor terms fixture.'),
               ('vendor/AUTHORS', b'Owned authorship fixture.'),
               ('vendor/redist.txt', b'Owned redistribution fixture.'),
               ('vendor/licenses/component.spdx', b'SPDX-License-Identifier: MIT\n'),
               ('vendor/README', b'Ordinary unselected fixture.')]
    archive = zip_fixture(tmp_path / 'vendor.zip', members)
    output = tmp_path / 'legal'
    records = legal.collect_archive(archive, {'component': 'fixture', 'format': 'zip',
                                  'collect_legal_members': True, 'required_members': []}, output)
    assert {row['member'] for row in records} == {name for name, _ in members[:-1]}
    assert all(row['retained'] is True for row in records)
    for name, data in members[:-1]:
        assert (output / name.split('/', 1)[1]).read_bytes() == data


def test_binary_in_legal_directory_is_hash_recorded_without_retaining_or_executing_it(tmp_path):
    data = b'MZ\x00\xffEXPLICITLY SYNTHETIC NATIVE FIXTURE'
    archive = zip_fixture(tmp_path / 'vendor.zip', [('vendor/licenses/helper.dll', data)])
    output = tmp_path / 'legal'
    records = legal.collect_archive(archive, {'component': 'fixture', 'format': 'zip',
                                  'collect_legal_members': True, 'required_members': []}, output)
    assert len(records) == 1 and records[0]['member'] == 'vendor/licenses/helper.dll'
    assert records[0]['sha256'] == sha(data) and records[0]['size'] == len(data)
    assert records[0]['retained'] is False and records[0]['text_omission'] == 'not_utf8_plain_text'
    assert not list(output.rglob('*.dll'))


@pytest.mark.parametrize('has_tpp', [False, True])
def test_complete_archive_index_distinguishes_observed_tpp_presence_from_absence(tmp_path, has_tpp):
    members = [('vendor/README', b'Ordinary member.'), ('vendor/LICENSE', b'Owned terms.')]
    if has_tpp:
        members.append(('vendor/tpp.txt', b'Owned third-party terms.'))
    archive = zip_fixture(tmp_path / 'vendor.zip', members)
    result = legal.archive_index(archive, 'zip')
    assert result['archive_entry_count'] == len(members)
    assert result['legal_candidates_complete'] is True
    assert result['legal_candidate_count'] == (2 if has_tpp else 1)
    assert {row['path'] for row in result['legal_candidates']} == ({'vendor/LICENSE', 'vendor/tpp.txt'} if has_tpp else {'vendor/LICENSE'})
    assert all(row['kind'] == 'file' for row in result['legal_candidates'])
    assert len(result['canonical_entry_index_sha256']) == 64
    assert result['canonical_entry_index_sha256'] == legal.archive_index(archive, 'zip')['canonical_entry_index_sha256']


def test_full_archive_index_hash_includes_unselected_ordinary_names(tmp_path):
    first = zip_fixture(tmp_path / 'first.zip', [('vendor/README', b'Ordinary.'), ('vendor/LICENSE', b'Owned terms.')])
    second = zip_fixture(tmp_path / 'second.zip', [('vendor/CHANGELOG', b'Ordinary.'), ('vendor/LICENSE', b'Owned terms.')])
    left, right = legal.archive_index(first, 'zip'), legal.archive_index(second, 'zip')
    assert left['legal_candidates'] == right['legal_candidates']
    assert left['archive_entry_count'] == right['archive_entry_count'] == 2
    assert left['canonical_entry_index_sha256'] != right['canonical_entry_index_sha256']


@pytest.mark.parametrize('kind', ['zip', 'tar'])
def test_archive_index_keeps_legal_file_directory_and_link_types_explicit(tmp_path, kind):
    archive = tmp_path / ('vendor.' + kind)
    text = b'SPDX-License-Identifier: MIT\n'
    if kind == 'zip':
        link = zipfile.ZipInfo('vendor/tpp.txt')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, 'w') as handle:
            handle.writestr('vendor/licenses/', b'')
            handle.writestr('vendor/licenses/component.spdx', text)
            handle.writestr(link, b'../original-tpp.txt')
    else:
        with tarfile.open(archive, 'w') as handle:
            directory = tarfile.TarInfo('vendor/licenses')
            directory.type = tarfile.DIRTYPE
            handle.addfile(directory)
            member = tarfile.TarInfo('vendor/licenses/component.spdx')
            member.size = len(text)
            handle.addfile(member, io.BytesIO(text))
            link = tarfile.TarInfo('vendor/tpp.txt')
            link.type = tarfile.SYMTYPE
            link.linkname = '../original-tpp.txt'
            handle.addfile(link)
    result = legal.archive_index(archive, kind)
    assert result['archive_entry_count'] == result['legal_candidate_count'] == 3
    assert result['legal_candidates_complete'] is True
    types = {row['path']: row['kind'] for row in result['legal_candidates']}
    assert types == {'vendor/licenses': 'directory', 'vendor/licenses/component.spdx': 'file',
                     'vendor/tpp.txt': 'link_or_special'}
