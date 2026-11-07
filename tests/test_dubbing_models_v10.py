"""Installer/protocol evidence with explicit owned synthetic artifacts.

These tests exercise real transfers, subprocesses and files. They are not neural
inference or owner-GPU acceptance; actual model tests have separate receipts.
"""
import copy
import hashlib
import http.server
import io
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import threading
import time
from types import SimpleNamespace
import urllib.request
import zipfile

import pytest

from forge import dubbing_models as models
from forge.core import json_write


@pytest.fixture
def managed(tmp_path, monkeypatch):
    data = tmp_path / 'private-data'
    blob = tmp_path / 'synthetic-tensor-artifact'
    blob.write_bytes(b'Owned installer fixture; not a neural checkpoint.')
    sha = hashlib.sha256(blob.read_bytes()).hexdigest()
    spec = copy.deepcopy(next(value for value in models._base_specs() if value['id'] == 'qwen3-tts-0.6b-base'))
    spec['sources'] = []
    spec['parts'] = [{'id': 'voice', 'repository': 'owned-fixture/voice', 'license': 'mit', 'revision': '1' * 40,
                     'artifacts': [{'name': 'weights.safetensors', 'url': 'https://download.pytorch.org/owned-fixture/weights.safetensors',
                                    'size': blob.stat().st_size, 'sha256': sha}]}]
    specs = {spec['id']: spec}
    monkeypatch.setattr(models, '_specs', lambda: copy.deepcopy(specs))
    monkeypatch.setattr(models, 'hardware', lambda *args, **kwargs: {'gpus': [], 'application_python_compatible': True,
        'managed_python_available': True, 'ffmpeg': True, 'ffprobe': True, 'disk_free_bytes': 50_000_000_000})
    monkeypatch.setattr(models, '_dependency_plan', lambda *args: {'requirements_sha256': models._hash(models.ROOT / spec['requirements']),
        'download_bytes': 20, 'wheels': [{'package': 'owned-fixture', 'version': '1.0', 'name': 'owned_fixture-1.0-py3-none-any.whl',
        'sha256': 'a' * 64, 'size': 20, 'url': 'https://files.pythonhosted.org/owned-fixture.whl'}]})
    monkeypatch.setattr(models, '_download', lambda *args, **kwargs: blob)
    def environment(data, plan, runtime, progress=None):
        root = Path(data) / 'environments' / plan['runtime_id']
        python = models._python(root); python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text('Owned synthetic interpreter marker; never executed.')
        json_write(root / 'forge-dubbing-runtime.json', {'schema': 1, 'id': root.name, 'import_tested': True,
                    'requirements_sha256': plan['dependencies']['requirements_sha256'], 'device': 'cpu', 'sources': []})
        return root
    monkeypatch.setattr(models, '_environment', environment)
    monkeypatch.setattr(models, '_runtime_probe', lambda *args: None)  # Explicit synthetic runtime, no inference claim.
    monkeypatch.setattr(models, '_run', lambda *args, **kwargs: '')
    for name, content in [('environments/speech-kokoro/preserve.txt', b'Original Kokoro environment'),
                          ('models/speech/ming/preserve.txt', b'Original Ming checkpoint'),
                          ('.venv/preserve.txt', b'Original application environment'),
                          ('presets/preserve.json', b'{"owner": "synthetic fixture"}')]:
        path = data / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
    return SimpleNamespace(data=data, spec=spec, specs=specs, blob=blob, component=spec['id'])


def install(managed, **kwargs):
    action = kwargs.pop('action', 'install')
    plan = models.installation_plan([managed.component], managed.data, device='cpu', action=action)
    result = models.install(managed.component, managed.data, consent=True, plan=plan, action=action, **kwargs)
    return result, plan


def test_all_official_ids_catalog_without_network_or_false_readiness(tmp_path, monkeypatch):
    monkeypatch.setattr(models, '_fetch_json', lambda *_: pytest.fail('Catalog must not fetch model metadata.'))
    values = models.catalog(tmp_path / 'empty-data')
    names = {value['id'] for value in values}
    assert {'opus-en-ru', 'qwen3-tts-0.6b-base', 'qwen3-tts-1.7b-base', 'whisper-large-v3-turbo', 'chatterbox-multilingual-v3'} <= names
    assert not any(value['ready'] or value['inference_verified'] for value in values)
    assert len(names) == len(values)


def test_unknown_pins_block_planning_without_installation(tmp_path, monkeypatch):
    monkeypatch.setattr(models, '_fetch_json', lambda *_: pytest.fail('An unreviewed revision must not be fetched as approved.'))
    with pytest.raises(models.ComponentUnavailable, match='awaiting verification'):
        models.installation_plan(['opus-en-ru'], tmp_path / 'data', device='cpu')
    assert not (tmp_path / 'data' / 'environments').exists()


def test_consent_and_server_owned_quote_are_required(managed):
    with pytest.raises(ValueError, match='Approve'):
        models.install(managed.component, managed.data)
    plan = models.installation_plan([managed.component], managed.data, device='cpu')
    forged = copy.deepcopy(plan)
    forged['components'][0]['device'] = 'cuda'
    forged.pop('plan_hash')
    forged['plan_hash'] = hashlib.sha256(models._canonical(forged)).hexdigest()
    with pytest.raises(ValueError, match='not issued'):
        models.install(managed.component, managed.data, consent=True, plan=forged)
    assert not list((managed.data / 'environments').glob('dubbing-*'))
    assert (managed.data / '.venv/preserve.txt').read_bytes() == b'Original application environment'


def test_quoted_action_cannot_change_after_approval(managed):
    plan = models.installation_plan([managed.component], managed.data, device='cpu')
    with pytest.raises(ValueError, match='action differs'):
        models.install(managed.component, managed.data, consent=True, plan=plan, repair=True)


def test_total_download_size_deduplicates_shared_runtime_and_artifacts(managed):
    second = copy.deepcopy(managed.spec); second['id'] = 'qwen3-tts-1.7b-base'
    managed.specs[second['id']] = second
    plan = models.installation_plan([managed.component, second['id']], managed.data, device='cpu')
    assert plan['download_bytes'] == managed.blob.stat().st_size + 20
    assert plan['components'][0]['runtime_id'] == plan['components'][1]['runtime_id']
    assert plan['required_storage_bytes'] > plan['download_bytes']


def test_install_verify_and_repeat_preserve_existing_engines(managed):
    first, plan = install(managed)
    second = models.install(managed.component, managed.data, consent=True, plan=plan)
    assert second['cached'] and first['model_revision'] == second['model_revision']
    assert models.verify(managed.component, managed.data)['files_verified'] == 1
    item = models.catalog(managed.data)[0]
    assert item['installed'] and item['ready'] and not item['inference_verified']
    assert (managed.data / 'environments/speech-kokoro/preserve.txt').read_bytes() == b'Original Kokoro environment'
    assert (managed.data / 'models/speech/ming/preserve.txt').read_bytes() == b'Original Ming checkpoint'
    assert (managed.data / '.venv/preserve.txt').read_bytes() == b'Original application environment'


def test_corrupt_runtime_is_not_reported_as_cached_ready(managed):
    _, plan = install(managed)
    models.resolve(managed.component, managed.data)['python'].unlink()
    with pytest.raises(models.ComponentUnavailable, match='Repair'):
        models.install(managed.component, managed.data, consent=True, plan=plan)
    assert not models.catalog(managed.data)[0]['ready']


def test_failed_repair_keeps_previous_activation_and_model(managed, monkeypatch):
    first, _ = install(managed)
    previous = models.resolve(managed.component, managed.data)
    state = models._state_path(managed.data).read_bytes()
    original = (previous['root'] / 'forge-dubbing-model.json').read_bytes()
    plan = models.installation_plan([managed.component], managed.data, device='cpu', action='repair')
    assert plan['components'][0]['runtime_id'] != first['runtime_revision']
    monkeypatch.setattr(models, '_download', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('Owned transfer failure')))
    with pytest.raises(RuntimeError, match='Owned transfer failure'):
        models.install(managed.component, managed.data, consent=True, plan=plan, repair=True)
    assert models._state_path(managed.data).read_bytes() == state
    assert (previous['root'] / 'forge-dubbing-model.json').read_bytes() == original
    assert models.resolve(managed.component, managed.data)['runtime_revision'] == first['runtime_revision']


def test_failed_final_activation_restores_previous_model(managed, monkeypatch):
    first, _ = install(managed)
    original = models.resolve(managed.component, managed.data)
    receipt = (original['root'] / 'forge-dubbing-model.json').read_bytes()
    plan = models.installation_plan([managed.component], managed.data, device='cpu', action='repair')
    monkeypatch.setattr(models, '_activate', lambda *args: (_ for _ in ()).throw(OSError('Owned activation failure')))
    with pytest.raises(OSError, match='Owned activation failure'):
        models.install(managed.component, managed.data, consent=True, plan=plan, repair=True)
    assert (original['root'] / 'forge-dubbing-model.json').read_bytes() == receipt
    assert models.resolve(managed.component, managed.data)['runtime_revision'] == first['runtime_revision']


def test_interrupted_final_swap_recovers_retained_verified_bundle(managed):
    install(managed)
    original = models.resolve(managed.component, managed.data)
    backup = original['root'].with_name('.retained-' + original['model_revision'] + '-' + 'f' * 32)
    original['root'].rename(backup)
    json_write(managed.data / 'dubbing/locks' / (managed.component + '.activation.json'),
               {'component': managed.component, 'revision': original['model_revision'], 'status': 'prepared', 'previous': str(backup)})
    restored = models.resolve(managed.component, managed.data)
    assert restored['root'].is_dir() and not backup.exists()


def test_move_storage_preserves_old_queued_paths_and_verifies_copy(managed, tmp_path):
    install(managed)
    original = models.resolve(managed.component, managed.data)
    result = models.move_storage(managed.data, tmp_path / 'new-storage', consent=True)
    moved = models.resolve(managed.component, managed.data)
    assert result['previous_storage_retained'] and original['root'].is_dir()
    assert moved['root'] != original['root'] and models.verify(managed.component, managed.data)['status'] == 'passed'


def test_remove_unused_keeps_projects_queued_revisions_and_other_engines(managed):
    import shutil
    install(managed)
    current = models.resolve(managed.component, managed.data)
    unused = current['root'].with_name('2' * 40)
    shutil.copytree(current['root'], unused)
    receipt = models._read(unused / 'forge-dubbing-model.json'); receipt['model_revision'] = unused.name
    json_write(unused / 'forge-dubbing-model.json', receipt)
    with sqlite3.connect(managed.data / 'forge.sqlite3') as database:
        database.execute('CREATE TABLE dubbing_projects(data TEXT)')
        database.execute('INSERT INTO dubbing_projects VALUES (?)', (json.dumps({'model_revision': unused.name}),))
    assert not models.remove_unused(managed.component, managed.data, consent=True)['removed']
    with sqlite3.connect(managed.data / 'forge.sqlite3') as database:
        database.execute('DELETE FROM dubbing_projects')
    assert models.remove_unused(managed.component, managed.data, consent=True)['removed'] == ['2' * 40]
    assert current['root'].exists() and (managed.data / 'models/speech/ming/preserve.txt').exists()


def test_load_only_test_never_sets_inference_verified(managed, monkeypatch):
    install(managed); current = models.resolve(managed.component, managed.data)
    monkeypatch.setattr(models, 'invoke', lambda *args, **kwargs: {'success': True, 'inference_tested': False,
        'model_revision': current['model_revision'], 'runtime_revision': current['runtime_revision'], 'elapsed_seconds': .01})
    with pytest.raises(models.ComponentUnavailable, match='load-only'):
        models.test(managed.component, managed.data, request={'ref_audio': str(managed.blob), 'consent': True})
    assert not models.catalog(managed.data)[0]['inference_verified']


def test_inference_receipt_does_not_transfer_to_repaired_runtime(managed, monkeypatch):
    install(managed)
    def fixture_inference(*args, **kwargs):
        current = models.resolve(managed.component, managed.data)
        return {'success': True, 'inference_tested': True, 'model_revision': current['model_revision'],
                'runtime_revision': current['runtime_revision'], 'actual_device': 'cpu', 'elapsed_seconds': .01}
    monkeypatch.setattr(models, 'invoke', fixture_inference)
    models.test(managed.component, managed.data, request={'ref_audio': str(managed.blob), 'consent': True, 'device': 'auto'})
    assert models.catalog(managed.data)[0]['inference_verified']
    install(managed, action='repair')
    assert not models.catalog(managed.data)[0]['inference_verified']


def test_cache_presence_alone_does_not_make_existing_whisper_ready(tmp_path, monkeypatch):
    from forge import model_cache
    versions = {'faster-whisper': '1.2.1', 'ctranslate2': '4.6.0', 'av': '19.0.0', 'setuptools': '80.9.0'}
    monkeypatch.setattr(models.importlib.metadata, 'version', lambda name: versions[name])
    monkeypatch.setattr(models.importlib.util, 'find_spec', lambda name: object())
    monkeypatch.setattr(model_cache, 'inventory', lambda data: [{'model': 'large-v3-turbo', 'cached': True, 'selected': '1' * 40}])
    value = models.existing_whisper_state(tmp_path)
    assert value['model_cached'] and not value['dependencies_ready'] and not value['ready']


def test_guided_python_extract_rejects_escape_and_links(tmp_path):
    for unsafe, kind in [('python/../../escape.txt', tarfile.REGTYPE), ('python/linked', tarfile.SYMTYPE)]:
        archive = tmp_path / ('archive-' + str(int(kind[0])) + '.tgz')
        with tarfile.open(archive, 'w:gz') as output:
            item = tarfile.TarInfo(unsafe); item.type = kind; item.size = 1 if kind == tarfile.REGTYPE else 0
            item.linkname = '/outside'
            output.addfile(item, io.BytesIO(b'x') if item.size else None)
        with pytest.raises(ValueError, match='unsafe'):
            models._extract_runtime(archive, tmp_path / 'runtime')
    assert not (tmp_path / 'escape.txt').exists()


def test_reviewed_source_profile_rejects_native_or_linked_members(tmp_path, monkeypatch):
    for name, mode in [('qwen_tts/evil.pyd', 0o100644), ('qwen_tts/link.py', 0o120777)]:
        wheel = tmp_path / ('source-' + str(mode) + '.whl')
        with zipfile.ZipFile(wheel, 'w') as archive:
            item = zipfile.ZipInfo(name); item.external_attr = mode << 16; archive.writestr(item, b'owned fixture')
        monkeypatch.setattr(models, '_download', lambda *args: wheel)
        with pytest.raises(ValueError):
            models._install_sources([{'kind': 'wheel_source', 'package_prefix': 'qwen_tts/', 'name': 'fixture'}], tmp_path / 'upstream', tmp_path / 'data', None)


@pytest.fixture
def media_http():
    payload = (b'Owned resumable component fixture\n' * 40000)
    requests = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            header = self.headers.get('Range'); requests.append((self.path, header))
            offset = int(header.removeprefix('bytes=').split('-', 1)[0]) if header and self.path != '/ignore' else 0
            self.send_response(206 if offset else 200)
            self.send_header('Content-Length', str(len(payload) - offset))
            if offset:
                self.send_header('Content-Range', f'bytes {offset}-{len(payload)-1}/{len(payload)}')
            self.end_headers()
            try:
                self.wfile.write(payload[offset:])
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield payload, requests, server.server_port
    server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.mark.parametrize('path', ['/range', '/ignore'])
def test_real_component_download_resumes_or_restarts_safely(tmp_path, monkeypatch, media_http, path):
    payload, requests, port = media_http
    monkeypatch.setattr(models, '_open', lambda url, headers=None, **kwargs:
        urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}' + path, headers=headers or {}), timeout=5))
    artifact = {'url': 'https://download.pytorch.org/owned-fixture', 'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    key = hashlib.sha256(('sha256' + str(len(payload)) + artifact['sha256']).encode()).hexdigest()
    partial = tmp_path / (key + '.part'); partial.write_bytes(payload[:12345])
    events = []
    result = models._download(artifact, tmp_path, lambda value, message, **metadata: events.append(metadata))
    assert result.read_bytes() == payload and requests[0][1] == 'bytes=12345-'
    assert events and all(event['transferred_bytes'] <= len(payload) and event['progress_measured'] for event in events)
    assert models._download(artifact, tmp_path).read_bytes() == payload and len(requests) == 1


def test_concurrent_component_downloads_share_one_verified_transfer(tmp_path, monkeypatch, media_http):
    from concurrent.futures import ThreadPoolExecutor
    payload, requests, port = media_http
    opened = threading.Event(); release = threading.Event(); barrier = threading.Barrier(2)
    def open_fixture(url, headers=None, **kwargs):
        opened.set(); assert release.wait(3)
        return urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}/range', headers=headers or {}), timeout=5)
    monkeypatch.setattr(models, '_open', open_fixture)
    artifact = {'url': 'https://download.pytorch.org/owned-fixture', 'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    def transfer():
        barrier.wait(timeout=3)
        return models._download(artifact, tmp_path)
    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(transfer); second = executor.submit(transfer)
        try:
            assert opened.wait(3)
            time.sleep(.05)
        finally:
            release.set()
        left, right = first.result(timeout=4), second.result(timeout=4)
    assert left == right and left.read_bytes() == payload and len(requests) == 1


def test_invoke_drains_large_stderr_without_deadlock(tmp_path, monkeypatch):
    app = tmp_path / 'app'; scripts = app / 'scripts'; scripts.mkdir(parents=True)
    runner = scripts / 'fixture.py'
    runner.write_text('import sys,json,pathlib\nr=json.loads(pathlib.Path(sys.argv[2]).read_text())\nsys.stderr.write("owned warning "*150000);sys.stderr.flush()\nprint(json.dumps({"kind":"progress","progress":20,"message":"Owned fixture"}),flush=True)\npathlib.Path(r["response"]).write_text(json.dumps({"success":True,"model_revision":r["model_revision"],"inference_tested":False}))\n')
    monkeypatch.setattr(models, 'ROOT', app)
    monkeypatch.setattr(models, '_spec', lambda component: {'runner': 'scripts/fixture.py'})
    monkeypatch.setattr(models, 'resolve', lambda *args: {'python': Path(sys.executable), 'root': tmp_path,
        'model_revision': '1' * 40, 'runtime_revision': 'dubbing-fixture-' + '2' * 16,
        'device': 'cpu', 'paths': {}, 'environment': tmp_path})
    started = time.monotonic(); events = []
    result = models.invoke('fixture', tmp_path / 'data', {'task': 'owned-fixture'}, progress=lambda *args, **kwargs: events.append(args))
    assert result['success'] and events and time.monotonic() - started < 8
    assert not list((tmp_path / 'data/dubbing/requests').glob('*.json'))


def test_hung_inference_has_bounded_deadline_before_stdout_eof(tmp_path, monkeypatch):
    app = tmp_path / 'app'; scripts = app / 'scripts'; scripts.mkdir(parents=True)
    (scripts / 'fixture.py').write_text('import time\ntime.sleep(60)\n')
    monkeypatch.setattr(models, 'ROOT', app)
    monkeypatch.setattr(models, 'INFERENCE_TIMEOUT_SECONDS', .3)
    monkeypatch.setattr(models, '_spec', lambda component: {'runner': 'scripts/fixture.py'})
    monkeypatch.setattr(models, 'resolve', lambda *args: {'python': Path(sys.executable), 'root': tmp_path,
        'model_revision': '1' * 40, 'runtime_revision': 'dubbing-fixture-' + '2' * 16,
        'device': 'cpu', 'paths': {}, 'environment': tmp_path})
    started = time.monotonic()
    with pytest.raises(models.ComponentUnavailable, match='bounded inference'):
        models.invoke('fixture', tmp_path / 'data', {'task': 'owned-fixture'})
    assert time.monotonic() - started < 4


def test_native_base_test_needs_permitted_reference(managed):
    with pytest.raises(ValueError, match='permitted voice reference'):
        models.test(managed.component, managed.data)


def test_glossary_numbers_and_dates_never_enter_translation_generator():
    import importlib.util
    spec = importlib.util.spec_from_file_location('owned_dubbing_runtime', models.ROOT / 'scripts/dubbing_runtime.py')
    runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(runtime)
    pieces = list(runtime.protected_pieces('Birthright reviewed 12.5% on 2026-11-07 [3].', {'Birthright': 'Бёртрайт'}))
    protected = [value for is_protected, value in pieces if is_protected]
    assert protected == ['Бёртрайт', '12.5%', '2026-11-07', '[3]']
    assert all('Birthright' not in value and '2026-11-07' not in value for key, value in pieces if not key)


def _runtime_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location('owned_dubbing_runtime', models.ROOT / 'scripts/dubbing_runtime.py')
    runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(runtime)
    return runtime


@pytest.mark.parametrize('collision', [False, True])
def test_segment_publication_is_atomic_and_never_replaces_a_late_existing_file(tmp_path, monkeypatch, collision):
    from concurrent.futures import ThreadPoolExecutor
    import numpy as np
    runtime = _runtime_module()
    started = threading.Event(); release = threading.Event(); target = tmp_path / 'completed.wav'
    def write(path, samples, rate, **kwargs):
        # Explicit synthetic codec boundary; filesystem finality is real.
        Path(path).write_bytes(b'partial-owned-fixture')
        started.set()
        assert release.wait(3)
        Path(path).write_bytes(b'complete-owned-fixture')
    monkeypatch.setitem(sys.modules, 'soundfile', SimpleNamespace(write=write,
        info=lambda path: SimpleNamespace(frames=2400, samplerate=24000)))
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(runtime._audio_output, np.ones(2400, dtype=np.float32) * .1, 24000, str(target))
        try:
            assert started.wait(3)
            assert not target.exists(), 'An incomplete voice segment became visible to the production cache.'
            if collision:
                target.write_bytes(b'Original owner file created while inference was running')
        finally:
            release.set()
        if collision:
            with pytest.raises(FileExistsError):
                future.result(timeout=3)
            assert target.read_bytes() == b'Original owner file created while inference was running'
        else:
            result = future.result(timeout=3)
            assert result['duration'] == .1 and target.read_bytes() == b'complete-owned-fixture'
    assert not list(tmp_path.glob('*.partial*')) and not list(tmp_path.glob('.*.partial*'))


def test_failed_codec_never_publishes_partial_segment(tmp_path, monkeypatch):
    import numpy as np
    runtime = _runtime_module(); target = tmp_path / 'completed.wav'
    def fail(path, *args, **kwargs):
        Path(path).write_bytes(b'Owned partial codec output')
        raise OSError('Owned codec interruption')
    monkeypatch.setitem(sys.modules, 'soundfile', SimpleNamespace(write=fail))
    with pytest.raises(OSError, match='codec interruption'):
        runtime._audio_output(np.ones(2400, dtype=np.float32), 24000, str(target))
    assert not target.exists() and not list(tmp_path.iterdir())


def test_component_parent_link_cannot_redirect_cleanup_or_install(managed, tmp_path):
    storage = models._roots(managed.data)[0]; storage.mkdir(parents=True)
    outside = tmp_path / 'owner-backup'; outside.mkdir()
    marker = outside / 'keep.txt'; marker.write_bytes(b'Original backup data')
    try:
        (storage / managed.component).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('This platform does not grant directory-link creation for the fixture.')
    with pytest.raises(models.ComponentUnavailable, match='links outside'):
        models.installation_plan([managed.component], managed.data, device='cpu')
    with pytest.raises(models.ComponentUnavailable, match='links outside'):
        models.remove_unused(managed.component, managed.data, consent=True)
    assert marker.read_bytes() == b'Original backup data'


def test_frozen_native_dependency_inventory_is_used_without_network(tmp_path, monkeypatch):
    spec = models._spec('qwen3-tts-0.6b-base')
    source = models.ROOT / 'forge/dubbing_wheel_locks/requirements-dubbing-core-linux-x64-py312-cpu.json'
    value = json.loads(source.read_text())
    lock = tmp_path / 'native.json'; json_write(lock, value)
    monkeypatch.setattr(models, '_target_platform', lambda: ('linux', 'x64'))
    monkeypatch.setattr(models, '_wheel_lock_path', lambda *args: lock)
    monkeypatch.setattr(models, '_run', lambda *args, **kwargs: pytest.fail('A frozen native inventory must not resolve again.'))
    plan = models._dependency_plan(spec, {'version': '3.12.16'}, 'cpu', tmp_path)
    assert len(plan['wheels']) == 59 and plan['download_bytes'] == 445785274
    value['requirements_sha256'] = '0' * 64; json_write(lock, value)
    with pytest.raises(models.ComponentUnavailable, match='does not match'):
        models._dependency_plan(spec, {'version': '3.12.16'}, 'cpu', tmp_path)


def test_windows_lock_requires_native_platform_evidence(tmp_path, monkeypatch):
    spec = models._spec('qwen3-tts-0.6b-base')
    source = models.ROOT / 'forge/dubbing_wheel_locks/requirements-dubbing-core-linux-x64-py312-cpu.json'
    value = json.loads(source.read_text()); value['system'] = 'windows'
    lock = tmp_path / 'not-native.json'; json_write(lock, value)
    monkeypatch.setattr(models, '_target_platform', lambda: ('windows', 'x64'))
    monkeypatch.setattr(models, '_wheel_lock_path', lambda *args: lock)
    with pytest.raises(models.ComponentUnavailable, match='does not match'):
        models._load_wheel_lock(spec, {'version': '3.12.16'}, 'cpu')


def test_cross_python_resolution_never_guesses_environment_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(models, '_load_wheel_lock', lambda *args: None)
    monkeypatch.setattr(models, '_run', lambda *args, **kwargs: pytest.fail('Cross-Python pip cannot prove native dependency markers.'))
    incompatible = '3.11.0' if sys.version_info[:2] != (3, 11) else '3.12.0'
    with pytest.raises(models.ComponentUnavailable, match='target-Python'):
        models._dependency_plan(models._spec('qwen3-tts-0.6b-base'), {'version': incompatible}, 'cpu', tmp_path)


def test_same_runtime_concurrent_installs_recheck_after_cache_lock(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    runtime_id = 'dubbing-core-' + 'a' * 16
    plan = {'runtime_id': runtime_id, 'dependencies': {'requirements_sha256': 'b' * 64, 'wheels': []},
            'sources': [], 'device': 'cpu', 'smoke': ''}
    bootstrapped = []; started = threading.Event(); release = threading.Event()
    def bootstrap(*args, **kwargs):
        bootstrapped.append(True); started.set(); assert release.wait(3)
        return Path(sys.executable)
    def execute(args, **kwargs):
        if '-m' in args and 'venv' in args:
            interpreter = models._python(Path(args[-1])); interpreter.parent.mkdir(parents=True)
            interpreter.write_text('Owned interpreter fixture; not executed.')
        return ''
    monkeypatch.setattr(models, '_bootstrap_python', bootstrap)
    monkeypatch.setattr(models, '_run', execute)
    monkeypatch.setattr(models, '_install_sources', lambda *args: [])
    monkeypatch.setattr(models, '_runtime_probe', lambda *args: None)
    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(models._environment, tmp_path, plan, {'version': '3.12.16'})
        assert started.wait(3)
        second = executor.submit(models._environment, tmp_path, plan, {'version': '3.12.16'})
        try:
            time.sleep(.05)
        finally:
            release.set()
        assert first.result(timeout=4) == second.result(timeout=4)
    assert len(bootstrapped) == 1


def test_archived_api_patch_reuses_identical_upstream_defaults_and_fails_closed(tmp_path):
    package = tmp_path / 'chatterbox'; flow = package / 'models/s3gen/flow.py'; flow.parent.mkdir(parents=True)
    (package / '__init__.py').write_text('from importlib.metadata import version\n__version__ = version("chatterbox-tts")\n')
    defaults = "DictConfig(\n                                           {'sigma_min': 1e-06, 'solver': 'euler', 't_scheduler': 'cosine',\n                                            'training_cfg_rate': 0.2, 'inference_cfg_rate': 0.7,\n                                            'reg_loss_type': 'l1'})"
    flow.write_text('from omegaconf import DictConfig\nfrom .flow_matching import CFM_PARAMS\ndef api(cfm_params=' + defaults + '): pass\n')
    assert models._patch_chatterbox_api(tmp_path) == {'chatterbox/__init__.py', 'chatterbox/models/s3gen/flow.py'}
    assert 'def api(cfm_params=CFM_PARAMS)' in flow.read_text() and 'from omegaconf' not in flow.read_text()
    assert '__version__ = "0.1.7+forge.ru.api1"' in (package / '__init__.py').read_text()
    with pytest.raises(models.ComponentUnavailable, match='differs'):
        models._patch_chatterbox_api(tmp_path)


def test_guided_bootstrap_repairs_only_owned_runtime_and_retains_corrupt_copy(tmp_path, monkeypatch):
    artifact = {**models.MANAGED_PYTHON, 'python_exe_sha256': hashlib.sha256(b'Owned synthetic interpreter').hexdigest()}
    monkeypatch.setattr(models, 'MANAGED_PYTHON', artifact)
    monkeypatch.setattr(models, '_download', lambda *args: tmp_path / 'owned-artifact')
    monkeypatch.setattr(models, '_extract_runtime', lambda archive, directory: (directory / 'python.exe').write_bytes(b'Owned synthetic interpreter'))
    monkeypatch.setattr(models, '_run', lambda *args, **kwargs: artifact['version'])
    runtime = {'kind': 'managed', **artifact, 'download_bytes': artifact['size']}
    original = tmp_path / 'environments/whisper/keep.txt'; original.parent.mkdir(parents=True)
    original.write_bytes(b'Original installed environment')
    python = models._bootstrap_python(runtime, tmp_path)
    python.write_bytes(b'Owned corrupt bootstrap fixture')
    assert models._bootstrap_python(runtime, tmp_path).read_bytes() == b'Owned synthetic interpreter'
    retained = list((tmp_path / 'runtimes').glob('.retained-*/python.exe'))
    assert len(retained) == 1 and retained[0].read_bytes() == b'Owned corrupt bootstrap fixture'
    assert original.read_bytes() == b'Original installed environment'


@pytest.fixture
def publisher_wheel(monkeypatch):
    # Public publisher identity verified separately from pip's missing cache hash.
    name = 'jinja2-3.1.6-py3-none-any.whl'
    url = 'https://files.pythonhosted.org/packages/62/a1/3d680cbfd5f4b8f15abc1d571870c5fc3e594bb582bc3b64ea099db13e56/' + name
    sha = '85ece4451f492d0c13c5dd7c13a64681a86afae63a5f347908daf103ce6d2f67'
    value = {'info': {'name': 'Jinja2', 'version': '3.1.6'},
             'urls': [{'filename': name, 'url': url, 'packagetype': 'bdist_wheel', 'digests': {'sha256': sha}, 'size': 134899}]}
    def metadata(request):
        assert request == 'https://pypi.org/pypi/Jinja2/3.1.6/json'
        return copy.deepcopy(value)
    monkeypatch.setattr(models, '_fetch_json', metadata)
    return SimpleNamespace(name=name, url=url, sha=sha, value=value)


def test_missing_native_pip_hash_is_filled_only_from_exact_publisher_file(tmp_path, monkeypatch, publisher_wheel):
    artifact = publisher_wheel
    report = {'install': [{'download_info': {'url': artifact.url, 'archive_info': {}},
                           'metadata': {'name': 'Jinja2', 'version': '3.1.6', 'license_expression': 'BSD-3-Clause'}}]}
    commands = []
    def resolver(args, **kwargs):
        commands.append(args)
        json_write(Path(args[args.index('--report') + 1]), report)
    monkeypatch.setattr(models, '_run', resolver)
    monkeypatch.setattr(models, '_torch_wheel_requirement', lambda package, version, device: {'requirement': package + '==2.8.0+cpu', 'html': '<a>Owned resolver fixture</a>'})
    monkeypatch.setattr(models, '_head_size', lambda *args: pytest.fail('The exact publisher file already provides its size.'))
    result = models._dependency_plan(models._spec('qwen3-tts-0.6b-base'),
                                    {'version': '.'.join(map(str, sys.version_info[:3]))}, 'cpu', tmp_path, use_lock=False)
    assert result['download_bytes'] == 134899 and result['wheels'][0]['sha256'] == artifact.sha
    assert '--no-cache-dir' in commands[0]
    assert '--extra-index-url' not in commands[0] and commands[0][commands[0].index('--index-url') + 1] == 'https://pypi.org/simple'


@pytest.mark.parametrize('conflict', ['url', 'filename', 'version', 'package', 'hash', 'fragment', 'missing_hash', 'size', 'duplicate'])
def test_publisher_identity_conflicts_fail_closed(publisher_wheel, conflict):
    artifact = publisher_wheel; url = artifact.url; name = artifact.name; reported_sha = ''
    if conflict == 'url': url = url.replace('/packages/62/', '/packages/63/')
    elif conflict == 'filename': name = 'unrelated-3.1.6-py3-none-any.whl'
    elif conflict == 'version': artifact.value['info']['version'] = '3.1.5'
    elif conflict == 'package': artifact.value['info']['name'] = 'unrelated-package'
    elif conflict == 'hash': reported_sha = '0' * 64
    elif conflict == 'fragment': url += '#sha256=' + '0' * 64
    elif conflict == 'missing_hash': artifact.value['urls'][0]['digests'] = {}
    elif conflict == 'size': artifact.value['urls'][0]['size'] = 0
    elif conflict == 'duplicate': artifact.value['urls'].append(copy.deepcopy(artifact.value['urls'][0]))
    with pytest.raises(models.ComponentUnavailable):
        models._pypi_wheel_identity('Jinja2', '3.1.6', url, name, reported_sha=reported_sha)


@pytest.mark.parametrize('invalid', [None, 'missing_hash', 'wrong_host', 'wrong_python', 'wrong_version', 'missing_metadata'])
def test_torch_native_selection_uses_only_hashed_matching_publisher_wheel(monkeypatch, invalid):
    from pip._vendor.packaging import tags
    monkeypatch.setattr(tags, 'sys_tags', lambda: iter([tags.Tag('cp312', 'cp312', 'win_amd64')]))
    suffix = '#sha256=' + 'a' * 64
    url = 'https://download-r2.pytorch.org/whl/cpu/torch-2.8.0%2Bcpu-cp312-cp312-win_amd64.whl'
    if invalid == 'missing_hash': suffix = ''
    elif invalid == 'wrong_host': url = url.replace('download-r2.pytorch.org', 'unapproved.invalid')
    elif invalid == 'wrong_python': url = url.replace('cp312', 'cp311')
    elif invalid == 'wrong_version': url = url.replace('2.8.0', '2.7.0')
    metadata = '' if invalid == 'missing_metadata' else ' data-dist-info-metadata="sha256=' + 'b' * 64 + '"'
    raw = ('<a href="' + url + suffix + '"' + metadata + '>torch</a>'
           '<a href="/whl/jinja2-3.1.6-py3-none-any.whl">unhashed unrelated mirror</a>').encode()
    monkeypatch.setattr(models, '_open', lambda *args, **kwargs: io.BytesIO(raw))
    if invalid:
        with pytest.raises(models.ComponentUnavailable, match='no hashed compatible'):
            models._torch_wheel_requirement('torch', '2.8.0', 'cpu')
    else:
        result = models._torch_wheel_requirement('torch', '2.8.0', 'cpu')
        assert result['requirement'] == 'torch==2.8.0+cpu' and url + suffix in result['html']
        assert 'data-dist-info-metadata="sha256=' + 'b' * 64 + '"' in result['html']
