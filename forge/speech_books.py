"""Bounded local document import and resumable, content-addressed speech assembly.

Importing a document never evaluates HTML, fetches URLs, extracts ZIP members, or
overwrites the original. Model inference is delegated to the isolated adapters.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit
import wave
from xml.etree import ElementTree as ET
import zipfile

from .core import executable, json_write, no_window, safe_name

MAX_INPUT = 32 * 1024 * 1024
MAX_TEXT = 1_000_000
MAX_CHAPTERS = 2000
RATE = 24000


class _TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = []

    def handle_starttag(self, tag, attrs):
        if self.skip or tag in {'script', 'style', 'head', 'noscript', 'iframe', 'object', 'svg'}:
            if tag not in {'br', 'hr', 'img', 'meta', 'link', 'input'}:
                self.skip.append(tag)
            return
        if tag in {'p', 'div', 'section', 'article', 'br', 'li', 'tr', 'blockquote'}:
            self.parts.append('\n')
        if re.fullmatch('h[1-6]', tag):
            self.parts.append('\n\n' + '#' * int(tag[1]) + ' ')

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag in {'p', 'div', 'section', 'article', 'li', 'tr', 'blockquote'} or re.fullmatch('h[1-6]', tag):
            self.parts.append('\n\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _html_text(value):
    parser = _TextHTML()
    parser.feed(value)
    return re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', ''.join(parser.parts)).strip()


def _chapters(text, fallback):
    """Only explicit headings/chapter labels are split; prose is left intact."""
    lines = text.splitlines(keepends=True)
    chapters = []
    title = fallback
    body = []
    for line in lines:
        heading = re.match(r'^\s*(?:#{1,2}\s+(.+)|((?:chapter|part|book)\s+(?:\d+|[IVXLCDM]+)\b.*))\s*$', line, re.I)
        if heading:
            if ''.join(body).strip():
                chapters.append({'title': title, 'text': ''.join(body).strip()})
            title = (heading.group(1) or heading.group(2)).strip()
            body = [line]
        else:
            body.append(line)
    if ''.join(body).strip():
        chapters.append({'title': title, 'text': ''.join(body).strip()})
    return chapters


def _xml(data):
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)', data, re.I):
        raise ValueError('XML document type declarations and entities are not supported.')
    return ET.fromstring(data)


def _zip_name(value):
    if not value or '\\' in value or '\x00' in value or value.startswith('/'):
        raise ValueError('Unsafe EPUB archive member.')
    path = PurePosixPath(value.rstrip('/'))
    if any(part in {'.', '..'} or ':' in part for part in path.parts):
        raise ValueError('Unsafe EPUB archive member.')
    return str(path)


def _epub(path):
    warnings = []
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > 2000 or sum(i.file_size for i in members) > 64 * 1024 * 1024:
            raise ValueError('EPUB exceeds the bounded import size.')
        names = {}
        for entry in members:
            name = _zip_name(entry.filename)
            if name in names or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('EPUB has duplicate paths or symbolic links.')
            if entry.file_size > 8 * 1024 * 1024 or entry.flag_bits & 1:
                raise ValueError('EPUB has an oversized or encrypted member.')
            names[name] = entry
        container = _xml(archive.read(names['META-INF/container.xml']))
        rootfile = next((n.attrib.get('full-path') for n in container.iter() if n.tag.rsplit('}', 1)[-1] == 'rootfile'), None)
        opf_name = _zip_name(rootfile or '')
        package = _xml(archive.read(names[opf_name]))
        title = next((n.text for n in package.iter() if n.tag.rsplit('}', 1)[-1] == 'title' and n.text), path.stem)
        manifest = {n.attrib.get('id'): n.attrib for n in package.iter() if n.tag.rsplit('}', 1)[-1] == 'item'}
        ordered = [n.attrib.get('idref') for n in package.iter() if n.tag.rsplit('}', 1)[-1] == 'itemref' and n.attrib.get('linear') != 'no']
        if not ordered:
            raise ValueError('EPUB has no readable spine.')
        chapters = []
        for identity in ordered:
            item = manifest.get(identity, {})
            if item.get('media-type') not in {'application/xhtml+xml', 'text/html'}:
                warnings.append('A non-text EPUB spine item was skipped.')
                continue
            href = urlsplit(item.get('href', ''))
            if href.scheme or href.netloc or href.query:
                raise ValueError('External EPUB references are not imported.')
            decoded = unquote(href.path)
            if any(p == '..' for p in PurePosixPath(decoded).parts) or decoded.startswith('/') or '\\' in decoded:
                raise ValueError('Unsafe EPUB spine reference.')
            name = _zip_name(posixpath.join(posixpath.dirname(opf_name), decoded))
            text = _html_text(archive.read(names[name]).decode('utf-8-sig'))
            chapters.extend(_chapters(text, 'Chapter ' + str(len(chapters) + 1)))
        return title, chapters, warnings


def import_document(path: Path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_INPUT:
        raise ValueError('Choose a local document no larger than 32 MiB.')
    ext = path.suffix.lower()
    warnings = []
    title = path.stem
    if ext == '.epub':
        try:
            title, chapters, warnings = _epub(path)
        except (KeyError, ET.ParseError, UnicodeError, zipfile.BadZipFile) as error:
            raise ValueError('The EPUB structure could not be read safely.') from error
    elif ext == '.pdf':
        try:
            from pypdf import PdfReader
        except ImportError as error:
            raise ValueError('PDF import requires the optional document runtime (pypdf). TXT, EPUB and HTML import remain available.') from error
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError('Encrypted PDF documents must be unlocked before import.')
        if len(reader.pages) > MAX_CHAPTERS:
            raise ValueError('PDF page count exceeds the import limit.')
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title)[:300]
        text = '\n\n'.join(page.extract_text() or '' for page in reader.pages)
        chapters = _chapters(text, title)
        warnings.append('PDF text extraction preserves available text, not visual layout. OCR was not performed.')
    elif ext in {'.txt', '.md', '.markdown', '.html', '.htm', '.srt', '.vtt'}:
        try:
            text = path.read_text(encoding='utf-8-sig')
        except UnicodeError as error:
            raise ValueError('Save text documents using UTF-8 before importing.') from error
        if ext in {'.html', '.htm'}:
            text = _html_text(text)
        elif ext in {'.srt', '.vtt'}:
            lines = []
            in_note = False
            for line in text.splitlines():
                value = line.strip()
                if not value:
                    in_note = False
                    continue
                if value.startswith(('NOTE', 'STYLE', 'REGION')):
                    in_note = True
                if in_note or value == 'WEBVTT' or value.isdigit() or '-->' in value:
                    continue
                lines.append(_html_text(value))
            text = '\n\n'.join(lines)
            warnings.append('Subtitle text was imported; original cue timing is not a TTS alignment.')
        chapters = _chapters(text, title)
    else:
        raise ValueError('Supported documents: EPUB, PDF, TXT, Markdown, HTML, SRT and VTT.')
    text = '\n\n'.join(c['text'] for c in chapters)
    if not text.strip():
        raise ValueError('The document contains no extractable text. Scanned pages require a separately approved OCR workflow.')
    if len(text) > MAX_TEXT or len(chapters) > MAX_CHAPTERS:
        raise ValueError('Document text or chapter count exceeds the import limit.')
    return {'name': str(title)[:300], 'text': text, 'chapters': chapters, 'warnings': warnings}


def _digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _wave_info(path):
    with wave.open(str(path), 'rb') as audio:
        if audio.getcomptype() != 'NONE' or audio.getsampwidth() != 2 or audio.getnchannels() != 1 or audio.getframerate() != RATE or audio.getnframes() < 1:
            raise ValueError('A rendered speech segment is not valid 24 kHz mono PCM audio.')
        return audio.getnframes() / RATE


def _ffmpeg(settings, arguments):
    binary = executable('ffmpeg', settings)
    if not binary:
        raise ValueError('FFmpeg is required to assemble speech audio.')
    local = []
    for argument in arguments:
        if argument == '-i':
            local.extend(['-protocol_whitelist', 'file,pipe'])
        local.append(argument)
    result = subprocess.run([binary, '-nostdin', '-hide_banner', '-loglevel', 'error', '-n', *local], capture_output=True, timeout=1800, **no_window())
    if result.returncode:
        raise ValueError('Speech audio conversion failed: ' + result.stderr.decode('utf-8', 'replace')[-1200:])


def _canonical(source, target, settings):
    _ffmpeg(settings, ['-i', str(source), '-map_metadata', '-1', '-vn', '-ar', str(RATE), '-ac', '1', '-c:a', 'pcm_s16le', str(target)])
    _wave_info(target)


def _silence(path, milliseconds):
    count = round(RATE * milliseconds / 1000)
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, RATE, count, 'NONE', 'not compressed'))
        remaining = count
        while remaining:
            n = min(remaining, RATE)
            output.writeframesraw(b'\x00\x00' * n)
            remaining -= n


def _join(paths, output):
    with wave.open(str(output), 'wb') as writer:
        writer.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        for path in paths:
            _wave_info(path)
            with wave.open(str(path), 'rb') as reader:
                while data := reader.readframes(RATE * 5):
                    writer.writeframesraw(data)
    return _wave_info(output)


def _effects(source, output, chain, settings):
    if chain:
        from .audio_effects import render_chain
        render_chain(source, output, chain, settings)
    else:
        shutil.copyfile(source, output)
    return output


def _cache_segment(text, options, data, settings, progress, *, cache_only=False):
    from .speech_engines import resolve_options, synthesize
    from .model_cache import cache_lock
    resolved = resolve_options(options, data)
    reference = resolved.get('reference_audio')
    # The same filename can later contain a different permitted reference voice.
    # Cache identity follows those bytes as well as the pinned model/options.
    reference_digest = _digest(Path(reference)) if reference else None
    key = hashlib.sha256(json.dumps({'schema': 1, 'text': text, 'options': resolved, 'reference_sha256': reference_digest}, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    folder = Path(data) / 'speech' / 'audio' / key[:2] / key
    folder.mkdir(parents=True, exist_ok=True)
    audio = folder / 'speech.wav'
    receipt = folder / 'receipt.json'
    with cache_lock(folder / '.lock', progress, timeout=300):
        if audio.is_file() and receipt.is_file():
            try:
                saved = json.loads(receipt.read_text())
                if saved['sha256'] == _digest(audio) and saved['key'] == key:
                    _wave_info(audio)
                    return audio, {**saved, 'reused': True}
            except (KeyError, ValueError, OSError, wave.Error):
                pass
        if cache_only:
            raise ValueError('Export requires verified generated audio for every selected segment. Generate the missing or changed segments first.')
        with tempfile.TemporaryDirectory(prefix='render-', dir=folder) as temporary:
            raw = Path(temporary) / 'raw.wav'
            metadata = synthesize(text, resolved, data, raw, progress)
            canonical = Path(temporary) / 'speech.wav'
            _canonical(raw, canonical, settings)
            saved = {'key': key, 'sha256': _digest(canonical), 'duration': _wave_info(canonical), 'options': resolved, 'reference_sha256': reference_digest, 'inference': metadata}
            os.replace(canonical, audio)
            json_write(receipt, saved)
        return audio, {**saved, 'reused': False}


def _metadata_escape(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('=', '\\=').replace(';', '\\;').replace('#', '\\#')


def _bounded_segments(segments, limit=4000):
    """Bound inference windows even for prose without sentence punctuation."""
    for segment in segments:
        if segment['kind'] != 'speech' or len(segment['text']) <= limit:
            yield segment
            continue
        remaining = segment['text']
        index = 0
        while remaining:
            end = len(remaining) if len(remaining) <= limit else remaining.rfind(' ', 0, limit)
            if end < limit // 2:
                end = min(limit, len(remaining))
            text = remaining[:end].strip()
            remaining = remaining[end:].lstrip()
            if not text:
                continue
            identity = hashlib.sha256(json.dumps([segment.get('id'), text, index]).encode()).hexdigest()[:32]
            yield {**segment, 'id': identity, 'parent_id': segment.get('id'), 'text': text}
            index += 1


def _export(source, output, fmt, settings, metadata, chapters=None, cover=None):
    if fmt not in {'wav', 'flac', 'mp3', 'opus', 'm4b'}:
        raise ValueError('Choose WAV, FLAC, MP3, OPUS or M4B speech export.')
    args = ['-i', str(source)]
    metadata_file = None
    if fmt == 'm4b' and chapters:
        metadata_file = output.with_suffix('.ffmetadata')
        value = ';FFMETADATA1\n'
        for chapter in chapters:
            value += '\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=' + str(round(chapter['start'] * 1000)) + '\nEND=' + str(round(chapter['end'] * 1000)) + '\ntitle=' + _metadata_escape(chapter['title']) + '\n'
        metadata_file.write_text(value, encoding='utf-8')
        args += ['-f', 'ffmetadata', '-i', str(metadata_file)]
    if cover:
        cover = Path(cover)
        if not cover.is_file() or cover.suffix.lower() not in {'.png', '.jpg', '.jpeg'} or cover.stat().st_size > 10_000_000:
            raise ValueError('Choose a local PNG/JPEG cover no larger than 10 MB.')
        from PIL import Image
        with Image.open(cover) as image:
            image.verify()
        args += ['-i', str(cover)]
        cover_index = 2 if metadata_file else 1
        if fmt not in {'mp3', 'm4b', 'flac'}:
            raise ValueError('Cover images are supported for MP3, FLAC and M4B exports.')
        args += ['-map', '0:a:0', '-map', str(cover_index) + ':v:0', '-c:v', 'mjpeg', '-disposition:v', 'attached_pic']
    else:
        args += ['-map', '0:a:0', '-vn']
    if metadata_file:
        args += ['-map_chapters', '1']
    codecs = {'wav': ['-c:a', 'pcm_s16le'], 'flac': ['-c:a', 'flac'], 'mp3': ['-c:a', 'libmp3lame', '-q:a', '2'], 'opus': ['-c:a', 'libopus', '-b:a', '96k'], 'm4b': ['-c:a', 'aac', '-b:a', '128k', '-f', 'ipod']}
    args += codecs[fmt]
    for key, value in metadata.items():
        if key not in {'title', 'artist', 'album', 'copyright', 'description', 'genre', 'date'} or not isinstance(value, str) or len(value) > 2000:
            raise ValueError('Unsupported or oversized audiobook metadata.')
        args += ['-metadata', key + '=' + value]
    try:
        _ffmpeg(settings, args + [str(output)])
    finally:
        if metadata_file:
            metadata_file.unlink(missing_ok=True)
    from .worker import probe
    info = probe(output, settings)
    if not any(s.get('codec_type') == 'audio' for s in info.get('streams', [])):
        raise ValueError('Speech export has no playable audio stream.')


def process(payload: dict, stage_dir: Path, data: Path):
    """Render a queued immutable project snapshot; completed synthesis survives retry.

    The worker must not instantiate Store: its startup recovery logic intentionally
    marks running jobs interrupted. Parent snapshots the saved project at enqueue.
    """
    from .speech_documents import parse_script, EMOTIONS, READING_STYLES
    from .worker import progress
    options = dict(payload.get('options', {}))
    settings = payload.get('settings', {})
    project = payload.get('speech_project') or options.get('project') or {}
    supplied = options.get('text')
    import_warnings = []
    if not project and not supplied and payload.get('source'):
        project = import_document(Path(payload['source']['path']))
        import_warnings = project['warnings']
    chapters = list(project.get('chapters') or [])
    if supplied or options.get('scope') in {'selection', 'preview'}:
        chapters = [{'id': 'selection', 'title': options.get('name') or 'Selection', 'text': supplied}]
    elif not chapters and project.get('text'):
        chapters = [{'id': 'document', 'title': project.get('name', 'Narration'), 'text': project['text']}]
    chapter_id = options.get('chapter_id')
    chapter_ids = options.get('chapter_ids') or []
    if chapter_ids:
        if not isinstance(chapter_ids, list) or any(not isinstance(value, str) for value in chapter_ids) or len(chapter_ids) > MAX_CHAPTERS:
            raise ValueError('Choose a bounded list of chapter IDs.')
        if set(chapter_ids) - {chapter.get('id') for chapter in chapters}:
            raise ValueError('A selected chapter is no longer available.')
        chapters = [chapter for chapter in chapters if chapter.get('id') in chapter_ids]
    if chapter_id:
        chapters = [c for c in chapters if c.get('id') == chapter_id]
    if not chapters or len(chapters) > MAX_CHAPTERS:
        raise ValueError('Choose a nonempty project, chapter or text selection.')
    if sum(len(c.get('text', '')) for c in chapters) > MAX_TEXT:
        raise ValueError('Speech document exceeds the bounded render size.')
    stage_dir = Path(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    fmt = options.get('format', 'wav')
    name = safe_name(options.get('name') or project.get('name') or 'Narration')
    metadata = dict(options.get('metadata') or {})
    base = {**project.get('options', {}), **options}
    speakers = project.get('speakers') or options.get('speakers') or {}
    warnings = list(import_warnings)
    records = []
    chapter_audio = []
    original_audio = []
    chapter_timing = []
    position = 0.0
    with tempfile.TemporaryDirectory(prefix='speech-assembly-', dir=stage_dir) as temporary:
        work = Path(temporary)
        for chapter_index, chapter in enumerate(chapters):
            segments = list(_bounded_segments(parse_script(chapter.get('text', ''), speaker=base.get('speaker', 'narrator'), emotion=base.get('emotion', 'neutral'), style=base.get('style', 'normal'), pronunciation=base.get('pronunciation'), pauses=base.get('pauses'), language=base.get('language', 'en'))))
            if chapter_index < len(chapters) - 1:
                saved_style = base.get('style', 'normal')
                preset = saved_style if isinstance(saved_style, dict) else next((style for style in READING_STYLES if style['id'] == saved_style), READING_STYLES[0])
                gap = base.get('pauses', {}).get('chapter', preset['pauses']['chapter'])
                if segments and segments[-1]['kind'] == 'silence':
                    if segments[-1].get('method') != 'manual':
                        segments[-1]['pause_ms'] = max(gap, segments[-1]['pause_ms'])
                else:
                    segments.append({'kind': 'silence', 'pause_ms': gap, 'method': 'chapter'})
            paths = []
            raw_paths = []
            for index, segment in enumerate(segments):
                part = work / f'c{chapter_index}-s{index}.wav'
                if segment['kind'] == 'silence':
                    _silence(part, segment['pause_ms'])
                    paths.append(part)
                    raw_paths.append(part)
                    continue
                if segment['kind'] != 'event' and not segment.get('text', '').strip():
                    continue
                if segment.get('speaker', 'narrator') != 'narrator' and segment.get('speaker') not in speakers:
                    raise ValueError('Assign a voice to speaker ' + segment['speaker'] + ' before generating this passage.')
                selected = {**base, **speakers.get(segment.get('speaker'), {})}
                # Document/project control fields are not adapter arguments.
                keys = {'engine', 'voice', 'model_revision', 'voice_revision', 'language', 'speed', 'device', 'blend', 'seed', 'task', 'instructions', 'reference_audio', 'reference_text', 'consent', 'dialect', 'attribute_speed', 'attribute_pitch', 'attribute_volume'}
                engine_options = {key: value for key, value in selected.items() if key in keys}
                if selected.get('revision') and not engine_options.get('model_revision'):
                    engine_options['model_revision'] = selected['revision']
                if selected.get('reference') and not engine_options.get('reference_audio'):
                    engine_options['reference_audio'] = selected['reference']
                if engine_options.get('task') == 'voice':
                    engine_options['task'] = 'IP' if engine_options.get('engine') == 'ming' else 'TTS'
                # Empty optional task fields are omitted. Incompatible nonempty
                # fields remain so the engine's strict task validator rejects them.
                engine_options = {key: value for key, value in engine_options.items() if value not in ('', None)}
                emotion = segment.get('emotion', 'neutral')
                expression = (base.get('emotion_profiles') or {}).get(emotion)
                expression_chain = []
                if emotion != 'neutral':
                    if not expression and engine_options.get('engine', 'kokoro') == 'kokoro':
                        expression = next((item.get('fallback') for item in EMOTIONS if item['id'] == emotion), None)
                    if expression and expression.get('method') == 'effects':
                        expression_chain = expression.get('chain') or []
                        if not expression_chain:
                            raise ValueError('The selected acoustic expression has no configured effects.')
                        warnings.append('Expression ' + emotion + ' used acoustic effects; this is not native emotional synthesis.')
                    elif engine_options.get('engine', 'kokoro') == 'kokoro':
                        raise ValueError('Kokoro does not support native emotion ' + emotion + '. Select an explicit acoustic-effects expression or a compatible expressive engine.')
                    else:
                        if engine_options.get('task') == 'EMOTION':
                            engine_options['emotion'] = (expression or {}).get('base_emotion') or emotion
                        elif engine_options.get('task') == 'FREEFORM' and expression and expression.get('instructions'):
                            engine_options['instructions'] = expression['instructions']
                        else:
                            engine_options['emotion'] = emotion
                synthesis_text = segment.get('text', '')
                if segment['kind'] == 'event':
                    from .speech_engines import EVENTS
                    known = {name.lower(): name for name in EVENTS}
                    event = known.get(segment['event'].lower())
                    if not event or engine_options.get('engine') != 'ming' or engine_options.get('task') != 'TTS':
                        raise ValueError('This vocal event requires a documented Ming TTS tag in reference-based TTS mode.')
                    synthesis_text = '[' + event + ']'
                progress(0, f'Chapter {chapter_index + 1}/{len(chapters)} · segment {index + 1}/{len(segments)}', stage='Speech synthesis', progress_measured=False)
                cached, receipt = _cache_segment(synthesis_text, engine_options, data, settings, progress, cache_only=options.get('assemble_only', False))
                raw_paths.append(cached)
                voice_chain = selected.get('voice_effects') or []
                extra = selected.get('segment_effects') or []
                segment_chain = extra.get(segment.get('id'), extra.get(segment.get('parent_id'), [])) if isinstance(extra, dict) else extra
                ranged = []
                for assignment in project.get('segment_processing', options.get('segment_processing', [])):
                    if assignment.get('chapter_id') != chapter.get('id'):
                        continue
                    start, end = assignment.get('start'), assignment.get('end')
                    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(chapter.get('text', '')):
                        raise ValueError('Saved selection effects have invalid manuscript offsets.')
                    current_hash=hashlib.sha256(chapter.get('text','')[start:end].encode('utf-8')).hexdigest()
                    if assignment.get('stale') or (assignment.get('source_sha256') and assignment['source_sha256']!=current_hash):
                        raise ValueError('Saved selection text changed. Review and reapply its effects before rendering.')
                    seg_start, seg_end = segment.get('start', 0), segment.get('end', 0)
                    if min(end, seg_end) > max(start, seg_start):
                        if not (start <= seg_start and end >= seg_end):
                            raise ValueError('Selection effects must cover whole spoken segments. Select the complete sentence or paragraph.')
                        ranged.extend(assignment.get('chain') or [])
                segment_chain = [*segment_chain, *ranged]
                attributes = []
                if selected.get('pitch'):
                    attributes.append({'type': 'pitch', 'params': {'semitones': selected['pitch']}})
                if selected.get('volume'):
                    attributes.append({'type': 'gain', 'params': {'db': selected['volume']}})
                combined = [*voice_chain, *attributes, *expression_chain, *segment_chain]
                if combined:
                    changed = work / f'c{chapter_index}-s{index}-fx.wav'
                    _effects(cached, changed, combined, settings)
                    _canonical(changed, part, settings)
                else:
                    shutil.copyfile(cached, part)
                paths.append(part)
                records.append({'chapter_id': chapter.get('id', str(chapter_index)), 'segment_id': segment.get('id', str(index)), 'cache_key': receipt['key'], 'reused': receipt['reused'], 'inference': receipt.get('inference', {}), 'source_sha256': receipt['sha256'], 'voice_effects': voice_chain, 'attribute_effects': attributes, 'expression_effects': expression_chain, 'segment_effects': segment_chain})
            if not paths:
                raise ValueError('A selected chapter contains no spoken text or pauses.')
            rendered = work / f'chapter-{chapter_index}.wav'
            length = _join(paths, rendered)
            chapter_audio.append(rendered)
            raw_chapter = work / f'chapter-original-{chapter_index}.wav'
            _join(raw_paths, raw_chapter)
            original_audio.append(raw_chapter)
            chapter_timing.append({'id': chapter.get('id'), 'title': chapter.get('title') or 'Chapter ' + str(chapter_index + 1), 'start': position, 'end': position + length})
            position += length
        combined = work / 'combined.wav'
        _join(chapter_audio, combined)
        mastered = work / 'mastered.wav'
        master_chain = base.get('master_effects') or []
        _effects(combined, mastered, master_chain, settings)
        files = []
        if options.get('separate_chapters'):
            # Master each chapter with the same saved parameters exactly once.
            for index, (source, chapter) in enumerate(zip(chapter_audio, chapter_timing)):
                chapter_master = work / f'chapter-master-{index}.wav'
                _effects(source, chapter_master, master_chain, settings)
                target = stage_dir / f'{index + 1:03d}_{safe_name(chapter["title"])}.{fmt}'
                _export(chapter_master, target, fmt, settings, {**metadata, 'title': chapter['title']}, cover=options.get('cover'))
                files.append(target)
        if not options.get('separate_chapters') or options.get('combined', True):
            output = stage_dir / (name + '.' + fmt)
            # Duration-changing master processing invalidates original chapter offsets.
            if master_chain and fmt == 'm4b':
                from .worker import probe, duration
                changed_duration = duration(probe(mastered, settings))
                if abs(changed_duration - position) > .05:
                    raise ValueError('Chapterized M4B cannot use a duration-changing master chain. Apply tempo changes per segment or export without chapters.')
            _export(mastered, output, fmt, settings, metadata, chapter_timing, options.get('cover'))
            files.append(output)
        if master_chain or any(record.get(key) for record in records for key in ('voice_effects', 'attribute_effects', 'expression_effects', 'segment_effects')):
            original = stage_dir / (name + '_original.wav')
            _join(original_audio, original)
            files.append(original)
        report = stage_dir / (name + '_speech-render.json')
        json_write(report, {'schema': 1, 'project_id': project.get('id'), 'project_revision': project.get('revision'), 'chapters': chapter_timing, 'segments': records, 'master_effects': master_chain, 'timing_scope': 'Generated segment boundaries; not word alignment', 'warnings': sorted(set(warnings)), 'original_text_preserved': True})
        files.append(report)
    return files, sorted(set(warnings))
