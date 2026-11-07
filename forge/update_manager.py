"""Read-only local release inventory and a fixed, explicitly opened Windows updater.

The browser never supplies executable names, archive paths, or command arguments.
Package inspection uses the same verifier as the external recovery updater.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from scripts import update as updater


def installation_preflight(root: Path, manager, application=None):
    """Read-only health, work and conservative free-space summary before consent."""
    from . import __version__
    root = root.resolve()
    try:
        disk_version = updater.installed_version(root)
        declared_version = (root / 'VERSION.txt').read_text('utf-8').strip()
    except (OSError,ValueError,updater.UpdateError):
        disk_version = ''
        declared_version = ''
    healthy = disk_version == declared_version == __version__ and (root / 'server.py').is_file()
    installed_bytes = 0
    try:
        manifest = json.loads((root / 'release-manifest.json').read_text('utf-8'))
        for name in manifest.get('files', {}):
            path = (root / name).resolve()
            if path.is_relative_to(root) and path.is_file() and not path.is_symlink() and not name.startswith('original_scripts/'):
                installed_bytes += path.stat().st_size
    except (OSError, ValueError, TypeError):
        pass
    size = (application or {}).get('legacy_bytes') or (application or {}).get('bytes')
    download_bytes = size if type(size) is int and size > 0 else None
    # Compression cannot be measured before transfer. This is explicitly an
    # estimate; exact manifest sizes are checked again before service shutdown.
    required = installed_bytes + (download_bytes or 0) * 8 + 512 * 1048576
    free = shutil.disk_usage(root).free
    with manager.lock:
        active = [job for job in manager.store.jobs(limit=100000)
                  if job['status'] in {'running', 'cancelling', 'finalizing'}]
        queued = sum(job['status'] == 'queued' for job in manager.store.jobs(limit=100000))
    return {'application_version': (application or {}).get('available'), 'download_bytes':download_bytes,
            'required_storage_bytes':required,'storage_estimate':True,'free_storage_bytes':free,
            'storage_ready':free >= required,'active_jobs':len(active),'queued_jobs':queued,
            'health':{'ready':healthy,'version':__version__,'disk_version':disk_version,
                      'pid':os.getpid(),'root':str(root)},
            'installation_directory':str(root),'download_directory':str(root / 'Build'),
            'message':'Exact package space is verified after download and before replacement. Queued work is preserved.'}


def windows_supported():
    return sys.platform == 'win32'


def update_home(root: Path):
    root = root.resolve()
    configured = os.getenv('FORGE_UPDATE_HOME')
    if configured:
        home = Path(configured).expanduser().resolve()
        if updater.find_installation(home).resolve() != root:
            raise ValueError('FORGE_UPDATE_HOME does not identify this running installation.')
        return home
    # Keep the established parent/Build layout when the app is in FORGE-v0.x.
    parent = root.parent
    if root.name.casefold().startswith('forge') and (parent / 'Build').is_dir():
        if updater.find_installation(parent).resolve() != root:
            raise ValueError('The parent Build folder belongs to another installation. Set FORGE_UPDATE_HOME explicitly.')
        return parent
    return root


def rollback_status(root: Path, home: Path):
    record_path = root / 'update-last.json'
    if not record_path.exists():
        return {'available': False, 'error': 'No completed update backup is registered.'}
    try:
        record = updater.read_json(record_path.read_text(encoding='utf-8'))
        backup = Path(record['backup']).resolve()
        if not backup.is_relative_to((home / 'Backups').resolve()):
            raise ValueError('The registered backup is outside this installation’s Backups directory.')
        journal = updater.inspect_backup(root, backup)
        if journal['state'] != 'completed' or journal['to'] != updater.installed_version(root):
            raise ValueError('The backup does not match the current completed update.')
        return {'available': True, 'from': journal['to'], 'to': journal['from'], 'backup': str(backup)}
    except (OSError, ValueError, KeyError, TypeError, updater.UpdateError) as exc:
        return {'available': False, 'error': str(exc)}


def inventory(root: Path):
    """Inspect disk state without creating directories, stopping work, or going online."""
    root = root.resolve()
    home = update_home(root)
    current = updater.installed_version(root)
    builds = home / 'Build'
    candidates = []
    warnings = []
    paths = sorted(builds.glob('FORGE-v*.zip'), key=lambda path: path.name.casefold()) if builds.exists() else []
    if len(paths) > 100:
        raise ValueError('More than 100 local build ZIPs were found. Move older builds into an archive folder before scanning.')
    for path in paths:
        item = {'name': path.name, 'status': 'invalid'}
        try:
            if updater.is_link(path):
                raise ValueError('Build ZIP links are not accepted; use a regular local file.')
            manifest, _ = updater.inspect_release(path)
            item.update(version=manifest['version'], minimum_from=manifest.get('minimum_from', '0.1.0'))
            with path.open('rb') as stream:
                item['sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
            target = updater.version(item['version'])
            if target == updater.version(current):
                item['status'] = 'current'
            elif target < updater.version(current):
                item['status'] = 'older'
            elif updater.version(item['minimum_from']) > updater.version(current):
                item.update(status='incompatible', error='Requires FORGE ' + item['minimum_from'] + ' or later.')
            else:
                item.update(updater.plan_release(root, path))
                item['status'] = 'available'
        except Exception as exc:
            # Each malformed/unreadable build is reported; it cannot hide other builds.
            item.update(status='invalid', error=str(exc))
        candidates.append(item)
    available = [item for item in candidates if item['status'] == 'available']
    selected = None
    if available:
        newest = max(updater.version(item['version']) for item in available)
        latest = [item for item in available if updater.version(item['version']) == newest]
        if len({item['sha256'] for item in latest}) != 1:
            for item in latest:
                item.update(status='conflict', error='Different ZIPs claim the same newest version. Keep only the intended trusted build.')
            warnings.append('Conflicting newest builds prevent an update selection.')
        else:
            selected = latest[-1]
    return {
        'checked': time.time(), 'version': current, 'installation': str(root), 'home': str(home),
        'build_directory': str(builds), 'backup_directory': str(home / 'Backups'),
        'platform_supported': windows_supported(), 'candidates': candidates, 'selected': selected,
        'rollback': rollback_status(root, home),
        'interrupted': (root / '.forge-update-state.json').exists() or (root / '.forge-updating').exists(),
        'warnings': warnings,
    }


def open_console(root: Path, home: Path, action: str):
    if not windows_supported():
        raise ValueError('The update console requires Windows. Inspection is available on this platform.')
    if action not in {'update', 'rollback'}:
        raise ValueError('Unknown update action.')
    interpreter = Path(sys.executable).with_name('python.exe')
    script = root / 'scripts' / 'update.py'
    if not interpreter.is_file() or not script.is_file():
        raise ValueError('The local Python updater is missing. Run the external Update FORGE.bat for recovery.')
    command = [str(interpreter), str(script), '--root', str(home), '--pause-on-exit']
    if action == 'rollback':
        command.append('--rollback')
    return subprocess.Popen(command, cwd=root, creationflags=subprocess.CREATE_NEW_CONSOLE)


def mount(app, store, manager, root: Path):
    import threading
    from fastapi import HTTPException

    scan_lock = threading.Lock()
    console = None

    def busy_jobs():
        with store.lock:
            return store.db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','cancelling','finalizing')").fetchone()[0]

    def inspect_inventory():
        try:
            return inventory(root)
        except updater.UpdateError as exc:
            raise ValueError(str(exc)) from exc

    @app.get('/api/application-updates')
    def application_updates():
        if not scan_lock.acquire(blocking=False):
            raise HTTPException(409, 'A local build scan is already in progress.')
        try:
            result = inspect_inventory()
            result.update(busy_jobs=busy_jobs(), maintenance=manager.maintenance)
            return result
        finally:
            scan_lock.release()

    @app.post('/api/application-updates/open')
    def open_updater(body: dict):
        nonlocal console
        if (set(body) != {'action', 'consent'} or body.get('consent') is not True
                or not isinstance(body.get('action'), str) or body['action'] not in {'update', 'rollback'}):
            raise ValueError('Choose update or rollback and explicitly confirm opening its native console.')
        with manager.lock:
            if console is not None and console.poll() is None:
                raise HTTPException(409, 'The update console is already open.')
            if manager.maintenance or busy_jobs():
                raise ValueError('Finish or cancel all queued/active jobs before opening the updater.')
            status = inspect_inventory()
            if status['interrupted']:
                raise ValueError('An interrupted update needs recovery through the external Update FORGE.bat.')
            if body['action'] == 'update' and not status['selected']:
                raise ValueError('No newer compatible, verified local build is available.')
            if body['action'] == 'rollback' and not status['rollback']['available']:
                raise ValueError(status['rollback']['error'])
            console = open_console(root.resolve(), Path(status['home']), body['action'])
        return {'started': True, 'message': 'Review the selected action in the Windows update console. FORGE stays running until you confirm there; the updater rechecks the queue and package before changing files.'}
