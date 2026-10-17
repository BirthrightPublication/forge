"""Shared, read-only media inspection and local JSON/HTML audit reports."""
from __future__ import annotations

import html
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from PIL import ExifTags, Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .core import IMAGES, MEDIA, executable, safe_name
from .media_extensions import image_inspection, media_inspection
from .provenance import inspect_provenance


class ProvenanceOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reports: bool = True
    report_formats: list[Literal['json', 'html']] = Field(default_factory=lambda: ['json', 'html'], max_length=2)

    @field_validator('report_formats')
    @classmethod
    def unique_formats(cls, value):
        return list(dict.fromkeys(value))

    @model_validator(mode='after')
    def needs_format(self):
        if self.reports and not self.report_formats:
            raise ValueError('Select JSON or HTML when reports are enabled.')
        return self


class TrackingAuditOptions(ProvenanceOptions):
    include_provenance: bool = True
    include_fingerprints: bool = False


CATEGORY_WORDS = {
    'gps': ('gps', 'latitude', 'longitude', 'location', 'geotag', 'geoposition'),
    'device': ('make', 'model', 'serial', 'lens', 'camera', 'device', 'ownername'),
    'software': ('software', 'encoder', 'handler_name', 'encoding_tool', 'writing_app', 'creator_tool'),
    'timestamps': ('date', 'time', 'year'),
    'author': ('artist', 'author', 'creator', 'copyright', 'composer', 'producer', 'owner'),
    'comments': ('comment', 'description', 'usercomment', 'imagedescription'),
}
TECHNICAL_IMAGE_FIELDS = {'jfif', 'jfif_version', 'jfif_unit', 'jfif_density', 'dpi', 'aspect', 'transparency', 'loop', 'duration'}
LIMITATIONS = [
    'Findings cover fields exposed by the named parser. Not detected is not proof that identifying data is absent.',
    'Filenames, visible or audible content and filesystem timestamps can identify media; they are not removed by inspection.',
    'This inspection does not detect arbitrary hidden data, unknown watermarks or every proprietary recognition method.',
    'No total-anonymity percentage is calculated.',
]


def _value(value):
    if isinstance(value, bytes):
        return f'[binary: {len(value)} bytes]'
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value[:8000] if isinstance(value, str) else value
    return str(value)[:8000]


def _entry(entries: list, key, value, location: str):
    if value is None or value == '' or len(entries) >= 4096:
        return
    entries.append({'key': str(key)[:200], 'value': _value(value), 'location': location})


def classify_metadata(entries: list[dict]) -> dict:
    categories = {key: [] for key in (*CATEGORY_WORDS, 'descriptive')}
    for entry in entries:
        normalized = re.sub('[^a-z0-9_]', '', entry['key'].lower())
        groups = [key for key, words in CATEGORY_WORDS.items() if any(word in normalized for word in words)]
        for key in groups or ['descriptive']:
            categories[key].append(entry)
    return categories


def _xmp_entries(value, entries: list[dict], location: str) -> bool:
    """Read bounded embedded XMP, rejecting declarations/entities outright."""
    if not value:
        return True
    raw = value if isinstance(value, bytes) else str(value).encode('utf-8')
    if len(raw) > 1024 * 1024 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        return False
    try:
        root = ET.fromstring(raw)
        for node in root.iter():
            local = str(node.tag).split('}')[-1]
            if node.text and node.text.strip():
                _entry(entries, local, node.text.strip(), location)
            for key, item in node.attrib.items():
                _entry(entries, key.split('}')[-1], item, location)
        return True
    except (ET.ParseError, ValueError):
        return False


def _bounded_probe(path: Path, settings: dict) -> dict:
    """The same FFprobe fields as worker.probe, with bounded output storage."""
    from .fingerprints import capture_local, INPUT_DEMUXERS
    binary = executable('ffprobe', settings)
    if not binary:
        raise FileNotFoundError('FFprobe is not installed.')
    data = capture_local(
        [binary, '-v', 'error', '-protocol_whitelist', 'file,pipe', '-format_whitelist', INPUT_DEMUXERS,
         '-show_format', '-show_streams', '-show_chapters', '-of', 'json', str(path)],
        max_bytes=16 * 1024 * 1024, timeout=120,
    )
    return json.loads(data)


def inspect_media(path: Path, settings: dict, *, include_provenance: bool = True) -> dict:
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('Choose an existing local media file.')
    report = {
        'schema_version': 1, 'filename': path.name, 'size_bytes': path.stat().st_size,
        'kind': 'unsupported', 'status': 'unsupported', 'media': {},
        'metadata': {'entries': [], 'categories': {}}, 'findings': [],
        'provenance': {'status': 'unsupported', 'message': 'Provenance inspection was not selected.'},
        'capabilities': {}, 'limitations': list(LIMITATIONS), 'errors': [],
    }
    entries = report['metadata']['entries']
    method, inspected = 'No supported parser selected', False
    suffix = path.suffix.lower()
    if suffix in IMAGES | {'.tif', '.tiff', '.gif', '.bmp'}:
        report['kind'], method = 'image', 'Pillow EXIF, image headers and supported embedded-preview headers'
        try:
            original = image_inspection(path)
            with Image.open(path) as image:
                xmp = image.info.get('xmp') or image.info.get('XML:com.adobe.xmp')
                if xmp and not _xmp_entries(xmp, entries, 'image.xmp'):
                    report['limitations'].append('Embedded XMP could not be fully parsed within the safe XML limits.')
            for key, value in original.get('info', {}).items():
                if key not in TECHNICAL_IMAGE_FIELDS and key != 'exif':
                    _entry(entries, key, value, 'image.info')
            for key, value in original.get('exif', {}).items():
                if isinstance(value, dict):
                    names = ExifTags.GPSTAGS if key == '34853' else ExifTags.TAGS
                    for subkey, subvalue in value.items():
                        _entry(entries, names.get(int(subkey), subkey), subvalue, 'exif.gps' if key == '34853' else 'exif.detail')
                else:
                    _entry(entries, ExifTags.TAGS.get(int(key), key), value, 'exif')
            report['media'] = {
                'format_name': original['format'], 'width': original['size'][0], 'height': original['size'][1],
                'animated': original['animated'], 'streams': [], 'chapters': [],
                'embedded_artwork': original['embedded_previews'], 'attachments': [], 'unusual_streams': [],
            }
            inspected = True
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            report['errors'].append('The image parser could not complete inspection; metadata absence is unknown.')
    elif suffix in MEDIA:
        report['kind'], method = 'media', 'FFprobe container, stream and chapter fields'
        try:
            info = _bounded_probe(path, settings)
            inspected = True
            detail = media_inspection(info)
            for key, value in detail['format_tags'].items():
                _entry(entries, key, value, 'container')
                if 'xmp' in key.lower() and not _xmp_entries(value, entries, 'container.xmp'):
                    report['limitations'].append('Embedded XMP could not be fully parsed within the safe XML limits.')
            for stream in detail['stream_tags']:
                for key, value in stream['tags'].items():
                    _entry(entries, key, value, 'stream:' + str(stream['index']))
            for chapter in detail['chapters']:
                for key, value in chapter.get('tags', {}).items():
                    _entry(entries, key, value, 'chapter:' + str(chapter.get('id', '')))
            selected = ('index', 'codec_type', 'codec_name', 'codec_long_name', 'profile', 'width', 'height',
                        'sample_rate', 'channels', 'channel_layout', 'duration', 'avg_frame_rate', 'r_frame_rate',
                        'sample_aspect_ratio', 'display_aspect_ratio', 'pix_fmt', 'bits_per_sample', 'bit_rate', 'tags', 'disposition')
            streams = [{key: stream[key] for key in selected if key in stream} for stream in info.get('streams', [])]
            report['media'] = {key: value for key, value in info.get('format', {}).items()
                               if key in {'format_name', 'format_long_name', 'duration', 'bit_rate', 'nb_streams'}}
            report['media'].update({
                'streams': streams, 'chapters': detail['chapters'],
                'embedded_artwork': [s for s in streams if s.get('disposition', {}).get('attached_pic')],
                'attachments': [s for s in streams if s.get('codec_type') == 'attachment'],
                'unusual_streams': [s for s in streams if s.get('codec_type') not in {'audio', 'video', 'subtitle', 'attachment'}],
            })
        except FileNotFoundError:
            report['errors'].append('FFprobe is not installed; container and stream inspection is unsupported.')
            method = 'FFprobe unavailable'
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            report['errors'].append('FFprobe could not complete inspection; metadata absence is unknown.')
    categories = classify_metadata(entries)
    report['metadata']['categories'] = categories
    unavailable = report['kind'] == 'unsupported' or method == 'FFprobe unavailable'
    report['status'] = ('detected' if entries else 'not_detected') if inspected else ('unsupported' if unavailable else 'unknown')
    for category, evidence in categories.items():
        report['findings'].append({
            'category': category, 'status': ('detected' if evidence else 'not_detected') if inspected else report['status'],
            'method': method, 'evidence': evidence,
            'note': 'No matching field detected by this parser is not proof of absence.' if not evidence else 'These fields were exposed by the selected parser.',
        })
    for category, key in (('artwork', 'embedded_artwork'), ('attachments', 'attachments'), ('chapters', 'chapters'), ('unusual_streams', 'unusual_streams')):
        evidence = report['media'].get(key, [])
        report['findings'].append({'category': category, 'status': ('detected' if evidence else 'not_detected') if inspected else report['status'],
                                   'method': method, 'evidence': evidence, 'note': 'Parser-exposed structures only.'})
    report['capabilities'] = {'metadata': {'method': method, 'supported': inspected},
                              'hidden_data': {'status': 'unsupported'}, 'recognition_service_prediction': {'status': 'unsupported'}}
    if include_provenance:
        report['provenance'] = inspect_provenance(path, settings)
    return report


def render_report_html(report: dict) -> str:
    """Render untrusted report values as text with no active/remote content."""
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    title = html.escape(str(report.get('title', 'FORGE media inspection report')))
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            '<title>' + title + '</title><style>body{background:#191919;color:#eee;font:16px system-ui;margin:2rem;max-width:90rem}h1{color:#d4b26b;font-size:1.4rem}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#242424;padding:1rem;border-radius:.5rem}p{color:#ccc}</style>'
            '<h1>' + title + '</h1><p>Local evidence report. “Not detected” is not proof of absence. Review inspection methods and limitations.</p>'
            '<pre>' + html.escape(serialized) + '</pre></html>')


def write_reports(report: dict, stage_dir: Path, stem: str, formats: list[str]) -> list[Path]:
    if any(fmt not in {'json', 'html'} for fmt in formats):
        raise ValueError('Reports support JSON and HTML only.')
    stage_dir = Path(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    name = safe_name(stem)
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    files = []
    for fmt in dict.fromkeys(formats):
        path = stage_dir / (name + '.' + fmt)
        if fmt == 'json':
            content = serialized + '\n'
        else:
            content = render_report_html(report)
        # Stage files never overwrite an existing report; final collision handling
        # remains the shared worker publisher's responsibility.
        with path.open('x', encoding='utf-8') as handle:
            handle.write(content)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        files.append(path)
    return files


def process_audit(source: Path, stage_dir: Path, options: dict, settings: dict, context: dict | None = None):
    config = TrackingAuditOptions.model_validate(options)
    report = inspect_media(source, settings, include_provenance=config.include_provenance)
    report['title'] = 'FORGE Tracking Audit'
    warnings = list(report['errors'])
    if config.include_fingerprints:
        from .fingerprints import analyze_audio, analyze_video, public_analysis
        fingerprints = {}
        stream_types = {s.get('codec_type') for s in report['media'].get('streams', [])}
        for kind, analyze in (('audio', analyze_audio), ('video', analyze_video)):
            if kind in stream_types:
                try:
                    fingerprints[kind] = public_analysis(analyze(Path(source), settings))
                except (RuntimeError, ValueError, OSError):
                    fingerprints[kind] = {'status': 'unknown', 'message': 'The requested fingerprint analysis did not complete.'}
                    warnings.append(kind.title() + ' fingerprint inspection did not complete.')
        report['fingerprints'] = fingerprints or {'status': 'unsupported', 'message': 'No supported audiovisual stream was detected.'}
    if config.include_provenance and report['provenance']['status'] in {'unsupported', 'unknown'}:
        warnings.append(report['provenance']['message'])
    files = write_reports(report, stage_dir, safe_name(Path(source).stem) + '_tracking-audit', config.report_formats) if config.reports else []
    return files, warnings


def process_provenance(source: Path, stage_dir: Path, options: dict, settings: dict, context: dict | None = None):
    config = ProvenanceOptions.model_validate(options)
    result = inspect_provenance(source, settings)
    report = {'schema_version': 1, 'title': 'FORGE Content Credentials Inspection', 'filename': Path(source).name, 'provenance': result}
    files = write_reports(report, stage_dir, safe_name(Path(source).stem) + '_provenance', config.report_formats) if config.reports else []
    warnings = [result['message']] if result['status'] in {'unsupported', 'unknown'} else []
    return files, warnings
