"""Shared downloader policies; no cookies, private URLs or raw extractor logs in diagnostics."""
from __future__ import annotations

import importlib.metadata
import re
import shutil
import subprocess
from urllib.parse import urlsplit


def validate_template(value):
    if not value:
        return value
    # Deliberately small, portable yt-dlp template language. No paths, expressions,
    # conversion flags or metadata traversal; the unique media ID is mandatory.
    if any(c in value for c in '\\/:\x00\r\n') or '..' in value:
        raise ValueError('Filename templates cannot contain paths or control characters.')
    tokens = re.findall(r'%\([a-z_]+\)(?:[.]?[0-9]{1,3})?[sdB]', value)
    allowed = {'title','id','ext','uploader','channel','upload_date','playlist_index'}
    if any(re.search(r'%\(([^)]+)\)', t).group(1) not in allowed for t in tokens):
        raise ValueError('Use title, id, ext, uploader, channel, upload_date or playlist_index.')
    remainder = value
    for token in tokens:
        remainder = remainder.replace(token, '')
    if '%' in remainder or not re.fullmatch(r'[\w .()\[\]-]*', remainder) or '%(id)s' not in value or not value.endswith('.%(ext)s'):
        raise ValueError('Use a portable filename ending in [%(id)s].%(ext)s; include the media ID.')
    return value


def media_url(value):
    parts = urlsplit(value.strip())
    if len(value) > 8000 or parts.scheme not in {'http','https'} or not parts.hostname or parts.username or parts.password:
        raise ValueError('Use a full HTTP/HTTPS media URL without embedded credentials.')
    return value.strip()


def diagnostic(error):
    text = str(error).lower()
    rules = [
        ('authentication', ('sign in','not a bot','login required','cookies','authentication','members-only','age-restricted'),
         'The source requires authentication. Select an authorized local cookies file if supported. Updating yt-dlp does not bypass account or network restrictions.', False),
        ('unavailable', ('private video','video unavailable','has been removed','deleted','not available in your country','copyright'),
         'This media is private, deleted, restricted or unavailable to this connection.', False),
        ('rate_limit', ('429','too many requests','rate limit','rate-limit'), 'The source rate-limited this connection. Monitoring will back off before retrying.', True),
        ('javascript', ('javascript','js runtime','deno','node.js'), 'A compatible JavaScript runtime is missing or failed. Review Download diagnostics and install an official compatible runtime.', False),
        ('ejs', ('ejs','signature solving','n challenge','nsig'), 'YouTube signature support failed. Repair the Downloads dependency class in Updates and check its EJS/runtime diagnostics.', False),
        ('format', ('requested format','no video formats','unsupported url','unsupported format'), 'The selected source or format is not available. Refresh the preview and choose a supported format.', False),
        ('postprocessing', ('ffmpeg','ffprobe','postprocess','conversion failed'), 'FFmpeg post-processing failed. Check the configured FFmpeg/FFprobe and available disk space.', False),
        ('network', ('timeout','timed out','connection','resolve','network','certificate','unreachable','could not be reached'), 'The media source could not be reached. Saved records are retained; retry after connectivity returns.', True),
        ('dependency', ('no module named','yt-dlp is not installed'), 'The download engine is unavailable. Repair the Downloads dependency class in Updates.', False),
    ]
    for code, words, message, retryable in rules:
        if any(word in text for word in words):
            return {'code':code, 'message':message, 'retryable':retryable}
    return {'code':'extractor', 'message':'The extractor could not complete this request. Check source access and Downloads diagnostics; automatic retries are paused.', 'retryable':False}


def ejs_compatible():
    try:
        installed=importlib.metadata.version('yt-dlp-ejs')
        requirements=importlib.metadata.requires('yt-dlp') or []
        expected=next((re.search(r'yt-dlp-ejs==([0-9.]+)',r,re.I).group(1)
                       for r in requirements if re.search(r'yt-dlp-ejs==([0-9.]+)',r,re.I)),None)
        return bool(expected and installed==expected)
    except importlib.metadata.PackageNotFoundError:
        return False


def diagnostics():
    packages = {}
    for package in ('yt-dlp','yt-dlp-ejs'):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    runtimes = []
    from .core import no_window
    for name in ('deno','node'):
        path = shutil.which(name)
        version, compatible = '', False
        if path:
            try:
                r = subprocess.run([path,'--version'], capture_output=True, text=True, timeout=5, **no_window())
                version = r.stdout.splitlines()[0][:100] if r.stdout else ''
                match = re.search(r'(\d+)\.(\d+)\.(\d+)', version)
                parsed = tuple(map(int,match.groups())) if match else (0,0,0)
                compatible = r.returncode == 0 and parsed >= ((2,3,0) if name == 'deno' else (22,0,0))
            except (OSError, subprocess.TimeoutExpired):
                pass
        runtimes.append({'name':name,'installed':bool(path),'version':version,'compatible':compatible})
    try: supported=2026<=int((packages['yt-dlp'] or '0').split('.')[0])<2027
    except ValueError: supported=False
    return {'packages':packages,'runtimes':runtimes,'yt_dlp_in_supported_range':supported,
            'ejs_installed':bool(packages['yt-dlp-ejs']),'ejs_compatible':ejs_compatible(),'repair_engine':'downloads',
            'runtime_links':{'deno':'https://deno.com/','node':'https://nodejs.org/'},
            'note':'Presence and minimum versions are diagnostics, not proof of access to a platform. Repairs use the existing approved dependency installer.'}
