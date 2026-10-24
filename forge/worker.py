"""Isolated, allowlisted job worker. No user-supplied shell commands are executed."""
from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from .core import ROOT, CLEAN_MEDIA, IMAGES, executable, json_write, no_window, safe_name


def emit(kind: str, **data):
    print(json.dumps({'kind': kind, **data}, ensure_ascii=False), flush=True)


def progress(value: float, message: str, *, progress_measured: bool = False, **metrics):
    emit('progress', progress=min(99, max(0, value)), message=message, progress_measured=progress_measured, **metrics)


def probe(path: Path, settings: dict) -> dict:
    binary = executable('ffprobe', settings)
    if not binary:
        raise RuntimeError('FFprobe was not found. Set its path in Environment, normally beside ffmpeg.exe.')
    p = subprocess.run([binary, '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_format', '-show_streams', '-show_chapters', '-of', 'json', str(path)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', timeout=120, **no_window())
    if p.returncode:
        raise RuntimeError('Could not inspect the media: ' + p.stderr[-1200:])
    return json.loads(p.stdout)


def duration(info: dict) -> float:
    try:
        return max(0, float(info.get('format', {}).get('duration', 0)))
    except (ValueError, TypeError):
        return 0


def run_ffmpeg(args: list[str], settings: dict, total: float = 0):
    binary = executable('ffmpeg', settings)
    if not binary:
        raise RuntimeError('FFmpeg was not found. Configure ffmpeg.exe in Environment.')
    local_args = []
    for arg in args:
        if arg == '-i':
            local_args.extend(['-protocol_whitelist', 'file,pipe'])
        local_args.append(arg)
    command = [binary, '-nostdin', '-hide_banner', '-loglevel', 'error', '-n', '-progress', 'pipe:1', *local_args]
    errors = []
    speed=0.0
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          encoding='utf-8', errors='replace', **no_window()) as p:
        def drain():
            for line in p.stderr:
                errors.append(line)
                if len(errors) > 100:
                    del errors[:50]
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        for line in p.stdout:
            key, _, value = line.strip().partition('=')
            if key=='speed':
                with contextlib.suppress(ValueError): speed=float(value.rstrip('x'))
            if key == 'out_time_us' and value.isdigit() and total:
                done=int(value)/1_000_000
                progress(100*done/total,'Processing media',progress_measured=True,stage='Media conversion',speed=f'{speed:.2f}x' if speed else '',eta_seconds=max(0,total-done)/speed if speed>0 else None)
        code = p.wait()
        reader.join(timeout=3)
    if code:
        raise RuntimeError('FFmpeg failed: ' + ''.join(errors)[-2500:])


def publish(stage: Path, destination: Path, *, allow_empty: bool = False) -> Path:
    """Publish a finished file on the same filesystem, without overwriting any file."""
    if not stage.is_file() or (stage.stat().st_size == 0 and not allow_empty):
        raise RuntimeError('Refusing to publish an empty or missing output.')
    for index in range(10000):
        name = destination.name if index == 0 else f'{destination.stem} ({index}){destination.suffix}'
        final = destination.with_name(name)
        try:
            if os.name == 'nt':
                # Windows rename fails rather than replacing an existing destination.
                os.rename(stage, final)
            else:
                # link is atomic and fails on collision; stage and final share a volume.
                os.link(stage, final)
                stage.unlink()
            return final
        except FileExistsError:
            continue
    raise RuntimeError('Too many existing output files. Choose another name or directory.')


def retune(source: Path, stage: Path, options: dict, settings: dict):
    info = probe(source, settings)
    audio = [s for s in info.get('streams', []) if s.get('codec_type') == 'audio']
    if not audio:
        raise RuntimeError('The input has no audio track.')
    sample_rate = int(audio[0].get('sample_rate') or 48000)
    ratio = options['target_hz'] / options['source_hz']
    warnings = ['Retuning assumes the supplied source tuning; it does not detect the original concert pitch.']
    if len(audio) > 1:
        warnings.append('Only the first audio track was retuned and exported.')
    # A detected input rate avoids the old 48 kHz assumption.  The small
    # tempo compensation is deterministic across FFmpeg builds; automatic
    # Rubber Band selection showed measurable short-tone drift in our tests.
    chain = f'asetrate={sample_rate}*{ratio:.12f},aresample={sample_rate}'
    if options['preserve_tempo']:
        chain += f',atempo={1/ratio:.12f}'
    warnings.append('Sample-rate pitch shift with optional tempo compensation; duration can differ slightly at file boundaries.')
    if options.get('normalize'):
        chain+=f',loudnorm=I={options["loudness_lufs"]}:TP={options["true_peak_db"]}:LRA=11,aresample={sample_rate}'
        warnings.append('Single-pass loudness normalization enabled. Target LUFS/true peak are requested, not certified measurements; review the result.')
    ext = options['format']
    args = ['-i', str(source)]
    if ext in {'mp4', 'mkv'}:
        video = [s for s in info.get('streams', []) if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
        if not video:
            raise RuntimeError('Video output requires a video source. Choose an audio format instead.')
        args += ['-map', f'0:{video[0]["index"]}', '-c:v', 'copy']
        warnings.append('Primary video is copied; subtitles, extra video tracks and attachments are not included.')
    args += ['-map', f'0:{audio[0]["index"]}', '-af', chain, '-map_metadata', '0' if options['keep_tags'] else '-1']
    if ext == 'mp3':
        args += ['-c:a','libmp3lame']
        args += ['-b:a',str(options['mp3_bitrate'])+'k'] if options.get('mp3_bitrate') else ['-q:a','2' if options['quality']=='high' else '4']
    elif ext == 'wav':
        args += ['-c:a','pcm_s16le' if options.get('wav_bits',24)==16 else 'pcm_s24le']
    elif ext == 'flac':
        args += ['-c:a', 'flac']
    else:
        args += ['-c:a', 'aac', '-b:a', '256k' if options['quality'] == 'high' else '192k']
    args += [str(stage)]
    progress(3, 'Retuning the first audio track')
    run_ffmpeg(args, settings, duration(info))
    after = probe(stage, settings)
    if not any(s.get('codec_type') == 'audio' for s in after.get('streams', [])):
        raise RuntimeError('Output verification failed: missing audio stream.')
    return warnings


def image_metadata(path: Path) -> dict:
    from .media_extensions import image_inspection
    return image_inspection(path)


def media_metadata(info: dict) -> dict:
    from .media_extensions import media_inspection
    return media_inspection(info)


def wipe(source: Path, stage: Path, settings: dict, options: dict | None = None):
    ext = source.suffix.lower()
    level = (options or {}).get('cleaning_level', 'maximum')
    if level not in {'standard', 'maximum'}:
        raise ValueError('Choose Standard or Maximum metadata reduction.')
    if ext in IMAGES:
        from .media_extensions import clean_image
        return clean_image(source, stage, level)
    if ext not in CLEAN_MEDIA:
        raise RuntimeError('This format is not supported by this cleaner. PDF and Office sanitization are deliberately not advertised.')
    info = probe(source, settings)
    before = media_metadata(info)
    types = {'audio', 'video', 'subtitle'} if level == 'standard' else {'audio', 'video'}
    keep = [s for s in info.get('streams', []) if s.get('codec_type') in types and not s.get('disposition', {}).get('attached_pic')]
    if not keep:
        raise RuntimeError('No supported audio/video stream to copy.')
    args = ['-i', str(source)]
    for s in keep:
        args += ['-map', f'0:{s["index"]}']
    args += ['-c', 'copy', '-map_metadata', '-1', '-map_metadata:s', '-1', '-map_chapters', '-1',
             '-metadata', 'title=', '-metadata', 'comment=', '-metadata', 'encoder=']
    if ext == '.mp3':
        args += ['-id3v2_version', '0', '-write_id3v1', '0']
    run_ffmpeg(args + [str(stage)], settings, duration(info))
    after_info = probe(stage, settings)
    expected = [(s.get('codec_type'), s.get('codec_name')) for s in keep]
    actual = [(s.get('codec_type'), s.get('codec_name')) for s in after_info.get('streams', [])]
    if sorted(expected) != sorted(actual):
        raise RuntimeError('Output verification failed: expected copied streams were not preserved.')
    after = media_metadata(after_info)
    stream_note = 'Audio/video and compatible subtitle streams were copied without re-encoding. Covers, attachments, data tracks and chapters were excluded.' if level == 'standard' else 'Audio/video streams were copied, not re-encoded. Covers, attachments, subtitles, data tracks and chapters were excluded.'
    return before, after, [stream_note,
                           'Containers may regenerate technical tags. This is metadata reduction, not forensic anonymization.']


def edit_metadata(source: Path,stage: Path,options: dict,settings: dict):
    if source.suffix.lower() in IMAGES:
        raise RuntimeError('Image selective editing is not enabled. Use full image metadata reduction; originals are preserved.')
    info=probe(source,settings);before=media_metadata(info)
    args=['-i',str(source),'-map','0','-c','copy','-map_metadata','0','-map_chapters','0']
    removals=options.get('remove_tags',[])
    if not removals and not options.get('tags'):
        raise RuntimeError('Choose metadata fields to remove or enter at least one replacement value.')
    for key in removals:
        for tag in (['location','location-eng','com.apple.quicktime.location.ISO6709'] if key=='location' else [key]):
            args+=['-metadata',tag+'=','-metadata:s',tag+'=']
    if options['mode']=='edit':
        for key,value in options.get('tags',{}).items():args+=['-metadata',key+'='+value]
    if source.suffix.lower()=='.mp3':args+=['-id3v2_version','3','-write_id3v1','0']
    run_ffmpeg(args+[str(stage)],settings,duration(info))
    after_info=probe(stage,settings)
    if not after_info.get('streams'):raise RuntimeError('Edited output failed verification.')
    return before,media_metadata(after_info),['Selective metadata editing preserves unselected fields and supported streams. This is not privacy sanitization.','Container/codec support determines which tags can be written. Review the private before/after report or inspect the exported copy.']


def timestamp(seconds: float, comma=False) -> str:
    n = max(0, round(seconds * 1000))
    hours, n = divmod(n, 3600000)
    minutes, n = divmod(n, 60000)
    secs, ms = divmod(n, 1000)
    return f'{hours:02}:{minutes:02}:{secs:02}{"," if comma else "."}{ms:03}'


from .transcripts import export_transcript, assign_speakers


def transcribe(source: Path, stage_dir: Path, stem: str, options: dict, data: Path):
    from .cuda_health import add_runtime_paths, runtime_report, failure_message, remember_failure, recent_failure
    from .model_cache import ensure_model
    add_runtime_paths()
    if options.get('diarization'):
        from .speaker_labels import validate
        validate(options)
    try:
        from faster_whisper import WhisperModel
        import ctranslate2
    except ImportError as exc:
        raise RuntimeError('Run Install Transcription.bat once, then restart FORGE.') from exc
    os.environ['PYANNOTE_METRICS_ENABLED']='0'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['DO_NOT_TRACK'] = '1'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    cached = ensure_model(data, options['model'], progress, options.get('model_revision', ''))
    device = options['device']
    warnings = []
    if device == 'auto':
        report = runtime_report()
        failure = recent_failure(data)
        device = 'cuda' if report['runtime_available'] and not failure else 'cpu'
        if device == 'cpu' and (failure or report.get('cuda_devices')):
            warnings.append('Auto selected CPU. ' + (failure or failure_message(report)))
    elif device == 'cuda':
        report = runtime_report()
        failure = recent_failure(data)
        if not report['runtime_available'] or failure:
            raise RuntimeError(failure or failure_message(report))
    progress(1, f'Loading {options["model"]} on {device}', stage='Model loading')

    def recognize(dev):
        model = WhisperModel(str(cached), device=dev, compute_type='float16' if dev == 'cuda' else 'int8',
                             local_files_only=True)
        started=time.monotonic()
        need_words=options['timestamps'] in {'word','sentence'} or options.get('diarization',False) or any(f in options['formats'] for f in ('srt','vtt','html','csv'))
        generated, info = model.transcribe(str(source), language=options['language'] or None,
                    task=options['task'], beam_size=options['beam_size'], vad_filter=options['vad'],
                    word_timestamps=need_words, initial_prompt=options['vocabulary'] or None)
        total = float(info.duration or 0)
        measured = math.isfinite(total) and total > 0
        segments = []
        for s in generated:
            item = {'start': s.start, 'end': s.end, 'text': s.text, 'avg_logprob': s.avg_logprob,
                    'no_speech_prob': s.no_speech_prob}
            if need_words:
                item['words'] = [{'start': w.start, 'end': w.end, 'word': w.word, 'probability': w.probability} for w in (s.words or [])]
            segments.append(item)
            elapsed=max(.001,time.monotonic()-started)
            speed=s.end/elapsed
            progress(100*s.end/total if measured else 0,f'Transcribing on {dev} · {timestamp(s.end)}',progress_measured=measured,stage='Speech recognition',speed=f'{speed:.2f}x audio',eta_seconds=max(0,total-s.end)/speed if measured and speed>0 else None)
        details = {'language': info.language, 'language_probability': info.language_probability,
                   'duration': info.duration, 'device': dev, 'task': options['task'], 'model_revision': cached.name}
        if dev == 'cuda':
            json_write(data / 'cuda-health.json', {'ready': True, 'inference_tested': True,
                       'inference_passed': True, 'checked': time.time(), 'python': sys.executable,
                       'model': options['model'], 'revision': cached.name})
        return segments, details

    try:
        segments, info = recognize(device)
    except Exception as error:
        gpu_error = any(word in str(error).lower() for word in ('cuda', 'cudnn', 'cublas', 'driver', 'device', 'float16', 'out of memory'))
        if device == 'cuda' and gpu_error and 'out of memory' not in str(error).lower():
            remember_failure(data, str(error))
        if options['device'] == 'auto' and device == 'cuda' and gpu_error:
            warnings.append('GPU execution failed; Auto restarted this job on CPU.')
            emit('warning', message=warnings[-1])
            segments, info = recognize('cpu')
        else:
            raise
    if options.get('diarization') and segments:
        from .speaker_labels import diarize
        progress(88,'Separating speakers locally',stage='Speaker labeling')
        turns=diarize(source,stage_dir,options)
        segments=assign_speakers(segments,turns)
        info['speaker_labeling']='pyannote Community-1; labels are anonymous estimates'
        warnings.append('Speaker labels are estimated, not identities. Overlap or background speech can require manual review.')
    if options.get('style')=='clean':
        warnings.append('A cleaned derivative was created. Raw TXT/JSON files are retained separately; cleanup does not fact-check or invent words.')
    if not segments:
        warnings.append('No speech segments were detected. Empty transcript output should be reviewed.')
    return export_transcript(segments, info, options, stage_dir, stem), warnings


def download(url: str, stage_dir: Path, options: dict, settings: dict, tool: str, *, resume_dir=None):
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError('Run Install Downloads.bat once, then restart FORGE.') from exc
    # Disable automatic third-party plugin discovery; no config or shell hooks are loaded.
    try:
        from yt_dlp.globals import plugin_dirs
        plugin_dirs.value = []
    except ImportError:
        pass
    last_progress = [0.0]
    warnings = []
    class Logger:
        def debug(self, message): pass
        def info(self, message): pass
        def warning(self, message):
            if len(warnings) < 12:
                from .download_support import diagnostic
                warning=diagnostic(message)['message']
                if warning not in warnings: warnings.append(warning)
        def error(self, message): pass

    def hook(d):
        now = time.monotonic()
        if now - last_progress[0] < 0.3:
            return
        last_progress[0] = now
        total = d.get('total_bytes') or 0
        measured = total > 0 and not options.get('playlist')
        if d.get('status') == 'downloading':
            done = d.get('downloaded_bytes', 0)
            progress(100*done/total if measured else 0, 'Downloading playlist item '+str(d.get('info_dict',{}).get('playlist_index','?')) if options.get('playlist') else 'Downloading media',progress_measured=bool(measured),stage='Playlist / current file' if options.get('playlist') else 'Download',speed=(f'{d.get("speed",0)/1048576:.2f} MB/s' if d.get('speed') else ''),eta_seconds=d.get('eta') if measured else None)
        elif d.get('status') == 'finished':
            progress(0, 'Download finished; post-processing', progress_measured=False, stage='Post-processing')
    ff = executable('ffmpeg', settings)
    if not ff or not executable('ffprobe', settings):
        raise RuntimeError('Download conversion/merging requires FFmpeg and FFprobe. Configure them in Environment.')
    params = {'outtmpl': str(stage_dir / '%(title).120B [%(id)s].%(ext)s'), 'paths': {'home': str(stage_dir)},
              'quiet': True, 'no_warnings': True, 'logger': Logger(), 'progress_hooks': [hook],
              'noplaylist': not options.get('playlist',False), 'windowsfilenames': True, 'restrictfilenames': False,
              'overwrites': False, 'socket_timeout': 25, 'retries': 2, 'extractor_retries': 0,
              'ffmpeg_location': str(Path(ff).parent), 'cachedir': False, 'remote_components': [],
              'postprocessors': [], 'writethumbnail': options.get('thumbnail',False), 'writeinfojson': False,
              'continuedl': options.get('resume',True), 'trim_file_name':200,
              'match_filter': lambda info, **kwargs: 'Live broadcasts are not enabled.' if info.get('is_live') and not options.get('capture_live') else None}
    templates={'title_id':'%(title).120B [%(id)s].%(ext)s','channel_title':'%(uploader).60B - %(title).100B [%(id)s].%(ext)s','playlist_index':'%(playlist_index)03d - %(title).100B [%(id)s].%(ext)s'}
    params['outtmpl']=str(stage_dir/templates[options.get('filename_style','title_id')])
    if options.get('filename_template'):
        from .download_support import validate_template
        params['outtmpl']=str(stage_dir/validate_template(options['filename_template']))
    if resume_dir is not None and options.get('resume',True):
        # Relative outtmpl is required for yt-dlp's home/temp split. Partial
        # fragments remain in private app data across failed/cancelled retries.
        params['outtmpl']=Path(params['outtmpl']).name
        params['paths']={'home':str(stage_dir),'temp':str(resume_dir)}
    if options.get('playlist'):
        params.update(playliststart=options['playlist_start'],playlistend=options['playlist_end'])
    if options.get('subtitles') or options.get('auto_subtitles'):
        params.update(writesubtitles=options.get('subtitles',False),writeautomaticsub=options.get('auto_subtitles',False),subtitleslangs=options.get('subtitle_languages','en').split(','),subtitlesformat='srt/vtt/best')
    js = {}
    for name in ('deno', 'node'):
        if shutil.which(name):
            js[name] = {'path': shutil.which(name)}
    if js:
        params['js_runtimes'] = js
    if options.get('cookies'):
        # Copy only within private work storage so yt-dlp cannot rewrite the user's cookie file.
        cookie_copy = stage_dir / '_private_cookies.txt'
        shutil.copyfile(options['cookies'], cookie_copy)
        cookie_copy.chmod(0o600)
        params['cookiefile'] = str(cookie_copy)
    if tool == 'download_audio':
        params['format'] = 'bestaudio/best'
        params['postprocessors'].append({'key': 'FFmpegExtractAudio', 'preferredcodec': options['format'],
                                         'preferredquality': '0' if options['quality'] == 'best' else options['quality']})
    else:
        cap = f'[height<={options["max_height"]}]' if options['max_height'] else ''
        if options['format'] == 'mp4':
            params['format'] = f'bv*{cap}[ext=mp4]+ba[ext=m4a]/b{cap}[ext=mp4]'
            params['merge_output_format'] = 'mp4'
        else:
            params['format'] = f'bv*{cap}+ba/b{cap}'
            params['merge_output_format'] = 'mkv'
            params['postprocessors'].append({'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mkv'})
    if options['embed_metadata']:
        params['postprocessors'].append({'key': 'FFmpegMetadata', 'add_metadata': True})
    with yt_dlp.YoutubeDL(params) as ydl:
        try:ydl.extract_info(url,download=True)
        except Exception as exc:
            from .download_support import diagnostic
            message=diagnostic(exc)['message']
            if not options.get('playlist'):raise RuntimeError(message) from None
            warnings.append('Playlist stopped before all requested entries completed. '+message)
    files = [p for p in stage_dir.iterdir() if p.is_file() and p.suffix.lower() in {'.mp3','.m4a','.flac','.wav','.mp4','.mkv','.webm','.opus','.ogg'}]
    if not files:
        raise RuntimeError('No complete media output was produced.')
    for p in files:
        if not probe(p, settings).get('streams'):
            raise RuntimeError('Downloaded output failed media verification.')
    if options.get('subtitles') or options.get('auto_subtitles'):
        files.extend(p for p in stage_dir.iterdir() if p.is_file() and p.suffix.lower() in {'.srt','.vtt','.ass'})
    if options.get('thumbnail'):
        from PIL import Image
        for p in stage_dir.iterdir():
            if p.is_file() and p.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}:
                with Image.open(p) as image: image.verify()
                files.append(p)
    return files, warnings


def run(payload: dict):
    tool, options, settings = payload['tool'], payload['options'], payload['settings']
    from .models import OPTION_MODELS
    if tool in OPTION_MODELS:options=OPTION_MODELS[tool].model_validate(options).model_dump()  # Fill new defaults for preserved V0.1 jobs.
    dest = Path(payload['destination']).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    data = Path(payload['data'])
    job_id = payload['job_id']
    stage_dir = dest / ('.forge-' + job_id + '.work')
    stage_dir.mkdir(exist_ok=False)
    warnings = []
    published = []
    manifest = data / 'work' / job_id / 'result.json'
    try:
        source = Path(payload['source']['path']) if payload.get('source') else None
        if source:
            stat = source.stat()
            expected = payload.get('source_stat', {})
            if expected and (stat.st_size != expected['size'] or stat.st_mtime_ns != expected['mtime_ns']):
                raise RuntimeError('The source changed after being queued. Select the file again before retrying.')
        # Assets and comparison inputs are immutable selections too. Never let
        # a changed logo, reference signature or comparison silently alter a job.
        for selected in [*payload.get('asset_snapshots', []),
                         *payload.get('comparison_sources', []),
                         *payload.get('reference_sources', [])]:
            if not isinstance(selected, dict) or not selected.get('path'):
                continue
            stat = Path(selected['path']).stat()
            expected = selected.get('source_stat')
            if expected and {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns} != expected:
                raise RuntimeError('An asset or comparison file changed after being queued. Select the inputs again.')
        if tool=='model_download':
            if not options.get('consent'):raise RuntimeError('Model download consent is required.')
            os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
            from .model_cache import ensure_model
            cached=ensure_model(data,options['model'],progress,options.get('revision',''))
            report=stage_dir/(safe_name(options['model'])+'_model-ready.json')
            json_write(report,{'model':options['model'],'cache':str(cached),'note':'Downloaded/cached; inference has not been tested.'})
            files=[report]
        elif tool == 'qr':
            from .media_extensions import qr_bytes, qr_sensitive
            if qr_sensitive(options):
                raise ValueError('Wi-Fi QR codes use the private preview/export endpoint; secret values cannot be processed in job history.')
            path = stage_dir / (safe_name(options['name']) + '.' + options['format'])
            path.write_bytes(qr_bytes(options))
            files = [path]
        elif tool == 'retune':
            path = stage_dir / (safe_name(source.stem) + f'_{options["target_hz"]:g}hz.' + options['format'])
            warnings = retune(source, path, options, settings)
            files = [path]
        elif tool == 'metadata':
            path = stage_dir / (safe_name(source.stem) + ('_metadata-reduced' if options.get('mode','wipe')=='wipe' else '_metadata-edited') + source.suffix.lower())
            if options.get('mode','wipe')=='wipe':
                before,after,warnings=wipe(source,path,settings,options)
            else:
                before,after,warnings=edit_metadata(source,path,options,settings)
            files = [path]
            from .media_extensions import capabilities, metadata_inventory
            remaining = metadata_inventory(after)
            if options['audit_report']:
                report = stage_dir / (path.stem + '_PRIVATE-audit.json')
                json_write(report, {'warning': 'This private report contains metadata from the ORIGINAL file. Do not publish it with the cleaned file.',
                                    'before': before, 'after': after, 'remaining': remaining,
                                    'capabilities': capabilities(source.suffix), 'limitations': warnings})
                files.append(report)
            if options.get('remaining_report'):
                report = stage_dir / (path.stem + '_remaining-metadata.json')
                json_write(report, {'warning': 'Remaining metadata can still contain private fields. Review before sharing.',
                                    'remaining': remaining, 'capabilities': capabilities(source.suffix), 'limitations': warnings})
                files.append(report)
        elif tool == 'transcribe':
            files, warnings = transcribe(source, stage_dir, safe_name(source.stem) + '_transcript', options, data)
        elif tool in {'download_audio','download_video'}:
            import hashlib
            from .model_cache import cache_lock
            key=hashlib.sha256(str(payload.get('resume_key') or payload.get('watch_media') or job_id).encode()).hexdigest()
            resume_dir=data/'download-cache'/key
            with cache_lock(resume_dir/'.lock',lambda *a,**k:progress(0,'Waiting for the previous download attempt',stage='Download resume'),timeout=60):
                files, warnings = download(payload['url'], stage_dir, options, settings, tool, resume_dir=resume_dir)
        elif tool == 'watermark':
            from .watermark import process
            files, warnings = process(source, stage_dir, options, settings, context=payload)
        elif tool in {'audio_fingerprint', 'video_fingerprint'}:
            from .fingerprints import process_audio, process_video
            process = process_audio if tool == 'audio_fingerprint' else process_video
            files, warnings = process(source, stage_dir, options, settings, context=payload)
        elif tool in {'tracking_audit', 'provenance'}:
            from .media_inspection import process_audit, process_provenance
            process = process_audit if tool == 'tracking_audit' else process_provenance
            files, warnings = process(source, stage_dir, options, settings, context=payload)
        elif tool == 'privacy_export':
            from .privacy_export import process
            files, warnings = process(source, stage_dir, options, settings, context=payload)
        elif tool == 'privacy_verify':
            from .privacy_verification import process
            files, warnings = process(source, stage_dir, options, settings, context=payload)
        else:
            raise RuntimeError('Tool is not registered.')
        # Signal finalization before atomically committing finished outputs.
        emit('finalizing', message='Verifying and publishing outputs')
        # Publish actual media first. Collision resolution can change its name;
        # private verification reports must describe that real final filename.
        files = [Path(path) for path in files]
        if tool in {'privacy_export', 'privacy_verify'}:
            files.sort(key=lambda path: path.suffix.lower() in {'.json', '.html'})
        published_paths = {}
        reports_finalized = False
        for path in files:
            if (tool in {'privacy_export', 'privacy_verify'} and not reports_finalized
                    and path.suffix.lower() in {'.json', '.html'}):
                from .privacy_verification import finalize_reports
                finalize_reports(files, published_paths)
                reports_finalized = True
            # Empty text is useful when no speech is detected, but make it explicit.
            if path.stat().st_size == 0 and path.suffix == '.txt':
                path.write_text('[No speech detected]\n', encoding='utf-8')
            final = publish(path, dest / path.name, allow_empty=(tool == 'transcribe' and path.suffix == '.srt'))
            published_paths[str(path)] = str(final)
            stat = final.stat()
            published.append({'name': final.name, 'path': str(final), 'size': stat.st_size,
                              'source_stat': {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}})
            json_write(manifest, {'outputs': published, 'warnings': warnings, 'complete': False})
        result = {'outputs': published, 'warnings': warnings, 'complete': True}
        json_write(manifest, result)
        emit('result', **result)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


def watch_parent(pid: int, started: float):
    import psutil
    while True:
        time.sleep(2)
        try:
            p = psutil.Process(pid)
            if abs(p.create_time() - started) < 1:
                continue
        except psutil.Error:
            pass
        # No automatic restart: stop native children if the service disappears.
        for child in psutil.Process().children(recursive=True):
            with contextlib.suppress(psutil.Error):
                child.kill()
        os._exit(2)


if __name__ == '__main__':
    try:
        payload = json.load(sys.stdin)
        if payload.get('parent_pid'):
            threading.Thread(target=watch_parent, args=(payload['parent_pid'], payload['parent_started']), daemon=True).start()
        run(payload)
    except Exception as error:
        emit('error', message=str(error)[:4000])
        sys.exit(1)
