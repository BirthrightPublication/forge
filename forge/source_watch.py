"""App-independent discovery capability boundary. The FORGE scheduler owns persistence/jobs."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import base64
import io
import hashlib
import os
import threading
import time
import uuid
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from .core import ROOT, no_window
from .download_support import diagnostic
from .source_probe import youtube_source


class SourceError(ValueError):
    def __init__(self, details):
        self.details=details
        super().__init__(details['message'])


def approved_thumbnail_url(value):
    """Only public platform preview hosts; no credentials or arbitrary fetches."""
    try:
        parts=urlsplit(value)
        return (isinstance(value,str) and len(value)<=8000 and parts.scheme=='https'
                and parts.hostname in {'yt3.googleusercontent.com','yt3.ggpht.com','i.ytimg.com','i9.ytimg.com','img.youtube.com'}
                and parts.port in {None,443} and not parts.username and not parts.password)
    except (ValueError,TypeError):
        return False


def thumbnail_bytes(url):
    """Fetch and normalize a bounded preview without owner cookies or headers."""
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            if not approved_thumbnail_url(newurl): raise ValueError('Unsupported thumbnail redirect.')
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    if not approved_thumbnail_url(url): return b''
    request=urllib.request.Request(url,headers={'User-Agent':'FORGE local thumbnail preview'})
    with urllib.request.build_opener(Redirect()).open(request,timeout=5) as response:
        length=response.headers.get('Content-Length','') if getattr(response,'headers',None) else ''
        if length.isdigit() and int(length)>1024*1024: return b''
        raw=response.read(1024*1024+1)
    if len(raw)>1024*1024: return b''
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as picture:
        if picture.width*picture.height>4_000_000: return b''
        picture.thumbnail((320,180));out=io.BytesIO();picture.convert('RGB').save(out,format='PNG')
    return out.getvalue()


class ThumbnailCache:
    """Small internal preview cache, independent of downloaded media archives."""
    def __init__(self,data,*,max_files=400,max_bytes=32*1024*1024,ttl=86400):
        self.data=Path(data).resolve()
        self.root=self.data/'thumbnail-cache'
        self.max_files,self.max_bytes,self.ttl=max_files,max_bytes,ttl
        self.lock=threading.RLock()
        self.fetches=threading.BoundedSemaphore(2)
        self.failed_until={}

    def get(self,url):
        if not approved_thumbnail_url(url): return b''
        parts=urlsplit(url);url=urlunsplit(parts._replace(fragment=''))
        key=hashlib.sha256(url.encode()).hexdigest()+'.png'
        stale=b''
        try:
            self.root.mkdir(parents=True,exist_ok=True)
            if self.root.is_symlink() or not self.root.resolve().is_relative_to(self.data): return b''
            path=self.root/key
            if path.is_file() and not path.is_symlink() and path.stat().st_size<=1024*1024:
                stale=path.read_bytes()
                if not stale.startswith(b'\x89PNG\r\n\x1a\n'): stale=b''
                if stale and time.time()-path.stat().st_mtime<self.ttl: return stale
            with self.lock:
                if self.failed_until.get(key,0)>time.time(): return stale
            if not self.fetches.acquire(blocking=False): return stale
            try:
                raw=thumbnail_bytes(url)
                if not raw:
                    with self.lock:
                        self.failed_until[key]=time.time()+300
                        if len(self.failed_until)>400: self.failed_until.pop(min(self.failed_until,key=self.failed_until.get),None)
                    return stale
                with self.lock:
                    self.failed_until.pop(key,None)
                    temporary=self.root/('.preview-'+uuid.uuid4().hex)
                    try:
                        with temporary.open('xb') as stream: stream.write(raw)
                        os.replace(temporary,path)
                    finally: temporary.unlink(missing_ok=True)
                    entries=sorted((file for file in self.root.glob('*.png') if file.is_file() and not file.is_symlink()),key=lambda file:file.stat().st_mtime)
                    size=sum(file.stat().st_size for file in entries)
                    while entries and (len(entries)>self.max_files or size>self.max_bytes):
                        old=entries.pop(0);size-=old.stat().st_size;old.unlink(missing_ok=True)
                return raw
            finally: self.fetches.release()
        except Exception:
            with self.lock:
                self.failed_until[key]=time.time()+300
                if len(self.failed_until)>400: self.failed_until.pop(min(self.failed_until,key=self.failed_until.get),None)
            return stale


class YouTubeAdapter:
    platform='youtube'
    capabilities={'discovery':True,'public_counts':'when_reported','pagination':True,
                  'authentication':'local_cookie_file','live_status':'when_reported',
                  'content_types':['video','short','live','replay','upcoming','premiere','clip','podcast','unclassified'],
                  'downloads':True,'polling':True,'push_notifications':False}

    def inspect(self,url,*,limit=100,offset=0,cookies='',watch=True,playlist=False):
        if watch: url=youtube_source(url)
        with tempfile.TemporaryDirectory(prefix='forge-source-probe-') as tmp:
            cookie_copy=''
            if cookies:
                source=Path(cookies).expanduser()
                if not source.is_file() or source.stat().st_size>4_000_000:
                    raise ValueError('Select an existing local cookies file smaller than 4 MB.')
                cookie_copy=str(Path(tmp)/'cookies.txt')
                shutil.copyfile(source,cookie_copy)
                Path(cookie_copy).chmod(0o600)
            request={'url':url,'watch':watch,'playlist':playlist,'limit':limit,'offset':offset,'cookies':cookie_copy}
            try:
                run=subprocess.run([sys.executable,'-m','forge.source_probe'],cwd=ROOT,
                                   input=json.dumps(request),capture_output=True,text=True,
                                   encoding='utf-8',timeout=60,**no_window())
                if run.returncode or len(run.stdout)>4_000_000:
                    raise SourceError(diagnostic('extractor process failed'))
                value=json.loads(run.stdout)
                if not value.get('ok'):
                    raise SourceError(value.get('error') or diagnostic('extractor process failed'))
                result=value['result']
                if watch: result['thumbnail_data']=thumbnail_data(result.get('thumbnail',''))
                return result
            except (subprocess.TimeoutExpired,OSError,ValueError) as error:
                if isinstance(error,SourceError): raise
                raise SourceError(diagnostic('network timeout' if isinstance(error,subprocess.TimeoutExpired) else error)) from None


ADAPTERS={'youtube':YouTubeAdapter}


def thumbnail_data(url):
    """Legacy small avatar representation, sharing the validated preview fetch."""
    try:
        raw=thumbnail_bytes(url)
        if not raw: return ''
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as picture:
            picture.thumbnail((160,160));out=io.BytesIO();picture.convert('RGB').save(out,format='PNG')
        return 'data:image/png;base64,'+base64.b64encode(out.getvalue()).decode('ascii')
    except Exception:
        return ''  # An unavailable thumbnail must not discard source discovery.
