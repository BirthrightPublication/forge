"""Reviewed local MuseTalk/Hybrid Demucs inference in an isolated environment.

The MuseTalk tensor layout follows Tencent Music Entertainment's MIT-licensed
MuseTalk implementation (copyright 2024 Tencent Music Entertainment Group),
source 0a89dec45a0192b824e3cf4daf96c239440c5ed8. This modern port omits upstream
shell downloaders, pickle caches, old MMCV/DWPose binaries and demonstration
media. Face selection, streaming, bounded work and safety checks are FORGE code.
Models are loaded only from the verified paths supplied by the managed installer.
"""
from __future__ import annotations

from collections import OrderedDict, deque
import argparse
import copy
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time


def emit(value, message, **extra):
    print(json.dumps({'kind': 'progress', 'progress': max(0, min(99, value)),
                      'message': message, **extra}, ensure_ascii=False), flush=True)


def _number(value, minimum, maximum, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'{label} is outside its supported bounds.')
    return float(value)


def _file(value):
    path = Path(str(value)).expanduser()
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError('The selected local media/model file is missing or is a link.')
    return path.resolve()


def _program(name, settings):
    import shutil
    selected = str(settings.get(name) or '')
    if selected:
        path = Path(selected)
        if path.is_dir():
            path = path / (name + ('.exe' if os.name == 'nt' else ''))
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f'The configured {name} executable is not available.')
        return str(path.resolve())
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f'{name} is required. Repair Audio Processing in Dubbing Setup.')
    return found


def _probe(path, settings):
    result = subprocess.run([_program('ffprobe', settings), '-v', 'error', '-protocol_whitelist', 'file,pipe',
                             '-show_format', '-show_streams', '-of', 'json', str(path)],
                            capture_output=True, timeout=120, **_no_window())
    if result.returncode:
        raise RuntimeError('Media inspection failed: ' + result.stderr.decode('utf-8', 'replace')[-1200:])
    return json.loads(result.stdout)


def _no_window():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def _seconds(info):
    value = float(info.get('format', {}).get('duration') or 0)
    return _number(value, .001, 86400, 'Media duration')


def _rate(value):
    from fractions import Fraction
    try:
        result = Fraction(str(value))
        if not 1 <= float(result) <= 60:
            raise ValueError()
        return result
    except (ValueError, ZeroDivisionError):
        raise ValueError('Lip sync needs a frame rate between 1 and 60 FPS.') from None


def _video_geometry(stream):
    width, height = int(stream['width']), int(stream['height'])
    rotation = stream.get('tags', {}).get('rotate', 0)
    for entry in stream.get('side_data_list', []):
        if 'rotation' in entry:
            rotation = entry['rotation']
            break
    rotation = _number(float(rotation), -36000, 36000, 'Video rotation')
    nearest = round(rotation / 90) * 90
    if abs(rotation - nearest) > .01:
        raise ValueError('Normalise non-right-angle video rotation before selecting a lip-sync face.')
    if nearest % 180:
        width, height = height, width
    fps = None
    for candidate in (stream.get('avg_frame_rate'), stream.get('r_frame_rate')):
        try:
            fps = _rate(candidate)
            break
        except ValueError:
            pass
    if fps is None:
        raise ValueError('Lip sync needs a known supported video frame rate.')
    return width, height, fps, nearest % 360


def _run(args, settings, *, timeout=86400):
    result = subprocess.run([_program('ffmpeg', settings), '-nostdin', '-hide_banner', '-loglevel', 'error',
                             '-n', *args], capture_output=True, timeout=timeout, **_no_window())
    if result.returncode:
        raise RuntimeError('Media conversion failed: ' + result.stderr.decode('utf-8', 'replace')[-1800:])


class Cancellation:
    def __init__(self, marker=None):
        self.event = threading.Event()
        self.marker = Path(marker) if marker else None

    def check(self):
        if self.event.is_set() or self.marker and self.marker.exists():
            raise InterruptedError('Processing cancelled. Original media and completed narration were preserved.')


def _drain(stream, output):
    try:
        while True:
            line = stream.readline()
            if not line:
                break
            output.append(line.decode('utf-8', 'replace'))
    except (OSError, ValueError):
        pass


def _stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def gpu_probe(device='auto', *, require_cuda=False):
    """Actual CUDA allocation/matmul, never a documentation-based GPU claim."""
    import torch
    report = {'torch': torch.__version__, 'cuda_build': torch.version.cuda,
              'cuda_available': bool(torch.cuda.is_available()), 'device': 'cpu',
              'kernel_test': 'not_run', 'rtx_5080_validated': False}
    if device not in {'auto', 'cpu', 'cuda'}:
        raise ValueError('Choose Auto, CPU or CUDA.')
    if device == 'cpu':
        return report
    if report['cuda_available']:
        try:
            report['name'] = torch.cuda.get_device_name(0)
            capability = torch.cuda.get_device_capability(0)
            report['compute_capability'] = list(capability)
            report['compiled_architectures'] = torch.cuda.get_arch_list()
            a = torch.ones((64, 64), device='cuda', dtype=torch.float32)
            result = a @ a
            torch.cuda.synchronize()
            if not bool(torch.isfinite(result).all()) or float(result[0, 0].item()) != 64:
                raise RuntimeError('The CUDA test did not return the expected finite result.')
            del a, result
            free, total = torch.cuda.mem_get_info(0)
            report.update(device='cuda', kernel_test='passed', free_bytes=free, total_bytes=total)
        except Exception as exc:
            report.update(kernel_test='failed', reason=str(exc)[-1200:])
    else:
        report['reason'] = 'CUDA is unavailable in this isolated runtime or the NVIDIA driver is incompatible.'
    if (device == 'cuda' or require_cuda) and report['device'] != 'cuda':
        raise RuntimeError('The CUDA compatibility test failed. Repair the isolated component or choose CPU. '
                           + report.get('reason', ''))
    return report


def _part(request, name):
    paths = request.get('model_paths', {})
    value = paths.get(name)
    if not value:
        root = request.get('model_dir') or request.get('root')
        if not root:
            raise ValueError('A verified installed model bundle is required.')
        value = Path(root) / 'parts' / name
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError('The installed model part is missing or is a link: ' + name)
    return path.resolve()


def _tensor_state(path):
    import torch
    if path.suffix == '.safetensors':
        from safetensors.torch import load_file
        value = load_file(str(path), device='cpu')
    else:
        # No user/model-host classes or globals are allowlisted for deserialization.
        value = torch.load(path, map_location='cpu', weights_only=True)
    if not isinstance(value, dict) or not value or not all(isinstance(k, str) and isinstance(v, torch.Tensor) for k, v in value.items()):
        raise ValueError('The checkpoint must contain a plain tensor state dictionary.')
    return value


def _resize(array, width, height):
    """Vectorised bilinear RGB/BGR resize, keeping normal tests lightweight."""
    import numpy as np
    if array.ndim != 3 or array.shape[2] != 3 or min(array.shape[:2]) < 1:
        raise ValueError('Expected a three-channel image.')
    if not 1 <= width <= 16384 or not 1 <= height <= 16384:
        raise ValueError('Invalid image dimensions.')
    ys = np.linspace(0, array.shape[0] - 1, height)
    xs = np.linspace(0, array.shape[1] - 1, width)
    yi, xi = ys.astype(int), xs.astype(int)
    yn, xn = np.minimum(yi + 1, array.shape[0] - 1), np.minimum(xi + 1, array.shape[1] - 1)
    wy, wx = (ys - yi)[:, None, None], (xs - xi)[None, :, None]
    result = ((1 - wy) * ((1 - wx) * array[yi[:, None], xi] + wx * array[yi[:, None], xn])
              + wy * ((1 - wx) * array[yn[:, None], xi] + wx * array[yn[:, None], xn]))
    return np.clip(result, 0, 255).astype('uint8')


def _pixel_box(box, normalized, width, height):
    if len(box) != 4:
        raise ValueError('A face box needs x, y, width and height.')
    x, y, w, h = [float(v) for v in box]
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        raise ValueError('Face coordinates must be finite.')
    if normalized:
        x, w = x * width, w * width
        y, h = y * height, h * height
    x, y, w, h = round(x), round(y), round(w), round(h)
    if min(x, y) < 0 or min(w, h) < 16 or x + w > width or y + h > height:
        raise ValueError('The selected face box is too small or lies outside the video.')
    return (x, y, w, h)


def _iou(a, b):
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    return intersection / max(1, a[2] * a[3] + b[2] * b[3] - intersection)


class FaceTracker:
    """A selected spatial track, not facial identity or speaker recognition."""
    def __init__(self, options, width, height):
        import cv2
        self.cv2, self.width, self.height = cv2, width, height
        self.options, self.previous_gray, self.box = options, None, None
        self.identity, self.previous_mouth = None, None
        self.detector = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / 'haarcascade_frontalface_default.xml'))
        if self.detector.empty():
            raise RuntimeError('The local face detector is unavailable. Repair the optional lip-sync component.')

    def locate(self, frame, region):
        import numpy as np
        cv2 = self.cv2
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        face_id = region.get('face_id') or self.options.get('face_id')
        face = next((f for f in self.options.get('faces', []) if f['id'] == face_id), None)
        if face is None or face.get('consent') is not True:
            raise ValueError('The active scene has no approved face selection.')
        manual = region if 'box' in region else face
        identity = (face_id, region.get('start'), region.get('end'), tuple(manual['box'])) if manual else ('auto',)
        changed = identity != self.identity
        if manual and changed:
            self.box = _pixel_box(manual['box'], manual.get('normalized', False), self.width, self.height)
        elif self.box and self.previous_gray is not None:
            x, y, w, h = self.box
            mask = np.zeros_like(gray)
            mask[y:y + h, x:x + w] = 255
            points = cv2.goodFeaturesToTrack(self.previous_gray, maxCorners=60, qualityLevel=.02, minDistance=5, mask=mask)
            if points is not None and len(points) >= 6:
                next_points, status, _ = cv2.calcOpticalFlowPyrLK(self.previous_gray, gray, points, None)
                valid = status.reshape(-1).astype(bool) if status is not None else np.zeros(len(points), dtype=bool)
                if valid.sum() >= 6:
                    movement = np.median(next_points[valid] - points[valid], axis=0).reshape(-1)
                    dx, dy = movement
                    if math.isfinite(float(dx)) and math.isfinite(float(dy)) and abs(dx) < w / 3 and abs(dy) < h / 3:
                        self.box = (int(max(0, min(self.width - w, x + round(float(dx))))),
                                    int(max(0, min(self.height - h, y + round(float(dy))))), w, h)
                    else:
                        self.box = None
                else:
                    self.box = None
            elif not manual:
                self.box = None
        candidates = []
        if not manual:
            candidates = [tuple(map(int, b)) for b in self.detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                                                                   minSize=(32, 32))]
            if self.box:
                matching = [b for b in candidates if _iou(b, self.box) >= .3]
                self.box = max(matching, key=lambda b: _iou(b, self.box)) if matching else None
            elif len(candidates) == 1:
                self.box = candidates[0]
            # Multiple faces without a selected spatial track are not guessed.
        motion = None
        if self.box:
            x, y, w, h = self.box
            mouth = _resize(frame[y + h // 2:y + h, x:x + w], 64, 32).astype('float32')
            if self.previous_mouth is not None and not changed:
                motion = float(np.mean(np.abs(mouth - self.previous_mouth)) / 255)
            self.previous_mouth = mouth
        else:
            self.previous_mouth = None
        self.previous_gray, self.identity = gray, identity
        return self.box, motion


class WhisperConditioner:
    def __init__(self, path, audio, torch, device, dtype):
        import soundfile as sf
        from transformers import WhisperFeatureExtractor, WhisperModel
        self.torch, self.device, self.dtype = torch, device, dtype
        self.extractor = WhisperFeatureExtractor.from_pretrained(str(path), local_files_only=True)
        self.model = WhisperModel.from_pretrained(str(path), local_files_only=True,
                                                  use_safetensors=True, torch_dtype=dtype).to(device).eval()
        self.model.requires_grad_(False)
        self.audio = sf.SoundFile(audio)
        if self.audio.samplerate != 16000 or self.audio.channels != 1:
            raise ValueError('Internal lip-sync audio must be mono 16 kHz.')
        self.samples, self.cache = len(self.audio), OrderedDict()

    def _chunk(self, chunk):
        torch = self.torch
        if chunk in self.cache:
            self.cache.move_to_end(chunk)
            return self.cache[chunk]
        self.audio.seek(chunk * 480000)
        signal = self.audio.read(480000, dtype='float32')
        features = self.extractor(signal, sampling_rate=16000, return_tensors='pt').input_features.to(self.device, dtype=self.dtype)
        with torch.inference_mode():
            hidden = self.model.encoder(features, output_hidden_states=True).hidden_states
            value = torch.stack(hidden, dim=2)[0]
        if value.shape[1:] != (5, 384):
            raise RuntimeError('The installed Whisper encoder is not the MuseTalk Tiny conditioning architecture.')
        self.cache[chunk] = value
        while len(self.cache) > 3:
            self.cache.popitem(last=False)
        return value

    def batch(self, times):
        torch = self.torch
        values = []
        actual = math.floor(self.samples / 16000 * 50)
        for at in times:
            center = math.floor(at * 50)
            rows = []
            for index in range(center - 4, center + 6):
                if index < 0 or index >= actual:
                    rows.append(torch.zeros((5, 384), device=self.device, dtype=self.dtype))
                else:
                    rows.append(self._chunk(index // 1500)[index % 1500])
            values.append(torch.stack(rows).reshape(50, 384))
        return torch.stack(values)

    def close(self):
        self.audio.close()


class ModelBundle:
    def __init__(self, request, audio):
        import torch
        from diffusers import AutoencoderKL, UNet2DConditionModel
        self.torch = torch
        self.gpu = gpu_probe(request.get('device', 'auto'))
        self.device = self.gpu['device']
        self.dtype = torch.float16 if self.device == 'cuda' else torch.float32
        muse, vae_path, whisper_path = [_part(request, part) for part in ('musetalk', 'vae', 'whisper')]
        config_path, weights = muse / 'musetalkV15/musetalk.json', muse / 'musetalkV15/unet.pth'
        config = json.loads(_file(config_path).read_text())
        self.unet = UNet2DConditionModel(**config)
        self.unet.load_state_dict(_tensor_state(_file(weights)), strict=True)
        self.unet.to(self.device, dtype=self.dtype).eval().requires_grad_(False)
        vae_config = json.loads(_file(vae_path / 'config.json').read_text())
        self.vae = AutoencoderKL.from_config(vae_config)
        safe = vae_path / 'diffusion_pytorch_model.safetensors'
        self.vae.load_state_dict(_tensor_state(_file(safe if safe.is_file() else vae_path / 'diffusion_pytorch_model.bin')), strict=True)
        self.vae.to(self.device, dtype=self.dtype).eval().requires_grad_(False)
        self.conditioner = WhisperConditioner(whisper_path, audio, torch, self.device, self.dtype)
        position = torch.arange(50, dtype=torch.float32).unsqueeze(1)
        frequencies = torch.exp(torch.arange(0, 384, 2, dtype=torch.float32) * (-math.log(10000.0) / 384))
        encoding = torch.zeros(50, 384, dtype=torch.float32)
        encoding[:, 0::2] = torch.sin(position * frequencies)
        encoding[:, 1::2] = torch.cos(position * frequencies)
        self.position = encoding.unsqueeze(0).to(self.device, dtype=self.dtype)
        self.generator = torch.Generator(device=self.device).manual_seed(0)
        if self.device == 'cuda':
            torch.cuda.reset_peak_memory_stats(0)

    def safe_batch(self, requested, reserve_mb):
        if self.device != 'cuda':
            return min(requested, 2)
        free, _ = self.torch.cuda.mem_get_info(0)
        available = free - reserve_mb * 1024 * 1024
        count = min(requested, max(0, int(available // (512 * 1024 * 1024))))
        if count < 1:
            raise RuntimeError('Insufficient free VRAM after loading MuseTalk. Close other GPU work or choose CPU; narration is preserved.')
        return count

    def render(self, crops, times):
        import numpy as np
        torch = self.torch
        pixels = np.stack([_resize(crop, 256, 256)[..., ::-1].copy() for crop in crops])
        image = torch.from_numpy(pixels).permute(0, 3, 1, 2).to(self.device, dtype=self.dtype) / 255
        masked = image.clone()
        masked[:, :, 128:, :] = 0
        with torch.inference_mode():
            scaling = float(self.vae.config.scaling_factor)
            first = self.vae.encode(masked * 2 - 1).latent_dist.sample(generator=self.generator) * scaling
            second = self.vae.encode(image * 2 - 1).latent_dist.sample(generator=self.generator) * scaling
            latents = torch.cat([first, second], dim=1)
            audio = self.conditioner.batch(times) + self.position
            predicted = self.unet(latents, torch.tensor([0], device=self.device), encoder_hidden_states=audio).sample
            decoded = self.vae.decode(predicted / scaling).sample
            if not bool(torch.isfinite(decoded).all()):
                raise RuntimeError('MuseTalk returned nonfinite video pixels.')
            decoded = ((decoded / 2 + .5).clamp(0, 1) * 255).round().byte().permute(0, 2, 3, 1).cpu().numpy()
        return [frame[..., ::-1].copy() for frame in decoded]

    def metrics(self):
        result = {'gpu_used': self.device == 'cuda', 'device': self.device, 'gpu_compatibility': self.gpu}
        if self.device == 'cuda':
            self.torch.cuda.synchronize()
            free, total = self.torch.cuda.mem_get_info(0)
            result.update(gpu_peak_allocated_mb=self.torch.cuda.max_memory_allocated(0) / 1024 ** 2,
                          gpu_peak_reserved_mb=self.torch.cuda.max_memory_reserved(0) / 1024 ** 2,
                          gpu_free_mb=free / 1024 ** 2, gpu_total_mb=total / 1024 ** 2,
                          gpu_name=self.torch.cuda.get_device_name(0))
        return result

    def close(self):
        self.conditioner.close()


def _blend(frame, patch, box):
    import numpy as np
    x, y, width, height = box
    generated = _resize(patch, width, height).astype('float32')
    yy, xx = np.mgrid[0:height, 0:width]
    yy, xx = (yy + .5) / height, (xx + .5) / width
    mask = np.clip((1 - ((xx - .5) / .58) ** 2 - ((yy - .63) / .6) ** 2) * 3, 0, 1)
    mask *= np.clip((yy - .28) / .16, 0, 1)
    mask = (mask * mask * (3 - 2 * mask))[..., None]
    result = frame.copy()
    result[y:y + height, x:x + width] = np.clip(frame[y:y + height, x:x + width] * (1 - mask) + generated * mask, 0, 255).astype('uint8')
    return result


def _region(at, options):
    if options.get('speech_windows') and not any(w['start'] <= at < w['end'] for w in options['speech_windows']):
        return None
    selected = next((r for r in options.get('regions', []) if r['start'] <= at < r['end']), None)
    if options.get('mode') == 'selected':
        return selected
    return selected if selected is not None else {}


def lipsync(request, cancellation):
    import numpy as np
    source, audio = _file(request.get('source')), _file(request.get('audio'))
    output = Path(request.get('output', '')).expanduser()
    if not output.is_absolute() or output.exists() or output.resolve() in {source, audio}:
        raise ValueError('Lip sync requires a new local output file.')
    settings, options = request.get('settings', {}), copy.deepcopy(request.get('options', {}))
    if options.get('mode') not in {'automatic', 'selected', 'full'} or options.get('consent') is not True:
        raise ValueError('Enable lip sync and confirm permission for the selected visible faces.')
    if not options.get('faces') or any(face.get('consent') is not True for face in options['faces']):
        raise ValueError('Permission is required for each selected face.')
    info = _probe(source, settings)
    streams = [v for v in info.get('streams', []) if v.get('codec_type') == 'video' and not v.get('disposition', {}).get('attached_pic')]
    if not streams:
        raise ValueError('A visible video stream is required.')
    stream = streams[0]
    width, height, fps, rotation = _video_geometry(stream)
    if not 16 <= width <= 16384 or not 16 <= height <= 16384:
        raise ValueError('The source resolution is outside the supported bounds.')
    duration = _seconds(info)
    start = _number(options.get('preview_start', 0), 0, duration, 'Preview start') if options.get('preview') else 0
    seconds = min(_number(options.get('preview_seconds', 5), 5, 10, 'Preview duration'), duration - start) if options.get('preview') else duration
    if seconds <= 0:
        raise ValueError('The preview lies outside the source video.')
    max_height = int(_number(options.get('max_height', 0), 0, 2160, 'Maximum height'))
    scale = min(1, max_height / height) if max_height else 1
    target_width, target_height = max(16, round(width * scale / 2) * 2), max(16, round(height * scale / 2) * 2)
    # Face coordinates describe the imported video, not a resized preview.
    for item in [*options.get('faces', []), *options.get('regions', [])]:
        if 'box' in item and not item.get('normalized'):
            item['box'] = [float(value) * scale for value in item['box']]
    width, height = target_width, target_height
    requested_batch = int(_number(options.get('batch_size', 1), 1, 4, 'Frame batch size'))
    reserve = int(_number(options.get('gpu_reserve_mb', 1024), 256, 16384, 'GPU reserve'))
    crf = int(_number(options.get('crf', 18), 14, 30, 'Video quality'))
    known_frames = int(stream.get('nb_frames') or 0) if str(stream.get('nb_frames') or '').isdigit() and not options.get('preview') else None
    warning = ['Review face edges and timing in the preview. Spatial tracking does not verify a person’s identity.']
    reported_rate = None
    with contextlib.suppress(ValueError):
        reported_rate = float(_rate(stream.get('r_frame_rate')))
    if reported_rate and abs(reported_rate - float(fps)) > .01:
        warning.append('Variable-frame-rate footage was rendered at its reported average frame rate for deterministic audio alignment.')
        known_frames = None
    output.parent.mkdir(parents=True, exist_ok=True)
    started, changed, seen, no_face = time.monotonic(), 0, 0, 0
    reader = writer = bundle = None
    with tempfile.TemporaryDirectory(prefix='.forge-lipsync-', dir=output.parent) as temporary:
        work = Path(temporary)
        pcm, rendered = work / 'voice.wav', work / ('rendered' + output.suffix)
        _run(['-protocol_whitelist', 'file,pipe', '-ss', str(start), '-i', str(audio), '-t', str(seconds),
              '-vn', '-ac', '1', '-ar', '16000', '-af', f'apad=whole_dur={seconds:.9f}', str(pcm)], settings)
        cancellation.check()
        emit(0, 'Loading the verified MuseTalk runtime', stage='Lip-sync model', progress_measured=False)
        try:
            bundle = ModelBundle(request, pcm)
            batch_size = bundle.safe_batch(requested_batch, reserve)
            tracker = FaceTracker(options, width, height)
            binary = _program('ffmpeg', settings)
            reader_errors, writer_errors = deque(maxlen=30), deque(maxlen=30)
            reader = subprocess.Popen([binary, '-nostdin', '-hide_banner', '-loglevel', 'error',
                '-protocol_whitelist', 'file,pipe', '-ss', str(start), '-i', str(source), '-t', str(seconds),
                '-map', f'0:{stream["index"]}', '-an', '-sn', '-vf', f'fps={fps},scale={width}:{height}',
                '-pix_fmt', 'bgr24', '-f', 'rawvideo', 'pipe:1'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_no_window())
            writer = subprocess.Popen([binary, '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
                '-f', 'rawvideo', '-pixel_format', 'bgr24', '-video_size', f'{width}x{height}', '-framerate', str(fps), '-i', 'pipe:0',
                '-protocol_whitelist', 'file,pipe', '-ss', str(start), '-i', str(audio),
                '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264', '-preset', 'medium', '-crf', str(crf),
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-af', f'apad=whole_dur={seconds:.9f}',
                '-t', str(seconds), '-map_metadata', '-1', str(rendered)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, **_no_window())
            threading.Thread(target=_drain, args=(reader.stderr, reader_errors), daemon=True).start()
            threading.Thread(target=_drain, args=(writer.stderr, writer_errors), daemon=True).start()
            pending = []

            def flush():
                nonlocal changed
                ready = [item for item in pending if item['box'] is not None]
                if ready:
                    cancellation.check()
                    predictions = bundle.render([item['crop'] for item in ready], [item['time'] for item in ready])
                    if len(predictions) != len(ready):
                        raise RuntimeError('The lip-sync model returned an incomplete frame batch.')
                    for item, patch in zip(ready, predictions):
                        item['frame'] = _blend(item['frame'], patch, item['box'])
                        changed += 1
                for item in pending:
                    cancellation.check()
                    writer.stdin.write(item['frame'].tobytes())
                pending.clear()

            while True:
                cancellation.check()
                raw = reader.stdout.read(width * height * 3)
                if not raw:
                    break
                if len(raw) != width * height * 3:
                    raise RuntimeError('Video decoding ended in an incomplete frame.')
                frame = np.frombuffer(raw, dtype='uint8').reshape(height, width, 3).copy()
                local_time = seen / float(fps)
                region = _region(start + local_time, options)
                box = None
                if region is not None:
                    box, motion = tracker.locate(frame, region)
                    if options['mode'] == 'automatic' and (motion is None or motion < options.get('mouth_motion_threshold', .004)):
                        box = None
                    if box is None:
                        no_face += 1
                pending.append({'frame': frame, 'box': box, 'time': local_time,
                                'crop': frame[box[1]:box[1] + box[3], box[0]:box[0] + box[2]].copy() if box else None})
                seen += 1
                if len(pending) >= batch_size:
                    flush()
                if seen % max(1, round(float(fps))) == 0:
                    elapsed = max(.001, time.monotonic() - started)
                    speed = seen / elapsed
                    emit(100 * seen / known_frames if known_frames else 0, f'{seen} frames processed',
                         stage='Lip sync', progress_measured=bool(known_frames), frames_processed=seen,
                         frames_total=known_frames, speed=f'{speed:.2f} fps',
                         eta_seconds=max(0, known_frames - seen) / speed if known_frames else None)
            flush()
            writer.stdin.close()
            if reader.wait(timeout=30):
                raise RuntimeError('Video decoding failed: ' + ''.join(reader_errors)[-1600:])
            if writer.wait(timeout=180):
                raise RuntimeError('Video encoding failed: ' + ''.join(writer_errors)[-1600:])
            if not changed:
                raise RuntimeError('No suitable permitted face frames were found. Review scene/face selection or use audio-only dubbing.')
            result_info = _probe(rendered, settings)
            actual = _seconds(result_info)
            if not any(s.get('codec_type') == 'video' for s in result_info.get('streams', [])) or abs(actual - seconds) > max(.25, 2 / float(fps)):
                raise RuntimeError('Rendered video verification failed or its duration drifted.')
            cancellation.check()
            # Same-volume no-replace publication; this cannot replace source or
            # an export that appeared after the initial existence check.
            if os.name == 'nt':
                rendered.rename(output)
            else:
                os.link(rendered, output)
                rendered.unlink()
            metrics = {**bundle.metrics(), 'elapsed_seconds': time.monotonic() - started,
                       'frames_processed': seen, 'frames_changed': changed, 'frames_without_suitable_face': no_face,
                       'fps': float(fps), 'frame_batch_size': batch_size, 'width': width, 'height': height,
                       'source_rotation': rotation}
            if no_face:
                warning.append(f'{no_face} frames without a suitable selected speaking face were preserved without model edits.')
            return {'path': str(output), 'duration': actual, 'model_inference': True, 'inference_tested': True,
                    'actual_device': metrics['device'], 'frames_changed': changed,
                    'warnings': warning, 'metrics': metrics}
        finally:
            _stop(reader)
            _stop(writer)
            if bundle is not None:
                bundle.close()


def separate(request, cancellation):
    """Chunked official HDEMUCS architecture with strict tensor-only weights."""
    import numpy as np
    import soundfile as sf
    import torch
    from torchaudio.models import hdemucs_high
    source = _file(request.get('source'))
    output = Path(request.get('output_dir', '')).expanduser()
    if not output.is_absolute():
        raise ValueError('An owned local staging directory is required.')
    output.mkdir(parents=True, exist_ok=True)
    targets = {name: output / (name + '.wav') for name in ('vocals', 'background')}
    if any(path.exists() or path.resolve() == source for path in targets.values()):
        raise ValueError('Existing audio and source files cannot be overwritten.')
    settings, options = request.get('settings', {}), request.get('options', {})
    seconds = _seconds(_probe(source, settings))
    chunk_seconds = _number(options.get('chunk_seconds', 10), 4, 20, 'Separation chunk size')
    overlap_seconds = _number(options.get('overlap_seconds', 1), .25, 2, 'Separation overlap')
    if overlap_seconds * 2 >= chunk_seconds:
        raise ValueError('Separation overlap must be shorter than half the chunk.')
    root = _part(request, 'weights')
    weight = root / 'hdemucs_high_trained.pt'
    if not weight.is_file():
        weight = root / 'weights/hdemucs_high_trained.pt'
    model = hdemucs_high(sources=['drums', 'bass', 'other', 'vocals'])
    model.load_state_dict(_tensor_state(_file(weight)), strict=True)
    gpu = gpu_probe(request.get('device', 'auto'))
    device = gpu['device']
    model.to(device).eval().requires_grad_(False)
    if device == 'cuda':
        reserve = _number(options.get('gpu_reserve_mb', 1024), 256, 16384, 'GPU reserve')
        if torch.cuda.mem_get_info(0)[0] < reserve * 1024 ** 2 + 1024 ** 3:
            raise RuntimeError('Insufficient free VRAM for source separation. Close other GPU jobs or choose CPU.')
        torch.cuda.reset_peak_memory_stats(0)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='.forge-stems-', dir=output) as temporary:
        pcm = Path(temporary) / 'source.wav'
        _run(['-protocol_whitelist', 'file,pipe', '-i', str(source), '-vn', '-ar', '44100', '-ac', '2',
              '-c:a', 'pcm_f32le', str(pcm)], settings)
        with sf.SoundFile(pcm) as original, contextlib.ExitStack() as stack:
            temporary_targets = {name: Path(temporary) / (name + '.wav') for name in targets}
            writers = {name: stack.enter_context(sf.SoundFile(path, mode='w', samplerate=44100, channels=2, subtype='PCM_24'))
                       for name, path in temporary_targets.items()}
            count, chunk, overlap = len(original), round(chunk_seconds * 44100), round(overlap_seconds * 44100)
            for start in range(0, count, chunk):
                cancellation.check()
                left, right = max(0, start - overlap), min(count, start + chunk + overlap)
                original.seek(left)
                audio = original.read(right - left, dtype='float32', always_2d=True)
                tensor = torch.from_numpy(audio.T).unsqueeze(0).to(device)
                mean = tensor.mean(dim=(1, 2), keepdim=True)
                std = tensor.std(dim=(1, 2), keepdim=True).clamp_min(1e-8)
                with torch.inference_mode():
                    estimates = model((tensor - mean) / std) * std.unsqueeze(1)
                if not bool(torch.isfinite(estimates).all()):
                    raise RuntimeError('Source separation returned nonfinite audio.')
                # Mixture mean belongs to the reconstruction, not independently
                # to every stem. Residual background preserves exact summation.
                vocals = estimates[0, 3].cpu().numpy().T
                offset, keep = start - left, min(chunk, count - start)
                voice = vocals[offset:offset + keep]
                raw = audio[offset:offset + keep]
                writers['vocals'].write(voice)
                writers['background'].write(raw - voice)
                completed = start + keep
                elapsed = max(.001, time.monotonic() - started)
                emit(100 * completed / count, 'Separating local audio stems', stage='Audio separation',
                     progress_measured=True, samples_processed=completed, samples_total=count,
                     eta_seconds=(count - completed) / (completed / elapsed))
        cancellation.check()
        for name, target in targets.items():
            info = _probe(temporary_targets[name], settings)
            if abs(_seconds(info) - seconds) > .1:
                raise RuntimeError('A separated stem changed the original timeline.')
        for name, target in targets.items():
            if os.name == 'nt':
                temporary_targets[name].rename(target)
            else:
                os.link(temporary_targets[name], target)
                temporary_targets[name].unlink()
    metrics = {'elapsed_seconds': time.monotonic() - started, 'gpu_used': device == 'cuda', 'device': device,
               'gpu_compatibility': gpu}
    if device == 'cuda':
        metrics['gpu_peak_allocated_mb'] = torch.cuda.max_memory_allocated(0) / 1024 ** 2
    return {'vocals': str(targets['vocals']), 'background': str(targets['background']),
            'duration': seconds, 'model_inference': True, 'inference_tested': True,
            'actual_device': device, 'warnings': [], 'metrics': metrics}


def run(request):
    # Do not let a model library fetch a newer revision during inference.
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    cancellation = Cancellation(request.get('cancel_file'))
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, lambda *_: cancellation.event.set())
    task = request.get('task')
    if task == 'probe':
        return {'status': 'passed', **gpu_probe(request.get('device', 'auto')),
                'model_inference': False, 'inference_tested': False,
                'note': 'CUDA kernel compatibility is distinct from model/owner-hardware acceptance.'}
    if task == 'lipsync':
        return lipsync(request, cancellation)
    if task == 'separate':
        return separate(request, cancellation)
    if task == 'test':
        # The caller supplies a permitted short fixture and receives a real
        # rendered clip. A mere import test is never model inference evidence.
        if request.get('source') and request.get('audio') and request.get('output'):
            request = {**request, 'options': {**request.get('options', {}), 'preview': True}}
            return lipsync(request, cancellation)
        raise ValueError('A model test requires a permitted source clip, narration and new preview destination.')
    raise ValueError('Unsupported visual/audio runtime task.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--response', type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding='utf-8'))
    result = run(request)
    destination = args.response or request.get('response_path') or request.get('response')
    if destination:
        path = Path(destination)
        if not path.is_absolute() or path.exists():
            raise ValueError('A new owned response file is required.')
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.tmp')
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.rename(path)
    print(json.dumps({'kind': 'result', **result}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'kind': 'error', 'message': str(exc)[-2500:]}), flush=True)
        raise SystemExit(1)
