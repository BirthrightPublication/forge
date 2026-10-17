"""Exercise actual encoded watermarks, rather than only generated filter strings."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
from PIL import Image
import pytest
from pydantic import ValidationError

from forge import watermark


pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg integration tests require local executables')


def run(*args):
    return subprocess.run(list(args), check=True, capture_output=True).stdout


@pytest.fixture(scope='module')
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp('watermark-fixtures')
    source, clip, audio = root / 'source.mp4', root / 'announcement.wav', root / 'silence.wav'
    run('ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=320x240:r=24:d=2',
        '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', '2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source))
    run('ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=1000:duration=0.25', str(clip))
    run('ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', '2', str(audio))
    return source, clip, audio


def frame(path, at=.5):
    raw = run('ffmpeg', '-nostdin', '-v', 'error', '-ss', str(at), '-protocol_whitelist', 'file,pipe', '-i', str(path),
              '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1')
    return np.frombuffer(raw, dtype=np.uint8).reshape(240, 320, 3)


def brightness(path, at=.5):
    return frame(path, at).max(axis=2) > 30


def pcm(path):
    raw = run('ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-i', str(path),
              '-map', '0:a:0', '-ac', '1', '-ar', '48000', '-f', 'f32le', 'pipe:1')
    return np.frombuffer(raw, dtype='<f4')


def test_literal_text_is_encoded_and_original_untouched(media, tmp_path):
    source = media[0]
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    outputs, warnings = watermark.process(source, tmp_path, {'text': "Owner '; [x] $(touch) %"}, {})
    assert len(outputs) == 1 and brightness(outputs[0]).sum() > 100
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert not (tmp_path / '_watermark_overlay.png').exists()
    assert any('CPU H.264' in text for text in warnings)
    assert any('FFprobe verified' in text for text in warnings)


def test_image_logo_changes_real_pixels(media, tmp_path):
    logo = tmp_path / 'logo.png'
    Image.new('RGBA', (80, 40), (255, 0, 255, 255)).save(logo)
    outputs, _ = watermark.process(media[0], tmp_path / 'out', {'video_kind': 'image', 'image_path': str(logo), 'opacity': 1, 'fade_seconds': 0}, {})
    image = frame(outputs[0])
    magenta = (image[:, :, 0] > 150) & (image[:, :, 2] > 150) & (image[:, :, 1] < 80)
    assert magenta.sum() > 1000


@pytest.mark.parametrize('placement', ['full-frame-diagonal', 'perimeter'])
def test_repeated_placement_covers_multiple_regions(media, tmp_path, placement):
    output = watermark.process(media[0], tmp_path, {'text': 'OWNER', 'font_size': 20, 'placement': placement, 'repeat_spacing': 25, 'margin': 4}, {})[0][0]
    mask = brightness(output)
    ys, xs = np.where(mask)
    assert xs.max() - xs.min() > 200
    assert ys.max() - ys.min() > 150


def test_scheduled_marks_and_periodic_fades(media, tmp_path):
    options = {'text': 'VISIBLE', 'start_seconds': .25, 'end_seconds': 1.75, 'appearance_interval': 1, 'appearance_duration': .5, 'fade_seconds': .1}
    output = watermark.process(media[0], tmp_path, options, {})[0][0]
    assert brightness(output, .05).sum() == 0
    assert brightness(output, .5).sum() > 100
    assert brightness(output, 1).sum() == 0
    assert brightness(output, 1.5).sum() > 100
    assert brightness(output, 1.9).sum() == 0


def test_bounded_motion_changes_visible_position(media, tmp_path):
    options = {'text': 'X', 'font_size': 16, 'placement': 'custom', 'movement': 'bounce', 'movement_speed': 50,
               'movement_bounds': [.1, .1, .9, .9], 'margin': 0, 'fade_seconds': 0}
    output = watermark.process(media[0], tmp_path, options, {})[0][0]
    first_y, first_x = np.where(brightness(output, .25))
    later_y, later_x = np.where(brightness(output, 1.25))
    assert later_x.mean() > first_x.mean() + 30
    assert later_y.mean() > first_y.mean() + 30
    assert first_x.min() >= 32 and later_x.max() < 288
    assert first_y.min() >= 24 and later_y.max() < 216


def test_horizontal_motion_respects_vertical_boundary(media, tmp_path):
    options = {'text': 'X', 'font_size': 16, 'placement': 'bottom-center', 'movement': 'horizontal',
               'movement_bounds': [.2, .2, .8, .8], 'margin': 0, 'fade_seconds': 0}
    output = watermark.process(media[0], tmp_path, options, {})[0][0]
    ys, xs = np.where(brightness(output, 1))
    assert xs.min() >= 64 and xs.max() < 256
    assert ys.min() >= 48 and ys.max() < 192


def test_audible_insertions_change_signal_only_at_selected_times(media, tmp_path):
    _, clip, source = media
    options = {'mode': 'audio', 'format': 'wav', 'audio_clip_path': str(clip), 'audio_timestamps': [.5],
               'audio_repeat_seconds': 1, 'audio_volume': .5, 'audio_fade_seconds': .02}
    output = watermark.process(source, tmp_path, options, {})[0][0]
    samples = pcm(output)
    assert np.abs(samples[:20000]).max() < .0001
    for start in (.55, 1.55):
        section = samples[int(start * 48000):int((start + .1) * 48000)]
        assert np.sqrt(np.mean(section ** 2)) > .02
        frequencies = np.fft.rfftfreq(len(section), 1 / 48000)
        assert abs(frequencies[np.argmax(abs(np.fft.rfft(section))) ] - 1000) < 12
    assert np.abs(samples[40000:65000]).max() < .0001


def packet_hashes(path):
    data = json.loads(run('ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-select_streams', 'v:0',
                          '-show_packets', '-show_data_hash', 'sha256', '-of', 'json', str(path)))
    return [packet['data_hash'] for packet in data['packets']]


def test_audio_only_watermark_copies_original_video_packets(media, tmp_path):
    source, clip, _ = media
    output, warnings = watermark.process(source, tmp_path, {'mode': 'audio', 'format': 'mp4', 'audio_clip_path': str(clip)}, {})
    assert packet_hashes(source) == packet_hashes(output[0])
    assert np.abs(pcm(output[0])).max() > .02
    assert any('copied without re-encoding' in text for text in warnings)


def test_combined_watermarks_have_both_visible_and_audible_changes(media, tmp_path):
    output = watermark.process(media[0], tmp_path, {'mode': 'both', 'audio_clip_path': str(media[1])}, {})[0][0]
    assert brightness(output).sum() > 100
    assert np.abs(pcm(output)).max() > .02


def test_preview_is_encoded_and_still_is_from_output(media, tmp_path):
    preview = watermark.preview(media[0], tmp_path / 'clip', {}, {})
    assert '_preview' in preview.name and brightness(preview).sum() > 100
    still = watermark.preview_still(media[0], tmp_path / 'still', {}, {})
    with Image.open(still) as image:
        assert image.size == (320, 240)
        assert np.array(image).max() > 100


def test_preview_stops_at_five_seconds(tmp_path):
    source = tmp_path / 'long.mp4'
    run('ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=320x240:r=12:d=6',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source))
    preview = watermark.preview(source, tmp_path / 'preview', {}, {})
    assert 4.9 <= watermark._duration(watermark._probe(preview, {})) <= 5.1


@pytest.mark.parametrize('options', [
    {'command': 'touch'}, {'text': ' '}, {'text': 'nul\x00text'}, {'color': 'red;movie=http://example.com'},
    {'font_size': 10000}, {'opacity': float('nan')}, {'audio_timestamps': [float('inf')]},
    {'movement_bounds': [0, 0, 0, 1]}, {'movement_bounds': [-1, 0, 1, 1]},
    {'end_seconds': 1, 'start_seconds': 2}, {'format': 'wav'}, {'video_kind': 'image'},
    {'mode': 'audio', 'format': 'wav'}, {'appearance_interval': 1, 'appearance_duration': 2},
    {'movement': 'bounce', 'placement': 'perimeter'},
])
def test_reject_invalid_or_unbounded_options(options):
    with pytest.raises(ValidationError):
        watermark.WatermarkOptions.model_validate(options)


def test_assets_require_local_supported_files(tmp_path):
    with pytest.raises(ValueError, match='absolute'):
        watermark.validate_assets({'video_kind': 'image', 'image_path': 'relative.png'})
    executable = tmp_path / 'payload.exe'
    executable.write_bytes(b'MZ')
    with pytest.raises(ValueError, match='supported image'):
        watermark.validate_assets({'video_kind': 'image', 'image_path': str(executable)})


def test_plan_rejects_no_effect_and_excessive_audio_events(media):
    with pytest.raises(ValueError, match='starts after'):
        watermark.publicplan(media[0], {'start_seconds': 10}, {})
    with pytest.raises(ValueError, match='100 audible insertions'):
        watermark._events(watermark.WatermarkOptions(audio_repeat_seconds=.1), 100)
    with pytest.raises(ValueError, match='video stream'):
        watermark.publicplan(media[2], {}, {})
    with pytest.raises(ValueError, match='must not overlap'):
        watermark.publicplan(media[2], {'mode': 'audio', 'format': 'wav', 'audio_clip_path': str(media[1]), 'audio_repeat_seconds': .1}, {})


def test_explicit_nvenc_falls_back_when_unavailable(media, tmp_path, monkeypatch):
    monkeypatch.setattr(watermark, 'nvenc_available', lambda _: False)
    outputs, warnings = watermark.process(media[0], tmp_path, {'encoder': 'nvenc'}, {})
    assert brightness(outputs[0]).sum() > 100
    assert any('CPU H.264 fallback' in text for text in warnings)


def test_failed_encode_removes_partial_output(media, tmp_path, monkeypatch):
    def failed(args, *_):
        Path(args[-1]).write_bytes(b'incomplete')
        raise RuntimeError('simulated encode failure')
    monkeypatch.setattr(watermark, '_run_ffmpeg', failed)
    with pytest.raises(RuntimeError, match='simulated'):
        watermark.process(media[0], tmp_path, {'encoder': 'cpu'}, {})
    assert list(tmp_path.iterdir()) == []


def test_custom_font_and_scale_work(media, tmp_path):
    font = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    if not font.is_file():
        pytest.skip('Fixture font is not installed on this platform')
    output = watermark.process(media[0], tmp_path, {'font_path': str(font), 'font_size': 20, 'scale': 1.5, 'rotation': 25}, {})[0][0]
    assert brightness(output).sum() > 100


def test_network_protocols_are_restricted(monkeypatch):
    captured = []
    def fake_run(command, **kwargs):
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='{}', stderr='')
    monkeypatch.setattr(watermark.subprocess, 'run', fake_run)
    monkeypatch.setattr(watermark, '_binary', lambda name, _: name)
    watermark._probe(Path('/tmp/a.mp4'), {})
    assert captured[0][captured[0].index('-protocol_whitelist') + 1] == 'file,pipe'
    class FakeProcess:
        stdout = []
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def wait(self):
            return 0
    def fake_popen(command, **kwargs):
        captured.append(command)
        return FakeProcess()
    monkeypatch.setattr(watermark.subprocess, 'Popen', fake_popen)
    watermark._run_ffmpeg(['-i', '/tmp/a.mp4', '-i', '/tmp/b.png', '/tmp/out.mp4'], {}, 1)
    assert captured[1].count('-protocol_whitelist') == 2


def test_progress_is_based_on_actual_encoded_time(media, tmp_path, monkeypatch):
    updates = []
    monkeypatch.setattr('forge.worker.progress', lambda *args, **kwargs: updates.append((args, kwargs)))
    watermark.process(media[0], tmp_path, {}, {}, {'tool': 'watermark', 'source': str(media[0])})
    assert updates[0][1]['progress_measured'] is False
    assert any(item[1]['progress_measured'] and item[0][0] > 0 for item in updates)
