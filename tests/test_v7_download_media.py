"""Real yt-dlp/FFmpeg against a harness-owned HTTP source, never a third-party video."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from forge.models import Download
from forge.worker import download, probe, publish

pytestmark=pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg required')


@pytest.mark.parametrize('tool,format',[('download_video','mp4'),('download_video','mkv'),('download_audio','mp3'),('download_audio','flac')])
def test_real_download_formats_resume_template_and_collision(tmp_path,tool,format):
    pytest.importorskip('yt_dlp')
    source=tmp_path/'source';source.mkdir()
    media=source/'synthetic.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=gold:s=160x120:d=1',
                    '-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264','-c:a','aac','-shortest',str(media)],check=True)
    original=media.read_bytes()
    ranges=[]
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            value=self.headers.get('Range','')
            if value.startswith('bytes='):
                ranges.append(value);start=int(value[6:].split('-')[0]);content=media.read_bytes()
                self.send_response(206);self.send_header('Content-Type','video/mp4')
                self.send_header('Content-Length',str(len(content)-start))
                self.send_header('Content-Range',f'bytes {start}-{len(content)-1}/{len(content)}')
                self.send_header('Accept-Ranges','bytes');self.end_headers();self.wfile.write(content[start:]);return
            super().do_GET()
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Quiet,directory=str(source)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    stage=tmp_path/'stage';stage.mkdir();cache=tmp_path/'private-parts';cache.mkdir()
    # Seed a real partial file; the fixture server honors byte ranges.
    part=cache/('Fixture [synthetic].'+('mp4' if tool=='download_video' else 'mp4')+'.part')
    part.write_bytes(original[:500])
    try:
        options=Download(format=format,filename_template='Fixture [%(id)s].%(ext)s').model_dump()
        files,warnings=download(f'http://127.0.0.1:{server.server_port}/synthetic.mp4',stage,options,{},tool,resume_dir=cache)
        assert len(files)==1 and files[0].suffix=='.'+format
        assert probe(files[0],{})['streams'] and files[0].name.startswith('Fixture [synthetic]')
        output=tmp_path/'existing';output.mkdir();destination=output/files[0].name;destination.write_bytes(b'keep existing file')
        actual=publish(files[0],destination)
        assert actual!=destination and actual.is_file() and destination.read_bytes()==b'keep existing file'
        assert media.read_bytes()==original
        assert 'bytes=500-' in ranges, ranges
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
