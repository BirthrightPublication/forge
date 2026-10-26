"""Acceptance harness mechanics, without pretending a model was inferred."""
import hashlib
import importlib.util
import json
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
