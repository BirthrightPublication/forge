"""Queue 3.0 routes; mounted inside the existing loopback/auth/CSRF guard."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field

from .core import IMAGES, MEDIA
from .models import Strict
from .queue import public_job

OPENABLE_OUTPUTS = MEDIA | IMAGES | {'.txt', '.json', '.srt', '.vtt'}


class BulkAction(Strict):
    ids: list[str] = Field(min_length=1, max_length=1000)
    action: Literal['cancel', 'retry', 'remove', 'up', 'down', 'first', 'last']


def windows_supported():
    return os.name == 'nt'


def open_media_output(job, index):
    if not 0 <= index < len(job['outputs']):
        raise ValueError('Output not found.')
    raw = Path(job['outputs'][index]['path'])
    path = raw.resolve()
    if not raw.is_absolute() or not path.is_file():
        raise ValueError('This output was moved or deleted.')
    # Plain transcript files and media can use native associations. Scriptable
    # documents, shell shortcuts, executables and extension aliases cannot.
    if raw.suffix.lower() not in OPENABLE_OUTPUTS or path.suffix.lower() not in OPENABLE_OUTPUTS:
        raise ValueError('Native Open supports media, raster images and plain TXT/JSON/SRT/VTT transcripts. Use Preview or Download for other formats.')
    destination = job.get('payload', {}).get('destination')
    if not destination:
        raise ValueError('This legacy output has no verified destination. Use its containing folder.')
    try:
        path.relative_to(Path(destination).resolve())
    except ValueError:
        raise ValueError('The output no longer points inside its recorded destination.') from None
    if not windows_supported():
        return {'opened': False, 'path': str(path), 'reason': 'Native file opening is supported on Windows.'}
    # Fixed native operation; a stored output path is the only argument. Never
    # invoke cmd.exe, PowerShell, a request-selected executable, or shell=True.
    os.startfile(str(path), 'open')
    return {'opened': True}


def mount(app, store, manager):
    @app.get('/api/queue/status')
    def status():
        return manager.status()

    @app.get('/api/queue/jobs')
    def jobs(view: Literal['active', 'failed', 'history'] = 'active', limit: int = 500):
        if not 1 <= limit <= 10000:
            raise ValueError('Choose a history limit from 1 to 10000.')
        states = {'active': {'queued', 'running', 'cancelling', 'finalizing'},
                  'failed': {'failed', 'blocked', 'interrupted'},
                  'history': {'completed', 'cancelled'}}[view]
        records = [job for job in store.jobs(limit=100000) if job['status'] in states]
        if view == 'active':
            records.sort(key=lambda job: (job['status'] == 'queued', job.get('rank', job['created'])))
        return {'jobs': [public_job(job) for job in records[:limit]], 'total': len(records), 'view': view}

    @app.get('/api/queue/jobs/{id}')
    def details(id: str):
        return public_job(store.job(id))

    @app.post('/api/queue/actions')
    def actions(body: BulkAction):
        return manager.bulk_action(body.ids, body.action)

    @app.post('/api/jobs/{id}/open-output/{index}')
    def open_output(id: str, index: int):
        return open_media_output(store.job(id), index)
