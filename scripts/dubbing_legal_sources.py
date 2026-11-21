"""Collect exact public legal/source evidence without installing or executing it.

Normal urllib TLS/proxy settings are inherited. An explicit higher download
limit is required for a separately reviewed large artifact manifest.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import platform
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

SHA = re.compile(r'^[a-f0-9]{64}$')
MAX_NOTICE = 1024 * 1024
MAX_NOTICES = 4 * 1024 * 1024
DEFAULT_LIMIT = 40 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]
INVENTORY = 'forge/dubbing_legal_sources_inventory.json'
SOURCE_FILES = ('scripts/dubbing_legal_sources.py', INVENTORY)


class EvidenceError(ValueError):
    """Controlled internal diagnostics with a safe machine-readable category."""

    def __init__(self, reason_code, message):
        super().__init__(message)
        self.reason_code = reason_code


def error_record(error):
    record = {'error': 'ValueError' if isinstance(error, EvidenceError) else type(error).__name__,
              'reason': 'Public evidence verification failed; expected integrity and normal network/TLS checks were retained.'}
    if isinstance(error, EvidenceError):
        record['reason_code'] = error.reason_code
    elif isinstance(error, urllib.error.HTTPError):
        record['reason_code'] = 'http_error'
        if type(error.code) is int and 100 <= error.code <= 599:
            record['http_status'] = error.code
    elif isinstance(error, urllib.error.URLError):
        record['reason_code'] = 'network_transport_error'
    elif isinstance(error, TimeoutError):
        record['reason_code'] = 'network_timeout'
    elif isinstance(error, ValueError):
        record['reason_code'] = 'external_value_error'
    elif isinstance(error, OSError):
        record['reason_code'] = 'io_error'
    else:
        record['reason_code'] = 'external_error'
    if isinstance(error, EvidenceError) and hasattr(error, 'source_attempts'):
        record['source_attempts'] = error.source_attempts
    return record


def relative(value):
    if not isinstance(value, str) or not value or re.search(r'[\\<>:"|?*\x00-\x1f]', value):
        raise EvidenceError('unsafe_evidence_path', 'Unsafe public evidence output path.')
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or str(path) != value
            or any(part in {'.', '..'} or part.endswith((' ', '.')) for part in path.parts)
            or any(re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part, re.I) for part in path.parts)):
        raise EvidenceError('unsafe_evidence_path', 'Unsafe public evidence output path.')
    return path


def safe_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password
            or parsed.fragment or re.search(r'[\x00-\x20]', value)):
        raise EvidenceError('unsafe_public_url', 'Public evidence requires an ordinary HTTPS URL without credentials.')
    return value


class HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        safe_url(url)
        return super().redirect_request(request, response, code, message, headers, url)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise EvidenceError('duplicate_json_key', 'Duplicate public source inventory key.')
            value[key] = item
        return value
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise EvidenceError('inventory_unavailable_or_oversized', 'Public source inventory is missing or oversized.')
    result = json.loads(path.read_text('utf-8'), object_pairs_hook=unique,
                        parse_constant=lambda value: (_ for _ in ()).throw(EvidenceError('invalid_json_number', 'Invalid JSON number.')))
    if not isinstance(result, dict):
        raise EvidenceError('inventory_not_object', 'Public source inventory must be an object.')
    return result


def ordinary(path):
    path = Path(path)
    for item in [path, *path.parents]:
        if item.is_symlink() or item.exists() and getattr(item.lstat(), 'st_file_attributes', 0) & 0x400:
            raise EvidenceError('linked_path', 'Linked/reparse source and output paths are not allowed.')
    return path


def source_identity(root, manifest_path):
    """Bind this verifier and its inputs to exact committed application bytes."""
    root = ordinary(Path(root).absolute())
    manifest_path = ordinary(Path(manifest_path).absolute())
    if manifest_path != root / INVENTORY:
        raise EvidenceError('unregistered_inventory', 'Use the registered source-bound public legal inventory.')
    manifest = ordinary(root / 'release-manifest.json')
    value = read_json(manifest)
    files = value.get('files')
    if value.get('app') != 'FORGE' or not isinstance(files, dict):
        raise EvidenceError('invalid_source_manifest', 'Application source manifest is invalid.')
    identities = {}
    for name in SOURCE_FILES:
        path = ordinary(root / name)
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise EvidenceError('source_file_unavailable', 'Required source file is missing or oversized.')
        identities[name] = digest(path.read_bytes())
        if files.get(name) != identities[name]:
            raise EvidenceError('source_manifest_hash_mismatch', 'Application manifest does not bind verifier/inventory bytes.')
    if not (root / '.git').exists():
        raise EvidenceError('git_source_required', 'This native source-verification job requires its exact Git checkout.')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise EvidenceError('source_commit_invalid', 'An exact source commit is required.')
    committed = subprocess.check_output(['git', 'show', 'HEAD:release-manifest.json'], cwd=root)
    if committed != manifest.read_bytes():
        raise EvidenceError('source_manifest_changed', 'The application source manifest differs from its committed identity.')
    clean = not bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, text=True).strip())
    return {'source_commit': commit, 'source_manifest_sha256': digest(committed),
            'source_files_sha256': identities, 'source_kind': 'git', 'source_checkout_clean': clean}


def identity(data, expected):
    expected_size = expected.get('expected_size', expected.get('size'))
    expected_sha = expected.get('expected_sha256', expected.get('sha256'))
    if expected_size is not None and (type(expected_size) is not int or len(data) != expected_size):
        raise EvidenceError('artifact_size_mismatch', 'Public evidence size differs from its expected identity.')
    if expected_sha is not None and (not SHA.fullmatch(expected_sha) or digest(data) != expected_sha):
        raise EvidenceError('artifact_sha256_mismatch', 'Public evidence SHA-256 differs from its expected identity.')
    blob = expected.get('expected_git_blob_sha1', expected.get('git_blob_sha1'))
    if blob is not None:
        actual = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()
        if not re.fullmatch(r'[a-f0-9]{40}', blob) or actual != blob:
            raise EvidenceError('git_blob_mismatch', 'Public evidence Git blob identity differs.')
    return {'size': len(data), 'sha256': digest(data), **({'git_blob_sha1': blob} if blob else {})}


def download(item, destination, budget, *, staging=None):
    alternatives = item.get('alternative_urls', [])
    if not isinstance(alternatives, list) or len(alternatives) > 3 or any(not isinstance(url, str) for url in alternatives):
        raise EvidenceError('invalid_alternative_sources', 'Use a bounded list of exact public artifact source aliases.')
    sources = list(dict.fromkeys([item['url'], *alternatives]))
    for url in sources:
        safe_url(url)
    attempts = []
    for index, url in enumerate(sources):
        try:
            result = _download_one({**item, 'url': url}, destination, budget, staging=staging)
            if attempts:
                result['source_attempts'] = [*attempts, {'source_index': index, 'status': 'identity_verified'}]
            return result
        except Exception as error:
            retryable = (isinstance(error, (urllib.error.URLError, TimeoutError))
                         or isinstance(error, EvidenceError) and error.reason_code in {
                             'artifact_sha256_mismatch', 'artifact_size_mismatch',
                             'transfer_length_mismatch', 'content_length_mismatch'})
            if len(sources) == 1 or not retryable:
                raise
            attempts.append({'source_index': index, **error_record(error)})
    failure = EvidenceError('all_sources_failed', 'All registered source aliases failed without weakening expected artifact identity.')
    failure.source_attempts = attempts
    raise failure


def _download_one(item, destination, budget, *, staging=None):
    safe_url(item['url'])
    expected = item.get('expected_size', item.get('size'))
    if expected is not None and (type(expected) is not int or expected <= 0):
        raise EvidenceError('expected_size_invalid', 'Expected artifact size must be a positive integer or explicitly unknown.')
    maximum = item.get('max_bytes', expected if type(expected) is int else MAX_NOTICE)
    if type(maximum) is not int or maximum <= 0 or maximum > budget['limit']:
        raise EvidenceError('artifact_download_bound', 'Artifact exceeds the explicitly selected download bound.')
    if type(expected) is int and (expected > maximum or expected > budget['limit'] - budget['used']):
        raise EvidenceError('remaining_download_bound', 'Artifact exceeds remaining bounded download allowance.')
    if not isinstance(item.get('sha256'), str) or not SHA.fullmatch(item['sha256']):
        raise EvidenceError('expected_sha256_invalid', 'An independently expected SHA-256 is required before downloading.')
    blob = item.get('expected_git_blob_sha1', item.get('git_blob_sha1'))
    if blob is not None and (not isinstance(blob, str) or not re.fullmatch(r'[a-f0-9]{40}', blob)):
        raise EvidenceError('expected_git_blob_invalid', 'An expected Git blob identity must be exact before downloading.')
    destination = ordinary(Path(destination).absolute())
    if destination.exists():
        raise EvidenceError('existing_destination', 'Existing evidence destinations are preserved; choose a fresh output.')
    public = next((parent for parent in destination.parents if parent.name == 'public_source_texts'), None)
    staging = ordinary(Path(staging).absolute()) if staging is not None else (
        (public.parent if public is not None else destination.parent) / '.transfer-staging')
    if public is not None and staging.resolve().is_relative_to(public.resolve()):
        raise EvidenceError('unsafe_transfer_staging', 'Unverified transfers must stay outside the public upload tree.')
    opener = urllib.request.build_opener(HttpsRedirect())
    request = urllib.request.Request(item['url'], headers={'User-Agent': 'FORGE-V010-public-legal-source-verifier', 'Accept-Encoding': 'identity'})
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    count = 0
    hasher = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix='transfer-', dir=staging) as private:
        partial = Path(private) / 'artifact.part'
        with opener.open(request, timeout=45) as response, partial.open('xb') as output:
            declared = response.headers.get('Content-Length')
            declared = int(declared) if declared and declared.isdigit() else None
            if declared is not None and (declared > maximum or type(expected) is int and declared != expected):
                raise EvidenceError('content_length_mismatch', 'Publisher Content-Length exceeds or differs from bounded identity.')
            while block := response.read(1024 * 1024):
                count += len(block)
                budget['used'] += len(block)
                if count > maximum or budget['used'] > budget['limit']:
                    raise EvidenceError('download_byte_bound', 'Public evidence download exceeded its reviewed resource bound.')
                hasher.update(block)
                output.write(block)
        if type(expected) is int and count != expected or count <= 0:
            raise EvidenceError('artifact_size_mismatch', 'Downloaded public artifact failed expected size verification.')
        if hasher.hexdigest() != item['sha256']:
            raise EvidenceError('artifact_sha256_mismatch', 'Downloaded public artifact failed expected SHA-256 verification.')
        if declared is not None and count != declared:
            raise EvidenceError('transfer_length_mismatch', 'Public artifact transfer did not match Content-Length.')
        if blob is not None:
            git = hashlib.sha1(b'blob ' + str(count).encode('ascii') + b'\0')
            with partial.open('rb') as stream:
                while block := stream.read(1024 * 1024):
                    git.update(block)
            if git.hexdigest() != blob:
                raise EvidenceError('git_blob_mismatch', 'Public evidence Git blob identity differs.')
        result = {'url': item['url'], 'size': count, 'sha256': hasher.hexdigest(),
                  'expected_size': expected, 'publisher_content_length': declared,
                  'independent_expected_size_available': type(expected) is int,
                  'identity_verified': True, 'retained': True,
                  **({'git_blob_sha1': blob} if blob is not None else {})}
        if public is not None:
            if count > MAX_NOTICE:
                return {**result, 'retained': False, 'text_omission': 'per_file_text_bound'}
            try:
                text = partial.read_bytes().decode('utf-8-sig')
            except UnicodeError:
                return {**result, 'retained': False, 'text_omission': 'not_utf8_plain_text'}
            if any(ord(char) < 32 and char not in '\n\r\t\f' for char in text):
                return {**result, 'retained': False, 'text_omission': 'non_text_control_bytes'}
        # Same-volume staging keeps publication atomic. Hard-link creation is
        # exclusive on Windows and POSIX: an existing destination is never
        # overwritten, and no partial transfer enters the upload allowlist.
        ordinary(destination)
        os.link(partial, destination)
    return result


def archive_members(archive, kind):
    if kind not in {'tar', 'zip'}:
        raise EvidenceError('archive_format_invalid', 'Only bounded tar/ZIP evidence archives are registered.')
    if kind == 'zip':
        handle = zipfile.ZipFile(archive)
        def read_member(member):
            mode = stat.S_IFMT(member.external_attr >> 16)
            if member.flag_bits & 1 or mode not in {0, stat.S_IFREG}:
                raise EvidenceError('unsafe_selected_zip_member', 'Selected ZIP evidence is encrypted, linked or a special file.')
            return handle.read(member)
        for member in handle.infolist():
            if not member.is_dir():
                yield member.filename, member.file_size, lambda member=member: read_member(member)
    else:
        handle = tarfile.open(archive, 'r:*')
        for member in handle:
            if member.isfile():
                yield member.name, member.size, lambda member=member: handle.extractfile(member).read()
    handle.close()


def legal_member(name):
    parts = PurePosixPath(name).parts
    if not parts:
        return False
    normalized = lambda value: re.sub(r'[^a-z0-9]', '', value.lower())
    return (normalized(parts[-1]).startswith(('license', 'licence', 'copying', 'notice', 'copyright',
                                             'eula', 'thirdparty', 'tpp', 'authors', 'redist'))
            or any(normalized(part) in {'license', 'licenses', 'licence', 'licences',
                                        'notice', 'notices', 'thirdparty', 'thirdpartynotices',
                                        'thirdpartylicenses', 'thirdpartylicences'} for part in parts[:-1]))


def archive_index(archive, kind):
    """Expose bounded candidate names/types, including unextracted links."""
    names = hashlib.sha256()
    candidates = []
    count = 0
    if kind == 'zip':
        handle = zipfile.ZipFile(archive)
        entries = ((entry.filename, entry.file_size,
                    'directory' if entry.is_dir() else
                    'file' if stat.S_IFMT(entry.external_attr >> 16) in {0, stat.S_IFREG} else 'link_or_special')
                   for entry in handle.infolist())
    elif kind == 'tar':
        handle = tarfile.open(archive, 'r:*')
        entries = ((entry.name, entry.size, 'file' if entry.isfile() else
                    'directory' if entry.isdir() else 'link_or_special') for entry in handle)
    else:
        raise EvidenceError('archive_format_invalid', 'Only bounded tar/ZIP evidence archives are registered.')
    with handle:
        for name, size, entry_kind in entries:
            count += 1
            if count > 50000:
                raise EvidenceError('archive_member_count_bound', 'Source archive contains too many members.')
            clean = str(relative(name.rstrip('/') if entry_kind == 'directory' else name))
            row = {'path': clean, 'size': size, 'kind': entry_kind}
            names.update(json.dumps(row, sort_keys=True, separators=(',', ':')).encode('utf-8') + b'\n')
            if legal_member(clean):
                candidates.append(row)
                if len(candidates) > 2000 or len(json.dumps(candidates).encode('utf-8')) > 512 * 1024:
                    raise EvidenceError('archive_legal_index_bound', 'The complete legal candidate index exceeded its bound.')
    return {'archive_entry_count': count, 'canonical_entry_index_sha256': names.hexdigest(),
            'legal_candidates': candidates, 'legal_candidate_count': len(candidates),
            'legal_candidates_complete': True,
            'index_scope': 'All archive entry names/types/sizes were indexed; only bounded plain legal/source text is retained.'}


def collect_archive(archive, item, output):
    wanted = item.get('required_members', [])
    if not isinstance(wanted, list) or len(wanted) > 100:
        raise EvidenceError('required_member_inventory_invalid', 'Invalid bounded archive member inventory.')
    found = set()
    records = []
    legal_bytes = 0
    seen = set()
    retained_paths = set()
    count = 0
    for name, size, reader in archive_members(archive, item.get('format', 'tar')):
        count += 1
        if count > 50000:
            raise EvidenceError('archive_member_count_bound', 'Source archive contains too many members.')
        path = relative(name)
        stripped = '/'.join(path.parts[1:]) if len(path.parts) > 1 else str(path)
        matches = [(index, required) for index, required in enumerate(wanted)
                   if required.get('path') == stripped or required.get('path') == str(path)
                   or required.get('path_suffix') and ('/' + str(path)).endswith(required['path_suffix'])]
        legal = item.get('collect_legal_members') is True and legal_member(str(path))
        if not matches and not legal:
            continue
        if name.casefold() in seen:
            raise EvidenceError('duplicate_archive_member', 'Duplicate retained archive evidence member.')
        seen.add(name.casefold())
        # Required native DLLs are read only for hash comparison, never retained.
        retained = legal or any(required.get('retain', True) for _, required in matches)
        maximum = MAX_NOTICE if retained else max((required.get('expected_size') or 0 for _, required in matches), default=0)
        if type(size) is not int or size <= 0 or size > maximum:
            raise EvidenceError('archive_member_byte_bound', 'Selected public archive evidence exceeds its bounded size.')
        data = reader()
        values = identity(data, {})
        for index, required in matches:
            if index in found:
                raise EvidenceError('ambiguous_required_member', 'Required source evidence member matched more than once.')
            identity(data, required)
            found.add(index)
        record = {'member': name, **values, 'retained': retained}
        if retained:
            try:
                text = data.decode('utf-8-sig')
                if any(ord(char) < 32 and char not in '\n\r\t\f' for char in text):
                    raise ValueError('Non-text control bytes.')
            except (UnicodeError, ValueError):
                record.update(retained=False, text_omission='not_utf8_plain_text')
                records.append(record)
                continue
            legal_bytes += len(data)
            if legal_bytes > MAX_NOTICES:
                raise EvidenceError('notice_total_byte_bound', 'Selected public legal source text exceeds total bound.')
            relative_path = relative(stripped)
            if str(relative_path).casefold() in retained_paths:
                raise EvidenceError('retained_path_collision', 'Retained evidence paths collide across archive roots or Windows case.')
            retained_paths.add(str(relative_path).casefold())
            destination = ordinary(output.joinpath(*relative_path.parts))
            if destination.exists():
                raise EvidenceError('existing_destination', 'Public evidence member collides with an existing output.')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            record['retained_path'] = (Path(item['component']) / Path(*relative_path.parts)).as_posix()
        records.append(record)
    if len(found) != len(wanted):
        missing = [required.get('path', required.get('path_suffix')) for index, required in enumerate(wanted) if index not in found]
        raise EvidenceError('required_member_missing', 'Required exact public source evidence missing: ' + ', '.join(missing))
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=ROOT / INVENTORY)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--metadata-only', action='store_true', help='Verify immutable individual sources only; explicitly incomplete archive review.')
    parser.add_argument('--require-clean-source', action='store_true', help='Require the exact clean committed verifier/inventory source before and after collection.')
    parser.add_argument('--download-limit-bytes', type=int, default=DEFAULT_LIMIT,
                        help='Explicit resource bound; a larger manifest does not silently enlarge the default 40 MiB.')
    args = parser.parse_args()
    manifest = read_json(args.manifest)
    if manifest.get('schema') != 1 or not 0 < args.download_limit_bytes <= 2 * 1024 * 1024 * 1024:
        parser.error('Use schema 1 and a positive bounded download allowance up to 2 GiB.')
    manifest_limit = manifest.get('total_download_limit', DEFAULT_LIMIT)
    if type(manifest_limit) is not int or manifest_limit <= 0:
        parser.error('Manifest must declare its positive total download bound.')
    for key, maximum in (('immutable_assets', 150), ('source_archives', 10)):
        if not isinstance(manifest.get(key), list) or len(manifest[key]) > maximum or any(not isinstance(row, dict) for row in manifest[key]):
            parser.error('The source inventory requires bounded descriptor lists.')
    budget = {'limit': min(manifest_limit, args.download_limit_bytes), 'used': 0}
    source = source_identity(ROOT, args.manifest)
    if args.require_clean_source and source['source_checkout_clean'] is not True:
        parser.error('The native evidence job requires a clean exact-source checkout.')
    ordinary(args.output)
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'schema': 1, 'kind': manifest.get('kind'), 'status': 'blocked',
              'observed_at': datetime.now(timezone.utc).isoformat(),
              'manifest_sha256': digest(args.manifest.read_bytes()),
              'download_limit_bytes': budget['limit'], 'downloads': [], 'errors': [],
              **source,
              'runner_platform': {'system': platform.system(), 'python': platform.python_version()},
              'native_execution_performed': False, 'native_binary_execution_performed': False,
              'package_installation_performed': False, 'model_inference_performed': False,
              'license_reviewed': False, 'distribution_approved': False,
              'physical_gpu_tested': False, 'archive_review_skipped': args.metadata_only}
    for asset in manifest.get('immutable_assets', []):
        name = str(relative(asset['save_as']))
        try:
            destination = args.output / 'public_source_texts' / 'immutable' / name
            result = download(asset, destination, budget, staging=args.output / '.transfer-staging')
            if result['retained']:
                identity(destination.read_bytes(), asset)
            report['downloads'].append({'kind': 'immutable_asset',
                                        **({'path': 'public_source_texts/immutable/' + name} if result['retained'] else {'requested_path': 'public_source_texts/immutable/' + name}),
                                        **result, **({'git_blob_sha1': asset['git_blob_sha1']} if 'git_blob_sha1' in asset else {})})
        except Exception as error:
            report['errors'].append({'asset': name, **error_record(error)})
    if not args.metadata_only:
        for archive in manifest.get('source_archives', []):
            name = str(relative(archive['component']))
            if '/' in name:
                raise EvidenceError('component_name_invalid', 'Use one component name for each bounded source archive.')
            try:
                destination = args.output / 'archives' / (name + '.verified-archive')
                result = download(archive, destination, budget, staging=args.output / '.transfer-staging')
                indexed = archive_index(destination, archive.get('format', 'tar'))
                documents = collect_archive(destination, archive, args.output / 'public_source_texts' / name)
                report['downloads'].append({'kind': 'verified_archive', 'component': name, **result,
                                            'archive_index': indexed, 'documents': documents})
            except Exception as error:
                report['errors'].append({'component': name, **error_record(error)})
    report['downloaded_bytes'] = budget['used']
    if not report['errors']:
        report['status'] = 'partial_individual_sources_only' if args.metadata_only else 'identities_verified_legal_review_pending'
    try:
        if source_identity(ROOT, args.manifest) != source:
            raise EvidenceError('source_changed', 'Verifier or source inventory changed during collection.')
    except Exception as error:
        report['status'] = 'blocked'
        report['errors'].append({'source_identity': 'changed_or_unavailable', **error_record(error)})
    (args.output / 'PUBLIC_LEGAL_SOURCE_REPORT.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n', 'utf-8')
    print(json.dumps({'status': report['status'], 'downloaded_bytes': report['downloaded_bytes'], 'errors': len(report['errors'])}))
    return 2 if report['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
