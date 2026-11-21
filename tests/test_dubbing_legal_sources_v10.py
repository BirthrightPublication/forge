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
