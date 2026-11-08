"""Fail closed before publishing V0.10+; never install or execute a model.

The reviewed source must contain forge/dubbing_release_inventory.json (schema
1), listing the two recommended components, their pinned parts/artifact hashes,
license approval, and native Windows wheel inventory. Its file identity is bound
to DUBBING_ACCEPTANCE.json from a successful exact-source Actions run. Acceptance
reports are external evidence, never fabricated by this release checker.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile


REPOSITORY = 'BirthrightPublication/forge'
TAG = re.compile(r'^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')
SHA = re.compile(r'^[a-f0-9]{64}$')
COMMIT = re.compile(r'^[a-f0-9]{40}$')
INVENTORY = 'forge/dubbing_release_inventory.json'
RECOMMENDED = {'opus-en-ru': 'Helsinki-NLP/opus-mt-en-ru',
               'qwen3-tts-0.6b-base': 'Qwen/Qwen3-TTS-12Hz-0.6B-Base'}
CHECKS = ('gui_recommended_install', 'restart_model_persistence', 'timed_english_asr',
          'russian_translation', 'reference_russian_tts', 'playable_outputs',
          'windows_v09_upgrade', 'protected_userdata_preservation', 'external_windows_updater',
          'rollback_recovery', 'running_backend_version')
MAX_BYTES = 128 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
MAX_FILES = 1000
PROOF_FILES = ('DUBBING_ACCEPTANCE.json', 'DUBBING_RELEASE_GATE.json', 'DUBBING_EVIDENCE.zip')


class GateError(ValueError):
    pass


def require(value, message):
    if not value:
        raise GateError('Dubbing stable-publication gate: ' + message)


def required(tag):
    require(isinstance(tag, str) and TAG.fullmatch(tag), 'use an exact vMAJOR.MINOR.PATCH tag.')
    return tuple(map(int, tag[1:].split('.'))) >= (0, 10, 0)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def relative(value):
    require(isinstance(value, str) and value and not re.search(r'[\\:\x00-\x1f]', value), 'unsafe evidence/inventory path.')
    path = PurePosixPath(value)
    require(not path.is_absolute() and str(path) == value and all(part not in {'.', '..'} and not part.endswith((' ', '.')) for part in path.parts), 'unsafe evidence/inventory path.')
    return path


def safe_file(root, name):
    path = Path(root)
    for parent in [path, *path.parents]:
        require(not parent.is_symlink(), 'linked evidence/source roots are not allowed.')
        if parent.exists():
            require(not getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400, 'Windows reparse points are not allowed.')
    for part in relative(name).parts:
        path = path / part
        require(not path.is_symlink(), 'linked evidence/source files are not allowed.')
        if path.exists():
            info = path.lstat()
            require(not getattr(info, 'st_file_attributes', 0) & 0x400, 'Windows reparse points are not allowed.')
    require(path.is_file() and path.stat().st_size <= MAX_FILE, 'missing or oversized required file: ' + name)
    return path


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate evidence JSON key.')
            result[key] = value
        return result
    try:
        require(Path(path).is_file() and Path(path).stat().st_size <= MAX_FILE, 'missing or oversized JSON evidence.')
        result = json.loads(Path(path).read_text('utf-8'), object_pairs_hook=unique,
                            parse_constant=lambda value: (_ for _ in ()).throw(GateError('Invalid JSON number.')))
        require(isinstance(result, dict), 'evidence must be a JSON object.')
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise GateError('Dubbing stable-publication gate: unreadable JSON evidence.') from error


def static_inventory(source, tag, source_commit):
    if not required(tag):
        return {'required': False, 'status': 'not_required', 'tag': tag}
    require(isinstance(source_commit, str) and COMMIT.fullmatch(source_commit), 'an exact tagged source commit is required.')
    source = Path(source)
    manifest_path = safe_file(source, 'release-manifest.json')
    manifest = read_json(manifest_path)
    require(manifest.get('app') == 'FORGE' and manifest.get('version') == tag[1:], 'source release manifest must match the exact tag.')
    files = manifest.get('files', {})

    def tracked(name):
        path = safe_file(source, name)
        require(isinstance(files, dict) and files.get(name) == digest(path), 'source manifest does not verify ' + name + '.')
        return path

    declarations = {}
    for node in ast.parse(tracked('forge/dubbing_models.py').read_text('utf-8')).body:
        target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None
        if isinstance(target, ast.Name) and target.id == 'MODEL_PINS':
            try:
                declarations = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                raise GateError('Dubbing stable-publication gate: MODEL_PINS must have a reviewed static inventory.') from None
    require(isinstance(declarations, dict), 'MODEL_PINS must be a reviewed mapping.')
    for component, repository in RECOMMENDED.items():
        require(isinstance(declarations.get(repository), str) and COMMIT.fullmatch(declarations[repository]),
                'missing reviewed immutable recommended model pin: ' + component + '. No V0.10 release may be published.')
    inventory_path = tracked(INVENTORY)
    inventory = read_json(inventory_path)
    require(inventory.get('schema') == 1 and inventory.get('license_reviewed') is True, 'the model/license inventory has not been reviewed.')
    components = inventory.get('components', {})
    identities = {}
    wheel_hashes = {}
    for component, repository in RECOMMENDED.items():
        item = components.get(component, {})
        parts = item.get('parts', [])
        require(isinstance(parts, list) and 1 <= len(parts) <= 10, 'missing complete model artifact inventory: ' + component + '.')
        require(any(part.get('repository') == repository for part in parts if isinstance(part, dict)), 'recommended repository is absent: ' + component + '.')
        identities[component] = {}
        all_names = []
        for part in parts:
            require(isinstance(part, dict), 'invalid model part descriptor.')
            repo, revision = part.get('repository'), part.get('revision')
            require(isinstance(repo, str) and COMMIT.fullmatch(str(revision)) and declarations.get(repo) == revision,
                    'model inventory differs from the source MODEL_PINS: ' + component + '.')
            license = part.get('license', {})
            require(isinstance(license, dict) and license.get('reviewed') is True and license.get('commercial_use') is True and license.get('id'),
                    'model checkpoint commercial-use/license review is missing: ' + component + '.')
            artifacts = part.get('artifacts', [])
            require(isinstance(artifacts, list) and 1 <= len(artifacts) <= MAX_FILES, 'model artifact hashes/sizes are missing: ' + component + '.')
            names = set()
            for artifact in artifacts:
                require(isinstance(artifact, dict), 'invalid model artifact.')
                name = str(relative(artifact.get('path')))
                require(name not in names and SHA.fullmatch(str(artifact.get('sha256', ''))) and type(artifact.get('size')) is int and artifact['size'] > 0,
                        'model artifact integrity inventory is incomplete: ' + component + '.')
                names.add(name)
            require(any(name.lower().startswith(('license', 'notice')) for name in names), 'the exact checkpoint license artifact is missing: ' + component + '.')
            all_names.extend(names)
            identities[component][repo] = revision
        require('config.json' in all_names and any(name.endswith(('.safetensors', '.bin')) for name in all_names), 'model config/weights are absent: ' + component + '.')
        if component == 'opus-en-ru':
            require({'source.spm', 'target.spm', 'vocab.json'} <= set(all_names), 'OPUS tokenizer artifacts are incomplete.')
        else:
            require({'generation_config.json', 'tokenizer_config.json', 'speech_tokenizer/config.json'} <= set(all_names)
                    and any(name.startswith('speech_tokenizer/') and name.endswith('.safetensors') for name in all_names),
                    'Qwen embedded speech-tokenizer artifacts are incomplete.')
        runtime = item.get('runtime', {})
        require(runtime.get('requirements') == 'scripts/requirements-dubbing-core.txt', 'recommended components require the reviewed core runtime.')
        requirements_path = tracked(runtime['requirements'])
        require(runtime.get('requirements_sha256') == digest(requirements_path), 'requirements do not match the reviewed runtime inventory.')
        lock_path = tracked(runtime.get('wheel_inventory', ''))
        lock_sha = digest(lock_path)
        require(runtime.get('wheel_inventory_sha256') == lock_sha, 'native wheel inventory hash differs from its review.')
        lock = read_json(lock_path)
        require(lock.get('schema') == 1 and lock.get('system') == 'windows' and lock.get('architecture') == 'x64'
                and lock.get('python_version') in {'3.11', '3.12'} and lock.get('native_windows_execution') is True
                and lock.get('cross_resolved') is False and lock.get('reviewed') is True
                and lock.get('requirements_sha256') == digest(requirements_path), 'a reviewed native Windows wheel inventory is required.')
        require(lock.get('device') in {'cpu', 'cuda'} and lock.get('device') == runtime.get('device'), 'native wheel device profile differs from its review.')
        require(runtime['wheel_inventory'] == 'forge/dubbing_wheel_locks/requirements-dubbing-core-windows-x64-py'
                + lock['python_version'].replace('.', '') + '-' + lock['device'] + '.json'
                and lock.get('profile') == 'requirements-dubbing-core' and lock.get('dependency_closure_complete') is True,
                'the reviewed native wheel inventory must be the complete registered installer profile.')
        wheels = lock.get('wheels', [])
        require(isinstance(wheels, list) and 1 <= len(wheels) <= 250, 'the native wheel dependency closure is missing.')
        for wheel in wheels:
            require(isinstance(wheel, dict) and SHA.fullmatch(str(wheel.get('sha256', ''))) and type(wheel.get('size')) is int
                    and wheel['size'] > 0 and wheel.get('name', '').endswith('.whl') and wheel.get('package') and wheel.get('version')
                    and wheel.get('license') and not re.search(r'unknown|unreviewed|see publisher', str(wheel['license']), re.I),
                    'a binary wheel lacks reviewed version/hash/size/license evidence.')
        require(sum(wheel['size'] for wheel in wheels) == lock.get('download_bytes'), 'the wheel byte inventory is inconsistent.')
        wheel_hashes[component] = lock_sha
    return {'required': True, 'status': 'inventory_verified', 'tag': tag, 'source_commit': source_commit,
            'source_manifest_sha256': digest(manifest_path), 'model_inventory_sha256': digest(inventory_path),
            'model_revisions': identities, 'wheel_inventory_sha256': wheel_hashes}


def verify_evidence(source, tag, source_commit, evidence, *, run_id):
    expected = static_inventory(source, tag, source_commit)
    if not expected['required']:
        return expected
    require(evidence is not None, 'missing DUBBING_ACCEPTANCE.json from a separate real native acceptance run.')
    evidence = Path(evidence)
    value = read_json(safe_file(evidence.parent, evidence.name))
    require(value.get('schema') == 1 and value.get('app') == 'FORGE' and value.get('status') == 'passed', 'Dubbing acceptance did not pass.')
    for key in ('tag', 'source_commit', 'source_manifest_sha256', 'model_inventory_sha256', 'model_revisions', 'wheel_inventory_sha256'):
        require(value.get(key) == expected[key], 'foreign or stale acceptance proof: ' + key + '.')
    require(str(run_id).isdigit() and int(run_id) > 0 and value.get('actions_run_id') == str(run_id)
            and value.get('repository') == REPOSITORY, 'acceptance proof does not identify its exact same-repository Actions run.')
    require(value.get('native_windows_execution') is True and value.get('inference_substituted') is False
            and value.get('synthetic_only') is False, 'real native Windows model inference is required; synthetic-only/state/browser proof is insufficient.')
    require(value.get('reference_consent') is True and value.get('private_inputs_not_embedded') is True, 'owned/permitted reference consent and private-input exclusion must be explicit.')
    artifacts = value.get('artifacts', [])
    require(isinstance(artifacts, list) and 1 <= len(artifacts) <= MAX_FILES, 'acceptance must include bounded hashed evidence reports.')
    verified = set()
    for item in artifacts:
        require(isinstance(item, dict), 'invalid acceptance report descriptor.')
        name = str(relative(item.get('path')))
        require(name not in verified and name not in PROOF_FILES and Path(name).suffix.lower() in {'.json', '.xml', '.txt', '.log', '.png'}, 'evidence reports must be unique bounded reports/screenshots, not private media or archives.')
        path = safe_file(evidence.parent, name)
        require(type(item.get('size')) is int and path.stat().st_size == item['size'] and item.get('sha256') == digest(path), 'acceptance report is missing or differs from its digest: ' + name + '.')
        verified.add(name)

    def passed(check, label):
        require(isinstance(check, dict) and check.get('status') == 'passed' and isinstance(check.get('evidence'), list)
                and check['evidence'] and all(name in verified for name in check['evidence']), 'missing/failed real acceptance check: ' + label + '.')

    checks = value.get('checks', {})
    for label in CHECKS:
        passed(checks.get(label), label)
    for label in ('timed_english_asr', 'russian_translation', 'reference_russian_tts'):
        require(checks[label].get('inference_tested') is True, 'actual model inference evidence is missing: ' + label + '.')
    require(checks['timed_english_asr'].get('timings_verified') is True
            and type(checks['timed_english_asr'].get('segment_count')) is int and checks['timed_english_asr']['segment_count'] > 0,
            'actual ASR must produce verified nonempty timed segments.')
    require(checks['russian_translation'].get('target_language') == 'ru'
            and checks['reference_russian_tts'].get('language') == 'ru'
            and checks['reference_russian_tts'].get('reference_consent') is True
            and SHA.fullmatch(str(checks['reference_russian_tts'].get('reference_sha256', ''))),
            'Russian translation/speech and the exact permitted reference identity must be verified.')
    require(checks['gui_recommended_install'].get('through_forge_gui') is True
            and set(RECOMMENDED) <= set(checks['gui_recommended_install'].get('installed_components', []))
            and checks['restart_model_persistence'].get('verified_after_restart') is True
            and checks['playable_outputs'].get('decoded_and_probed') is True
            and checks['running_backend_version'].get('version') == tag[1:],
            'GUI installation, restart persistence, playable output decoding and the running tagged backend must be verified.')
    gpu = value.get('physical_gpu', {})
    passed(gpu, 'physical_gpu; no V0.8 GPU exception applies to V0.10')
    require(gpu.get('physical_gpu_tested') is True and gpu.get('inference_tested') is True and gpu.get('device_name')
            and type(gpu.get('peak_vram_bytes')) is int and gpu['peak_vram_bytes'] > 0,
            'physical GPU inference and measured VRAM evidence are required for stable V0.10.')
    return {**expected, 'status': 'passed', 'repository': REPOSITORY, 'acceptance_run_id': str(run_id),
            'acceptance_sha256': digest(evidence), 'physical_gpu_tested': True,
            'verified_evidence': sorted(verified)}


def safe_extract(archive, destination):
    destination = Path(destination)
    require(not destination.exists(), 'use a new owned directory for downloaded acceptance evidence.')
    for parent in destination.parents:
        require(not parent.is_symlink() and (not parent.exists() or not getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400), 'acceptance extraction cannot follow linked/reparse-point parents.')
    require(Path(archive).stat().st_size <= MAX_BYTES, 'acceptance ZIP exceeds its compressed bound.')
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        require(1 <= len(entries) <= MAX_FILES and sum(item.file_size for item in entries) <= MAX_BYTES, 'acceptance ZIP exceeds extraction bounds.')
        names, files = set(), set()
        for item in entries:
            path = relative(item.filename[:-1] if item.is_dir() else item.filename)
            key = str(path).casefold()
            mode = item.external_attr >> 16
            require(not item.flag_bits & 1 and stat.S_IFMT(mode) in {0, stat.S_IFREG, stat.S_IFDIR}
                    and item.file_size <= MAX_FILE and (not item.file_size or item.file_size <= max(1, item.compress_size) * 1000), 'encrypted/linked/special or oversized acceptance ZIP entry.')
            require(key not in names and not any(str(parent).casefold() in files for parent in path.parents if str(parent) != '.'), 'duplicate/conflicting acceptance ZIP path.')
            require(item.is_dir() or not any(name.startswith(key + '/') for name in names), 'acceptance ZIP file/directory collision.')
            names.add(key)
            if not item.is_dir():
                files.add(key)
        destination.mkdir(parents=True)
        try:
            for item in entries:
                target = destination.joinpath(*relative(item.filename[:-1] if item.is_dir() else item.filename).parts)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(item) as source, target.open('xb') as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                    require(target.stat().st_size == item.file_size, 'acceptance ZIP entry has an unexpected size.')
        except BaseException:
            shutil.rmtree(destination)
            raise
    return destination


def validate_run(value, source_commit):
    require(value.get('status') == 'completed' and value.get('conclusion') == 'success', 'the separate acceptance Actions run must have completed successfully.')
    require(value.get('head_sha') == source_commit and isinstance(value.get('repository'), dict) and value['repository'].get('full_name') == REPOSITORY
            and isinstance(value.get('head_repository'), dict) and value['head_repository'].get('full_name') == REPOSITORY, 'the acceptance Actions run must belong to this repository and exact tag commit.')


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _api(path, token):
    request = urllib.request.Request('https://api.github.com/repos/' + REPOSITORY + '/' + path,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'User-Agent': 'FORGE-dubbing-release-gate'})
    with urllib.request.build_opener(NoRedirects()).open(request, timeout=30) as response:
        raw = response.read(MAX_FILE + 1)
    require(len(raw) <= MAX_FILE, 'GitHub metadata exceeds its safety bound.')
    return json.loads(raw)


def download_evidence(source, tag, source_commit, *, run_id, destination, token):
    inventory = static_inventory(source, tag, source_commit)  # Refuse before any network I/O.
    if not inventory['required']:
        return inventory
    require(str(run_id).isdigit() and int(run_id) > 0, 'missing external acceptance run ID; set dubbing_acceptance_run_id or FORGE_DUBBING_ACCEPTANCE_RUN_ID.')
    require(bool(token), 'a read-only Actions token is required to read acceptance proof.')
    run = _api('actions/runs/' + str(run_id), token)
    validate_run(run, source_commit)
    collection = _api('actions/runs/' + str(run_id) + '/artifacts?per_page=100', token)
    require(type(collection.get('total_count')) is int and collection['total_count'] <= 100, 'acceptance artifact inventory is too large or incomplete.')
    matches = [item for item in collection.get('artifacts', []) if item.get('name') == 'dubbing-acceptance-' + tag]
    require(len(matches) == 1 and matches[0].get('expired') is False, 'exactly one unexpired artifact named dubbing-acceptance-' + tag + ' is required.')
    artifact = matches[0]
    require(type(artifact.get('size_in_bytes')) is int and 0 < artifact['size_in_bytes'] <= MAX_BYTES and type(artifact.get('id')) is int, 'acceptance artifact exceeds its safety bound.')
    require(isinstance(artifact.get('digest'), str) and artifact['digest'].startswith('sha256:') and SHA.fullmatch(artifact['digest'][7:]), 'GitHub must provide the acceptance artifact SHA-256 digest.')
    destination = Path(destination)
    require(not destination.exists(), 'acceptance destination must be a new owned directory.')
    archive = destination.with_name(destination.name + '.zip')
    require(not archive.exists(), 'acceptance download must not overwrite an existing file.')
    url = 'https://api.github.com/repos/' + REPOSITORY + '/actions/artifacts/' + str(artifact['id']) + '/zip'
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token, 'User-Agent': 'FORGE-dubbing-release-gate'})
    try:
        response = urllib.request.build_opener(NoRedirects()).open(request, timeout=30)
    except urllib.error.HTTPError as error:
        require(error.code in {301, 302, 303, 307, 308}, 'GitHub acceptance download failed.')
        target = urllib.parse.urlsplit(error.headers.get('Location', ''))
        require(target.scheme == 'https' and not target.username and not target.password and target.port in {None, 443}
                and ((target.hostname or '').endswith(('.blob.core.windows.net', '.actions.githubusercontent.com', '.githubusercontent.com'))), 'GitHub returned an unexpected acceptance download host.')
        # Never forward GitHub credentials to the signed artifact host.
        response = urllib.request.build_opener(NoRedirects()).open(urllib.request.Request(target.geturl()), timeout=30)
    try:
        with response, archive.open('xb') as handle:
            total = 0
            for chunk in iter(lambda: response.read(1024 * 1024), b''):
                total += len(chunk)
                require(total <= MAX_BYTES, 'acceptance download exceeds its safety bound.')
                handle.write(chunk)
        require(digest(archive) == artifact['digest'][7:], 'downloaded acceptance ZIP does not match GitHub artifact identity.')
        safe_extract(archive, destination)
        proof = verify_evidence(source, tag, source_commit, destination / 'DUBBING_ACCEPTANCE.json', run_id=run_id)
        proof['acceptance_actions_url'] = run.get('html_url', 'https://github.com/' + REPOSITORY + '/actions/runs/' + str(run_id))
        proof['acceptance_artifact'] = {'id': artifact['id'], 'name': artifact['name'], 'sha256': artifact['digest'][7:]}
        with zipfile.ZipFile(destination / 'DUBBING_EVIDENCE.zip', 'x', zipfile.ZIP_DEFLATED) as output:
            for name in ['DUBBING_ACCEPTANCE.json', *proof['verified_evidence']]:
                output.write(safe_file(destination, name), name)
        packaged = destination / 'DUBBING_EVIDENCE.zip'
        proof['evidence_archive'] = {'name': packaged.name, 'sha256': digest(packaged), 'size': packaged.stat().st_size}
        (destination / 'DUBBING_RELEASE_GATE.json').write_text(json.dumps(proof, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        return proof
    finally:
        archive.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inventory', 'verify', 'download'])
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--run-id', default='')
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'inventory':
            result = static_inventory(args.source, args.tag, args.source_commit)
        elif args.action == 'verify':
            result = verify_evidence(args.source, args.tag, args.source_commit, args.evidence, run_id=args.run_id)
        else:
            require(args.destination is not None, 'a new evidence destination is required.')
            result = download_evidence(args.source, args.tag, args.source_commit, run_id=args.run_id,
                                       destination=args.destination, token=os.environ.get('GH_TOKEN', ''))
        print(json.dumps(result, sort_keys=True, indent=2))
    except (GateError, OSError, ValueError, zipfile.BadZipFile) as error:
        # Network exceptions may contain signed URLs; never expose those here.
        print(str(error) if isinstance(error, GateError) else 'Dubbing stable-publication gate failed to read verified evidence.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
