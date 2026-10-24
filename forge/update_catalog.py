"""Bounded background discovery from registered publishers; discovery never installs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__
from .core import ROOT, json_write
from .birthright_update.core import Asset, NetworkPolicy, Transport
from .dependencies import inspect
from .model_cache import inventory, REPOSITORIES, REVISION

RELEASE_API = 'https://api.github.com/repos/BirthrightPublication/forge/releases/latest'
VERSION = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$')


def version(value):
    match = VERSION.fullmatch(value or '')
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


POLICY = NetworkPolicy(origins=(
    'https://api.github.com/repos/BirthrightPublication/forge/',
    'https://github.com/BirthrightPublication/forge/releases/download/',
    'https://huggingface.co/api/models/',
    'https://pypi.org/pypi/',
), redirect_hosts=('release-assets.githubusercontent.com', 'objects.githubusercontent.com'))
TRANSPORT = Transport(POLICY, 'FORGE/' + __version__)


def read_json(url):
    return TRANSPORT.json(url)


def application_entry(release):
    tag = release.get('tag_name', '')
    if not VERSION.fullmatch(tag) or release.get('draft') or release.get('prerelease'):
        raise ValueError('No compatible stable application release.')
    expected = f'FORGE-{tag}-windows-x64.zip'
    assets = release.get('assets', [])
    windows = [a for a in assets if a.get('name') == expected]
    selectable_windows = [a for a in assets if re.search(r'(win(dows)?|x64|amd64)', a.get('name', ''), re.I) and a.get('name', '').lower().endswith('.zip')]
    sums = [a for a in assets if a.get('name') == expected + '.sha256']
    legacy = [a for a in assets if re.fullmatch(r'FORGE-v\d+\.\d+(?:\.\d+)?\.zip', a.get('name', ''))]
    # The managed local updater uses the existing legacy manifest contract. The
    # independently published app/ ZIP remains compatible with the user's PS1.
    available = len(windows) == len(selectable_windows) == len(sums) == len(legacy) == 1
    legacy_sum = [a for a in assets if legacy and a.get('name') == legacy[0]['name'] + '.sha256']
    available &= len(legacy_sum) == 1
    entry = {'id': 'application', 'category': 'application', 'label': 'FORGE', 'installed': __version__,
             'available': tag, 'compatible': available, 'action': 'update' if available and version(tag) > version(__version__) else '',
             'restart': True, 'published': release.get('published_at', ''), 'notes': str(release.get('body', ''))[:12000], 'bytes': windows[0].get('size') if windows else None,
             'rollback': 'Existing verified updater backup', 'url': 'https://github.com/BirthrightPublication/forge/releases/tag/' + tag}
    if available:
        prefix = 'https://github.com/BirthrightPublication/forge/releases/download/' + tag + '/'
        for asset in (windows[0], sums[0], legacy[0], legacy_sum[0]):
            if asset.get('browser_download_url') != prefix + asset['name']:
                raise ValueError('Unexpected release asset publisher or path.')
        entry.update(zip_url=legacy[0]['browser_download_url'], checksum_url=legacy_sum[0]['browser_download_url'], filename=legacy[0]['name'],legacy_bytes=legacy[0].get('size'))
    return entry


class Catalog:
    def __init__(self, store, manager):
        self.store, self.manager = store, manager
        self.lock = threading.Lock()
        self.busy = False
        self.thread = None
        self.maintenance_service = None

    def snapshot(self):
        value = self.store.get('update_catalog', {'entries': [], 'checked': None})
        value['busy'] = self.busy
        value['count'] = sum(bool(e.get('action')) for e in value['entries'])
        return value

    def start(self):
        def loop():
            while not self.manager.stop_event.is_set():
                with self.manager.lock:
                    if self.manager.stop_event.is_set():
                        return
                    if self.store.settings().get('automatic_update_checks', True):
                        self.refresh()
                if self.manager.stop_event.wait(300):
                    break
        threading.Thread(target=loop, daemon=True, name='forge-update-discovery').start()

    def refresh(self, force=False):
        with self.lock:
            previous = self.snapshot()
            if self.busy or (not force and time.time() < previous.get('next_check', 0)):
                return previous
            self.busy = True
            self.thread = threading.Thread(target=self._check, daemon=True)
            self.thread.start()
        return self.snapshot()

    def _check(self):
        errors = []
        try:
            with self.manager.lock:
                if self.manager.stop_event.is_set():
                    return
                settings = self.store.settings()
            entries = inspect(settings)
            for engine, package, major in [('downloads', 'yt-dlp', 2026), ('speakers', 'pyannote.audio', 4)]:
                entry = next(e for e in entries if e['id'] == engine)
                if not entry['compatible']:
                    continue
                try:
                    metadata = read_json('https://pypi.org/pypi/' + package + '/json')
                    candidates = []
                    for value, files in metadata.get('releases', {}).items():
                        if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', value) or int(value.split('.')[0]) != major:
                            continue
                        if not any(not f.get('yanked') and f.get('packagetype') == 'bdist_wheel'
                                   and (f.get('filename', '').endswith('none-any.whl') or 'win_amd64' in f.get('filename', '')) for f in files):
                            continue
                        candidates.append(value)
                    numbers = lambda value: tuple(int(part) for part in value.split('.'))
                    if candidates:
                        latest = max(candidates, key=numbers)
                        installed = entry['packages'][0]['version']
                        if re.fullmatch(r'\d+\.\d+(?:\.\d+)?', installed) and numbers(latest) > numbers(installed):
                            entry.update(available=package + '==' + latest, action='update',
                                         notes='Within the reviewed compatibility range. Wheel resolution and pip check run before installation is accepted.')
                except Exception:
                    errors.append(entry['label'] + ' update check unavailable.')
            try:
                entries.insert(0, application_entry(read_json(RELEASE_API)))
            except Exception:
                errors.append('Application release check unavailable; retry when online.')
            for item in inventory(self.store.data):
                # Only installed models produce update alerts; optional missing
                # models remain install choices, not six misleading update badges.
                model = item['model']
                entry = {'id': 'model:' + model, 'label': model, 'category': 'models',
                         'installed': (item['selected'] or item['revisions'][-1]['revision']) if item['cached'] else '',
                         'available': '', 'action': '', 'restart': False, 'revisions': item['revisions'],
                         'notes': 'Approved Hugging Face model. Existing revisions are retained.'}
                if item['cached']:
                    try:
                        doc = read_json('https://huggingface.co/api/models/' + REPOSITORIES[model])
                        revision = doc.get('sha', '')
                        if not REVISION.fullmatch(revision):
                            raise ValueError('Invalid model revision.')
                        entry.update(available=revision, compatible=True,
                                     action='update' if revision != entry['installed'] else '')
                    except Exception:
                        entry['notes'] = 'Offline discovery; existing cached revisions remain usable.'
                        errors.append('Model update check unavailable: ' + model)
                entries.append(entry)
            # Missing optional engines are install choices, not update notifications.
            for entry in entries:
                if entry.get('category') == 'dependencies' and entry.get('action'):
                    if not any(p['present'] for p in entry.get('packages', [])):
                        entry.update(action='', installable=True)
            result = {'entries': entries, 'checked': time.time(), 'errors': errors,
                      'next_check': time.time() + (1800 if errors else 43200)}
            with self.manager.lock:
                if self.manager.stop_event.is_set():
                    return
                self.store.set('update_catalog', result)
                approvals = self.store.get('dependency_approvals', [])
                repairs = [e['id'] for e in entries if e.get('category') == 'dependencies'
                           and e.get('repairable') and e['id'] in approvals]
                if (os.name == 'nt' and repairs and self.maintenance_service
                        and self.store.settings().get('automatic_dependency_repairs', True)
                        and time.time() - self.store.get('last_automatic_repair', 0) > 86400
                        and not self.manager.maintenance_pending and not self.manager.maintenance):
                    self.store.set('last_automatic_repair', time.time())
                    self.maintenance_service.request(repairs, preserve_queued=True)
        except Exception as exc:
            with self.manager.lock:
                if self.manager.stop_event.is_set():
                    return
                old = self.store.get('update_catalog', {'entries': []})
                self.store.set('update_catalog', {**old, 'errors': ['Dependency discovery failed: ' + str(exc)[:300]],
                                                'next_check': time.time() + 1800})
        finally:
            self.busy = False


def download_application(entry, root=ROOT, progress=None):
    """Download to Build only; the existing updater verifies the complete manifest."""
    if entry.get('id') != 'application' or not entry.get('action'):
        raise ValueError('No verified compatible application update is available.')
    checksum = TRANSPORT.metadata(entry['checksum_url']).decode('utf-8-sig').split()[0].lower()
    asset = Asset(entry['filename'], entry['zip_url'], checksum, size=entry.get('legacy_bytes'))
    return TRANSPORT.download(asset, root / 'Build', progress=progress or (lambda *args: None))


def mount(app, store, manager):
    catalog = Catalog(store, manager)
    app.state.update_catalog = catalog
    catalog.maintenance_service = app.state.maintenance_service

    @app.get('/api/updates/catalog')
    def snapshot():
        return catalog.snapshot()

    @app.post('/api/updates/refresh')
    def refresh():
        return catalog.refresh(force=True)

    install_lock = threading.Lock()

    @app.post('/api/updates/install')
    def install(body: dict):
        if not install_lock.acquire(blocking=False):
            raise ValueError('An update request is already being prepared. Review Updates for progress.')
        try:
            return install_selected(body)
        except Exception:
            store.set('update_transfer', {})
            raise
        finally:
            install_lock.release()

    def install_selected(body):
        if body.get('consent') is not True:
            raise ValueError('Approve the selected updates before installation.')
        ids = body.get('ids')
        if not isinstance(ids, list) or not ids or len(ids) > 30 or not all(isinstance(v, str) for v in ids):
            raise ValueError('Select registered updates.')
        entries = {e['id']: e for e in catalog.snapshot()['entries']}
        selected = []
        for id in dict.fromkeys(ids):
            entry = entries.get(id)
            if not entry or not (entry.get('action') or entry.get('installable') or entry['category'] == 'models' and not entry['installed']):
                raise ValueError('The selected update is no longer available. Refresh Updates.')
            selected.append(entry)
        engines = [e['id'] for e in selected if e['category'] == 'dependencies']
        app_entry = next((e for e in selected if e['category'] == 'application'), None)
        if (engines or app_entry) and os.name != 'nt':
            raise ValueError('Managed package/application installation requires Windows.')
        with manager.lock:
            manager.check_accepting()
        def progress(received, total, speed):
            store.set('update_transfer', {'status':'downloading','message':'Downloading the approved application package.',
                                         'received':received,'total':total,'speed':speed})
        if app_entry:
            store.set('update_transfer', {'status':'downloading','message':'Connecting to the approved release publisher.'})
        package = download_application(app_entry, progress=progress or (lambda *args: None)) if app_entry else None
        if package:
            store.set('update_transfer', {'status':'verifying','message':'Verifying checksum, manifest and protected paths.'})
        if package:
            result = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'update.py'), '--root', str(ROOT),
                                     '--zip', str(package), '--dry-run'], capture_output=True, text=True, timeout=90)
            if result.returncode:
                raise ValueError('The updater rejected this package: ' + result.stderr[-800:])
        from .entitlements import require_feature
        import uuid
        models = [e for e in selected if e['category'] == 'models']
        if models:
            require_feature(app, 'transcribe')
        with manager.lock:
            manager.check_accepting()
            for entry in models:
                model = entry['id'].split(':', 1)[1]
                payload = {'tool': 'model_download', 'options': {'model': model, 'revision': entry['available'], 'consent': True},
                           'settings': store.settings(), 'destination': str(store.data / 'models'),
                           'source': None, 'url': None, 'batch_id': uuid.uuid4().hex}
                store.add_job('model_download', 'Install ' + model, payload)
            if engines or package:
                # Model jobs may need the engine being repaired in this batch.
                # Keep them queued until maintenance verifies it and restarts.
                result = app.state.maintenance_service.request(engines, approve=True, application_package=package,
                                                             preserve_queued=bool(models))
                store.set('update_transfer', {})
                return result
        return {'message': 'Model installations queued; existing revisions are retained.'}

    @app.post('/api/updates/application')
    def application(body: dict):
        # One verified installation path, including shutdown and restart checks.
        return install({'ids':['application'], 'consent':body.get('consent')})
