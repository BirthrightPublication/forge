"""Acceptance harness mechanics, without pretending a model was inferred."""
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import shutil
import struct
import sys
import wave

import pytest

spec = importlib.util.spec_from_file_location('asr_acceptance_harness', Path(__file__).with_name('asr_model_acceptance.py'))
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def test_wer_reports_raw_and_normalized_without_hiding_number_errors():
    values = harness.measured_wer('Hello, world! Twenty five.', 'hello world twenty five')
    assert values['normalized_words'] == 0 and values['case_punctuation_sensitive'] > 0
    assert harness.measured_wer('twenty five', '25')['normalized_words'] > 0
    assert harness.measured_wer('two three four', 'four three two')['normalized_words'] > 0


def test_receipts_never_log_tokens_signed_queries_or_owner_paths(tmp_path):
    token = 'ghp_' + 'x' * 36
    value = harness.sanitized(f'{tmp_path} https://example.test/file?sig=secret Authorization=owner-secret {token}', (tmp_path,))
    assert str(tmp_path) not in value and token not in value
    assert 'sig=secret' not in value and 'owner-secret' not in value


def test_acceptance_rejects_source_tree_and_reused_directories(tmp_path):
    with pytest.raises(ValueError, match='source repository'):
        harness.fresh_folder(harness.ROOT)
    folder = harness.fresh_folder(tmp_path / 'new')
    (folder / 'evidence.json').write_text('{}')
    with pytest.raises(ValueError, match='new or empty'):
        harness.fresh_folder(folder)


def test_observed_process_reports_actual_metrics_and_preserves_failure(tmp_path):
    log = tmp_path / 'observed.log'
    result = harness.run_observed([sys.executable, '-c', 'print("synthetic child")'], log, timeout=10)
    assert result['wall_seconds'] > 0
    assert 'sampled_process_tree_peak_rss_bytes' in result
    assert 'synthetic child' in log.read_text()
    with pytest.raises(RuntimeError, match='exited 7'):
        harness.run_observed([sys.executable, '-c', 'raise SystemExit(7)'], tmp_path / 'failed.log', timeout=10)


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='Actual FFmpeg fixture mechanics required')
def test_bounded_long_fixture_repeats_declared_bytes_and_reference(tmp_path):
    # A tone tests only fixture/manifest plumbing; it is never transcribed or
    # counted as a neural-model acceptance result.
    audio, text, manifest = tmp_path / 'fixture.wav', tmp_path / 'fixture.txt', tmp_path / 'fixture.json'
    with wave.open(str(audio), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<h', 1000) * 16000 * 3)
    text.write_text('Synthetic fixture contract contains exactly seven reference words.', encoding='utf-8')
    manifest.write_text(json.dumps({'engine': 'kokoro', 'voice': 'am_onyx', 'model_revision': 'a' * 40,
                                   'audio_sha256': harness.digest(audio), 'reference_sha256': harness.digest(text)}))
    out = tmp_path / 'out'; out.mkdir()
    expected, long_audio, long_reference, receipt = harness.fixture(audio, text, manifest, out,
        {'ffmpeg': shutil.which('ffmpeg'), 'ffprobe': shutil.which('ffprobe')})
    assert long_audio.is_file() and 60 <= receipt['long_duration_seconds'] <= 240
    assert long_reference == ' '.join([expected] * receipt['long_repetitions'])
    text.write_text('Mutated reference invalidates its explicit fixture identity.')
    with pytest.raises(ValueError, match='Fixture bytes differ'):
        harness.fixture(audio, text, manifest, out, {'ffmpeg': shutil.which('ffmpeg'), 'ffprobe': shutil.which('ffprobe')})


def test_download_approval_and_non_nested_artifact_paths_are_required(tmp_path):
    required = ['--data', str(tmp_path / 'model-data'), '--output', str(tmp_path / 'model-data' / 'evidence'),
                '--audio', 'fixture.wav', '--reference', 'fixture.txt', '--fixture-manifest', 'fixture.json']
    with pytest.raises(SystemExit):
        harness.main(required)
    with pytest.raises(SystemExit):
        harness.main([*required, '--allow-model-downloads', '--ffmpeg', sys.executable, '--ffprobe', sys.executable])


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='Actual FFmpeg fixture mechanics required')
@pytest.mark.parametrize('amplitude', [12000, 31000])
def test_controlled_noise_is_reproducible_measured_and_does_not_clip_or_replace_source(tmp_path, amplitude):
    audio = tmp_path / 'clean.wav'
    samples = [round(amplitude * math.sin(2 * math.pi * 310 * n / 16000)) for n in range(16000 * 2)]
    with wave.open(str(audio), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
    original = harness.digest(audio)
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir(); second.mkdir()
    settings = {'ffmpeg': shutil.which('ffmpeg'), 'ffprobe': shutil.which('ffprobe')}
    noisy, evidence = harness.noisy_fixture(audio, first, settings)
    replay, repeated = harness.noisy_fixture(audio, second, settings)
    assert noisy.read_bytes() == replay.read_bytes()
    assert evidence['seed'] == repeated['seed'] and evidence['target_snr_db'] == 20
    assert evidence['measured_snr_db'] == pytest.approx(20, abs=.1)
    assert evidence['clipped_samples'] == 0 and harness.digest(audio) == original
    assert not (first / 'noise-source-private.wav').exists()
    with wave.open(str(noisy), 'rb') as stream:
        assert stream.getparams()[:3] == (1, 2, 16000)
        data = struct.unpack('<' + 'h' * stream.getnframes(), stream.readframes(stream.getnframes()))
    assert len(data) == len(samples) and max(map(abs, data)) < 32767
    clean = [round(value * evidence['clean_global_gain']) for value in samples]
    actual_snr = 10 * math.log10(sum(v * v for v in clean) / sum((a - b) ** 2 for a, b in zip(data, clean)))
    assert actual_snr == pytest.approx(evidence['measured_snr_db'], abs=1e-9)
    if amplitude == 31000:
        assert evidence['clean_global_gain'] < 1


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='Actual FFmpeg fixture mechanics required')
def test_optional_british_fixture_binds_provider_voice_reference_and_model_identity(tmp_path):
    # This verifies provenance mechanics only; test tones are never submitted to
    # ASR or counted as neural voice/recognition acceptance.
    audio = tmp_path / 'acceptance-reference.wav'
    reference = tmp_path / 'acceptance-reference.txt'
    reference.write_text('Shared synthetic reference prose with known ordering.')
    primary = {'tts_model_revision': 'a' * 40}
    settings = {'ffmpeg': shutil.which('ffmpeg'), 'ffprobe': shutil.which('ffprobe')}
    candidate, evidence = harness.british_fixture(audio, reference, primary, settings)
    assert candidate is None and evidence['status'] == 'unavailable'
    british = tmp_path / 'acceptance-reference-british.wav'
    with wave.open(str(british), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<h', 1000) * 16000 * 3)
    manifest = british.with_suffix('.json')
    values = {'engine': 'kokoro', 'voice': 'bm_george', 'audio_sha256': harness.digest(british),
              'reference_sha256': harness.digest(reference), 'model_revision': 'a' * 40}
    manifest.write_text(json.dumps(values))
    candidate, evidence = harness.british_fixture(audio, reference, primary, settings)
    assert candidate == british and evidence['voice'] == 'bm_george' and evidence['status'] == 'available'
    for invalid in [{'voice': 'am_onyx'}, {'reference_sha256': 'f' * 64}, {'model_revision': 'b' * 40}]:
        manifest.write_text(json.dumps({**values, **invalid}))
        with pytest.raises(ValueError, match='verified bm_george'):
            harness.british_fixture(audio, reference, primary, settings)


def test_scope_labels_cannot_escape_evidence_directory(tmp_path):
    with pytest.raises(ValueError, match='simple registered label'):
        harness.run_child(sys.executable, 'bark', 'bark-3.0', tmp_path / 'audio.wav', tmp_path / 'data',
                          tmp_path / 'evidence', {}, scope='../outside')
