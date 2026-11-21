"""Model acceptance trust contracts. No synthetic fixture is neural/GPU proof."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import math
import os
import shutil
import struct
import wave

import pytest

from scripts import dubbing_cpu_acceptance as acceptance


def packaged_source(tmp_path, *, commit='a' * 40):
    root = tmp_path / 'application'; root.mkdir()
    files = {}
    for name in (*acceptance.SOURCE_FILES, 'forge/owned-extra.py'):
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'# Owned source binding fixture, never executed.\n')
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {'app': 'FORGE', 'version': '0.10.0', 'format': 1,
                'package': {'source_commit': commit, 'source_manifest_sha256': 'b' * 64}, 'files': files}
    (root / 'release-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return root


def test_packaged_identity_uses_real_commit_and_binds_adapter_and_extra_bytes(tmp_path):
    root = packaged_source(tmp_path)
    value = acceptance.source_identity(root, additional_files=('forge/owned-extra.py',))
    assert value['source_commit'] == 'a' * 40 and value['source_kind'] == 'installed_manifest'
    assert value['source_manifest_sha256'] == 'b' * 64
    assert value['installed_manifest_sha256'] == acceptance.digest(root / 'release-manifest.json')
    assert 'forge/owned-extra.py' in value['source_files_sha256']
    (root / 'forge/owned-extra.py').write_bytes(b'Changed source; reject stale acceptance.')
    with pytest.raises(acceptance.AcceptanceBlocked, match='adapter bytes'):
        acceptance.source_identity(root, additional_files=('forge/owned-extra.py',))


@pytest.mark.parametrize('commit', ['', 'v0.10.0', '0.10.0', None, 'a' * 39])
def test_app_version_or_missing_metadata_never_substitutes_for_source_commit(tmp_path, commit):
    root = packaged_source(tmp_path, commit=commit)
    with pytest.raises(acceptance.AcceptanceBlocked, match='exact source commit'):
        acceptance.source_identity(root)


def test_source_identity_rejects_extra_path_escape(tmp_path):
    root = packaged_source(tmp_path)
    with pytest.raises(acceptance.AcceptanceBlocked, match='application-relative'):
        acceptance.source_identity(root, additional_files=('../outside.py',))


def test_cpu_and_gpu_claims_require_matching_actual_inference_metrics():
    base = {'inference_tested': True, 'actual_device': 'cpu', 'physical_gpu_tested': False,
            'model_revision': 'a' * 40, 'runtime_revision': 'owned-runtime', 'elapsed_seconds': 1.0}
    assert acceptance._metrics(base, 'cpu')['physical_gpu_tested'] is False
    for changed in [{'inference_tested': False}, {'actual_device': 'cuda'}, {'physical_gpu_tested': True}]:
        with pytest.raises(acceptance.AcceptanceBlocked):
            acceptance._metrics({**base, **changed}, 'cpu')
    gpu = {**base, 'actual_device': 'cuda', 'physical_gpu_tested': True}
    with pytest.raises(acceptance.AcceptanceBlocked, match='incomplete'):
        acceptance._metrics(gpu, 'cuda')
    gpu.update(gpu_name='Owned schema fixture; not hardware', gpu_capability=[12, 0],
               peak_vram_bytes=4096, peak_reserved_vram_bytes=8192)
    assert acceptance._metrics(gpu, 'cuda')['peak_vram_bytes'] == 4096
    with pytest.raises(acceptance.AcceptanceBlocked, match='positive actual'):
        acceptance._metrics({**gpu, 'peak_vram_bytes': 0}, 'cuda')


@pytest.mark.skipif(os.name == 'nt', reason='Linux must not impersonate native Windows.')
def test_native_requirement_fails_before_any_install_or_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance.models, 'installation_plan', lambda *a, **k: pytest.fail('No installation from a wrong native platform.'))
    result = acceptance.run_acceptance(tmp_path / 'data', tmp_path / 'evidence', approve_install=True, require_native_windows=True)
    assert result['status'] == 'blocked' and result['native_windows_execution'] is False
    assert result['physical_gpu_tested'] is False and result['satisfies_release_acceptance'] is False
    assert 'native Windows' in result['error']
    assert json.loads((tmp_path / 'evidence' / acceptance.REPORT_NAME).read_text()) == result


def test_missing_models_never_trigger_unapproved_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, 'source_identity', lambda **kwargs: {'source_commit': 'a' * 40})
    monkeypatch.setattr(acceptance.shutil, 'which', lambda _: 'owned-media-tool')
    monkeypatch.setattr(acceptance.models, 'resolve', lambda *a, **k: (_ for _ in ()).throw(acceptance.models.ComponentUnavailable('Install the exact approved pack first.')))
    monkeypatch.setattr(acceptance.models, 'installation_plan', lambda *a, **k: pytest.fail('No implicit installation consent.'))
    result = acceptance.run_acceptance(tmp_path / 'data', tmp_path / 'evidence')
    assert result['status'] == 'blocked' and result['components'] == {}
    assert result['controller_environment_unchanged'] is True


def test_frozen_source_rejection_happens_before_model_access(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, 'source_identity', lambda **kwargs: {'source_commit': 'b' * 40})
    monkeypatch.setattr(acceptance.shutil, 'which', lambda _: 'owned-media-tool')
    monkeypatch.setattr(acceptance, 'installed_identities', lambda *a, **k: pytest.fail('Do not read models after a changed source.'))
    result = acceptance.run_acceptance(tmp_path / 'data', tmp_path / 'evidence', expected_source={'source_commit': 'a' * 40})
    assert result['status'] == 'blocked' and 'source changed' in result['error']


def test_device_mismatch_does_not_become_a_cached_gpu_success(monkeypatch):
    monkeypatch.setattr(acceptance.models, 'resolve', lambda *a: {'device': 'cpu', 'model_revision': 'a' * 40, 'runtime_revision': 'owned-cpu-runtime'})
    with pytest.raises(acceptance.AcceptanceBlocked, match='selected device profile'):
        acceptance.installed_identities(Path('/owned-generated-test-data'), 'cuda')


def test_existing_evidence_is_never_overwritten(tmp_path):
    output = tmp_path / 'evidence'; output.mkdir()
    sentinel = output / acceptance.REPORT_NAME; sentinel.write_text('Retain original evidence.')
    with pytest.raises(FileExistsError):
        acceptance.run_acceptance(tmp_path / 'data', output)
    assert sentinel.read_text() == 'Retain original evidence.'


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='Real FFmpeg decoding is required.')
def test_real_audio_decode_is_finite_nonsilent_and_preserves_input(tmp_path):
    target = tmp_path / 'owned-tone.wav'
    with wave.open(str(target), 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
        stream.writeframes(b''.join(struct.pack('<h', int(5000 * math.sin(2 * math.pi * 440 * i / 24000))) for i in range(24000)))
    before = target.read_bytes()
    actual = acceptance.decode_audio(target)
    assert actual['decoded_and_probed'] and actual['finite_samples'] and actual['duration'] == 1
    assert target.read_bytes() == before
    with wave.open(str(target), 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
        stream.writeframes(b'\0' * 48000)
    with pytest.raises(acceptance.AcceptanceBlocked, match='silent'):
        acceptance.decode_audio(target)
