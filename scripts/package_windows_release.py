"""Build or verify the app/ ZIP consumed by Birthright's Windows updater.

This is a source application payload, not a bundled Python environment or native
executable. The original tagged release manifest is verified before filtering;
only the new package manifest is regenerated. No application code is executed.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zipfile


MAX_FILES = 5000
MAX_TOTAL = 250 * 1024 * 1024
MAX_MANIFEST = 2_000_000
VERSION_PATTERN = r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
APP_DIRS = {'forge', 'web', 'scripts', 'tests', 'docs', 'original_scripts'}
ROOT_FILES = {
    '.gitignore', 'CHANGELOG.md', 'LICENSE.txt', 'README.md', 'START HERE.txt',
    'VERSION.txt', 'release-manifest.json', 'Open FORGE.bat', 'Stop FORGE.bat',
    'Setup FORGE.bat', 'Update FORGE.bat', 'Repair Start Shortcut.bat', 'Run Tests.bat',
    'Install Downloads.bat', 'Install Transcription.bat', 'Install Speaker Labels.bat',
    'launcher.py', 'server.py', 'pytest.ini', 'requirements-core.txt',
    'requirements-dev.txt', 'requirements-transcription.txt', 'requirements-downloads.txt',
    'requirements-diarization.txt',
}
# Exact case-insensitive component blacklist in the supplied Update-Application.ps1.
UPDATER_PROTECTED = {
    'build', 'update-backups', 'update-logs', 'data', 'userdata', 'user-data',
    'settings', 'config', 'presets', 'queue', 'history', 'logs', 'projects',
    'workspace', 'uploads', 'outputs', '.git', '.venv', 'venv', 'node_modules',
    'update forge.bat', 'update true engine.bat', 'update plate.bat',
    'update-application.ps1',
}
PROTECTED_COMPONENTS = UPDATER_PROTECTED | {
    'backups', '.diarization-venv', 'tools', 'models', 'staging', 'work', 'exports',
    '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.cache',
}
PRIVATE_FILES = {
    '.env', 'runtime.json', 'service.log', 'settings.json', 'config.json',
    'presets.json', 'queue.json', 'history.json', 'cookies.txt', 'credentials.json',
    'installed-versions.txt', 'update-last.json', '.forge-update-state.json',
    '.forge-updating',
}
EXCLUDED_SUFFIXES = {
    '.exe', '.dll', '.pyd', '.so', '.dylib', '.lnk', '.pyc', '.pyo', '.whl',
    '.zip', '.7z', '.rar', '.sqlite', '.sqlite3', '.db', '.safetensors', '.onnx',
    '.pt', '.pth', '.gguf',
}
DEVELOPMENT_FILES = {'Run Tests.bat', 'pytest.ini', 'requirements-dev.txt'}
REQUIRED_FILES = {
    'VERSION.txt', 'LICENSE.txt', 'launcher.py', 'server.py', 'requirements-core.txt',
    'Open FORGE.bat', 'Setup FORGE.bat', 'Stop FORGE.bat', 'forge/__init__.py',
    'forge/app.py', 'forge/core.py', 'forge/models.py', 'forge/worker.py', 'forge/queue.py',
    'web/index.html', 'web/app.js', 'web/style.css', 'scripts/setup.ps1', 'scripts/update.py',
}


class PackageError(ValueError):
    """The source or archive does not satisfy the Windows package contract."""


def version_from_tag(tag: str) -> str:
    if not isinstance(tag, str) or not re.fullmatch('v' + VERSION_PATTERN, tag):
        raise PackageError('Use an exact release tag such as v0.5.0.')
    return tag[1:]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PackageError('Duplicate JSON key in release manifest.')
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except PackageError:
        raise
    except (ValueError, UnicodeError) as error:
        raise PackageError('Invalid release manifest JSON.') from error
    if not isinstance(value, dict):
        raise PackageError('Release manifest must be a JSON object.')
    return value


def safe_relative(value: str, *, directory: bool = False) -> PurePosixPath:
    if (not isinstance(value, str) or not value or len(value) > 1000
            or re.search(r'[\\<>:"|?*\x00-\x1f]', value)):
        raise PackageError('Unsafe application path.')
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or str(path) != value
            or any(part in {'.', '..'} or part.endswith((' ', '.')) for part in path.parts)):
        raise PackageError('Unsafe application path: ' + value)
    if any(re.match(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', part, re.I)
           for part in path.parts):
        raise PackageError('Reserved Windows filename: ' + value)
    if len(path.parts) == 1:
        allowed = APP_DIRS if directory else ROOT_FILES
        if value not in allowed:
            raise PackageError('Unregistered application root path: ' + value)
    elif path.parts[0] not in APP_DIRS:
        raise PackageError('Unregistered application directory: ' + value)
    return path


def excluded(relative: str) -> bool:
    path = PurePosixPath(relative)
    parts = [part.casefold() for part in path.parts]
    return (relative in DEVELOPMENT_FILES or parts[0] in {'tests', 'original_scripts'}
            or (len(parts) >= 2 and parts[0] == 'docs' and parts[1] in {'previews', 'screenshots'})
            or any(part in PROTECTED_COMPONENTS for part in parts)
            or any(part in PRIVATE_FILES or part.startswith(('.env.', '.forge-')) for part in parts)
            or path.suffix.casefold() in EXCLUDED_SUFFIXES
            or any(part.endswith(('.sqlite3-wal', '.sqlite3-shm', '.sqlite-wal', '.sqlite-shm'))
                   for part in parts))


def payload_path(relative: str, *, directory: bool = False) -> PurePosixPath:
    path = safe_relative(relative, directory=directory)
    if excluded(relative):
        raise PackageError('Protected or non-application path in Windows payload: ' + relative)
    return path


def check_case_paths(paths, explicit_directories=()):
    spellings = {}
    files = set()
    directories = set()
    for relative, is_directory in [(p, False) for p in paths] + [(p, True) for p in explicit_directories]:
        parts = PurePosixPath(relative).parts
        for index in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:index])
            key = prefix.casefold()
            if key in spellings and spellings[key] != prefix:
                raise PackageError('Case-insensitive application path collision: ' + relative)
            spellings[key] = prefix
        folded = relative.casefold()
        parents = {'/'.join(parts[:index]).casefold() for index in range(1, len(parts))}
        if folded in files or (not is_directory and folded in directories) or parents & files:
            raise PackageError('Duplicate or conflicting application path: ' + relative)
        (directories if is_directory else files).add(folded)
        directories.update(parents)


def manifest_inventory(raw: bytes, expected_version: str, *, windows: bool = False):
    if len(raw) > MAX_MANIFEST:
        raise PackageError('Release manifest is too large.')
    manifest = read_json(raw)
    if (manifest.get('app') != 'FORGE' or type(manifest.get('format')) is not int
            or manifest['format'] != 1 or manifest.get('version') != expected_version):
        raise PackageError('Release manifest identity or version does not match the requested tag.')
    minimum = manifest.get('minimum_from', '0.1.0')
    if not isinstance(minimum, str) or not re.fullmatch(VERSION_PATTERN, minimum):
        raise PackageError('Invalid minimum source version.')
    files = manifest.get('files')
    if not isinstance(files, dict) or not files or len(files) > MAX_FILES - 1:
        raise PackageError('Release manifest has an invalid file inventory.')
    for relative, digest in files.items():
        (payload_path if windows else safe_relative)(relative)
        if relative == 'release-manifest.json':
            raise PackageError('A release manifest cannot list itself.')
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise PackageError('Invalid release file hash: ' + relative)
    check_case_paths(files)
    missing = REQUIRED_FILES - files.keys()
    if missing:
        raise PackageError('Required application files are missing: ' + ', '.join(sorted(missing)))
    if windows:
        package = manifest.get('package', {})
        if (not isinstance(package, dict) or package.get('layout') != 'app'
                or package.get('architecture') != 'x64' or package.get('platform') != 'windows'
                or package.get('type') != 'source'
                or not re.fullmatch(r'[a-f0-9]{64}', str(package.get('source_manifest_sha256', '')))):
            raise PackageError('Invalid Windows package metadata.')
    return manifest, files


def check_versions(files: dict[str, bytes], version: str):
    try:
        text_version = files['VERSION.txt'].decode('utf-8').strip()
        tree = ast.parse(files['forge/__init__.py'], filename='forge/__init__.py')
        values = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets)]
        if text_version != version or values != [version]:
            raise PackageError('Application source versions do not match the requested release.')
        for relative, content in files.items():
            if relative.endswith('.py'):
                compile(content, relative, 'exec')
    except (SyntaxError, UnicodeError, ValueError, TypeError) as error:
        if isinstance(error, PackageError):
            raise
        raise PackageError('Invalid Python source or version declaration.') from error


def is_link(path: Path) -> bool:
    """Reject Unix symlinks and Windows reparse points, including junctions."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def source_file(root: Path, relative: str) -> bytes:
    path = root
    for part in PurePosixPath(relative).parts:
        path /= part
        if is_link(path):
            raise PackageError('Source links are not accepted: ' + relative)
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise PackageError('Source entry is not a regular file: ' + relative)
        if metadata.st_size > MAX_TOTAL:
            raise PackageError('Source file exceeds the size limit: ' + relative)
        return path.read_bytes()
    except OSError as error:
        raise PackageError('Source manifest file is unavailable: ' + relative) from error


def expected_payload(source: Path, tag: str) -> tuple[dict[str, bytes], dict]:
    version = version_from_tag(tag)
    source = Path(source)
    if is_link(source) or not source.is_dir():
        raise PackageError('Select a real source checkout directory, not a link.')
    source = source.resolve()
    raw = source_file(source, 'release-manifest.json')
    manifest, inventory = manifest_inventory(raw, version)
    files = {}
    total = 0
    # Verify the whole source manifest, including files intentionally excluded
    # from this installer format. A stale source manifest is never refreshed here.
    for relative, digest in inventory.items():
        content = source_file(source, relative)
        total += len(content)
        if total > MAX_TOTAL:
            raise PackageError('Source release exceeds the size limit.')
        if sha256(content) != digest:
            raise PackageError('Source manifest hash mismatch: ' + relative)
        files[relative] = content
    # Detect an incomplete/stale inventory without traversing private root data
    # directories. Cache files created by tests are intentionally disregarded.
    candidates = [path for path in source.iterdir() if path.is_file() and path.name in ROOT_FILES]
    for folder in APP_DIRS:
        base = source / folder
        if is_link(base):
            raise PackageError('Source application directories cannot be links: ' + folder)
        if not base.is_dir():
            continue
        for directory, names, filenames in os.walk(base, followlinks=False):
            names[:] = [name for name in names if name not in {'__pycache__', '.pytest_cache'}]
            for name in names:
                if is_link(Path(directory) / name):
                    raise PackageError('Source application directories cannot contain links.')
            candidates.extend(Path(directory) / name for name in filenames if not name.endswith(('.pyc', '.pyo')))
    for path in candidates:
        relative = path.relative_to(source).as_posix()
        if relative != 'release-manifest.json' and relative not in inventory:
            raise PackageError('Source manifest omits an application file: ' + relative)
    check_versions(files, version)
    retained = {relative: content for relative, content in files.items() if not excluded(relative)}
    package_manifest = {
        'app': 'FORGE', 'format': 1, 'version': version,
        'minimum_from': manifest.get('minimum_from', '0.1.0'),
        'note': 'Windows source application payload. User state, original scripts, historical docs, tests, and protected updater entrypoints are excluded. Checksums do not prove publisher authenticity.',
        'package': {'platform': 'windows', 'architecture': 'x64', 'layout': 'app',
                    'type': 'source', 'source_manifest_sha256': sha256(raw)},
        'files': {relative: sha256(content) for relative, content in sorted(retained.items())},
    }
    encoded = (json.dumps(package_manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    manifest_inventory(encoded, version, windows=True)
    retained['release-manifest.json'] = encoded
    return retained, package_manifest


def validate_archive(archive: Path, *, tag: str | None = None, checksum: Path | None = None,
                     source: Path | None = None) -> dict:
    archive = Path(archive)
    match = re.fullmatch(r'FORGE-v(' + VERSION_PATTERN + r')-windows-x64\.zip', archive.name)
    if not match:
        raise PackageError('Windows asset name must be FORGE-vMAJOR.MINOR.PATCH-windows-x64.zip.')
    version = match.group(1)
    if tag is not None and version_from_tag(tag) != version:
        raise PackageError('Windows archive filename does not match the requested tag.')
    if is_link(archive) or not archive.is_file():
        raise PackageError('Windows archive must be a regular file, not a link.')
    if archive.stat().st_size > MAX_TOTAL + 10_000_000:
        raise PackageError('Windows archive exceeds the size limit.')
    try:
        with zipfile.ZipFile(archive) as zipped:
            entries = zipped.infolist()
            if len(entries) > MAX_FILES or sum(entry.file_size for entry in entries) > MAX_TOTAL:
                raise PackageError('Windows payload exceeds the size limit.')
            seen = set()
            files = {}
            directories = []
            for entry in entries:
                mode = entry.external_attr >> 16
                if (entry.flag_bits & 1 or stat.S_ISLNK(mode)
                        or entry.external_attr & 0x400
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise PackageError('Encrypted entries, links and special files are not allowed.')
                if entry.compress_type != zipfile.ZIP_STORED or entry.compress_size != entry.file_size:
                    raise PackageError('Windows release assets require uncompressed ZIP_STORED entries with exact sizes.')
                name = entry.filename
                if name != entry.orig_filename:
                    raise PackageError('ZIP entry contains a truncated or noncanonical filename.')
                if not name.startswith('app/') or name == 'app':
                    raise PackageError('Every archive entry must be beneath the single app/ root.')
                if name.casefold() in seen:
                    raise PackageError('Duplicate archive entry.')
                seen.add(name.casefold())
                relative = name[len('app/'):]
                if entry.is_dir():
                    if relative:
                        directory = relative[:-1]
                        payload_path(directory, directory=True)
                        directories.append(directory)
                    continue
                if stat.S_IFMT(mode) == stat.S_IFDIR:
                    raise PackageError('A file entry has directory attributes.')
                payload_path(relative)
                if relative == 'release-manifest.json' and entry.file_size > MAX_MANIFEST:
                    raise PackageError('Release manifest is too large.')
                files[relative] = zipped.read(entry)
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise PackageError('Cannot read Windows release ZIP: ' + str(error)) from error
    if 'release-manifest.json' not in files:
        raise PackageError('Windows archive is missing its app/release-manifest.json.')
    check_case_paths(files, directories)
    manifest, inventory = manifest_inventory(files['release-manifest.json'], version, windows=True)
    if set(files) != set(inventory) | {'release-manifest.json'}:
        raise PackageError('Windows archive has missing or unlisted application files.')
    for relative, digest in inventory.items():
        if sha256(files[relative]) != digest:
            raise PackageError('Windows archive manifest hash mismatch: ' + relative)
    check_versions(files, version)
    source_report = {}
    if source is not None:
        expected, _ = expected_payload(source, 'v' + version)
        if files != expected:
            raise PackageError('Windows archive does not match the selected source checkout.')
        source_inventory = read_json(source_file(Path(source), 'release-manifest.json'))['files']
        source_report = {'source_files_verified': len(source_inventory),
                         'excluded_source_files': len(source_inventory) - len(inventory),
                         'source_comparison': 'unchanged application bytes'}
    digest = sha256(archive.read_bytes())
    if checksum is not None:
        checksum = Path(checksum)
        if is_link(checksum) or not checksum.is_file() or checksum.stat().st_size > 1024:
            raise PackageError('Checksum must be a small regular file, not a link.')
        try:
            contents = checksum.read_text(encoding='ascii').rstrip('\r\n')
        except (OSError, UnicodeError) as error:
            raise PackageError('Cannot read the SHA-256 checksum file.') from error
        if contents != digest + '  ' + archive.name:
            raise PackageError('SHA-256 checksum or archive basename does not match.')
    return {'archive': str(archive), 'tag': 'v' + version, 'version': version,
            'platform': 'windows', 'architecture': 'x64', 'root': 'app',
            'application_files': len(inventory), 'archive_files': len(files),
            'sha256': digest, 'source_manifest_sha256': manifest['package']['source_manifest_sha256'],
            **source_report}


def write_new_or_same(path: Path, data: bytes):
    if is_link(path):
        raise PackageError('Refusing to write through an output link: ' + str(path))
    try:
        stream = path.open('xb')
    except FileExistsError:
        if not path.is_file() or path.read_bytes() != data:
            raise PackageError('Output exists with different contents; choose a clean output directory: ' + str(path))
        return
    try:
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def release_timestamp(version: str) -> tuple[int, ...]:
    """Unique DOS timestamp per supported version; independent of build clocks.

    Base-1000 components map bijectively to two-second ZIP timestamp slots.
    All supported versions fit between 2000 and 2064 (also within signed Unix
    timestamp capacity through normal 0.x versions). Never reuse a version for
    changed source. Fail explicitly instead of wrapping an oversized component.
    """
    major, minor, patch = map(int, version_from_tag('v' + version).split('.'))
    if max(major, minor, patch) > 999:
        raise PackageError('Windows ZIP timestamps support version components 0 through 999.')
    value = datetime(2000, 1, 1) + timedelta(seconds=2 * (major * 1000000 + minor * 1000 + patch))
    return (value.year, value.month, value.day, value.hour, value.minute, value.second)


def build(source: Path, output: Path, *, tag: str) -> Path:
    version = version_from_tag(tag)
    timestamp = release_timestamp(version)
    files, _ = expected_payload(source, tag)
    output = Path(output)
    if is_link(output):
        raise PackageError('Output directory cannot be a link.')
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = f'FORGE-v{version}-windows-x64.zip'
    # Version-specific, deterministic mtimes invalidate timestamp-based Python
    # caches retained by the owner's unchanged Copy-Item updater.
    with tempfile.TemporaryDirectory(prefix='.forge-windows-', dir=output) as temporary:
        candidate = Path(temporary) / name
        with zipfile.ZipFile(candidate, 'w', compression=zipfile.ZIP_STORED) as zipped:
            for relative, content in sorted(files.items()):
                info = zipfile.ZipInfo('app/' + relative, date_time=timestamp)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                zipped.writestr(info, content)
        validate_archive(candidate, tag=tag)
        data = candidate.read_bytes()
    archive = output / name
    checksum = archive.with_name(archive.name + '.sha256')
    checksum_data = (sha256(data) + '  ' + archive.name + '\n').encode('ascii')
    for path, content in ((archive, data), (checksum, checksum_data)):
        if is_link(path) or (path.exists() and (not path.is_file() or path.read_bytes() != content)):
            raise PackageError('Refusing to replace a different existing output: ' + str(path))
    write_new_or_same(archive, data)
    write_new_or_same(checksum, checksum_data)
    validate_archive(archive, tag=tag, checksum=checksum)
    return archive


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('build', help='Verify tagged source and create its app/ Windows asset.')
    create.add_argument('--source', required=True, type=Path)
    create.add_argument('--output', required=True, type=Path)
    create.add_argument('--tag', required=True)
    verify = commands.add_parser('validate', help='Validate a received Windows asset without extracting it.')
    verify.add_argument('archive', type=Path)
    verify.add_argument('--tag')
    verify.add_argument('--checksum', type=Path)
    verify.add_argument('--source', type=Path, help='Also compare with this verified source manifest and its files.')
    args = parser.parse_args(argv)
    try:
        if args.command == 'build':
            archive = build(args.source, args.output, tag=args.tag)
            checksum = archive.with_name(archive.name + '.sha256')
            result = validate_archive(archive, tag=args.tag, checksum=checksum, source=args.source)
            result['checksum'] = str(checksum)
        else:
            result = validate_archive(args.archive, tag=args.tag, checksum=args.checksum, source=args.source)
    except (PackageError, OSError) as error:
        parser.exit(1, 'Windows package validation failed: ' + str(error) + '\n')
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
