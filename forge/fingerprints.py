"""Bounded, local signal descriptors. Scores are measurements, never tracking guarantees.

No automatic reference index is maintained. Raw descriptors are exported only when
retain_reference is explicitly selected. Analysis and comparison are also usable
in memory by the inspection and verification modules.
"""
from __future__ import annotations

import array
import base64
import copy
import hashlib
import html
import io
import json
import math
import struct
import subprocess
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .core import executable, no_window, safe_name

AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.wma', '.aif', '.aiff', '.mka'}
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.mpeg', '.mpg', '.wmv', '.ts', '.mts', '.m2ts'}
DEMUXERS = 'mp3,wav,flac,mov,matroska,ogg,aac,asf,aiff,avi,mpeg,mpegts'
METHOD_VERSION = 1
SAMPLE_RATE = 8000
AUDIO_LIMITATIONS = [
    'Fingerprints describe the decoded signal; removing metadata does not remove these signal characteristics.',
    'Only the first audio stream and the configured initial time window are analyzed, downmixed to mono.',
    'FORGE spectral plots and profiles use an 8 kHz mono decode (0–4 kHz); higher frequencies and stereo spatial information are not represented.',
    'Similarity is descriptor agreement, not a probability of identity or a prediction of proprietary recognition services.',
    'Silence, short tones, repeated patterns, edits, time shifts, pitch changes and unrelated recordings can confound comparisons.',
]
VIDEO_LIMITATIONS = [
    'Only selected frames of the first video stream within the configured initial time window are sampled.',
    'Timestamps indicate requested sample positions; the decoder returns the nearest available frame at or after that position.',
    'Matching sampled frames do not establish that unsampled frames or entire videos match.',
    'Hashes and color summaries can collide, particularly for static, dark, simple or repeated scenes; scores do not predict proprietary recognition.',
]


class AudioFingerprintOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    max_seconds: float = Field(120, ge=15, le=600, allow_inf_nan=False)
    report_json: bool = True
    report_html: bool = True
    retain_reference: bool = False


class VideoFingerprintOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sample_frames: int = Field(12, ge=2, le=48)
    max_seconds: float = Field(300, ge=1, le=1800, allow_inf_nan=False)
    selected_times: list[float] = Field(default_factory=list, max_length=48)
    report_json: bool = True
    report_html: bool = True
    retain_reference: bool = False

    @field_validator('selected_times')
    @classmethod
    def times(cls, values):
        if any(not math.isfinite(v) or v < 0 or v > 1800 for v in values):
            raise ValueError('Frame timestamps must be finite and between 0 and 1,800 seconds.')
        return sorted(set(values))


def _capture(command: list[str], *, max_bytes: int, timeout: float = 90) -> bytes:
    """Drain both pipes with hard output limits and kill on timeout/overflow."""
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, **no_window()) as process:
        output, error = bytearray(), bytearray()
        overflow = threading.Event()

        def read(stream, target, limit):
            while True:
                block = stream.read(65536)
                if not block:
                    break
                if len(target) + len(block) > limit:
                    overflow.set()
                    process.kill()
                    break
                target.extend(block)

        threads = [threading.Thread(target=read, args=(process.stdout, output, max_bytes), daemon=True),
                   threading.Thread(target=read, args=(process.stderr, error, 262144), daemon=True)]
        for thread in threads:
            thread.start()
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            raise RuntimeError('Media analysis exceeded its bounded processing time.') from exc
        finally:
            for thread in threads:
                thread.join(timeout=3)
        if overflow.is_set():
            raise RuntimeError('Media analysis exceeded its bounded output size.')
        if code:
            # Reports must not echo potentially private paths from decoder stderr.
            raise RuntimeError('The local media decoder could not inspect or decode this file.')
        return bytes(output)


def _path(path: Path) -> Path:
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() not in AUDIO_EXTENSIONS | VIDEO_EXTENSIONS:
        raise ValueError('Choose a supported local audio or video file; playlists and network media are not accepted.')
    if not path.is_file():
        raise ValueError('The selected local media file no longer exists.')
    if str(path).startswith(('\\\\', '//')):
        raise ValueError('Network paths are not enabled for media analysis.')
    return path


def _binary(name: str, settings: dict) -> str:
    result = executable(name, settings)
    if not result:
        raise RuntimeError(f'{name} is required for local fingerprint analysis. Configure it in Environment.')
    return result


def _input(path: Path) -> list[str]:
    # No HTTP, concat, subfile, HLS, image sequences or device demuxers.
    return ['-protocol_whitelist', 'file,pipe', '-format_whitelist', DEMUXERS, '-i', str(path)]


def _probe(path: Path, settings: dict) -> dict:
    command = [_binary('ffprobe', settings), '-v', 'error', '-protocol_whitelist', 'file,pipe',
               '-format_whitelist', DEMUXERS, '-show_format', '-show_streams', '-of', 'json', str(path)]
    try:
        return json.loads(_capture(command, max_bytes=4 * 1024 * 1024, timeout=45))
    except (ValueError, TypeError) as exc:
        raise RuntimeError('FFprobe returned an invalid inspection result.') from exc


def _number(value, fallback=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else fallback
    except (TypeError, ValueError):
        return fallback


def _duration(info, stream):
    value = _number(info.get('format', {}).get('duration')) or _number(stream.get('duration'))
    if not value:
        raise ValueError('This media has no usable duration; bounded sampling is unavailable.')
    return value


def _ffmpeg(settings):
    return [_binary('ffmpeg', settings), '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1', '-filter_threads', '1']


def _png(image: Image.Image) -> str:
    output = io.BytesIO()
    image.save(output, format='PNG', optimize=True)
    return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')


@lru_cache(maxsize=4)
def _fft_constants(n):
    bits = int(math.log2(n))
    reverse = [int(f'{i:0{bits}b}'[::-1], 2) for i in range(n)]
    phases = {size: [complex(math.cos(-2 * math.pi * k / size), math.sin(-2 * math.pi * k / size))
                     for k in range(size // 2)] for size in (2 ** e for e in range(1, bits + 1))}
    return reverse, phases


def _power_spectrum(samples):
    n = len(samples)
    reverse, phases = _fft_constants(n)
    signal = [complex(samples[i] * (0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)))) for i in range(n)]
    values = [signal[i] for i in reverse]
    for size, factors in phases.items():
        for start in range(0, n, size):
            for index, factor in enumerate(factors):
                left, right = values[start + index], values[start + index + size // 2] * factor
                values[start + index], values[start + index + size // 2] = left + right, left - right
    return [(v.real * v.real + v.imag * v.imag) for v in values[:n // 2]]


def _spectral_features(samples):
    size, windows = 512, min(160, max(1, len(samples) // 512))
    spectra, temporal = [], []
    for i in range(windows):
        start = round(i * max(0, len(samples) - size) / max(1, windows - 1))
        block = list(samples[start:start + size])
        block += [0] * (size - len(block))
        powers = _power_spectrum(block)
        bands = [sum(powers[j:j + 8]) for j in range(0, len(powers), 8)]
        total = sum(bands)
        normalized = [p / total if total else 0.0 for p in bands]
        spectra.append(powers)
        temporal.append(normalized)
    average = [sum(spectrum[i] for spectrum in spectra) / len(spectra) for i in range(size // 2)]
    bands = [sum(average[j:j + 8]) for j in range(0, len(average), 8)]
    total = sum(bands)
    signature = [round(p / total, 8) if total else 0.0 for p in bands]
    maximum = max(average) or 1
    peaks = sorted(range(1, len(average)), key=average.__getitem__, reverse=True)[:8]
    frequency_peaks = [{'hz': round(i * SAMPLE_RATE / size, 2), 'relative_power': round(average[i] / maximum, 5)} for i in peaks]
    spec = Image.new('RGB', (len(spectra), 128))
    max_log = math.log10(max(max(s) for s in spectra) + 1) or 1
    for x, spectrum in enumerate(spectra):
        for y in range(128):
            amount = max(0.0, min(1.0, math.log10(sum(spectrum[y * 2:y * 2 + 2]) + 1) / max_log))
            spec.putpixel((x, 127 - y), (round(210 * amount), round(160 * amount), round(24 + 60 * amount)))
    spec = spec.resize((640, 256), Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(spec)
    draw.text((5, 5), '4 kHz', fill='white')
    draw.text((5, 240), '0 Hz  |  sampled windows across analyzed duration', fill='white')
    chart = Image.new('RGB', (640, 220), '#191919')
    draw = ImageDraw.Draw(chart)
    points = [(round(i * 620 / 255) + 10, 195 - round(175 * math.sqrt(power / maximum))) for i, power in enumerate(average)]
    draw.line(points, fill='#caa24e', width=2)
    draw.text((10, 200), '0 Hz', fill='white')
    draw.text((560, 200), '4 kHz', fill='white')
    return signature, temporal, frequency_peaks, {'spectrogram': _png(spec), 'frequency': _png(chart)}


@lru_cache(maxsize=16)
def _chromaprint_available(binary: str) -> bool:
    try:
        result = _capture([binary, '-hide_banner', '-muxers'], max_bytes=262144, timeout=15)
        return any('chromaprint' in line.split() for line in result.decode('utf-8', 'replace').splitlines())
    except RuntimeError:
        return False


def analyze_audio(path: Path, settings: dict, options: dict | None = None) -> dict:
    config = AudioFingerprintOptions.model_validate(options or {})
    source = _path(path)
    info = _probe(source, settings)
    stream = next((s for s in info.get('streams', []) if s.get('codec_type') == 'audio'), None)
    if stream is None:
        raise ValueError('The selected file contains no audio stream.')
    duration = _duration(info, stream)
    limit = min(duration, config.max_seconds)
    pcm = _capture(_ffmpeg(settings) + _input(source) + ['-map', f'0:{int(stream["index"])}', '-vn', '-sn', '-dn',
                   '-t', str(limit), '-ac', '1', '-ar', str(SAMPLE_RATE), '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1'],
                   max_bytes=math.ceil(limit * SAMPLE_RATE * 2) + 65536, timeout=min(180, 45 + limit / 2))
    if len(pcm) < 512:
        raise ValueError('The audio stream is too short to analyze.')
    samples = array.array('h')
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    if sys.byteorder != 'little':
        samples.byteswap()
    spectrum, temporal, peaks, visualizations = _spectral_features(samples)
    rms = math.sqrt(sum(v * v for v in samples) / len(samples)) / 32768
    chromaprint, chroma_status = [], 'unsupported: this FFmpeg build has no Chromaprint muxer'
    if _chromaprint_available(_binary('ffmpeg', settings)):
        try:
            raw = _capture(_ffmpeg(settings) + _input(source) + ['-map', f'0:{int(stream["index"])}', '-vn', '-sn', '-dn',
                           '-t', str(limit), '-ac', '1', '-ar', '11025', '-f', 'chromaprint', '-algorithm', '1', '-fp_format', 'raw', 'pipe:1'],
                           max_bytes=65536, timeout=min(180, 45 + limit / 2))
            chromaprint = list(struct.unpack('<' + 'I' * (len(raw) // 4), raw)) if raw and len(raw) % 4 == 0 else []
            chroma_status = 'calculated' if len(chromaprint) >= 8 else 'insufficient signal duration for a useful Chromaprint sequence'
        except RuntimeError:
            chroma_status = 'unavailable: the installed Chromaprint decoder did not produce a fingerprint'
    limitations = AUDIO_LIMITATIONS.copy()
    if rms < 0.0001:
        limitations.append('This analyzed interval is silent or nearly silent; acoustic similarity is not meaningful.')
    if len(set(chromaprint)) <= 2 and chromaprint:
        limitations.append('Chromaprint has little variation in this interval; spectral evidence is required and a match may be ambiguous.')
    return {
        'schema_version': METHOD_VERSION, 'kind': 'audio', 'name': source.name,
        'duration': round(duration, 6), 'analyzed_seconds': round(len(samples) / SAMPLE_RATE, 6),
        'codec': stream.get('codec_name', 'unknown'), 'channels': stream.get('channels'),
        'sample_rate': _number(stream.get('sample_rate')), 'rms_dbfs': round(20 * math.log10(max(rms, 1e-12)), 3),
        'analysis_sample_rate': SAMPLE_RATE, 'frequency_peaks': peaks, 'visualizations': visualizations,
        'fingerprint_method': 'Chromaprint algorithm 1 + FORGE spectral profile v1' if chromaprint else 'FORGE spectral profile v1',
        'chromaprint_status': chroma_status,
        'fingerprint': {'method_version': METHOD_VERSION, 'chromaprint': chromaprint, 'spectrum': spectrum,
                        'temporal_spectra': temporal, 'decoded_pcm_sha256': hashlib.sha256(pcm).hexdigest()},
        'limitations': limitations,
    }


def _cosine(a, b):
    magnitude = math.sqrt(sum(v * v for v in a) * sum(v * v for v in b))
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b)) / magnitude)) if magnitude else 0.0


def _chroma_similarity(a, b):
    if min(len(a), len(b)) < 8:
        return None
    best = 0.0
    max_shift = min(24, max(0, min(len(a), len(b)) // 4))
    for shift in range(-max_shift, max_shift + 1):
        aa, bb = (a[shift:], b) if shift >= 0 else (a, b[-shift:])
        count = min(len(aa), len(bb))
        agreement = 1 - sum((x ^ y).bit_count() for x, y in zip(aa[:count], bb[:count])) / (32 * count)
        best = max(best, agreement)
    return round(best, 6)


def compare_audio(first: dict, second: dict) -> dict:
    if first.get('kind') != 'audio' or second.get('kind') != 'audio':
        raise ValueError('Audio comparison requires two audio analysis results.')
    a, b = first['fingerprint'], second['fingerprint']
    spectral = _cosine(a['spectrum'], b['spectrum'])
    chroma = _chroma_similarity(a['chromaprint'], b['chromaprint'])
    aa, bb = a['temporal_spectra'], b['temporal_spectra']
    count = min(len(aa), len(bb))
    temporal = sum(_cosine(aa[round(i * (len(aa) - 1) / max(1, count - 1))], bb[round(i * (len(bb) - 1) / max(1, count - 1))]) for i in range(count)) / count if count else 0.0
    similarity = min(spectral, temporal, chroma if chroma is not None else 1.0)
    quiet = min(first.get('rms_dbfs', -240), second.get('rms_dbfs', -240)) < -80
    exact = a['decoded_pcm_sha256'] == b['decoded_pcm_sha256']
    classification = ('insufficient audible signal' if quiet else 'identical decoded analyzed interval' if exact else
                      'candidate near-duplicate; review audio' if similarity >= 0.92 else
                      'some analyzed characteristics overlap' if similarity >= 0.70 else 'low similarity by these methods')
    return {'first': first['name'], 'second': second['name'], 'similarity': round(similarity, 6) if not quiet else None,
            'classification': classification, 'components': {'chromaprint_bit_agreement': chroma,
            'spectral_cosine_similarity': round(spectral, 6), 'sampled_temporal_spectral_similarity': round(temporal, 6),
            'identical_decoded_interval': exact, 'duration_difference_seconds': round(second['duration'] - first['duration'], 6)},
            'limitations': AUDIO_LIMITATIONS + ['Chromaprint uses a bounded short-offset search; spectral windows use relative positions. These are heuristic comparisons, not a calibrated duplicate classifier.']}


@lru_cache(maxsize=1)
def _cosines():
    return [[math.cos((2 * x + 1) * u * math.pi / 64) for x in range(32)] for u in range(8)]


def _hashes(image):
    gray = image.convert('L')
    small = list(gray.resize((9, 8), Image.Resampling.LANCZOS).get_flattened_data())
    dhash = sum(1 << (y * 8 + x) for y in range(8) for x in range(8) if small[y * 9 + x] > small[y * 9 + x + 1])
    pixels = list(gray.resize((32, 32), Image.Resampling.LANCZOS).get_flattened_data())
    cosines = _cosines()
    horizontal = [[sum(pixels[y * 32 + x] * cosines[u][x] for x in range(32)) for u in range(8)] for y in range(32)]
    dct = [sum(horizontal[y][u] * cosines[v][y] for y in range(32)) for v in range(8) for u in range(8)]
    median = sorted(dct[1:])[31]
    phash = sum(1 << i for i, value in enumerate(dct) if i and value > median)
    colors = image.resize((32, 32)).convert('RGB')
    histogram = [0] * 64
    mean = [0, 0, 0]
    for red, green, blue in colors.get_flattened_data():
        histogram[(red // 64) * 16 + (green // 64) * 4 + blue // 64] += 1
        mean[0] += red; mean[1] += green; mean[2] += blue
    return {'phash': f'{phash:016x}', 'dhash': f'{dhash:016x}',
            'color_histogram': [v / 1024 for v in histogram], 'mean_rgb': [round(v / 1024, 3) for v in mean]}


def _frame_rate(value):
    try:
        n, d = str(value).split('/')
        return round(float(n) / float(d), 6) if float(d) else None
    except (ValueError, TypeError):
        return None


def analyze_video(path: Path, settings: dict, options: dict | None = None) -> dict:
    config = VideoFingerprintOptions.model_validate(options or {})
    source = _path(path)
    info = _probe(source, settings)
    stream = next((s for s in info.get('streams', []) if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')), None)
    if stream is None:
        raise ValueError('The selected file contains no video stream.')
    duration = _duration(info, stream)
    limit = min(duration, config.max_seconds)
    times = config.selected_times or [i * limit / config.sample_frames for i in range(config.sample_frames)]
    if any(t >= limit for t in times):
        raise ValueError('Selected frame timestamps must fall inside the media duration and analysis window.')
    frames, warnings = [], []
    deadline = time.monotonic() + 180
    for timestamp in times:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('Video sampling exceeded its bounded processing time.')
        command = _ffmpeg(settings) + ['-ss', f'{timestamp:.6f}'] + _input(source) + ['-map', f'0:{int(stream["index"])}',
                  '-an', '-sn', '-dn', '-frames:v', '1', '-vf', 'scale=160:90:force_original_aspect_ratio=decrease,pad=160:90:(ow-iw)/2:(oh-ih)/2',
                  '-pix_fmt', 'rgb24', '-c:v', 'rawvideo', '-threads:v', '1', '-f', 'rawvideo', 'pipe:1']
        raw = _capture(command, max_bytes=160 * 90 * 3 + 1024, timeout=min(35, remaining))
        if len(raw) != 160 * 90 * 3:
            warnings.append(f'No decodable frame at requested timestamp {timestamp:.3f} seconds.')
            continue
        image = Image.frombytes('RGB', (160, 90), raw)
        frames.append({'timestamp': round(timestamp, 6), 'preview': _png(image), **_hashes(image)})
    if not frames:
        raise RuntimeError('No selected video frame could be decoded.')
    width, height = int(stream.get('width', 0)), int(stream.get('height', 0))
    return {'schema_version': METHOD_VERSION, 'kind': 'video', 'name': source.name, 'duration': round(duration, 6),
            'analyzed_seconds': round(limit, 6), 'codec': stream.get('codec_name', 'unknown'), 'width': width, 'height': height,
            'aspect_ratio': round(width / height, 6) if height else None,
            'frame_rate': _frame_rate(stream.get('avg_frame_rate')), 'frames': frames,
            'pixel_format': stream.get('pix_fmt'), 'color_space': stream.get('color_space'),
            'color_transfer': stream.get('color_transfer'), 'color_primaries': stream.get('color_primaries'),
            'sample_aspect_ratio': stream.get('sample_aspect_ratio'), 'display_aspect_ratio': stream.get('display_aspect_ratio'),
            'fingerprint_method': 'FORGE sampled pHash/dHash + RGB histogram v1',
            'fingerprint': {'method_version': METHOD_VERSION, 'frames': [{k: v for k, v in f.items() if k != 'preview'} for f in frames]},
            'limitations': VIDEO_LIMITATIONS + warnings}


def _frame_similarity(a, b):
    phash = 1 - (int(a['phash'], 16) ^ int(b['phash'], 16)).bit_count() / 64
    dhash = 1 - (int(a['dhash'], 16) ^ int(b['dhash'], 16)).bit_count() / 64
    color = sum(min(x, y) for x, y in zip(a['color_histogram'], b['color_histogram']))
    mean = 1 - sum(abs(x - y) for x, y in zip(a['mean_rgb'], b['mean_rgb'])) / (3 * 255)
    return {'phash_agreement': phash, 'dhash_agreement': dhash, 'color_histogram_intersection': color,
            'mean_color_agreement': mean, 'similarity': 0.35 * phash + 0.25 * dhash + 0.25 * color + 0.15 * mean}


def compare_video(first: dict, second: dict) -> dict:
    if first.get('kind') != 'video' or second.get('kind') != 'video':
        raise ValueError('Video comparison requires two video analysis results.')
    aa, bb = first['fingerprint']['frames'], second['fingerprint']['frames']
    if not aa or not bb:
        raise ValueError('Video comparison requires decoded frames in both files.')
    timeline, components = [], []
    for a in aa:
        # Compare relative positions for the overall measure. A separate best
        # sampled-frame match aids shifted-section review without inflating it.
        position = a['timestamp'] / max(first['analyzed_seconds'], 0.001)
        b = min(bb, key=lambda frame: abs(frame['timestamp'] / max(second['analyzed_seconds'], 0.001) - position))
        measurement = _frame_similarity(a, b)
        candidates = [(frame, _frame_similarity(a, frame)) for frame in bb]
        best, score = max(candidates, key=lambda pair: pair[1]['similarity'])
        timeline.append({'first_timestamp': a['timestamp'], 'second_timestamp': b['timestamp'],
                         'similarity': round(measurement['similarity'], 6),
                         'best_sample_timestamp': best['timestamp'], 'best_sample_similarity': round(score['similarity'], 6)})
        components.append(measurement)
    means = {key: round(sum(row[key] for row in components) / len(components), 6) for key in components[0] if key != 'similarity'}
    similarity = sum(row['similarity'] for row in timeline) / len(timeline)
    sections, active = [], None
    for row in timeline:
        if row['similarity'] >= 0.90:
            if active is None:
                active = {'first_start': row['first_timestamp'], 'first_end': row['first_timestamp'],
                          'second_start': row['second_timestamp'], 'second_end': row['second_timestamp'], 'matching_samples': 1}
            else:
                active.update(first_end=row['first_timestamp'], second_end=row['second_timestamp'], matching_samples=active['matching_samples'] + 1)
        elif active is not None:
            sections.append(active); active = None
    if active is not None:
        sections.append(active)
    means.update(duration_difference_seconds=round(second['duration'] - first['duration'], 6),
                 frame_rate_changed=first.get('frame_rate') != second.get('frame_rate'),
                 resolution_changed=(first.get('width'), first.get('height')) != (second.get('width'), second.get('height')),
                 codec_changed=first.get('codec') != second.get('codec'),
                 color_metadata_changed=any(first.get(k) != second.get(k) for k in ('color_space', 'color_transfer', 'color_primaries')))
    return {'first': first['name'], 'second': second['name'], 'similarity': round(similarity, 6),
            'classification': 'candidate near-duplicate sampled frames; review video' if similarity >= 0.90 else
                              'some sampled visual features overlap' if similarity >= 0.70 else 'low sampled-frame similarity',
            'components': means, 'timeline': timeline, 'near_matching_sections': sections,
            'limitations': VIDEO_LIMITATIONS + ['Section boundaries connect near-matching sample positions only; intervening frames have not been verified.']}


def public_analysis(result: dict) -> dict:
    """Report/view data excludes reusable raw reference vectors by default."""
    value = copy.deepcopy(result)
    value.pop('fingerprint', None)
    if 'frames' in value:
        value['frames'] = [{key: val for key, val in frame.items() if key in {'timestamp', 'preview'}} for frame in value['frames']]
    return value


def _reference_record(result: dict) -> dict:
    # No absolute source paths, spectrograms, previews or incidental metadata.
    allowed = {'schema_version', 'kind', 'name', 'duration', 'analyzed_seconds', 'codec', 'channels', 'sample_rate',
               'rms_dbfs', 'width', 'height', 'aspect_ratio', 'frame_rate', 'fingerprint_method', 'fingerprint',
               'color_space', 'color_transfer', 'color_primaries', 'pixel_format', 'sample_aspect_ratio', 'display_aspect_ratio'}
    return {k: copy.deepcopy(v) for k, v in result.items() if k in allowed}


def _validate_reference_record(record, kind):
    if not isinstance(record, dict) or record.get('schema_version') != METHOD_VERSION or record.get('kind') != kind:
        raise ValueError('Unsupported fingerprint reference record or method version.')
    if not isinstance(record.get('name'), str) or not 1 <= len(record['name']) <= 255 or '/' in record['name'] or '\\' in record['name']:
        raise ValueError('A reference must contain a plain filename.')
    for key in ('duration', 'analyzed_seconds'):
        if not isinstance(record.get(key), (int, float)) or not math.isfinite(record[key]) or not 0 < record[key] <= 1e8:
            raise ValueError('A reference contains an invalid media duration.')
    f = record.get('fingerprint', {})
    if not isinstance(f, dict) or f.get('method_version') != METHOD_VERSION:
        raise ValueError('Unsupported fingerprint method version.')

    def vector(values, length, upper=1):
        if not isinstance(values, list) or len(values) != length or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 or v > upper for v in values):
            raise ValueError('A reference contains an invalid feature vector.')

    if kind == 'audio':
        vector(f.get('spectrum'), 32)
        temporal = f.get('temporal_spectra')
        if not isinstance(temporal, list) or not 1 <= len(temporal) <= 160:
            raise ValueError('Invalid temporal spectral reference.')
        for row in temporal:
            vector(row, 32)
        chroma = f.get('chromaprint')
        if not isinstance(chroma, list) or len(chroma) > 16384 or any(type(v) is not int or not 0 <= v <= 0xffffffff for v in chroma):
            raise ValueError('Invalid Chromaprint reference.')
        digest = f.get('decoded_pcm_sha256')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Invalid decoded-signal reference hash.')
        if type(record.get('rms_dbfs')) not in (int, float) or not math.isfinite(record['rms_dbfs']) or not -240 <= record['rms_dbfs'] <= 10:
            raise ValueError('Invalid audio signal level.')
    else:
        frames = f.get('frames')
        if not isinstance(frames, list) or not 1 <= len(frames) <= 48:
            raise ValueError('Invalid video reference frame count.')
        for frame in frames:
            if not isinstance(frame, dict) or type(frame.get('timestamp')) not in (int, float) or not math.isfinite(frame['timestamp']) or not 0 <= frame['timestamp'] <= 1800:
                raise ValueError('Invalid video reference timestamp.')
            for key in ('phash', 'dhash'):
                value = frame.get(key)
                if not isinstance(value, str) or len(value) != 16 or any(c not in '0123456789abcdef' for c in value):
                    raise ValueError('Invalid perceptual frame hash.')
            vector(frame.get('color_histogram'), 64)
            vector(frame.get('mean_rgb'), 3, 255)
    return _reference_record(record)


def read_references(path: Path, kind: Literal['audio', 'video']) -> list[dict]:
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError('Choose a local fingerprint reference file smaller than 5 MB.')
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('The fingerprint reference is not valid JSON.') from exc
    if not isinstance(doc, dict) or doc.get('format') != 'forge-fingerprints' or doc.get('schema_version') != METHOD_VERSION or doc.get('kind') != kind:
        raise ValueError('The reference file uses a different media kind or unsupported fingerprint schema.')
    records = doc.get('records')
    if not isinstance(records, list) or not 1 <= len(records) <= 20:
        raise ValueError('A reference file must contain 1 to 20 fingerprints.')
    return [_validate_reference_record(record, kind) for record in records]


def _html_report(report):
    # All source strings and JSON are escaped; local PNG data URIs are generated
    # internally. There are no external resources, scripts or remote requests.
    chunks = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>FORGE fingerprint comparison</title>',
              '<style>body{font:16px system-ui;background:#191919;color:#eee;max-width:1100px;margin:2rem auto;padding:1rem}h1,h2{color:#d2ae61}img{max-width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}figure{display:inline-block;margin:8px}figcaption{font-size:.85rem}</style>',
              '<h1>FORGE ' + html.escape(report['kind']) + ' fingerprint analysis</h1>',
              '<p>Local descriptor measurements. These results do not establish anonymity or predict proprietary recognition.</p>']
    for result in report['analyses']:
        chunks += ['<h2>' + html.escape(result['name']) + '</h2>']
        for label, uri in result.get('visualizations', {}).items():
            chunks.append('<figure><img alt="' + html.escape(label, quote=True) + '" src="' + uri + '"><figcaption>' + html.escape(label) + '</figcaption></figure>')
        for frame in result.get('frames', []):
            if 'preview' in frame:
                chunks.append('<figure><img alt="Selected video frame" src="' + frame['preview'] + '"><figcaption>' + str(frame['timestamp']) + ' s</figcaption></figure>')
    visible = copy.deepcopy(report)
    for result in visible['analyses']:
        result.pop('visualizations', None)
        for frame in result.get('frames', []):
            frame.pop('preview', None)
    chunks.append('<h2>Measured results and limitations</h2><pre>' + html.escape(json.dumps(visible, ensure_ascii=False, indent=2)) + '</pre></html>')
    return ''.join(chunks)


def _process(kind, source, stage_dir, options, settings, context):
    config = (AudioFingerprintOptions if kind == 'audio' else VideoFingerprintOptions).model_validate(options)
    analyze, compare = (analyze_audio, compare_audio) if kind == 'audio' else (analyze_video, compare_video)
    sources = [Path(source)] + [Path(value['path'] if isinstance(value, dict) else value) for value in context.get('comparison_sources', [])]
    reference_sources = context.get('reference_sources', [])
    if len(sources) + len(reference_sources) > 20:
        raise ValueError('Analyze at most 20 media/reference inputs in one comparison job.')
    references = []
    for value in reference_sources:
        references.extend(read_references(Path(value['path'] if isinstance(value, dict) else value), kind))
    if len(sources) + len(references) > 20:
        raise ValueError('A comparison can include at most 20 total media and retained reference records.')
    analyses = [analyze(path, settings, config.model_dump()) for path in sources]
    comparisons = [compare(analyses[0], result) for result in analyses[1:] + references]
    report = {'schema_version': METHOD_VERSION, 'kind': kind, 'analyses': [public_analysis(result) for result in analyses],
              'reference_names': [record['name'] for record in references], 'comparisons': comparisons,
              'comparison_policy': 'Each subsequent media/reference is compared with the first media input.',
              'reference_trust': 'Imported reference files are user-supplied descriptors; their origin and media association are not cryptographically authenticated.',
              'limitations': AUDIO_LIMITATIONS if kind == 'audio' else VIDEO_LIMITATIONS}
    context['analysis_result'] = report
    stem = safe_name(Path(source).stem) + '_' + kind + '-fingerprint'
    directory = Path(stage_dir)
    files = []
    if config.report_json:
        path = directory / (stem + '.json')
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        files.append(path)
    if config.report_html:
        path = directory / (stem + '.html')
        path.write_text(_html_report(report), encoding='utf-8')
        files.append(path)
    if config.retain_reference:
        path = directory / (stem + '.forge-fingerprint.json')
        path.write_text(json.dumps({'format': 'forge-fingerprints', 'schema_version': METHOD_VERSION, 'kind': kind,
                                   'records': [_reference_record(result) for result in analyses]}, ensure_ascii=False, indent=2), encoding='utf-8')
        files.append(path)
    warnings = ['Original media was inspected without modification. Scores measure selected local descriptors and are not recognition or anonymity guarantees.']
    if not config.report_json and not config.report_html and not config.retain_reference:
        warnings.append('Report and reference retention disabled; no analysis files were created.')
    return files, warnings


def process_audio(source, stage_dir, options, settings, context):
    return _process('audio', source, stage_dir, options, settings, context)


def process_video(source, stage_dir, options, settings, context):
    return _process('video', source, stage_dir, options, settings, context)


def capabilities(settings: dict) -> dict:
    """Actual installed capability, no dependency download or media reads."""
    ffmpeg = executable('ffmpeg', settings)
    ffprobe = executable('ffprobe', settings)
    chromaprint = bool(ffmpeg and _chromaprint_available(ffmpeg))
    return {'ffmpeg': bool(ffmpeg), 'ffprobe': bool(ffprobe), 'chromaprint': chromaprint,
            'audio_method': 'Chromaprint algorithm 1 + FORGE spectral profile v1' if chromaprint else 'FORGE spectral profile v1 (Chromaprint unavailable)',
            'video_method': 'FORGE sampled pHash/dHash + RGB histogram v1',
            'reference_storage': 'Explicitly exported local .forge-fingerprint.json files; no automatic indexing'}


# Shared bounded subprocess contract for other local media inspectors.
capture_local = _capture
INPUT_DEMUXERS = DEMUXERS
