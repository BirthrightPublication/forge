"""FORGE adapter for Birthright Update Core's installation/recovery contract."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .birthright_update.core import UpdateError, digest


class LocalUpdaterAdapter:
    """The existing updater owns locks, backup journal and atomic code rollback.

    Used by the external maintenance host after it has waited on the exact service
    process handle. Restart belongs to that host so dependency work can finish too.
    """
    def __init__(self, root, python, package, *, service_stopped=False):
        self.root, self.python, self.package = Path(root), str(python), Path(package)
        self.service_stopped = service_stopped

    def _run(self, *args):
        result = subprocess.run([self.python, str(self.root / 'scripts' / 'update.py'),
                                 '--root', str(self.root), *args], cwd=self.root, timeout=300)
        if result.returncode:
            raise UpdateError('The verified FORGE updater reported a failure. Review its recovery journal and console.')

    def preflight(self, release, asset):
        if Path(asset).resolve() != self.package.resolve() or release.application != 'birthright.forge':
            raise UpdateError('Wrong application or package for this installation adapter.')
        self._run('--zip', str(self.package), '--dry-run')
        return {'package': str(self.package)}

    def quiesce(self, *, cancel_confirmed=False):
        if not self.service_stopped:
            raise UpdateError('The maintenance host must stop FORGE after coordinating active jobs.')

    def install(self, release, asset):
        self._run('--zip', str(self.package), '--yes', '--no-launch')
        return {'version': release.version}

    def validate(self, receipt):
        if (self.root / 'VERSION.txt').read_text('utf-8').strip() != receipt['version']:
            raise UpdateError('Installed FORGE version did not match the approved release.')
        manifest = json.loads((self.root / 'release-manifest.json').read_text('utf-8'))
        if manifest.get('version') != receipt['version']:
            raise UpdateError('Installed manifest version did not match the approved release.')
        for name, checksum in manifest['files'].items():
            if name.startswith('original_scripts/'):
                continue  # Existing user-owned originals deliberately survive updates.
            if name.casefold() == 'update forge.bat' and (self.root / 'Update-Application.ps1').is_file():
                continue  # Keep the owner's external GitHub recovery entrypoint.
            file = (self.root / name).resolve()
            if not file.is_relative_to(self.root.resolve()) or digest(file) != checksum:
                raise UpdateError('Installed application file failed integrity verification: ' + name)

    def rollback(self, receipt):
        self._run('--rollback', '--yes', '--no-launch')

    def recover(self):
        # A failed copy is journaled by update.py before any files are replaced.
        if (self.root / '.forge-update-state.json').is_file() or (self.root / '.forge-updating').is_file():
            self._run('--zip', str(self.package), '--yes', '--no-launch')
        return {'recovered': True}

    def restart(self):
        pass  # The external maintenance host restores queue preference, then restarts once.
