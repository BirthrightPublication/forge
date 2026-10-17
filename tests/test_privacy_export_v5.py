"""Actual FFmpeg privacy exports and independent parser/signal comparisons."""
from __future__ import annotations

import hashlib
import html
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from forge.privacy_export import PrivacyExportOptions, effective_options, output_suffix, process, publicplan
from forge.privacy_verification import VerificationOptions, build_report, finalize_reports, process as verify_process
from forge.worker import probe, publish

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='Real FFmpeg/FFprobe required')


def ffmpeg(*args):
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', *map(str, args)], check=True, capture_output=True, timeout=30)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decoded_hash(path, stream):
    return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map', stream, '-f', 'hash', '-hash', 'sha256', '-'], timeout=20)


@pytest.fixture
def rich_media(tmp_path):
    captions = tmp_path / 'captions.srt'
    captions.write_text('1\n00:00:00,000 --> 00:00:00,800\nA retained caption\n', encoding='utf-8')
    attachment = tmp_path / 'personal-name.txt'
    attachment.write_text('Attachment content is retained only when explicitly selected.', encoding='utf-8')
    metadata = tmp_path / 'chapters.txt'
    metadata.write_text(';FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=800\ntitle=Private chapter\n', encoding='utf-8')
    output = tmp_path / 'original.mkv'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=size=96x64:rate=10:duration=1',
           '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
           '-f', 'lavfi', '-i', 'sine=frequency=880:duration=1',
           '-i', captions, '-f', 'ffmetadata', '-i', metadata,
           '-map', '0:v', '-map', '1:a', '-map', '2:a', '-map', '3:s',
           '-map_metadata', '-1', '-map_chapters', '4', '-c:v', 'ffv1', '-c:a', 'pcm_s16le', '-c:s', 'srt',
           '-attach', attachment, '-metadata:s:t', 'mimetype=text/plain',
           '-metadata', 'title=<script>private-title</script>', '-metadata', 'artist=Private Artist',
           '-metadata', 'comment=Keep only when selected', '-metadata', 'custom_id=Private Custom Identifier', '-metadata:c:0', 'title=Private chapter',
           '-metadata:s:a:0', 'title=Private first track', '-metadata:s:a:1', 'title=Private second track', output)
    return output


def metadata_values(info):
    values = list(info.get('format', {}).get('tags', {}).values())
    for item in info.get('streams', []) + info.get('chapters', []):
        values += list(item.get('tags', {}).values())
    return values


def test_standard_preserves_multiple_streams_chapters_and_decoded_media(rich_media, tmp_path):
    before_hash = digest(rich_media)
    plan = publicplan(rich_media, {}, {})
    assert len(plan['retained_streams']) == 5 and plan['encoding'] == 'stream_copy'
    files, warnings = process(rich_media, tmp_path / 'standard', {}, {}, {})
    assert len(files) == 1
    result = probe(files[0], {})
    assert [s['codec_type'] for s in result['streams']] == ['video', 'audio', 'audio', 'subtitle', 'attachment']
    assert len(result['chapters']) == 1
    assert all('Private' not in str(value) and 'private-title' not in str(value) for value in metadata_values(result))
    assert result['streams'][-1]['tags']['filename'].startswith('attachment-')
    for stream in ('0:v:0', '0:a:0', '0:a:1'):
        assert decoded_hash(rich_media, stream) == decoded_hash(files[0], stream)
    assert digest(rich_media) == before_hash
    assert any('not retained' in warning for warning in warnings)


def test_advanced_removes_extras_and_original_preserved(rich_media, tmp_path):
    before = digest(rich_media)
    files, _ = process(rich_media, tmp_path / 'advanced', {'preset': 'advanced'}, {}, {})
    info = probe(files[0], {})
    assert [s['codec_type'] for s in info['streams']] == ['video', 'audio', 'audio']
    assert info['chapters'] == []
    assert digest(rich_media) == before
    assert decoded_hash(rich_media, '0:v:0') == decoded_hash(files[0], '0:v:0')


def test_custom_selective_removes_custom_tags_and_retains_other_metadata(rich_media, tmp_path):
    options = {'preset': 'custom', 'metadata': 'selective', 'remove_tags': ['custom_id', 'title'],
               'retain_subtitles': False, 'retain_attachments': False, 'retain_chapters': True}
    files, _ = process(rich_media, tmp_path / 'custom', options, {}, {})
    info = probe(files[0], {})
    tags = {key.casefold(): value for key, value in info['format']['tags'].items()}
    assert tags['artist'] == 'Private Artist'
    assert 'custom_id' not in tags and 'title' not in tags
    assert not any(s['codec_type'] in {'subtitle', 'attachment'} for s in info['streams'])
    assert len(info['chapters']) == 1 and not info['chapters'][0].get('tags', {}).get('title')


def test_custom_metadata_retention_is_an_actual_copy_of_fields(rich_media, tmp_path):
    files, _ = process(rich_media, tmp_path / 'retain', {'preset': 'custom', 'metadata': 'retain'}, {}, {})
    values = metadata_values(probe(files[0], {}))
    assert 'Private Custom Identifier' in values and 'Private Artist' in values and 'Private chapter' in values


def test_artwork_standard_preserved_advanced_removed(tmp_path):
    image = tmp_path / 'cover.jpg'
    Image.new('RGB', (16, 16), 'red').save(image)
    original = tmp_path / 'cover.flac'
    ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-i', image,
           '-map', '0:a', '-map', '1:v', '-c:a', 'flac', '-c:v', 'copy', '-disposition:v', 'attached_pic', original)
    for preset, expected in [('standard', True), ('advanced', False)]:
        files, _ = process(original, tmp_path / preset, {'preset': preset}, {}, {})
        streams = probe(files[0], {})['streams']
        assert any(s.get('disposition', {}).get('attached_pic') for s in streams) is expected


def test_reports_show_actual_findings_and_escape_html(rich_media, tmp_path):
    files, _ = process(rich_media, tmp_path / 'report', {'preset': 'advanced', 'reports': True}, {}, {})
    output, report_path, html_path = files
    report = json.loads(report_path.read_text())
    assert report['report_type'] == 'privacy_verification'
    assert report['original']['format'] == 'matroska,webm'
    assert report['final_output_location'] == str(output.resolve())
    assert report['stream_changes']['codecs_changed']
    assert any(entry['value'] == 'Private Custom Identifier' for entry in report['metadata_changes']['removed_from_selected_inspection'])
    assert report['remaining_detectable_metadata']
    assert report['fingerprints']['audio']['status'] == 'not_requested'
    html_text = html_path.read_text()
    assert '<script>private-title</script>' not in html_text
    assert '&lt;script&gt;private-title&lt;/script&gt;' in html_text
    assert "default-src 'none'" in html_text


@pytest.mark.parametrize('formats', [['json', 'html'], ['html'], ['json']])
def test_actual_collision_location_finalized_in_all_report_formats(rich_media, tmp_path, formats):
    stage = tmp_path / 'stage'
    files, _ = process(rich_media, stage, {'reports': True, 'report_formats': formats, 'preset': 'advanced'}, {}, {})
    destination = tmp_path / files[0].name
    destination.write_text('existing output must survive')
    staged = files[0]
    final = publish(staged, destination)
    assert final.name.endswith('(1).mkv')
    finalize_reports(files, {str(staged.resolve()): str(final.resolve())})
    for path in files[1:]:
        text = html.unescape(path.read_text())
        assert str(final.resolve()) in text
        assert str(staged.resolve()) not in text
        assert final.name in text
    assert destination.read_text() == 'existing output must survive'


def test_reports_disabled_do_not_create_private_metadata_files(rich_media, tmp_path):
    stage = tmp_path / 'disabled'
    files, warnings = process(rich_media, stage, {'reports': False, 'preset': 'advanced'}, {}, {})
    assert list(stage.iterdir()) == files
    assert not any(path.suffix in {'.json', '.html'} for path in stage.rglob('*'))
    assert any('Verified parser-visible metadata' in warning for warning in warnings)


def test_real_audio_video_fingerprint_comparison_after_stream_copy(rich_media, tmp_path):
    files, _ = process(rich_media, tmp_path / 'fingerprints', {'preset': 'advanced'}, {}, {})
    report = build_report(rich_media, files[0], {}, options={'compare_audio': True, 'compare_video': True})
    for kind in ('audio', 'video'):
        assert report['fingerprints'][kind]['status'] == 'compared'
        assert report['fingerprints'][kind]['comparison']['similarity'] > .99
    assert report['fingerprints']['audio']['comparison']['components']['identical_decoded_interval'] is True
    assert report['fingerprints']['video']['comparison']['timeline']


def test_explicit_lossy_encode_and_format_change_disclosed_and_verified(rich_media, tmp_path):
    options = {'preset': 'advanced', 'format': 'mp4', 'reencode': True, 'reports': True}
    plan = publicplan(rich_media, options, {})
    assert plan['encoding'] == 'reencode' and any('lossy' in note for note in plan['warnings'])
    files, _ = process(rich_media, tmp_path / 'encoded', options, {}, {})
    assert files[0].suffix == '.mp4'
    assert [stream['codec_name'] for stream in probe(files[0], {})['streams']] == ['h264', 'aac', 'aac']
    report = json.loads(files[1].read_text())
    assert report['stream_changes']['codecs_changed']


def test_image_export_reuses_non_destructive_cleaner_and_retention(tmp_path):
    source = tmp_path / 'image.png'
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('Author', 'Private Artist')
    Image.new('RGB', (24, 16), 'gold').save(source, pnginfo=metadata)
    before = digest(source)
    files, _ = process(source, tmp_path / 'clean', {'reports': True}, {}, {})
    assert not Image.open(files[0]).info.get('Author')
    assert Image.open(source).tobytes() == Image.open(files[0]).tobytes()
    retained, _ = process(source, tmp_path / 'retain', {'preset': 'custom', 'metadata': 'retain'}, {}, {})
    assert digest(retained[0]) == before == digest(source)
    with pytest.raises(ValueError, match='Selective image'):
        publicplan(source, {'preset': 'custom', 'metadata': 'selective', 'remove_tags': ['Author']}, {})


def test_standalone_and_chain_verification_contexts(rich_media, tmp_path):
    outputs, _ = process(rich_media, tmp_path / 'processed', {'preset': 'advanced'}, {}, {})
    files, _ = verify_process(rich_media, tmp_path / 'standalone', {'reports': True}, {}, {'comparison_sources': [{'path': str(outputs[0])}]})
    report = json.loads(files[0].read_text())
    assert report['original']['filename'] == rich_media.name and report['output']['filename'] == outputs[0].name
    files, _ = verify_process(outputs[0], tmp_path / 'chain', {'reports': True}, {}, {
        'original_source': {'path': str(rich_media)}, 'chain_operations': ['metadata', 'privacy_export']})
    report = json.loads(files[0].read_text())
    assert [item['operation'] for item in report['operations']] == ['metadata', 'privacy_export']
    assert all(item['status'] == 'completed_upstream' for item in report['operations'])
    empty, warnings = verify_process(outputs[0], tmp_path / 'sensitive', {}, {}, {'original_source': {'path': str(rich_media)}})
    assert empty == [] and not (tmp_path / 'sensitive').exists()
    assert any('Verified parser-visible metadata' in warning for warning in warnings)


def test_changed_original_and_missing_comparison_rejected(rich_media, tmp_path):
    with pytest.raises(ValueError, match='at least one'):
        verify_process(rich_media, tmp_path / 'report', {}, {}, {})
    with pytest.raises(RuntimeError, match='original changed'):
        verify_process(rich_media, tmp_path / 'report', {}, {}, {'original_source': {'path': str(rich_media)},
                                                               'original_source_stat': {'size': 1, 'mtime_ns': 1}})


@pytest.mark.parametrize('options', [
    {'preset': 'silent'}, {'quality': 2}, {'quality': 36}, {'format': '../../bad'}, {'shell': 'unsafe'},
    {'report_formats': ['xml']}, {'preset': 'custom', 'metadata': 'selective'},
    {'remove_tags': ['-unsafe']}, {'remove_tags': ['a\ncommand']}, {'remove_tags': ['a' * 129]},
])
def test_export_parameters_are_allowlisted(options):
    with pytest.raises(ValueError):
        PrivacyExportOptions.model_validate(options)


def test_presets_project_choices_and_suffixes():
    assert effective_options({'preset': 'advanced', 'retain_subtitles': True})['retain_subtitles'] is False
    assert effective_options({'preset': 'standard', 'retain_subtitles': False})['retain_subtitles'] is True
    assert output_suffix(Path('a.MKV'), {}) == '.mkv'
    assert output_suffix(Path('a.mkv'), {'format': 'mp4'}) == '.mp4'
    with pytest.raises(ValueError):
        VerificationOptions.model_validate({'report_formats': ['xml']})


def test_incompatible_streams_and_failed_encode_do_not_publish_or_change_original(rich_media, tmp_path):
    original = digest(rich_media)
    with pytest.raises(ValueError, match='attachments'):
        publicplan(rich_media, {'format': 'mp4'}, {})
    with pytest.raises(ValueError, match='exactly one audio'):
        publicplan(rich_media, {'format': 'wav'}, {})
    with pytest.raises(RuntimeError, match='FFmpeg failed'):
        process(rich_media, tmp_path / 'fail', {'format': 'mp4', 'preset': 'advanced'}, {}, {})
    assert digest(rich_media) == original


def test_existing_stage_media_not_overwritten(rich_media, tmp_path):
    stage = tmp_path / 'stage'
    stage.mkdir()
    existing = stage / 'original_privacy.mkv'
    existing.write_text('keep')
    with pytest.raises(ValueError, match='new staged'):
        process(rich_media, stage, {}, {}, {})
    assert existing.read_text() == 'keep'

