"""Application-independent discovery, immutable release metadata and verified transfer."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit


class UpdateError(ValueError):
    pass


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    sha256: str
    size: int | None = None
    platform: str = ''
    architecture: str = ''
    format: str = ''


@dataclass(frozen=True)
class Release:
    application: str
    version: str
    published: str
    notes: str
    assets: tuple[Asset, ...]
    channel: str = 'stable'
    sequence: int | None = None
    authenticity: str = 'publisher-https-checksum'


@dataclass(frozen=True)
class NetworkPolicy:
    origins: tuple[str, ...]
    redirect_hosts: tuple[str, ...] = ()
    timeout: float = 10
    max_metadata_bytes: int = 2_000_000
    max_asset_bytes: int = 128 * 1048576
    max_transfer_seconds: float = 300

    def validate(self, url, *, redirect=False):
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.username or parts.password or parts.fragment or parts.port not in (None, 443):
            raise UpdateError('Update URLs must use approved HTTPS publishers.')
        if not any(url.startswith(origin) for origin in self.origins):
            if not redirect or parts.hostname not in self.redirect_hosts:
                raise UpdateError('The update publisher or redirect is not approved.')


class RedirectPolicy(urllib.request.HTTPRedirectHandler):
    def __init__(self, policy):
        self.policy = policy

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        self.policy.validate(newurl, redirect=True)
        # No service authentication tokens are used or forwarded.
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class Transport:
    def __init__(self, policy: NetworkPolicy, user_agent='Birthright-Update-Core/0.1', opener=None):
        self.policy, self.user_agent = policy, user_agent
        self.opener = opener or urllib.request.build_opener(RedirectPolicy(policy))

    def open(self, url):
        self.policy.validate(url)
        request = urllib.request.Request(url, headers={'User-Agent': self.user_agent})
        return self.opener.open(request, timeout=self.policy.timeout)

    def metadata(self, url):
        with self.open(url) as response:
            content = response.read(self.policy.max_metadata_bytes + 1)
        if len(content) > self.policy.max_metadata_bytes:
            raise UpdateError('Update metadata exceeds the size limit.')
        return content

    def json(self, url):
        return json.loads(self.metadata(url))

    def download(self, asset: Asset, directory: Path, progress=lambda *args: None):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}', asset.name) or '..' in asset.name:
            raise UpdateError('Unsafe update asset filename.')
        if not re.fullmatch(r'[0-9a-f]{64}', asset.sha256):
            raise UpdateError('A SHA-256 digest is required before transfer.')
        if asset.size is not None and (type(asset.size) is not int or not 0 < asset.size <= self.policy.max_asset_bytes):
            raise UpdateError('Invalid update size.')
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / asset.name
        if target.is_symlink():
            raise UpdateError('Update destination cannot be a link.')
        if target.exists():
            if digest(target) == asset.sha256 and (asset.size is None or target.stat().st_size == asset.size):
                return target
            raise UpdateError('A different local file already has this update filename.')
        part = directory / ('.' + asset.name + '.' + uuid.uuid4().hex + '.partial')
        started, count, checksum = time.monotonic(), 0, hashlib.sha256()
        try:
            with self.open(asset.url) as response, part.open('xb') as stream:
                while block := response.read(1024 * 1024):
                    count += len(block)
                    elapsed = time.monotonic() - started
                    if count > (asset.size or self.policy.max_asset_bytes) or elapsed > self.policy.max_transfer_seconds:
                        raise UpdateError('Update transfer exceeded its size or time limit.')
                    checksum.update(block)
                    stream.write(block)
                    progress(count, asset.size, count / max(elapsed, .001))
                stream.flush()
                os.fsync(stream.fileno())
            if checksum.hexdigest() != asset.sha256 or (asset.size is not None and count != asset.size):
                raise UpdateError('Update integrity check failed. Nothing was installed.')
            # Hard-link publication is atomic and refuses to overwrite a concurrent file.
            try:
                os.link(part, target)
            except FileExistsError:
                if target.is_symlink() or digest(target) != asset.sha256:
                    raise UpdateError('A different update file appeared during transfer.')
            return target
        finally:
            part.unlink(missing_ok=True)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


class InstallationAdapter(Protocol):
    """Adapters own OS-specific locks, application/data boundaries and recovery."""
    def preflight(self, release: Release, asset: Path) -> dict: ...
    def quiesce(self, *, cancel_confirmed: bool = False) -> None: ...
    def install(self, release: Release, asset: Path) -> dict: ...
    def validate(self, receipt: dict) -> None: ...
    def rollback(self, receipt: dict) -> None: ...
    def recover(self) -> dict: ...
    def restart(self) -> None: ...


def apply_update(adapter: InstallationAdapter, release: Release, asset: Path, *, approved: bool):
    if approved is not True:
        raise UpdateError('Application installation requires explicit approval.')
    adapter.preflight(release, asset)
    adapter.quiesce()
    receipt = None
    try:
        receipt = adapter.install(release, asset)
        adapter.validate(receipt)
    except Exception:
        if receipt is not None:
            adapter.rollback(receipt)
        else:
            adapter.recover()  # Installation journals must exist before writes.
        adapter.restart()  # Only after successful recovery; never launch broken code.
        raise
    adapter.restart()
    return receipt
