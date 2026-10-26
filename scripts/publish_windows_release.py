"""Append verified Windows assets to an existing GitHub release, never replace them.

The Linux and native Windows jobs create evidence before this helper receives a
write token. Existing asset bytes must match on a rerun; conflicts fail closed.
Only GitHub's authenticated API and GitHub-owned asset download hosts are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

TAG_PATTERN = r'v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)'
COMMIT_PATTERN = r'[a-f0-9]{40}'
WINDOWS_PATTERN = re.compile(r'(win(dows)?|x64|amd64)', re.I)
REQUIRED_CASES = {'install', 'cancelled', 'wrong-checksum', 'no-windows-asset', 'protected-payload', 'existing-junction'}
UPDATER_SHA256 = '9d499a9122f179e5ae2d1f19393be84fefeac2d013f5d70fcf7ed3b5b59895db'


class PublicationError(RuntimeError):
    pass


class GitHubError(PublicationError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def require(condition: bool, message: str):
    if not condition:
        raise PublicationError(message)


def release_names(tag: str) -> tuple[str, str, str]:
    require(bool(re.fullmatch(TAG_PATTERN, tag)), 'The release tag must be exactly vMAJOR.MINOR.PATCH.')
    archive = f'FORGE-{tag}-windows-x64.zip'
    return archive, archive + '.sha256', f'FORGE-{tag}-windows-x64.validation.json'


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def read_json(path: Path) -> dict:
    result = json.loads(path.read_text(encoding='utf-8-sig'))
    require(isinstance(result, dict), f'{path.name} must contain a JSON object.')
    return result


def verify_local_assets(directory: Path, tag: str) -> tuple[Path, Path, str]:
    archive_name, checksum_name, _ = release_names(tag)
    archive, checksum = directory / archive_name, directory / checksum_name
    require(archive.is_file() and not archive.is_symlink(), 'The exact Windows ZIP is missing or is a symbolic link.')
    require(checksum.is_file() and not checksum.is_symlink(), 'The exact companion checksum is missing or is a symbolic link.')
    checksum_text = checksum.read_text(encoding='ascii').strip()
    match = re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?' + re.escape(archive_name), checksum_text)
    require(match is not None, 'The checksum must name exactly this Windows ZIP.')
    actual = digest(archive)
    require(match.group(1).lower() == actual, 'The Windows ZIP does not match its companion checksum.')
    return archive, checksum, actual


def junit_counts(path: Path) -> dict:
    root = ET.parse(path).getroot()
    cases = list(root.iter('testcase'))
    counts = {'total': len(cases), 'failed': 0, 'errors': 0, 'skipped': 0}
    for case in cases:
        for field, element in [('failed', 'failure'), ('errors', 'error'), ('skipped', 'skipped')]:
            counts[field] += case.find(element) is not None
    counts['passed'] = counts['total'] - counts['failed'] - counts['errors'] - counts['skipped']
    require(counts['passed'] > 0 and counts['failed'] == counts['errors'] == 0,
            f'{path.name} must record a passing suite with at least one executed test.')
    return counts


def checkout_commit(path: Path) -> str:
    result = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True, timeout=30).strip()
    require(bool(re.fullmatch(COMMIT_PATTERN, result)), 'The checkout commit must be a full SHA-1 identity.')
    return result


def requires_download_acceptance(tag: str) -> bool:
    release_names(tag)
    return tuple(map(int, tag[1:].split('.'))) >= (0, 9, 0)


def download_acceptance_summary(value: dict, source_commit: str) -> dict:
    """Require actual tagged Windows download checks and retain stable evidence.

    Timestamps, machine paths, tool banners and workflow identifiers belong in
    the complete CI receipt. The release asset keeps only this fixed contract.
    """
    require(isinstance(value, dict), 'Required native Windows download acceptance is missing.')
    require(value.get('status') == 'passed', 'Native Windows download acceptance did not pass.')
    require(isinstance(source_commit, str) and bool(re.fullmatch(COMMIT_PATTERN, source_commit))
            and value.get('source_commit') == source_commit,
            'Native Windows download acceptance belongs to a different source commit.')
    require(value.get('working_tree_clean') is True, 'Native Windows download acceptance requires a clean checkout.')
    require(value.get('native_windows_execution') is True and value.get('platform') == 'win32',
            'Native Windows download acceptance must execute on Windows.')
    require(value.get('physical_gpu_tested') is False and value.get('live_youtube_tested') is False,
            'Owned-media download acceptance must not claim GPU or live YouTube verification.')
    tests, failed, skipped = (value.get(key) for key in ('tests', 'failed', 'skipped'))
    require(type(tests) is int and tests > 0 and type(failed) is int and failed == 0
            and type(skipped) is int and skipped == 0,
            'Native Windows download checks must execute and pass without failures or skips.')
    cases = value.get('cases')
    require(isinstance(cases, list) and len(cases) == tests
            and all(isinstance(case, dict) and isinstance(case.get('name'), str) and case['name'].strip()
                    and len(case['name']) <= 400 and case.get('status') == 'passed' for case in cases),
            'Native Windows download evidence has inconsistent or nonpassing test cases.')
    require(len({case['name'] for case in cases}) == tests,
            'Native Windows download evidence contains duplicate test cases.')
    scope = value.get('scope')
    require(isinstance(scope, str) and scope.strip() and len(scope) <= 2000,
            'Native Windows download acceptance must declare its tested scope.')
    return {'status': 'passed', 'source_commit': source_commit, 'working_tree_clean': True,
            'native_windows_execution': True, 'platform': 'win32', 'tests': tests, 'failed': 0, 'skipped': 0,
            'cases': [{'name': case['name'], 'status': 'passed'} for case in sorted(cases, key=lambda case: case['name'])],
            'scope': scope, 'physical_gpu_tested': False, 'live_youtube_tested': False}


def record_linux(args):
    archive, _, sha256 = verify_local_assets(args.assets, args.tag)
    require((args.source / 'VERSION.txt').read_text(encoding='utf-8').strip() == args.tag[1:],
            'The exact tagged source version does not match the requested release.')
    metadata = {'schema_version': 1, 'status': 'passed', 'tag': args.tag,
                'source_commit': checkout_commit(args.source), 'tooling_commit': checkout_commit(args.tooling),
                'archive_name': archive.name, 'archive_sha256': sha256,
                'tagged_python_suite': junit_counts(args.source_junit),
                'windows_packager_suite': junit_counts(args.packager_junit),
                'package_validation': read_json(args.package_validation),
                'manifest_validation': 'passed', 'tagged_source_comparison': 'passed',
                'protected_paths_excluded': True}
    args.output.write_text(json.dumps(metadata, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    print('Recorded passing Linux suites and exact-source package validation.')


def build_validation(tag: str, directory: Path, linux: dict, windows: dict) -> Path:
    archive, _, sha256 = verify_local_assets(directory, tag)
    require(linux.get('status') == windows.get('status') == 'passed', 'Both Linux and native Windows validation must pass.')
    require(linux.get('tag') == tag, 'Linux validation belongs to a different release tag.')
    for key in ['source_commit', 'tooling_commit']:
        require(bool(re.fullmatch(COMMIT_PATTERN, linux.get(key, ''))), f'Missing verified {key}.')
    for record, name in [(linux, 'Linux'), (windows, 'Windows')]:
        require(record.get('archive_name') == archive.name and record.get('archive_sha256') == sha256,
                f'{name} validation does not describe the exact ZIP bytes.')
    require(windows.get('native_windows_execution') is True, 'A native Windows run is required before publication.')
    require(windows.get('updater_sha256') == UPDATER_SHA256, 'Windows validation used a different updater fixture.')
    require(isinstance(windows.get('checks'), list) and windows['checks']
            and windows.get('passed') == len(windows['checks']), 'Windows evidence has inconsistent check counts.')
    require(isinstance(windows.get('cases'), list) and windows['cases']
            and all(case.get('status') == 'passed' for case in windows['cases'])
            and REQUIRED_CASES <= {case.get('name') for case in windows['cases']}, 'Every native updater acceptance case must pass.')
    smoke = windows.get('installed_app_smoke', {})
    require(isinstance(smoke, dict) and smoke.get('status') == 'passed'
            and smoke.get('native_windows_execution') is True and smoke.get('installed_version') == tag[1:]
            and smoke.get('expected_version') == tag[1:] and isinstance(smoke.get('checks'), list)
            and smoke['checks'] and smoke.get('passed') == len(smoke['checks']),
            'A real installed-application smoke test must pass.')
    require(linux.get('manifest_validation') == linux.get('tagged_source_comparison') == 'passed'
            and linux.get('protected_paths_excluded') is True, 'Exact-source package validation must pass.')
    for name in ['tagged_python_suite', 'windows_packager_suite']:
        counts = linux.get(name, {})
        require(counts.get('passed', 0) > 0 and counts.get('failed') == counts.get('errors') == 0,
                f'The {name} did not pass.')
    downloads = download_acceptance_summary(windows.get('download_acceptance'), linux['source_commit']) if requires_download_acceptance(tag) else None
    # Stable evidence allows byte-identical reruns. Volatile machine paths,
    # durations, timestamps and Actions run IDs stay in the CI artifacts.
    validation = {'schema_version': 1, 'tag': tag, 'source_commit': linux['source_commit'],
                  'tooling_commit': linux['tooling_commit'], 'archive_name': archive.name, 'archive_sha256': sha256,
                  'tagged_python_suite': linux['tagged_python_suite'],
                  'windows_packager_suite': linux['windows_packager_suite'],
                  'manifest_validation': 'passed', 'tagged_source_comparison': 'passed',
                  'protected_paths_excluded': True, 'native_windows_execution': True,
                  'native_windows_updater': 'passed', 'native_windows_checks': windows['passed'],
                  'native_windows_cases': [{'name': case['name'], 'status': case['status']}
                                          for case in windows['cases']],
                  'installed_application_smoke': 'passed', 'updater_sha256': UPDATER_SHA256,
                  'physical_gpu_tested': False,
                  'temporary_directory_policy': 'A separate canonical temporary directory per native test scenario.',
                  'updater_limitations': ['The supplied updater can miscalculate destinations when TEMP uses an 8.3 alias. Use a canonical temporary path.'],
                  'scope': 'Updater install, preservation, rejection cases and real installed-app smoke on Windows x64 with canonical temporary paths. Not a physical GPU or all-codec acceptance claim.'}
    if downloads is not None:
        validation['download_acceptance'] = downloads
    path = directory / release_names(tag)[2]
    path.write_text(json.dumps(validation, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return path


def allowed_download_host(host: str) -> bool:
    return host in {'api.github.com', 'github.com', 'uploads.github.com'} or host.endswith('.githubusercontent.com')


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        target = urlparse(newurl)
        require(target.scheme == 'https' and allowed_download_host(target.hostname or ''),
                'GitHub redirected an asset to an unexpected host or protocol.')
        redirected = super().redirect_request(request, fp, code, message, headers, newurl)
        if redirected and target.hostname != urlparse(request.full_url).hostname:
            redirected.remove_header('Authorization')
        return redirected


class GitHub:
    def __init__(self, repository: str, token: str):
        require(bool(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)), 'Invalid GitHub repository identity.')
        require(bool(token), 'GITHUB_TOKEN or GH_TOKEN is required.')
        self.repository, self.token = repository, token
        self.base = f'https://api.github.com/repos/{repository}'
        self.opener = build_opener(SafeRedirect())

    def request(self, url: str, *, method='GET', data=None, accept='application/vnd.github+json', content_type='application/json'):
        parsed = urlparse(url)
        require(parsed.scheme == 'https' and parsed.hostname in {'api.github.com', 'uploads.github.com'},
                'Authenticated requests are restricted to GitHub API hosts.')
        headers = {'Accept': accept, 'Authorization': 'Bearer ' + self.token,
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'FORGE-tested-windows-release',
                   'Content-Type': content_type}
        try:
            return self.opener.open(Request(url, data=data, headers=headers, method=method), timeout=90)
        except HTTPError as error:
            # Never print response bodies, request headers or signed redirects.
            raise GitHubError(error.code, f'GitHub {method} request failed with HTTP {error.code}.') from None
        except (OSError, URLError):
            raise PublicationError(f'GitHub {method} request failed. No existing asset was replaced; rerun to compare and resume.') from None

    def json(self, path: str, *, method='GET', value=None):
        data = json.dumps(value).encode() if value is not None else None
        with self.request(self.base + path, method=method, data=data) as response:
            return json.load(response)

    def release(self, tag: str):
        return self.json('/releases/tags/' + quote(tag, safe=''))

    def assets(self, release_id: int):
        assets = []
        for page in range(1, 101):
            values = self.json(f'/releases/{release_id}/assets?per_page=100&page={page}')
            require(isinstance(values, list), 'GitHub returned an invalid asset list.')
            assets.extend(values)
            if len(values) < 100:
                return assets
        raise PublicationError('The release has too many assets to verify safely.')

    def asset_digest(self, asset: dict):
        result, size = hashlib.sha256(), 0
        with self.request(self.base + f'/releases/assets/{int(asset["id"])}', accept='application/octet-stream') as response:
            for block in iter(lambda: response.read(1024 * 1024), b''):
                size += len(block)
                require(size <= int(asset['size']), 'The downloaded asset exceeds its declared size.')
                result.update(block)
        require(size == int(asset['size']), 'The downloaded asset size differs from GitHub metadata.')
        return result.hexdigest()

    def asset_bytes(self, asset: dict, max_bytes: int = 256 * 1024):
        require(0 < int(asset['size']) <= max_bytes, 'The existing validation record exceeds the allowed size.')
        with self.request(self.base + f'/releases/assets/{int(asset["id"])}', accept='application/octet-stream') as response:
            result = response.read(max_bytes + 1)
        require(len(result) == int(asset['size']) and len(result) <= max_bytes,
                'The existing validation download has an unexpected size.')
        return result

    def upload(self, release_id: int, path: Path):
        url = f'https://uploads.github.com/repos/{self.repository}/releases/{release_id}/assets?' + urlencode({'name': path.name})
        with self.request(url, method='POST', data=path.read_bytes(), content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream') as response:
            asset = json.load(response)
        require(asset.get('name') == path.name and asset.get('size') == path.stat().st_size,
                'GitHub did not confirm the uploaded asset name and size.')
        return asset

    def tag_commit(self, tag: str):
        value = self.json('/git/ref/tags/' + quote(tag, safe=''))['object']
        for _ in range(8):
            if value.get('type') == 'commit':
                return value.get('sha')
            require(value.get('type') == 'tag' and re.fullmatch(COMMIT_PATTERN, value.get('sha', '')),
                    'The release tag does not resolve to a known Git commit.')
            value = self.json('/git/tags/' + value['sha'])['object']
        raise PublicationError('The release tag contains too many annotated-tag indirections.')


def windows_assets(assets: list[dict]) -> list[dict]:
    # Match the user's unchanged Update-Application.ps1 selector exactly.
    return [asset for asset in assets if WINDOWS_PATTERN.search(asset['name']) and asset['name'].lower().endswith('.zip')]


def asset_identity(asset: dict) -> tuple:
    return tuple(asset.get(key) for key in ['id', 'name', 'size', 'digest', 'created_at', 'updated_at'])


def wait_release(client, tag: str, wait_seconds: int, poll_seconds: float = 15):
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            release = client.release(tag)
            require(release.get('tag_name') == tag, 'GitHub returned a release for another tag.')
            if not release.get('draft') and release.get('published_at'):
                return release
        except GitHubError as error:
            if error.status != 404:
                raise
        if time.monotonic() >= deadline:
            raise PublicationError('The existing release was not published before the wait expired. No release or tag was created or changed.')
        print('Waiting for the independently tested release to be published...', flush=True)
        time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))


def append_installation_note(client, release: dict, tag: str):
    marker = f'<!-- FORGE-WINDOWS-ASSET:{tag} -->'
    names = release_names(tag)
    base = f'https://github.com/{client.repository}/releases/download/{tag}/'
    note = (marker + '\n\n'
            '**Windows updater package:** Download [' + names[0] + '](' + base + names[0] + ') '
            'and its [' + names[1] + '](' + base + names[1] + ') companion checksum. '
            'Use your existing `Update-Application.ps1` and its wrapper; they remain unchanged. '
            'This package passed the tagged Python suite, exact-source package validation and native Windows updater/installed-app checks with canonical temporary paths. '
            'Earlier assets remain available for the legacy offline updater. GitHub Source code archives are not updater packages.\n\n'
            'The supplied updater miscalculates destinations if TEMP expands from an 8.3 alias. '
            'After closing FORGE, run the existing wrapper from PowerShell with a normal staging path:\n\n'
            "```powershell\n$env:TEMP = 'B:\\Forge\\Build'\n& 'B:\\Forge\\Update FORGE.bat'\n```\n"
            'Type `UPDATE` at its confirmation. This changes only the current process environment; '
            'the updater creates and removes its own temporary staging folder inside Build.\n')
    # Fetch again so independently added release notes are preserved.
    current = client.release(tag)
    require(current['id'] == release['id'] and not current.get('draft'), 'The release changed during asset publication.')
    body = current.get('body') or ''
    if marker in body:
        require(note.strip() in body, 'The existing Windows installation note differs; it was not overwritten.')
        return
    updated = client.json(f'/releases/{release["id"]}', method='PATCH', value={'body': body + ('\n' if body.endswith('\n') else '\n\n') + note})
    require(updated.get('body') == body + ('\n' if body.endswith('\n') else '\n\n') + note,
            'GitHub did not confirm the additive installation note.')


def verify_existing_validation(value: dict, expected: dict):
    # Keep the first passing receipt when later workflow/tooling commits rebuild
    # the same tagged application bytes. Its original tooling identity is kept.
    require(isinstance(value, dict) and value.get('schema_version') == 1,
            'The existing Windows validation record has an unsupported schema.')
    for key in ['tag', 'source_commit', 'archive_name', 'archive_sha256', 'updater_sha256']:
        require(value.get(key) == expected.get(key), f'The existing validation record differs in {key}.')
    require(bool(re.fullmatch(COMMIT_PATTERN, value.get('tooling_commit', ''))),
            'The existing validation record lacks its original tooling commit.')
    require(value.get('native_windows_execution') is True and value.get('protected_paths_excluded') is True,
            'The existing validation record does not certify native execution and protected-path filtering.')
    for key in ['manifest_validation', 'tagged_source_comparison', 'native_windows_updater', 'installed_application_smoke']:
        require(value.get(key) == 'passed', f'The existing validation record lacks a passing {key}.')
    for key in ['tagged_python_suite', 'windows_packager_suite']:
        counts = value.get(key, {})
        require(counts.get('passed', 0) > 0 and counts.get('failed') == counts.get('errors') == 0,
                f'The existing validation record lacks a passing {key}.')
    cases = value.get('native_windows_cases', [])
    require(isinstance(value.get('native_windows_checks'), int) and value['native_windows_checks'] > 0
            and isinstance(cases, list) and REQUIRED_CASES <= {case.get('name') for case in cases}
            and all(case.get('status') == 'passed' for case in cases),
            'The existing validation record lacks the required native Windows acceptance cases.')
    if requires_download_acceptance(expected.get('tag', '')):
        expected_downloads = download_acceptance_summary(expected.get('download_acceptance'), expected['source_commit'])
        existing_downloads = download_acceptance_summary(value.get('download_acceptance'), expected['source_commit'])
        require(existing_downloads == expected_downloads,
                'The existing Windows validation record has different native download evidence; it was not overwritten.')


def publish(client, tag: str, directory: Path, validation: Path, *, wait_seconds: int = 600, add_note: bool = True):
    archive, checksum, _ = verify_local_assets(directory, tag)
    proof = read_json(validation)
    if requires_download_acceptance(tag):
        download_acceptance_summary(proof.get('download_acceptance'), proof.get('source_commit', ''))
    files = [archive, checksum, validation]
    require(validation.name == release_names(tag)[2], 'Unexpected Windows validation asset name.')
    release = wait_release(client, tag, wait_seconds)
    require(client.tag_commit(tag) == proof['source_commit'], 'The current release tag differs from the tested source commit.')
    existing = client.assets(release['id'])
    matches = windows_assets(existing)
    require(len(matches) <= 1 and all(asset['name'] == archive.name for asset in matches),
            'Another Windows-matching ZIP exists. No assets were uploaded, replaced or deleted.')
    names = [asset['name'] for asset in existing]
    require(len(names) == len(set(names)), 'GitHub returned duplicate asset names; publication stopped.')
    by_name = {asset['name']: asset for asset in existing}
    expected_bytes = {path.name: (path.stat().st_size, digest(path)) for path in files}
    # Compare every pre-existing target before the first upload. This avoids a
    # partial append when an existing checksum or validation record conflicts.
    for path in files:
        if path.name in by_name:
            asset = by_name[path.name]
            if path == validation:
                original = client.asset_bytes(asset)
                original_digest = hashlib.sha256(original).hexdigest()
                if original_digest != digest(path):
                    verify_existing_validation(json.loads(original), proof)
                    expected_bytes[path.name] = (len(original), original_digest)
                    print('Retaining the original passing validation receipt and its tooling provenance.', flush=True)
            else:
                require(asset['size'] == path.stat().st_size and client.asset_digest(asset) == digest(path),
                        f'Existing asset {path.name} differs. It was not overwritten or deleted.')
    for path in files:
        if path.name in by_name:
            print('Verified and preserved existing asset:', path.name, flush=True)
        else:
            client.upload(release['id'], path)
            print('Uploaded new tested asset:', path.name, flush=True)
    final = client.assets(release['id'])
    final_by_name = {asset['name']: asset for asset in final}
    require(len(windows_assets(final)) == 1 and windows_assets(final)[0]['name'] == archive.name,
            'Post-upload validation did not find exactly one expected Windows ZIP.')
    require(all(asset['name'] in final_by_name and asset_identity(final_by_name[asset['name']]) == asset_identity(asset)
                for asset in existing), 'An existing asset changed during publication; investigate the concurrent change.')
    for path in files:
        asset = final_by_name.get(path.name)
        require(asset is not None and (asset['size'], client.asset_digest(asset)) == expected_bytes[path.name],
                f'Post-upload download verification failed for {path.name}.')
    require(client.tag_commit(tag) == proof['source_commit'], 'The release tag moved during publication.')
    if add_note:
        append_installation_note(client, release, tag)
    print('Verified the Windows ZIP, downloaded checksum and validation record. All previous assets and the tag were preserved.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    linux = commands.add_parser('record-linux')
    for name in ['source', 'tooling', 'assets', 'source-junit', 'packager-junit', 'package-validation', 'output']:
        linux.add_argument('--' + name, type=Path, required=True)
    linux.add_argument('--tag', required=True)
    prepare = commands.add_parser('prepare-validation')
    prepare.add_argument('--tag', required=True)
    prepare.add_argument('--assets', type=Path, required=True)
    prepare.add_argument('--linux-results', type=Path, required=True)
    prepare.add_argument('--windows-results', type=Path, required=True)
    release = commands.add_parser('publish')
    release.add_argument('--tag', required=True)
    release.add_argument('--assets', type=Path, required=True)
    release.add_argument('--linux-results', type=Path, required=True)
    release.add_argument('--windows-results', type=Path, required=True)
    release.add_argument('--repository', default=os.getenv('GITHUB_REPOSITORY', ''))
    release.add_argument('--wait-seconds', type=int, default=600, choices=range(0, 601), metavar='0..600')
    release.add_argument('--no-installation-note', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'record-linux':
            record_linux(args)
        elif args.command == 'prepare-validation':
            proof = build_validation(args.tag, args.assets, read_json(args.linux_results), read_json(args.windows_results))
            print('Prepared tested Windows validation asset:', proof.name)
        else:
            proof = build_validation(args.tag, args.assets, read_json(args.linux_results), read_json(args.windows_results))
            client = GitHub(args.repository, os.getenv('GH_TOKEN') or os.getenv('GITHUB_TOKEN', ''))
            publish(client, args.tag, args.assets, proof, wait_seconds=args.wait_seconds, add_note=not args.no_installation_note)
        return 0
    except (PublicationError, OSError, ValueError, ET.ParseError, subprocess.SubprocessError) as error:
        message = str(error)
        for key in ['GH_TOKEN', 'GITHUB_TOKEN']:
            value = os.getenv(key)
            if value:
                message = message.replace(value, '[redacted]')
        print('Windows release publication stopped: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
