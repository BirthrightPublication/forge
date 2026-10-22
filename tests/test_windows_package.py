"""Contract tests for the separate app/ asset used by the Windows updater."""
from __future__ import annotations

import importlib.util
from datetime import datetime
import json
import os
import py_compile
from pathlib import Path
import stat
import struct
import subprocess
import sys
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'package_windows_release.py'
SPEC = importlib.util.spec_from_file_location('windows_package', SCRIPT)
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)
TAG = 'v0.5.0'
ASSET = 'FORGE-v0.5.0-windows-x64.zip'


def write_source_manifest(source):
    inventory = {
        path.relative_to(source).as_posix(): package.sha256(path.read_bytes())
        for path in source.rglob('*') if path.is_file() and path.name != 'release-manifest.json'
    }
    manifest = {'app': 'FORGE', 'format': 1, 'version': '0.5.0',
                'minimum_from': '0.1.0', 'files': inventory}
    (source / 'release-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    for relative in package.REQUIRED_FILES | {
        'docs/BUILD.md', 'docs/history/v0.4/README.md', 'tests/test_saved.py',
        'original_scripts/example.py', 'Update FORGE.bat', 'scripts/Update-Application.ps1',
        'forge/data/private.txt', 'web/settings/preferences.json', 'Run Tests.bat',
        'pytest.ini', 'requirements-dev.txt', 'requirements-transcription.txt',
        'scripts/install_optional.ps1', 'web/assets/icon.svg',
    }:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        contents = '# harmless Python fixture\n' if target.suffix == '.py' else 'fixture content\n'
        if relative == 'VERSION.txt':
            contents = '0.5.0\n'
        elif relative == 'forge/__init__.py':
            contents = '__version__ = "0.5.0"\n'
        target.write_text(contents, encoding='utf-8')
    write_source_manifest(root)
    return root


def contents(archive):
    with zipfile.ZipFile(archive) as zipped:
        return {entry.filename: zipped.read(entry) for entry in zipped.infolist()}


def save_archive(path, files, *, special=None, compression=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, 'w', compression=compression) as zipped:
        for name, data in files.items():
            if special and name == special.filename:
                zipped.writestr(special, data)
            else:
                zipped.writestr(name, data)


def update_package_manifest(files):
    manifest = json.loads(files['app/release-manifest.json'])
    manifest['files'] = {name[4:]: package.sha256(data) for name, data in files.items()
                         if name != 'app/release-manifest.json' and not name.endswith('/')}
    files['app/release-manifest.json'] = json.dumps(manifest).encode()


@pytest.fixture
def archive(source, tmp_path):
    return package.build(source, tmp_path / 'output', tag=TAG)


def test_build_preserves_tagged_application_bytes_and_filters_protected_files(source, archive):
    original = (source / 'release-manifest.json').read_bytes()
    files = contents(archive)
    assert all(name.startswith('app/') for name in files)
    assert 'app/VERSION.txt' in files and 'app/forge/__init__.py' in files
    assert 'app/scripts/update.py' in files
    assert 'app/scripts/install_optional.ps1' in files
    assert 'app/requirements-transcription.txt' in files
    assert not any(package.excluded(name[4:]) for name in files)
    for name, data in files.items():
        if name != 'app/release-manifest.json':
            assert data == (source / name[4:]).read_bytes()
    manifest = json.loads(files['app/release-manifest.json'])
    assert set(manifest['files']) == {name[4:] for name in files} - {'release-manifest.json'}
    assert manifest['package']['source_manifest_sha256'] == package.sha256(original)
    assert (source / 'release-manifest.json').read_bytes() == original
    checksum = archive.with_name(archive.name + '.sha256')
    assert checksum.read_text() == package.sha256(archive.read_bytes()) + '  ' + ASSET + '\n'
    assert sorted(path.name for path in archive.parent.iterdir()) == [ASSET, ASSET + '.sha256']
    report = package.validate_archive(archive, tag=TAG, checksum=checksum, source=source)
    assert report['application_files'] == len(files) - 1
    assert report['archive_files'] == len(files)
    assert report['source_files_verified'] == len(json.loads(original)['files'])
    assert report['excluded_source_files'] > 0


def test_deterministic_and_idempotent_without_overwriting_other_assets(source, archive, tmp_path):
    for path in source.rglob('*'):
        os.utime(path, (1700000000, 1700000000))
    second = package.build(source, tmp_path / 'other', tag=TAG)
    assert second.read_bytes() == archive.read_bytes()
    assert package.build(source, archive.parent, tag=TAG) == archive
    second.write_bytes(b'an existing public asset')
    with pytest.raises(package.PackageError, match='replace a different'):
        package.build(source, second.parent, tag=TAG)
    assert second.read_bytes() == b'an existing public asset'


def test_checksum_conflict_does_not_create_archive(source, tmp_path):
    output = tmp_path / 'assets'
    output.mkdir()
    checksum = output / (ASSET + '.sha256')
    checksum.write_bytes(b'existing')
    with pytest.raises(package.PackageError, match='replace a different'):
        package.build(source, output, tag=TAG)
    assert not (output / ASSET).exists()
    assert checksum.read_bytes() == b'existing'


@pytest.mark.parametrize('previous_timestamp', [(1980, 1, 1, 0, 0, 0), package.release_timestamp('0.4.0')])
def test_copying_new_release_invalidates_same_size_cached_python(source, archive, tmp_path, previous_timestamp):
    installed = tmp_path / 'installation'
    module = installed / 'forge' / '__init__.py'
    module.parent.mkdir(parents=True)
    module.write_bytes((source / 'forge/__init__.py').read_bytes().replace(b'0.5.0', b'0.4.0'))
    stamp = datetime(*previous_timestamp).timestamp()
    os.utime(module, (stamp, stamp))
    py_compile.compile(str(module), doraise=True)
    probe = [sys.executable, '-I', '-c',
             'import sys; sys.path.insert(0, sys.argv[1]); import forge; print(forge.__version__)', str(installed)]
    assert subprocess.check_output(probe, text=True).strip() == '0.4.0'
    with zipfile.ZipFile(archive) as zipped:
        entry = zipped.getinfo('app/forge/__init__.py')
        assert len(zipped.read(entry)) == module.stat().st_size
        module.write_bytes(zipped.read(entry))
        stamp = datetime(*entry.date_time).timestamp()
        os.utime(module, (stamp, stamp))
    assert subprocess.check_output(probe, text=True).strip() == '0.5.0'


def test_version_timestamps_are_unique_representable_and_do_not_wrap():
    versions = ['0.0.0', '0.0.1', '0.5.0', '0.6.0', '0.999.999', '1.0.0', '999.999.999']
    timestamps = [package.release_timestamp(version) for version in versions]
    assert timestamps == sorted(set(timestamps))
    assert all(2000 <= timestamp[0] <= 2064 and timestamp[-1] % 2 == 0 for timestamp in timestamps)
    for version in ['1000.0.0', '0.1000.0', '0.0.1000']:
        with pytest.raises(package.PackageError, match='0 through 999'):
            package.release_timestamp(version)


@pytest.mark.parametrize('tag', ['v0.5', '0.5.0', 'v00.5.0', 'v0.5.0-rc1', 'v0.5.0/extra', 'v0.5.1١'])
def test_requires_full_exact_release_tag(source, tmp_path, tag):
    with pytest.raises(package.PackageError, match='exact release tag'):
        package.build(source, tmp_path / 'out', tag=tag)


@pytest.mark.parametrize('filename', ['FORGE-v0.5-windows-x64.zip', 'FORGE-v0.5.0.zip',
                                    'FORGE-v0.5.0-windows-arm64.zip', 'forge-v0.5.0-windows-x64.zip'])
def test_validator_requires_exact_windows_asset_name(archive, filename):
    renamed = archive.with_name(filename)
    renamed.write_bytes(archive.read_bytes())
    with pytest.raises(package.PackageError, match='asset name'):
        package.validate_archive(renamed)


@pytest.mark.parametrize('relative', ['forge/app.py', 'original_scripts/example.py', 'tests/test_saved.py'])
def test_source_hash_verification_includes_excluded_files(source, relative):
    (source / relative).write_text('# changed source\n')
    with pytest.raises(package.PackageError, match='Source manifest hash mismatch'):
        package.expected_payload(source, TAG)


def test_missing_and_unlisted_source_files_fail(source):
    extra = source / 'forge' / 'unlisted.py'
    extra.write_text('# unlisted\n')
    with pytest.raises(package.PackageError, match='omits an application file'):
        package.expected_payload(source, TAG)
    extra.unlink()
    (source / 'web/app.js').unlink()
    with pytest.raises(package.PackageError, match='unavailable'):
        package.expected_payload(source, TAG)


def test_private_root_state_is_never_traversed_or_bundled(source):
    for relative in ['data/user.db', '.venv/bin/python', 'models/private.bin', '.env']:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('private')
    files, _ = package.expected_payload(source, TAG)
    assert not any('private' in str(data) for data in files.values())


@pytest.mark.parametrize('relative', ['VERSION.txt', 'forge/__init__.py'])
def test_source_version_must_match_manifest_and_tag(source, relative):
    path = source / relative
    path.write_text(path.read_text().replace('0.5.0', '0.4.0'))
    write_source_manifest(source)
    with pytest.raises(package.PackageError, match='versions do not match'):
        package.expected_payload(source, TAG)


def test_manifest_version_cannot_be_changed_by_requested_tag(source):
    with pytest.raises(package.PackageError, match='identity or version'):
        package.expected_payload(source, 'v0.6.0')


def test_packager_never_executes_application_code(source, tmp_path):
    marker = tmp_path / 'executed'
    (source / 'forge/__init__.py').write_text(
        '__version__ = "0.5.0"\nfrom pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
    write_source_manifest(source)
    package.expected_payload(source, TAG)
    assert not marker.exists()


def test_source_symlinks_are_rejected(source, tmp_path):
    target = source / 'forge/app.py'
    original = tmp_path / 'outside.py'
    original.write_bytes(target.read_bytes())
    target.unlink()
    try:
        target.symlink_to(original)
    except OSError:
        pytest.skip('Symlink creation is unavailable on this host.')
    with pytest.raises(package.PackageError, match='links are not accepted'):
        package.expected_payload(source, TAG)


@pytest.mark.parametrize('mutation,expected', [
    ('missing_required', 'Required application files'),
    ('case_collision', 'Case-insensitive'),
    ('self_reference', 'cannot list itself'),
])
def test_source_manifest_structure(source, mutation, expected):
    path = source / 'release-manifest.json'
    manifest = json.loads(path.read_bytes())
    if mutation == 'missing_required':
        del manifest['files']['forge/app.py']
    elif mutation == 'case_collision':
        manifest['files']['forge/APP.py'] = manifest['files']['forge/app.py']
    else:
        manifest['files']['release-manifest.json'] = '0' * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(package.PackageError, match=expected):
        package.expected_payload(source, TAG)


def test_duplicate_manifest_keys_rejected(source):
    path = source / 'release-manifest.json'
    path.write_text(path.read_text().replace('"app": "FORGE"', '"app": "FORGE", "app": "FORGE"'))
    with pytest.raises(package.PackageError, match='Duplicate JSON key'):
        package.expected_payload(source, TAG)


@pytest.mark.parametrize('name', [
    'app/forge/Data/private.txt', 'app/docs/history/release.md', 'app/Update FORGE.bat',
    'app/scripts/Update-Application.ps1', 'app/original_scripts/script.py',
    'app/tests/test_example.py', 'app/forge/models/model.onnx', 'app/forge/private.sqlite',
    'app/forge/.env.production', 'app/web/settings.json', 'app/Run Tests.bat',
    'app/forge/../secret.txt', 'app/forge//secret.txt', 'app/forge/secret:stream',
    'app/forge/NUL.txt', 'app/forge/COM¹.txt', 'app/forge/name.', 'app/forge\\secret.py',
    'FORGE-v0.5.0/forge/app.py', 'outside.txt', 'app/.github/workflows/release.yml', 'app/.',
])
def test_received_archive_rejects_unsafe_or_protected_paths_even_with_valid_hash(archive, name):
    files = contents(archive)
    files[name] = b'forbidden'
    update_package_manifest(files)
    save_archive(archive, files)
    with pytest.raises(package.PackageError):
        package.validate_archive(archive)


@pytest.mark.parametrize('mutation,expected', [
    ('changed', 'hash mismatch'), ('missing', 'missing or unlisted'),
    ('unlisted', 'missing or unlisted'), ('case', 'Case-insensitive'),
    ('directory_case', 'Case-insensitive'), ('directory_file', 'conflicting'),
    ('directory_noncanonical', 'Unsafe'),
])
def test_received_archive_inventory_and_directory_collisions(archive, mutation, expected):
    files = contents(archive)
    if mutation == 'changed':
        files['app/forge/app.py'] = b'# tampered\n'
    elif mutation == 'missing':
        del files['app/web/app.js']
    elif mutation == 'unlisted':
        files['app/forge/new.py'] = b'# unlisted\n'
    elif mutation == 'case':
        files['app/web/Assets/second.svg'] = b'case alias'
    elif mutation == 'directory_case':
        files['app/web/Assets/'] = b''
    elif mutation == 'directory_file':
        files['app/forge/app.py/'] = b''
    else:
        files['app/web/assets//'] = b''
    save_archive(archive, files)
    with pytest.raises(package.PackageError, match=expected):
        package.validate_archive(archive)


@pytest.mark.parametrize('special_type', ['symlink', 'fifo', 'reparse'])
def test_received_archive_links_and_special_files(archive, special_type):
    files = contents(archive)
    entry = zipfile.ZipInfo('app/web/app.js')
    entry.create_system = 3
    entry.external_attr = ((stat.S_IFLNK if special_type == 'symlink' else stat.S_IFIFO) | 0o644) << 16
    if special_type == 'reparse':
        entry.create_system = 0
        entry.external_attr = 0x400
    save_archive(archive, files, special=entry)
    with pytest.raises(package.PackageError, match='links and special files'):
        package.validate_archive(archive)


@pytest.mark.parametrize('compression', [zipfile.ZIP_BZIP2, zipfile.ZIP_DEFLATED])
def test_zip_requires_deterministic_stored_format(archive, compression):
    save_archive(archive, contents(archive), compression=compression)
    with pytest.raises(package.PackageError, match='require uncompressed ZIP_STORED'):
        package.validate_archive(archive)


def test_received_archive_rejects_null_truncated_entry_name(archive):
    archive.write_bytes(archive.read_bytes().replace(b'app/web/app.js', b'app/web/a\x00p.js'))
    with pytest.raises(package.PackageError, match='truncated or noncanonical'):
        package.validate_archive(archive)


@pytest.mark.parametrize('compression', [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_forged_size_cannot_hide_bytes_from_hash_validation(archive, source, compression):
    # zipfile can truncate a read to the declared uncompressed size. A Windows
    # extractor must never receive extra bytes that our source check did not see.
    files = contents(archive)
    name = 'app/web/app.js'
    original = files[name]
    files[name] += b'\nUNVERIFIED EXTRA BYTES\n'
    save_archive(archive, files, compression=compression)
    with zipfile.ZipFile(archive) as zipped:
        offset = zipped.getinfo(name).header_offset
        central = zipped.start_dir
    data = bytearray(archive.read_bytes())
    struct.pack_into('<I', data, offset + 14, zipfile.crc32(original))
    struct.pack_into('<I', data, offset + 22, len(original))
    while data[central:central + 4] == b'PK\x01\x02':
        name_size, extra_size, comment_size = struct.unpack_from('<HHH', data, central + 28)
        if data[central + 46:central + 46 + name_size] == name.encode():
            struct.pack_into('<I', data, central + 16, zipfile.crc32(original))
            struct.pack_into('<I', data, central + 24, len(original))
            break
        central += 46 + name_size + extra_size + comment_size
    else:
        pytest.fail('Test fixture did not contain the selected central-directory entry.')
    archive.write_bytes(data)
    with pytest.raises(package.PackageError, match='require uncompressed ZIP_STORED'):
        package.validate_archive(archive, source=source)


def test_optional_source_comparison_detects_self_consistent_repacked_application(source, archive):
    files = contents(archive)
    files['app/web/app.js'] = b'console.log("changed application");\n'
    update_package_manifest(files)
    save_archive(archive, files)
    package.validate_archive(archive)
    with pytest.raises(package.PackageError, match='does not match the selected source'):
        package.validate_archive(archive, source=source)


@pytest.mark.parametrize('contents', ['0' * 64 + '  ' + ASSET + '\n',
                                    '{digest}  wrong-name.zip\n', '{digest} *' + ASSET + '\n'])
def test_checksum_requires_exact_digest_and_basename(archive, contents):
    checksum = archive.with_name(archive.name + '.sha256')
    checksum.write_text(contents.format(digest=package.sha256(archive.read_bytes())))
    with pytest.raises(package.PackageError, match='checksum or archive basename'):
        package.validate_archive(archive, checksum=checksum)


def test_archive_size_limit_applies_before_reading_entries(archive, monkeypatch):
    monkeypatch.setattr(package, 'MAX_TOTAL', 100)
    with pytest.raises(package.PackageError, match='exceeds the size limit'):
        package.validate_archive(archive)


def test_cli_build_and_received_asset_validation(source, tmp_path):
    output = tmp_path / 'cli-output'
    result = subprocess.run([sys.executable, str(SCRIPT), 'build', '--source', str(source),
                             '--output', str(output), '--tag', TAG], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert Path(report['archive']).name == ASSET
    assert report['tag'] == TAG and report['root'] == 'app'
    result = subprocess.run([sys.executable, str(SCRIPT), 'validate', report['archive'],
                             '--checksum', report['checksum'], '--tag', TAG, '--source', str(source)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['sha256'] == report['sha256']
