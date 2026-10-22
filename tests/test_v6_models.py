"""Synthetic model artifacts exercise caching; these are not inference benchmarks."""
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge import model_cache as cache
from forge.timing import JobClock

REV = '1' * 40
FILES = {'config.json': b'{"model_type":"whisper"}', 'tokenizer.json': b'{}', 'model.bin': b'synthetic-model-test-fixture'}


def snapshot(data, model='tiny', revision=REV, files=FILES):
    path = data / 'models' / ('models--' + cache.REPOSITORIES[model].replace('/', '--')) / 'snapshots' / revision
    path.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        (path / name).write_bytes(value)
    return path


def publisher(monkeypatch, data, *, revision=REV, fail=False, bad_hash=False):
    calls = []
    siblings = [SimpleNamespace(rfilename=name, lfs={'sha256': ('0' * 64 if bad_hash else hashlib.sha256(value).hexdigest())}) for name, value in FILES.items()]
    class API:
        def __init__(self, **kwargs):
            assert kwargs.get('token') is False
        def model_info(self, repo, **kwargs):
            calls.append(('info', repo, kwargs))
            return SimpleNamespace(sha=revision, siblings=siblings)
    def download(repo, **kwargs):
        calls.append(('download', repo, kwargs))
        if fail:
            snapshot(data, revision=revision, files={'config.json': FILES['config.json']})
            raise OSError('synthetic interrupted transfer')
        return str(snapshot(data, revision=revision))
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(HfApi=API, snapshot_download=download))
    monkeypatch.setitem(sys.modules, 'tqdm.auto', SimpleNamespace(tqdm=object))
    monkeypatch.delenv('HF_HUB_OFFLINE', raising=False)
    return calls


def test_first_download_is_verified_and_reused_offline(tmp_path, monkeypatch):
    calls = publisher(monkeypatch, tmp_path)
    path = cache.ensure_model(tmp_path, 'tiny')
    assert cache.complete(path) and len(calls) == 2
    assert calls[1][2]['revision'] == REV
    receipt = json.loads((tmp_path / 'models/forge-receipts/tiny' / (REV + '.json')).read_text())
    assert set(receipt['files']) == set(FILES)
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    assert cache.ensure_model(tmp_path, 'tiny') == path and len(calls) == 2


def test_existing_model_is_adopted_without_download_or_migration(tmp_path, monkeypatch):
    path = snapshot(tmp_path)
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    assert cache.ensure_model(tmp_path, 'tiny') == path
    assert cache.inventory(tmp_path)[0]['selected'] == REV


def test_new_upstream_revision_does_not_replace_selected_model(tmp_path, monkeypatch):
    first = snapshot(tmp_path)
    cache.ensure_model(tmp_path, 'tiny', download=False)
    calls = publisher(monkeypatch, tmp_path, revision='2' * 40)
    assert cache.ensure_model(tmp_path, 'tiny') == first and not calls
    newer = cache.ensure_model(tmp_path, 'tiny', revision='2' * 40)
    assert newer != first and first.is_dir()
    cache.select_revision(tmp_path, 'tiny', REV)
    assert cache.ensure_model(tmp_path, 'tiny') == first


def test_interrupted_download_never_marks_model_installed(tmp_path, monkeypatch):
    publisher(monkeypatch, tmp_path, fail=True)
    with pytest.raises(RuntimeError, match='did not complete'):
        cache.ensure_model(tmp_path, 'tiny')
    assert not cache.inventory(tmp_path)[0]['cached']
    assert not (tmp_path / 'models/forge-selected.json').exists()
    publisher(monkeypatch, tmp_path)
    assert cache.complete(cache.ensure_model(tmp_path, 'tiny'))


def test_hash_mismatch_is_not_selected(tmp_path, monkeypatch):
    publisher(monkeypatch, tmp_path, bad_hash=True)
    with pytest.raises(RuntimeError, match='integrity'):
        cache.ensure_model(tmp_path, 'tiny')
    assert not (tmp_path / 'models/forge-selected.json').exists()
    assert not cache.inventory(tmp_path)[0]['cached']
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    with pytest.raises(RuntimeError, match='offline'):
        cache.ensure_model(tmp_path, 'tiny')


def test_changed_cache_fails_offline_instead_of_loading_corrupt_weights(tmp_path, monkeypatch):
    publisher(monkeypatch, tmp_path)
    path = cache.ensure_model(tmp_path, 'tiny')
    (path / 'model.bin').write_bytes(b'changed')
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    with pytest.raises(RuntimeError, match='offline'):
        cache.ensure_model(tmp_path, 'tiny')


def test_offline_message_lists_cached_fallbacks(tmp_path, monkeypatch):
    snapshot(tmp_path, model='base')
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    with pytest.raises(RuntimeError, match='Cached models: base'):
        cache.ensure_model(tmp_path, 'tiny')


def test_two_workers_share_single_download(tmp_path, monkeypatch):
    calls = publisher(monkeypatch, tmp_path)
    results, errors = [], []
    def work():
        try:
            results.append(cache.ensure_model(tmp_path, 'tiny'))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(5)
    assert not errors and len(results) == 2 and results[0] == results[1]
    assert len([c for c in calls if c[0] == 'download']) == 1


def test_cache_lock_releases_after_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with cache.cache_lock(tmp_path / 'lock'):
            raise RuntimeError('interrupted')
    with cache.cache_lock(tmp_path / 'lock', timeout=.1):
        pass


@pytest.mark.parametrize('model,revision', [('https://untrusted/model',''), ('tiny','main'), ('tiny','../../escape')])
def test_unapproved_model_or_revision_rejected(tmp_path, model, revision):
    with pytest.raises(ValueError): cache.ensure_model(tmp_path, model, revision=revision)


def test_execution_clock_excludes_queue_wait_and_measures_stages():
    current = [1000.0]
    clock = JobClock(lambda: current[0])
    current[0] += 2
    clock.enter('Download')
    current[0] += 10
    clock.enter('Conversion')
    current[0] += 4
    assert clock.snapshot() == {'execution_seconds': 16, 'stage_seconds': {'Starting': 2, 'Download': 10, 'Conversion': 4}}
