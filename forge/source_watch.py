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
import urllib.request
from urllib.parse import urlsplit

from .core import ROOT, no_window
from .download_support import diagnostic
from .source_probe import youtube_source


class SourceError(ValueError):
    def __init__(self, details):
        self.details=details
        super().__init__(details['message'])


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
    """Bounded public YouTube thumbnails; never forward cookies or arbitrary URLs."""
    def approved(value):
        parts=urlsplit(value)
        return (parts.scheme=='https' and parts.hostname in {'yt3.googleusercontent.com','yt3.ggpht.com','i.ytimg.com'}
                and parts.port in {None,443} and not parts.username and not parts.password)
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            if not approved(newurl): raise ValueError('Unsupported thumbnail redirect.')
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    try:
        if not approved(url): return ''
        with urllib.request.build_opener(Redirect()).open(url,timeout=5) as response:
            raw=response.read(1024*1024+1)
        if len(raw)>1024*1024: return ''
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as picture:
            if picture.width*picture.height>4_000_000: return ''
            picture.thumbnail((160,160));out=io.BytesIO();picture.convert('RGB').save(out,format='PNG')
        return 'data:image/png;base64,'+base64.b64encode(out.getvalue()).decode('ascii')
    except Exception:
        return ''  # An unavailable thumbnail must not discard source discovery.
