"""Local QR rendering and format-specific metadata inspection/reduction.

QR preview responses are generated in memory. Neither request contents nor
rendered images are stored in the queue, presets, files, or application logs.
"""
from __future__ import annotations

import io
import re
import struct
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

from fastapi import HTTPException
from fastapi.responses import Response
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .core import CLEAN_MEDIA, IMAGES, safe_name


def qr_payload(options: dict) -> str:
    """Validate and serialize the four supported QR payload types."""
    kind = options.get('kind', 'text')
    text = options.get('text', '')
    if not isinstance(text, str) or '\x00' in text or len(text) > 2000:
        raise ValueError('QR text must contain at most 2000 characters and no NUL.')
    if kind in {'text', 'url'}:
        if not text.strip():
            raise ValueError('Enter the content to encode.')
        if kind == 'url':
            try:
                parsed = urlsplit(text)
                valid = parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password
            except ValueError:
                valid = False
            if not valid or any(c.isspace() for c in text):
                raise ValueError('Use a complete HTTP or HTTPS URL without embedded credentials.')
        return text
    if kind == 'contact':
        contact = options.get('contact', {})
        allowed = {'name', 'organization', 'phone', 'email', 'url', 'address', 'note'}
        if not isinstance(contact, dict) or set(contact) - allowed:
            raise ValueError('Unsupported contact field.')
        if any(not isinstance(v, str) or len(v) > 400 or any(ord(c) < 32 and c not in '\n\t' for c in v) for v in contact.values()):
            raise ValueError('Contact fields must be text of at most 400 characters.')
        if not contact.get('name', '').strip():
            raise ValueError('Enter a contact name.')
        def escaped(value):
            return value.replace('\\', '\\\\').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')
        lines = ['BEGIN:VCARD', 'VERSION:3.0', 'FN:' + escaped(contact['name']), 'N:;' + escaped(contact['name']) + ';;;']
        fields = {'organization': 'ORG', 'phone': 'TEL', 'email': 'EMAIL', 'url': 'URL', 'note': 'NOTE'}
        for key, tag in fields.items():
            if contact.get(key):
                lines.append(tag + ':' + escaped(contact[key]))
        if contact.get('address'):
            lines.append('ADR:;;' + escaped(contact['address']) + ';;;;')
        lines.append('END:VCARD')
        payload = '\r\n'.join(lines) + '\r\n'
        if len(payload.encode('utf-8')) > 2800:
            raise ValueError('This contact is too large for a QR code. Shorten its fields.')
        return payload
    if kind == 'wifi':
        wifi = options.get('wifi', {})
        if not isinstance(wifi, dict) or set(wifi) - {'ssid', 'password', 'security', 'hidden'}:
            raise ValueError('Unsupported Wi-Fi field.')
        ssid, password = wifi.get('ssid', ''), wifi.get('password', '')
        security, hidden = wifi.get('security', 'WPA'), wifi.get('hidden', False)
        if not isinstance(ssid, str) or not ssid or len(ssid.encode('utf-8')) > 32:
            raise ValueError('The Wi-Fi network name must contain 1–32 UTF-8 bytes.')
        if not isinstance(password, str) or len(password) > 128 or any(ord(c) < 32 for c in ssid + password):
            raise ValueError('Wi-Fi values must not contain control characters; passwords are limited to 128 characters.')
        if security not in {'WPA', 'WEP', 'nopass'} or not isinstance(hidden, bool):
            raise ValueError('Choose WPA, WEP, or an open network and a valid hidden-network setting.')
        if security != 'nopass' and not password:
            raise ValueError('Enter the Wi-Fi password, or choose an open network.')
        def escaped(value):
            return ''.join('\\' + c if c in '\\;,:"' else c for c in value)
        return f'WIFI:T:{security};S:{escaped(ssid)};P:{escaped(password) if security != "nopass" else ""};H:{str(hidden).lower()};;'
    raise ValueError('Unsupported QR content type.')


def qr_sensitive(options: dict) -> bool:
    """Also catch Wi-Fi strings pasted in the legacy plain-text field."""
    return options.get('kind') == 'wifi' or qr_payload(options).lstrip().upper().startswith('WIFI:') or bool(options.get('wifi'))


def validate_qr_style(options: dict) -> tuple[str, str, str, int, int]:
    foreground, background = options.get('foreground', '#111111'), options.get('background', '#ffffff')
    if any(not isinstance(c, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', c) for c in (foreground, background)):
        raise ValueError('QR colors must be six-digit hexadecimal colors.')
    def luminance(color):
        rgb = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
        rgb = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in rgb]
        return sum(c * weight for c, weight in zip(rgb, (.2126, .7152, .0722)))
    fg, bg = luminance(foreground), luminance(background)
    if bg <= fg or (bg + .05) / (fg + .05) < 4.5:
        raise ValueError('Use a dark QR foreground and a lighter background with at least 4.5:1 contrast.')
    correction = options.get('error_correction', 'M')
    box, border = options.get('box_size', 12), options.get('border', 4)
    if correction not in {'L', 'M', 'Q', 'H'} or type(box) is not int or not 4 <= box <= 30 or type(border) is not int or not 4 <= border <= 16:
        raise ValueError('Choose valid QR correction, module size (4–30), and quiet zone (4–16).')
    return foreground, background, correction, box, border


def qr_bytes(options: dict) -> bytes:
    import qrcode
    from qrcode.exceptions import DataOverflowError
    payload = qr_payload(options)
    foreground, background, correction, box, border = validate_qr_style(options)
    qr = qrcode.QRCode(error_correction=getattr(qrcode.constants, 'ERROR_CORRECT_' + correction), box_size=box, border=border)
    qr.add_data(payload)
    try:
        qr.make(fit=True)
    except DataOverflowError as exc:
        raise ValueError('The content is too large for this error-correction setting. Shorten it or lower correction.') from exc
    if options.get('format', 'png') == 'svg':
        matrix = qr.get_matrix()
        n = len(matrix)
        segments = ' '.join(f'M{x},{y}h1v1h-1z' for y, row in enumerate(matrix) for x, dark in enumerate(row) if dark)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{n*box}" height="{n*box}" viewBox="0 0 {n} {n}" role="img" aria-label="QR code">'
                f'<rect width="{n}" height="{n}" fill="{xml_escape(background)}"/>'
                f'<path d="{segments}" fill="{xml_escape(foreground)}"/></svg>').encode('utf-8')
    if options.get('format', 'png') != 'png':
        raise ValueError('Choose PNG or SVG output.')
    output = io.BytesIO()
    qr.make_image(fill_color=foreground, back_color=background).save(output, format='PNG')
    return output.getvalue()


def capabilities(suffix: str) -> dict:
    suffix = suffix.lower()
    image = suffix in IMAGES
    supported = image or suffix in CLEAN_MEDIA
    return {
        'format': suffix.lstrip('.'), 'supported': supported,
        'inspection': 'Pillow fields and supported embedded-preview headers' if image else 'FFprobe container and stream fields',
        'cleaning': 'Container metadata removed without recompressing pixels unless EXIF orientation must be applied' if image else 'Compatible streams remuxed without re-encoding',
        'standard': 'Removes descriptive fields and previews; retains ICC/color-rendering fields' if image else 'Removes tags, chapters, covers, attachments and data; retains compatible audio/video/subtitles',
        'maximum': 'Also removes supported ICC/color-profile metadata; appearance can change' if image else 'Also excludes subtitles; technical container fields may be regenerated',
        'selective_edit': not image and supported,
        'limits': ['Animated images are inspectable but cleaning is disabled.', 'Inspection covers supported parser fields, not arbitrary embedded data or steganography.', 'Visible content, filenames and filesystem history remain. No forensic-anonymity guarantee.'] if image else ['Subtitle and audiovisual content may contain identifying information.', 'Containers/codecs can regenerate technical fields.', 'No forensic-anonymity guarantee; filenames and filesystem history remain.'],
    }


def _exif_preview(data: bytes) -> list[dict]:
    """Inspect the TIFF IFD1 thumbnail pointer, with bounded offset reads."""
    if data.startswith(b'Exif\0\0'):
        data = data[6:]
    if len(data) < 8 or data[:2] not in {b'II', b'MM'}:
        return []
    endian = '<' if data[:2] == b'II' else '>'
    def word(offset, fmt):
        return struct.unpack_from(endian + fmt, data, offset)[0]
    try:
        offset = word(4, 'I')
        count = word(offset, 'H')
        next_ifd = word(offset + 2 + 12 * count, 'I')
        if not next_ifd:
            return []
        count = word(next_ifd, 'H')
        tags = {}
        for i in range(min(count, 1024)):
            entry = next_ifd + 2 + 12 * i
            tag, type_id, amount, value = struct.unpack_from(endian + 'HHII', data, entry)
            if tag in {513, 514} and type_id == 4 and amount == 1:
                tags[tag] = value
        offset, length = tags.get(513, 0), tags.get(514, 0)
        if not length or offset + length > len(data):
            return []
        item = {'kind': 'EXIF JPEG thumbnail', 'bytes': length, 'inspection': 'embedded image header; content not exported'}
        try:
            with Image.open(io.BytesIO(data[offset:offset + length])) as thumbnail:
                item['size'] = list(thumbnail.size)
        except (OSError, ValueError):
            item['readable'] = False
        return [item]
    except (ValueError, struct.error, OverflowError):
        return []


def image_inspection(path: Path) -> dict:
    def display(value):
        return f'[binary: {len(value)} bytes]' if isinstance(value, bytes) else str(value)[:2000]
    with Image.open(path) as image:
        exif = image.getexif()
        fields = {str(k): display(v) for k, v in exif.items()}
        for pointer in (34665, 34853):
            try:
                nested = exif.get_ifd(pointer)
                if nested:
                    fields[str(pointer)] = {str(k): display(v) for k, v in nested.items()}
            except (KeyError, ValueError, SyntaxError, struct.error):
                pass
        previews = _exif_preview(image.info.get('exif', b''))
        if image.format == 'JPEG':
            for marker, data in getattr(image, 'applist', []):
                if marker == 'APP0' and data.startswith(b'JFIF\0') and len(data) >= 14 and data[12] and data[13]:
                    previews.append({'kind': 'JFIF RGB thumbnail', 'size': [data[12], data[13]], 'bytes': len(data) - 14})
                if marker == 'APP0' and data.startswith(b'JFXX\0'):
                    previews.append({'kind': 'JFXX thumbnail', 'bytes': len(data) - 6})
        return {'format': image.format, 'size': list(image.size), 'animated': bool(getattr(image, 'is_animated', False)),
                'info': {k: display(v) for k, v in image.info.items()}, 'exif': fields,
                'embedded_previews': previews, 'capabilities': capabilities(path.suffix)}


def media_inspection(info: dict) -> dict:
    streams = info.get('streams', [])
    return {'format_tags': info.get('format', {}).get('tags', {}),
            'stream_tags': [{'index': s.get('index'), 'type': s.get('codec_type'), 'tags': s.get('tags', {})} for s in streams],
            'embedded_previews': [{'index': s.get('index'), 'kind': 'attached picture' if s.get('disposition', {}).get('attached_pic') else 'attachment',
                                   'codec': s.get('codec_name'), 'size': [s.get('width'), s.get('height')], 'tags': s.get('tags', {}),
                                   'inspection': 'FFprobe stream headers; content not exported'}
                                  for s in streams if s.get('disposition', {}).get('attached_pic') or s.get('codec_type') == 'attachment'],
            'chapters': info.get('chapters', [])}


def metadata_inventory(report: dict) -> dict:
    """Report observed fields, distinguishing format structure from EXIF/tags."""
    if 'info' in report:
        technical = {'jfif', 'jfif_version', 'jfif_unit', 'jfif_density', 'dpi', 'aspect', 'transparency'}
        return {'observed_metadata': {k: v for k, v in report['info'].items() if k not in technical},
                'exif': report.get('exif', {}), 'embedded_previews': report.get('embedded_previews', []),
                'technical_fields': {k: v for k, v in report['info'].items() if k in technical},
                'note': 'Fields observed by the supported parser. An empty report does not certify forensic anonymity.'}
    return {'observed_metadata': {'format_tags': report.get('format_tags', {}), 'stream_tags': report.get('stream_tags', [])},
            'embedded_previews': report.get('embedded_previews', []), 'chapters': report.get('chapters', []),
            'note': 'Technical container/stream tags can be regenerated. Inspect any remaining values before sharing.'}


def _clean_jpeg(data: bytes, maximum: bool) -> bytes:
    if data[:2] != b'\xff\xd8':
        raise ValueError('Invalid JPEG header.')
    result = bytearray(data[:2])
    offset = 2
    while offset < len(data):
        start = offset
        if data[offset] != 255:
            raise ValueError('Invalid JPEG marker.')
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset >= len(data):
            raise ValueError('Incomplete JPEG marker.')
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            result.extend(data[start:offset])
            return bytes(result)
        size = int.from_bytes(data[offset:offset+2], 'big')
        if size < 2 or offset + size > len(data):
            raise ValueError('Invalid JPEG segment length.')
        if marker == 0xDA:
            # Preserve every compressed scan, including progressive JPEG scans,
            # while still removing metadata segments between/after scans.
            scan = offset + size
            while scan < len(data):
                if data[scan] != 255:
                    scan += 1
                    continue
                next_byte = scan + 1
                while next_byte < len(data) and data[next_byte] == 255:
                    next_byte += 1
                if next_byte >= len(data):
                    raise ValueError('Incomplete JPEG scan.')
                if data[next_byte] == 0 or 0xD0 <= data[next_byte] <= 0xD7:
                    scan = next_byte + 1
                    continue
                break
            result.extend(data[start:scan])
            offset = scan
            continue
        payload = data[offset+2:offset+size]
        keep = marker not in range(0xE0, 0xF0) and marker != 0xFE
        if marker == 0xE0 and payload.startswith(b'JFIF\0') and len(payload) >= 14:
            # Retain rendering header but remove its optional RGB thumbnail.
            payload = payload[:12] + b'\0\0'
            result.extend(b'\xff\xe0' + (len(payload)+2).to_bytes(2, 'big') + payload)
        elif marker == 0xEE and payload.startswith(b'Adobe'):
            result.extend(data[start:offset+size])
        elif keep or (marker == 0xE2 and payload.startswith(b'ICC_PROFILE\0') and not maximum):
            result.extend(data[start:offset+size])
        offset += size
    raise ValueError('JPEG image data was not found.')


def _clean_png(data: bytes, maximum: bool) -> bytes:
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('Invalid PNG header.')
    result = bytearray(data[:8])
    offset = 8
    keep = {b'tRNS'} | (set() if maximum else {b'iCCP', b'sRGB', b'cHRM', b'gAMA', b'sBIT'})
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset:offset+4], 'big')
        kind = data[offset+4:offset+8]
        end = offset + size + 12
        if end > len(data):
            raise ValueError('Invalid PNG chunk length.')
        if kind[:1].isupper() or kind in keep:
            result.extend(data[offset:end])
        offset = end
        if kind == b'IEND':
            return bytes(result)
    raise ValueError('Incomplete PNG image.')


def _clean_webp(data: bytes, maximum: bool) -> bytes:
    if data[:4] != b'RIFF' or data[8:12] != b'WEBP':
        raise ValueError('Invalid WebP header.')
    chunks = bytearray()
    offset = 12
    keep = {b'VP8 ', b'VP8L', b'VP8X', b'ALPH'} | (set() if maximum else {b'ICCP'})
    while offset + 8 <= len(data):
        kind = data[offset:offset+4]
        size = int.from_bytes(data[offset+4:offset+8], 'little')
        end = offset + 8 + size + (size % 2)
        if end > len(data):
            raise ValueError('Invalid WebP chunk length.')
        chunk = bytearray(data[offset:end])
        if kind == b'VP8X' and size:
            chunk[8] &= ~(0x08 | 0x04 | (0x20 if maximum else 0))
        if kind in keep:
            chunks.extend(chunk)
        offset = end
    return b'RIFF' + (len(chunks)+4).to_bytes(4, 'little') + b'WEBP' + bytes(chunks)


def clean_image(source: Path, stage: Path, level: str = 'maximum'):
    if level not in {'standard', 'maximum'}:
        raise ValueError('Choose Standard or Maximum metadata reduction.')
    before = image_inspection(source)
    if before['animated']:
        raise RuntimeError('Animated image cleaning is not implemented; the original was left untouched.')
    maximum = level == 'maximum'
    notes = ['Metadata removal does not conceal visible content, watermarks, steganography, filenames or filesystem history.']
    with Image.open(source) as original:
        original.load()
        orientation = original.getexif().get(274, 1)
        if orientation not in {None, 1}:
            image = ImageOps.exif_transpose(original)
            mode = 'RGBA' if 'A' in image.mode or 'transparency' in image.info else 'RGB'
            clean = Image.new(mode, image.size)
            clean.paste(image.convert(mode))
            kwargs = {}
            if not maximum and original.info.get('icc_profile'):
                kwargs['icc_profile'] = original.info['icc_profile']
            if source.suffix.lower() in {'.jpg', '.jpeg'}:
                clean.convert('RGB').save(stage, 'JPEG', quality=95, subsampling=0, **kwargs)
            elif source.suffix.lower() == '.webp':
                clean.save(stage, 'WEBP', lossless=True, **kwargs)
            else:
                clean.save(stage, 'PNG', **kwargs)
            notes.append('EXIF orientation was applied to the pixels; this image was re-encoded. JPEG recompression can affect quality.')
        else:
            function = _clean_jpeg if source.suffix.lower() in {'.jpg', '.jpeg'} else _clean_png if source.suffix.lower() == '.png' else _clean_webp
            stage.write_bytes(function(source.read_bytes(), maximum))
            notes.append('Compressed image data was preserved without re-encoding; supported container metadata was removed.')
    with Image.open(stage) as verified:
        verified.load()
        expected_size = before['size'][::-1] if orientation in {5, 6, 7, 8} else before['size']
        if verified.format != before['format'] or list(verified.size) != expected_size:
            raise RuntimeError('Cleaned image failed format/dimension verification.')
    if maximum:
        notes.append('ICC/color-profile metadata was removed where supported; color appearance can change.')
    else:
        notes.append('ICC/color-rendering fields are retained by Standard cleaning and appear in the remaining-metadata report.')
    after = image_inspection(stage)
    return before, after, notes


class QRBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[dict] = Field(min_length=1, max_length=50)


def mount(app, store):
    """Mount behind the application's existing loopback/authentication middleware."""
    from .entitlements import require_feature
    def validated(options):
        from .models import QR
        try:
            return QR.model_validate(options).model_dump()
        except ValidationError:
            # Pydantic's default input echoes can contain Wi-Fi credentials.
            raise HTTPException(422, 'Invalid QR settings. Check the selected type, colors, size and fields.') from None

    @app.get('/api/metadata/capabilities')
    def metadata_capabilities():
        require_feature(app, 'metadata')
        return {'formats': [capabilities(suffix) for suffix in sorted(IMAGES | CLEAN_MEDIA)]}

    @app.post('/api/qr/preview')
    def qr_preview(body: dict):
        require_feature(app, 'qr')
        options = validated(body)
        output = qr_bytes(options)
        mime = 'image/svg+xml' if options['format'] == 'svg' else 'image/png'
        return Response(output, media_type=mime, headers={'Cache-Control': 'no-store, private', 'X-Content-Type-Options': 'nosniff', 'X-Forge-Persisted': 'false'})

    @app.post('/api/qr/bulk-preview')
    def qr_bulk_preview(body: QRBatch):
        require_feature(app, 'qr')
        options = [validated(item) for item in body.items]
        # Bound worst-case raster allocations while keeping useful bulk exports.
        if sum(item['box_size'] ** 2 for item in options) > 20000:
            raise ValueError('Reduce the batch size or pixels per module for bulk export.')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            used = set()
            for index, item in enumerate(options, 1):
                stem = safe_name(item['name'])
                name = stem + '.' + item['format']
                collision = 1
                while name.casefold() in used:
                    name = f'{stem} ({collision}).{item["format"]}'
                    collision += 1
                used.add(name.casefold())
                archive.writestr(name, qr_bytes(item))
        return Response(output.getvalue(), media_type='application/zip', headers={
            'Cache-Control': 'no-store, private', 'X-Content-Type-Options': 'nosniff', 'X-Forge-Persisted': 'false',
            'Content-Disposition': 'attachment; filename="FORGE-QR-codes.zip"'})
