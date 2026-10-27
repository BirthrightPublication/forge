"""Actual downloader measurements and private retry/publication records.

The existing yt-dlp/FFmpeg worker performs transfers. This module translates its
callbacks and records only owned state needed to recover a stopped attempt.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from .core import json_write


class DownloadFailure(RuntimeError):
    def __init__(self, details, *, partial_files=(), warnings=()):
        self.code = details['code']
        self.retryable = details['retryable']
        self.partial_files = list(partial_files)
        self.warnings = list(warnings)
        super().__init__(details['message'])


def number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value if value > 0 or not positive and value >= 0 else None


class DownloadProgress:
    """Keep measured transfer progress separate from unmeasured processing.

    A complete stream never establishes a complete job. Aggregate byte progress
    is available only when every selected stream has an actual known length.
    Current-transfer percentages remain useful when that aggregate is unknown.
    """
    def __init__(self, report, *, tool, playlist=False, clock=time.monotonic):
        self.report, self.tool, self.playlist, self.clock = report, tool, playlist, clock
        self.started = clock()
        self.media = None
        self.streams = {}
        self.selected = []
        self.last_emitted = -math.inf
        self.last_stage = None
        self.last_progress = 0

    def prepare(self, info):
        media = str(info.get('id') or '')
        if self.media is not None and media and media != self.media:
            self.streams, self.selected = {}, []
            self.last_progress = 0
        if media:
            self.media = media
        formats = info.get('requested_formats')
        if not formats and info.get('format_id'):
            formats = [info]
        if formats:
            self.selected = [str(item.get('format_id') or index) for index, item in enumerate(formats)]
            for key, item in zip(self.selected, formats):
                self.streams.setdefault(key, {'done': 0, 'total': number(item.get('filesize'), positive=True)})

    def hook(self, event):
        status = event.get('status')
        if status not in {'downloading', 'finished'}:
            return
        info = event.get('info_dict') or {}
        # Real yt-dlp removes requested_formats from the per-stream callback.
        # The match-filter's resolved entry captured the complete selection.
        if not self.selected:
            self.prepare(info)
        key = str(info.get('format_id') or event.get('filename') or 'media')
        if len(self.selected) == 1 and key not in self.selected:
            key = self.selected[0]
        stream = self.streams.setdefault(key, {'done': 0, 'total': None})
        done = number(event.get('downloaded_bytes'))
        total = number(event.get('total_bytes'), positive=True)
        if done is not None:
            stream['done'] = done
        if total is not None:
            stream['total'] = total
        # Approximate sizes are not promoted to measured totals.
        if status == 'finished':
            stream['finished'] = True
        vcodec, acodec = info.get('vcodec'), info.get('acodec')
        if vcodec == 'none' or self.tool == 'download_audio':
            stage = 'Downloading Audio'
        elif acodec == 'none':
            stage = 'Downloading Video'
        else:
            stage = 'Downloading Media'
        stage_key = (key, stage, status)
        now = self.clock()
        if status == 'downloading' and stage_key == self.last_stage and now - self.last_emitted < .3:
            return
        self.last_stage, self.last_emitted = stage_key, now
        selected = self.selected or ([key] if self.tool == 'download_audio' or vcodec not in {None, 'none'} and acodec not in {None, 'none'} else [])
        aggregate = bool(selected and not self.playlist and all(self.streams.get(item, {}).get('total') for item in selected))
        current_percent = min(100, 100 * stream['done'] / stream['total']) if stream['total'] else None
        if aggregate:
            expected = sum(self.streams[item]['total'] for item in selected)
            transferred = sum(min(self.streams[item]['done'], self.streams[item]['total']) for item in selected)
            self.last_progress = min(99, 100 * transferred / expected)
            scope = 'transfers'
        else:
            expected, transferred = total, done
            scope = 'current_transfer'
        speed = number(event.get('speed'), positive=True)
        eta = max(0, expected - transferred) / speed if aggregate and speed else None
        self.report(self.last_progress if aggregate else 0, stage,
                    progress_measured=aggregate, stage=stage, transfer_stage=stage,
                    downloaded_bytes=int(transferred) if transferred is not None else None,
                    total_bytes=int(expected) if expected is not None else None,
                    speed_bps=speed, speed=f'{speed / 1048576:.2f} MB/s' if speed else '',
                    elapsed_seconds=max(0, now - self.started), eta_seconds=eta,
                    stage_progress=current_percent, progress_scope=scope,
                    stream_index=selected.index(key) + 1 if key in selected else None,
                    stream_count=len(selected) if selected else None)

    def postprocess(self, event):
        if event.get('status') not in {'started', 'processing', 'finished'}:
            return
        name = str(event.get('postprocessor') or '').lower()
        if 'merg' in name or 'concat' in name:
            stage = 'Merging Media'
        elif 'metadata' in name or 'subtitle' in name:
            stage = 'Processing Metadata'
        elif 'movefiles' in name:
            stage = 'Verifying Output'
        else:
            stage = 'Processing Media'
        self.processing(stage)

    def processing(self, stage):
        self.report(self.last_progress, stage, progress_measured=False, stage=stage,
                    transfer_stage=stage, progress_scope='processing', stage_progress=None,
                    eta_seconds=None, speed='', speed_bps=None,
                    elapsed_seconds=max(0, self.clock() - self.started))


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def transfer_identity(url, options, tool):
    # Credentials and private cookie filenames never enter a disk record. The
    # URL is hashed too, since otherwise a signed URL could disclose a token.
    public_options = {key: value for key, value in options.items() if key not in {'cookies', 'thumbnail'}}
    return hashlib.sha256(json.dumps([url, public_options, tool], sort_keys=True).encode()).hexdigest()


def prepare_cache(root, url, options, tool):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    identity = transfer_identity(url, options, tool)
    ledger = root / 'transfer.json'
    try:
        previous = json.loads(ledger.read_text('utf-8'))
        if not isinstance(previous, dict):
            previous = {'identity': 'invalid'}
    except (OSError, ValueError):
        previous = {'identity': 'invalid'} if ledger.exists() else {}
    if previous.get('identity') not in {None, identity}:
        # Do not delete or accidentally resume an older request's fragments.
        root = root / 'variants' / identity
        root.mkdir(parents=True, exist_ok=True)
        ledger = root / 'transfer.json'
    if not ledger.exists():
        json_write(ledger, {'schema': 1, 'identity': identity,
                           'legacy_parts_preserved': any(root.glob('*.part')),
                           'created': time.time()})
    return root, identity


class DownloadPublication:
    """Journal final names before atomic publication, then reconcile by hash.

    A killed worker may have moved a verified file before updating its normal
    result manifest. The intended name and digest make that crash recoverable
    without publishing a second copy. No saved path outside destination is read.
    """
    def __init__(self, cache, destination, identity):
        self.path = Path(cache) / 'publication.json'
        self.destination = Path(destination).resolve()
        self.identity = identity
        try:
            record = json.loads(self.path.read_text('utf-8'))
        except (OSError, ValueError):
            record = {}
        self.record = record if (isinstance(record, dict) and record.get('identity') == identity
                                 and record.get('destination') == str(self.destination)
                                 and isinstance(record.get('entries'), list)
                                 and all(isinstance(entry, dict) for entry in record['entries'])) else {
            'schema': 1, 'identity': identity, 'destination': str(self.destination),
            'entries': [], 'request_complete': False, 'warnings': []}

    def save(self):
        json_write(self.path, self.record)

    def verified(self, entry):
        name = entry.get('final_name')
        if not isinstance(name, str) or not name or Path(name).name != name or '\\' in name or name in {'.', '..'}:
            return None
        path = self.destination / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != entry.get('size'):
            return None
        expected = entry.get('sha256', '')
        if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected) or file_digest(path) != expected:
            return None
        stat = path.stat()
        return {'name': name, 'path': str(path), 'size': stat.st_size, 'sha256': expected,
                'source_stat': {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}}

    def recovered(self):
        entries = self.record.get('entries', [])
        if not self.record.get('request_complete') or not isinstance(entries, list) or not entries:
            return []
        outputs = [self.verified(entry) for entry in entries]
        return outputs if all(outputs) else []

    def prepare(self, files, warnings, *, complete=True):
        previous = self.record.get('entries', [])
        entries = []
        for path in files:
            digest = file_digest(path)
            matched = next((entry for entry in previous if entry.get('sha256') == digest
                            and entry.get('size') == path.stat().st_size and self.verified(entry)), {})
            entries.append({'source_name': path.name, 'size': path.stat().st_size,
                            'sha256': digest, 'final_name': matched.get('final_name')})
        # Earlier complete files from a partially failed playlist stay referenced.
        for entry in previous:
            if self.verified(entry) and not any(item.get('final_name') == entry.get('final_name') for item in entries):
                entries.append(entry)
        self.record.update(entries=entries, request_complete=complete, warnings=list(warnings))
        self.save()

    def publish(self, source):
        source = Path(source)
        entry = next(item for item in self.record['entries'] if item['source_name'] == source.name)
        recovered = self.verified(entry)
        if recovered:
            return recovered
        for index in range(10000):
            name = source.name if index == 0 else f'{source.stem} ({index}){source.suffix}'
            destination = self.destination / name
            if destination.exists():
                continue
            entry['final_name'] = name
            self.save()  # Durable intent comes before the filesystem commit.
            try:
                if os.name == 'nt':
                    os.rename(source, destination)
                else:
                    os.link(source, destination)
                    source.unlink()
            except FileExistsError:
                continue
            result = self.verified(entry)
            if not result:
                raise RuntimeError('Published download changed before verification.')
            return result
        raise RuntimeError('Too many existing output files. Choose another directory.')
