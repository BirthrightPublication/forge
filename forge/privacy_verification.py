"""Evidence-based local comparisons of original and processed media."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .core import safe_name


class VerificationOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reports: bool = False
    report_formats: list[Literal['json', 'html']] = Field(default_factory=lambda: ['json', 'html'], min_length=1, max_length=2)
    compare_audio: bool = False
    compare_video: bool = False

    @field_validator('report_formats')
    @classmethod
    def formats(cls, values):
        return list(dict.fromkeys(values))


def _path(value) -> Path:
    return Path(value['path'] if isinstance(value, dict) else value)


def _format(report: dict) -> str:
    return str(report.get('media', {}).get('format_name') or report.get('media', {}).get('format') or report.get('kind', 'unknown'))


def _entries(report: dict) -> list[dict]:
    return report.get('metadata', {}).get('entries', [])


def _entry_id(entry: dict) -> tuple:
    # Identical metadata values surviving at another stream index remain detected.
    return str(entry.get('key', '')).casefold(), json.dumps(entry.get('value'), sort_keys=True, ensure_ascii=False)


def _metadata_changes(before: dict, after: dict) -> dict:
    original, remaining = _entries(before), _entries(after)
    old = {_entry_id(entry) for entry in original}
    new = {_entry_id(entry) for entry in remaining}
    supported = all(report.get('status') not in {'unsupported', 'unknown'} for report in (before, after))
    removed = [entry for entry in original if _entry_id(entry) not in new]
    retained = [entry for entry in remaining if _entry_id(entry) in old]
    generated = [entry for entry in remaining if _entry_id(entry) not in old]
    return {'status': 'unknown' if not supported else 'partially_processed' if removed and retained else 'removed_and_verified' if removed else 'detected' if remaining else 'not_detected',
            'removed_from_selected_inspection': removed, 'remaining': remaining, 'retained': retained, 'new_or_changed': generated,
            'method': 'Comparison of supported parser metadata fields; not detected is not proof of absence.'}


def _stream_changes(before: dict, after: dict) -> dict:
    def brief(stream):
        keys = ('index', 'codec_type', 'codec_name', 'width', 'height', 'sample_rate', 'channels', 'channel_layout', 'avg_frame_rate', 'r_frame_rate', 'pix_fmt')
        return {key: stream[key] for key in keys if key in stream}
    first = [brief(stream) for stream in before.get('media', {}).get('streams', [])]
    second = [brief(stream) for stream in after.get('media', {}).get('streams', [])]
    original_codecs = Counter((s.get('codec_type'), s.get('codec_name')) for s in first)
    output_codecs = Counter((s.get('codec_type'), s.get('codec_name')) for s in second)
    return {'before': first, 'after': second, 'codecs_changed': original_codecs != output_codecs,
            'before_chapters': len(before.get('media', {}).get('chapters', [])),
            'after_chapters': len(after.get('media', {}).get('chapters', [])),
            'before_dimensions': {key: before.get('media', {}).get(key) for key in ('width', 'height') if key in before.get('media', {})},
            'after_dimensions': {key: after.get('media', {}).get(key) for key in ('width', 'height') if key in after.get('media', {})},
            'note': 'Header/codec comparison does not measure every quality change or certify perceptual equivalence.'}


def build_report(original: Path | str, output: Path | str, settings: dict, *, options: dict | None = None,
                 operations: list | None = None, warnings: list | None = None) -> dict:
    from .media_inspection import inspect_media
    original, output = Path(original), Path(output)
    options = options or {}
    before = inspect_media(original, settings, include_provenance=True)
    after = inspect_media(output, settings, include_provenance=True)
    errors = list(before.get('errors', [])) + list(after.get('errors', []))
    fingerprints = {}
    for kind in ('audio', 'video'):
        if not options.get('compare_' + kind):
            fingerprints[kind] = {'status': 'not_requested'}
            continue
        try:
            from . import fingerprints as methods
            analyze = getattr(methods, 'analyze_' + kind)
            compare = getattr(methods, 'compare_' + kind)
            fingerprints[kind] = {'status': 'compared', 'comparison': compare(analyze(original, settings), analyze(output, settings))}
        except ValueError as exc:
            fingerprints[kind] = {'status': 'unsupported', 'reason': str(exc)}
        except (RuntimeError, OSError) as exc:
            fingerprints[kind] = {'status': 'unknown', 'reason': str(exc)}
            errors.append(f'{kind.title()} comparison could not be completed: {exc}')
    unsupported = [{'file': label, 'category': finding.get('category'), 'status': finding.get('status'), 'note': finding.get('note', '')}
                   for label, report in (('original', before), ('output', after))
                   for finding in report.get('findings', []) if finding.get('status') in {'unsupported', 'unknown'}]
    for label, report in (('original', before), ('output', after)):
        provenance = report.get('provenance', {})
        if provenance.get('status') in {'unsupported', 'unknown'}:
            unsupported.append({'file': label, 'category': 'provenance', 'status': provenance['status'], 'note': provenance.get('message', provenance.get('note', provenance.get('reason', '')))})
    limitations = list(dict.fromkeys((warnings or []) + before.get('limitations', []) + after.get('limitations', []) + [
        'This report records supported local inspections of these files. A missing field or credential is not proof of complete removal or untraceability.',
        'Acoustic and perceptual comparisons describe the selected local method; they do not predict all proprietary recognition systems.',
        'Report contents include private original metadata, filenames and the final local output location.']))
    return {'schema_version': 1, 'report_type': 'privacy_verification', 'title': 'FORGE Private Verification Report',
            'original': {'filename': original.name, 'format': _format(before), 'size_bytes': original.stat().st_size},
            'output': {'filename': output.name, 'format': _format(after), 'size_bytes': output.stat().st_size},
            'operations': operations or [{'operation': 'comparison', 'selection': 'Actual file inspection; processing history was not supplied.'}],
            'before': before, 'after': after, 'metadata_changes': _metadata_changes(before, after),
            'remaining_detectable_metadata': _entries(after), 'stream_changes': _stream_changes(before, after),
            'fingerprints': fingerprints, 'processing_errors': [], 'errors': errors,
            'unsupported_inspections': unsupported, 'quality_and_compatibility': [
                'Review stream/codec differences and the actual exported content. Equal codec names alone do not establish unchanged quality.',
                'Signed provenance can change or become invalid after editing; validity and trust are separate from metadata detection.'],
            'final_output_location': str(output.resolve()), 'limitations': limitations}


def save_report(report: dict, stage_dir: Path, stem: str, formats: list[str]) -> list[Path]:
    from .core import json_write
    from .media_inspection import write_reports
    stage_dir, stem = Path(stage_dir), safe_name(stem)
    files = write_reports(report, stage_dir, stem, formats)
    if 'html' in formats and 'json' not in formats:
        json_write(stage_dir / ('.' + stem + '.forge-private.json'), report)
    return files


def summary_warnings(report: dict) -> list[str]:
    metadata = report['metadata_changes']
    if metadata['status'] in {'unknown', 'unsupported'}:
        messages = ['Verification is incomplete: one or both metadata inspections were unavailable. No removal conclusion was established.']
    else:
        messages = [f'Verified parser-visible metadata: {len(metadata["removed_from_selected_inspection"])} original field(s) no longer detected; {len(metadata["remaining"])} field(s) remain detectable.',
                    'Verified stream/codec inventory: ' + ('changed.' if report['stream_changes']['codecs_changed'] else 'same stream types and codec counts.')]
    for kind, result in report['fingerprints'].items():
        if result['status'] == 'compared':
            comparison = result['comparison']
            similarity = comparison.get('similarity')
            measurement = f'{similarity:.3f} similarity' if isinstance(similarity, (int, float)) else 'no reliable similarity measurement'
            messages.append(f'{kind.title()} comparison: {comparison.get("classification", "measured")} ({measurement} by the selected local method).')
        elif result['status'] != 'not_requested':
            messages.append(f'{kind.title()} comparison {result["status"]}: {result.get("reason", "")}.')
    if report['unsupported_inspections']:
        messages.append('Some inspections are unsupported or unknown; no claim of complete metadata or tracking removal is made.')
    return messages


def finalize_reports(files: list[Path], published_paths: dict[str, str]) -> None:
    """Rewrite staged media references before report publication, including collisions.

    Worker publishes media first, then calls this helper before publishing report
    files. HTML-only exports embed the report in a private companion cache which
    remains inside the worker staging directory and is never returned for export.
    """
    from .core import json_write
    from .media_inspection import render_report_html
    if not published_paths:
        return
    returned = [Path(file) for file in files]
    candidates = [path for path in returned if path.suffix == '.json']
    candidates += [path.parent / ('.' + path.stem + '.forge-private.json') for path in returned if path.suffix == '.html']
    for path in candidates:
        if path.suffix != '.json' or not path.is_file():
            continue
        try:
            report = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if report.get('report_type') != 'privacy_verification':
            continue
        old = report.get('final_output_location')
        final = published_paths.get(old)
        if not final:
            continue
        report['final_output_location'] = str(final)
        report['output']['filename'] = Path(final).name
        report['after']['filename'] = Path(final).name
        for comparison in report.get('fingerprints', {}).values():
            if 'comparison' in comparison:
                comparison['comparison']['second'] = Path(final).name
        json_write(path, report)
        stem = path.name[1:-len('.forge-private.json')] if path.name.startswith('.') and path.name.endswith('.forge-private.json') else path.stem
        html = path.parent / (stem + '.html')
        if html in returned and html.is_file():
            html.write_text(render_report_html(report), encoding='utf-8')


def process(source: Path | str, stage_dir: Path, options: dict, settings: dict, context: dict | None = None):
    values = VerificationOptions.model_validate(options).model_dump()
    context = context or {}
    source, stage_dir = Path(source), Path(stage_dir)
    if context.get('original_source'):
        original = _path(context['original_source'])
        expected = context.get('original_source_stat') or {}
        stat = original.stat()
        if expected and (stat.st_size != expected['size'] or stat.st_mtime_ns != expected['mtime_ns']):
            raise RuntimeError('The original changed after the script was queued; verification cannot use a different original.')
        pairs = [(original, source)]
    else:
        comparisons = context.get('comparison_sources') or []
        if not comparisons:
            raise ValueError('Select an original and at least one processed comparison file.')
        pairs = [(source, _path(value)) for value in comparisons]
    files, warnings = [], []
    for index, (original, output) in enumerate(pairs, 1):
        operations = context.get('operations')
        if not operations and context.get('chain_operations'):
            operations = [{'operation': tool, 'status': 'completed_upstream',
                           'evidence': 'FORGE dependency execution history; this is not independent forensic identification of the operation.'}
                          for tool in context['chain_operations']]
        report = build_report(original, output, settings, options=values, operations=operations)
        warnings += report['errors']
        warnings += summary_warnings(report)
        if values['reports']:
            files += save_report(report, stage_dir, f'{safe_name(output.stem)}_{index}_PRIVATE-verification', values['report_formats'])
        else:
            warnings.append(f'Compared {original.name} with {output.name}; private reports were not retained.')
    if values['reports']:
        warnings.append('Private verification reports contain original metadata, filenames and local output locations. Review before sharing.')
    return files, list(dict.fromkeys(warnings))
