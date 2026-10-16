"""Offline entitlement boundary for future Birthright providers.

V0.4 deliberately has no activation, billing, identity or remote license client.
Only server-owned provider implementations can decide access; browser input cannot
replace a provider or grant a permission.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
import time

from fastapi import HTTPException

from .core import TOOLS


@dataclass(frozen=True)
class ModuleIdentity:
    key: str
    title: str
    version: int = 1

    @property
    def feature_id(self) -> str:
        return 'birthright.forge.' + self.key


MODULES = tuple(ModuleIdentity(key, tool['title']) for key, tool in TOOLS.items()) + (
    ModuleIdentity('black_scripting', 'Black Scripting'),
    ModuleIdentity('queue', 'Processing Queue'),
)
BY_KEY = {module.key: module for module in MODULES}


class EntitlementProvider(Protocol):
    """Future adapters verify their own signed grants and offline policy here."""
    provider_id: str
    mode: str

    def allows(self, feature_id: str) -> bool: ...


@dataclass(frozen=True)
class VerifiedLicense:
    """A verifier's result, never a directly trusted browser JSON object."""
    issuer: str
    subject: str
    features: frozenset[str]
    expires_at: float
    device_id: str | None = None
    updates_until: float | None = None


class LicenseVerifier(Protocol):
    """A future adapter must check signature, issuer, audience and token format.

    No production signing keys, token parser or activation transport is supplied.
    """
    def verify(self, signed_token: str) -> VerifiedLicense: ...


class DeviceIdentity(Protocol):
    """Future opt-in device management; V0.4 collects no device identifier."""
    def identifier(self) -> str: ...


@dataclass(frozen=True)
class OfflinePolicy:
    """Expiry and optional device matching are enforced without contacting a server."""
    def allows(self, license: VerifiedLicense, feature_id: str, *, now: float,
               device_id: str | None = None) -> bool:
        return (feature_id in license.features and now < license.expires_at
                and (license.device_id is None or license.device_id == device_id))

    def update_eligible(self, license: VerifiedLicense, *, release_timestamp: float) -> bool:
        return license.updates_until is not None and release_timestamp <= license.updates_until


class VerifiedLicenseProvider:
    """In-memory adapter boundary, unused by the development installation.

    Callers must provide an actual verifier; no unsigned-token fallback exists.
    Commercial update eligibility is distinct from permission to process files.
    """
    provider_id = 'birthright.verified-offline'
    mode = 'licensed'

    def __init__(self, signed_token: str, verifier: LicenseVerifier,
                 device: DeviceIdentity | None = None, policy: OfflinePolicy | None = None):
        self.license = verifier.verify(signed_token)
        self.device_id = device.identifier() if device is not None else None
        self.policy = policy or OfflinePolicy()

    def allows(self, feature_id: str) -> bool:
        return self.policy.allows(self.license, feature_id, now=time.time(), device_id=self.device_id)

    def update_eligible(self, release_timestamp: float) -> bool:
        return self.policy.update_eligible(self.license, release_timestamp=release_timestamp)


class DevelopmentProvider:
    provider_id = 'birthright.local-development'
    mode = 'development'

    def allows(self, feature_id: str) -> bool:
        return feature_id in {module.feature_id for module in MODULES}


class EntitlementService:
    def __init__(self, provider: EntitlementProvider | None = None):
        self.provider = provider if provider is not None else DevelopmentProvider()

    def allowed(self, key: str) -> bool:
        module = BY_KEY.get(key)
        return bool(module and self.provider.allows(module.feature_id))

    def require(self, key: str) -> None:
        if not self.allowed(key):
            raise HTTPException(403, 'This module is unavailable with the current entitlement provider.')

    def snapshot(self) -> dict:
        return {
            'schema_version': 1,
            'product_id': 'birthright.forge',
            'provider': self.provider.provider_id,
            'mode': self.provider.mode,
            'activation_required': False,
            'account': None,
            'billing_available': False,
            'modules': [{'key': m.key, 'id': m.feature_id, 'version': m.version,
                         'title': m.title, 'enabled': self.allowed(m.key)} for m in MODULES],
        }


def require_feature(app, key: str) -> None:
    app.state.entitlements.require(key)


def mount(app):
    @app.get('/api/entitlements')
    def entitlements():
        return app.state.entitlements.snapshot()
