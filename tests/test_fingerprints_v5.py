"""Real local decode and independent media preservation checks for V0.5 labs."""
import hashlib
import io
import json
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from forge.fingerprints import (AudioFingerprintOptions, VideoFingerprintOptions, _capture,
    analyze_audio, analyze_video, compare_audio, compare_video, process_audio, process_video,
    public_analysis, read_references)

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg and FFprobe required')


def command(args):
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1', '-filter_threads', '1', *args], check=True, capture_output=True, timeout=45)


@pytest.fixture(scope='module')
def audio_fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp('audio-lab')
    source = root / 'owned original.wav'
    with wave.open(str(source), 'wb') as output:
        output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        data = bytearray()
        for index in range(16000 * 16):
            t = index / 16000
            frequency = [220, 330, 440, 660, 294, 392, 523, 784][int(t * 2) % 8]
            envelope = .35 + .3 * math.sin(2 * math.pi * 1.7 * t)
            value = int(16000 * envelope * (math.sin(2 * math.pi * frequency * t) + .25 * math.sin(2 * math.pi * frequency * 3 * t)))
            data.extend(struct.pack('<h', value))
        output.writeframes(data)
    quiet = root / 'gain copy.flac'
    command(['-i', str(source), '-af', 'volume=0.5', str(quiet)])
    unrelated = root / 'unrelated.wav'
    command(['-f', 'lavfi', '-i', 'sine=frequency=1800:duration=16:sample_rate=16000', str(unrelated)])
    return source, quiet, unrelated


@pytest.fixture(scope='module')
def video_fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp('video-lab')
    source, other, compressed = (root / name for name in ('owned video.mp4', 'different.mp4', 'copy.mp4'))
    command(['-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=8:duration=3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)])
    command(['-f', 'lavfi', '-i', 'color=c=blue:size=320x180:rate=8:duration=3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(other)])
    command(['-i', str(source), '-vf', 'scale=256:144', '-c:v', 'libx264', '-crf', '28', str(compressed)])
    return source, other, compressed


def test_real_audio_identity_gain_and_unrelated(audio_fixture):
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in audio_fixture]
    a, b, c = [analyze_audio(p, {}, {'max_seconds': 15}) for p in audio_fixture]
    identical = compare_audio(a, a)
    similar = compare_audio(a, b)
    unrelated = compare_audio(a, c)
    assert identical['similarity'] == 1
    assert identical['components']['identical_decoded_interval'] is True
    assert similar['similarity'] > .9
    assert unrelated['similarity'] < .5
    assert a['sample_rate'] == 16000 and a['channels'] == 1 and a['codec'] == 'pcm_s16le'
    assert a['analyzed_seconds'] == 15
    assert a['frequency_peaks'] and a['fingerprint']['spectrum']
    assert a['chromaprint_status'] == 'calculated' or 'Chromaprint' not in a['fingerprint_method']
    for uri in a['visualizations'].values():
        import base64
        image = Image.open(io.BytesIO(base64.b64decode(uri.split(',')[1])))
        image.verify()
    assert before == [hashlib.sha256(p.read_bytes()).hexdigest() for p in audio_fixture]


def test_real_video_identity_reencode_and_unrelated(video_fixture):
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in video_fixture]
    a, b, c = [analyze_video(p, {}, {'sample_frames': 5, 'max_seconds': 3}) for p in video_fixture]
    assert compare_video(a, a)['similarity'] == 1
    reencoded, unrelated = compare_video(a, c), compare_video(a, b)
    assert reencoded['similarity'] > .85
    assert unrelated['similarity'] < .8
    assert reencoded['similarity'] > unrelated['similarity'] + .2
    assert reencoded['components']['resolution_changed'] is True
    assert a['frame_rate'] == 8 and a['width'] == 320 and a['height'] == 180
    assert len(a['frames']) == 5 and a['frames'][1]['timestamp'] == .6
    assert reencoded['timeline'] and reencoded['near_matching_sections']
    assert all(len(f['phash']) == 16 and len(f['dhash']) == 16 for f in a['fingerprint']['frames'])
    assert before == [hashlib.sha256(p.read_bytes()).hexdigest() for p in video_fixture]


def test_selected_frames_and_invalid_window(video_fixture):
    result = analyze_video(video_fixture[0], {}, {'selected_times': [1.5, .5, .5], 'max_seconds': 3})
    assert [f['timestamp'] for f in result['frames']] == [.5, 1.5]
    with pytest.raises(ValueError, match='inside'):
        analyze_video(video_fixture[0], {}, {'selected_times': [4], 'max_seconds': 3})


@pytest.mark.parametrize('model,options', [
    (AudioFingerprintOptions, {'max_seconds': 601}), (AudioFingerprintOptions, {'max_seconds': float('nan')}),
    (AudioFingerprintOptions, {'command': 'sh'}), (VideoFingerprintOptions, {'sample_frames': 49}),
    (VideoFingerprintOptions, {'selected_times': [-1]}), (VideoFingerprintOptions, {'selected_times': [float('inf')]}),
    (VideoFingerprintOptions, {'selected_times': [1] * 49}),
])
def test_option_limits(model, options):
    with pytest.raises(ValidationError):
        model.model_validate(options)


def test_reports_default_no_raw_references_and_explicit_retention(audio_fixture, tmp_path):
    context = {'comparison_sources': [{'path': str(audio_fixture[1])}]}
    files, warnings = process_audio(audio_fixture[0], tmp_path, {'max_seconds': 15}, {}, context)
    assert len(files) == 2 and warnings
    assert all('forge-fingerprint' not in p.name for p in files)
    report = json.loads(next(p for p in files if p.suffix == '.json').read_text())
    assert len(report['analyses']) == 2 and len(report['comparisons']) == 1
    assert all('fingerprint' not in result for result in report['analyses'])
    assert str(audio_fixture[0].parent) not in next(p for p in files if p.suffix == '.html').read_text()
    stage = tmp_path / 'references'; stage.mkdir()
    retained, _ = process_audio(audio_fixture[0], stage, {'max_seconds': 15, 'report_json': False, 'report_html': False, 'retain_reference': True}, {}, {})
    assert len(retained) == 1 and retained[0].name.endswith('.forge-fingerprint.json')
    imported = read_references(retained[0], 'audio')
    assert len(imported) == 1 and imported[0]['fingerprint']['spectrum']
    stage2 = tmp_path / 'compared'; stage2.mkdir()
    compared_context = {'reference_sources': [{'path': str(retained[0])}]}
    process_audio(audio_fixture[0], stage2, {'max_seconds': 15, 'report_json': False, 'report_html': False}, {}, compared_context)
    assert compared_context['analysis_result']['comparisons'][0]['similarity'] == 1
    assert list(stage2.iterdir()) == []


def test_video_reports_and_reference_roundtrip(video_fixture, tmp_path):
    context = {'comparison_sources': [str(video_fixture[2])]}
    files, _ = process_video(video_fixture[0], tmp_path, {'sample_frames': 3, 'max_seconds': 3, 'retain_reference': True}, {}, context)
    assert len(files) == 3
    refs = read_references(next(p for p in files if p.name.endswith('.forge-fingerprint.json')), 'video')
    assert len(refs) == 2
    assert compare_video(refs[0], refs[1])['similarity'] > .8
    assert all('phash' not in f for a in context['analysis_result']['analyses'] for f in a['frames'])


def test_retention_disabled_creates_no_files(audio_fixture, tmp_path):
    context = {}
    files, warnings = process_audio(audio_fixture[0], tmp_path, {'max_seconds': 15, 'report_json': False, 'report_html': False}, {}, context)
    assert files == [] and list(tmp_path.iterdir()) == []
    assert 'analysis_result' in context
    assert any('disabled' in warning for warning in warnings)


def test_silence_not_reported_as_identity(tmp_path):
    source = tmp_path / 'silence.wav'
    command(['-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-t', '1', str(source)])
    result = analyze_audio(source, {})
    comparison = compare_audio(result, result)
    assert comparison['similarity'] is None
    assert comparison['classification'] == 'insufficient audible signal'


def test_wrong_stream_and_playlist_rejected(audio_fixture, video_fixture, tmp_path):
    with pytest.raises(ValueError, match='no video'):
        analyze_video(audio_fixture[0], {})
    with pytest.raises(ValueError, match='no audio'):
        analyze_audio(video_fixture[0], {})
    playlist = tmp_path / 'list.m3u8'
    playlist.write_text('#EXTM3U\nhttps://example.com/private.wav')
    with pytest.raises(ValueError, match='playlists'):
        analyze_audio(playlist, {})
    # Spoofing a media suffix still cannot invoke the HLS demuxer.
    disguised = tmp_path / 'disguised.mp4'; disguised.write_bytes(playlist.read_bytes())
    with pytest.raises(RuntimeError, match='decoder'):
        analyze_audio(disguised, {})


def test_invalid_reference_shapes(tmp_path):
    path = tmp_path / 'reference.json'
    path.write_text(json.dumps({'format': 'forge-fingerprints', 'schema_version': 1, 'kind': 'audio', 'records': [{'kind': 'audio'}]}))
    with pytest.raises(ValueError):
        read_references(path, 'audio')
    path.write_text('[]')
    with pytest.raises(ValueError):
        read_references(path, 'video')


def test_process_batch_limit_rejected_before_decode(audio_fixture, tmp_path):
    with pytest.raises(ValueError, match='20'):
        process_audio(audio_fixture[0], tmp_path, {}, {}, {'comparison_sources': [str(audio_fixture[0])] * 20})


def test_subprocess_limits():
    import sys
    with pytest.raises(RuntimeError, match='output size'):
        _capture([sys.executable, '-c', 'print("x"*10000)'], max_bytes=100)
    with pytest.raises(RuntimeError, match='processing time'):
        _capture([sys.executable, '-c', 'import time;time.sleep(5)'], max_bytes=100, timeout=.1)
