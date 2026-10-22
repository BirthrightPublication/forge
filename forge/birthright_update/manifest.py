"""Signed Birthright Update Manifest v1. No keys are trusted from a manifest itself."""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone

from .core import Asset, Release, UpdateError


def canonical(payload):
    # Deliberately restricted canonical JSON, not an incomplete implementation of JCS.
    def check(value):
        if value is None or type(value) is bool or isinstance(value, str):
            return
        if type(value) is int and abs(value) <= 9007199254740991:
            return
        if isinstance(value, list):
            for child in value:
                check(child)
            return
        if isinstance(value, dict) and all(isinstance(k, str) and k.isascii() for k in value):
            for child in value.values():
                check(child)
            return
        raise UpdateError('Manifest canonical JSON permits no floats or non-ASCII object keys.')
    check(payload)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value):
        raise UpdateError('Manifest dates must use UTC seconds.')
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()


def verify(envelope, *, application, channel, trusted_keys, highest_sequence=0, now=None, policy=None):
    """Verify a signed envelope using application-pinned raw Ed25519 public keys.

    Persist highest_sequence only after successful verification. An installed
    application's explicit local rollback does not lower that discovery floor.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    if not isinstance(envelope, dict) or set(envelope) != {'signed', 'signatures'}:
        raise UpdateError('Invalid signed update envelope.')
    payload = envelope['signed']
    message = b'Birthright-Update-Manifest-v1\n' + canonical(payload)
    if not isinstance(envelope['signatures'], list) or len(envelope['signatures']) > 16:
        raise UpdateError('Invalid manifest signature list.')
    verified = False
    for item in envelope['signatures']:
        if not isinstance(item, dict) or item.get('algorithm') != 'ed25519' or item.get('key_id') not in trusted_keys:
            continue
        try:
            signature = base64.b64decode(item['signature'], validate=True)
            Ed25519PublicKey.from_public_bytes(trusted_keys[item['key_id']]).verify(signature, message)
            verified = True
            break
        except (KeyError, ValueError, InvalidSignature):
            continue
    if not verified:
        raise UpdateError('The update manifest has no valid trusted publisher signature.')
    required = {'schema', 'application', 'channel', 'version', 'sequence', 'published', 'expires', 'notes', 'assets'}
    if not isinstance(payload, dict) or set(payload) != required or payload['schema'] != 1:
        raise UpdateError('Unsupported signed manifest schema.')
    if payload['application'] != application or payload['channel'] != channel:
        raise UpdateError('The manifest belongs to another application or channel.')
    sequence = payload['sequence']
    if type(sequence) is not int or sequence < max(1, highest_sequence):
        raise UpdateError('An older manifest cannot replace newer update metadata.')
    current = now if now is not None else datetime.now(timezone.utc).timestamp()
    published, expires = timestamp(payload['published']), timestamp(payload['expires'])
    if not published <= current < expires or expires - published > 90 * 86400:
        raise UpdateError('The update manifest is expired, future-dated or valid for too long.')
    if not re.fullmatch(r'\d+\.\d+\.\d+', payload['version']) or not isinstance(payload['notes'], str):
        raise UpdateError('Invalid release version or notes.')
    if not isinstance(payload['assets'], list) or not 1 <= len(payload['assets']) <= 20:
        raise UpdateError('Invalid manifest asset inventory.')
    assets, names = [], set()
    for item in payload['assets']:
        if not isinstance(item, dict) or set(item) != {'name', 'url', 'sha256', 'size', 'platform', 'architecture', 'format'}:
            raise UpdateError('Invalid signed asset fields.')
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}', item['name']) or '..' in item['name']
                or item['name'].casefold() in names or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
                or type(item['size']) is not int or item['size'] <= 0
                or item['format'] not in {'app-zip-v1', 'source-zip-v1'}
                or item['platform'] not in {'windows', 'linux', 'macos'}
                or item['architecture'] not in {'x64', 'arm64'}):
            raise UpdateError('Invalid signed asset contract.')
        if policy:
            policy.validate(item['url'])
        elif not item['url'].startswith('https://'):
            raise UpdateError('Signed assets require HTTPS.')
        names.add(item['name'].casefold())
        assets.append(Asset(item['name'], item['url'], item['sha256'], item['size'], item['platform'], item['architecture'], item['format']))
    return Release(application, payload['version'], payload['published'], payload['notes'], tuple(assets),
                   channel, sequence, 'ed25519')
