"""Actual QR/image/FFmpeg checks; ZXing decoding is optional in minimal installs."""
from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest
from PIL import Image, ImageCms, PngImagePlugin

from forge.media_extensions import image_inspection, metadata_inventory, qr_bytes, qr_payload, qr_sensitive
from forge.models import QR
from forge.worker import probe, run, wipe
from test_forge import client, tone


def rasterized_svg(data):
    root = ElementTree.fromstring(data)
    width = int(root.attrib['width'])
    size = int(root.attrib['viewBox'].split()[-1])
    rect = root.find('{http://www.w3.org/2000/svg}rect')
    path = root.find('{http://www.w3.org/2000/svg}path')
    image = Image.new('RGB', (size, size), rect.attrib['fill'])
    for x, y in re.findall(r'M(\d+),(\d+)h1v1h-1z', path.attrib['d']):
        image.putpixel((int(x), int(y)), tuple(bytes.fromhex(path.attrib['fill'][1:])))
    return image.resize((width, width), Image.Resampling.NEAREST)


@pytest.mark.parametrize('fmt', ['png', 'svg'])
@pytest.mark.parametrize('options', [
    {'text': 'FORGE offline · Unicode ✓'},
    {'kind': 'url', 'text': 'https://example.com/a?b=one&c=two'},
    {'kind': 'contact', 'contact': {'name': 'Casey; Example', 'email': 'casey@example.com', 'note': 'Line one\nLine two', 'address': '1 Fictional Road'}},
    {'kind': 'wifi', 'wifi': {'ssid': 'semi;colon', 'password': 'secret:,\\value', 'hidden': True}},
])
def test_png_svg_decode_actual_payload(options, fmt):
    zxing = pytest.importorskip('zxingcpp', reason='Optional independent QR decoder; pip install zxing-cpp==2.3.0')
    options = QR(**options, format=fmt, foreground='#123456', background='#fff9ed', box_size=6, border=5).model_dump()
    data = qr_bytes(options)
    image = Image.open(io.BytesIO(data)) if fmt == 'png' else rasterized_svg(data)
    result = zxing.read_barcode(image)
    assert result is not None and result.valid
    assert result.text == qr_payload(options)
    assert image.getpixel((0, 0))[:3] == (255, 249, 237)
    assert image.size[0] % 6 == 0
    assert image.getpixel((5*6-1, 5*6-1))[:3] == (255, 249, 237)


def test_contact_wifi_escaping_and_legacy_payload():
    assert qr_payload({'text': 'Old queued content'}) == 'Old queued content'
    card = qr_payload({'kind': 'contact', 'contact': {'name': 'Example; Name', 'note': 'First\nINJECT: second'}})
    assert 'FN:Example\\; Name\r\n' in card and 'NOTE:First\\nINJECT: second\r\n' in card
    wifi = {'kind': 'wifi', 'wifi': {'ssid': 'test;:,"\\', 'password': 'pass;word'}}
    assert qr_payload(wifi) == 'WIFI:T:WPA;S:test\\;\\:\\,\\"\\\\;P:pass\\;word;H:false;;'
    assert qr_sensitive(wifi) and qr_sensitive({'text': '  WiFi:T:nopass;S:private;;'})


@pytest.mark.parametrize('options', [
    {'kind': 'url', 'text': 'javascript:alert(1)'},
    {'kind': 'url', 'text': 'https://user:secret@example.com'},
    {'kind': 'contact', 'contact': {'name': 'A\rB'}},
    {'kind': 'contact', 'contact': {'name': 'Valid', 'shell': 'ignored?'}},
    {'kind': 'wifi', 'wifi': {'ssid': 'x'*33, 'password': 'abc'}},
    {'kind': 'wifi', 'wifi': {'ssid': 'x', 'security': 'WPA', 'password': ''}},
    {'kind': 'wifi', 'wifi': {'ssid': 'x', 'password': 'abc', 'hidden': 'false'}},
    {'text': 'x', 'foreground': '#eeeeee', 'background': '#ffffff'},
    {'text': 'x', 'foreground': 'url(javascript:alert(1))'},
    {'text': 'x', 'foreground': '#ffffff', 'background': '#111111'},
    {'text': 'x', 'border': 0},
])
def test_invalid_qr_payloads_and_colors(options):
    with pytest.raises(ValueError):
        qr_bytes(options)


def test_memory_only_wifi_preview_and_bulk_collision_names(client, tmp_path):
    options = {'kind': 'wifi', 'wifi': {'ssid': 'Test network', 'password': 'unique-secret-no-disk-46912'}, 'name': 'Identical'}
    before = client.app.state.store.jobs()
    response = client.post('/api/qr/preview', json=options)
    assert response.status_code == 200 and response.headers['content-type'] == 'image/png'
    assert response.headers['x-forge-persisted'] == 'false' and 'no-store' in response.headers['cache-control']
    Image.open(io.BytesIO(response.content)).verify()
    response = client.post('/api/qr/bulk-preview', json={'items': [options, options, {**options, 'name': 'IDENTICAL'}]})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert len(names) == len(set(n.casefold() for n in names)) == 3
        assert names == ['Identical.png', 'Identical (1).png', 'IDENTICAL (2).png']
        for name in names:
            Image.open(io.BytesIO(archive.read(name))).verify()
    assert client.app.state.store.jobs() == before
    for path in (tmp_path/'data').rglob('*'):
        if path.is_file():
            assert b'unique-secret-no-disk-46912' not in path.read_bytes()
    for body in [options, {'text': ' WIFI:T:WPA;S:Test;P:unique-secret-no-disk-46912;;'}]:
        response = client.post('/api/jobs', json={'tool': 'qr', 'options': body})
        assert response.status_code in {400, 422}
    assert client.app.state.store.jobs() == before


def test_qr_private_validation_errors_and_auth(client):
    bad = {'kind': 'wifi', 'wifi': {'ssid': 'Test', 'password': 'do-not-echo-this-secret'}, 'format': 'html'}
    response = client.post('/api/qr/preview', json=bad)
    assert response.status_code == 422 and 'do-not-echo-this-secret' not in response.text
    response = client.post('/api/qr/bulk-preview', json={'items': [{'text': 'a'}]*51})
    assert response.status_code == 422
    response = client.post('/api/qr/preview', json={'text': 'x'}, headers={'X-Forge-Token': 'wrong'})
    assert response.status_code == 401


def test_contact_presets_persist_but_wifi_secrets_do_not(client):
    options = {'kind': 'contact', 'contact': {'name': 'Fictional Example', 'email': 'example@example.com'}, 'foreground': '#123456'}
    response = client.post('/api/presets', json={'tool': 'qr', 'name': 'Contact style', 'options': options, 'output': {'mode': 'default'}})
    assert response.status_code == 200
    saved = client.app.state.store.get('presets', [])
    assert saved[-1]['options']['contact']['name'] == 'Fictional Example'
    response = client.post('/api/presets', json={'tool': 'qr', 'name': 'Secret', 'options': {'kind': 'wifi', 'wifi': {'ssid': 'Example', 'password': 'must-not-save'}}, 'output': {'mode': 'default'}})
    assert response.status_code == 400
    assert client.app.state.store.get('presets', []) == saved


def test_qr_worker_preserves_existing_output(tmp_path):
    dest = tmp_path/'outputs';dest.mkdir()
    original = dest/'QR Code.svg';original.write_text('existing unrelated file')
    run({'tool': 'qr', 'options': {'kind': 'contact', 'contact': {'name': 'Example'}, 'format': 'svg'},
         'settings': {}, 'destination': str(dest), 'data': str(tmp_path/'data'), 'job_id': 'qr-colors'})
    assert original.read_text() == 'existing unrelated file'
    result = json.loads((tmp_path/'data/work/qr-colors/result.json').read_text())
    output = Path(result['outputs'][0]['path'])
    assert output.name == 'QR Code (1).svg'
    ElementTree.fromstring(output.read_bytes())
    assert not list(dest.glob('.forge-*.work'))


@pytest.mark.parametrize('suffix', ['png', 'jpg', 'webp'])
@pytest.mark.parametrize('level', ['standard', 'maximum'])
def test_image_reduction_preserves_decoded_pixels_and_original(tmp_path, suffix, level):
    source = tmp_path/f'input.{suffix}';out = tmp_path/f'clean.{suffix}'
    image = Image.new('RGB', (20, 16))
    for y in range(16):
        for x in range(20):
            image.putpixel((x, y), (x*12, y*15, (x+y)*7))
    exif = Image.Exif();exif[315] = 'Private creator';exif[270] = 'Private description'
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    kwargs = {'exif': exif, 'icc_profile': icc}
    if suffix == 'png':
        info = PngImagePlugin.PngInfo();info.add_text('Private note', 'hidden location');kwargs['pnginfo'] = info
    if suffix == 'jpg':
        kwargs['progressive'] = True
    image.save(source, **kwargs)
    original = source.read_bytes()
    with Image.open(source) as image:
        pixels = image.convert('RGB').tobytes()
    before, after, notes = wipe(source, out, {}, {'cleaning_level': level})
    assert source.read_bytes() == original
    with Image.open(out) as image:
        assert image.convert('RGB').tobytes() == pixels
        assert bool(image.info.get('icc_profile')) == (level == 'standard')
        assert not image.getexif()
    assert 'Private creator' in json.dumps(before) and 'Private creator' not in json.dumps(after)
    assert 'hidden location' not in json.dumps(after)
    assert any('without re-encoding' in note for note in notes)
    assert metadata_inventory(after)['exif'] == {}


def test_orientation_reencoding_is_disclosed_and_applied(tmp_path):
    source = tmp_path/'rotated.png';out = tmp_path/'clean.png'
    exif = Image.Exif();exif[274] = 6
    Image.new('RGB', (12, 8), 'red').save(source, exif=exif)
    original = source.read_bytes()
    _, after, notes = wipe(source, out, {})
    assert after['size'] == [8, 12] and source.read_bytes() == original
    assert any('re-encoded' in note for note in notes)


def test_embedded_exif_thumbnail_is_inspected_and_removed(tmp_path):
    thumbnail = io.BytesIO();Image.new('RGB', (3, 2), 'blue').save(thumbnail, 'JPEG')
    thumbnail = thumbnail.getvalue()
    tiff = b'II' + struct.pack('<HI', 42, 8) + struct.pack('<HI', 0, 14)
    tiff += struct.pack('<H', 2) + struct.pack('<HHII', 513, 4, 1, 44) + struct.pack('<HHII', 514, 4, 1, len(thumbnail)) + struct.pack('<I', 0) + thumbnail
    source = tmp_path/'thumbnail.jpg';out = tmp_path/'clean.jpg'
    Image.new('RGB', (20, 10), 'red').save(source, exif=b'Exif\0\0'+tiff)
    before = image_inspection(source)
    assert before['embedded_previews'][0]['size'] == [3, 2]
    _, after, _ = wipe(source, out, {})
    assert after['embedded_previews'] == [] and after['exif'] == {}


def test_real_media_standard_preserves_subtitles_maximum_excludes(tmp_path):
    audio = tone(tmp_path/'tone.wav', seconds=.5)
    captions = tmp_path/'captions.srt';captions.write_text('1\n00:00:00,000 --> 00:00:00,400\nExample subtitle\n')
    source = tmp_path/'original.mkv'
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(audio), '-i', str(captions), '-map', '0:a', '-map', '1:s',
                    '-c:a', 'flac', '-c:s', 'srt', '-metadata', 'title=Private title', '-metadata', 'comment=Private comment', str(source)], check=True)
    original = source.read_bytes()
    for level, expected in [('standard', ['audio', 'subtitle']), ('maximum', ['audio'])]:
        output = tmp_path/f'{level}.mkv'
        before, after, notes = wipe(source, output, {}, {'cleaning_level': level})
        assert [s['codec_type'] for s in probe(output, {})['streams']] == expected
        assert 'Private title' in json.dumps(before) and 'Private title' not in json.dumps(after)
        def audio_hash(path):
            return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a', '-f', 'hash', '-hash', 'sha256', '-'])
        assert audio_hash(source) == audio_hash(output)
    assert source.read_bytes() == original


def test_actual_media_attached_picture_inspection_and_removal(tmp_path):
    from forge.worker import media_metadata
    audio = tone(tmp_path/'tone.wav', seconds=.2)
    cover = tmp_path/'cover.png';Image.new('RGB', (8, 8), 'purple').save(cover)
    source = tmp_path/'covered.flac';output = tmp_path/'clean.flac'
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(audio), '-i', str(cover), '-map', '0:a', '-map', '1:v',
                    '-c:a', 'flac', '-c:v', 'copy', '-disposition:v', 'attached_pic', str(source)], check=True)
    assert media_metadata(probe(source, {}))['embedded_previews'][0]['kind'] == 'attached picture'
    _, after, _ = wipe(source, output, {})
    assert after['embedded_previews'] == [] and len(probe(output, {})['streams']) == 1


def test_private_audit_and_remaining_reports_describe_actual_result(tmp_path):
    source = tmp_path/'source.png'
    metadata = PngImagePlugin.PngInfo();metadata.add_text('Author', 'Private test author')
    Image.new('RGB', (8, 8)).save(source, pnginfo=metadata)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    dest = tmp_path/'exports'
    run({'tool': 'metadata', 'options': {'audit_report': True, 'remaining_report': True}, 'source': {'path': str(source)},
         'settings': {}, 'destination': str(dest), 'data': str(tmp_path/'data'), 'job_id': 'metadata-audit'})
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    result = json.loads((tmp_path/'data/work/metadata-audit/result.json').read_text())
    assert result['complete'] and len(result['outputs']) == 3
    audit = json.loads(next(dest.glob('*_PRIVATE-audit.json')).read_text())
    remaining = json.loads(next(dest.glob('*_remaining-metadata.json')).read_text())
    assert 'Private test author' in json.dumps(audit['before'])
    assert 'Private test author' not in json.dumps(audit['after']) + json.dumps(remaining)
    assert audit['remaining'] == remaining['remaining']
    assert remaining['capabilities']['format'] == 'png'


def test_inspection_capabilities_match_actual_formats(client):
    response = client.get('/api/metadata/capabilities')
    assert response.status_code == 200
    formats = {entry['format']: entry for entry in response.json()['formats']}
    assert {'png', 'jpg', 'webp', 'mp3', 'mp4', 'flac'} <= set(formats)
    assert 'pdf' not in formats and not formats['png']['selective_edit']
    assert formats['mp3']['selective_edit']


@pytest.mark.parametrize('duration,expected', [(.5, 80), (0, None), (None, None)])
def test_mocked_asr_adapter_measures_subsecond_duration(tmp_path, monkeypatch, duration, expected):
    import sys
    from types import SimpleNamespace
    import forge.worker as worker
    from forge.models import Transcribe

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, *args, **kwargs):
            segment = SimpleNamespace(start=0, end=.4, text='Example', avg_logprob=-.1, no_speech_prob=.1)
            return [segment], SimpleNamespace(duration=duration, language='en', language_probability=.99)

    events = []
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr('forge.model_cache.ensure_model', lambda *a, **k: tmp_path/'cached-model')
    monkeypatch.setattr('forge.cuda_health.runtime_report', lambda: {'runtime_available': False, 'dlls': {}})
    monkeypatch.setitem(sys.modules, 'ctranslate2', SimpleNamespace())
    monkeypatch.setattr(worker, 'emit', lambda kind, **data: events.append({'kind': kind, **data}))
    monkeypatch.setattr(worker, 'export_transcript', lambda *args: [])
    options = Transcribe(device='cpu', formats=['txt']).model_dump()
    worker.transcribe(tmp_path/'example.wav', tmp_path, 'example', options, tmp_path)
    assert events[0]['progress_measured'] is False
    event = next(event for event in events if event.get('stage') == 'Speech recognition')
    assert event['progress_measured'] is (expected is not None)
    if expected is not None:
        assert event['progress'] == pytest.approx(expected)
        assert event['eta_seconds'] >= 0
    else:
        assert event['progress'] == 0 and event['eta_seconds'] is None


@pytest.mark.parametrize('sizes,playlist,expected', [
    ({'total_bytes': 100}, False, 50),
    ({'total_bytes_estimate': 100}, False, None),
    ({'total_bytes': 0, 'total_bytes_estimate': 100}, False, None),
    ({'total_bytes': 100}, True, None),
])
def test_mocked_download_adapter_only_measures_known_totals(tmp_path, monkeypatch, sizes, playlist, expected):
    import sys
    from types import SimpleNamespace
    import forge.worker as worker
    from forge.models import Download

    class Downloader:
        def __init__(self, options):
            self.hook = options['progress_hooks'][0]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, *args, **kwargs):
            self.hook({'status': 'downloading', 'downloaded_bytes': 50, 'speed': 25, 'eta': 2, **sizes})
            (tmp_path/'example.mp3').write_bytes(b'mocked media')

    events = []
    monkeypatch.setitem(sys.modules, 'yt_dlp', SimpleNamespace(YoutubeDL=Downloader))
    monkeypatch.setattr(worker, 'emit', lambda kind, **data: events.append({'kind': kind, **data}))
    monkeypatch.setattr(worker, 'executable', lambda *args: '/mock/ffmpeg')
    monkeypatch.setattr(worker, 'probe', lambda *args: {'streams': [{'codec_type': 'audio'}]})
    worker.download('https://example.com/media', tmp_path, Download(playlist=playlist).model_dump(), {}, 'download_audio')
    event = events[0]
    assert event['progress_measured'] is (expected is not None)
    if expected is not None:
        assert event['progress'] == expected and event['eta_seconds'] == 2
    else:
        assert event['progress'] == 0 and event['eta_seconds'] is None
