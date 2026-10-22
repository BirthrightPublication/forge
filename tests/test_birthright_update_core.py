import base64
import copy
import hashlib
import io
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from forge.birthright_update.core import Asset, NetworkPolicy, Release, Transport, UpdateError, apply_update
from forge.birthright_update.manifest import canonical, verify
from forge.birthright_update.discovery import SignedDiscovery

POLICY = NetworkPolicy(('https://publisher.example/releases/',), redirect_hosts=('cdn.example',), max_asset_bytes=100)


def transport(content):
    return Transport(POLICY, opener=SimpleNamespace(open=lambda *a, **k: io.BytesIO(content)))


def test_verified_transfer_is_atomic_and_reuses_exact_file(tmp_path):
    content = b'verified test fixture'
    asset = Asset('Application-v1.zip', 'https://publisher.example/releases/v1.zip', hashlib.sha256(content).hexdigest(), len(content))
    target = transport(content).download(asset, tmp_path)
    assert target.read_bytes() == content
    assert transport(b'not fetched').download(asset, tmp_path) == target
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize('url', ['http://publisher.example/releases/a', 'https://publisher.example.evil/releases/a',
                               'https://other.example/a', 'https://user:pass@publisher.example/releases/a'])
def test_publisher_boundaries(url):
    with pytest.raises(UpdateError): POLICY.validate(url)


def test_redirect_policy_is_separate_from_initial_publisher():
    POLICY.validate('https://cdn.example/asset?temporary=signature', redirect=True)
    with pytest.raises(UpdateError): POLICY.validate('https://cdn.example/asset')
    with pytest.raises(UpdateError): POLICY.validate('https://evil.example/asset', redirect=True)


@pytest.mark.parametrize('size,digest', [(3,'0'*64),(500,'0'*64),(None,'invalid')])
def test_invalid_download_never_publishes(tmp_path, size, digest):
    with pytest.raises(UpdateError): transport(b'bad').download(Asset('app.zip','https://publisher.example/releases/a',digest,size),tmp_path)
    assert not list(tmp_path.iterdir())


def test_download_preserves_different_existing_local_file(tmp_path):
    (tmp_path/'app.zip').write_bytes(b'local build')
    with pytest.raises(UpdateError):
        transport(b'new').download(Asset('app.zip','https://publisher.example/releases/a',hashlib.sha256(b'new').hexdigest()),tmp_path)
    assert (tmp_path/'app.zip').read_bytes() == b'local build'


def signed_manifest():
    # Ephemeral synthetic test signing key; never a production trust anchor.
    key = Ed25519PrivateKey.generate()
    payload = {'schema': 1, 'application': 'birthright.example', 'channel': 'stable', 'version': '1.2.0',
               'sequence': 7, 'published': '2026-10-01T00:00:00Z', 'expires': '2026-11-01T00:00:00Z',
               'notes': 'Synthetic release', 'assets': [{'name': 'Example-v1.2.0-windows-x64.zip',
               'url': 'https://publisher.example/releases/v1.2.0.zip', 'sha256': '1'*64, 'size': 10,
               'platform': 'windows', 'architecture': 'x64', 'format': 'app-zip-v1'}]}
    def sign(value):
        return {'signed': value, 'signatures': [{'key_id': 'test-only', 'algorithm': 'ed25519',
                'signature': base64.b64encode(key.sign(b'Birthright-Update-Manifest-v1\n'+canonical(value))).decode()}]}
    keys = {'test-only': key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
    return payload, sign, keys


def checked(envelope, keys, **kwargs):
    return verify(envelope, application='birthright.example', channel='stable', trusted_keys=keys,
                  now=datetime(2026,10,22,tzinfo=timezone.utc).timestamp(), policy=POLICY, **kwargs)


def test_signed_contract_preserves_platform_and_provenance():
    payload, sign, keys = signed_manifest()
    release = checked(sign(payload), keys, highest_sequence=6)
    assert release.authenticity == 'ed25519' and release.sequence == 7
    assert release.assets[0].platform == 'windows' and release.assets[0].format == 'app-zip-v1'


@pytest.mark.parametrize('field,value', [('version','9.9.9'),('notes','tampered'),('sequence',100)])
def test_signed_payload_tampering_is_rejected(field,value):
    payload, sign, keys = signed_manifest()
    envelope = sign(payload);envelope['signed'][field] = value
    with pytest.raises(UpdateError,match='signature'): checked(envelope,keys)


@pytest.mark.parametrize('field,value', [('application','birthright.other'),('channel','beta'),('sequence',0),
                                      ('expires','2026-10-02T00:00:00Z'),('published','2026-10-23T00:00:00Z')])
def test_valid_signature_does_not_override_manifest_policy(field,value):
    payload, sign, keys = signed_manifest();payload[field] = value
    with pytest.raises(UpdateError): checked(sign(payload),keys)


def test_unknown_key_and_replayed_manifest_fail_closed():
    payload, sign, keys = signed_manifest()
    with pytest.raises(UpdateError): checked(sign(payload),{})
    with pytest.raises(UpdateError): checked(sign(payload),keys,highest_sequence=8)


@pytest.mark.parametrize('value',[{'a':1.2},{'é':1},{'a':float('nan')},{'a':2**60}])
def test_canonical_manifest_rejects_ambiguous_numbers_and_keys(value):
    with pytest.raises(UpdateError): canonical(value)


class Adapter:
    def __init__(self,fail='',recovery_fails=False): self.calls=[];self.fail=fail;self.recovery_fails=recovery_fails
    def preflight(self,*args):self.calls.append('preflight')
    def quiesce(self):self.calls.append('quiesce')
    def install(self,*args):
        self.calls.append('install')
        if self.fail=='install':raise UpdateError('installation interrupted')
        return {'backup':'test-only'}
    def validate(self,*args):
        self.calls.append('validate')
        if self.fail=='validate':raise UpdateError('validation failed')
    def rollback(self,*args):
        self.calls.append('rollback')
        if self.recovery_fails:raise UpdateError('recovery needs external updater')
    def recover(self):self.calls.append('recover')
    def restart(self):self.calls.append('restart')


def test_shared_installation_gate_requires_approval_before_any_side_effect():
    adapter=Adapter()
    with pytest.raises(UpdateError): apply_update(adapter,Release('example','1.0.0','','',()),None,approved=False)
    assert not adapter.calls


@pytest.mark.parametrize('failure,expected',[('', ['preflight','quiesce','install','validate','restart']),
    ('install',['preflight','quiesce','install','recover','restart']),
    ('validate',['preflight','quiesce','install','validate','rollback','restart'])])
def test_shared_installation_and_recovery_order(failure,expected):
    adapter=Adapter(failure)
    if failure:
        with pytest.raises(UpdateError):apply_update(adapter,Release('example','1.0.0','','',()),None,approved=True)
    else:apply_update(adapter,Release('example','1.0.0','','',()),None,approved=True)
    assert adapter.calls==expected


def test_failed_recovery_does_not_restart_partial_installation():
    adapter=Adapter('validate',True)
    with pytest.raises(UpdateError): apply_update(adapter,Release('example','1.0.0','','',()),None,approved=True)
    assert adapter.calls[-1]=='rollback' and 'restart' not in adapter.calls


def test_signed_discovery_backoff_preserves_cached_release_but_rechecks_expiration():
    payload,sign,keys=signed_manifest()
    state={};now=[datetime(2026,10,22,tzinfo=timezone.utc).timestamp()];calls=[]
    source=SimpleNamespace(policy=POLICY)
    def load(url):calls.append(url);return sign(payload)
    source.json=load
    store=SimpleNamespace(get=lambda key,default=None:state.get(key,default),set=lambda key,value:state.update({key:value}))
    client=SignedDiscovery('birthright.example','stable','https://publisher.example/releases/manifest.json',keys,source,store,clock=lambda:now[0])
    result=client.check();assert not result['offline'] and result['highest_sequence']==7
    client.check();assert len(calls)==1
    source.json=lambda *_:(_ for _ in ()).throw(OSError('offline'))
    failed=client.check(force=True)
    assert failed['offline'] and failed['release']['version']=='1.2.0'
    assert failed['next_check']==now[0]+300
    assert client.for_installation().version=='1.2.0'
    now[0]=datetime(2026,11,2,tzinfo=timezone.utc).timestamp()
    with pytest.raises(UpdateError,match='expired'):client.for_installation()
