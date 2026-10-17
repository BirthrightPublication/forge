"""Offline C2PA inspection. This module never creates or signs credentials.

The optional CAI SDK runs in a bounded subprocess because it parses untrusted
native-format data. Network manifests, OCSP and remote identity resolution are
disabled explicitly; an empty network allowlist is an additional SDK guard.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .core import ROOT, no_window

TESTED_VERSION = '0.38.0'
MAX_REPORT_BYTES = 8 * 1024 * 1024
OFFLINE_SETTINGS = {
    'verify': {
        'verify_after_reading': True, 'verify_trust': True,
        'verify_timestamp_trust': True, 'remote_manifest_fetch': False,
        'ocsp_fetch': False,
    },
    'core': {
        'allowed_network_hosts': [], 'decode_identity_assertions': False,
        'backing_store_memory_threshold_in_mb': 64,
    },
}


def capabilities() -> dict:
    """Import-free dependency discovery; never downloads a trust list."""
    try:
        version = importlib.metadata.version('c2pa-python')
        available = importlib.util.find_spec('c2pa') is not None
    except (importlib.metadata.PackageNotFoundError, ImportError, ValueError):
        version, available = None, False
    compatible = available and version == TESTED_VERSION
    return {
        'name': 'c2pa-python', 'version': version, 'available': available,
        'supported_version': TESTED_VERSION, 'compatible': compatible,
        'license': 'MIT OR Apache-2.0', 'network': 'disabled',
        'installation': 'Install the optional requirements-media-privacy.txt dependencies.' if not compatible else '',
    }


def _base(tool: dict, status: str, message: str) -> dict:
    return {
        'status': status, 'method': 'Local C2PA SDK reader', 'tool': tool,
        'message': message, 'active_manifest': None, 'manifests': {},
        'signature': {'status': 'unknown', 'scope': 'Active manifest cryptographic signature'},
        'trust': {'status': 'unknown', 'scope': 'SDK bundled/local trust configuration; no online refresh or OCSP'},
        'binding': {'status': 'unknown', 'scope': 'Active manifest hash binding to this media'},
        'validation_state': None, 'validation_status': [], 'validation_results': {},
        'limitations': [
            'Not detected means no embedded manifest was found by this supported parser; it is not proof of absence.',
            'Remote manifests, online revocation and online trust-list refresh are disabled.',
            'Signature integrity, media binding and issuer trust are separate results. Signed assertions are not independent proof of their claims.',
            'CAWG identity assertions are not remotely resolved or independently verified.',
        ],
    }


def summarize_manifest(data: dict, tool: dict) -> dict:
    """Use explicit SDK success/failure evidence, never absence of errors."""
    result = _base(tool, 'detected', 'An embedded Content Credentials manifest was inspected locally.')
    manifests = data.get('manifests', {})
    if not isinstance(manifests, dict) or not manifests:
        return _base(tool, 'unknown', 'The parser returned no readable manifest store.')
    result.update({key: data.get(key, default) for key, default in (
        ('active_manifest', None), ('manifests', {}), ('validation_state', None),
        ('validation_status', []), ('validation_results', {}),
    )})
    active = data.get('validation_results', {}).get('activeManifest', {})
    successes = {item.get('code', '') for item in active.get('success', []) if isinstance(item, dict)}
    failures = {item.get('code', '') for item in active.get('failure', []) if isinstance(item, dict)}
    # Legacy flattened status may contain ingredient failures. Only use active
    # manifest results to issue affirmative integrity/trust/binding claims.
    if any(code in failures for code in ('claimSignature.mismatch', 'claimSignature.missing', 'claimSignature.invalid')):
        result['signature']['status'] = 'invalid'
    elif 'claimSignature.validated' in successes:
        result['signature']['status'] = 'valid'
    if 'signingCredential.untrusted' in failures:
        result['trust']['status'] = 'untrusted'
    elif 'signingCredential.trusted' in successes:
        result['trust']['status'] = 'trusted'
    matches = {'assertion.dataHash.match', 'assertion.bmffHash.match', 'assertion.boxesHash.match'}
    mismatches = {'assertion.dataHash.mismatch', 'assertion.bmffHash.mismatch', 'assertion.boxesHash.mismatch'}
    if failures & mismatches:
        result['binding']['status'] = 'mismatch'
    elif successes & matches:
        result['binding']['status'] = 'match'
    return result


def _inspect_library(path: Path) -> dict:
    """Child-process-only SDK entry point, also usable by focused tests."""
    tool = capabilities()
    if not tool['compatible']:
        return _base(tool, 'unsupported', 'The tested optional C2PA SDK is not installed.')
    try:
        import c2pa
        with c2pa.Context.from_dict(OFFLINE_SETTINGS) as context:
            # A binary stream avoids implicit sibling .c2pa sidecar discovery.
            # Only the explicitly selected media is inspected.
            with path.open('rb') as stream:
                with c2pa.Reader(path.suffix.lstrip('.'), stream, context=context) as reader:
                    raw = reader.json()
                    if len(raw.encode('utf-8')) > MAX_REPORT_BYTES:
                        return _base(tool, 'unknown', 'The embedded manifest exceeds the inspection report limit.')
                    return summarize_manifest(json.loads(raw), tool)
    except Exception as exc:
        name = type(exc).__name__
        # Do not expose arbitrary parser messages: they can contain private
        # directories, embedded URLs or values from untrusted media.
        if name == '_C2paManifestNotFound':
            return _base(tool, 'not_detected', 'No embedded C2PA manifest was detected by the SDK.')
        if name == '_C2paNotSupported':
            return _base(tool, 'unsupported', 'This media format is unsupported by the C2PA SDK.')
        if name == '_C2paRemoteManifest':
            return _base(tool, 'unknown', 'A remote manifest reference could not be inspected with network access disabled.')
        return _base(tool, 'unknown', 'The local C2PA parser could not complete validation (' + name[:80] + ').')


def inspect_provenance(path: Path, settings: dict | None = None) -> dict:
    """Inspect a selected local file without uploading or persisting its data."""
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('Choose an existing local media file.')
    tool = capabilities()
    if not tool['compatible']:
        return _base(tool, 'unsupported', 'Install the tested optional c2pa-python dependency to inspect Content Credentials.')
    # Temporary stdout also prevents an unexpectedly large child response from
    # filling parent memory; the child has a separate native-parser lifetime.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'forge.provenance', '--inspect-internal', str(path)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                timeout=120, **no_window(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return _base(tool, 'unknown', 'The isolated C2PA inspection could not finish within its resource limits.')
        if result.returncode or output.tell() > MAX_REPORT_BYTES:
            return _base(tool, 'unknown', 'The isolated C2PA parser failed or exceeded the report limit.')
        output.seek(0)
        try:
            report = json.load(output)
            if not isinstance(report, dict) or 'status' not in report:
                raise ValueError('Invalid parser result')
            return report
        except (ValueError, UnicodeError):
            return _base(tool, 'unknown', 'The isolated C2PA parser returned an unreadable result.')


def _main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] != '--inspect-internal':
        raise SystemExit('Internal inspection interface only.')
    if os.name != 'nt':
        import resource
        # Windows still has subprocess timeout and bounded result handling.
        resource.setrlimit(resource.RLIMIT_CPU, (110, 110))
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_REPORT_BYTES * 2, MAX_REPORT_BYTES * 2))
    print(json.dumps(_inspect_library(Path(sys.argv[2])), ensure_ascii=False))


if __name__ == '__main__':
    _main()
