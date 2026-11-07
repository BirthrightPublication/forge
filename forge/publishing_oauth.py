"""Google's installed-application OAuth flow and Windows user-bound credentials.

There is no embedded Google project or password login. The application owner
registers a Desktop OAuth client and approves the requested YouTube permissions.
Inference and ordinary local exports do not depend on this optional connection.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

AUTHORIZATION = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN = 'https://oauth2.googleapis.com/token'
REVOKE = 'https://oauth2.googleapis.com/revoke'
CHANNELS = 'https://www.googleapis.com/youtube/v3/channels'
VIDEOS = 'https://www.googleapis.com/upload/youtube/v3/videos'
VIDEO_RESOURCE = 'https://www.googleapis.com/youtube/v3/videos'
CAPTIONS = 'https://www.googleapis.com/upload/youtube/v3/captions'
READ_SCOPE = 'https://www.googleapis.com/auth/youtube.readonly'
UPLOAD_SCOPE = 'https://www.googleapis.com/auth/youtube.upload'
CAPTIONS_SCOPE = 'https://www.googleapis.com/auth/youtube.force-ssl'
STUDIO = 'https://studio.youtube.com/'
ALLOWED_HOSTS = {'oauth2.googleapis.com', 'www.googleapis.com', 'youtube.googleapis.com'}
IDENTITY = re.compile(r'^[a-zA-Z0-9_-]{1,100}$')


class PublishingError(ValueError):
    def __init__(self, message, *, code='publishing_error', retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


class DPAPIProtector:
    """Current Windows user's DPAPI; no plaintext fallback on other systems."""
    available = os.name == 'nt'

    @staticmethod
    def _crypt(data: bytes, decrypt=False):
        if os.name != 'nt':
            raise PublishingError('Secure YouTube connection storage requires Windows. Export locally and open YouTube Studio instead.', code='secure_storage_unavailable')

        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

        def blob(value):
            buffer = ctypes.create_string_buffer(value)
            return Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

        source, source_buffer = blob(data)
        entropy, entropy_buffer = blob(b'Birthright.FORGE.LanguagePublishing.v1')
        target = Blob()
        crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        if decrypt:
            method = crypt32.CryptUnprotectData
            method.restype = wintypes.BOOL
            method.argtypes = [ctypes.POINTER(Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(Blob),
                               ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            ok = method(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(target))
        else:
            method = crypt32.CryptProtectData
            method.restype = wintypes.BOOL
            method.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p,
                               ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            ok = method(ctypes.byref(source), 'FORGE YouTube connection', ctypes.byref(entropy), None, None, 1, ctypes.byref(target))
        if not ok:
            raise PublishingError('Windows could not protect this YouTube connection. Reconnect using the same Windows account.', code='secure_storage_failed')
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))

    def protect(self, data):
        return self._crypt(data)

    def unprotect(self, data):
        return self._crypt(data, True)


class CredentialVault:
    def __init__(self, data: Path, protector=None):
        self.root = Path(data).resolve() / 'publishing' / 'credentials'
        self.protector = protector if protector is not None else DPAPIProtector()

    @property
    def available(self):
        return bool(self.protector.available)

    def _path(self, key):
        if not IDENTITY.fullmatch(str(key)):
            raise PublishingError('Invalid local connection identity.')
        return self.root / (key + '.dpapi')

    def exists(self, key):
        path = self._path(key)
        return path.is_file() and not path.is_symlink()

    def save(self, key, value):
        if not self.available:
            raise PublishingError('Windows secure credential storage is unavailable. Use the export-and-open-Studio fallback.', code='secure_storage_unavailable')
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(encoded) > 256 * 1024:
            raise PublishingError('The connection record is too large.')
        protected = self.protector.protect(encoded)
        if self.root.parent.is_symlink() or self.root.is_symlink():
            raise PublishingError('The credential directory cannot be a symbolic link.')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self._path(key)
        if target.is_symlink():
            raise PublishingError('The credential file cannot be a symbolic link.')
        temporary = target.with_name(target.name + '.' + secrets.token_hex(8) + '.tmp')
        try:
            with temporary.open('xb') as handle:
                handle.write(protected)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, key):
        if not self.available:
            raise PublishingError('Reconnect this account on Windows, or export locally and open YouTube Studio.', code='secure_storage_unavailable')
        path = self._path(key)
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 512 * 1024:
            raise PublishingError('The saved connection is unavailable. Reconnect the intended channel.', code='connection_missing')
        try:
            result = json.loads(self.protector.unprotect(path.read_bytes()).decode('utf-8'))
        except PublishingError:
            raise
        except Exception:
            raise PublishingError('Windows could not unlock the connection. Reconnect using the original Windows account.', code='secure_storage_failed') from None
        if not isinstance(result, dict):
            raise PublishingError('The protected connection record is invalid.')
        return result

    def remove(self, key):
        path = self._path(key)
        if path.is_symlink():
            raise PublishingError('The credential file cannot be a symbolic link.')
        path.unlink(missing_ok=True)


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        try:
            value = json.loads(self.body.decode('utf-8'))
        except (UnicodeError, ValueError):
            raise PublishingError('Google returned an invalid response.', code='invalid_api_response') from None
        if not isinstance(value, dict):
            raise PublishingError('Google returned an invalid response.', code='invalid_api_response')
        return value


def allowed_url(url):
    value = urlparse(url)
    return (value.scheme == 'https' and value.hostname in ALLOWED_HOSTS and value.port in {None, 443}
            and not value.username and not value.password and not value.fragment)


def upload_url(url, kind='videos'):
    value = urlparse(url)
    if not allowed_url(url) or value.hostname not in {'www.googleapis.com', 'youtube.googleapis.com'}:
        raise PublishingError('Google returned an unsupported upload destination. No credentials were sent to it.', code='invalid_upload_destination')
    if not value.path.startswith('/upload/youtube/v3/' + kind):
        raise PublishingError('Google returned an unsupported upload path.', code='invalid_upload_destination')
    return url


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GoogleHTTP:
    """Bounded requests only to supported Google API endpoints; no bearer redirects."""
    def __init__(self):
        self.opener = build_opener(NoRedirects())

    def request(self, method, url, *, headers=None, data=None, timeout=45):
        if not allowed_url(url):
            raise PublishingError('Unsupported Google API destination.', code='invalid_api_destination')
        clean = {'User-Agent': 'Birthright-FORGE/LanguagePublishing', **(headers or {})}
        try:
            response = self.opener.open(Request(url, data=data, headers=clean, method=method), timeout=min(90, max(1, timeout)))
        except HTTPError as error:
            body = error.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise PublishingError('Google returned an oversized error response.', code='invalid_api_response') from None
            return Response(error.code, {k.lower(): v for k, v in error.headers.items()}, body)
        except (OSError, URLError):
            raise PublishingError('The Google publishing service is unavailable. Check the connection, then retry the saved transfer.', code='network_unavailable', retryable=True) from None
        with response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise PublishingError('Google returned an oversized response.', code='invalid_api_response')
            return Response(response.status, {k.lower(): v for k, v in response.headers.items()}, body)


ERROR_REASONS = {
    'quotaExceeded': 'The Google API project quota is exhausted. Keep the saved transfer and retry after its quota resets.',
    'uploadLimitExceeded': 'YouTube has reached this channel’s upload limit. Retry later or use YouTube Studio.',
    'insufficientPermissions': 'This connection lacks the required YouTube permission. Reconnect with the displayed permissions.',
    'forbidden': 'YouTube rejected this operation. Check the connected channel and API project permissions.',
    'invalidPublishAt': 'YouTube rejected the scheduled time. Choose a future time and Private visibility.',
    'youtubeSignupRequired': 'This Google account needs a YouTube channel before it can upload.',
    'invalid_grant': 'The Google connection expired or was revoked. Reconnect the intended channel.',
}


def check_response(response, allowed=(200,)):
    if response.status in allowed:
        return response
    reason = ''
    try:
        error = response.json().get('error', {})
        if isinstance(error, str):
            reason = error
        elif isinstance(error, dict):
            reason = ((error.get('errors') or [{}])[0]).get('reason', '')
    except (PublishingError, IndexError, AttributeError, TypeError):
        pass
    if not isinstance(reason, str):
        reason = ''
    message = ERROR_REASONS.get(reason)
    if not message:
        message = ('The Google connection is no longer authorized. Reconnect the intended channel.' if response.status == 401 else
                   f'Google publishing request failed (HTTP {response.status}). The saved transfer is retained.')
    error = PublishingError(message, code=reason if reason in ERROR_REASONS else f'google_http_{response.status}',
                            retryable=response.status == 429 or 500 <= response.status <= 599)
    retry_after = response.headers.get('retry-after', '')
    error.retry_after = int(retry_after) if str(retry_after).isdigit() else 0
    raise error


def normalize_client(value):
    if not isinstance(value, dict):
        raise PublishingError('Choose the downloaded Google Desktop OAuth client JSON.')
    if 'web' in value:
        raise PublishingError('Register a Desktop app OAuth client, not a Web application client.')
    value = value.get('installed', value)
    if not isinstance(value, dict) or not isinstance(value.get('client_id'), str) or not isinstance(value.get('client_secret'), str):
        raise PublishingError('The Google Desktop client JSON requires plain-text client ID and client secret fields.')
    identifier = value['client_id'].strip()
    secret = value['client_secret'].strip()
    if not re.fullmatch(r'[A-Za-z0-9._-]{5,250}\.apps\.googleusercontent\.com', identifier) or not 1 <= len(secret) <= 500:
        raise PublishingError('The Google Desktop client ID or client secret is missing or invalid.')
    if value.get('auth_uri', AUTHORIZATION) != AUTHORIZATION or value.get('token_uri', TOKEN) != TOKEN:
        raise PublishingError('OAuth endpoints must use Google’s supported authorization and token services.')
    return {'client_id': identifier, 'client_secret': secret}


def sanitize_channels(response):
    result = []
    for item in response.get('items', [])[:50]:
        identifier = item.get('id', '')
        if not re.fullmatch(r'UC[A-Za-z0-9_-]{20,24}', identifier):
            continue
        snippet = item.get('snippet') or {}
        thumbnail = ((snippet.get('thumbnails') or {}).get('default') or {}).get('url', '')
        parsed = urlparse(thumbnail)
        # UI can use its own placeholder; credentials never accompany avatars.
        if parsed.scheme != 'https' or not parsed.hostname or not (parsed.hostname.endswith('.googleusercontent.com') or parsed.hostname in {'yt3.ggpht.com', 'yt3.googleusercontent.com'}):
            thumbnail = ''
        result.append({'id': identifier, 'title': str(snippet.get('title') or 'YouTube channel')[:200],
                       'handle': str(snippet.get('customUrl') or '')[:200], 'thumbnail': thumbnail})
    return result


class OAuthFlow:
    """Temporary loopback listener owned by a single explicit Connect action."""
    def __init__(self, client, http, complete, *, captions=True, ttl=600):
        self.id = secrets.token_hex(16)
        self.state = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(64)
        self.client, self.http, self.complete = client, http, complete
        self.expires = time.time() + min(900, max(60, ttl))
        self.status, self.message, self.channels, self.connection_id = 'pending', '', [], ''
        self.lock = threading.RLock()
        self.used = False
        self.scopes = [READ_SCOPE, UPLOAD_SCOPE] + ([CAPTIONS_SCOPE] if captions else [])
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                # Authorization codes and state must never enter access logs.
                return

            def do_GET(self):
                host = self.headers.get('Host', '')
                if host != f'127.0.0.1:{self.server.server_port}' or self.client_address[0] != '127.0.0.1':
                    self.send_error(403)
                    return
                try:
                    values = parse_qs(urlparse(self.path).query, max_num_fields=12)
                except ValueError:
                    self.send_error(400)
                    return
                if urlparse(self.path).path != '/oauth/callback':
                    self.send_error(404)
                    return
                try:
                    outer.callback(values)
                    text = 'FORGE received the connection. Return to Dubbing Studio and confirm the intended channel.'
                    code = 200
                except PublishingError:
                    text = 'The connection was not completed. Return to FORGE for a safe diagnostic and retry.'
                    code = 400
                body = ('<!doctype html><meta charset="utf-8"><title>FORGE channel connection</title><p>' + text + '</p>').encode()
                self.send_response(code)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'none'; frame-ancestors 'none'")
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.server.timeout = .25
        self.redirect_uri = f'http://127.0.0.1:{self.server.server_port}/oauth/callback'
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')
        self.authorization_url = AUTHORIZATION + '?' + urlencode({'client_id': client['client_id'],
            'redirect_uri': self.redirect_uri, 'response_type': 'code', 'scope': ' '.join(self.scopes),
            'state': self.state, 'code_challenge': challenge, 'code_challenge_method': 'S256',
            'access_type': 'offline', 'prompt': 'consent select_account', 'include_granted_scopes': 'true'})
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, name='FORGE-YouTube-OAuth-' + self.id[:8], daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            while not self.stop.is_set() and time.time() < self.expires:
                self.server.handle_request()
        finally:
            self.server.server_close()

    def callback(self, values):
        supplied = values.get('state', [''])[0]
        with self.lock:
            if (self.used or time.time() >= self.expires or not isinstance(supplied, str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{40,100}', supplied)
                    or len(values.get('state', [])) != 1 or not secrets.compare_digest(supplied, self.state)):
                raise PublishingError('This channel connection expired or could not be verified. Start Connect again.', code='oauth_state_invalid')
            self.used = True
            if values.get('error'):
                self.status, self.message = 'failed', 'Google authorization was declined or unavailable. Local exports remain available.'
                self.stop.set()
                raise PublishingError(self.message, code='oauth_declined')
        code = values.get('code', [''])[0]
        if not isinstance(code, str) or not 1 <= len(code) <= 4096:
            self.status, self.message = 'failed', 'Google did not provide a valid authorization code.'
            self.stop.set()
            raise PublishingError(self.message)
        try:
            response = check_response(self.http.request('POST', TOKEN, headers={'Content-Type': 'application/x-www-form-urlencoded'},
                data=urlencode({'grant_type': 'authorization_code', 'code': code, 'client_id': self.client['client_id'],
                                'client_secret': self.client['client_secret'], 'redirect_uri': self.redirect_uri,
                                'code_verifier': self.verifier}).encode()))
            tokens = response.json()
            if (not isinstance(tokens.get('access_token'), str) or not 1 <= len(tokens['access_token']) <= 16384
                    or not tokens['access_token'].isascii() or any(char.isspace() for char in tokens['access_token'])):
                raise PublishingError('Google did not provide an authorized token.', code='oauth_token_invalid')
            tokens['expires_at'] = time.time() + min(86400, max(30, float(tokens.get('expires_in', 3600))))
            tokens['scopes'] = str(tokens.get('scope') or ' '.join(self.scopes)).split()
            if self.stop.is_set() or time.time() >= self.expires:
                raise PublishingError('The channel connection was cancelled or expired before completion.', code='oauth_cancelled')
            channels = sanitize_channels(check_response(self.http.request('GET', CHANNELS + '?' + urlencode({'part': 'id,snippet', 'mine': 'true', 'maxResults': 50}),
                headers={'Authorization': 'Bearer ' + tokens['access_token']})).json())
            if not channels:
                raise PublishingError('No supported YouTube channel was returned for this account. Choose the intended channel or use YouTube Studio.', code='channel_missing')
            with self.lock:
                if self.stop.is_set() or time.time() >= self.expires:
                    raise PublishingError('The channel connection was cancelled or expired before completion.', code='oauth_cancelled')
                connection_id = self.complete(tokens, channels)
                self.connection_id, self.channels, self.status = connection_id, channels, 'select_channel'
                self.message = 'Confirm the exact Russian channel before publishing.'
        except PublishingError as error:
            self.status, self.message = 'failed', str(error)
            raise
        except Exception:
            self.status, self.message = 'failed', 'The channel connection could not be completed. Retry Connect or use YouTube Studio.'
            raise PublishingError(self.message, code='oauth_failed') from None
        finally:
            self.stop.set()
            self.verifier = ''
            self.client = {}

    def public(self):
        with self.lock:
            if self.status not in {'connected', 'failed'} and time.time() >= self.expires:
                self.status, self.message = 'failed', 'The channel connection expired. Start Connect again.'
                self.stop.set()
            return {'flow_id': self.id, 'status': self.status, 'message': self.message,
                    'channels': list(self.channels), 'expires_at': self.expires}

    def close(self):
        with self.lock:
            self.stop.set()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
        with self.lock:
            self.client, self.verifier = {}, ''
