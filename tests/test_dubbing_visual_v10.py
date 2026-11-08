"""Actual local media plumbing; explicitly synthetic neural/face test doubles.

These tests are not MuseTalk, source-separation or physical GPU acceptance.
They exercise real FFmpeg decoding, temporal selection, compositing, encoding,
output inspection and preservation around a deterministic neural boundary.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import types

import numpy as np
import pytest

from forge import dubbing_visual as visual
from forge import dubbing_separation as separation
from forge.worker import probe
from scripts import dubbing_visual_runtime as runtime


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def media(tmp_path):
    video, audio = tmp_path / 'original.mp4', tmp_path / 'russian.wav'
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
                    '-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10:duration=1',
                    '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', str(video)],
                   check=True, capture_output=True, timeout=30)
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000:duration=1',
                    '-c:a', 'pcm_s16le', str(audio)], check=True, capture_output=True, timeout=30)
    return video, audio, {str(p): digest(p) for p in (video, audio)}


def options(**changes):
    value = {'mode': 'selected', 'consent': True, 'device': 'cpu', 'face_id': 'speaker1',
             'faces': [{'id': 'speaker1', 'box': [16, 16, 32, 32], 'consent': True}],
             'regions': [{'id': 'scene1', 'start': .2, 'end': .6, 'face_id': 'speaker1'}],
             'batch_size': 1, 'preview': False}
    return {**value, **changes}


class NeuralFixture:
    """A test boundary only; no production option enables this implementation."""
    calls = []
    closed = False

    def __init__(self, request, audio):
        self.request = request

    def safe_batch(self, requested, reserve):
        return requested

    def render(self, crops, times):
        type(self).calls.extend(times)
        return [np.full((256, 256, 3), [0, 0, 220], dtype='uint8') for crop in crops]

    def metrics(self):
        return {'gpu_used': False, 'device': 'synthetic-test-boundary'}

    def close(self):
        type(self).closed = True


class FaceFixture:
    def __init__(self, selected, width, height):
        self.selected = selected

    def locate(self, frame, region):
        return (16, 16, 32, 32), .1


@pytest.fixture
def fixture_runtime(monkeypatch):
    NeuralFixture.calls, NeuralFixture.closed = [], False
    monkeypatch.setattr(runtime, 'ModelBundle', NeuralFixture)
    monkeypatch.setattr(runtime, 'FaceTracker', FaceFixture)
    return runtime


@pytest.mark.parametrize('bad', [None, float('nan'), float('inf'), True, -1])
def test_face_coordinates_reject_nonfinite_or_boolean_values(bad):
    selected = options()
    selected['faces'][0]['box'][0] = bad
    with pytest.raises(ValueError):
        visual.validate_options(selected)


def test_permission_is_required_globally_and_for_each_selected_face():
    for selected in [options(consent=False), options(faces=[]),
                     options(faces=[{'id': 'speaker1', 'box': [0, 0, 32, 32], 'consent': False}])]:
        with pytest.raises(ValueError):
            visual.validate_options(selected)


def test_disabled_lip_sync_does_not_require_face_permission_or_load_models(media, tmp_path, monkeypatch):
    video, audio, before = media
    monkeypatch.setitem(sys.modules, 'forge.dubbing_models', None)
    value = visual.process(video, tmp_path / 'not-loaded.wav', tmp_path / 'off.mp4',
                           {'mode': 'off', 'faces': [{'id': 'proposal', 'box': [0, 0, 32, 32], 'consent': False}]},
                           {}, tmp_path)
    assert value['path'] == str(video) and value['changed'] is False
    assert value['metrics']['gpu_used'] is False and not (tmp_path / 'off.mp4').exists()
    assert all(digest(path) == sha for path, sha in before.items())


def test_selected_regions_are_bounded_and_reference_approved_faces():
    for rows in [[{'start': .3, 'end': .2}], [{'start': .1, 'end': 2}],
                 [{'start': 0, 'end': .5, 'face_id': 'unknown'}]]:
        with pytest.raises(ValueError):
            visual.validate_options(options(regions=rows), duration=1)
    with pytest.raises(ValueError):
        visual.validate_options(options(regions=[]))
    selected = visual.validate_options(options(regions=[{'start': .2, 'end': .6, 'box': []}]), duration=1)
    assert selected['regions'][0]['face_id'] == 'speaker1' and 'box' not in selected['regions'][0]


def test_multi_face_automatic_render_requires_an_explicit_default():
    faces = [{'id': 'one', 'box': [0, 0, 32, 32], 'consent': True},
             {'id': 'two', 'box': [32, 0, 32, 32], 'consent': True}]
    with pytest.raises(ValueError):
        visual.validate_options(options(mode='automatic', faces=faces, face_id='', regions=[]))
    selected = visual.validate_options(options(mode='automatic', faces=faces, face_id='two', regions=[]))
    assert selected['face_id'] == 'two'


def test_region_selection_and_real_speech_windows_do_not_invent_speaker_identity():
    selected = visual.validate_options(options(speech_windows=[{'start': .3, 'end': .5, 'speaker': 'speaker1'}]))
    assert visual.active_region(.1, selected) is None
    assert visual.active_region(.25, selected) is None
    assert visual.active_region(.4, selected)['face_id'] == 'speaker1'
    assert visual.active_region(.6, selected) is None


def test_actual_decode_crop_composite_encode_and_audio_mux_preserve_originals(media, tmp_path, fixture_runtime):
    video, audio, before = media
    selected = visual.validate_options(options(), duration=1)
    snapshot = copy.deepcopy(selected)
    target = tmp_path / 'dubbed.mp4'
    result = runtime.lipsync({'source': str(video), 'audio': str(audio), 'output': str(target),
                              'options': selected, 'device': 'cpu'}, runtime.Cancellation())
    assert result['frames_changed'] == 4 and len(NeuralFixture.calls) == 4
    assert result['metrics']['frames_processed'] == 10
    assert result['metrics']['gpu_used'] is False and NeuralFixture.closed
    info = probe(target, {})
    assert {stream['codec_type'] for stream in info['streams']} >= {'video', 'audio'}
    assert float(info['format']['duration']) == pytest.approx(1, abs=.15)
    assert selected == snapshot, 'Rendering must not mutate the project selection snapshot.'
    assert all(digest(path) == sha for path, sha in before.items())
    assert not list(tmp_path.glob('.forge-lipsync-*'))


def test_preview_trims_only_new_output_and_reports_actual_short_source_duration(media, tmp_path, fixture_runtime):
    video, audio, before = media
    selected = visual.validate_options(options(mode='full', regions=[], preview=True, preview_start=.2), duration=1)
    result = runtime.lipsync({'source': str(video), 'audio': str(audio), 'output': str(tmp_path / 'preview.mkv'),
                              'options': selected, 'device': 'cpu'}, runtime.Cancellation())
    assert result['duration'] == pytest.approx(.8, abs=.2)
    assert result['metrics']['frames_processed'] == 8
    assert all(digest(path) == sha for path, sha in before.items())


def test_actual_rotated_phone_video_keeps_display_orientation(media, tmp_path, fixture_runtime):
    _, audio, _ = media
    plain, rotated = tmp_path / 'landscape.mp4', tmp_path / 'portrait.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n', '-f', 'lavfi',
                    '-i', 'testsrc2=size=80x48:rate=10:duration=1', '-c:v', 'libx264', '-threads', '1', str(plain)],
                   check=True, capture_output=True, timeout=30)
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n', '-display_rotation:v:0', '90',
                    '-i', str(plain), '-c', 'copy', str(rotated)],
                   check=True, capture_output=True, timeout=30)
    before = digest(rotated)
    target = tmp_path / 'upright.mp4'
    result = runtime.lipsync({'source': str(rotated), 'audio': str(audio), 'output': str(target),
                             'options': visual.validate_options(options(mode='full', regions=[])), 'device': 'cpu'}, runtime.Cancellation())
    stream = next(s for s in probe(target, {})['streams'] if s['codec_type'] == 'video')
    assert (stream['width'], stream['height']) == (48, 80)
    assert result['metrics']['source_rotation'] in {90, 270} and digest(rotated) == before


def test_unknown_average_frame_rate_uses_valid_reported_rate_without_guessing():
    assert float(runtime._video_geometry({'width': 80, 'height': 48, 'avg_frame_rate': '0/0', 'r_frame_rate': '25/1'})[2]) == 25
    with pytest.raises(ValueError):
        runtime._video_geometry({'width': 80, 'height': 48, 'avg_frame_rate': '0/0', 'r_frame_rate': '0/0'})


def test_no_suitable_face_does_not_publish_a_fake_success(media, tmp_path, fixture_runtime, monkeypatch):
    video, audio, before = media
    monkeypatch.setattr(FaceFixture, 'locate', lambda *args: (None, None))
    target = tmp_path / 'no-face.mp4'
    with pytest.raises(RuntimeError, match='No suitable'):
        runtime.lipsync({'source': str(video), 'audio': str(audio), 'output': str(target),
                         'options': visual.validate_options(options()), 'device': 'cpu'}, runtime.Cancellation())
    assert not target.exists() and NeuralFixture.closed
    assert not list(tmp_path.glob('.forge-lipsync-*'))
    assert all(digest(path) == sha for path, sha in before.items())


def test_mid_render_cancellation_cleans_owned_video_work_and_keeps_audio(media, tmp_path, fixture_runtime, monkeypatch):
    video, audio, before = media
    marker = tmp_path / 'cancel'
    original = NeuralFixture.render

    def cancel_after_inference(self, crops, times):
        result = original(self, crops, times)
        marker.touch()
        return result

    monkeypatch.setattr(NeuralFixture, 'render', cancel_after_inference)
    target = tmp_path / 'cancelled.mp4'
    with pytest.raises(InterruptedError):
        runtime.lipsync({'source': str(video), 'audio': str(audio), 'output': str(target),
                         'options': visual.validate_options(options()), 'device': 'cpu'}, runtime.Cancellation(marker))
    assert not target.exists() and NeuralFixture.closed
    assert not list(tmp_path.glob('.forge-lipsync-*'))
    assert all(digest(path) == sha for path, sha in before.items())


def test_existing_output_is_never_replaced(media, tmp_path, fixture_runtime):
    video, audio, _ = media
    target = tmp_path / 'keep.mp4'
    target.write_bytes(b'Existing export fixture')
    with pytest.raises(ValueError):
        runtime.lipsync({'source': str(video), 'audio': str(audio), 'output': str(target),
                         'options': visual.validate_options(options()), 'device': 'cpu'}, runtime.Cancellation())
    assert target.read_bytes() == b'Existing export fixture' and not NeuralFixture.calls


def test_uninstalled_component_preserves_completed_audio_and_gives_repair_action(media, tmp_path, monkeypatch):
    video, audio, before = media
    module = types.ModuleType('forge.dubbing_models')
    module.resolve = lambda *a, **k: (_ for _ in ()).throw(ValueError('Checkpoint verification is pending.'))
    module.invoke = lambda *a, **k: pytest.fail('Unverified models must not execute.')
    monkeypatch.setitem(sys.modules, 'forge.dubbing_models', module)
    with pytest.raises(visual.LipSyncUnavailable, match='Install Optional or Repair') as error:
        visual.process(video, audio, tmp_path / 'not-ready.mp4', options(), {}, tmp_path)
    assert error.value.audio_preserved and all(digest(path) == sha for path, sha in before.items())


def test_wrapper_checks_actual_runtime_video_before_atomic_publish(media, tmp_path, fixture_runtime, monkeypatch):
    video, audio, before = media
    module = types.ModuleType('forge.dubbing_models')
    selected_revision = 'b' * 64
    resolved = []

    def resolve(component, data, **kwargs):
        resolved.append(kwargs.get('revision'))
        return {'model_revision': selected_revision}

    def invoke(component, data, request, **kwargs):
        assert request['model_revision'] == selected_revision
        return runtime.lipsync(request, runtime.Cancellation())

    module.resolve, module.invoke = resolve, invoke
    monkeypatch.setitem(sys.modules, 'forge.dubbing_models', module)
    target = tmp_path / 'verified.mp4'
    result = visual.process(video, audio, target, options(model_revision=selected_revision), {}, tmp_path)
    assert result['path'] == str(target) and result['changed'] is True
    assert result['model_revision'] == selected_revision and target.is_file() and resolved == [selected_revision]
    assert not list(tmp_path.glob('.forge-preview-*'))
    assert all(digest(path) == sha for path, sha in before.items())


def test_wrapper_does_not_accept_a_copy_only_runtime_as_neural_success(media, tmp_path, monkeypatch):
    video, audio, before = media
    module = types.ModuleType('forge.dubbing_models')
    module.resolve = lambda *a, **k: {'model_revision': 'a' * 40}

    def fake_result(component, data, request, **kw):
        Path(request['output']).write_bytes(video.read_bytes())
        return {'model_inference': False, 'frames_changed': 0}

    module.invoke = fake_result
    monkeypatch.setitem(sys.modules, 'forge.dubbing_models', module)
    target = tmp_path / 'no-neural.mp4'
    with pytest.raises(visual.LipSyncUnavailable, match='model-generated'):
        visual.process(video, audio, target, options(), {}, tmp_path)
    assert not target.exists() and not list(tmp_path.glob('.forge-preview-*'))
    assert all(digest(path) == sha for path, sha in before.items())


def test_gpu_probe_does_not_claim_a_cuda_model_or_rtx_acceptance(monkeypatch):
    fake = types.SimpleNamespace(__version__='synthetic-torch', version=types.SimpleNamespace(cuda=None),
                                 cuda=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, 'torch', fake)
    result = runtime.gpu_probe('auto')
    assert result['device'] == 'cpu' and result['kernel_test'] == 'not_run'
    assert result['rtx_5080_validated'] is False
    with pytest.raises(RuntimeError, match='compatibility test failed'):
        runtime.gpu_probe('cuda')


def test_tensor_checkpoint_loader_never_allows_pickle_globals(monkeypatch, tmp_path):
    class Tensor:
        pass

    calls = []

    def load(path, **kwargs):
        calls.append(kwargs)
        return {'weight': Tensor()}

    fake = types.SimpleNamespace(Tensor=Tensor, load=load)
    monkeypatch.setitem(sys.modules, 'torch', fake)
    runtime._tensor_state(tmp_path / 'reviewed.pth')
    assert calls == [{'map_location': 'cpu', 'weights_only': True}]
    fake.load = lambda *a, **kw: {'unreviewed-object': object()}
    with pytest.raises(ValueError, match='plain tensor'):
        runtime._tensor_state(tmp_path / 'bad.pth')


@pytest.mark.skipif(importlib.util.find_spec('cv2') is None, reason='Run in the optional isolated visual dependency environment for actual OpenCV tracking.')
def test_actual_opencv_tracks_selected_spatial_features_and_does_not_reseed_a_lost_face(monkeypatch):
    import cv2
    selected = visual.validate_options(options(mode='full', regions=[]))
    # Generated checkerboard features exercise real Lucas-Kanade flow. This is
    # spatial tracking evidence, not face identity or neural rendering evidence.
    frame = np.zeros((96, 96, 3), dtype='uint8')
    pattern = np.indices((32, 32)).sum(axis=0) // 4 % 2 * 255
    frame[16:48, 16:48] = pattern[..., None]
    translated = cv2.warpAffine(frame, np.array([[1, 0, 4], [0, 1, 3]], dtype='float32'), (96, 96))
    tracker = runtime.FaceTracker(selected, 96, 96)
    first, _ = tracker.locate(frame, {})
    second, _ = tracker.locate(translated, {})
    assert first == (16, 16, 32, 32)
    assert second[0] == pytest.approx(20, abs=1) and second[1] == pytest.approx(19, abs=1)
    monkeypatch.setattr(cv2, 'calcOpticalFlowPyrLK', lambda a, b, points, *args: (points, np.zeros((len(points), 1), dtype='uint8'), None))
    assert tracker.locate(translated, {})[0] is None
    assert tracker.locate(translated, {})[0] is None, 'A lost face must not be silently re-seeded in the same scene.'


@pytest.mark.skipif(importlib.util.find_spec('cv2') is None, reason='Run in the optional isolated visual dependency environment for actual OpenCV tracking.')
def test_actual_opencv_face_detector_rejects_blank_unselected_scene():
    selected = visual.validate_options(options(mode='full', regions=[]))
    tracker = runtime.FaceTracker(selected, 96, 96)
    blank = np.zeros((96, 96), dtype='uint8')
    assert len(tracker.detector.detectMultiScale(blank, scaleFactor=1.1, minNeighbors=6, minSize=(32, 32))) == 0


@pytest.mark.parametrize('field,value', [('batch_size', 1.2), ('batch_size', True), ('max_height', 512.1)])
def test_frame_resource_options_are_integral_and_never_silently_rounded(field, value):
    with pytest.raises(ValueError):
        visual.validate_options(options(**{field: value}))


def test_manual_background_uses_actual_audio_and_preserves_source_timeline(media, tmp_path):
    video, audio, before = media
    background = tmp_path / 'short-background.wav'
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n', '-f', 'lavfi',
                    '-i', 'sine=frequency=220:sample_rate=48000:duration=0.4', str(background)],
                   check=True, capture_output=True, timeout=30)
    before[str(background)] = digest(background)
    result = separation.process(audio, tmp_path / 'stems', {'mode': 'manual', 'background_path': str(background)}, {}, tmp_path)
    assert result['separation_performed'] is False and result['vocals'] is None
    assert result['duration'] == pytest.approx(1, abs=.05)
    assert result['background'] != str(background) and result['warnings']
    assert all(digest(path) == sha for path, sha in before.items())


def test_separation_off_does_not_label_original_mix_as_isolated_vocals(media, tmp_path):
    _, audio, _ = media
    result = separation.process(audio, tmp_path / 'unused', {'mode': 'off'}, {}, tmp_path)
    assert result['separation_performed'] is False and result['background'] is None and result['vocals'] is None
    assert not (tmp_path / 'unused').exists()


def test_unverified_separation_checkpoint_has_working_manual_fallback_message(media, tmp_path, monkeypatch):
    _, audio, before = media
    module = types.ModuleType('forge.dubbing_models')
    module.resolve = lambda *a, **k: (_ for _ in ()).throw(ValueError('License review is pending.'))
    module.invoke = lambda *a, **k: pytest.fail('Unverified checkpoints must not execute.')
    monkeypatch.setitem(sys.modules, 'forge.dubbing_models', module)
    with pytest.raises(separation.SeparationUnavailable, match='manual background stem or ducking'):
        separation.process(audio, tmp_path / 'pending', {'mode': 'hdemucs'}, {}, tmp_path)
    assert all(digest(path) == sha for path, sha in before.items())


def test_soft_blend_keeps_eyes_and_outside_face_pixels_unchanged():
    image = np.full((64, 64, 3), 40, dtype='uint8')
    patch = np.full((256, 256, 3), 200, dtype='uint8')
    result = runtime._blend(image, patch, (16, 16, 32, 32))
    assert np.array_equal(result[:16], image[:16])
    assert np.array_equal(result[16:22, 16:48], image[16:22, 16:48])
    assert np.array_equal(result[:, :16], image[:, :16])
    assert not np.array_equal(result[30:44, 22:42], image[30:44, 22:42])


@pytest.mark.parametrize('box,normalized', [([-1, 0, 20, 20], False), ([0, 0, 1, 1], False),
                                        ([.8, 0, .4, .5], True), ([0, 0, float('inf'), 1], True)])
def test_runtime_face_bounds_reject_invalid_or_out_of_frame_boxes(box, normalized):
    with pytest.raises(ValueError):
        runtime._pixel_box(box, normalized, 64, 64)
