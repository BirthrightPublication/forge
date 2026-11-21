"""Measured local dubbing alignment, audio mixing and loss-aware video export.

These helpers operate on new files only. They use FORGE's existing FFmpeg worker
and Audio Effects renderer; they do not create another execution system.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile

from .core import executable, no_window


def finite(value, minimum=0, maximum=86400, *, name='Value'):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be a finite number from {minimum} to {maximum}.')
    return float(value)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def media_info(path, settings):
    from .worker import probe
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('The selected media file is no longer available.')
    result = probe(path, settings)
    try:
        seconds = float(result.get('format', {}).get('duration') or 0)
    except (ValueError, TypeError):
        seconds = 0
    if not math.isfinite(seconds) or not 0 < seconds <= 86400:
        raise ValueError('Dubbing requires media with a known duration of at most 24 hours.')
    result['duration'] = seconds
    result['audio_streams'] = [item for item in result.get('streams', []) if item.get('codec_type') == 'audio']
    result['video_streams'] = [item for item in result.get('streams', []) if item.get('codec_type') == 'video' and not item.get('disposition', {}).get('attached_pic')]
    return result


def new_output(path, *sources):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('A dubbing output cannot replace or follow a symbolic link.')
    path = path.resolve()
    if path.exists() or any(path == Path(source).resolve() for source in sources if source):
        raise ValueError('Dubbing must create a new file; an original or existing output cannot be replaced.')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def ffmpeg(arguments, settings, total=0):
    from .worker import run_ffmpeg
    run_ffmpeg([str(item) for item in arguments], settings, total)


def audio_info(path, settings):
    info = media_info(path, settings)
    if not info['audio_streams']:
        raise ValueError('The selected media has no audio stream.')
    stream = info['audio_streams'][0]
    return {'duration': info['duration'], 'sample_rate': int(stream.get('sample_rate') or 48000),
            'channels': int(stream.get('channels') or 1), 'codec': stream.get('codec_name', '')}


def extract_audio(source, output, settings, *, start=0, end=None, rate=48000, channels=2):
    source = Path(source).resolve()
    info = audio_info(source, settings)
    start = finite(start, 0, info['duration'], name='Audio selection start')
    end = info['duration'] if end is None else finite(end, 0, info['duration'], name='Audio selection end')
    if end <= start:
        raise ValueError('Audio selection end must follow its start.')
    if rate not in {16000, 22050, 24000, 44100, 48000} or channels not in {1, 2}:
        raise ValueError('Unsupported dubbing audio sample format.')
    output = new_output(output, source)
    ffmpeg(['-i', source, '-ss', f'{start:.9f}', '-t', f'{end-start:.9f}', '-map', '0:a:0',
            '-vn', '-map_metadata', '-1', '-ar', rate, '-ac', channels, '-c:a', 'pcm_s24le', output], settings, end-start)
    measured = audio_info(output, settings)
    if abs(measured['duration'] - (end-start)) > .15:
        raise RuntimeError('Extracted audio duration does not match the selected source window.')
    return measured


def silence(output, seconds, settings):
    seconds = finite(seconds, .001, 86400, name='Silence duration')
    output = new_output(output)
    ffmpeg(['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', f'{seconds:.9f}',
            '-c:a', 'pcm_s24le', output], settings, seconds)
    return audio_info(output, settings)


def plan_alignment(segments, strategy='balanced', *, max_tempo=1.15, min_tempo=1, gap_seconds=.08, source_duration=None):
    aliases = {'preserve_original_timing': 'preserve', 'preserve_original': 'preserve',
               'natural_russian_pacing': 'natural', 'natural_pacing': 'natural',
               'balanced_alignment': 'balanced', 'manual_timing': 'manual'}
    strategy = aliases.get(strategy, strategy)
    if strategy not in {'preserve', 'natural', 'balanced', 'manual'}:
        raise ValueError('Select preserve, natural, balanced or manual alignment.')
    max_tempo = finite(max_tempo, 1, 1.25, name='Maximum allowed tempo')
    min_tempo = finite(min_tempo, .75, 1, name='Minimum allowed tempo')
    gap_seconds = finite(gap_seconds, 0, 5, name='Minimum narration pause')
    if source_duration is not None:
        source_duration = finite(source_duration, .001, 86400, name='Source duration')
    if not isinstance(segments, list) or len(segments) > 10000:
        raise ValueError('A dubbing timeline accepts at most 10,000 speech segments.')
    result, warnings = [], []
    previous_end = 0.0
    previous_original_end = 0.0
    ordered = sorted(segments, key=lambda item: (item.get('start', 0), str(item.get('id', ''))))
    for index, original in enumerate(ordered):
        item = copy.deepcopy(original)
        start = finite(item.get('start'), 0, 86400, name='Speech start')
        end = finite(item.get('end'), 0, 86400, name='Speech end')
        if end <= start:
            raise ValueError('Every speech segment needs an end after its start.')
        measured = finite((item.get('audio') or {}).get('duration'), .001, 86400, name='Generated speech duration')
        target_start, target_end = start, end
        # start/end are the user's explicit current window. ``timing`` is a
        # previous alignment result (including its measured audible end), not
        # a second editable window; reusing it would undo later timeline edits.
        if strategy == 'natural':
            pause = max(gap_seconds if index else 0, start - previous_original_end)
            target_start = max(start, previous_end + pause)
            target_end = target_start + measured
        elif strategy == 'balanced':
            target_start = max(start, previous_end)
            # Borrow real available silence, never the next speaker's window.
            if index + 1 < len(ordered):
                following = finite(ordered[index + 1].get('start'), 0, 86400, name='Next speech start')
                target_end = max(end, following - min(gap_seconds, max(0, following-end)))
            elif source_duration is not None:
                target_end = max(end, source_duration)
        available = max(.001, target_end - target_start)
        tempo = 1.0
        local = []
        if strategy != 'natural' and measured > available + .015:
            needed = measured / available
            if needed <= max_tempo:
                tempo = needed
            else:
                local.append('Speech does not fit its selected window. Shorten the Russian script, regenerate, or adjust timing; it was not aggressively accelerated or truncated.')
        elif strategy != 'natural' and measured < available - .015 and min_tempo < 1:
            tempo = max(min_tempo, measured / available)
        # A short utterance remains short; the remainder of its window is silence.
        actual_end = target_start + measured / tempo
        overlap = max(0, previous_end - target_start)
        overflow = max(0, actual_end - target_end)
        if overlap > .015:
            local.append('This segment overlaps the preceding narration; review manual timing before rendering.')
        if strategy == 'balanced' and target_start > start + .015:
            local.append('This segment starts later because the preceding Russian narration overran its window.')
        if source_duration is not None and actual_end > source_duration + .05:
            local.append('Narration extends beyond the original video. Shorten/realign it or explicitly enable an extended-video export.')
        item['timing'] = {'strategy': strategy, 'start': round(target_start, 9), 'end': round(actual_end, 9),
                          'window_start': round(start, 9), 'window_end': round(target_end, 9),
                          'measured_duration': measured, 'tempo': round(tempo, 9), 'overlap_seconds': round(overlap, 9),
                          'overflow_seconds': round(overflow, 9), 'warnings': local,
                          'needs_review': bool(local)}
        result.append(item)
        warnings.extend(str(item.get('id', index + 1)) + ': ' + value for value in local)
        previous_end = max(previous_end, actual_end)
        previous_original_end = end
    return {'segments': result, 'duration': max(source_duration or 0, previous_end),
            'strategy': strategy, 'max_tempo': max_tempo, 'min_tempo': min_tempo, 'warnings': warnings,
            'needs_review': bool(warnings), 'method': 'Measured speech durations with silence preservation and bounded FFmpeg atempo correction.'}


def render_alignment(plan, output, settings, *, allow_overlap=False):
    output = new_output(output, *((item.get('audio') or {}).get('path') for item in plan['segments']))
    segments = plan['segments']
    if not segments:
        raise ValueError('Generate at least one Russian speech segment before alignment.')
    if not allow_overlap and any(item['timing']['overlap_seconds'] > .015 for item in segments):
        raise ValueError('Narration overlaps remain. Adjust the highlighted segments before alignment.')
    total = finite(plan['duration'], .001, 86400, name='Aligned duration')
    with tempfile.TemporaryDirectory(prefix='.dubbing-align-', dir=output.parent) as temporary:
        temporary = Path(temporary)
        # Small batches avoid Windows command-line and file-descriptor limits.
        groups = []
        for group_index, begin in enumerate(range(0, len(segments), 24)):
            selected = segments[begin:begin+24]
            window_start = min(item['timing']['start'] for item in selected)
            window_end = max(item['timing']['end'] for item in selected)
            window_duration = max(.001, window_end-window_start)
            args, filters, labels = [], [], []
            for index, segment in enumerate(selected):
                source = Path(segment['audio']['path']).resolve()
                observed = audio_info(source, settings)
                if abs(observed['duration'] - segment['timing']['measured_duration']) > .025:
                    raise ValueError('Generated speech changed after timing analysis. Regenerate alignment.')
                args += ['-i', source]
                delay = int(round((segment['timing']['start']-window_start) * 1000))
                tempo = segment['timing']['tempo']
                # Some FFmpeg builds emit NOPTS from atempo's first frame;
                # normalize after the inserted silence so amix cannot discard
                # the leading pause and shift every utterance toward time zero.
                tempo_filter = f',atempo={tempo:.10g}' if abs(tempo-1) > 1e-9 else ''
                filters.append(f'[{index}:a:0]aresample=48000,aformat=channel_layouts=stereo{tempo_filter},adelay={delay}|{delay},asetpts=N/SR/TB[a{index}]')
                labels.append(f'[a{index}]')
            path = temporary / f'batch-{group_index}.wav'
            filters.append(''.join(labels) + f'amix=inputs={len(labels)}:normalize=0:dropout_transition=0,apad,atrim=end={window_duration:.9f}[out]')
            ffmpeg([*args, '-filter_complex', ';'.join(filters), '-map', '[out]', '-c:a', 'pcm_s24le', '-rf64', 'auto', path], settings, window_duration)
            groups.append((path, window_start, window_end))
        while len(groups) > 24:
            merged = []
            for offset in range(0, len(groups), 24):
                subset = groups[offset:offset+24]
                path = temporary / ('reduce-' + str(len(list(temporary.iterdir()))) + '.wav')
                start, end = min(item[1] for item in subset), max(item[2] for item in subset)
                _mix_windows(subset, path, settings, start, end)
                merged.append((path, start, end))
            for path, _, _ in groups:
                path.unlink()
            groups = merged
        _mix_windows(groups, output, settings, 0, total)
    measured = audio_info(output, settings)
    if abs(measured['duration'] - total) > .03:
        raise RuntimeError('Aligned narration failed final duration verification.')
    return {'path': str(output), **measured, 'warnings': plan.get('warnings', []),
            'segments': plan['segments'], 'method': plan['method']}


def _mix_windows(windows, output, settings, start, end):
    args, filters = [], []
    for index, (path, offset, _) in enumerate(windows):
        args += ['-i', path]
        delay = max(0, int(round((offset-start)*1000)))
        filters.append(f'[{index}:a:0]adelay={delay}|{delay},asetpts=N/SR/TB[a{index}]')
    seconds = max(.001, end-start)
    filters.append(''.join(f'[a{index}]' for index in range(len(windows))) +
                   f'amix=inputs={len(windows)}:normalize=0:dropout_transition=0,apad,atrim=end={seconds:.9f}[out]')
    ffmpeg([*args, '-filter_complex', ';'.join(filters), '-map', '[out]', '-c:a', 'pcm_s24le', '-rf64', 'auto', output], settings, seconds)


def mix_audio(narration, output, settings, options, *, original_audio=None, background=None, vocals=None, music=None):
    narration = Path(narration).resolve()
    output = new_output(output, narration, original_audio, background, vocals, music)
    info = audio_info(narration, settings)
    warnings = []
    seconds = info['duration']
    gains = {name: finite(options.get(name, default), -96, 24, name=name)
             for name, default in [('narration_db', 0), ('original_voice_db', -18), ('background_db', 0), ('music_db', 0)]}
    paths, volumes = [narration], [gains['narration_db']]
    separate = bool(background or vocals or music)
    if background:
        paths.append(Path(background).resolve()); volumes.append(gains['background_db'])
    if vocals:
        paths.append(Path(vocals).resolve()); volumes.append(gains['original_voice_db'])
    if music:
        paths.append(Path(music).resolve()); volumes.append(gains['music_db'])
    if not separate and original_audio:
        paths.append(Path(original_audio).resolve()); volumes.append(gains['original_voice_db'])
        warnings.append('The original track is not source-separated: its voice, music and ambience share the original-track gain. Independent background/music controls require separated or manually supplied stems.')
    if not music and gains['music_db'] != 0:
        warnings.append('No distinct music stem is selected; the music-only gain is not applied. Vocal/background separation does not guarantee independent music and ambience.')
    if separate and not background and gains['background_db'] != 0:
        warnings.append('No distinct background stem is selected; its independent gain is not applied.')
    for path in paths[1:]:
        seconds = max(seconds, audio_info(path, settings)['duration'])
    seconds = finite(options.get('duration', seconds), .001, 86400, name='Mix duration')
    args, graph = [], []
    for index, (path, gain) in enumerate(zip(paths, volumes)):
        args += ['-i', path]
        graph.append(f'[{index}:a:0]aresample=48000,aformat=channel_layouts=stereo,volume={gain:.9f}dB,apad,atrim=end={seconds:.9f}[p{index}]')
    if len(paths) > 1:
        bed = ''.join(f'[p{index}]' for index in range(1, len(paths)))
        graph.append(bed + f'amix=inputs={len(paths)-1}:normalize=0:dropout_transition=0[bed]')
        if options.get('ducking', True):
            requested_ducking = finite(options.get('ducking_db', -12), -36, 0, name='Ducking strength')
            ratio = min(20, 1 + abs(requested_ducking)/2)
            graph.append('[p0]asplit=2[voice][side]')
            graph.append(f'[bed][side]sidechaincompress=threshold=0.04:ratio={ratio:.9f}:attack=10:release=250[ducked]')
            graph.append('[voice][ducked]amix=inputs=2:normalize=0:dropout_transition=0[sum]')
        else:
            graph.append('[p0][bed]amix=inputs=2:normalize=0:dropout_transition=0[sum]')
    else:
        graph.append('[p0]anull[sum]')
    with tempfile.TemporaryDirectory(prefix='.dubbing-mix-', dir=output.parent) as temporary:
        raw = Path(temporary) / 'mixed.wav'
        ffmpeg([*args, '-filter_complex', ';'.join(graph), '-map', '[sum]', '-c:a', 'pcm_s24le', raw], settings, seconds)
        from .audio_effects import render_chain
        chain = copy.deepcopy(options.get('effects') or [])
        if options.get('compression', True):
            chain.append({'type': 'compressor', 'params': {'threshold_db': -18, 'ratio': 3}})
        if options.get('normalize', True):
            chain.append({'type': 'loudness', 'params': {'lufs': finite(options.get('loudness_lufs', -16), -30, -9, name='Loudness target'), 'true_peak': finite(options.get('true_peak_db', -1.5), -9, -1, name='True peak target'), 'range': 11}})
        if options.get('limiter', True):
            chain.append({'type': 'limiter', 'params': {'ceiling_db': finite(options.get('true_peak_db', -1.5), -9, -1, name='Limiter ceiling')}})
        mastering = render_chain(raw, output, chain, settings)
    measured = audio_info(output, settings)
    return {'path': str(output), **measured, 'warnings': warnings, 'mastering': mastering,
            'separate_background': separate, 'ducking': bool(options.get('ducking', True)),
            'ducking_note': 'Dynamic sidechain attenuation depends on the signal; its strength control is not a certified fixed attenuation.',
            'method': 'FFmpeg measured local mix and the existing FORGE Audio Effects renderer.'}


def export_audio(source, output, settings, *, bitrate=256):
    source = Path(source).resolve()
    output = new_output(output, source)
    info = audio_info(source, settings)
    codecs = {'.wav': ['-c:a', 'pcm_s24le'], '.flac': ['-c:a', 'flac'],
              '.m4a': ['-c:a', 'aac', '-b:a', f'{bitrate}k'], '.mp3': ['-c:a', 'libmp3lame', '-b:a', f'{bitrate}k']}
    if output.suffix.lower() not in codecs:
        raise ValueError('Select WAV, FLAC, M4A or MP3 for a separate audio export.')
    if bitrate not in {128, 192, 256, 320}:
        raise ValueError('Unsupported audio bitrate.')
    ffmpeg(['-i', source, '-map', '0:a:0', '-vn', '-map_metadata', '-1', *codecs[output.suffix.lower()], output], settings, info['duration'])
    measured = audio_info(output, settings)
    return {'path': str(output), **measured}


def encode_video(source, narration, output, settings, options, *, subtitle=None):
    source, narration = Path(source).resolve(), Path(narration).resolve()
    output = new_output(output, source, narration)
    original = media_info(source, settings)
    if not original['video_streams']:
        raise ValueError('A dubbed video export requires a source video stream.')
    audio = audio_info(narration, settings)
    if output.suffix.lower() not in {'.mp4', '.mkv'}:
        raise ValueError('Select MP4 or MKV for dubbed video export.')
    codec = options.get('video_codec', 'copy')
    if codec not in {'copy', 'h264', 'hevc', 'libx264', 'libx265', 'h264_nvenc', 'hevc_nvenc', 'av1_nvenc'}:
        raise ValueError('Select a supported video encoder.')
    encoder = {'h264': 'libx264', 'hevc': 'libx265'}.get(codec, codec)
    source_codec = original['video_streams'][0].get('codec_name', '')
    if encoder == 'copy' and output.suffix.lower() == '.mp4' and source_codec not in {'h264', 'hevc', 'mpeg4', 'av1'}:
        raise ValueError('This video codec cannot be copied into MP4. Choose MKV or explicitly select H.264/HEVC encoding.')
    duration = original['duration']
    filters = []
    if audio['duration'] > duration + .15:
        if not options.get('extend_video', False):
            raise ValueError('Russian narration is longer than the video. Shorten/realign it, or explicitly allow an extended-video export.')
        if encoder == 'copy':
            raise ValueError('Extending video requires explicit video encoding; stream copy cannot create extra frames.')
        filters.append(f'tpad=stop_mode=clone:stop_duration={audio["duration"]-duration:.9f}')
        duration = audio['duration']
    resolution = options.get('resolution', 'source')
    if options.get('preserve_resolution', True) and resolution != 'source':
        raise ValueError('Disable Preserve source resolution before selecting a different output size.')
    if options.get('preserve_resolution') is False and resolution == 'source':
        raise ValueError('Choose an explicit output resolution or enable Preserve source resolution.')
    if resolution != 'source':
        if resolution not in {'480p', '720p', '1080p', '1440p', '2160p'} or encoder == 'copy':
            raise ValueError('Changing resolution requires a supported height and explicit video encoding.')
        filters.append(f'scale=-2:{int(resolution[:-1])}')
    frame_rate = options.get('frame_rate', 'source')
    if options.get('preserve_frame_rate', True) and frame_rate != 'source':
        raise ValueError('Disable Preserve source frame rate before selecting a different rate.')
    if options.get('preserve_frame_rate') is False and frame_rate == 'source':
        raise ValueError('Choose an explicit frame rate or enable Preserve source frame rate.')
    if frame_rate != 'source':
        if encoder == 'copy':
            raise ValueError('Changing frame rate requires explicit video encoding.')
        frame_rate = finite(frame_rate, 1, 120, name='Frame rate')
        filters.append(f'fps={frame_rate:.9f}')
    args = ['-i', source, '-i', narration]
    subtitles = {'embed': 'embedded'}.get(options.get('subtitles', 'sidecar'), options.get('subtitles', 'sidecar'))
    if subtitles not in {'sidecar', 'none', 'embedded', 'burn'}:
        raise ValueError('Select supported subtitle handling.')
    if subtitle and subtitles in {'embedded', 'burn'}:
        subtitle = Path(subtitle).resolve()
        if not subtitle.is_file() or subtitle.suffix.lower() not in {'.srt', '.vtt'}:
            raise ValueError('Select a generated SRT/VTT subtitle file.')
        if subtitles == 'burn':
            # A filter-expression filename is not a general user-supplied filter.
            if encoder == 'copy':
                raise ValueError('Burning subtitles requires explicit video encoding.')
            # FFmpeg parses both the filter graph and its AVOption value. Escape
            # both layers; quotes/commas in a legitimate Windows profile path
            # must neither break the path nor inject another filter.
            raw = str(subtitle).replace('\\', '/')
            first = ''.join(('\\' + char) if char in "\\':" or char.isspace() else char for char in raw)
            escaped = ''.join(('\\' + char) if char in "\\'[],;" else char for char in first)
            filters.append('subtitles=filename=' + escaped)
        else:
            args += ['-i', subtitle]
    args += ['-map', f'0:{original["video_streams"][0]["index"]}', '-map', '1:a:0', '-map_metadata', '-1', '-map_chapters', '-1', '-c:v', encoder]
    if filters:
        args += ['-vf', ','.join(filters)]
    if encoder != 'copy':
        quality = options.get('quality', 'high')
        if quality not in {'high', 'standard', 'compact'}:
            raise ValueError('Select a supported video quality preset.')
        if encoder.endswith('_nvenc'):
            args += ['-preset', 'p5', '-cq', str({'high': 18, 'standard': 23, 'compact': 28}[quality])]
        else:
            requested_crf = options.get('crf')
            crf = int(finite(requested_crf if requested_crf is not None else {'high': 18, 'standard': 23, 'compact': 28}[quality], 12, 35, name='Video CRF'))
            args += ['-preset', 'medium', '-crf', str(crf)]
        bitrate = options.get('video_bitrate_kbps') or (options.get('bitrate', 0)/1000 if options.get('bitrate') else None)
        if bitrate is not None:
            bitrate = int(finite(bitrate, 250, 200000, name='Video bitrate'))
            args += ['-b:v', f'{bitrate}k']
    audio_codec = options.get('audio_codec', 'aac')
    if audio_codec not in {'aac', 'flac', 'pcm_s24le', 'libopus'} or (output.suffix.lower() == '.mp4' and audio_codec != 'aac'):
        raise ValueError('MP4 uses AAC; MKV also supports FLAC or PCM.')
    args += ['-af', 'apad', '-t', f'{duration:.9f}', '-c:a', audio_codec]
    if audio_codec in {'aac', 'libopus'}:
        args += ['-b:a', '256k']
    if subtitle and subtitles == 'embedded':
        args += ['-map', '2:0', '-c:s', 'mov_text' if output.suffix.lower() == '.mp4' else 'srt', '-metadata:s:s:0', 'language=rus']
    args += ['-metadata:s:a:0', 'language=rus', '-metadata:s:a:0', 'title=Russian narration']
    if options.get('provenance'):
        args += ['-metadata', 'comment=Russian localized narration generated with FORGE; AI-assisted audio. See the unsigned local provenance sidecar.']
    if output.suffix.lower() == '.mp4':
        args += ['-movflags', '+faststart']
    ffmpeg([*args, output], settings, duration)
    final = media_info(output, settings)
    if not final['audio_streams'] or not final['video_streams'] or abs(final['duration'] - duration) > .2:
        raise RuntimeError('Dubbed video failed final stream/duration verification.')
    if encoder == 'copy' and final['video_streams'][0].get('codec_name') != source_codec:
        raise RuntimeError('The copied video stream did not preserve its source codec.')
    return {'path': str(output), 'duration': final['duration'], 'video_codec': final['video_streams'][0].get('codec_name'),
            'audio_codec': final['audio_streams'][0].get('codec_name'), 'video_stream_copied': encoder == 'copy',
            'source_resolution': [original['video_streams'][0].get('width'), original['video_streams'][0].get('height')],
            'output_resolution': [final['video_streams'][0].get('width'), final['video_streams'][0].get('height')],
            'method': 'Original video stream copied without re-encoding.' if encoder == 'copy' else 'Video explicitly re-encoded using ' + encoder + '.'}


def prepare_reference(source, output, settings, *, consent=False, start=None, seconds=15):
    if consent is not True:
        raise ValueError('Approve use of this identifiable voice recording before preparing a reference.')
    source = Path(source).resolve()
    info = audio_info(source, settings)
    seconds = finite(seconds, 3, 30, name='Voice reference length')
    window = min(info['duration'], 120)
    if window < 3:
        raise ValueError('Use a permitted recording with at least three seconds of clear speech.')
    binary = executable('ffmpeg', settings)
    if not binary:
        raise RuntimeError('FFmpeg is required to inspect a voice reference.')
    result = subprocess.run([binary, '-nostdin', '-hide_banner', '-protocol_whitelist', 'file,pipe', '-i', str(source),
                             '-t', str(window), '-map', '0:a:0', '-af', 'silencedetect=noise=-40dB:d=0.25',
                             '-f', 'null', '-'], capture_output=True, timeout=180, **no_window())
    if result.returncode:
        raise RuntimeError('Voice reference inspection failed: ' + result.stderr.decode('utf-8', 'replace')[-1200:])
    text = result.stderr.decode('utf-8', 'replace')
    silences = []
    opened = None
    for match in re.finditer(r'silence_(start|end):\s*([0-9.]+)', text):
        value = min(window, float(match.group(2)))
        if match.group(1) == 'start':
            opened = value
        elif opened is not None:
            silences.append((opened, value)); opened = None
    if opened is not None:
        silences.append((opened, window))
    candidates, cursor = [], 0.0
    for silence_start, silence_end in silences:
        if silence_start > cursor:
            candidates.append((cursor, silence_start))
        cursor = max(cursor, silence_end)
    if cursor < window:
        candidates.append((cursor, window))
    candidates = sorted(candidates, key=lambda interval: interval[1]-interval[0], reverse=True)
    if start is None:
        if not candidates or candidates[0][1]-candidates[0][0] < 3:
            raise ValueError('No continuous non-silent reference of at least three seconds was found. Select a clean speech interval manually.')
        start, candidate_end = candidates[0]
        end = min(candidate_end, start + seconds)
    else:
        start = finite(start, 0, info['duration']-3, name='Reference start')
        end = min(info['duration'], start + seconds)
    prepared = extract_audio(source, output, settings, start=start, end=end, rate=24000, channels=1)
    from .audio_effects import levels
    measured = levels(output, settings)
    warnings = ['The candidate was selected using non-silent audio energy, not speaker identification. Listen and approve a recording of the intended permitted speaker.']
    if measured.get('peak_dbfs') is not None and measured['peak_dbfs'] > -.1:
        warnings.append('The reference is close to clipping; choose a cleaner recording for better cloning quality.')
    if measured.get('rms_dbfs') is not None and measured['rms_dbfs'] < -40:
        warnings.append('The reference is very quiet; choose a clearer recording.')
    return {'path': str(Path(output).resolve()), **prepared, 'start': start, 'end': end,
            'quality': {**measured, 'silence_intervals': silences, 'analysis_window_seconds': window,
                        'method': 'Measured FFmpeg silence, peak and RMS analysis; human speech/identity is not certified.'},
            'warnings': warnings, 'approved': False}
