"""Reusable signed discovery with persistent replay protection and offline status."""
from __future__ import annotations

import time
from dataclasses import asdict

from .manifest import verify


class SignedDiscovery:
    """Storage implements get(key, default) and set(key, value); no cloud account.

    The caller schedules checks on a background thread. Offline snapshots are
    displayable, but installation must reverify the cached envelope (including
    expiration) with approved publisher keys. Keys never come from this endpoint.
    """
    def __init__(self, application, channel, manifest_url, keys, transport, storage,
                 *, interval=43200, clock=time.time):
        self.application, self.channel, self.url = application, channel, manifest_url
        self.keys, self.transport, self.storage = keys, transport, storage
        self.interval, self.clock = max(300, interval), clock
        self.key = 'birthright_updates:' + application + ':' + channel

    def check(self, *, force=False):
        old = self.storage.get(self.key, {})
        now = self.clock()
        if not force and now < old.get('next_check', 0):
            return old
        try:
            envelope = self.transport.json(self.url)
            release = verify(envelope, application=self.application, channel=self.channel,
                             trusted_keys=self.keys, highest_sequence=old.get('highest_sequence', 0),
                             now=now, policy=self.transport.policy)
            result = {'release': asdict(release), 'envelope': envelope, 'checked': now,
                      'next_check': now + self.interval, 'highest_sequence': release.sequence,
                      'failures': 0, 'offline': False, 'error': ''}
        except Exception as exc:
            failures = min(10, old.get('failures', 0) + 1)
            result = {**old, 'checked': now, 'next_check': now + min(43200, 300 * 2 ** (failures - 1)),
                      'failures': failures, 'offline': True, 'error': str(exc)[:500]}
        self.storage.set(self.key, result)
        return result

    def for_installation(self):
        current = self.storage.get(self.key, {})
        return verify(current.get('envelope'), application=self.application, channel=self.channel,
                      trusted_keys=self.keys, highest_sequence=current.get('highest_sequence', 0),
                      now=self.clock(), policy=self.transport.policy)
