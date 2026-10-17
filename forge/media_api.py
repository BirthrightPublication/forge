"""Local Media Privacy API, sharing the Studio's auth, output and job boundaries."""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import Field, ValidationError
from starlette.background import BackgroundTask

from .core import IMAGES, MEDIA, PRIVACY_TOOLS, TOOLS, absolute_path, executable
from .entitlements import require_feature
from .models import OPTION_MODELS, Strict


class PlanRequest(Strict):
    tool: Literal['watermark', 'audio_fingerprint', 'video_fingerprint', 'tracking_audit',
                  'privacy_export', 'provenance', 'privacy_verify']
    source: str = Field(min_length=1, max_length=80)
    options: dict = Field(default_factory=dict)


class PreviewRequest(PlanRequest):
    tool: Literal['watermark'] = 'watermark'
    still: bool = False


def snapshot(path: Path) -> dict:
    stat = path.stat()
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def checked_options(tool: str, options: dict) -> dict:
    try:
        return OPTION_MODELS[tool].model_validate(options).model_dump()
    except ValidationError as error:
        detail = '; '.join('.'.join(map(str, entry['loc'])) + ': ' + entry['msg']
                           for entry in error.errors(include_input=False, include_context=False))
        raise ValueError('Invalid media settings: ' + detail) from None


def validate_media_options(tool: str, source: Path, options: dict, settings: dict, *,
                           input_suffix: str | None = None, inspect_source: bool = True) -> dict:
    """Cheap submission checks and truthful transformation disclosures.

    Black Scripting uses this same boundary before binding its execution preview.
    Parsing media is done by the selected tool; filenames alone never prove validity.
    """
    if tool not in PRIVACY_TOOLS:
        return {'output_suffix': source.suffix.lower(), 'disclosures': [], 'asset_snapshots': []}
    source = absolute_path(str(source))
    suffix = input_suffix or source.suffix.lower()
    if suffix not in TOOLS[tool]['accept']:
        raise ValueError('This format is not supported by ' + TOOLS[tool]['title'] + '.')
    plan = {'tool': tool, 'source_name': source.name, 'output_suffix': suffix,
            'disclosures': [], 'asset_snapshots': [], 'preserves_original': True}
    if tool == 'watermark':
        from .watermark import publicplan, validate_assets
        if inspect_source:
            plan.update(publicplan(source, options, settings))
            plan['disclosures'] = [
                'Video is re-encoded as 8-bit H.264.' if plan.get('video_reencoded') else 'Video is copied where present.',
                'Audio is re-encoded.' if plan.get('audio_reencoded') else 'Audio is copied where present.',
                'Only primary audio/video tracks are retained; subtitles, attachments and chapters are discarded.',
                str(plan.get('privacy_note') or 'Descriptive metadata and embedded provenance can be lost.'),
            ]
        else:
            plan['disclosures'] = ['Watermarking encodes a new media copy. The actual preceding output is inspected at execution.',
                                   'Only primary audio/video tracks are retained; subtitles, attachments, chapters and provenance can be lost.']
        plan['output_suffix'] = '.' + options['format']
        plan['asset_snapshots'] = [{'path': str(path), 'source_stat': snapshot(path)}
                                   for path in validate_assets(options)]
    elif tool == 'privacy_export':
        from .privacy_export import publicplan
        if inspect_source:
            plan.update(publicplan(source, options, settings))
        else:
            plan['output_suffix'] = suffix if options.get('format', 'same') == 'same' else '.' + options['format']
            plan['disclosures'] = ['The actual preceding output is inspected before privacy export.',
                                   'Advanced processing can discard subtitles, artwork, attachments, chapters and provenance.',
                                   'Explicit re-encoding can alter quality.' if options.get('reencode') else 'Compatible streams are copied without re-encoding.']
    else:
        plan['disclosures'] = ['Inspection reads the original without changing its media.',
                               'Findings describe selected local methods, not total anonymity.']
    plan['disclosures'] = list(dict.fromkeys(list(plan.get('disclosures', [])) + list(plan.get('warnings', []))))
    saves = options.get('reports', options.get('report_json', False) or options.get('report_html', False))
    if saves:
        plan['disclosures'] = list(plan.get('disclosures', [])) + [
            'Private reports can contain filenames, paths, metadata and fingerprints. Save and share them deliberately.']
    if options.get('retain_reference'):
        plan['disclosures'].append('A local reusable fingerprint reference will be explicitly exported.')
    return plan


def registered_sources(store, ids: list[str], *, references: bool = False) -> list[dict]:
    if len(ids) > 20:
        raise ValueError('Analyze at most 20 media files or references in one comparison.')
    values = []
    for source_id in dict.fromkeys(ids):
        source = store.source(source_id)
        path = absolute_path(source['path'])
        if references and (path.suffix.lower() != '.json' or path.stat().st_size > 2_000_000):
            raise ValueError('Fingerprint references must be local JSON files no larger than 2 MB.')
        values.append({**source, 'source_stat': snapshot(path)})
    return values


def media_capabilities(settings: dict) -> dict:
    try:
        version = importlib.metadata.version('c2pa-python')
    except importlib.metadata.PackageNotFoundError:
        version = None
    from .provenance import capabilities as provenance_capabilities
    from .fingerprints import capabilities as fingerprint_capabilities
    return {'ffmpeg': bool(executable('ffmpeg', settings)), 'ffprobe': bool(executable('ffprobe', settings)),
            'fingerprints': fingerprint_capabilities(settings),
            'provenance': {'available': importlib.util.find_spec('c2pa') is not None,
                           'package': 'c2pa-python', 'version': version,
                           **provenance_capabilities(),
                           'installer': 'scripts/Install Media Privacy.bat',
                           'note': 'Local inspection only. Missing/unsupported credentials do not prove absence of tracking.'},
            'processing': 'Local; no media, fingerprints or metadata are uploaded.'}


def mount(app, store, manager):
    preview_slots = threading.BoundedSemaphore(2)

    @app.get('/api/media/privacy/catalog')
    def catalog():
        return {'tools': {key: {'defaults': OPTION_MODELS[key]().model_dump(),
                                'schema': OPTION_MODELS[key].model_json_schema(), **TOOLS[key]}
                          for key in sorted(PRIVACY_TOOLS)},
                'capabilities': media_capabilities(store.settings())}

    @app.post('/api/media/privacy/plan')
    def plan(body: PlanRequest):
        require_feature(app, body.tool)
        source = absolute_path(store.source(body.source)['path'])
        return validate_media_options(body.tool, source, checked_options(body.tool, body.options), store.settings())

    @app.post('/api/media/privacy/preview')
    def preview(body: PreviewRequest):
        require_feature(app, 'watermark')
        if not preview_slots.acquire(blocking=False):
            raise HTTPException(429, 'Two media previews are already rendering. Wait for one to finish.')
        folder = None
        try:
            from .watermark import preview as encode_preview, preview_still
            source = absolute_path(store.source(body.source)['path'])
            options = checked_options(body.tool, body.options)
            validate_media_options(body.tool, source, options, store.settings())
            folder = Path(tempfile.mkdtemp(prefix='preview-v05-', dir=store.data / 'work'))
            output = (preview_still if body.still else encode_preview)(source, folder, options, store.settings())
            if not output.is_file() or output.resolve().parent != folder.resolve():
                raise ValueError('The preview did not produce an expected local output.')
            return FileResponse(output, filename='FORGE-encoded-preview' + output.suffix,
                                content_disposition_type='inline',
                                headers={'X-Forge-Preview': 'encoded-excerpt', 'Cache-Control': 'no-store'},
                                background=BackgroundTask(shutil.rmtree, folder, ignore_errors=True))
        except BaseException:
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)
            raise
        finally:
            preview_slots.release()

    @app.get('/api/media/privacy/sources/{id}/content')
    def content(id: str):
        source = store.source(id)
        path = absolute_path(source['path'])
        if path.suffix.lower() not in MEDIA | IMAGES:
            raise HTTPException(400, 'This registered file is not supported by the media preview.')
        return FileResponse(path, content_disposition_type='inline', headers={'Cache-Control': 'no-store'})

    @app.get('/api/media/privacy/jobs/{id}/report')
    def report(id: str):
        job = store.job(id)
        if job['tool'] not in PRIVACY_TOOLS:
            raise HTTPException(400, 'This is not a Media Privacy job.')
        require_feature(app, job['tool'])
        reports = []
        destination = Path(job['payload']['destination']).resolve()
        for output in job['outputs']:
            path = Path(output['path']).resolve()
            if path.suffix.lower() != '.json' or not path.is_file():
                continue
            try:
                path.relative_to(destination)
            except ValueError:
                continue
            if path.stat().st_size > 32_000_000:
                continue
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if isinstance(value, dict):
                reports.append({'name': path.name, 'report': value})
        return {'job_id': id, 'status': job['status'], 'reports': reports,
                'note': 'No private report was retained.' if not reports else 'Private local analysis. Review before sharing.'}
