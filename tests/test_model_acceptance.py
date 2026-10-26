"""Acceptance-harness guard and signal checks; these do not run neural inference."""
from array import array
import importlib.util
import math
import os
from pathlib import Path
import sys
import wave

import pytest

spec = importlib.util.spec_from_file_location('tts_model_acceptance_harness', Path(__file__).with_name('tts_model_acceptance.py'))
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def pcm(path, *, silent=False, clipped=False, width=2, duration=.5):
    rate = 24000
    samples = array('h', [32767 if clipped else 0 if silent else round(7000 * math.sin(2 * math.pi * 220 * index / rate))
                         for index in range(round(rate * duration))])
    if sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, width, rate, 0, 'NONE', 'uncompressed'))
        output.writeframes(samples.tobytes())
    return path


def test_actual_pcm_metrics_measure_duration_and_levels(tmp_path):
    path = pcm(tmp_path / 'tone.wav')
    metrics = harness.audio_metrics(path)
    assert metrics['duration_seconds'] == .5
    assert metrics['sample_rate'] == 24000
    assert metrics['peak'] == pytest.approx(7000 / 32768, abs=.0001)
    assert metrics['rms_dbfs'] == pytest.approx(-16.42, abs=.1)
    assert metrics['sha256'] == harness.digest(path)


@pytest.mark.parametrize('kwargs,message', [({'silent': True}, 'silent'), ({'clipped': True}, 'clipped'),
                                         ({'duration': .1}, 'duration')])
def test_signal_verifier_rejects_unusable_generated_output(tmp_path, kwargs, message):
    with pytest.raises(AssertionError, match=message):
        harness.audio_metrics(pcm(tmp_path / 'unusable.wav', **kwargs))


def test_model_downloads_require_explicit_opt_in_before_creating_data(tmp_path):
    data, evidence = tmp_path / 'models', tmp_path / 'evidence'
    with pytest.raises(SystemExit) as error:
        harness.main(['--engine', 'kokoro', '--data', str(data), '--output', str(evidence)])
    assert error.value.code == 2 and not data.exists() and not evidence.exists()


def test_existing_installation_is_never_reused(tmp_path):
    data, evidence = tmp_path / 'models', tmp_path / 'evidence'
    data.mkdir()
    sentinel = data / 'owner-model.txt'
    sentinel.write_text('Preserve existing models.')
    with pytest.raises(SystemExit) as error:
        harness.main(['--engine', 'kokoro', '--data', str(data), '--output', str(evidence), '--allow-model-downloads'])
    assert error.value.code == 2 and sentinel.read_text() == 'Preserve existing models.'
    assert not evidence.exists()


def test_offline_proxy_test_restores_previous_environment(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'https://original-proxy.invalid')
    monkeypatch.delenv('HF_HUB_OFFLINE', raising=False)
    with harness.offline_environment():
        assert os.environ['HTTPS_PROXY'] == 'http://127.0.0.1:9'
        assert os.environ['https_proxy'] == 'http://127.0.0.1:9'
        assert os.environ['HF_HUB_OFFLINE'] == '1'
    assert os.environ['HTTPS_PROXY'] == 'https://original-proxy.invalid'
    assert 'HF_HUB_OFFLINE' not in os.environ


def test_diagnostics_redact_private_paths_tokens_and_url_credentials():
    text = '/private/test-models https://user:secret@example.test/?token=privatevalue&x=one hf_' + 'a' * 30
    result = harness.redact(text, ['/private/test-models'])
    for secret in ['test-models', 'secret', 'privatevalue', 'hf_' + 'a' * 30]:
        assert secret not in result
    assert '<acceptance-path>' in result and '<redacted>' in result
