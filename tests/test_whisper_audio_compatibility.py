"""Real Whisper decoder compatibility and read-only dependency-repair discovery.

The actual decoder case runs in the optional pinned transcription environment;
no model weights or model inference are required for this audio regression.
"""
import hashlib
import io
import math
from pathlib import Path
import struct
from types import SimpleNamespace
import wave

import pytest


def test_actual_whisper_decoder_reads_resamples_and_splits_real_stereo_wav(tmp_path):
    audio = pytest.importorskip('faster_whisper.audio', reason='Run in the optional pinned transcription environment')
    import numpy as np
    source = tmp_path / 'real-decoder-input.wav'
    rate, frames = 22050, 11025
    samples = b''.join(struct.pack('<hh', round(10000 * math.sin(2 * math.pi * 440 * n / rate)),
                                  round(8000 * math.sin(2 * math.pi * 660 * n / rate))) for n in range(frames))
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((2, 2, rate, frames, 'NONE', 'not compressed'))
        stream.writeframes(samples)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    # This production call failed on PyAV 19: av.open removed metadata_errors.
    mono = audio.decode_audio(str(source), sampling_rate=16000)
    left, right = audio.decode_audio(str(source), sampling_rate=16000, split_stereo=True)
    buffered = audio.decode_audio(io.BytesIO(source.read_bytes()), sampling_rate=16000)
    assert mono.dtype == np.float32 and np.isfinite(mono).all()
    assert abs(len(mono) - 8000) <= 1 and len(left) == len(right) == len(mono)
    assert float(np.sqrt(np.mean(mono ** 2))) > .05
    assert not np.allclose(left, right) and np.allclose(mono, buffered)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original


@pytest.mark.parametrize('installed_av,expected_compatible', [('16.0.0', False), ('19.0.2', False), ('18.1.0', True)])
def test_installed_pyav_versions_offer_controlled_repair_without_mutating_environment(tmp_path, monkeypatch, installed_av, expected_compatible):
    from forge import dependencies
    root = Path(__file__).resolve().parents[1]
    requirements = (root / 'requirements-transcription.txt').read_text(encoding='utf-8')
    (tmp_path / 'requirements-transcription.txt').write_text(requirements, encoding='utf-8')
    original_environment = tmp_path / '.venv' / 'owner-preserved.txt'
    original_environment.parent.mkdir()
    original_environment.write_bytes(b'Existing owner environment must not be modified by discovery.')
    before = original_environment.read_bytes()
    versions = dict(line.strip().split('==', 1) for line in requirements.splitlines()
                    if line.strip() and not line.lstrip().startswith('#'))
    versions['av'] = installed_av
    monkeypatch.setattr(dependencies, 'ENGINES', {'transcription': dependencies.ENGINES['transcription']})
    monkeypatch.setattr(dependencies.importlib.metadata, 'version', lambda name: versions[name])
    monkeypatch.setattr(dependencies.importlib.util, 'find_spec', lambda name: object())
    monkeypatch.setattr(dependencies, 'executable', lambda *args: None)
    commands = []
    def health(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(dependencies.subprocess, 'run', health)
    entry = next(item for item in dependencies.inspect({}, root=tmp_path) if item['id'] == 'transcription')
    assert entry['compatible'] is expected_compatible
    assert entry['repairable'] is (not expected_compatible)
    assert entry['action'] == ('' if expected_compatible else 'repair')
    assert any(package['package'] == 'av' and package['version'] == installed_av for package in entry['packages'])
    assert original_environment.read_bytes() == before
    if expected_compatible:
        assert len(commands) == 1 and 'import ' in commands[0][-1] and 'av' in commands[0][-1]
    else:
        assert commands == []
