"""Open owner verification with separate state through the official launcher.

No application/model dependency installation occurs here. Model approval,
reference consent and publishing review remain ordinary FORGE UI actions.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def initialize(root=ROOT):
    from forge.core import Store, json_write

    root = Path(root).resolve()
    verification = root / 'Build' / 'V0.10-Verification'
    data = verification / 'Data'
    for part in (root / 'Build', verification, data):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise RuntimeError('Verification storage must be ordinary directories inside this candidate; linked existing data is not changed.')
    os.environ['FORGE_DATA_DIR'] = str(data)
    os.environ['FORGE_PORT'] = '18765'
    marker = data / 'verification-bootstrap.json'
    if not marker.exists():
        existed = (data / 'forge.sqlite3').exists()
        if not existed:
            store = Store(data)
            try:
                settings = store.settings()
                settings.update(automatic_update_checks=False, daily_update_check=False,
                                automatic_dependency_repairs=False,
                                default_output=str(verification / 'Exports'), last_output='')
                store.set('settings', settings)
            finally:
                store.close()
        json_write(marker, {'schema': 1, 'mode': 'isolated-v0.10-verification',
                            'initial_defaults_applied': not existed, 'port': 18765})
    return data


def main():
    initialize()
    import launcher

    # Do not turn verification arguments into arbitrary source registrations.
    before = sys.argv[:]
    try:
        sys.argv = [str(ROOT / 'launcher.py')]
        launcher.main()
    finally:
        sys.argv = before


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('FORGE verification could not open: ' + str(error), file=sys.stderr)
        raise SystemExit(1) from None
