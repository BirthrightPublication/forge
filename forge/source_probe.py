"""Bounded metadata extraction in an expendable subprocess, shared by downloads and watches."""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from urllib.parse import parse_qs, urlsplit

from .download_support import diagnostic, media_url

CONTENT_TYPES = ('video','short','live','replay','upcoming','premiere','clip','podcast','unclassified')


def classify(info):
    """Explicit live/availability markers take precedence; duration is never a Shorts test."""
    status = info.get('live_status')
    if info.get('is_upcoming') or status == 'is_upcoming':
        return 'upcoming'
    if info.get('is_live') or status == 'is_live':
        return 'live'
    if info.get('was_live') or status in {'was_live','post_live'}:
        return 'replay'
    if info.get('is_premiere'):
        return 'premiere'
    url = info.get('webpage_url') or info.get('url') or ''
    if '/shorts/' in url or info.get('_forge_tab') == 'shorts':
        return 'short'
    if '/clip/' in url:
        return 'clip'
    if info.get('is_podcast'):
        return 'podcast'
    if info.get('availability') in {'private','premium_only','subscriber_only','needs_auth'}:
        return 'unclassified'
    if info.get('extractor_key') == 'Youtube' or info.get('extractor') == 'youtube':
        return 'video'
    return 'unclassified'


def youtube_source(url):
    p = urlsplit(media_url(url))
    if p.hostname.lower() not in {'youtube.com','www.youtube.com','m.youtube.com'} or p.port not in {None,443}:
        raise ValueError('Channel Watch currently supports public YouTube channel, handle and playlist URLs.')
    if p.path == '/playlist':
        value = parse_qs(p.query).get('list',[''])[0]
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,100}',value):
            raise ValueError('Use a valid YouTube playlist URL.')
        return 'https://www.youtube.com/playlist?list=' + value
    match = re.fullmatch(r'/(@[\w.-]{1,100}|channel/[A-Za-z0-9_-]{10,80}|c/[\w.-]{1,100}|user/[\w.-]{1,100})(?:/(?:videos|shorts|streams))?/?',p.path)
    if not match:
        raise ValueError('Paste a channel URL, @handle or playlist URL, rather than an individual video.')
    return 'https://www.youtube.com/' + match.group(1)


def normalized(info):
    identity = str(info.get('id',''))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',identity):
        return None
    url = info.get('webpage_url') or info.get('url') or ''
    if not url.startswith(('http://','https://')) and info.get('extractor_key') == 'Youtube':
        url = 'https://www.youtube.com/watch?v=' + identity
    try:
        media_url(url)
    except ValueError:
        return None
    thumbs = info.get('thumbnails') or []
    thumb = info.get('thumbnail') or (thumbs[-1].get('url','') if thumbs else '')
    return {'id':identity,'url':url[:8000],'title':str(info.get('title') or identity)[:400],
            'type':classify(info),'published':info.get('timestamp') or info.get('release_timestamp'),
            'upload_date':str(info.get('upload_date') or '')[:8],
            'duration':info.get('duration'),'thumbnail':thumb if str(thumb).startswith('https://') else '',
            'channel_id':str(info.get('channel_id') or ''),
            'availability':info.get('availability') or 'unknown'}


def probe(request):
    import yt_dlp
    try:
        from yt_dlp.globals import plugin_dirs
        plugin_dirs.value=[]
    except ImportError:
        pass
    watch = request.get('watch',False)
    url = youtube_source(request['url']) if watch else media_url(request['url'])
    limit = max(1,min(200,int(request.get('limit',100))))
    offset = max(0,min(10000,int(request.get('offset',0))))
    class Quiet:
        def debug(self,*args): pass
        def info(self,*args): pass
        def warning(self,*args): pass
        def error(self,*args): pass
    params = {'quiet':True,'no_warnings':True,'logger':Quiet(),'skip_download':True,
              'extract_flat':'in_playlist','playliststart':offset+1,'playlistend':offset+limit,
              'socket_timeout':10,'retries':0,'extractor_retries':0,'cachedir':False,
              'remote_components':[], 'noplaylist':not watch and not request.get('playlist',False)}
    params['js_runtimes']={name:{'path':shutil.which(name)} for name in ('deno','node') if shutil.which(name)}
    if request.get('cookies'):
        # Metadata inspection also receives a private disposable copy, never the owner file.
        params['cookiefile']=request['cookies']
    tab_results=[]
    with yt_dlp.YoutubeDL(params) as ydl:
        if watch and '/playlist?' not in url:
            for tab in ('videos','shorts','streams'):
                try:
                    tab_info=ydl.extract_info(url+'/'+tab,download=False)
                except yt_dlp.utils.DownloadError as error:
                    if 'does not have a' in str(error).lower() and 'tab' in str(error).lower():
                        continue
                    raise
                if isinstance(tab_info,dict): tab_results.append((tab,tab_info))
            info=tab_results[0][1] if tab_results else None
        else:
            info = ydl.extract_info(url,download=False)
    if not isinstance(info,dict):
        raise ValueError('The source returned no accessible metadata.')
    entries = []
    def flatten(value,tab='',ceiling=limit):
        if len(entries) >= ceiling or not isinstance(value,dict): return
        if value.get('entries') is not None:
            for entry in value['entries']:
                flatten(entry,tab,ceiling)
                if len(entries) >= ceiling: break
        else:
            item=normalized({**value,'_forge_tab':tab})
            if item: entries.append(item)
    if tab_results:
        for tab,result in tab_results: flatten(result,tab,len(entries)+limit)
        entries=list({item['id']:item for item in entries}.values())
        entries.sort(key=lambda item:item.get('published') or 0,reverse=True)
    else: flatten(info)
    source_id = str(info.get('id') if '/playlist?' in url else info.get('channel_id') or info.get('id') or '')
    if watch and not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',source_id):
        raise ValueError('The source did not provide a stable identity.')
    # Missing totals remain unknown. playlist_count describes the extractor's
    # selected source, not necessarily every video ever uploaded by the channel.
    source_thumb=info.get('thumbnail') or ((info.get('thumbnails') or [{}])[-1].get('url',''))
    count=info.get('playlist_count') if not tab_results else None
    formats=[]
    for f in (info.get('formats') or [])[:200]:
        formats.append({key:f.get(key) for key in ('format_id','ext','height','width','vcodec','acodec','filesize','format_note')})
    return {'platform':'youtube' if 'youtube' in (info.get('extractor_key') or '').lower() or watch else str(info.get('extractor_key') or 'generic'),
            'source_id':source_id,'url':url,'title':str(info.get('channel') or info.get('title') or source_id)[:400],
            'thumbnail':str(source_thumb) if str(source_thumb).startswith('https://') else '',
            'reported_count':count if isinstance(count,int) and count>=0 else None,
            'reported_count_scope':'Selected source, as reported by the extractor',
            'entries':entries,'formats':formats,'offset':offset,'limit':limit,'has_more':len(entries)>=limit,
            'checked':time.time()}


if __name__ == '__main__':
    try:
        body=json.loads(sys.stdin.read(16000))
        result={'ok':True,'result':probe(body)}
    except Exception as error:
        result={'ok':False,'error':diagnostic(error)}
    print(json.dumps(result,ensure_ascii=True,allow_nan=False))
