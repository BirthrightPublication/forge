"""V0.9 real-browser acceptance against a disposable, authenticated FORGE service.

YouTube discovery and thumbnail responses are explicit synthetic fixtures. The
browser drives real planning, SQLite persistence and scheduler APIs. A bounded
local HTTP WAV transfer exercises the real yt-dlp/FFmpeg worker; this is not live
YouTube, authentication-challenge, native Windows or GPU acceptance.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
from pathlib import Path
import re
import secrets
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import parse_qs, urlsplit
import wave

from PIL import Image, ImageDraw
import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from forge import __version__
from forge.app import create_app
from forge.download_studio import WatchRequest


def fixture_png(index=0):
    """Original geometric artwork, not a downloaded/copyrighted thumbnail."""
    image = Image.new('RGB', (320, 180), (24 + index * 8, 31 + index * 5, 39 + index * 5))
    draw = ImageDraw.Draw(image)
    draw.ellipse((214, 16, 270, 72), fill=(192, 160, 87))
    draw.polygon([(0, 160), (86, 58), (151, 139), (216, 79), (320, 180)], fill=(58, 81, 78))
    draw.polygon([(0, 180), (119, 101), (201, 168), (263, 128), (320, 180)], fill=(32, 49, 45))
    buffer = io.BytesIO(); image.save(buffer, format='PNG')
    return buffer.getvalue()


class SyntheticYouTube:
    platform = 'youtube'
    capabilities = {'discovery': True, 'pagination': True, 'downloads': True,
                    'public_counts': 'when_reported', 'push_notifications': False}

    def __init__(self):
        names = ['Archive Observatory', 'Science Workshop', 'Ancient Maps',
                 'Technology Notes', 'World Journeys', 'Creative Fieldbook']
        self.channels = []
        self.calls = []
        self.failures = {}
        for index, name in enumerate(names):
            source = {'id': 'UC_SYNTHETIC_V09_' + str(index), 'handle': '@synthetic-v09-' + str(index),
                      'title': 'Synthetic ' + name, 'items': []}
            source['items'] = [self.item(f'v9{index}{item:08d}', source, item) for item in range(3)]
            self.channels.append(source)
        self.playlist = [self.item(f'v9list{index:05d}', self.channels[0], index)
                         for index in range(65)]

    @staticmethod
    def item(identity, source, position=0, **changes):
        return {'id': identity, 'url': 'https://www.youtube.com/watch?v=' + identity,
                'title': 'Synthetic upload ' + identity, 'type': 'video',
                'published': 1792700000 + position * 600,
                'upload_date': '20261022', 'duration': None if position == 0 else 35 + position * 17,
                'view_count': None if position == 0 else 200 + position * 301,
                'thumbnail': 'https://i.ytimg.com/vi/' + identity + '/hqdefault.jpg',
                'channel_id': source['id'], 'channel': source['title'], 'availability': 'public',
                **changes}

    def inspect(self, url, **kwargs):
        self.calls.append({'watch': kwargs.get('watch', True), 'playlist': kwargs.get('playlist', False),
                           'limit': kwargs.get('limit'), 'offset': kwargs.get('offset', 0)})
        offset = max(0, int(kwargs.get('offset', 0)))
        limit = min(200, max(1, int(kwargs.get('limit', 100))))
        if not kwargs.get('watch', True):
            if kwargs.get('playlist') or '/playlist?' in url:
                entries = self.playlist[offset:offset + limit]
                return {'platform': 'youtube', 'source_id': 'PL_SYNTHETIC_V09',
                        'url': 'https://www.youtube.com/playlist?list=PL_SYNTHETIC_V09',
                        'title': 'Synthetic selected playlist', 'entries': entries,
                        'formats': [{'height': 720}], 'reported_count': len(self.playlist),
                        'has_more': offset + len(entries) < len(self.playlist), 'offset': offset, 'limit': limit}
            identity = parse_qs(urlsplit(url).query).get('v', ['v9manual001'])[0]
            entry = self.item(identity, self.channels[0], 1)
            return {'platform': 'youtube', 'source_id': identity, 'title': entry['title'],
                    'url': entry['url'], 'entries': [entry], 'formats': [{'height': 720}],
                    'reported_count': 1, 'has_more': False, 'offset': 0, 'limit': limit}
        source = next((row for row in self.channels if row['id'] in url or row['handle'] in url), self.channels[0])
        if source['id'] in self.failures:
            raise self.failures[source['id']]
        entries = source['items'][offset:offset + limit]
        return {'platform': 'youtube', 'source_id': source['id'],
                'url': 'https://www.youtube.com/channel/' + source['id'],
                'title': source['title'], 'handle': source['handle'],
                'thumbnail': '', 'thumbnail_data': 'data:image/png;base64,' + base64.b64encode(fixture_png(self.channels.index(source))).decode(),
                'reported_count': None if source is self.channels[0] else len(source['items']),
                'entries': entries, 'formats': [], 'has_more': offset + len(entries) < len(source['items']),
                'offset': offset, 'limit': limit, 'checked': time.time()}


def make_wave(path):
    rate, frames = 16000, 16000 * 100
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, rate, frames, 'NONE', 'not compressed'))
        block = b''.join(struct.pack('<h', round(7000 * math.sin(2 * math.pi * 440 * index / rate)))
                         for index in range(rate))
        for _ in range(100):
            audio.writeframesraw(block)


def local_media_server(path):
    data = path.read_bytes()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_HEAD(self):
            self.respond(head=True)

        def do_GET(self):
            self.respond()

        def respond(self, head=False):
            request_path = urlsplit(self.path).path
            if request_path not in {'/synthetic-source.wav', '/synthetic-unknown.wav'}:
                self.send_error(404); return
            known_length = request_path == '/synthetic-source.wav'
            start, end = 0, len(data) - 1
            match = re.fullmatch(r'bytes=(\d+)-(\d*)', self.headers.get('Range', '')) if known_length else None
            if match:
                start = int(match[1]); end = min(end, int(match[2]) if match[2] else end)
                if start >= len(data):
                    self.send_response(416); self.send_header('Content-Range', f'bytes */{len(data)}'); self.end_headers(); return
            self.send_response(206 if match else 200)
            self.send_header('Content-Type', 'audio/wav')
            if known_length:
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Length', str(end - start + 1))
            if match:
                self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
            self.end_headers()
            if not head:
                try:
                    for offset in range(start, end + 1, 32768):
                        self.wfile.write(data[offset:min(end + 1, offset + 32768)]); self.wfile.flush()
                        time.sleep(.04)
                except (BrokenPipeError, ConnectionResetError):
                    pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    return server, f'http://127.0.0.1:{server.server_port}/synthetic-source.wav'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chromium', default=shutil.which('chromium'))
    args = parser.parse_args(argv)
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    checks, errors, page = [], [], None
    report = {'version': __version__, 'status': 'failed', 'checks': checks,
              'scope': {'real_browser': True, 'real_authenticated_api': True, 'real_sqlite': True,
                        'youtube_metadata': 'explicit synthetic fixtures',
                        'thumbnail_transport': 'generated PNG fixtures', 'live_youtube_tested': False,
                        'native_windows_execution': sys.platform == 'win32', 'physical_gpu_tested': False}}

    def check(label, condition=True):
        if not condition and page is not None:
            try:
                page.screenshot(path=str(output / 'FAILED-V09.png'), full_page=True)
            except Exception:
                pass
        assert condition, label
        checks.append(label); print('PASS', label, flush=True)

    with tempfile.TemporaryDirectory(prefix='forge-v9-browser-') as temporary:
        folder = Path(temporary); exports = folder / 'exports'; exports.mkdir()
        source_wave = folder / 'synthetic-source.wav'; make_wave(source_wave)
        source_hash = hashlib.sha256(source_wave.read_bytes()).hexdigest()
        media_server, media_url = local_media_server(source_wave)
        listener = socket.socket(); listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
        token = secrets.token_urlsafe(32)
        app = create_app(folder / 'data', token, port, workers=True)
        settings = app.state.store.settings()
        settings.update(automatic_update_checks=False, daily_update_check=False, automatic_dependency_repairs=False,
                        queue_paused=True, workers=1, cpu_workers=1, download_workers=1, disk_workers=1,
                        default_output=str(exports))
        app.state.store.set('settings', settings); app.state.store.set('queue_pause_explicit', True)
        adapter = SyntheticYouTube(); studio = app.state.download_studio; studio.adapter = adapter
        studio.destinations.remember(exports, name='Synthetic acceptance exports', pinned=True, default=True)
        source_ids = []
        for index, source in enumerate(adapter.channels):
            studio.register(WatchRequest(url='https://www.youtube.com/' + source['handle'],
                            config={'initial': 'review', 'instant_download': False,
                                    'priority': ['medium', 'ultra', 'extra', 'high', 'low', 'minimal'][index],
                                    'enabled': index != 5, 'options': {'format': 'wav'}}))
            source_ids.append('youtube:' + source['id'])
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True); thread.start()
        page = None
        try:
            for _ in range(150):
                if server.started: break
                time.sleep(.1)
            assert server.started, 'Disposable service did not start.'
            planner = app.state.manager.download_planner
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.chromium, headless=True, args=['--no-sandbox'])
                context = browser.new_context(viewport={'width': 1600, 'height': 1100}, timezone_id='America/Los_Angeles')
                page = context.new_page(); page.set_default_timeout(15000)
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/api/downloads/thumbnail?*', lambda route: route.fulfill(status=200, content_type='image/png', body=fixture_png()))
                page.goto(f'http://127.0.0.1:{port}/#token={token}')
                page.wait_for_selector('#dl-urls')
                check('Media Downloads is the working landing page', page.locator('h1').inner_text() == 'Media Downloads')
                check('Queue occupies the workspace slot and Settings is a single utility item', page.locator('#workspace-nav [data-view="queue"]').count() == 1 and page.locator('#utility-nav [data-view="settings"]').count() == 1 and page.locator('#sidebar [data-view="settings"]').count() == 1)
                check('Removed Overview is not reintroduced', page.locator('#sidebar [data-view="overview"]').count() == 0)
                check('All V0.8 speech tools remain in navigation', page.locator('#sidebar [data-view="tts"],#sidebar [data-view="audio_effects"]').count() == 2)
                page.locator('#workspace-nav [data-view="queue"]').hover()
                page.wait_for_selector('#navigation-tooltip:not([hidden])')
                check('Compact Queue navigation has its correct accessible tooltip', page.locator('#navigation-tooltip').inner_text() == 'Processing Queue')
                check('Local Engine stays at the absolute sidebar bottom', page.locator('.sidebar-foot').evaluate('el=>el.lastElementChild.classList.contains("local-mark")'))
                page.locator('[data-download-tab="watch"]').click(); page.wait_for_selector('.dl-channel-card')
                check('Six persisted fixture channels render as structured cards', page.locator('.dl-channel-card').count() == 6)
                check('Cards show separate bordered metric containers', page.locator('.dl-channel-card').first.locator('.dl-metric-grid').count() == 1 and page.locator('.dl-channel-card').first.locator('.dl-metric-grid > *').count() >= 4)
                unknown_card = page.locator('[data-channel="' + source_ids[0] + '"]')
                unknown_card.get_by_text('Activity details', exact=True).click()
                check('Unreported platform totals remain Unknown', 'Unknown' in unknown_card.inner_text())
                unknown_card.get_by_text('Activity details', exact=True).click()
                check('All six priority choices are immediately available', page.locator('[data-channel-priority]').first.locator('option').count() == 6)
                check('Discovery remains outside the active Processing Queue', not app.state.store.jobs() and planner.snapshot(state='all')['total'] >= 18)
                check('Review-required work does not make progress complete', planner.progress()['percent'] < 100)
                newest = max(adapter.channels[0]['items'], key=lambda value: value['published'])
                check('Latest channel preview uses the newest reported publication', unknown_card.locator('.dl-channel-latest strong').inner_text() == newest['title'])
                archive = adapter.inspect('https://www.youtube.com/' + adapter.channels[0]['handle'])
                archive['entries'] = [adapter.item('v9old000001', adapter.channels[0], published=1792600000)]
                studio.ingest(studio.get('watch_sources',source_ids[0]),archive,archive=True)
                page.evaluate('()=>window.ForgeDownloads.render(true)')
                check('Discovering older history does not replace the latest-upload preview', page.locator('[data-channel="' + source_ids[0] + '"] .dl-channel-latest strong').inner_text() == newest['title'])

                # Provider labels are untrusted text even in authenticated views.
                original_source = studio.get('watch_sources', source_ids[0])
                unsafe_source = {**original_source, 'title': '<img src=x onerror="window.__v9Unsafe=1">'}
                studio.put('watch_sources', source_ids[0], unsafe_source)
                page.evaluate('()=>window.ForgeDownloads.render(true)')
                unsafe_card = page.locator('[data-channel="' + source_ids[0] + '"]')
                check('Untrusted channel titles are escaped instead of creating markup', unsafe_card.locator('h3').inner_text() == unsafe_source['title'] and unsafe_card.locator('img[src="x"]').count() == 0 and page.evaluate('window.__v9Unsafe??null') is None)
                studio.put('watch_sources', source_ids[0], original_source)
                page.evaluate('()=>window.ForgeDownloads.render(true)')
                page.route('**/api/downloads/thumbnail?*fixture404=1', lambda route: route.fulfill(status=404, body=''))
                page.locator('.dl-channel-latest img').first.evaluate("el=>{el.src+='&fixture404=1';}")
                page.wait_for_function("()=>document.querySelector('.dl-channel-latest .dl-thumbnail.is-missing')")
                check('Unavailable thumbnail becomes a neutral placeholder', page.locator('.dl-channel-latest .dl-thumbnail.is-missing').count() >= 1)
                page.evaluate('()=>window.ForgeDownloads.render(true)')

                for width, columns in ((1600, 3), (1120, 2), (760, 1)):
                    page.set_viewport_size({'width': width, 'height': 1100}); page.wait_for_timeout(250)
                    cards = page.locator('.dl-channel-card')
                    boxes = [cards.nth(index).bounding_box() for index in range(cards.count())]
                    first_row = sum(abs(box['y'] - boxes[0]['y']) < 2 for box in boxes)
                    check(f'Channel cards use {columns} columns at {width}px', first_row == columns)
                    page.mouse.move(0, 0)
                    page.screenshot(path=str(output / f'FORGE-V09-Channel-Watch-{width}.png'), full_page=True)
                page.set_viewport_size({'width': 1600, 'height': 1100})
                page.locator('#watch-search').fill('Archive Observatory')
                page.wait_for_timeout(600)
                check('Channel search filters persisted source metadata', page.locator('.dl-channel-card').count() == 1)
                page.reload(); page.wait_for_selector('#watch-search')
                check('Channel search preference survives page reload', page.locator('#watch-search').input_value() == 'Archive Observatory' and page.locator('.dl-channel-card').count() == 1)
                page.locator('#watch-search').fill(''); page.wait_for_timeout(500)
                before = {row['id']: row['config']['priority'] for row in studio.sources()}
                page.locator('#watch-sort').select_option('name'); page.wait_for_timeout(350)
                check('Sorting visible cards does not alter scheduling priorities', {row['id']: row['config']['priority'] for row in studio.sources()} == before)
                page.locator('#watch-filter').select_option('paused'); page.wait_for_timeout(350)
                check('Paused filtering uses actual monitoring status', page.locator('.dl-channel-card').count() >= 1 and 'Paused' in page.locator('.dl-channel-card').first.inner_text())
                page.locator('#watch-filter').select_option('all'); page.wait_for_timeout(350)
                old_review = {item['id'] for item in planner.snapshot(source=source_ids[0], state='review')['items']}
                page.locator('[data-channel-instant="' + source_ids[0] + '"]').check()
                page.wait_for_timeout(450)
                check('Enabling Instant Download leaves historical review items held', old_review and all(planner.get(identity)['state'] == 'review' for identity in old_review))
                new_item = adapter.item('v9new000001', adapter.channels[0], 10)
                adapter.channels[0]['items'].append(new_item); studio.check_once(source_ids[0])
                new_plan = planner.get_media('youtube:' + new_item['id'])
                check('New eligible discovery is approved by Instant policy only after detection', new_plan['state'] == 'ready' and new_plan['approved'] and not app.state.store.jobs())
                page.locator('[data-channel-priority="' + source_ids[0] + '"]').select_option('ultra'); page.wait_for_timeout(350)
                check('Inline channel priority changes persist', studio.get('watch_sources', source_ids[0])['config']['priority'] == 'ultra')
                page.locator('[data-channel-order="' + source_ids[0] + '"]').select_option('most_viewed'); page.wait_for_timeout(350)
                check('Per-channel video order persists independently of the card sort', studio.get('watch_sources', source_ids[0])['config']['video_order'] == 'most_viewed')
                page.locator('[data-watch-media="' + source_ids[0] + '"]').click(); page.wait_for_selector('.dl-upload-row')
                check('Uploads opens a persisted channel-specific media browser', page.locator('.dl-upload-row').count() >= 4)
                page.locator('#uploads-grid').click(); page.wait_for_selector('.dl-uploads.is-grid')
                check('Channel Uploads supports a thumbnail grid', page.locator('.dl-uploads.is-grid .dl-upload-row').count() >= 4)
                page.locator('#uploads-back').click(); page.wait_for_selector('.dl-channel-card')

                page.locator('[data-download-tab="single"]').click(); page.wait_for_selector('#dl-urls')
                page.locator('#dl-urls').fill('https://www.youtube.com/playlist?list=PL_SYNTHETIC_V09')
                page.locator('#dl-inspect-playlist').check(); page.locator('#dl-preview').click()
                page.wait_for_selector('[data-playlist-select]')
                check('Persisted source preview retains actual reported resolution metadata', '720p' in page.locator('#dl-preview-result').inner_text())
                check('Playlist inspection renders only a bounded page of stored entries', page.locator('[data-playlist-select]').count() == 50)
                page.locator('#playlist-clear').click()
                first_playlist_id = page.locator('[data-playlist-select]').first.get_attribute('data-playlist-select')
                with page.expect_response(lambda response: '/api/downloads/import/selection' in response.url and response.request.method == 'POST'):
                    page.locator('[data-playlist-select]').first.check()
                page.locator('#playlist-next').click(); page.wait_for_timeout(250)
                second_playlist_id = page.locator('[data-playlist-select]').first.get_attribute('data-playlist-select')
                check('Playlist pagination advances without loading all rows into the DOM', second_playlist_id != first_playlist_id and page.locator('[data-playlist-select]').count() == 15)
                with page.expect_response(lambda response: '/api/downloads/import/selection' in response.url and response.request.method == 'POST'):
                    page.locator('[data-playlist-select]').first.check()
                page.locator('#playlist-prev').click(); page.wait_for_timeout(250)
                restored = page.evaluate("()=>api('/downloads/imports')")['imports'][0]
                restored_id = restored.get('id') or restored.get('import_id')
                stored_selection = page.evaluate("id=>api('/downloads/imports/'+id)", restored_id)
                check('Playlist selections survive pagination without losing the other page', page.locator('[data-playlist-select="' + first_playlist_id + '"]').is_checked() and stored_selection['selection_count'] == 2)
                page.reload(); page.wait_for_selector('#dl-restore-import')
                page.locator('#dl-restore-import').select_option(restored_id)
                page.wait_for_selector('[data-playlist-select]')
                check('Restoring a saved playlist retains its canonical selections', page.locator('[data-playlist-select="' + first_playlist_id + '"]').is_checked())
                page.locator('#playlist-clear').click(); page.locator('[data-playlist-select]').nth(1).check()
                page.locator('#playlist-plan').click(); page.wait_for_timeout(450)
                playlist_items = planner.snapshot(origin='manual_playlist', state='all')['items']
                check('Selecting one playlist entry creates one plan rather than downloading the playlist', len(playlist_items) == 1 and not app.state.store.jobs())
                check('Manual playlist destination is the selected root directly', playlist_items[0]['destination'] == str(exports))
                page.locator('#dl-urls').fill('https://www.youtube.com/watch?v=v9manual001')
                page.locator('#dl-inspect-playlist').uncheck(); page.locator('#dl-queue').click(); page.wait_for_timeout(450)
                single = planner.snapshot(origin='manual_single', state='all')['items']
                check('Manual single downloads also remain planned until capacity and approval permit', len(single) == 1 and not app.state.store.jobs())
                check('Manual single destination is not forced into watched-channel folders', single[0]['destination'] == str(exports))
                ready_order = planner.snapshot(state='ready')['items']
                check('Manual singles rank before playlists and Ultra-channel waiting items', [item['origin'] for item in ready_order[:3]] == ['manual_single', 'manual_playlist', 'channel'])
                imported = page.evaluate("()=>api('/downloads/imports')")
                check('Imported playlist selection is persisted server-side', len(imported['imports']) >= 1)

                # Add enough real persisted discovery records to exercise bounded DOM pages.
                bulk = [adapter.item(f'v9bulk{index:05d}', adapter.channels[4], index + 20) for index in range(125)]
                adapter.channels[4]['items'].extend(bulk); studio.check_once(source_ids[4])
                page.locator('[data-download-tab="priority"]').click(); page.wait_for_selector('.dl-priority-table')
                check('Planning view is titled Current Download Priority', 'Current Download Priority' in page.locator('#download-content').locator('..').inner_text() and 'Download History' not in page.locator('#download-content').locator('..').inner_text())
                check('Long planning lists are paginated instead of rendered in full', page.locator('[data-plan]').count() <= 50 and planner.snapshot(state='all')['total'] > 100)
                first_page_id = page.locator('[data-plan]').first.get_attribute('data-plan')
                page.locator('#plan-next').click(); page.wait_for_timeout(350)
                check('Priority pagination selects a different bounded page', page.locator('[data-plan]').first.get_attribute('data-plan') != first_page_id and page.locator('[data-plan]').count() <= 50)
                page.locator('#plan-prev').click(); page.wait_for_timeout(350)
                page.locator('#plan-filter').select_option('review'); page.wait_for_timeout(350)
                check('Review Required filter contains only unapproved held work', all(planner.get(row.get_attribute('data-plan'))['state'] == 'review' for row in page.locator('[data-plan]').all()))

                # Resolve every pending fake transfer before admitting the lawful local HTTP fixture.
                pending = planner.snapshot(state='outstanding', limit=1000)['items']
                planner.action([item['id'] for item in pending if item['state'] in {'ready', 'interrupted', 'failed'}], 'skip') if any(item['state'] in {'ready', 'interrupted', 'failed'} for item in pending) else None
                local_item = adapter.item('v9local0001', adapter.channels[1], 99, url=media_url,
                                          title='Synthetic local HTTP transfer', type='video')
                adapter.channels[1]['items'].append(local_item); studio.check_once(source_ids[1])
                local_plan = planner.get_media('youtube:' + local_item['id'])
                other_review = next(item for item in planner.snapshot(state='review', limit=1000)['items'] if item['id'] != local_plan['id'])
                batch = planner.frozen_batch([local_plan['id'], other_review['id']], name='Synthetic verified batch')
                denominator = planner.progress(batch=batch['id'])['total']
                extra = adapter.item('v9later0001', adapter.channels[1], 100)
                adapter.channels[1]['items'].append(extra); studio.check_once(source_ids[1])
                check('Frozen batch denominator remains fixed when new work is discovered', denominator == 2 and planner.progress(batch=batch['id'])['total'] == 2)
                check('Awaiting-review batch members cannot falsely produce 100 percent', planner.progress(batch=batch['id'])['percent'] < 100)
                page.evaluate("id=>window.ForgeDownloads.openChannel(id)", source_ids[1]); page.wait_for_selector('.dl-upload-row')
                page.locator('[data-media-select="youtube:' + local_item['id'] + '"]').check()
                page.locator('#uploads-start').click()
                page.locator('#workspace-nav [data-view="queue"]').click(); page.wait_for_selector('#queue-list')
                check('Starting a selected plan preserves an explicit global dispatch pause', app.state.store.settings()['queue_paused'] is True)
                page.locator('#resume-queue').click()
                deadline = time.monotonic() + 120; observed_progress = False; local_job = None
                transfer_samples = []
                while time.monotonic() < deadline:
                    value = planner.get(local_plan['id'])
                    if value.get('job_id'):
                        local_job = app.state.store.job(value['job_id'])
                        if local_job['status'] in {'failed', 'blocked', 'cancelled'}:
                            raise AssertionError('Local HTTP worker stopped: ' + local_job.get('message', ''))
                        if local_job.get('downloaded_bytes', 0) > 0 and local_job['status'] == 'running' and str(local_job.get('transfer_stage', local_job.get('stage', ''))).startswith('Downloading'):
                            sample = {'downloaded_bytes': local_job['downloaded_bytes'],
                                      'total_bytes': local_job.get('total_bytes'),
                                      'percent': local_job['progress']}
                            if 0 < sample['percent'] < 100 and (not transfer_samples or sample != transfer_samples[-1]):
                                transfer_samples.append(sample)
                            if len(transfer_samples) < 2 or not 20 <= sample['percent'] <= 60:
                                time.sleep(.05); continue
                            page.evaluate('()=>refresh(false).then(()=>renderQueue())'); page.wait_for_timeout(100)
                            row = page.locator('[data-row-job="' + local_job['id'] + '"]')
                            if row.count():
                                progress = row.locator('.v9-download-progress progress')
                                displayed = progress.get_attribute('value') if progress.count() else None
                                if displayed is None or not 20 <= float(displayed) <= 80:
                                    time.sleep(.05); continue
                                check('Real transfer bytes and percentage advance before completion', transfer_samples[-1]['downloaded_bytes'] > transfer_samples[0]['downloaded_bytes'] and transfer_samples[-1]['percent'] > transfer_samples[0]['percent'] and all(left['downloaded_bytes'] <= right['downloaded_bytes'] and left['percent'] <= right['percent'] for left,right in zip(transfer_samples,transfer_samples[1:])))
                                check('Actual transfer row displays measured byte information', 'B' in row.locator('.v9-transfer-facts').inner_text())
                                check('An in-progress transfer does not display whole-job 100 percent', progress.count() == 0 or progress.get_attribute('value') is None or float(progress.get_attribute('value')) < 100)
                                report['finite_progress_samples'] = transfer_samples
                                report['finite_screenshot_percent'] = float(displayed)
                                page.screenshot(path=str(output / 'FORGE-V09-Processing-Queue.png'), full_page=True)
                                observed_progress = True; break
                    time.sleep(.1)
                check('Real local HTTP downloader publishes measurable progress', observed_progress)
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    planner.reconcile()
                    value = planner.get(local_plan['id'])
                    if value['state'] == 'completed': break
                    if value['state'] in {'failed', 'unavailable'}:
                        raise AssertionError('Local HTTP transfer failed: ' + value.get('reason', ''))
                    time.sleep(.2)
                check('Only verified completed local media increments plan completion', value['state'] == 'completed' and value['outputs'] and all(Path(item['path']).is_file() and item.get('sha256') for item in value['outputs']))
                check('Original local HTTP source audio remains unchanged', hashlib.sha256(source_wave.read_bytes()).hexdigest() == source_hash)
                record = studio.get('watch_media', 'youtube:' + local_item['id'])
                job = app.state.store.job(value['job_id'])
                check('Queue History and Channel Uploads share one verified output', record['status'] == 'completed' and {item['path'] for item in record['outputs']} == {item['path'] for item in job['outputs']})
                check('Completed plus unresolved review batch remains below 100 percent', planner.progress(batch=batch['id'])['completed'] == 1 and planner.progress(batch=batch['id'])['percent'] < 100)
                page.evaluate('()=>refresh(false).then(()=>renderQueue())')
                page.locator('[data-filter="history"]').click()
                history_row = page.locator('[data-row-job="' + job['id'] + '"]')
                page.wait_for_selector('[data-row-job="' + job['id'] + '"]')
                check('Verified download history offers file/folder access and destination', history_row.locator('[data-open-output]').count() == 1 and history_row.locator('[data-reveal-output]').count() == 1 and history_row.locator('.v9-output-directory').count() == 1)
                page.screenshot(path=str(output / 'FORGE-V09-Queue-History.png'), full_page=True)
                removed = page.evaluate("id=>api('/queue/actions',{ids:[id],action:'remove'})", job['id'])
                check('Clearing Queue History preserves downloaded media and channel completion', removed['ok'] and planner.get(local_plan['id'])['state'] == 'completed' and studio.get('watch_media', 'youtube:' + local_item['id'])['status'] == 'completed' and all(Path(item['path']).is_file() for item in record['outputs']))
                page.evaluate("id=>window.ForgeDownloads.openChannel(id)", source_ids[1]); page.wait_for_selector('.dl-upload-row')
                page.locator('[data-upload-filter="completed"]').click(); page.wait_for_timeout(350)
                with page.expect_response(lambda response: '/api/' in response.url and response.request.method == 'POST') as opened:
                    page.locator('[data-upload-open]').first.click()
                check('Channel Uploads can still access verified media after queue history is cleared', opened.value.status == 200)

                # A second real fixture deliberately omits Content-Length. It is
                # a separate explicit source, not a manufactured percentage.
                unknown_url = media_url.replace('/synthetic-source.wav', '/synthetic-unknown.wav')
                page.locator('[data-download-tab="single"]').click(); page.wait_for_selector('#dl-urls')
                page.locator('#dl-kind').select_option('download_audio')
                page.locator('[data-dl-option="format"]').select_option('wav')
                page.locator('#dl-urls').fill(unknown_url); page.locator('#dl-directory').fill(str(exports))
                page.locator('#dl-queue').click(); page.wait_for_timeout(200)
                unknown_plan = next(item for item in planner.snapshot(origin='manual_single', state='all')['items'] if item['url'] == unknown_url)
                page.locator('#workspace-nav [data-view="queue"]').click(); page.wait_for_selector('#queue-list')
                page.locator('[data-filter="active"]').click()
                deadline = time.monotonic() + 120; unknown_observed = False
                while time.monotonic() < deadline:
                    unknown_value = planner.get(unknown_plan['id'])
                    if unknown_value.get('job_id'):
                        unknown_job = app.state.store.job(unknown_value['job_id'])
                        if unknown_job['status'] in {'failed', 'blocked', 'cancelled'}:
                            raise AssertionError('Unknown-length HTTP worker stopped: ' + unknown_job.get('message', ''))
                        if unknown_job['status'] == 'running' and unknown_job.get('downloaded_bytes', 0) > 0 and str(unknown_job.get('transfer_stage', unknown_job.get('stage', ''))).startswith('Downloading'):
                            page.evaluate('()=>refresh(false).then(()=>renderQueue())')
                            unknown_row = page.locator('[data-row-job="' + unknown_job['id'] + '"]')
                            progress = unknown_row.locator('.v9-download-progress progress')
                            report['unknown_progress_observation'] = {'total_bytes': unknown_job.get('total_bytes'),
                                'downloaded_bytes': unknown_job.get('downloaded_bytes'),
                                'progress_measured': unknown_job.get('progress_measured'),
                                'transfer_stage': unknown_job.get('transfer_stage'),
                                'visible_rows': unknown_row.count(), 'progress_bars': progress.count(),
                                'value': progress.get_attribute('value') if progress.count() else None}
                            check('Real unknown-length transfer keeps whole-job progress indeterminate', unknown_job.get('total_bytes') in {None, 0} and unknown_job.get('progress_measured') is False and progress.count() == 1 and progress.get_attribute('value') is None)
                            page.screenshot(path=str(output / 'FORGE-V09-Unknown-Transfer.png'), full_page=True)
                            unknown_observed = True; break
                    time.sleep(.1)
                check('Unknown-size progress was observed from actual local HTTP bytes', unknown_observed)
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    planner.reconcile()
                    unknown_value = planner.get(unknown_plan['id'])
                    if unknown_value['state'] == 'completed': break
                    if unknown_value['state'] in {'failed', 'unavailable'}:
                        raise AssertionError('Unknown-length HTTP transfer failed: ' + unknown_value.get('reason', ''))
                    time.sleep(.2)
                check('Unknown-length transfer completes only after verified media publication', unknown_value['state'] == 'completed' and unknown_value['outputs'])
                check('New verified downloads do not alter an older batch denominator', planner.progress(batch=batch['id'])['total'] == denominator)
                page.evaluate("()=>window.ForgeDownloads.openPriority()"); page.wait_for_selector('#plan-batch')
                page.locator('#plan-batch').select_option(batch['id']); page.wait_for_timeout(350)
                page.screenshot(path=str(output / 'FORGE-V09-Current-Download-Priority.png'), full_page=True)
                page.locator('#black-scripting-launcher').click(); page.wait_for_selector('.black-workbench')
                check('Black Scripting remains a distinct mode with an exit', not page.locator('#sidebar').is_visible() and 'Exit' in page.locator('#black-scripting-launcher').inner_text())
                page.locator('#black-scripting-launcher').click(); page.wait_for_selector('#sidebar')
                page.locator('#sidebar [data-view="tts"]').click(); page.wait_for_selector('#sp-editor')
                check('V0.8 speech editor and voice library remain intact', page.locator('.sp-library').count() == 1 and page.locator('[data-voice="am_onyx"]').count() == 1)
                page.locator('#sidebar [data-view="audio_effects"]').click()
                page.wait_for_selector('#sp-fx-path')
                check('Independent Audio Effects remains accessible', page.locator('#sp-fx-path').count() == 1)
                check('No thumbnail extraction or export tool is exposed', not re.search(r'(extract|download|save|export) thumbnail', page.locator('body').inner_text(), re.I))
                check('Browser recorded no uncaught JavaScript errors', not errors)
                report.update(status='passed', checks=checks, javascript_errors=errors,
                              real_local_http_transfer=True, original_source_preserved=True,
                              no_owner_installation_accessed=True)
                context.close(); browser.close()
        except Exception as error:
            report.update(error=str(error), traceback=traceback.format_exc(), javascript_errors=errors)
            if page is not None:
                try:
                    page.screenshot(path=str(output / 'FAILED-V09.png'), full_page=True)
                except Exception:
                    pass
            raise
        finally:
            server.should_exit = True; thread.join(timeout=12)
            media_server.shutdown(); media_server.server_close()
            report['checks'] = checks
            (output / 'BROWSER_V09_RESULTS.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
