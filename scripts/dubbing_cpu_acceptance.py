"""Run real isolated Dubbing model smoke checks; never publish release approval.

The hosted Windows workflow uses CPU. A local owner can select CUDA after
installing the reviewed CUDA packs through FORGE. Model installation is refused
unless --approve-install is explicitly supplied. A sanitized JSON report and a
generated listening sample are retained locally. CI uploads only the JSON;
references, environments and model checkpoints never become evidence artifacts.
"""
from __future__ import annotations

import argparse
from array import array
from datetime import datetime, timezone
import difflib
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from forge import dubbing_models as models

REPORT_NAME = 'DUBBING_MODEL_SMOKE.json'
RUNTIME_NOTICES_NAME = 'DUBBING_RUNTIME_NOTICES.json'
COMPONENTS = ('opus-en-ru', 'qwen3-tts-0.6b-base', 'whisper-tiny')
SOURCE_FILES = ('forge/dubbing_models.py', 'scripts/dubbing_runtime.py', 'scripts/dubbing_cpu_acceptance.py')
REFERENCE_TEXT = 'Hello. This is a local speech recognition test. The next meeting is on Friday at ten in the morning.'
RUSSIAN_TEXT = 'Привет. Это проверка русского голоса.'

# This probe executes only stdlib metadata/hash code in the resolved interpreter.
# It never imports model libraries, follows external RECORD paths or scans media.
RUNTIME_METADATA_PROBE = r'''
import base64, csv, hashlib, importlib.metadata as metadata, io, json, os, pathlib, re, sys
MAX_FILES, MAX_HASH_BYTES, MAX_FILE_BYTES = 2000, 64 * 1024 * 1024, 4 * 1024 * 1024
MAX_TEXT_BYTES, MAX_ONE_TEXT = 2 * 1024 * 1024, 256 * 1024
request = json.loads(sys.stdin.buffer.read(128 * 1024 + 1))
prefix = pathlib.Path(sys.prefix).resolve()
if prefix != pathlib.Path(request['environment']).resolve():
    raise ValueError('The metadata probe did not execute in its resolved environment.')
if '.'.join(map(str, sys.version_info[:3])) != request['python_version']:
    raise ValueError('The actual interpreter version differs from the runtime marker.')
normalize = lambda name: re.sub(r'[-_.]+', '-', name).lower()
def ordinary(path):
    path = pathlib.Path(path)
    if not path.resolve().is_relative_to(prefix):
        raise ValueError('A distribution metadata or notice file escapes its runtime.')
    for item in (path, *path.parents):
        if item == prefix.parent:
            break
        if item.is_symlink() or getattr(item.stat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked distribution metadata or notice files are not exported.')
    return path
def declaration(value):
    raw = str(value or '').encode('utf-8')
    text = raw[:1024].decode('utf-8', errors='ignore')
    # A declaration is publisher metadata, never a license approval.
    if str(prefix).lower() in text.lower() or re.search(r'(?i)(?:[a-z]:[\\/]users[\\/]|/(?:home|Users)/)', text):
        text = '<declaration omitted: local user path>'
    return {'text': text, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'truncated': len(raw) > 1024}
expected = request['expected_versions']
packages, notices, integrity_failures = {}, [], []
file_count = hash_bytes = text_bytes = records = metadata_bytes = 0
for dist in metadata.distributions():
    if len(packages) >= 250:
        raise ValueError('Installed distribution count exceeded the audit bound.')
    info = ordinary(dist._path)
    for filename in ('METADATA', 'RECORD'):
        path = info / filename
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('An installed distribution has missing or oversized metadata.')
        ordinary(path)
        metadata_bytes += path.stat().st_size
    if metadata_bytes > 32 * 1024 * 1024:
        raise ValueError('Installed metadata exceeded the total audit bound.')
    name, version = normalize(str(dist.metadata.get('Name') or '')), str(dist.version)
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or name in packages:
        raise ValueError('Installed distribution identities are invalid or duplicated.')
    if name not in expected and name != 'pip':
        raise ValueError('An installed distribution is outside the registered wheel closure: ' + name)
    if name in expected and version != expected[name]:
        raise ValueError('An installed version differs from its exact wheel receipt: ' + name)
    row = {'package': name, 'version': version, 'bootstrap_tool': name not in expected,
           'license_declaration': declaration(dist.metadata.get('License')),
           'license_expression_declaration': declaration(dist.metadata.get('License-Expression')),
           'notice_file_count': 0}
    packages[name] = row
    # Python 3.12 Distribution.files filters missing entries. Read the bounded
    # RECORD explicitly so missing recorded notices remain visible evidence.
    files = []
    for record in csv.reader(io.StringIO((info / 'RECORD').read_text('utf-8'))):
        if len(record) != 3:
            raise ValueError('An installed RECORD entry has an invalid shape.')
        entry = metadata.PackagePath(record[0])
        entry.hash = metadata.FileHash(record[1]) if record[1] else None
        entry.size = int(record[2]) if record[2] else None
        entry.dist = dist
        files.append(entry)
        if len(files) + records > 200000:
            raise ValueError('Installed RECORD entries exceeded the audit bound.')
    records += len(files)
    if records > 200000:
        raise ValueError('Installed RECORD entries exceeded the audit bound.')
    seen = set()
    for entry in files:
        # Include named notices and SPDX/text files inside recorded license dirs.
        parts = pathlib.PurePosixPath(str(entry).replace('\\', '/')).parts
        if not parts or not (re.sub(r'[^A-Z0-9]', '', parts[-1].upper()).startswith(
                ('LICENSE', 'LICENCE', 'COPYING', 'NOTICE', 'THIRDPARTYNOTICE', 'THIRDPARTYLICENSE', 'THIRDPARTYLICENCE', 'COPYRIGHT', 'AUTHORS'))
            or any(re.sub(r'[^a-z0-9]', '', part.lower()) in {'licenses', 'license', 'licences', 'licence', 'copying', 'notice', 'notices',
                'thirdpartylicenses', 'thirdpartylicences', 'thirdpartynotices'} for part in parts[:-1])):
            continue
        if pathlib.PurePosixPath(str(entry)).is_absolute() or '..' in parts or any(':' in part for part in parts):
            raise ValueError('A recorded notice has an unsafe package-relative path.')
        relative = '/'.join(parts)
        if relative in seen:
            raise ValueError('A distribution records a notice more than once.')
        seen.add(relative)
        row['notice_file_count'] += 1
        file_count += 1
        if file_count > MAX_FILES:
            raise ValueError('Notice file count exceeded the audit bound.')
        item = {'package': name, 'version': version, 'path': relative, 'scope': 'installed_distribution'}
        target = pathlib.Path(dist.locate_file(entry))
        if not target.exists():
            item.update(integrity='missing', text_omission='recorded_notice_missing')
            notices.append(item); integrity_failures.append(name + '/' + relative); continue
        ordinary(target)
        size = target.stat().st_size
        if not target.is_file() or size > MAX_FILE_BYTES or hash_bytes + size > MAX_HASH_BYTES:
            raise ValueError('A recorded notice exceeded the bounded file/hash budget.')
        raw = target.read_bytes(); hash_bytes += len(raw)
        item.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        recorded = entry.hash
        if recorded is None or recorded.mode not in {'sha256', 'sha384', 'sha512'} or entry.size is None:
            item.update(integrity='pending', text_omission='record_hash_or_size_unavailable')
        elif (len(raw) != entry.size or base64.urlsafe_b64encode(hashlib.new(recorded.mode, raw).digest()).rstrip(b'=').decode('ascii') != recorded.value):
            item.update(integrity='failed', text_omission='record_hash_or_size_mismatch')
            integrity_failures.append(name + '/' + relative)
        else:
            item['integrity'] = 'record_verified'
            if len(raw) > MAX_ONE_TEXT:
                item['text_omission'] = 'per_file_text_bound'
            elif text_bytes + len(raw) > MAX_TEXT_BYTES:
                item['text_omission'] = 'total_text_bound'
            else:
                try:
                    text = raw.decode('utf-8-sig')
                except UnicodeDecodeError:
                    item['text_omission'] = 'not_utf8'
                else:
                    if any(ord(char) < 32 and char not in '\n\r\t' for char in text):
                        item['text_omission'] = 'non_text_control_bytes'
                    elif str(prefix).lower() in text.lower() or re.search(r'(?i)(?:[a-z]:[\\/]users[\\/]|/(?:home|Users)/)', text):
                        item['text_omission'] = 'local_user_path'
                    else:
                        item['text'] = text; text_bytes += len(raw)
        notices.append(item)
missing = sorted(set(expected) - set(packages))
if missing:
    raise ValueError('Registered wheel packages are missing: ' + ', '.join(missing))
result = {'python_version': '.'.join(map(str, sys.version_info[:3])),
          'installed_versions': {name: row['version'] for name, row in sorted(packages.items())},
          'packages': [packages[name] for name in sorted(packages)], 'notices': notices,
          'packages_without_recorded_notice_files': sorted(name for name, row in packages.items() if not row['notice_file_count']),
          'notice_integrity_failures': integrity_failures,
          'notice_hash_bytes': hash_bytes, 'record_entries_examined': records}
encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
if len(encoded) > 8 * 1024 * 1024:
    raise ValueError('Runtime audit JSON exceeded its output bound.')
sys.stdout.buffer.write(encoded)
'''


class AcceptanceBlocked(ValueError):
    """A required identity, dependency, approval or real device is unavailable."""


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def source_identity(root=None, additional_files=()):
    root = Path(root or ROOT).resolve()
    names = tuple(dict.fromkeys((*SOURCE_FILES, *additional_files)))
    if any(not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
           or not (root / name).resolve().is_relative_to(root) for name in names):
        raise AcceptanceBlocked('Source identity requires ordinary application-relative source files.')
    files = {name: digest(root / name) for name in names}
    manifest = root / 'release-manifest.json'
    value = json.loads(manifest.read_text('utf-8'))
    if any(value.get('files', {}).get(name) != sha for name, sha in files.items()):
        raise AcceptanceBlocked('The installed adapter bytes do not match the application manifest. Use the normal verified FORGE updater.')
    identity = {'source_files_sha256': files, 'source_manifest_sha256': digest(manifest)}
    if (root / '.git').exists():
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
        committed_manifest = subprocess.check_output(['git', 'show', 'HEAD:release-manifest.json'], cwd=root)
        if committed_manifest != manifest.read_bytes():
            raise AcceptanceBlocked('The source manifest differs from its committed identity. Commit verified source before acceptance.')
        identity.update(source_commit=commit, source_kind='git', source_checkout_clean=not bool(
            subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, text=True).strip()))
    else:
        package = value.get('package', {})
        commit = package.get('source_commit', '')
        original = package.get('source_manifest_sha256', '')
        if not isinstance(original, str) or not re.fullmatch('[a-f0-9]{64}', original):
            raise AcceptanceBlocked('This package has no verified original source manifest identity.')
        identity.update(installed_manifest_sha256=identity['source_manifest_sha256'], source_manifest_sha256=original)
        identity.update(source_commit=commit, source_kind='installed_manifest', source_checkout_clean=None)
    if not isinstance(identity['source_commit'], str) or not re.fullmatch('[a-f0-9]{40}', identity['source_commit']):
        raise AcceptanceBlocked('This application has no exact source commit binding. Use a FORGE package carrying its immutable source identity.')
    return identity


def _desired_device(component, device):
    return 'cpu' if component == 'whisper-tiny' else device


def installed_identities(data_root, device='cpu'):
    if device not in {'cpu', 'cuda'}:
        raise AcceptanceBlocked('Choose CPU or CUDA.')
    result = {}
    for component in COMPONENTS:
        selected = models.resolve(component, data_root)
        if selected['device'] != _desired_device(component, device):
            raise AcceptanceBlocked('Install the selected device profile through the reviewed FORGE setup plan before testing: ' + component + '.')
        result[component] = {key: selected[key] for key in ('model_revision', 'runtime_revision', 'device')}
    return result


def _controller_identity():
    packages = sorted((str(item.metadata.get('Name') or ''), item.version) for item in importlib.metadata.distributions())
    value = {'python_sha256': digest(Path(sys.executable).resolve()),
             'packages_sha256': hashlib.sha256(json.dumps(packages, sort_keys=True).encode()).hexdigest()}
    config = Path(sys.prefix) / 'pyvenv.cfg'
    if config.exists():
        value['pyvenv_cfg_sha256'] = digest(config)
    return value


def _safe_error(error, data_root, output):
    detail = type(error).__name__ + ': ' + str(error)
    context = getattr(error, 'stderr', None)
    if isinstance(context, bytes):
        context = context.decode('utf-8', errors='replace')
    if isinstance(context, str) and context.strip():
        detail += '\n' + context[-2000:]
    for path in sorted({str(data_root), str(output), str(ROOT), str(Path.home()), str(sys.executable)}, key=len, reverse=True):
        if path:
            detail = detail.replace(path, '<local path>')
    detail = re.sub(r'https?://[^\s\"<>]+', '<publisher URL>', detail)
    detail = re.sub(r'(?i)(authorization|token|password|secret)\s*[=:]\s*[^\s,;]+', r'\1=<private>', detail)
    detail = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|hf_[A-Za-z0-9]+)\b', '<private>', detail)
    return detail[-1500:]


def _bounded_command(arguments, *, payload=None, timeout=120, limit=8 * 1024 * 1024):
    """Bound pipe memory and execution even if an installed package prints noise."""
    raw = json.dumps(payload).encode('utf-8') if payload is not None else b''
    if len(raw) > 128 * 1024:
        raise AcceptanceBlocked('Runtime audit input exceeded its bound.')
    process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, 'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PIP_NO_INPUT': '1', 'PYTHONUTF8': '1'})
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()
    def read(stream, target):
        while True:
            block = stream.read(8192)
            if not block:
                break
            if len(target) + len(block) > limit:
                overflow.set()
                process.kill()
                break
            target.extend(block)
    def write():
        try:
            process.stdin.write(raw)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    readers = [threading.Thread(target=read, args=(stream, target), daemon=True)
               for stream, target in zip((process.stdout, process.stderr), buffers)]
    for thread in readers:
        thread.start()
    writer = threading.Thread(target=write, daemon=True); writer.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait(timeout=10)
        raise AcceptanceBlocked('The installed runtime audit exceeded its execution bound.') from None
    finally:
        for thread in readers:
            thread.join(timeout=5)
        writer.join(timeout=5)
        process.stdout.close(); process.stderr.close()
    if overflow.is_set() or any(thread.is_alive() for thread in (*readers, writer)):
        raise AcceptanceBlocked('The installed runtime audit exceeded its bounded output.')
    return subprocess.CompletedProcess(arguments, code, bytes(buffers[0]), bytes(buffers[1]))


def _ordinary_runtime_file(root, relative):
    name = Path(relative)
    if (not isinstance(relative, str) or not relative or name.is_absolute() or '..' in name.parts
            or '\\' in relative or ':' in relative):
        raise AcceptanceBlocked('Runtime audit files require ordinary relative paths.')
    path = Path(root) / name
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise AcceptanceBlocked('A runtime audit file escapes its environment.')
    for ancestor in (path, *path.parents):
        if ancestor == Path(root).parent:
            break
        if ancestor.is_symlink() or getattr(ancestor.stat(), 'st_file_attributes', 0) & 0x400:
            raise AcceptanceBlocked('Linked runtime audit files are not exported.')
    if not path.is_file():
        raise AcceptanceBlocked('A recorded runtime audit file is missing.')
    return path


def _wheel_identities(wheels):
    if not isinstance(wheels, list) or not 0 < len(wheels) <= 250:
        raise AcceptanceBlocked('The runtime marker has no bounded exact wheel inventory.')
    result = {}
    for wheel in wheels:
        name = re.sub(r'[-_.]+', '-', str(wheel.get('package', ''))).lower()
        value = {key: wheel.get(key) for key in ('version', 'name', 'sha256', 'size')}
        if (not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or name in result
                or not isinstance(value['version'], str) or not re.fullmatch(r'[A-Za-z0-9_.+!-]+', value['version'])
                or not isinstance(value['name'], str) or not re.fullmatch(r'[A-Za-z0-9_.+!-]+\.whl', value['name'])
                or not isinstance(value['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', value['sha256'])
                or type(value['size']) is not int or value['size'] <= 0):
            raise AcceptanceBlocked('Runtime wheel identities are invalid or duplicated.')
        result[name] = {'package': name, **value}
    return {name: result[name] for name in sorted(result)}


def _runtime_audit_plan(component, data_root):
    selected = models.resolve(component, data_root)
    environment = Path(selected['environment'])
    marker_path = _ordinary_runtime_file(environment, 'forge-dubbing-runtime.json')
    if marker_path.stat().st_size > 2 * 1024 * 1024:
        raise AcceptanceBlocked('The runtime marker exceeded its audit bound.')
    marker = json.loads(marker_path.read_text('utf-8'))
    spec = models._spec(component)
    requirements = models.ROOT / spec['requirements']
    version = marker.get('python', '')
    if (marker.get('schema') != 1 or marker.get('id') != selected['runtime_revision']
            or marker.get('id') != environment.name or marker.get('device') != selected['device']
            or marker.get('import_tested') is not True or not re.fullmatch(r'3\.(?:11|12)\.\d+', str(version))
            or marker.get('requirements_sha256') != digest(requirements)):
        raise AcceptanceBlocked('The actual runtime marker differs from its registered requirements or selected environment.')
    runtime = {'version': version}
    lock = models._load_wheel_lock(spec, runtime, selected['device'])
    if not lock:
        raise AcceptanceBlocked('The installed runtime has no selected native wheel lock.')
    expected = _wheel_identities(lock['wheels'])
    if _wheel_identities(marker.get('dependency_wheels')) != expected:
        raise AcceptanceBlocked('The actual runtime receipt differs from its selected native wheel lock.')
    lock_path = models._wheel_lock_path(spec, runtime, selected['device'])
    identity = {'runtime_revision': selected['runtime_revision'], 'device': selected['device'],
        'python_version': version, 'marker_sha256': digest(marker_path),
        'requirements': {'path': spec['requirements'], 'sha256': digest(requirements)},
        'native_wheel_lock': {'path': lock_path.relative_to(models.ROOT).as_posix(), 'sha256': digest(lock_path)},
        'wheel_artifacts': list(expected.values())}
    return selected, marker, identity


def collect_runtime_evidence(data_root):
    """Inspect actual isolated environments; dependency/notice evidence is not legal approval."""
    environments, texts = {}, []
    seen = {}
    for component in COMPONENTS:
        selected, marker, identity = _runtime_audit_plan(component, data_root)
        environment = Path(selected['environment']).resolve()
        if environment in seen:
            if seen[environment] != identity:
                raise AcceptanceBlocked('Components sharing an environment disagree about its registered closure.')
            environments[identity['runtime_revision']]['components'].append(component)
            continue
        if identity['runtime_revision'] in environments:
            raise AcceptanceBlocked('A runtime identity resolves to multiple environments.')
        seen[environment] = identity
        expected = {item['package']: item['version'] for item in identity['wheel_artifacts']}
        probe = _bounded_command([str(selected['python']), '-I', '-c', RUNTIME_METADATA_PROBE],
            payload={'environment': str(environment), 'python_version': marker['python'], 'expected_versions': expected})
        if probe.returncode:
            # Never export an uncontrolled traceback or absolute interpreter path.
            detail = _safe_error(subprocess.CalledProcessError(probe.returncode, [], stderr=probe.stderr), data_root, data_root)
            raise AcceptanceBlocked('Installed runtime metadata verification failed. ' + detail)
        observed = json.loads(probe.stdout)
        check = _bounded_command([str(selected['python']), '-I', '-m', 'pip', 'check'], limit=64 * 1024)
        if check.returncode:
            raise AcceptanceBlocked('The resolved runtime pip check failed (exit ' + str(check.returncode) +
                '; diagnostic SHA-256 ' + hashlib.sha256(check.stdout + check.stderr).hexdigest() + ').')
        notices = observed.pop('notices')
        for notice in notices:
            text = notice.pop('text', None)
            if text is not None:
                texts.append({'runtime_revision': identity['runtime_revision'], **notice, 'text': text})
                notice['text_collected'] = True
        source_records = marker.get('sources', [])
        if not isinstance(source_records, list) or len(source_records) > 1000:
            raise AcceptanceBlocked('The auxiliary source receipt exceeded its audit bound.')
        source_notices = []
        source_bytes = 0
        for record in source_records:
            name = record.get('path', '')
            parts = Path(name).parts
            if not parts or not (re.sub(r'[^A-Z0-9]', '', parts[-1].upper()).startswith(
                    ('LICENSE', 'LICENCE', 'COPYING', 'NOTICE', 'THIRDPARTYNOTICE', 'THIRDPARTYLICENSE', 'THIRDPARTYLICENCE', 'COPYRIGHT', 'AUTHORS'))
                or any(re.sub(r'[^a-z0-9]', '', part.lower()) in {'licenses', 'license', 'licences', 'licence', 'copying', 'notice', 'notices',
                    'thirdpartylicenses', 'thirdpartylicences', 'thirdpartynotices'} for part in parts[:-1])):
                continue
            path = _ordinary_runtime_file(environment, 'upstream/' + name)
            size = path.stat().st_size; source_bytes += size
            if size > 4 * 1024 * 1024 or source_bytes > 32 * 1024 * 1024:
                raise AcceptanceBlocked('Auxiliary source notices exceeded their hash budget.')
            if size != record.get('size') or digest(path) != record.get('sha256'):
                raise AcceptanceBlocked('An auxiliary source notice differs from its verified receipt.')
            source_notices.append({'path': name, 'sha256': record['sha256'], 'size': size,
                                   'text_omission': 'source_archive_text_review_pending'})
        if digest(environment / 'forge-dubbing-runtime.json') != identity['marker_sha256']:
            raise AcceptanceBlocked('The runtime marker changed during its audit.')
        environments[identity['runtime_revision']] = {**identity, **observed, 'components': [component],
            'pip_check': {'exit_code': 0, 'summary': 'No broken requirements reported by the resolved interpreter.',
                          'stdout_sha256': hashlib.sha256(check.stdout).hexdigest(),
                          'stderr_sha256': hashlib.sha256(check.stderr).hexdigest()},
            'notice_files': notices, 'source_notice_files': source_notices,
            'auxiliary_source_receipt_sha256': hashlib.sha256(json.dumps(source_records, sort_keys=True).encode()).hexdigest(),
            'auxiliary_source_files': len(source_records), 'source_notice_text_review_pending': bool(source_records),
            'legal_review_status': 'pending', 'full_notice_review_status': 'pending'}
    evidence = {'schema': 1, 'kind': 'installed_runtime_evidence', 'status': 'passed',
            'environment_count': len(environments), 'environments': environments,
            'license_declarations_are_approval': False, 'legal_review_status': 'pending'}
    if len(json.dumps(evidence, ensure_ascii=False).encode('utf-8')) > 1280 * 1024:
        raise AcceptanceBlocked('The runtime evidence exceeded its main-report serialization budget.')
    return evidence, texts


def write_runtime_notices(output, texts):
    """Keep public, RECORD-verified notice texts in an explicitly bounded sidecar."""
    value = {'schema': 1, 'kind': 'installed_runtime_notices', 'legal_review_status': 'pending',
             'notice_texts': [], 'omitted_texts': [], 'omitted_text_count': 0}
    for record in texts:
        if record.get('integrity') != 'record_verified' or len(record['text'].encode('utf-8')) > 256 * 1024:
            raise AcceptanceBlocked('Only bounded RECORD-verified notice text may enter the public sidecar.')
        value['notice_texts'].append(record)
        if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 2 * 1024 * 1024 - 32768:
            value['notice_texts'].pop()
            value['omitted_text_count'] += 1
            if len(value['omitted_texts']) < 64:
                value['omitted_texts'].append({**{key: record[key] for key in ('runtime_revision', 'package', 'path', 'sha256', 'size')},
                                              'text_omission': 'sidecar_total_text_bound'})
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    if len(raw) > 2 * 1024 * 1024:
        raise AcceptanceBlocked('The runtime notice sidecar exceeded its serialization bound.')
    path = Path(output) / RUNTIME_NOTICES_NAME
    with path.open('xb') as stream:
        stream.write(raw)
    return {'filename': RUNTIME_NOTICES_NAME, 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw),
            'included_texts': len(value['notice_texts']), 'omitted_texts': value['omitted_text_count'],
            'legal_review_status': 'pending'}


def media_probe(path):
    completed = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration:stream=codec_name,codec_type,sample_rate,channels', '-of', 'json', str(path)],
        check=True, capture_output=True, text=True, timeout=30)
    result = json.loads(completed.stdout)
    seconds = float(result.get('format', {}).get('duration', 0))
    if not math.isfinite(seconds) or not .05 <= seconds <= 30:
        raise AcceptanceBlocked('The generated smoke audio has no bounded playable duration.')
    streams = result.get('streams', [])
    if len(streams) != 1 or streams[0].get('codec_type') != 'audio' or streams[0].get('channels') != 1:
        raise AcceptanceBlocked('The smoke fixture/output must contain exactly one mono audio stream.')
    return {'duration': seconds, 'sample_rate': int(streams[0]['sample_rate']), 'channels': 1,
            'codec': streams[0]['codec_name'], 'bytes': Path(path).stat().st_size, 'sha256': digest(path)}


def decode_audio(path):
    meta = media_probe(path)
    completed = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:a:0',
        '-t', '30', '-ac', '1', '-ar', '24000', '-f', 'f32le', 'pipe:1'], check=True,
        capture_output=True, timeout=60)
    if not completed.stdout or len(completed.stdout) > 30 * 24000 * 4 or len(completed.stdout) % 4:
        raise AcceptanceBlocked('Decoded smoke audio exceeded its bounds or contained no samples.')
    samples = array('f'); samples.frombytes(completed.stdout)
    if sys.byteorder != 'little':
        samples.byteswap()
    if not all(math.isfinite(value) for value in samples):
        raise AcceptanceBlocked('Decoded speech contains nonfinite audio samples.')
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    if peak < .001 or rms < .0001:
        raise AcceptanceBlocked('The model output is silent; no audible inference pass is recorded.')
    return {**meta, 'decoded_and_probed': True, 'finite_samples': True, 'peak': peak, 'rms': rms}


def make_reference(directory):
    directory.mkdir(parents=True, exist_ok=False)
    text = directory / 'generated-reference.txt'; text.write_text(REFERENCE_TEXT, encoding='utf-8')
    target = directory / 'generated-reference.wav'
    if os.name == 'nt':
        raw = directory / 'sapi-reference.wav'
        script = """
$ErrorActionPreference='Stop'
$voice=New-Object -ComObject SAPI.SpVoice
$voices=$voice.GetVoices('Language=409')
if ($voices.Count -lt 1) { throw 'No installed English SAPI voice; generate no substitute recording.' }
$voice.Voice=$voices.Item(0)
$stream=New-Object -ComObject SAPI.SpFileStream
$stream.Format.Type=22
try {
  $stream.Open($env:FORGE_SMOKE_REFERENCE,3,$false)
  $voice.AudioOutputStream=$stream
  [void]$voice.Speak($env:FORGE_SMOKE_TEXT)
} finally { $stream.Close() }
"""
        env = {**os.environ, 'FORGE_SMOKE_REFERENCE': str(raw), 'FORGE_SMOKE_TEXT': REFERENCE_TEXT}
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                       check=True, capture_output=True, timeout=60, env=env)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-i', str(raw), '-ar', '24000',
                        '-ac', '1', '-c:a', 'pcm_s16le', str(target)], check=True, capture_output=True, timeout=30)
        provenance = 'Windows installed English SAPI voice, generated locally from fixed harness text.'
    else:
        # Run from the owned fixture directory so FFmpeg sees a simple relative
        # text filename instead of parsing private Windows/path punctuation.
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-f', 'lavfi', '-i',
            'flite=textfile=generated-reference.txt:voice=slt', '-ar', '24000', '-ac', '1',
            '-c:a', 'pcm_s16le', str(target)], cwd=directory, check=True, capture_output=True, timeout=60)
        provenance = 'FFmpeg libflite slt, generated locally from fixed harness text.'
    meta = decode_audio(target)
    if not 1 <= meta['duration'] <= 30:
        raise AcceptanceBlocked('The generated reference is outside the model reference limits.')
    return target, provenance, meta


def _metrics(value, desired):
    if value.get('inference_tested') is not True or value.get('actual_device') != desired:
        raise AcceptanceBlocked('A real inference result from the requested device is required.')
    actual_gpu = value.get('physical_gpu_tested') is True
    if actual_gpu != (desired == 'cuda'):
        raise AcceptanceBlocked('The inference result does not prove the requested physical device.')
    result = {key: value[key] for key in ('model_revision', 'runtime_revision', 'elapsed_seconds', 'actual_device', 'physical_gpu_tested')}
    result['inference_tested'] = True
    if desired == 'cuda':
        for key in ('gpu_name', 'gpu_capability', 'peak_vram_bytes', 'peak_reserved_vram_bytes'):
            if key not in value:
                raise AcceptanceBlocked('Physical GPU evidence is incomplete: ' + key + '.')
            result[key] = value[key]
        if not result['gpu_name'] or type(result['peak_vram_bytes']) is not int or result['peak_vram_bytes'] <= 0:
            raise AcceptanceBlocked('The GPU result has no positive actual allocation evidence.')
    return result


def _install(data_root, device, progress):
    groups = [(COMPONENTS, 'cpu')] if device == 'cpu' else [(COMPONENTS[:2], 'cuda'), (COMPONENTS[2:], 'cpu')]
    receipts = []
    for components, chosen in groups:
        change_device = False
        for component in components:
            try:
                change_device |= models.resolve(component, data_root)['device'] != chosen
            except (models.ComponentUnavailable, ValueError):
                pass
        action = 'repair' if change_device else 'install'
        plan = models.installation_plan(list(components), data_root, device=chosen, action=action)
        receipts.append({'components': list(components), 'device': chosen, 'action': action,
                         'quoted_download_bytes': plan['download_bytes'], 'quoted_storage_bytes': plan['required_storage_bytes']})
        for component in components:
            models.install(component, data_root, consent=True, plan=plan, device=chosen, action=action,
                           repair=action == 'repair', progress=progress)
    return receipts


def run_acceptance(data_root, output, *, device='cpu', approve_install=False, require_native_windows=False,
                   require_clean_source=False, expected_models=None, expected_source=None, progress=None):
    data_root, output = Path(data_root).resolve(), Path(output).resolve()
    if data_root == ROOT or data_root in ROOT.parents or data_root == data_root.parent:
        raise AcceptanceBlocked('Choose the dedicated model data directory, not the application/source root.')
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema': 1, 'kind': 'dubbing_model_smoke', 'status': 'blocked',
              'observed_at': datetime.now(timezone.utc).isoformat(), 'requested_device': device,
              'native_windows_execution': os.name == 'nt', 'physical_gpu_tested': False,
              'inference_substituted': False, 'private_inputs_used': False, 'reference_consent': True,
              'through_forge_gui_install': False, 'satisfies_release_acceptance': False,
              'platform': {'system': platform.system(), 'architecture': platform.machine(), 'python': platform.python_version()},
              'components': {}, 'remaining_acceptance': ['GUI install/repair', 'native updater/upgrade/rollback',
                  'human translation and voice quality review', 'live reviewed YouTube OAuth/private upload', 'final release inventory and owner approval']}
    protected = _controller_identity()
    try:
        if device not in {'cpu', 'cuda'}:
            raise AcceptanceBlocked('Choose CPU or CUDA.')
        if require_native_windows and os.name != 'nt':
            raise AcceptanceBlocked('This check requires actual native Windows execution; Linux/cross-platform output is not substituted.')
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise AcceptanceBlocked('Install the verified FFmpeg and FFprobe prerequisites before downloading models.')
        extra_files = tuple(expected_source.get('source_files_sha256', {})) if expected_source is not None else ()
        source = source_identity(additional_files=extra_files); report.update(source)
        if require_clean_source and (source['source_kind'] != 'git' or source['source_checkout_clean'] is not True):
            raise AcceptanceBlocked('The native Actions check requires a clean checkout of its exact source commit.')
        if expected_source is not None and source != expected_source:
            raise AcceptanceBlocked('Application source changed after the acceptance request; review and queue it again.')
        if approve_install:
            if expected_models is not None:
                raise AcceptanceBlocked('A frozen installed-model check cannot authorize an installation.')
            report['installation_plans'] = _install(data_root, device, progress)
        identities = installed_identities(data_root, device)
        if expected_models is not None and identities != expected_models:
            raise AcceptanceBlocked('Installed model/runtime identities changed after approval; queue a fresh check.')
        report['installed_identities'] = identities
        for component in COMPONENTS:
            report['components'][component] = {'verification': models.verify(component, data_root)}
        runtime_evidence, notice_texts = collect_runtime_evidence(data_root)
        report['runtime_evidence'] = runtime_evidence
        report['runtime_notice_artifact'] = write_runtime_notices(output, notice_texts)
        if any(row['notice_integrity_failures'] for row in runtime_evidence['environments'].values()):
            raise AcceptanceBlocked('An installed notice failed its recorded hash/size integrity check; no altered notice text is exported. Use reviewed Repair.')
        fixture_root = data_root / 'dubbing' / 'acceptance-fixtures' / uuid.uuid4().hex
        reference, provenance, reference_media = make_reference(fixture_root)
        report.update(reference_provenance=provenance, reference_text=REFERENCE_TEXT, reference_media=reference_media)
        base = {'model_revision': identities['opus-en-ru']['model_revision'], 'device': device}
        translation = models.test('opus-en-ru', data_root, request={**base, 'mode': 'faithful',
            'glossary': {'FORGE': 'ФОРДЖ'}, 'segments': [
                {'id': 'owned-1', 'start': 0, 'end': 3, 'speaker': 'test-speaker', 'source_text': 'Hello. This is a local translation test.'},
                {'id': 'owned-2', 'start': 3, 'end': 7, 'speaker': 'test-speaker', 'source_text': 'FORGE keeps the original video. The project has 3 segments.'},
                {'id': 'owned-3', 'start': 7, 'end': 10, 'speaker': 'test-speaker', 'source_text': 'The meeting starts on 2026-11-13 at 10:30.'}]}, progress=progress)
        translated = translation.get('segments', [])
        if len(translated) != 3 or translation.get('generation_calls', 0) <= 0 or any(not re.search('[А-Яа-яЁё]', row.get('text', '')) for row in translated):
            raise AcceptanceBlocked('The translation model produced no actual Russian draft for every fixture segment.')
        for index, row in enumerate(translated):
            if (row.get('id'), row.get('start'), row.get('end'), row.get('speaker')) != ('owned-' + str(index + 1), [0, 3, 7][index], [3, 7, 10][index], 'test-speaker'):
                raise AcceptanceBlocked('Translation changed the stable source identity/timeline.')
            if re.search(r'(?:\.\s*){4,}', row['text']):
                raise AcceptanceBlocked('Punctuation-only decoder repetition is still present.')
        if 'ФОРДЖ' not in translated[1]['text'] or '3' not in translated[1]['text'] or '2026-11-13' not in translated[2]['text'] or '10:30' not in translated[2]['text']:
            raise AcceptanceBlocked('A protected glossary term, number or date was lost.')
        report['components']['opus-en-ru']['inference'] = {**_metrics(translation, device), 'segments': translated,
            'generation_calls': translation['generation_calls'], 'warnings': translation.get('warnings', []),
            'human_quality_approved': False}
        english = models.test('whisper-tiny', data_root, request={'source': str(reference), 'language': 'en',
            'device': 'cpu', 'model_revision': identities['whisper-tiny']['model_revision']}, progress=progress)
        english_meta = _metrics(english, 'cpu')
        segments = english.get('segments', [])
        if not segments:
            raise AcceptanceBlocked('The recognizer produced no real speech segments.')
        for segment in segments:
            if not 0 <= segment['start'] < segment['end'] <= reference_media['duration'] + .15 or not segment.get('words'):
                raise AcceptanceBlocked('Actual segment/word timings are missing or outside the source clip.')
            if any(not math.isfinite(word['start'] + word['end']) or not 0 <= word['start'] <= word['end'] <= reference_media['duration'] + .15 for word in segment['words']):
                raise AcceptanceBlocked('Actual word timings are invalid.')
        recognized = ' '.join(row['source_text'] for row in segments)
        expected_words = set(re.findall('[a-z]+', REFERENCE_TEXT.lower()))
        coverage = len(expected_words & set(re.findall('[a-z]+', recognized.lower()))) / len(expected_words)
        if coverage < .65:
            raise AcceptanceBlocked('The generated English fixture was not recognized with sufficient smoke-test coverage.')
        report['components']['whisper-tiny']['inference'] = {**english_meta, 'segments': segments,
            'timings_verified': True, 'owned_fixture_word_coverage': coverage}
        voice = models.test('qwen3-tts-0.6b-base', data_root, request={'device': device, 'consent': True,
            'ref_audio': str(reference), 'ref_audio_sha256': reference_media['sha256'], 'ref_text': REFERENCE_TEXT,
            'text': RUSSIAN_TEXT, 'max_new_tokens': 256,
            'model_revision': identities['qwen3-tts-0.6b-base']['model_revision']}, progress=progress)
        voice_meta = _metrics(voice, device)
        generated = Path(voice['path']); speech_media = decode_audio(generated)
        if not .5 <= speech_media['duration'] <= 30 or voice.get('sha256') != speech_media['sha256']:
            raise AcceptanceBlocked('The actual saved Russian speech output failed decoding/hash checks.')
        if voice.get('reference_sha256') != reference_media['sha256'] or digest(reference) != reference_media['sha256']:
            raise AcceptanceBlocked('The approved generated reference changed during synthesis.')
        shutil.copyfile(generated, output / 'Russian-acceptance.wav')
        russian = models.invoke('whisper-tiny', data_root, {'task': 'transcribe', 'source': str(generated),
            'language': 'ru', 'device': 'cpu', 'model_revision': identities['whisper-tiny']['model_revision']}, progress=progress)
        _metrics(russian, 'cpu')
        recovered = ' '.join(row['source_text'] for row in russian.get('segments', []))
        normalize = lambda text: ' '.join(re.findall('[а-яё]+', text.lower()))
        similarity = difflib.SequenceMatcher(None, normalize(RUSSIAN_TEXT), normalize(recovered)).ratio()
        if similarity < .6:
            raise AcceptanceBlocked('Independent local recognition did not recover the generated Russian smoke phrase.')
        report['components']['qwen3-tts-0.6b-base']['inference'] = {**voice_meta, 'text': RUSSIAN_TEXT,
            'media': speech_media, 'reference_sha256': reference_media['sha256'], 'human_quality_approved': False,
            'independent_recognition': {'text': recovered, 'normalized_character_similarity': similarity,
                                        'model_revision': russian['model_revision']}}
        if installed_identities(data_root, device) != identities or source_identity(additional_files=extra_files) != source:
            raise AcceptanceBlocked('Source or model/runtime identities changed during acceptance; retain the outputs and queue a fresh check.')
        # A separate controller process must read the persisted real receipts.
        code = "import json,sys;from forge import dubbing_models as m;v=m.catalog(sys.argv[1]);print(json.dumps([{k:r[k] for k in ('id','ready','inference_verified','model_revision','runtime_revision','device')} for r in v if r['id'] in " + repr(COMPONENTS) + "]))"
        reopened = json.loads(subprocess.check_output([sys.executable, '-c', code, str(data_root)], cwd=ROOT, text=True, timeout=180))
        if len(reopened) != 3 or not all(row['ready'] and row['inference_verified'] and row['model_revision'] == identities[row['id']]['model_revision'] and row['runtime_revision'] == identities[row['id']]['runtime_revision'] for row in reopened):
            raise AcceptanceBlocked('A fresh controller process did not retain the verified installed/inference receipts.')
        report.update(status='passed', reference_unchanged=True, verified_after_fresh_controller=True,
                      reopened_models=reopened, physical_gpu_tested=device == 'cuda')
        if device == 'cpu':
            report['remaining_acceptance'].append('Physical GPU/owner RTX 5080 acceptance')
    except Exception as error:
        report.update(status='blocked' if isinstance(error, (AcceptanceBlocked, models.ComponentUnavailable)) else 'failed',
                      error=_safe_error(error, data_root, output))
    finally:
        unchanged = protected == _controller_identity()
        report['controller_environment_unchanged'] = unchanged
        if not unchanged:
            report.update(status='failed', error='The application controller environment changed during acceptance.')
        (output / REPORT_NAME).write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--device', choices=['cpu', 'cuda'], default='cpu')
    parser.add_argument('--approve-install', action='store_true')
    parser.add_argument('--require-native-windows', action='store_true')
    parser.add_argument('--require-clean-source', action='store_true')
    args = parser.parse_args()
    last = [0.0, '']
    def progress(number, message, **details):
        now = time.monotonic()
        if now - last[0] >= 10 or message != last[1]:
            print(json.dumps({'kind': 'progress', 'progress': round(number, 2), 'message': message,
                              'stage': details.get('stage', 'Model acceptance')}), flush=True)
            last[:] = [now, message]
    report = run_acceptance(args.data_root, args.output, device=args.device, approve_install=args.approve_install,
        require_native_windows=args.require_native_windows, require_clean_source=args.require_clean_source, progress=progress)
    print(json.dumps({key: report.get(key) for key in ('kind', 'status', 'source_commit', 'native_windows_execution',
        'physical_gpu_tested', 'controller_environment_unchanged', 'error')}, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
