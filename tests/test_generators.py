from __future__ import annotations

import csv
import io
import json
import re
import string

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from forge.app import create_app
from forge.generators import (RESERVED_DOMAINS, catalog, csv_cell, export_result,
                              generate, validate_options)


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / 'data', 'generator-test-token', 9876, workers=False)
    with TestClient(app, base_url='http://127.0.0.1:9876', headers={'X-Forge-Token': 'generator-test-token'}) as client:
        yield client
    app.state.store.close()


def test_catalog_defaults_all_generate():
    document = catalog()
    assert set(document['tools']) == {'identity', 'obfuscation', 'addresses', 'strings'}
    for tool, spec in document['tools'].items():
        result = generate(tool, spec['defaults'])
        assert result['count'] == 10
        assert not result['persisted']
        assert spec['schema']['additionalProperties'] is False


def test_password_selected_groups_and_exclusions():
    result = generate('strings', {'count': 100, 'length': 24, 'exclude': 'abCD23', 'symbols': '!+?'})
    assert result['secret'] is True
    assert len({row['value'] for row in result['rows']}) == 100
    for row in result['rows']:
        value = row['value']
        assert len(value) == 24
        assert any(c.isupper() for c in value)
        assert any(c.islower() for c in value)
        assert any(c.isdigit() for c in value)
        assert any(c in '!+?' for c in value)
        assert not set(value) & set('abCD23Il1O0o')


@pytest.mark.parametrize('alphabet,allowed', [('hex', '0123456789abcdef'), ('base64url', string.ascii_letters + string.digits + '-_'), ('custom', 'xyZ7')])
def test_selected_alphabets(alphabet, allowed):
    result = generate('strings', {'kind': 'token', 'alphabet': alphabet, 'custom_chars': 'xyZ7', 'exclude_ambiguous': False})
    assert all(set(row['value']) <= set(allowed) for row in result['rows'])


def test_randomness_uses_secrets_module(monkeypatch):
    calls = []
    def pick(alphabet):
        calls.append(alphabet)
        return alphabet[0]
    monkeypatch.setattr('forge.generators.secrets.choice', pick)
    generate('strings', {'kind': 'token', 'count': 1, 'length': 10, 'alphabet': 'hex'})
    assert len(calls) == 10


@pytest.mark.parametrize('kind', ['username', 'localpart', 'email'])
def test_portable_localparts_and_reserved_emails(kind):
    result = generate('strings', {'kind': kind, 'length': 32, 'count': 40, 'domain': 'example.test'})
    for row in result['rows']:
        value = row['value']
        if kind == 'email':
            local, domain = value.split('@')
            assert domain in RESERVED_DOMAINS
        else:
            local = value
        assert re.fullmatch(r'[A-Za-z0-9_-]{32}', local)


def test_custom_pattern_and_fictional_label():
    result = generate('obfuscation', {'pattern': 'AAA-999-aa_X', 'count': 100, 'exclude': 'AB29', 'exclude_ambiguous': False})
    assert result['fictional'] and 'Not an official' in result['notice']
    for row in result['rows']:
        assert re.fullmatch(r'[A-Z]{3}-[0-9]{3}-[a-z]{2}_[A-Z0-9]', row['value'])
        assert not set(row['value']) & set('AB29')
        assert row['fictional'] is True


@pytest.mark.parametrize('kind', ['identifier', 'username', 'password'])
def test_obfuscation_delegates_shared_string_policies(kind):
    result = generate('obfuscation', {'kind': kind, 'length': 16})
    assert all(len(row['value']) == 16 for row in result['rows'])
    assert result['secret'] is (kind == 'password')


@pytest.mark.parametrize('region', ['international', 'anglophone', 'hispanic', 'japanese'])
@pytest.mark.parametrize('kind', ['legal', 'normie', 'pseudonym'])
def test_fictional_identity_formats(region, kind):
    result = generate('identity', {'kind': kind, 'region': region, 'convention': 'initial_family', 'separator': '_', 'suffix_digits': 3})
    assert result['fictional'] is True
    assert all(re.fullmatch(r'[A-Z]\._[A-Za-z]+[0-9]{3}', row['value']) for row in result['rows'])
    assert all(row['fictional'] for row in result['rows'])


@pytest.mark.parametrize('country,pattern', [('US', r'\d{5}'), ('GB', r'[A-Z]{2}\d \d[A-Z]{2}'), ('CA', r'[A-Z]\d[A-Z] \d[A-Z]\d'), ('DE', r'\d{5}'), ('AU', r'\d{4}'), ('JP', r'\d{3}-\d{4}')])
def test_synthetic_country_shaped_addresses(country, pattern):
    result = generate('addresses', {'country': country, 'include_unit': True, 'fields': ['street', 'unit', 'city', 'region', 'postal_code', 'country']})
    assert result['fictional']
    for row in result['rows']:
        assert row['fictional']
        assert re.fullmatch(pattern, row['postal_code'])
        assert row['unit'].startswith('Unit ')
        assert 'Fictional' in row['street'] or 'Fiktive' in row['street']


def test_address_field_selection():
    row = generate('addresses', {'fields': ['city', 'country']})['rows'][0]
    assert set(row) == {'value', 'fictional', 'city', 'country'}
    assert row['value'] == row['city'] + ', ' + row['country']


@pytest.mark.parametrize('tool,options', [
    ('identity', {'count': 0}), ('strings', {'count': 1001}), ('strings', {'count': True}),
    ('strings', {'count': '5'}), ('strings', {'length': 257}), ('strings', {'length': 7}),
    ('strings', {'kind': 'email', 'length': 65}), ('strings', {'kind': 'email', 'domain': 'gmail.com'}),
    ('strings', {'alphabet': 'custom', 'custom_chars': ''}), ('strings', {'symbols': '\n'}),
    ('strings', {'kind': 'username', 'alphabet': 'custom', 'custom_chars': 'a.b'}),
    ('strings', {'lower': False, 'upper': False, 'numbers': False, 'symbols': ''}),
    ('strings', {'exclude': string.ascii_lowercase}), ('strings', {'kind': 'identifier', 'length': 1}),
    ('obfuscation', {'pattern': '$(shell)'}), ('obfuscation', {'pattern': '---'}),
    ('obfuscation', {'pattern': 'AAA', 'exclude': string.ascii_uppercase}),
    ('obfuscation', {'pattern': 'AAA-999', 'exclude': '-'}), ('addresses', {'fields': []}),
    ('addresses', {'country': 'ZZ'}), ('identity', {'rows': ['should never be saved']}),
])
def test_invalid_configuration(tool, options):
    with pytest.raises((ValidationError, ValueError)):
        validate_options(tool, options)


def test_maximum_bounded_batch():
    result = generate('strings', {'kind': 'token', 'count': 1000, 'length': 256})
    assert len(result['rows']) == 1000
    assert sum(len(row['value']) for row in result['rows']) == 256000


def test_api_generated_secrets_never_persist(client):
    store = client.app.state.store
    before = client.get('/api/state').json()['jobs']
    response = client.post('/api/generators/generate', json={'tool': 'strings', 'options': {'count': 4}})
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['persisted'] is False
    assert client.get('/api/state').json()['jobs'] == before
    for row in response.json()['rows']:
        for file in store.data.rglob('*'):
            if file.is_file():
                assert row['value'].encode() not in file.read_bytes()


def test_api_requires_existing_auth(client):
    assert client.post('/api/generators/generate', json={'tool': 'strings'}, headers={'X-Forge-Token': ''}).status_code == 401
    assert client.get('/api/generators/catalog').status_code == 200
    assert client.post('/api/generators/generate', json={'tool': 'strings', 'options': {'count': 1001}}).status_code == 422
    assert client.post('/api/generators/generate', json={'tool': 'shell', 'options': {}}).status_code == 422


def test_generator_and_export_share_entitlement_boundary(client):
    class Denied:
        def allows(self, feature_id):
            return False
    client.app.state.entitlements.provider = Denied()
    for route in ('generate', 'export'):
        assert client.post('/api/generators/' + route, json={'tool': 'strings'}).status_code == 403


def test_safe_generator_preset_roundtrip_rename_delete(client):
    store = client.app.state.store
    original = store.get('presets', [])
    body = {'tool': 'strings', 'name': 'Test tokens', 'options': {'kind': 'token', 'length': 64}}
    saved = client.post('/api/generators/presets', json=body)
    assert saved.status_code == 200, saved.text
    preset = saved.json()['preset']
    assert set(preset) == {'id', 'tool', 'name', 'options', 'schema_version'}
    assert set(preset['options']) == set(catalog()['tools']['strings']['defaults'])
    assert client.get('/api/generators/presets').json()['presets'] == [preset]
    response = client.post('/api/generators/presets', json={**body, 'id': preset['id'], 'name': 'Renamed'})
    assert response.status_code == 200
    assert response.json()['preset']['id'] == preset['id']
    assert len(response.json()['presets']) == 1
    assert store.get('presets', []) == original
    assert client.post('/api/generators/presets/delete', json={'id': preset['id']}).json()['presets'] == []
    assert client.post('/api/generators/presets/delete', json={'id': preset['id']}).status_code == 404


def test_presets_reject_generated_values(client):
    body = {'tool': 'strings', 'name': 'Unsafe result', 'options': {'rows': ['generated-secret-value']}}
    assert client.post('/api/generators/presets', json=body).status_code == 422
    assert client.post('/api/generators/presets', json={**body, 'options': {}, 'values': ['generated-secret-value']}).status_code == 422
    assert client.get('/api/generators/presets').json()['presets'] == []


@pytest.mark.parametrize('value', ['=HYPERLINK("example")', '+SUM(1,2)', '-1+2', '@SUM(A1)', '  =1+2', '\ttext', '\rtext', '\ntext'])
def test_csv_formula_injection_escaped(value):
    assert csv_cell(value) == "'" + value
    result = {'rows': [{'value': value}], 'columns': ['value']}
    assert list(csv.reader(io.StringIO(export_result(result, 'csv'))))[1][0] == "'" + value


def test_exports_roundtrip_and_fictional_labels(client):
    result = generate('identity', {'count': 2})
    assert json.loads(export_result(result, 'json')) == result
    assert 'Not an official' in export_result(result, 'txt')
    assert 'fictional' in export_result(result, 'csv').splitlines()[0]
    with pytest.raises(ValueError):
        export_result(result, 'html')
    response = client.post('/api/generators/export', json={'tool': 'strings', 'format': 'csv', 'options': {'count': 2}})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['content-disposition'].endswith('FORGE-strings.csv"')
    assert len(list(csv.reader(io.StringIO(response.text)))) == 3
    assert client.app.state.store.visible_jobs() == []
