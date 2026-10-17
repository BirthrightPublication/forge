"""Real local fixtures plus explicitly separated dependency/error-boundary tests."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin
from pydantic import ValidationError

from forge import media_inspection as inspection
from forge import provenance


@pytest.fixture
def tagged_image(tmp_path):
    path = tmp_path / 'private-source.jpg'
    exif = Image.Exif()
    exif[271], exif[272], exif[305] = 'Synthetic Camera', 'Example Model', 'Test Editor'
    exif[315], exif[306], exif[270] = 'Fictional Creator', '2026:01:02 03:04:05', '<script>private comment</script>'
    exif[34853] = {1: 'N', 2: (33., 44., 55.), 3: 'E', 4: (11., 22., 33.)}
    Image.new('RGB', (48, 32), 'red').save(path, exif=exif)
    return path


@pytest.fixture
def structured_media(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('Real FFmpeg fixture tools unavailable')
    subtitles = tmp_path / 'captions.srt'
    subtitles.write_text('1\n00:00:00,000 --> 00:00:00,800\nSynthetic caption\n', encoding='utf-8')
    metadata = tmp_path / 'metadata.txt'
    metadata.write_text(';FFMETADATA1\ntitle=Fictional title\nartist=Fictional author\ncomment=Private fixture comment\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=800\ntitle=Example chapter\n', encoding='utf-8')
    attachment = tmp_path / 'attachment.txt'
    attachment.write_text('Synthetic attachment', encoding='utf-8')
    target = tmp_path / 'structured.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'color=c=blue:s=96x64:r=12',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-i', str(subtitles),
                    '-f', 'ffmetadata', '-i', str(metadata), '-map', '0:v', '-map', '1:a', '-map', '2:s',
                    '-map_metadata', '3', '-map_chapters', '3', '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-c:a', 'aac', '-c:s', 'srt', '-attach', str(attachment), '-metadata:s:t', 'mimetype=text/plain',
                    '-t', '1', str(target)], check=True, capture_output=True, timeout=30)
    return target


def test_real_image_classifies_sensitive_fields_without_changing_source(tagged_image):
    before = hashlib.sha256(tagged_image.read_bytes()).hexdigest()
    report = inspection.inspect_media(tagged_image, {}, include_provenance=False)
    assert report['kind'] == 'image'
    assert report['media']['width'] == 48
    for category in ('gps', 'device', 'software', 'timestamps', 'author', 'comments'):
        assert report['metadata']['categories'][category]
        assert next(f for f in report['findings'] if f['category'] == category)['status'] == 'detected'
    assert report['filename'] == tagged_image.name
    assert str(tagged_image.parent) not in json.dumps(report)
    assert hashlib.sha256(tagged_image.read_bytes()).hexdigest() == before


def test_real_clean_image_reports_selected_method_not_absolute_absence(tmp_path):
    path = tmp_path / 'clean.png'
    Image.new('RGB', (20, 20), 'blue').save(path)
    report = inspection.inspect_media(path, {}, include_provenance=False)
    assert report['status'] == 'not_detected'
    assert all(f['status'] == 'not_detected' for f in report['findings'])
    assert any('not proof' in note for note in report['limitations'])


def test_real_png_xmp_is_inspected_and_entities_are_not_expanded(tmp_path):
    path = tmp_path / 'xmp.png'
    info = PngImagePlugin.PngInfo()
    info.add_itxt('XML:com.adobe.xmp', '<x xmlns:e="http://ns.adobe.com/exif/1.0/" e:GPSLatitude="33,44N" e:Software="Local test"/>')
    Image.new('RGB', (20, 20)).save(path, pnginfo=info)
    report = inspection.inspect_media(path, {}, include_provenance=False)
    assert report['metadata']['categories']['gps']
    malicious = '<!DOCTYPE x [<!ENTITY f SYSTEM "file:///etc/passwd">]><x>&f;</x>'
    entries = []
    assert inspection._xmp_entries(malicious, entries, 'test') is False
    assert not entries


def test_real_streams_chapters_attachments_and_tags(structured_media):
    before = structured_media.read_bytes()
    report = inspection.inspect_media(structured_media, {}, include_provenance=False)
    assert report['status'] == 'detected'
    assert {s['codec_type'] for s in report['media']['streams']} >= {'video', 'audio', 'subtitle', 'attachment'}
    assert report['media']['chapters']
    assert report['media']['attachments']
    assert report['metadata']['categories']['author']
    assert report['metadata']['categories']['comments']
    assert str(structured_media.parent) not in json.dumps(report)
    assert structured_media.read_bytes() == before


def test_actual_reports_are_private_escaped_and_self_contained(tagged_image, tmp_path):
    files, warnings = inspection.process_audit(tagged_image, tmp_path / 'reports', {'include_provenance': False}, {})
    assert not warnings
    assert {p.suffix for p in files} == {'.json', '.html'}
    report = json.loads(next(p for p in files if p.suffix == '.json').read_text())
    assert report['metadata']['categories']['gps']
    rendered = next(p for p in files if p.suffix == '.html').read_text()
    assert '<script>private comment</script>' not in rendered
    assert '&lt;script&gt;private comment&lt;/script&gt;' in rendered
    assert 'default-src' in rendered
    assert '<script' not in rendered
    assert 'src="http' not in rendered


def test_report_creation_can_be_disabled(tagged_image, tmp_path):
    output = tmp_path / 'no-reports'
    files, warnings = inspection.process_audit(tagged_image, output, {'reports': False, 'report_formats': [], 'include_provenance': False}, {})
    assert files == [] and warnings == []
    assert not output.exists()


def test_real_audit_optional_fingerprints_do_not_retain_reference_vectors(structured_media, tmp_path):
    files, warnings = inspection.process_audit(structured_media, tmp_path / 'fingerprint-report',
        {'include_provenance': False, 'include_fingerprints': True, 'report_formats': ['json']}, {})
    result = json.loads(files[0].read_text())
    assert set(result['fingerprints']) == {'audio', 'video'}
    assert 'fingerprint' not in result['fingerprints']['audio']
    assert 'fingerprint' not in result['fingerprints']['video']
    assert result['fingerprints']['audio']['sample_rate']
    assert result['fingerprints']['video']['frames']
    assert all(set(frame) <= {'timestamp', 'preview'} for frame in result['fingerprints']['video']['frames'])
    assert not warnings


def test_malformed_media_reports_unknown_not_missing(tmp_path):
    path = tmp_path / 'broken.jpg'
    path.write_bytes(b'not a media file')
    report = inspection.inspect_media(path, {}, include_provenance=False)
    assert report['status'] == 'unknown'
    assert report['errors']
    assert all(f['status'] == 'unknown' for f in report['findings'])


def test_unsupported_file_is_not_misreported_as_clean(tmp_path):
    path = tmp_path / 'sample.document'
    path.write_bytes(b'synthetic file')
    report = inspection.inspect_media(path, {}, include_provenance=False)
    assert report['status'] == 'unsupported'


def test_options_reject_unsupported_fields_and_empty_enabled_reports():
    with pytest.raises(ValidationError):
        inspection.TrackingAuditOptions.model_validate({'upload': True})
    with pytest.raises(ValidationError):
        inspection.ProvenanceOptions.model_validate({'report_formats': []})
    with pytest.raises(ValidationError):
        inspection.ProvenanceOptions.model_validate({'report_formats': ['pdf']})
    assert inspection.ProvenanceOptions(report_formats=['json', 'json']).report_formats == ['json']


def test_report_output_cannot_escape_stage_or_overwrite(tmp_path):
    files = inspection.write_reports({'title': '<img src=x onerror=alert(1)>'}, tmp_path, '../../bad', ['html'])
    assert files[0].parent == tmp_path
    assert '<img src=x' not in files[0].read_text()
    with pytest.raises(FileExistsError):
        inspection.write_reports({}, tmp_path, '../../bad', ['html'])
    with pytest.raises(ValueError):
        inspection.write_reports({}, tmp_path, 'bad', ['exe'])


def test_mocked_missing_ffprobe_is_unsupported(monkeypatch, tmp_path):
    path = tmp_path / 'fixture.wav'
    path.write_bytes(b'fixture')
    monkeypatch.setattr(inspection, 'executable', lambda *args: None)
    report = inspection.inspect_media(path, {}, include_provenance=False)
    assert report['status'] == 'unsupported'


def test_mocked_missing_c2pa_is_unsupported(monkeypatch, tagged_image):
    monkeypatch.setattr(provenance, 'capabilities', lambda: {'name': 'c2pa-python', 'compatible': False, 'available': False})
    result = provenance.inspect_provenance(tagged_image, {})
    assert result['status'] == 'unsupported'
    assert result['signature']['status'] == 'unknown'


def test_mocked_c2pa_timeout_remains_unknown(monkeypatch, tagged_image):
    monkeypatch.setattr(provenance, 'capabilities', lambda: {'name': 'c2pa-python', 'compatible': True, 'available': True})
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('isolated-reader', 120)
    monkeypatch.setattr(provenance.subprocess, 'run', timeout)
    result = provenance.inspect_provenance(tagged_image, {})
    assert result['status'] == 'unknown'
    assert result['signature']['status'] == 'unknown'


def test_missing_input_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        inspection.inspect_media(tmp_path / 'missing.jpg', {})
    with pytest.raises(ValueError):
        provenance.inspect_provenance(tmp_path / 'missing.jpg', {})


def test_mocked_empty_validation_is_not_assumed_valid():
    result = provenance.summarize_manifest({'manifests': {'test': {'title': 'Unverified'}}, 'active_manifest': 'test'}, {})
    assert result['status'] == 'detected'
    assert result['signature']['status'] == result['trust']['status'] == result['binding']['status'] == 'unknown'


def test_mocked_ingredient_success_is_not_active_manifest_success():
    result = provenance.summarize_manifest({'manifests': {'test': {}}, 'validation_results': {'ingredientDeltas': [{'success': [{'code': 'claimSignature.validated'}]}]}}, {})
    assert result['signature']['status'] == 'unknown'


def test_offline_configuration_blocks_remote_fetches():
    assert provenance.OFFLINE_SETTINGS['verify']['remote_manifest_fetch'] is False
    assert provenance.OFFLINE_SETTINGS['verify']['ocsp_fetch'] is False
    assert provenance.OFFLINE_SETTINGS['core']['allowed_network_hosts'] == []
    assert provenance.OFFLINE_SETTINGS['core']['decode_identity_assertions'] is False


@pytest.fixture
def c2pa_fixture_factory(tmp_path):
    c2pa = pytest.importorskip('c2pa', reason='Optional actual C2PA SDK unavailable')
    if not provenance.capabilities()['compatible']:
        pytest.skip('Exact tested optional C2PA SDK unavailable')
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID, ObjectIdentifier

    # Ephemeral self-signed synthetic fixture only. FORGE exposes no signer API.
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'FORGE synthetic test fixture ONLY')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                                        key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=None, decipher_only=None), True)
            .add_extension(x509.ExtendedKeyUsage([ObjectIdentifier('1.3.6.1.5.5.7.3.4')]), False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False).sign(key, hashes.SHA256()))
    source = tmp_path / 'synthetic.jpg'
    Image.new('RGB', (48, 32), 'purple').save(source)

    def create(name='signed.jpg', remote_url=None):
        target = tmp_path / name
        settings = {**provenance.OFFLINE_SETTINGS, 'verify': {**provenance.OFFLINE_SETTINGS['verify'], 'verify_after_sign': False},
                    'builder': {'thumbnail': {'enabled': False}}}
        info = c2pa.C2paSignerInfo(c2pa.C2paSigningAlg.ES256, cert.public_bytes(serialization.Encoding.PEM),
                                 key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), None)
        definition = {'title': 'Explicit synthetic test fixture', 'assertions': [{'label': 'c2pa.actions', 'data': {'actions': [
            {'action': 'c2pa.created', 'digitalSourceType': 'http://cv.iptc.org/newscodes/digitalsourcetype/digitalCreation'}]}}]}
        with c2pa.Context.from_dict(settings) as context, c2pa.Signer.from_info(info) as signer:
            with c2pa.Builder(definition, context=context) as builder:
                if remote_url:
                    builder.set_remote_url(remote_url)
                    builder.set_no_embed()
                builder.sign_file(str(source), str(target), signer)
        return target
    return create


def test_real_c2pa_signed_media_has_separate_signature_trust_and_binding(c2pa_fixture_factory):
    path = c2pa_fixture_factory()
    before = path.read_bytes()
    result = provenance.inspect_provenance(path, {})
    assert result['status'] == 'detected'
    assert result['signature']['status'] == 'valid'
    assert result['trust']['status'] == 'untrusted'
    assert result['binding']['status'] == 'match'
    assert result['manifests'][result['active_manifest']]['signature_info']['common_name'].endswith('ONLY')
    assert path.read_bytes() == before


def test_real_c2pa_plain_image_has_no_detected_manifest(tagged_image):
    if not provenance.capabilities()['compatible']:
        pytest.skip('Optional C2PA SDK unavailable')
    result = provenance.inspect_provenance(tagged_image, {})
    assert result['status'] == 'not_detected'
    assert result['signature']['status'] == 'unknown'


def test_real_c2pa_tampered_media_reports_binding_mismatch(c2pa_fixture_factory):
    path = c2pa_fixture_factory()
    data = bytearray(path.read_bytes())
    data[-10] ^= 1
    path.write_bytes(data)
    result = provenance.inspect_provenance(path, {})
    assert result['status'] == 'detected'
    assert result['signature']['status'] == 'valid'
    assert result['binding']['status'] == 'mismatch'


def test_real_c2pa_remote_manifest_is_not_requested(c2pa_fixture_factory):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_error(404)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        path = c2pa_fixture_factory('remote.jpg', 'http://127.0.0.1:' + str(server.server_port) + '/manifest.c2pa')
        result = provenance.inspect_provenance(path, {})
        assert not requests
        assert result['status'] == 'unknown'
        assert result['signature']['status'] == 'unknown'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_provenance_reports_and_disabled_reports(c2pa_fixture_factory, tmp_path):
    path = c2pa_fixture_factory()
    files, warnings = inspection.process_provenance(path, tmp_path / 'report', {}, {})
    assert len(files) == 2 and not warnings
    assert json.loads(files[0].read_text())['provenance']['signature']['status'] == 'valid'
    files, warnings = inspection.process_provenance(path, tmp_path / 'disabled', {'reports': False}, {})
    assert not files and not warnings
    assert not (tmp_path / 'disabled').exists()
