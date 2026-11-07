"""Resolve a reviewed dubbing dependency profile on its actual target runtime.

Run on native Windows/Python3.12 to create Windows locks. This does not install
packages or model weights. A Linux cross-platform pip report is not native proof.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from forge import dubbing_models as models
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', default='qwen3-tts-0.6b-base')
    parser.add_argument('--device', choices=['cpu', 'cuda'], default='cpu')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--annotation', action='store_true')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise SystemExit('Native lock generation requires the reviewed Python3.12 target interpreter.')
    spec = models._spec(args.component)
    if args.device == 'cuda' and spec.get('cpu_only'):
        raise SystemExit('This optional component is registered for CPU only.')
    runtime = {'kind': 'existing', 'python': sys.executable, 'version': '.'.join(map(str, sys.version_info[:3])), 'download_bytes': 0}
    dependencies = models._dependency_plan(spec, runtime, args.device, args.output.parent, use_lock=False)
    system, architecture = models._target_platform()
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    clean = not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()
    result = {'schema': 1, 'profile': Path(spec['requirements']).stem,
              'requirements_sha256': dependencies['requirements_sha256'], 'system': system,
              'architecture': architecture, 'python_version': '3.12', 'device': args.device,
              'cross_resolved': False, 'native_windows_execution': os.name == 'nt',
              'physical_gpu_tested': False, 'model_inference_tested': False,
              'resolver_source_commit': commit, 'resolver_checkout_clean': clean,
              'actions_run_id': os.getenv('GITHUB_RUN_ID', ''),
              'wheels': dependencies['wheels'], 'download_bytes': dependencies['download_bytes']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(result, sort_keys=True, indent=2) + '\n').encode()
    if args.output.exists() and args.output.read_bytes() != raw:
        raise SystemExit('Refusing to overwrite a different existing dependency proof.')
    args.output.write_bytes(raw)
    print(json.dumps({'status': 'passed', 'system': system, 'python': '3.12', 'device': args.device,
                      'wheels': len(dependencies['wheels']), 'download_bytes': dependencies['download_bytes'],
                      'lock_sha256': hashlib.sha256(raw).hexdigest(), 'model_inference_tested': False}))
    if args.annotation:
        encoded = base64.b64encode(zlib.compress(raw, 9)).decode('ascii')
        if len(encoded) > 50000:
            raise SystemExit('The normalized dependency notice exceeds its bounded annotation size.')
        print('::notice title=Dubbing dependency lock::' + encoded)


if __name__ == '__main__':
    main()
