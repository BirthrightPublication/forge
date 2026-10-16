from fastapi import HTTPException

from forge.core import TOOLS
from forge.entitlements import EntitlementService, OfflinePolicy, VerifiedLicense, VerifiedLicenseProvider


def test_development_catalog_covers_all_local_tools():
    service = EntitlementService()
    snapshot = service.snapshot()
    assert snapshot['product_id'] == 'birthright.forge'
    assert snapshot['activation_required'] is False
    assert snapshot['billing_available'] is False
    assert {m['key'] for m in snapshot['modules']} == set(TOOLS) | {'black_scripting', 'queue'}
    assert all(m['enabled'] for m in snapshot['modules'])
    assert len({m['id'] for m in snapshot['modules']}) == len(snapshot['modules'])
    assert not service.allowed('unknown')


def test_server_owned_provider_can_deny_execution():
    class RestrictedProvider:
        provider_id = 'test.restricted'
        mode = 'test'

        def allows(self, feature_id):
            return feature_id == 'birthright.forge.qr'

    service = EntitlementService(RestrictedProvider())
    service.require('qr')
    try:
        service.require('transcribe')
    except HTTPException as error:
        assert error.status_code == 403
    else:
        raise AssertionError('A denied grant must block execution')


def test_future_offline_policy_separates_expiry_device_and_updates():
    grant = VerifiedLicense('birthright', 'test-user', frozenset({'birthright.forge.qr'}),
                            expires_at=200, device_id='opt-in-device', updates_until=150)
    policy = OfflinePolicy()
    assert policy.allows(grant, 'birthright.forge.qr', now=100, device_id='opt-in-device')
    assert not policy.allows(grant, 'birthright.forge.qr', now=200, device_id='opt-in-device')
    assert not policy.allows(grant, 'birthright.forge.qr', now=100)
    assert not policy.allows(grant, 'birthright.forge.transcribe', now=100, device_id='opt-in-device')
    assert policy.update_eligible(grant, release_timestamp=150)
    assert not policy.update_eligible(grant, release_timestamp=151)


def test_rejected_signed_token_has_no_unsigned_fallback():
    class RejectingVerifier:
        def verify(self, token):
            raise ValueError('Invalid signature')

    import pytest
    with pytest.raises(ValueError, match='Invalid signature'):
        VerifiedLicenseProvider('unsigned-fixture', RejectingVerifier())
