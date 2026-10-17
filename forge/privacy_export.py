"""Non-destructive, format-aware privacy exports using FORGE's media worker.

Inspection and the processing plan are available without creating any output.
Every completed export is inspected in memory, even when private reports are off.
"""
from __future__ import annotations

import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .core import CLEAN_MEDIA, IMAGES, MEDIA, safe_name


class PrivacyExportOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preset: Literal['standard', 'advanced', 'custom'] = 'standard'
    metadata: Literal['remove', 'retain', 'selective'] = 'remove'
    remove_tags: list[str] = Field(default_factory=list, max_length=64)
    retain_subtitles: bool = True
    retain_artwork: bool = True
    retain_attachments: bool = True
    retain_chapters: bool = True
    retain_data: bool = False
    reencode: bool = False
    format: Literal['same', 'mkv', 'mp4', 'flac', 'wav'] = 'same'
    quality: int = Field(20, ge=12, le=35)
    reports: bool = False
    report_formats: list[Literal['json', 'html']] = Field(default_factory=lambda: ['json', 'html'], min_length=1, max_length=2)
    compare_audio: bool = False
    compare_video: bool = False

    @field_validator('remove_tags')
    @classmethod
    def tags(cls, values):
        if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) for value in values):
            raise ValueError('Metadata keys must contain 1–128 letters, digits, underscores, dots, colons or hyphens.')
        return list(dict.fromkeys(value.casefold() for value in values))

    @field_validator('report_formats')
    @classmethod
    def formats(cls, values):
        return list(dict.fromkeys(values))

    @model_validator(mode='after')
    def valid_selection(self):
        if self.preset == 'custom' and self.metadata == 'selective' and not self.remove_tags:
            raise ValueError('Select at least one metadata key to remove.')
        return self


def effective_options(options: dict | PrivacyExportOptions) -> dict:
    value = options if isinstance(options, PrivacyExportOptions) else PrivacyExportOptions.model_validate(options)
    result = value.model_dump()
    if value.preset != 'custom':
        result['metadata'] = 'remove'
        result['remove_tags'] = []
        for key in ('retain_subtitles', 'retain_artwork', 'retain_attachments', 'retain_chapters'):
            result[key] = value.preset == 'standard'
        result['retain_data'] = False
    return result


def output_suffix(source: Path | str, options: dict) -> str:
    values = effective_options(options)
    return Path(source).suffix.lower() if values['format'] == 'same' else '.' + values['format']


def _kind(stream: dict) -> str:
    if stream.get('disposition', {}).get('attached_pic'):
        return 'artwork'
    return stream.get('codec_type', 'unknown')


def _selected(info: dict, options: dict) -> tuple[list[dict], list[dict]]:
    enabled = {'audio': True, 'video': True, 'subtitle': options['retain_subtitles'],
               'artwork': options['retain_artwork'], 'attachment': options['retain_attachments'],
               'data': options['retain_data']}
    keep, remove = [], []
    for stream in info.get('streams', []):
        (keep if enabled.get(_kind(stream), False) else remove).append(stream)
    if not any(_kind(stream) in {'audio', 'video'} for stream in keep):
        raise ValueError('No supported audio or video stream is available for this export.')
    return keep, remove


def _stream_summary(stream: dict) -> dict:
    return {'index': stream.get('index'), 'type': _kind(stream), 'codec': stream.get('codec_name')}


def _media_plan(source: Path, options: dict, settings: dict) -> tuple[dict, dict, list[dict]]:
    from .worker import probe
    info = probe(source, settings)
    keep, remove = _selected(info, options)
    extension = output_suffix(source, options)
    warnings = ['Container processing can change or invalidate signed provenance. Inspect the exported copy before sharing.',
                'Visible or audible identifiers, filenames, and recognizable content remain. Metadata inspection cannot prove absence of hidden data.']
    if options['reencode'] and extension not in {'.mkv', '.mp4', '.flac', '.wav'}:
        raise ValueError('Re-encoding is supported for MKV, MP4, FLAC and WAV. Select one of these output formats.')
    if extension in {'.flac', '.wav'}:
        audio = [stream for stream in keep if _kind(stream) == 'audio']
        allowed = {'audio', 'artwork'} if extension == '.flac' else {'audio'}
        if len(audio) != 1 or any(_kind(stream) not in allowed for stream in keep):
            raise ValueError('FLAC/WAV export requires exactly one audio stream; FLAC also permits cover artwork. Choose MKV to retain other streams.')
    if extension == '.mp4' and any(_kind(stream) in {'attachment', 'data'} for stream in keep):
        raise ValueError('MP4 cannot safely retain these attachments/data streams. Choose MKV or Custom and remove them.')
    if extension != source.suffix.lower():
        warnings.append('The output container changes. Retained streams must be compatible; incompatible combinations fail without publishing a partial export.')
    if options['metadata'] == 'remove':
        warnings.append('Descriptive container, stream and chapter tags are removed where supported; language/title labels may be lost and technical tags may be regenerated.')
    if any(_kind(stream) == 'attachment' for stream in keep) and options['metadata'] == 'remove':
        warnings.append('Attachment content is retained, with neutral filenames and required media-type labels. It may contain identifying data.')
    if options['retain_chapters'] and info.get('chapters'):
        warnings.append('Chapter timing is retained; chapter labels follow the metadata selection.')
    if remove:
        warnings.append('Excluded streams: ' + ', '.join(f'{_kind(stream)} #{stream.get("index")}' for stream in remove) + '.')
    if not options['retain_chapters'] and info.get('chapters'):
        warnings.append(f'{len(info["chapters"])} chapter(s) will be removed.')
    if options['reencode']:
        warnings.append('Audio/video streams will be encoded again using CPU encoders. H.264/AAC are lossy; FLAC/PCM preserve decoded samples but do not restore prior quality.')
        if extension == '.mp4' and any(_kind(stream) == 'subtitle' for stream in keep):
            warnings.append('Retained text subtitles are converted to MP4 timed text; formatting can change. Unsupported subtitle codecs fail.')
    operations = [{'operation': 'metadata', 'selection': options['metadata'], 'keys': options['remove_tags']},
                  {'operation': 'stream_selection', 'retained': [_stream_summary(s) for s in keep], 'removed': [_stream_summary(s) for s in remove]},
                  {'operation': 'chapters', 'selection': 'retain' if options['retain_chapters'] else 'remove'},
                  {'operation': 'encoding', 'selection': 'reencode' if options['reencode'] else 'stream_copy'},
                  {'operation': 'container', 'input': source.suffix.lower(), 'output': extension}]
    plan = {'schema_version': 1, 'tool': 'privacy_export', 'preset': options['preset'], 'filename': source.name,
            'output_suffix': extension, 'encoding': 'reencode' if options['reencode'] else 'stream_copy',
            'retained_streams': [_stream_summary(s) for s in keep], 'removed_streams': [_stream_summary(s) for s in remove],
            'operations': operations, 'warnings': warnings, 'reports': options['reports'],
            'verification': 'Actual exported streams, codecs and detectable metadata are inspected locally.'}
    return plan, info, keep


def publicplan(source: Path | str, options: dict, settings: dict) -> dict:
    source = Path(source)
    values = effective_options(options)
    if source.suffix.lower() not in MEDIA | IMAGES:
        raise ValueError('Privacy export supports registered audio, video, JPEG, PNG and WebP media.')
    if source.suffix.lower() not in IMAGES:
        return _media_plan(source, values, settings)[0]
    from .media_extensions import image_inspection
    inspected = image_inspection(source)
    if inspected.get('animated'):
        raise ValueError('Animated image privacy export is not supported. The original is preserved.')
    if values['format'] != 'same' or values['reencode']:
        raise ValueError('Image privacy export retains the existing format; media re-encoding controls apply to audio/video.')
    if values['metadata'] == 'selective':
        raise ValueError('Selective image-tag editing is not supported. Choose full metadata removal or retention.')
    warnings = list(inspected['capabilities']['limits'])
    warnings.append('EXIF orientation is applied to pixels when necessary; this can re-encode an image and affect JPEG quality.')
    warnings.append('Metadata processing can remove or invalidate provenance records; inspect the exported copy.')
    if values['preset'] == 'advanced':
        warnings.append('Advanced Privacy also removes supported ICC/color-profile metadata; color appearance can change.')
    else:
        warnings.append('Standard/Custom metadata reduction retains supported ICC/color-rendering fields.')
    return {'schema_version': 1, 'tool': 'privacy_export', 'preset': values['preset'], 'filename': source.name,
            'output_suffix': source.suffix.lower(), 'encoding': 'image_container_processing', 'retained_streams': [], 'removed_streams': [],
            'operations': [{'operation': 'image_metadata', 'selection': values['metadata'],
                            'level': 'maximum' if values['preset'] == 'advanced' else 'standard'}],
            'warnings': warnings, 'reports': values['reports'], 'verification': 'Exported image readability, dimensions and supported metadata are inspected locally.'}


def _removal_keys(options: dict) -> list[str]:
    values = list(options['remove_tags'])
    if 'location' in values:
        values += ['location-eng', 'com.apple.quicktime.location.ISO6709']
    return list(dict.fromkeys(values))


def _remux(source: Path, output: Path, options: dict, settings: dict, info: dict, keep: list[dict]):
    from .worker import duration, probe, run_ffmpeg
    args = ['-i', str(source)]
    for stream in keep:
        args += ['-map', f'0:{int(stream["index"])}']
    args += ['-c', 'copy', '-map_metadata', '-1' if options['metadata'] == 'remove' else '0',
             '-map_chapters', '0' if options['retain_chapters'] else '-1']
    if options['metadata'] == 'remove':
        args += ['-map_metadata:s', '-1']
        for key in ('title', 'comment', 'encoder'):
            args += ['-metadata', key + '=']
        if options['retain_chapters']:
            for index, chapter in enumerate(info.get('chapters', [])):
                for key in chapter.get('tags', {}):
                    args += [f'-metadata:c:{index}', key + '=']
        for index, stream in enumerate(keep):
            if _kind(stream) == 'attachment':
                args += [f'-metadata:s:{index}', f'filename=attachment-{index + 1}.bin',
                         f'-metadata:s:{index}', 'mimetype=' + stream.get('tags', {}).get('mimetype', 'application/octet-stream')]
        if output.suffix.lower() == '.mp3':
            args += ['-id3v2_version', '0', '-write_id3v1', '0']
    elif options['metadata'] == 'selective':
        for key in _removal_keys(options):
            args += ['-metadata', key + '=', '-metadata:s', key + '=']
            if options['retain_chapters']:
                for index, _ in enumerate(info.get('chapters', [])):
                    args += [f'-metadata:c:{index}', key + '=']
    expected = []
    for index, stream in enumerate(keep):
        kind, codec = _kind(stream), stream.get('codec_name')
        if options['reencode']:
            extension = output.suffix.lower()
            if kind == 'video':
                args += [f'-c:{index}', 'libx264', f'-crf:{index}', str(options['quality']), f'-preset:{index}', 'medium', f'-pix_fmt:{index}', 'yuv420p']
                codec = 'h264'
            elif kind == 'audio':
                codec = 'flac' if extension == '.flac' else 'pcm_s24le' if extension == '.wav' else 'aac'
                args += [f'-c:{index}', codec]
                if codec == 'aac':
                    args += [f'-b:{index}', '192k']
            elif kind == 'subtitle' and extension == '.mp4':
                args += [f'-c:{index}', 'mov_text']
                codec = 'mov_text'
        expected.append((stream.get('codec_type'), codec))
    run_ffmpeg(args + [str(output)], settings, duration(info))
    actual = probe(output, settings)
    actual_streams = [(s.get('codec_type'), s.get('codec_name')) for s in actual.get('streams', [])]
    if Counter(expected) != Counter(actual_streams):
        raise RuntimeError('Privacy export verification failed: the selected stream types or codecs were not preserved as requested.')
    if not options['retain_chapters'] and actual.get('chapters'):
        raise RuntimeError('Privacy export verification failed: excluded chapters remain.')


def process(source: Path | str, stage_dir: Path, options: dict, settings: dict, context: dict | None = None):
    from .privacy_verification import build_report, save_report, summary_warnings
    from .worker import wipe
    source, stage_dir = Path(source), Path(stage_dir)
    values = effective_options(options)
    plan = publicplan(source, values, settings)
    stage_dir.mkdir(parents=True, exist_ok=True)
    output = stage_dir / (safe_name(source.stem) + '_privacy' + plan['output_suffix'])
    if output.resolve() == source.resolve() or output.exists():
        raise ValueError('Privacy export requires a new staged output filename.')
    original_stat = source.stat()
    warnings = list(plan['warnings'])
    if source.suffix.lower() in IMAGES:
        if values['metadata'] == 'retain':
            shutil.copyfile(source, output)
        else:
            _, _, notes = wipe(source, output, settings, {'cleaning_level': 'maximum' if values['preset'] == 'advanced' else 'standard'})
            warnings += notes
    elif values['preset'] == 'advanced' and not values['reencode'] and values['format'] == 'same' and source.suffix.lower() in CLEAN_MEDIA:
        _, _, notes = wipe(source, output, settings, {'cleaning_level': 'maximum'})
        warnings += notes
    else:
        _, info, keep = _media_plan(source, values, settings)
        _remux(source, output, values, settings, info, keep)
    if (source.stat().st_size, source.stat().st_mtime_ns) != (original_stat.st_size, original_stat.st_mtime_ns):
        raise RuntimeError('The original changed during processing; this export cannot be verified against its starting state.')
    report = build_report(source, output, settings, options=values, operations=plan['operations'], warnings=warnings)
    warnings += report.get('errors', [])
    warnings += summary_warnings(report)
    files = [output]
    if values['reports']:
        files += save_report(report, stage_dir, output.stem + '_PRIVATE-verification', values['report_formats'])
        warnings.append('Private verification reports contain original metadata and local filenames. Review them before sharing.')
    else:
        warnings.append('Verification was performed in memory; private reports were not retained.')
    return files, list(dict.fromkeys(warnings))
