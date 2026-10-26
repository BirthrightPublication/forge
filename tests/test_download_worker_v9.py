"""Downloader measurements, recovery and real harness-owned HTTP media.

The multistream metadata is an explicit local fixture, not a YouTube response.
Transfers, range handling, cancellation, FFmpeg and final file probes are real.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from forge.core import ROOT
from forge.download_runtime import DownloadFailure, DownloadProgress, DownloadPublication, file_digest, prepare_cache, transfer_identity
from forge.models import Download
from forge import worker


def measured(**kwargs):
    events = []
    clock = [10.0]
    tracker = DownloadProgress(lambda value, message, **data: events.append({'progress': value, 'message': message, **data}),
                               tool=kwargs.get('tool', 'download_video'), playlist=kwargs.get('playlist', False), clock=lambda: clock[0])
    return tracker, events, clock


def test_real_byte_accounting_keeps_first_stream_below_complete_and_stage_changes_unthrottled():
    tracker, events, clock = measured()
    tracker.prepare({'id': 'fixture', 'requested_formats': [
        {'format_id': 'v', 'filesize': 100, 'vcodec': 'h264', 'acodec': 'none'},
        {'format_id': 'a', 'filesize': 50, 'vcodec': 'none', 'acodec': 'aac'}]})
    tracker.hook({'status': 'downloading', 'downloaded_bytes': 50, 'total_bytes': 100, 'speed': 25,
                  'info_dict': {'id': 'fixture', 'format_id': 'v', 'vcodec': 'h264', 'acodec': 'none'}})
    assert events[-1]['stage_progress'] == 50
    assert events[-1]['progress'] == pytest.approx(100 / 3)
    assert events[-1]['eta_seconds'] == 4
    tracker.hook({'status': 'finished', 'downloaded_bytes': 100, 'total_bytes': 100,
                  'info_dict': {'format_id': 'v', 'vcodec': 'h264', 'acodec': 'none'}})
    assert events[-1]['progress'] == pytest.approx(200 / 3)
    assert events[-1]['stream_index'] == 1 and events[-1]['stream_count'] == 2
    # No clock tick: a different stream must still emit immediately.
    tracker.hook({'status': 'downloading', 'downloaded_bytes': 25, 'total_bytes': 50, 'speed': 5,
                  'info_dict': {'format_id': 'a', 'vcodec': 'none', 'acodec': 'aac'}})
    assert events[-1]['stage'] == 'Downloading Audio' and events[-1]['downloaded_bytes'] == 125
    tracker.hook({'status': 'finished', 'downloaded_bytes': 50, 'total_bytes': 50,
                  'info_dict': {'format_id': 'a', 'vcodec': 'none', 'acodec': 'aac'}})
    assert events[-1]['progress'] < 100 and events[-1]['stage_progress'] == 100
    tracker.postprocess({'status': 'started', 'postprocessor': 'Merger'})
    assert events[-1]['stage'] == 'Merging Media' and not events[-1]['progress_measured']
    assert events[-1]['eta_seconds'] is None
    tracker.postprocess({'status': 'started', 'postprocessor': 'Metadata'})
    assert events[-1]['stage'] == 'Processing Metadata'


@pytest.mark.parametrize('total', [None, 0, -5, float('nan'), float('inf'), True])
def test_unknown_or_invalid_totals_are_not_promoted_from_estimates(total):
    tracker, events, _ = measured(tool='download_audio')
    tracker.hook({'status': 'downloading', 'downloaded_bytes': 50, 'total_bytes': total,
                  'total_bytes_estimate': 100, 'eta': 2, 'speed': 25})
    assert events[-1]['progress_measured'] is False and events[-1]['stage_progress'] is None
    assert events[-1]['progress'] == 0 and events[-1]['eta_seconds'] is None
    assert events[-1]['downloaded_bytes'] == 50 and events[-1]['total_bytes'] is None


def test_known_current_stream_does_not_invent_unknown_future_audio_length():
    tracker, events, _ = measured()
    tracker.prepare({'id': 'fixture', 'requested_formats': [{'format_id': 'v'}, {'format_id': 'a'}]})
    tracker.hook({'status': 'downloading', 'downloaded_bytes': 75, 'total_bytes': 100,
                  'info_dict': {'format_id': 'v', 'vcodec': 'h264', 'acodec': 'none'}})
    assert events[-1]['progress_scope'] == 'current_transfer' and events[-1]['stage_progress'] == 75
    assert not events[-1]['progress_measured'] and events[-1]['stream_count'] == 2


def test_legacy_playlist_does_not_claim_overall_completion_from_current_file():
    tracker, events, _ = measured(tool='download_audio', playlist=True)
    tracker.hook({'status': 'finished', 'downloaded_bytes': 100, 'total_bytes': 100, 'eta': 0})
    assert events[-1]['stage_progress'] == 100 and not events[-1]['progress_measured']
    assert events[-1]['progress'] == 0 and events[-1]['eta_seconds'] is None


def test_changed_request_gets_separate_cache_without_erasing_old_parts_or_credentials_record(tmp_path):
    root = tmp_path / 'cache'
    root.mkdir()
    legacy = root / 'video.mp4.part'
    legacy.write_bytes(b'original partial')
    options = Download(format='mp4', cookies='/private/cookies.txt').model_dump()
    first, identity = prepare_cache(root, 'https://example.test/video?secret=private', options, 'download_video')
    assert first == root and legacy.read_bytes() == b'original partial'
    assert json.loads((root / 'transfer.json').read_text())['legacy_parts_preserved'] is True
    second, other = prepare_cache(root, 'https://example.test/other?secret=private', options, 'download_video')
    assert second != first and second.is_relative_to(root) and other != identity
    assert legacy.read_bytes() == b'original partial'
    assert prepare_cache(root, 'https://example.test/other?secret=private', options, 'download_video')[0] == second
    record = (root / 'transfer.json').read_text()
    assert 'example.test' not in record and 'cookies.txt' not in record and 'private' not in record


def test_corrupt_cache_identity_never_adopts_unrecognized_fragments(tmp_path):
    (tmp_path / 'transfer.json').write_text('not json')
    (tmp_path / 'old.part').write_bytes(b'keep')
    selected, _ = prepare_cache(tmp_path, 'https://example.test/new', Download().model_dump(), 'download_audio')
    assert selected != tmp_path and not (selected / 'old.part').exists()
    assert (tmp_path / 'old.part').read_bytes() == b'keep'


def test_publication_intent_recovers_crash_after_link_without_duplicate_output(tmp_path, monkeypatch):
    import forge.download_runtime as runtime
    cache, output, stage = tmp_path / 'cache', tmp_path / 'output', tmp_path / 'stage'
    cache.mkdir(); output.mkdir(); stage.mkdir()
    source = stage / 'download.mp4'
    source.write_bytes(b'verified-content')
    journal = DownloadPublication(cache, output, 'a' * 64)
    journal.prepare([source], [])
    if sys.platform == 'win32':
        original = runtime.os.rename
        def stopped(before, after):
            original(before, after)
            raise RuntimeError('Simulated stop after filesystem commit')
        monkeypatch.setattr(runtime.os, 'rename', stopped)
    else:
        original = runtime.os.link
        def stopped(before, after):
            original(before, after)
            raise RuntimeError('Simulated stop after filesystem commit')
        monkeypatch.setattr(runtime.os, 'link', stopped)
    with pytest.raises(RuntimeError, match='Simulated stop'):
        journal.publish(source)
    recovered = DownloadPublication(cache, output, 'a' * 64).recovered()
    assert len(recovered) == 1 and recovered[0]['sha256'] == hashlib.sha256(b'verified-content').hexdigest()
    assert [file.name for file in output.iterdir()] == ['download.mp4']


def test_publication_preserves_collision_and_rejects_changed_or_outside_recovery(tmp_path):
    cache, output, stage = tmp_path / 'cache', tmp_path / 'output', tmp_path / 'stage'
    cache.mkdir(); output.mkdir(); stage.mkdir()
    original = output / 'download.mp4'
    original.write_bytes(b'User original')
    source = stage / 'download.mp4'
    source.write_bytes(b'new verified file')
    journal = DownloadPublication(cache, output, 'b' * 64)
    journal.prepare([source], [])
    published = journal.publish(source)
    assert Path(published['path']).name == 'download (1).mp4' and original.read_bytes() == b'User original'
    Path(published['path']).write_bytes(b'User changed finished file')
    assert journal.recovered() == []
    assert journal.verified({'final_name': '../outside.mp4'}) is None


@pytest.fixture(scope='module')
def real_media(tmp_path_factory):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Real FFmpeg/FFprobe are required')
    pytest.importorskip('yt_dlp')
    root = tmp_path_factory.mktemp('v09-local-media')
    media = root / 'synthetic.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=25:d=8',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=8', '-c:v', 'libx264',
                    '-preset', 'ultrafast', '-crf', '18', '-c:a', 'aac', '-shortest', str(media)], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(media), '-map', '0:v:0', '-c', 'copy', '-an', str(root / 'video.mp4')], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(media), '-map', '0:a:0', '-c', 'copy', '-vn', str(root / 'audio.m4a')], check=True)
    return root


@contextlib.contextmanager
def http_media(root, *, ranges=True, slow=False):
    requests = []
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            path = Path(self.translate_path(self.path))
            if not path.is_file():
                self.send_error(404)
                return
            content = path.read_bytes()
            requested = self.headers.get('Range', '')
            requests.append({'path': self.path, 'range': requested})
            start = int(requested[6:].split('-')[0]) if ranges and requested.startswith('bytes=') else 0
            self.send_response(206 if start else 200)
            self.send_header('Content-Type', 'audio/mp4' if path.suffix == '.m4a' else 'video/mp4')
            self.send_header('Content-Length', str(len(content) - start))
            if start:
                self.send_header('Content-Range', f'bytes {start}-{len(content)-1}/{len(content)}')
            if ranges:
                self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            try:
                for offset in range(start, len(content), 8192):
                    self.wfile.write(content[offset:offset + 8192])
                    self.wfile.flush()
                    if slow:
                        time.sleep(.015)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(root)))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


@pytest.mark.parametrize('ranges', [True, False])
def test_real_partial_transfer_continues_or_safely_restarts_without_range_support(tmp_path, real_media, monkeypatch, ranges):
    stage, cache = tmp_path / 'stage', tmp_path / 'cache'
    stage.mkdir(); cache.mkdir()
    original = (real_media / 'synthetic.mp4').read_bytes()
    (cache / 'Fixture [synthetic].mp4.part').write_bytes(original[:4096])
    events = []
    monkeypatch.setattr(worker, 'emit', lambda kind, **data: events.append({'kind': kind, **data}))
    options = Download(format='mp4', embed_metadata=False, filename_template='Fixture [%(id)s].%(ext)s').model_dump()
    with http_media(real_media, ranges=ranges) as (url, requests):
        files, warnings = worker.download(url + '/synthetic.mp4', stage, options, {}, 'download_video', resume_dir=cache)
    assert len(files) == 1 and files[0].read_bytes() == original
    assert any(request['range'] == 'bytes=4096-' for request in requests)
    assert worker.probe(files[0], {})['streams']
    assert any(event.get('stage') == 'Verifying Output' for event in events)
    assert not any(event.get('progress', 0) == 100 for event in events)
    assert (real_media / 'synthetic.mp4').read_bytes() == original


def test_real_separate_streams_and_ffmpeg_merge_report_actual_stages(tmp_path, real_media, monkeypatch):
    import yt_dlp
    original_ydl = yt_dlp.YoutubeDL
    events = []
    monkeypatch.setattr(worker, 'emit', lambda kind, **data: events.append({'kind': kind, **data}))
    with http_media(real_media) as (url, requests):
        class FixtureMetadata(original_ydl):
            def extract_info(self, supplied_url, download=True, **kwargs):
                return self.process_ie_result({'id': 'local-streams', 'title': 'Local fixture', 'extractor': 'Fixture',
                    'extractor_key': 'Fixture', 'webpage_url': url + '/fixture', 'formats': [
                    {'format_id': 'v', 'url': url + '/video.mp4', 'ext': 'mp4', 'vcodec': 'h264', 'acodec': 'none',
                     'width': 320, 'height': 180, 'filesize': (real_media / 'video.mp4').stat().st_size},
                    {'format_id': 'a', 'url': url + '/audio.m4a', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'aac',
                     'filesize': (real_media / 'audio.m4a').stat().st_size}]}, download=download)
        monkeypatch.setattr(yt_dlp, 'YoutubeDL', FixtureMetadata)
        options = Download(format='mp4', embed_metadata=True).model_dump()
        stage = tmp_path / 'stage'; stage.mkdir()
        files, _ = worker.download(url + '/fixture', stage, options, {}, 'download_video')
    assert len(files) == 1
    assert {'video', 'audio'} <= {stream['codec_type'] for stream in worker.probe(files[0], {})['streams']}
    stages = [event.get('stage') for event in events]
    assert 'Downloading Video' in stages and 'Downloading Audio' in stages and 'Merging Media' in stages
    assert 'Processing Metadata' in stages and stages[-1] == 'Verifying Output'
    video_events = [event for event in events if event.get('transfer_stage') == 'Downloading Video']
    assert video_events and all(event['progress'] < 100 for event in video_events)
    assert any(event.get('stream_count') == 2 and event['progress_measured'] for event in events)
    assert any(request['path'] == '/video.mp4' for request in requests) and any(request['path'] == '/audio.m4a' for request in requests)


def test_selected_playlist_video_retains_original_index_without_extracting_parent_playlist(tmp_path, real_media):
    options = Download(format='mp4', embed_metadata=False, playlist=False, filename_style='playlist_index').model_dump()
    with http_media(real_media) as (url, requests):
        files, _ = worker.download(url + '/synthetic.mp4', tmp_path, options, {}, 'download_video', playlist_index=7)
    assert len(files) == 1 and files[0].name.startswith('007 - ')
    assert '[synthetic]' in files[0].name
    assert files[0].read_bytes() == (real_media / 'synthetic.mp4').read_bytes()
    assert all(request['path'] == '/synthetic.mp4' for request in requests)


@pytest.mark.parametrize('value', [True, 0, -1, 1_000_001, '2', 1.5])
def test_invalid_server_playlist_index_is_rejected_before_transfer(tmp_path, value):
    with pytest.raises(ValueError, match='original playlist index'):
        worker.download('https://example.test/video', tmp_path, Download().model_dump(), {}, 'download_audio', playlist_index=value)
    assert not list(tmp_path.iterdir())


def payload(root, url, *, job='first', previous=''):
    return {'tool': 'download_video', 'options': Download(format='mp4', embed_metadata=False,
            filename_template='Fixture [%(id)s].%(ext)s').model_dump(), 'settings': {},
            'destination': str(root / 'output'), 'data': str(root / 'data'), 'job_id': job,
            'resume_key': 'same-planned-media', 'resume_from_job': previous, 'url': url}


def test_actual_abrupt_worker_stop_retains_partial_and_new_attempt_resumes_once(tmp_path, real_media):
    with http_media(real_media, slow=True) as (url, requests):
        first = payload(tmp_path, url + '/synthetic.mp4')
        process = subprocess.Popen([sys.executable, '-m', 'forge.worker'], cwd=ROOT, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        process.stdin.write(json.dumps(first)); process.stdin.close()
        incoming = queue.Queue()
        def read():
            for line in process.stdout:
                with contextlib.suppress(ValueError):
                    incoming.put(json.loads(line))
        reader = threading.Thread(target=read, daemon=True); reader.start()
        deadline = time.monotonic() + 25
        try:
            while True:
                event = incoming.get(timeout=max(.1, deadline - time.monotonic()))
                if event.get('downloaded_bytes', 0) and event['downloaded_bytes'] >= 65536:
                    break
                assert time.monotonic() < deadline
            process.kill(); process.wait(timeout=10)
            partials = list((tmp_path / 'data' / 'download-cache').rglob('*.part'))
            assert partials and any(0 < path.stat().st_size < (real_media / 'synthetic.mp4').stat().st_size for path in partials)
            second = payload(tmp_path, url + '/synthetic.mp4', job='second', previous='first')
            worker.run(second)
            report = json.loads((tmp_path / 'data' / 'work' / 'second' / 'result.json').read_text())
            assert report['complete'] and len(report['outputs']) == 1
            result = report['outputs'][0]
            assert Path(result['path']).read_bytes() == (real_media / 'synthetic.mp4').read_bytes()
            assert result['sha256'] == file_digest(Path(result['path']))
            assert len(list((tmp_path / 'output').glob('*.mp4'))) == 1
            assert not (tmp_path / 'output' / '.forge-first.work').exists()
            assert any(request['range'].startswith('bytes=') and request['range'] != 'bytes=0-' for request in requests)
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=10)
            reader.join(timeout=2)
            process.stdout.close(); process.stderr.close()


def test_already_published_download_is_verified_and_reused_without_network(tmp_path, real_media, monkeypatch):
    selected = payload(tmp_path, 'https://example.test/already-downloaded')
    destination = Path(selected['destination']); destination.mkdir()
    cache = Path(selected['data']) / 'download-cache' / hashlib.sha256(selected['resume_key'].encode()).hexdigest()
    cache, identity = prepare_cache(cache, selected['url'], selected['options'], selected['tool'])
    stage = tmp_path / 'old-stage'; stage.mkdir()
    source = stage / 'done.mp4'; shutil.copyfile(real_media / 'synthetic.mp4', source)
    publication = DownloadPublication(cache, destination, identity)
    publication.prepare([source], [])
    original = publication.publish(source)
    monkeypatch.setattr(worker, 'download', lambda *args, **kwargs: pytest.fail('A complete verified output must not be downloaded twice'))
    worker.run(selected)
    result = json.loads((Path(selected['data']) / 'work' / selected['job_id'] / 'result.json').read_text())
    assert result['complete'] and result['outputs'][0]['path'] == original['path']
    assert len(list(destination.glob('*.mp4'))) == 1


def test_partial_legacy_playlist_is_failed_without_losing_verified_files_or_duplicating_retry(tmp_path, real_media, monkeypatch):
    selected = payload(tmp_path, 'https://example.test/playlist')
    selected['options']['playlist'] = True
    calls = []
    def stopped(url, stage, *args, **kwargs):
        calls.append('partial')
        complete = stage / 'one.mp4'
        shutil.copyfile(real_media / 'synthetic.mp4', complete)
        raise DownloadFailure({'code': 'authentication', 'retryable': False, 'message': 'Access needs review.'},
                              partial_files=[complete], warnings=['One media item completed before access stopped.'])
    monkeypatch.setattr(worker, 'download', stopped)
    with pytest.raises(DownloadFailure):
        worker.run(selected)
    first = json.loads((Path(selected['data']) / 'work' / 'first' / 'result.json').read_text())
    assert first['complete'] is False and len(first['outputs']) == 1
    original = first['outputs'][0]
    assert Path(original['path']).is_file() and original['sha256'] == file_digest(Path(original['path']))
    def completed(url, stage, *args, **kwargs):
        calls.append('complete')
        one, two = stage / 'one.mp4', stage / 'two.mp4'
        shutil.copyfile(real_media / 'synthetic.mp4', one)
        shutil.copyfile(real_media / 'video.mp4', two)
        return [one, two], []
    monkeypatch.setattr(worker, 'download', completed)
    selected = {**selected, 'job_id': 'second', 'resume_from_job': 'first'}
    worker.run(selected)
    result = json.loads((Path(selected['data']) / 'work' / 'second' / 'result.json').read_text())
    assert result['complete'] and len(result['outputs']) == 2
    assert result['outputs'][0]['path'] == original['path']
    assert len(list(Path(selected['destination']).glob('*.mp4'))) == 2
    monkeypatch.setattr(worker, 'download', lambda *args, **kwargs: pytest.fail('Completed batch must reuse verified output'))
    worker.run({**selected, 'job_id': 'third', 'resume_from_job': 'second'})
    assert calls == ['partial', 'complete']


def test_legacy_thumbnail_setting_cannot_export_images_and_authentication_is_not_retryable(tmp_path, monkeypatch):
    seen = {}
    class Downloader:
        def __init__(self, options):
            seen.update(options)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, *args, **kwargs):
            (tmp_path / 'media.mp3').write_bytes(b'fixture audio')
            (tmp_path / 'image.jpg').write_bytes(b'fixture image, deliberately not exported')
    monkeypatch.setitem(sys.modules, 'yt_dlp', SimpleNamespace(YoutubeDL=Downloader))
    monkeypatch.setattr(worker, 'executable', lambda *args: '/fixture/ffmpeg')
    monkeypatch.setattr(worker, 'probe', lambda *args: {'streams': [{'codec_type': 'audio'}]})
    files, _ = worker.download('https://example.test/media', tmp_path, Download(thumbnail=True).model_dump(), {}, 'download_audio')
    assert [path.suffix for path in files] == ['.mp3'] and seen['writethumbnail'] is False
    class Authentication(Downloader):
        def extract_info(self, *args, **kwargs):
            raise RuntimeError('Sign in with cookies https://private.test/?token=secret')
    monkeypatch.setitem(sys.modules, 'yt_dlp', SimpleNamespace(YoutubeDL=Authentication))
    with pytest.raises(DownloadFailure) as error:
        worker.download('https://example.test/media', tmp_path, Download().model_dump(), {}, 'download_audio')
    assert error.value.code == 'authentication' and error.value.retryable is False
    assert 'secret' not in str(error.value) and 'private.test' not in str(error.value)


@pytest.mark.parametrize('error', ['HTTP Error 401: Unauthorized', 'HTTP 401', 'Unauthorized source access'])
def test_explicit_unauthorized_responses_require_review_without_retry_loop(error):
    from forge.download_support import diagnostic
    result = diagnostic(error + ' https://private.test/?token=secret')
    assert result['code'] == 'authentication' and result['retryable'] is False
    assert 'secret' not in result['message'] and 'private.test' not in result['message']
    assert diagnostic('HTTP Error 403: Forbidden')['code'] != 'authentication'
