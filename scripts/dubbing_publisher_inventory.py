"""Collect immutable publisher identities and bounded, hash-checked public evidence.

This does not install environments, download large weights, activate models or
approve distribution. Unpinned discovery needs --discover and never edits pins.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SMALL_ARTIFACT_LIMIT = 8_000_000
SMALL_TOTAL_LIMIT = 32_000_000
DEFAULT_COMPONENTS = ('opus-en-ru', 'qwen3-tts-0.6b-base', 'whisper-tiny')


def verify_component(component, cache, *, discover=False):
    from forge import dubbing_models as models

    spec = models._spec(component)
    if not spec['parts']:
        raise models.ComponentUnavailable('This component has no reviewed publisher parts.')
    parts = []; downloaded_bytes = 0
    for descriptor in spec['parts']:
        if not descriptor.get('revision') and not discover:
            raise models.ComponentUnavailable('Unpinned discovery requires --discover; review evidence never activates a model.')
        part = models.discover_part(descriptor)
        card_verified = False
        for artifact in part['artifacts']:
            artifact['content_download_verified'] = False
            if artifact['algorithm'] == 'sha256':
                artifact['sha256'] = artifact['digest']
            if artifact['size'] > SMALL_ARTIFACT_LIMIT:
                continue
            downloaded_bytes += artifact['size']
            if downloaded_bytes > SMALL_TOTAL_LIMIT:
                raise models.ComponentUnavailable('The public evidence exceeds its small-file download allowance.')
            path = models._download(artifact, cache)
            artifact['sha256'] = models._hash(path)
            artifact['content_download_verified'] = True
            if artifact['name'] == 'README.md':
                card = path.read_text('utf-8')
                header = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', card, re.S)
                licenses = re.findall(r'^license:\s*([^\r\n]+?)\s*$', header.group(1) if header else '', re.M)
                if licenses != [part['license']]:
                    raise models.ComponentUnavailable('The downloaded model card license differs from the publisher metadata.')
                card_verified = True
        if not card_verified:
            raise models.ComponentUnavailable('A hash-verified publisher model card is required.')
        part['license_evidence'] = {
            'publisher_declared_id': part['license'], 'model_card': 'README.md',
            'model_card_content_verified': True,
            'license_files': [item['name'] for item in part['artifacts']
                              if Path(item['name']).name.upper().startswith(('LICENSE', 'NOTICE'))],
            'distribution_approved': False, 'full_notice_review_pending': True,
        }
        parts.append(part)
    return {'component': component, 'status': 'publisher_metadata_verified', 'parts': parts,
            'model_bytes': sum(part['bytes'] for part in parts), 'small_artifact_bytes_verified': downloaded_bytes,
            'installed': False, 'inference_tested': False, 'native_windows_execution': False,
            'physical_gpu_tested': False}


def main():
    from forge import dubbing_models as models

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', action='append', choices=sorted(models._specs()))
    parser.add_argument('--discover', action='store_true')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--evidence-cache', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Choose a new output file; existing verification evidence is preserved.')
    records = []
    for component in dict.fromkeys(args.component or DEFAULT_COMPONENTS):
        try:
            record = verify_component(component, args.evidence_cache, discover=args.discover)
        except (models.ComponentUnavailable, ValueError, UnicodeError) as error:
            record = {'component': component, 'status': 'blocked', 'reason': str(error), 'installed': False, 'inference_tested': False}
        records.append(record)
        print(json.dumps({key: value for key, value in record.items() if key != 'parts'}), flush=True)
    result = {'schema': 1, 'observed_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'Official publisher metadata and bounded small-artifact verification; no large-weight inference or distribution approval.',
              'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'source_checkout_clean': not bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
              'source_files_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                      for name in ('forge/dubbing_models.py', 'scripts/dubbing_publisher_inventory.py')},
              'components': records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return int(any(record['status'] != 'publisher_metadata_verified' for record in records))


if __name__ == '__main__':
    raise SystemExit(main())
