"""Release appends are tested without tokens, network calls or remote mutations."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('forge_windows_publisher', Path(__file__).parents[1] / 'scripts/publish_windows_release.py')
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
TAG = 'v0.5.0'
SOURCE = 'a' * 40
TOOLING = 'b' * 40


class FakeGitHub:
    repository = 'Example/forge'

    def __init__(self):
        self.data = {}
        self.records = {}
        self.uploaded = []
        self.patches = []
        self.commit = SOURCE
        self.release_value = {'id': 42, 'tag_name': TAG, 'draft': False, 'published_at': '2026-01-01T00:00:00Z', 'body': 'Original release notes.\n'}

    def add(self, name, data):
        asset_id = len(self.records) + 100
        self.data[asset_id] = data
        self.records[asset_id] = {'id': asset_id, 'name': name, 'size': len(data), 'created_at': 'fixed', 'updated_at': 'fixed'}

    def release(self, tag):
        assert tag == TAG
        return copy.deepcopy(self.release_value)

    def assets(self, release_id):
        assert release_id == 42
        return copy.deepcopy(list(self.records.values()))

    def tag_commit(self, tag):
        assert tag == TAG
        return self.commit

    def asset_digest(self, asset):
        return hashlib.sha256(self.data[asset['id']]).hexdigest()

    def asset_bytes(self, asset):
        return self.data[asset['id']]

    def upload(self, release_id, path):
        assert release_id == 42
        assert path.name not in [value['name'] for value in self.records.values()]
        self.uploaded.append(path.name)
        self.add(path.name, path.read_bytes())

    def json(self, path, *, method, value):
        assert path == '/releases/42' and method == 'PATCH'
        assert set(value) == {'body'}
        self.patches.append(value)
        self.release_value.update(value)
        return copy.deepcopy(self.release_value)


@pytest.fixture
def candidate(tmp_path):
    archive_name, checksum_name, _ = publisher.release_names(TAG)
    archive = tmp_path / archive_name
    # Publication tests exercise byte identity; ZIP layout has its own real
    # deterministic package tests and must pass before this publisher job runs.
    archive.write_bytes(b'previously validated synthetic archive bytes')
    sha = publisher.digest(archive)
    (tmp_path / checksum_name).write_text(f'{sha}  {archive_name}\n')
    counts = {'total': 6, 'passed': 5, 'skipped': 1, 'failed': 0, 'errors': 0}
    linux = {'status': 'passed', 'tag': TAG, 'source_commit': SOURCE, 'tooling_commit': TOOLING,
             'archive_name': archive_name, 'archive_sha256': sha,
             'tagged_python_suite': counts, 'windows_packager_suite': counts,
             'manifest_validation': 'passed', 'tagged_source_comparison': 'passed', 'protected_paths_excluded': True}
    native = {'status': 'passed', 'native_windows_execution': True,
              'archive_name': archive_name, 'archive_sha256': sha, 'updater_sha256': publisher.UPDATER_SHA256,
              'checks': ['native installation', 'native rejection'], 'passed': 2,
              'cases': [{'name': name, 'status': 'passed'} for name in sorted(publisher.REQUIRED_CASES)],
              'installed_app_smoke': {'status': 'passed', 'native_windows_execution': True,
                                      'expected_version': TAG[1:], 'installed_version': TAG[1:],
                                      'checks': ['actual QR worker'], 'passed': 1}}
    proof = publisher.build_validation(TAG, tmp_path, linux, native)
    return tmp_path, proof, linux, native


def publish(client, candidate):
    directory, proof, *_ = candidate
    publisher.publish(client, TAG, directory, proof, wait_seconds=0)


def test_append_preserves_old_bytes_notes_and_tag_and_is_idempotent(candidate):
    client = FakeGitHub()
    client.add('FORGE-v0.5.zip', b'legacy ZIP unchanged')
    client.add('CHECKSUMS.sha256', b'legacy checksum unchanged')
    before = copy.deepcopy(client.data)
    publish(client, candidate)
    assert client.uploaded == list(publisher.release_names(TAG))
    assert all(client.data[key] == value for key, value in before.items())
    assert len(publisher.windows_assets(client.assets(42))) == 1
    assert client.commit == SOURCE
    assert client.release_value['body'].startswith('Original release notes.\n')
    assert len(client.patches) == 1
    publish(client, candidate)
    assert len(client.uploaded) == 3
    assert len(client.patches) == 1


def test_first_passing_validation_receipt_is_retained_after_tooling_commit_changes(candidate):
    client = FakeGitHub()
    directory, proof, linux, native = candidate
    publish(client, candidate)
    original = next(asset for asset in client.assets(42) if asset['name'] == proof.name)
    original_bytes = client.asset_bytes(original)
    linux['tooling_commit'] = 'c' * 40
    publisher.build_validation(TAG, directory, linux, native)
    assert proof.read_bytes() != original_bytes
    publish(client, candidate)
    assert client.asset_bytes(original) == original_bytes
    assert len(client.uploaded) == 3
    assert json.loads(original_bytes)['tooling_commit'] == TOOLING


@pytest.mark.parametrize('name', ['evidence-windows.zip', 'FORGE-win64.zip', 'unrelated-x64.ZIP', 'FORGE-amd64.zip'])
def test_other_windows_match_fails_before_upload(candidate, name):
    client = FakeGitHub()
    client.add(name, b'other package')
    with pytest.raises(publisher.PublicationError, match='Another Windows'):
        publish(client, candidate)
    assert not client.uploaded and not client.patches


@pytest.mark.parametrize('index', [0, 1])
def test_conflicting_exact_zip_or_checksum_never_overwrites_or_partially_appends(candidate, index):
    client = FakeGitHub()
    client.add(publisher.release_names(TAG)[index], b'unexpected bytes')
    with pytest.raises(publisher.PublicationError, match='differs'):
        publish(client, candidate)
    assert not client.uploaded and not client.patches
    assert list(client.data.values()) == [b'unexpected bytes']


def test_foreign_tag_commit_fails_before_upload(candidate):
    client = FakeGitHub()
    client.commit = 'd' * 40
    with pytest.raises(publisher.PublicationError, match='source commit'):
        publish(client, candidate)
    assert not client.uploaded


@pytest.mark.parametrize('field,value', [('status', 'failed'), ('native_windows_execution', False),
                                        ('archive_sha256', '0' * 64), ('updater_sha256', '0' * 64),
                                        ('passed', 99), ('cases', [])])
def test_native_acceptance_gates(candidate, field, value):
    directory, _, linux, native = candidate
    native[field] = value
    with pytest.raises(publisher.PublicationError):
        publisher.build_validation(TAG, directory, linux, native)


def test_installed_application_smoke_must_match_release_version(candidate):
    directory, _, linux, native = candidate
    native['installed_app_smoke']['installed_version'] = '0.4.0'
    with pytest.raises(publisher.PublicationError, match='installed-application'):
        publisher.build_validation(TAG, directory, linux, native)


@pytest.mark.parametrize('field,value', [('source_commit', 'd' * 40), ('archive_sha256', '0' * 64),
                                        ('native_windows_updater', 'failed'), ('tooling_commit', 'unknown')])
def test_invalid_existing_receipt_is_never_accepted(candidate, field, value):
    client = FakeGitHub()
    directory, proof, *_ = candidate
    record = json.loads(proof.read_text())
    record[field] = value
    client.add(proof.name, json.dumps(record).encode())
    with pytest.raises(publisher.PublicationError):
        publish(client, candidate)
    assert not client.uploaded


def test_partial_prior_upload_resumes_without_replacing_existing_zip(candidate):
    client = FakeGitHub()
    directory, _, *_ = candidate
    archive = directory / publisher.release_names(TAG)[0]
    client.add(archive.name, archive.read_bytes())
    publish(client, candidate)
    assert client.uploaded == list(publisher.release_names(TAG))[1:]


def test_draft_release_is_not_published_by_this_helper(candidate):
    client = FakeGitHub()
    client.release_value['draft'] = True
    with pytest.raises(publisher.PublicationError, match='not published'):
        publish(client, candidate)
    assert not client.uploaded and not client.patches


def test_post_upload_download_corruption_fails(candidate):
    client = FakeGitHub()
    client.asset_digest = lambda asset: '0' * 64
    with pytest.raises(publisher.PublicationError, match='Post-upload'):
        publish(client, candidate)
    assert not client.patches


def test_junit_requires_passed_executed_cases(tmp_path):
    path = tmp_path / 'results.xml'
    path.write_text('<testsuites><testsuite><testcase/><testcase><skipped/></testcase></testsuite></testsuites>')
    assert publisher.junit_counts(path) == {'total': 2, 'passed': 1, 'failed': 0, 'errors': 0, 'skipped': 1}
    path.write_text('<testsuites><testsuite><testcase><failure/></testcase></testsuite></testsuites>')
    with pytest.raises(publisher.PublicationError):
        publisher.junit_counts(path)


def test_download_redirect_strips_token_on_cross_host():
    request = publisher.Request('https://api.github.com/repos/Example/forge/releases/assets/1', headers={'Authorization': 'Bearer test-only'})
    redirected = publisher.SafeRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://release-assets.githubusercontent.com/asset')
    assert not redirected.has_header('Authorization')
    with pytest.raises(publisher.PublicationError):
        publisher.SafeRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://example.invalid/asset')
