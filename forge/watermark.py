"""Bounded local text/logo and audible-clip watermark rendering.

Visible marks are rasterized with Pillow, never interpreted as filter syntax.
Audio identifiers are deliberately audible clips, not claims of robust hidden
watermarking. Previews are actual encodes with the same processing pipeline.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import Literal

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .core import absolute_path, executable, no_window, safe_name


class WatermarkOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    mode: Literal['video', 'audio', 'both'] = 'video'
    video_kind: Literal['text', 'image'] = 'text'
    text: str = Field('© Your name', min_length=1, max_length=500)
    image_path: str = Field('', max_length=4096)
    font_path: str = Field('', max_length=4096)
    audio_clip_path: str = Field('', max_length=4096)
    font: Literal['sans', 'serif', 'mono'] = 'sans'
    font_size: int = Field(48, ge=8, le=256)
    color: str = Field('#ffffff', pattern=r'^#[0-9a-fA-F]{6}$')
    opacity: float = Field(.65, ge=.05, le=1)
    scale: float = Field(1, ge=.1, le=4)
    rotation: float = Field(0, ge=-180, le=180)
    placement: Literal['full-frame-diagonal', 'perimeter', 'bottom-center', 'custom'] = 'bottom-center'
    horizontal: Literal['left', 'center', 'right'] = 'center'
    vertical: Literal['top', 'center', 'bottom'] = 'bottom'
    margin: int = Field(24, ge=0, le=1024)
    x: float = Field(.5, ge=0, le=1)
    y: float = Field(.5, ge=0, le=1)
    repeat_spacing: int = Field(120, ge=20, le=1024)
    movement: Literal['none', 'horizontal', 'vertical', 'bounce'] = 'none'
    movement_speed: float = Field(40, ge=1, le=500)
    movement_bounds: list[float] = Field(default_factory=lambda: [0, 0, 1, 1], min_length=4, max_length=4)
    start_seconds: float = Field(0, ge=0, le=86400)
    end_seconds: float | None = Field(None, gt=0, le=86400)
    appearance_interval: float = Field(0, ge=0, le=86400)
    appearance_duration: float = Field(2, gt=0, le=86400)
    fade_seconds: float = Field(.25, ge=0, le=10)
    audio_volume: float = Field(.35, gt=0, le=2)
    audio_timestamps: list[float] = Field(default_factory=lambda: [0], min_length=1, max_length=100)
    audio_repeat_seconds: float = Field(0, ge=0, le=86400)
    audio_fade_seconds: float = Field(.1, ge=0, le=10)
    format: Literal['mp4', 'mkv', 'wav', 'mp3', 'flac', 'm4a'] = 'mp4'
    encoder: Literal['auto', 'cpu', 'nvenc'] = 'auto'
    quality: Literal['standard', 'high'] = 'standard'

    @field_validator('text')
    @classmethod
    def clean_text(cls, value):
        if not value.strip() or '\x00' in value or len(value.splitlines()) > 8:
            raise ValueError('Enter visible text with at most eight lines and no NUL characters.')
        return value

    @field_validator('movement_bounds')
    @classmethod
    def bounds(cls, value):
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in value) or value[0] >= value[2] or value[1] >= value[3]:
            raise ValueError('Movement bounds must be ordered left, top, right, bottom fractions from 0 to 1.')
        return value

    @field_validator('audio_timestamps')
    @classmethod
    def timestamps(cls, value):
        if any(not math.isfinite(v) or not 0 <= v <= 86400 for v in value):
            raise ValueError('Audio timestamps must be finite seconds from 0 to 86400.')
        return sorted(set(value))

    @model_validator(mode='after')
    def coherent(self):
        if self.end_seconds is not None and self.end_seconds <= self.start_seconds:
            raise ValueError('The watermark end must follow its start.')
        if self.mode in {'video', 'both'} and self.format not in {'mp4', 'mkv'}:
            raise ValueError('Visible watermarks require MP4 or MKV output.')
        if self.mode in {'video', 'both'} and self.video_kind == 'image' and not self.image_path:
            raise ValueError('Choose a local watermark image.')
        if self.mode in {'audio', 'both'} and not self.audio_clip_path:
            raise ValueError('Choose a recorded ownership announcement or another local audio clip.')
        if self.mode in {'video', 'both'} and self.movement != 'none' and self.placement in {'full-frame-diagonal', 'perimeter'}:
            raise ValueError('Movement supports Bottom Center or Custom placement. Repeated full-frame and perimeter patterns are stationary.')
        if self.appearance_interval and (self.appearance_interval < .1 or self.appearance_duration > self.appearance_interval):
            raise ValueError('Appearance interval must be at least 0.1 seconds and no shorter than the visible duration.')
        if self.audio_repeat_seconds and self.audio_repeat_seconds < .1:
            raise ValueError('Audio repeat interval must be at least 0.1 seconds.')
        return self


def _options(options):
    return options if isinstance(options, WatermarkOptions) else WatermarkOptions.model_validate(options)


def validate_assets(options) -> list[Path]:
    """Resolve only active local assets so the queue can snapshot their stats."""
    o = _options(options)
    assets = []
    requested = []
    if o.mode in {'video', 'both'}:
        if o.video_kind == 'image':
            requested.append((o.image_path, {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}, 32 * 1048576, 'image'))
        elif o.font_path:
            requested.append((o.font_path, {'.ttf', '.otf'}, 20 * 1048576, 'font'))
    if o.mode in {'audio', 'both'}:
        requested.append((o.audio_clip_path, {'.wav', '.mp3', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.wma', '.aiff', '.aif'}, 100 * 1048576, 'audio clip'))
    for value, extensions, maximum, label in requested:
        path = absolute_path(value)
        if path.suffix.lower() not in extensions or not 0 < path.stat().st_size <= maximum:
            raise ValueError(f'Choose a supported {label} smaller than {maximum // 1048576} MB.')
        assets.append(path)
    return assets


def _binary(name, settings):
    binary = executable(name, settings)
    if not binary:
        raise RuntimeError(f'{name} is required. Configure its local executable in Environment.')
    return binary


def _probe(path, settings):
    result = subprocess.run([_binary('ffprobe', settings), '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_format', '-show_streams', '-of', 'json', str(path)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120, **no_window())
    if result.returncode:
        raise RuntimeError('Could not inspect media: ' + result.stderr[-1200:])
    return json.loads(result.stdout)


def _duration(info):
    try:
        value = float(info.get('format', {}).get('duration', 0))
    except (TypeError, ValueError):
        value = 0
    if not math.isfinite(value) or not 0 < value <= 86400:
        raise ValueError('Watermark processing requires a known media duration of at most 24 hours.')
    return value


def _streams(info):
    streams = info.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    return video, audio


def _dimensions(video):
    width, height = int(video.get('width', 0)), int(video.get('height', 0))
    rotation = next((s.get('rotation', 0) for s in video.get('side_data_list', []) if 'rotation' in s), video.get('tags', {}).get('rotate', 0))
    try:
        if abs(round(float(rotation))) % 180 == 90:
            width, height = height, width
    except (ValueError, TypeError):
        pass
    if min(width, height) < 8 or width * height > 33_177_600:
        raise ValueError('Video dimensions must be valid and no larger than 8K (33.2 million pixels).')
    return width, height


def nvenc_available(settings) -> bool:
    """An installed encoder alone is insufficient; require a detected NVIDIA GPU."""
    smi = shutil.which('nvidia-smi')
    if not smi:
        return False
    try:
        hardware = subprocess.run([smi, '-L'], capture_output=True, text=True, timeout=5, **no_window())
        if hardware.returncode or 'GPU ' not in hardware.stdout:
            return False
        encoders = subprocess.run([_binary('ffmpeg', settings), '-hide_banner', '-encoders'], capture_output=True, text=True, timeout=15, **no_window())
        return encoders.returncode == 0 and 'h264_nvenc' in encoders.stdout
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return False


def _events(o, duration):
    times = set()
    for first in o.audio_timestamps:
        if first >= duration:
            continue
        count = math.ceil((duration - first) / o.audio_repeat_seconds) if o.audio_repeat_seconds else 1
        if count > 100:
            raise ValueError('At most 100 audible insertions are supported. Increase the repeat interval.')
        times.update(round(first + n * o.audio_repeat_seconds, 6) for n in range(count))
        if len(times) > 100:
            raise ValueError('At most 100 audible insertions are supported. Reduce timestamps or increase the repeat interval.')
    if not times:
        raise ValueError('At least one audio insertion must begin before the end of the source.')
    return sorted(times)


def publicplan(source, options, settings) -> dict:
    o = _options(options)
    validate_assets(o)
    info = _probe(source, settings)
    video, audio = _streams(info)
    duration = _duration(info)
    if o.mode in {'video', 'both'} and not video:
        raise ValueError('Visible watermarking requires a video stream.')
    if o.format in {'mp4', 'mkv'} and not video:
        raise ValueError('This source has no video. Choose WAV, FLAC, MP3, or M4A for an audio watermark.')
    if o.mode in {'video', 'both'} and o.start_seconds >= duration:
        raise ValueError('The visible watermark starts after the source ends.')
    if video:
        _dimensions(video)
    clip_duration = None
    events = []
    if o.mode in {'audio', 'both'}:
        clip_info = _probe(absolute_path(o.audio_clip_path), settings)
        if not _streams(clip_info)[1]:
            raise ValueError('The watermark clip has no audio stream.')
        clip_duration = _duration(clip_info)
        if clip_duration > 300:
            raise ValueError('Audio identifiers must be at most five minutes long.')
        events = _events(o, duration)
        if any(after < before + clip_duration - .000001 for before, after in zip(events, events[1:])):
            raise ValueError('Audio insertions must not overlap. Shorten the clip or increase the repeat interval.')
    audio_reencoded = o.mode in {'audio', 'both'} or bool(audio and o.format == 'mp4' and audio.get('codec_name') not in {'aac', 'mp3', 'ac3', 'alac', 'eac3'})
    included_video = bool(video) and o.format in {'mp4', 'mkv'}
    return {'duration_seconds': duration, 'has_video': bool(video), 'has_audio': bool(audio), 'output_has_video': included_video, 'format': o.format, 'output_suffix': '.' + o.format,
            'video_reencoded': o.mode in {'video', 'both'}, 'audio_reencoded': audio_reencoded,
            'video_encoder': ('CPU H.264' if o.encoder == 'cpu' else 'NVIDIA H.264 when available, with CPU fallback') if o.mode in {'video', 'both'} else 'Original primary video copied when included',
            'audio_insertions': events, 'audio_clip_duration': clip_duration, 'preview_kind': 'Encoded output excerpt, at most five seconds',
            'retained_streams': ('First video and first audio only' if included_video else 'First audio only; video is excluded') + '; subtitles, alternate tracks, attachments, descriptive tags, and chapters are excluded.',
            'privacy_note': 'A visible or audible ownership mark does not anonymize media or certify robust hidden-watermark protection.'}


def _font(o, size):
    if o.font_path:
        try:
            return ImageFont.truetype(str(absolute_path(o.font_path)), size)
        except OSError as exc:
            raise ValueError('The selected font could not be loaded by Pillow.') from exc
    fonts = {'sans': ('arial.ttf', 'DejaVuSans.ttf'), 'serif': ('georgia.ttf', 'DejaVuSerif.ttf'), 'mono': ('consola.ttf', 'DejaVuSansMono.ttf')}
    windows, linux = fonts[o.font]
    candidates = [Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / windows, Path('/usr/share/fonts/truetype/dejavu') / linux]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def _tile(o, width, height):
    if o.video_kind == 'image':
        with Image.open(absolute_path(o.image_path)) as source:
            if source.width * source.height > 33_177_600 or getattr(source, 'is_animated', False):
                raise ValueError('Use a still logo image no larger than 33.2 million pixels.')
            tile = ImageOps.exif_transpose(source).convert('RGBA')
        target = max(1, min(width, round(width * .25 * o.scale)))
        tile.thumbnail((target, max(1, round(height * .6))), Image.Resampling.LANCZOS)
    else:
        font = _font(o, max(1, round(o.font_size * o.scale)))
        scratch = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
        left, top, right, bottom = scratch.multiline_textbbox((0, 0), o.text, font=font, stroke_width=1, spacing=4, align=o.horizontal)
        tile_size = (max(1, right - left + 6), max(1, bottom - top + 6))
        if tile_size[0] * tile_size[1] > 16_777_216:
            raise ValueError('Rendered text is too large. Reduce font size, scale, or text length.')
        tile = Image.new('RGBA', tile_size)
        ImageDraw.Draw(tile).multiline_text((3 - left, 3 - top), o.text, font=font, fill=ImageColor.getrgb(o.color) + (255,), stroke_width=1,
                                            stroke_fill=(0, 0, 0, 180), spacing=4, align=o.horizontal)
    angle = o.rotation if o.rotation else (-30 if o.placement == 'full-frame-diagonal' else 0)
    if angle:
        tile = tile.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
    tile.thumbnail((max(1, width - 2), max(1, height - 2)), Image.Resampling.LANCZOS)
    tile.putalpha(tile.getchannel('A').point(lambda value: round(value * o.opacity)))
    return tile


def _stamp(canvas, tile, x, y):
    canvas.alpha_composite(tile, (int(x), int(y)))


def _overlay(o, width, height, path):
    tile = _tile(o, width, height)
    if o.placement == 'full-frame-diagonal':
        canvas = Image.new('RGBA', (width, height))
        dx, dy = tile.width + o.repeat_spacing, tile.height + o.repeat_spacing
        for row, y in enumerate(range(-tile.height // 2, height, dy)):
            for x in range(-tile.width // 2 - (dx // 2 if row % 2 else 0), width, dx):
                _stamp(canvas, tile, x, y)
        tile = canvas
    elif o.placement == 'perimeter':
        canvas = Image.new('RGBA', (width, height))
        mx, my = min(o.margin, max(0, (width - tile.width) // 2)), min(o.margin, max(0, (height - tile.height) // 2))
        xs = sorted(set([mx, max(mx, width - tile.width - mx)] + list(range(mx, max(mx + 1, width - tile.width - mx), tile.width + o.repeat_spacing))))
        for x in xs:
            _stamp(canvas, tile, x, my)
            _stamp(canvas, tile, x, height - tile.height - my)
        for y in range(my + tile.height + o.repeat_spacing, height - tile.height - my, tile.height + o.repeat_spacing):
            _stamp(canvas, tile, mx, y)
            _stamp(canvas, tile, width - tile.width - mx, y)
        tile = canvas
    tile.save(path, 'PNG')
    return tile.size


def _coordinates(o, width, height, tile_width, tile_height):
    if o.placement in {'full-frame-diagonal', 'perimeter'}:
        return '0', '0'
    available_x, available_y = width - tile_width, height - tile_height
    mx, my = min(o.margin, available_x / 2), min(o.margin, available_y / 2)
    if o.placement == 'bottom-center':
        x, y = available_x / 2, available_y - my
    else:
        anchor_x = {'left': 0, 'center': .5, 'right': 1}[o.horizontal]
        anchor_y = {'top': 0, 'center': .5, 'bottom': 1}[o.vertical]
        x = max(mx, min(available_x - mx, o.x * width - anchor_x * tile_width))
        y = max(my, min(available_y - my, o.y * height - anchor_y * tile_height))
    bounds = o.movement_bounds
    low_x, high_x = max(mx, width * bounds[0]), min(available_x - mx, width * bounds[2] - tile_width)
    low_y, high_y = max(my, height * bounds[1]), min(available_y - my, height * bounds[3] - tile_height)
    if o.movement != 'none' and (low_x > high_x or low_y > high_y):
        raise ValueError('The watermark does not fit inside the movement boundaries. Reduce its size or widen the bounds.')
    if o.movement != 'none':
        x, y = max(low_x, min(high_x, x)), max(low_y, min(high_y, y))
    def moving(low, high):
        span = high - low
        return f'{low:.6f}' if span < .01 else f'{low:.6f}+abs(mod(max(t-{o.start_seconds:.6f},0)*{o.movement_speed:.6f}+{span:.6f},{2 * span:.6f})-{span:.6f})'
    xx = moving(low_x, high_x) if o.movement in {'horizontal', 'bounce'} else f'{x:.6f}'
    yy = moving(low_y, high_y) if o.movement in {'vertical', 'bounce'} else f'{y:.6f}'
    return xx, yy


def _appearance(o, duration):
    end = min(duration, o.end_seconds if o.end_seconds is not None else duration)
    enabled = f'between(t,{o.start_seconds:.6f},{end:.6f})'
    if o.appearance_interval:
        enabled += f'*lt(mod(t-{o.start_seconds:.6f},{o.appearance_interval:.6f}),{o.appearance_duration:.6f})'
    if not o.fade_seconds:
        return enabled, ''
    if o.appearance_interval:
        elapsed = f'mod(T-{o.start_seconds:.6f},{o.appearance_interval:.6f})'
        fade = min(o.fade_seconds, o.appearance_duration / 2)
        alpha = f'max(0,min(1,min(({elapsed})/{fade:.6f},({o.appearance_duration:.6f}-({elapsed}))/{fade:.6f})))'
    else:
        fade = min(o.fade_seconds, (end - o.start_seconds) / 2)
        alpha = f'max(0,min(1,min((T-{o.start_seconds:.6f})/{fade:.6f},({end:.6f}-T)/{fade:.6f})))'
    # Every component is a validated number or fixed expression, never user text.
    return enabled, f",geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({alpha})'"


def _run_ffmpeg(args, settings, duration, context=None):
    # Worker isolation owns cancellation; a bounded timeout also covers previews.
    timeout = min(86400, max(120, duration * 30 + 60))
    callback = (context or {}).get('progress') if isinstance(context, dict) else None
    if callable(callback):
        callback(0, 'Encoding watermark output', progress_measured=False)
    restricted = []
    for arg in args:
        if arg == '-i':
            restricted += ['-protocol_whitelist', 'file,pipe']
        restricted.append(arg)
    command = [_binary('ffmpeg', settings), '-nostdin', '-hide_banner', '-loglevel', 'error', '-n', '-filter_complex_threads', '1', '-progress', 'pipe:1', *restricted]
    # Error output is streamed to a temporary file, not accumulated in memory.
    # A watchdog also bounds a decoder that stops producing progress entirely.
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8', errors='replace', **no_window()) as process:
            expired = threading.Event()
            def terminate():
                expired.set()
                try:
                    process.kill()
                except OSError:
                    pass
            watchdog = threading.Timer(timeout, terminate)
            watchdog.daemon = True
            watchdog.start()
            try:
                for line in process.stdout:
                    key, _, value = line.strip().partition('=')
                    if key == 'out_time_us' and value.isdigit() and callable(callback):
                        done = int(value) / 1_000_000
                        callback(min(99, 100 * done / duration), 'Encoding watermark output', progress_measured=True, stage='Watermark encoding')
                code = process.wait()
            except BaseException:
                process.kill()
                process.wait()
                raise
            finally:
                watchdog.cancel()
            if expired.is_set():
                raise subprocess.TimeoutExpired(command, timeout)
            if code:
                errors.seek(0, 2)
                errors.seek(max(0, errors.tell() - 2000))
                message = errors.read().decode('utf-8', errors='replace')
                raise RuntimeError('Watermark encoding failed: ' + message)


def _render(source, stage_dir, options, settings, context=None, preview_limit=None):
    o = _options(options)
    plan = publicplan(source, o, settings)
    source = absolute_path(str(source))
    stage_dir = Path(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    info = _probe(source, settings)
    video, audio = _streams(info)
    source_duration = plan['duration_seconds']
    total = min(source_duration, preview_limit) if preview_limit else source_duration
    output = stage_dir / (safe_name(source.stem) + ('_watermark_preview' if preview_limit else '_watermarked') + '.' + o.format)
    if output.exists():
        raise ValueError('The staging output already exists. Use an empty staging directory.')
    warnings = [plan['retained_streams'], plan['privacy_note']]
    visible = o.mode in {'video', 'both'}
    audible = o.mode in {'audio', 'both'}
    include_video = bool(video) and o.format in {'mp4', 'mkv'}
    args = ['-i', str(source)]
    filters = []
    next_input = 1
    overlay_path = None
    if visible:
        width, height = _dimensions(video)
        overlay_path = stage_dir / '_watermark_overlay.png'
        tile_width, tile_height = _overlay(o, width, height, overlay_path)
        overlay_index = next_input
        next_input += 1
        args += ['-loop', '1', '-framerate', '30', '-i', str(overlay_path)]
        enabled, opacity_filter = _appearance(o, source_duration)
        xx, yy = _coordinates(o, width, height, tile_width, tile_height)
        filters += [f'[{overlay_index}:v]format=rgba{opacity_filter}[mark]',
                    f'[0:{video["index"]}]setpts=PTS-STARTPTS[basev]',
                    f"[basev][mark]overlay=x='{xx}':y='{yy}':enable='{enabled}':shortest=1:format=auto,pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p[outv]"]
        warnings.append('The visible watermark is encoded into a new H.264 video. Pixel format becomes 8-bit YUV420; HDR/color fidelity is not preserved.')
    if audible:
        clip = absolute_path(o.audio_clip_path)
        clip_info = _probe(clip, settings)
        clip_audio = _streams(clip_info)[1]
        clip_duration = min(plan['audio_clip_duration'], source_duration)
        events = [t for t in plan['audio_insertions'] if t < total]
        if audio:
            filters.append(f'[0:{audio["index"]}]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[basea]')
        else:
            filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={total:.6f}[basea]')
        if events:
            # Timestamp-sized adelay buffers can exhaust RAM on long sources.
            # Stream finite silence and separately decoded clips through concat
            # instead. Each local input is independently bounded by the decoder.
            pieces = []
            previous_end = 0
            for n, timestamp in enumerate(events):
                gap = timestamp - previous_end
                if gap > .000001:
                    filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={gap:.6f},asetpts=PTS-STARTPTS[gap{n}]')
                    pieces.append(f'[gap{n}]')
                event_duration = min(clip_duration, total - timestamp)
                fade = min(o.audio_fade_seconds, event_duration / 2)
                args += ['-i', str(clip)]
                chain = f'[{next_input}:{clip_audio["index"]}]atrim=duration={event_duration:.6f},asetpts=PTS-STARTPTS,aresample=48000,apad=whole_dur={event_duration:.6f},atrim=duration={event_duration:.6f},aformat=sample_fmts=fltp:channel_layouts=stereo,volume={o.audio_volume:.6f}'
                next_input += 1
                if fade:
                    chain += f',afade=t=in:d={fade:.6f},afade=t=out:st={event_duration - fade:.6f}:d={fade:.6f}'
                chain += f'[event{n}]'
                filters.append(chain)
                pieces.append(f'[event{n}]')
                previous_end = timestamp + event_duration
            tail = total - previous_end
            if tail > .000001:
                filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={tail:.6f},asetpts=PTS-STARTPTS[tail]')
                pieces.append('[tail]')
            filters.append(''.join(pieces) + f'concat=n={len(pieces)}:v=0:a=1[identifier]')
            filters.append('[basea][identifier]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=0:latency=1[outa]')
        else:
            # The short preview can legitimately precede the configured insertions.
            filters.append('[basea]anull[outa]')
            warnings.append('The first-five-second preview precedes the configured audio insertions. The full export uses the saved timestamps.')
        warnings.append('Audible identifiers are mixed into a new audio encode with a peak limiter; the original file is preserved.')
    if filters:
        args += ['-filter_complex', ';'.join(filters)]
    if include_video:
        args += ['-map', '[outv]' if visible else f'0:{video["index"]}']
    if audible or audio:
        args += ['-map', '[outa]' if audible else f'0:{audio["index"]}']
    args += ['-map_metadata', '-1', '-map_chapters', '-1', '-t', f'{total:.6f}']
    if include_video and not visible:
        args += ['-c:v', 'copy']
        warnings.append('The primary video stream is copied without re-encoding; only the audio mix changes.')
    if audible:
        audio_codecs = {'wav': ['-c:a', 'pcm_s24le'], 'flac': ['-c:a', 'flac'], 'mp3': ['-c:a', 'libmp3lame', '-b:a', '192k']}
        args += audio_codecs.get(o.format, ['-c:a', 'aac', '-b:a', '192k'])
    elif audio:
        if o.format == 'mp4' and audio.get('codec_name') not in {'aac', 'mp3', 'ac3', 'alac', 'eac3'}:
            args += ['-c:a', 'aac', '-b:a', '192k']
            warnings.append('The source audio codec requires an AAC encode for MP4 compatibility.')
        else:
            args += ['-c:a', 'copy']
    if o.format in {'mp4', 'm4a'}:
        args += ['-movflags', '+faststart']
    try:
        if visible:
            gpu = o.encoder != 'cpu' and nvenc_available(settings)
            cpu_args = ['-c:v', 'libx264', '-preset', 'medium', '-crf', '18' if o.quality == 'high' else '22', '-threads', '2']
            if gpu:
                try:
                    _run_ffmpeg(args + ['-c:v', 'h264_nvenc', '-preset', 'p4', '-cq', '18' if o.quality == 'high' else '22', str(output)], settings, total, context)
                    warnings.append('Video encoded with NVIDIA NVENC.')
                except (RuntimeError, subprocess.TimeoutExpired):
                    output.unlink(missing_ok=True)
                    warnings.append('NVIDIA encoding failed; the output was encoded with the CPU fallback.')
                    _run_ffmpeg(args + cpu_args + [str(output)], settings, total, context)
            else:
                if o.encoder == 'nvenc':
                    warnings.append('A usable NVIDIA encoder was not detected. CPU H.264 fallback was used.')
                _run_ffmpeg(args + cpu_args + [str(output)], settings, total, context)
                warnings.append('Video encoded with CPU H.264.')
        else:
            _run_ffmpeg(args + [str(output)], settings, total, context)
        verified = _probe(output, settings)
        out_video, out_audio = _streams(verified)
        verified_duration = _duration(verified)
        if (include_video and not out_video) or ((audio or audible) and not out_audio) or abs(verified_duration - total) > max(.5, total * .02):
            raise RuntimeError('Output verification failed: required streams or expected duration are missing.')
        warnings.append('FFprobe verified output streams and duration. Review the encoded preview and final media for appearance and audibility.')
        if preview_limit:
            warnings.append('This is an encoded excerpt from the beginning, limited to five seconds; later scheduled marks are outside this excerpt.')
        return [output], warnings
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    finally:
        if overlay_path:
            overlay_path.unlink(missing_ok=True)


def process(source: Path, stage_dir: Path, options: dict, settings: dict, context: dict | None = None) -> tuple[list[Path], list[str]]:
    if not callable((context or {}).get('progress')):
        from .worker import progress
        context = {**(context or {}), 'progress': progress}
    return _render(source, stage_dir, options, settings, context)


def preview(source: Path, stage_dir: Path, options: dict, settings: dict) -> Path:
    return _render(source, stage_dir, options, settings, preview_limit=5)[0][0]


def preview_still(source: Path, stage_dir: Path, options: dict, settings: dict) -> Path:
    o = _options(options)
    if o.format not in {'mp4', 'mkv'}:
        raise ValueError('A still preview requires a video output format.')
    video = preview(source, stage_dir, o, settings)
    target = Path(stage_dir) / 'watermark_encoded_preview.png'
    at = min(2.5, max(0, _duration(_probe(video, settings)) / 2))
    _run_ffmpeg(['-ss', str(at), '-i', str(video), '-frames:v', '1', str(target)], settings, 5)
    return target
