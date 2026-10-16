"""Black Scripting API, compatibility and real media-chain integration checks."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forge.app import create_app
from forge.black_scripting import Script, from_legacy
from forge.models import Workflow
from forge.worker import probe
from test_forge import BASE, TOKEN, client, live, tone, wait_job


def definition(*nodes, name='Preparation'):
    values = [{'id': f'operation-{index + 1}', 'type': 'operation', 'node_version': 1,
               'tool': node.get('tool', 'metadata'), 'enabled': node.get('enabled', True),
               'options': node.get('options', {}), 'preset_name': node.get('preset_name', '')}
              for index, node in enumerate(nodes)]
    return {'schema_version': 1, 'id': 'test-script', 'name': name, 'nodes': values,
            'connections': [{'source': a['id'], 'target': b['id']} for a, b in zip(values, values[1:])],
            'output': {'mode': 'source'}}


def register(client, source):
    response = client.post('/api/register', json={'paths': [str(source)]})
    assert response.status_code == 200, response.text
    return response.json()['sources'][0]['id']


def preview(client, script, sources):
    body = {'script': script, 'sources': sources}
    response = client.post('/api/black-scripting/preview', json=body)
    assert response.status_code == 200, response.text
    return {**body, 'preview_token': response.json()['preview_token']}, response.json()


def test_old_presets_project_without_mutation_and_persist_after_restart(client, tmp_path):
    original = Workflow(name='Old saved preparation', steps=[{'tool': 'metadata'}, {'tool': 'transcribe'}]).model_dump()
    response = client.post('/api/workflows/save', json=original)
    assert response.status_code == 200
    store = client.app.state.store
    before = store.get('workflows')
    first = client.get('/api/black-scripting/scripts').json()
    assert len(first['scripts']) == 1 and not first['warnings']
    converted = first['scripts'][0]
    assert converted['id'] == from_legacy(original).id
    assert [node['tool'] for node in converted['nodes']] == ['metadata', 'transcribe']
    assert store.get('workflows') == before
    assert store.get('black_scripting') is None  # Reading does not migrate the database.
    converted['name'] = 'Renamed Black Scripting preset'
    response = client.post('/api/black-scripting/save', json=converted)
    assert response.status_code == 200
    assert len(response.json()['scripts']) == 1
    assert store.get('workflows') == before
    # Opening the same data using a new app preserves both old and new definitions.
    app = create_app(store.data, TOKEN, 9876, workers=False)
    with TestClient(app, base_url=BASE, headers={'X-Forge-Token': TOKEN}) as reopened:
        assert reopened.get('/api/black-scripting/scripts').json()['scripts'][0]['name'] == converted['name']
        assert reopened.get('/api/state').json()['workflows'] == before
    app.state.store.close()


def test_invalid_legacy_data_is_preserved_and_disclosed(client):
    values = [{'name': 'Unrecognized old preset', 'steps': [{'tool': 'shell'}]}]
    client.app.state.store.set('workflows', values)
    result = client.get('/api/black-scripting/scripts').json()
    assert result['scripts'] == [] and len(result['warnings']) == 1
    assert client.app.state.store.get('workflows') == values


def test_script_preset_snapshot_rename_duplicate_and_json_roundtrip(client):
    script = definition({'tool': 'retune', 'options': {'target_hz': 432}, 'preset_name': 'My tuning'})
    preset = {'tool': 'retune', 'name': 'My tuning', 'options': {'target_hz': 432}, 'output': {'mode': 'source'}}
    assert client.post('/api/presets', json=preset).status_code == 200
    saved = client.post('/api/black-scripting/save', json=script).json()['script']
    preset['options']['target_hz'] = 440
    assert client.post('/api/presets', json=preset).status_code == 200
    stored = client.get('/api/black-scripting/scripts').json()['scripts'][0]
    assert stored['nodes'][0]['options']['target_hz'] == 432
    stored['name'] = 'Renamed'
    assert client.post('/api/black-scripting/save', json=stored).status_code == 200
    exported = client.post('/api/black-scripting/export', json=stored).json()['definition']
    assert 'sources' not in exported
    imported = client.post('/api/black-scripting/import', json={'definition': json.loads(json.dumps(exported))})
    assert imported.status_code == 200, imported.text
    assert imported.json()['script']['id'] != stored['id']
    assert imported.json()['script']['nodes'] == saved['nodes']
    assert len(imported.json()['scripts']) == 2


def test_legacy_import_is_additive(client):
    legacy = {'name': 'Legacy import', 'steps': [{'tool': 'retune', 'options': {'format': 'flac'}}]}
    response = client.post('/api/black-scripting/import', json={'definition': legacy})
    assert response.status_code == 200, response.text
    assert response.json()['script']['nodes'][0]['options']['format'] == 'flac'
    assert client.app.state.store.get('workflows', []) == []


@pytest.mark.parametrize('mutation', [
    lambda script: script.update(schema_version=2),
    lambda script: script.update(command='ffmpeg --arbitrary'),
    lambda script: script['nodes'][0].update(id='../outside'),
    lambda script: script['nodes'][0].update(node_version=9),
    lambda script: script['nodes'][0].update(type='shell'),
    lambda script: script['nodes'][0].update(tool='download_audio'),
    lambda script: script['nodes'][0].update(options={'executable': 'arbitrary'}),
    lambda script: script['nodes'][1].update(id=script['nodes'][0]['id']),
    lambda script: script['connections'].append({'source': 'operation-2', 'target': 'operation-1'}),
    lambda script: script['connections'].append({'source': 'operation-1', 'target': 'operation-3'}),
    lambda script: script['connections'][0].update(target='unknown'),
    lambda script: script['connections'].clear(),
    lambda script: script['nodes'][0].update(tool='transcribe'),
])
def test_unsafe_or_non_linear_import_rejected_without_writes(client, mutation):
    script = definition({'tool': 'metadata'}, {'tool': 'metadata'})
    mutation(script)
    response = client.post('/api/black-scripting/import', json={'definition': script})
    assert response.status_code in {400, 422}, response.text
    assert client.app.state.store.get('black_scripting', []) == []
    assert not client.app.state.store.jobs()


def test_empty_and_all_disabled_presets_save_but_cannot_run(client, tmp_path):
    source_id = register(client, tone(tmp_path / 'source.wav', seconds=.1))
    for script in [definition(), definition({'tool': 'metadata', 'enabled': False})]:
        assert client.post('/api/black-scripting/save', json=script).status_code == 200
        response = client.post('/api/black-scripting/preview', json={'script': script, 'sources': [source_id]})
        assert response.status_code == 400 and 'enable' in response.text
    assert not client.app.state.store.jobs()


def test_disabled_and_reordered_operations_have_correct_dependencies(client, tmp_path):
    script = definition({'tool': 'retune', 'options': {'format': 'flac'}},
                        {'tool': 'transcribe', 'enabled': False}, {'tool': 'metadata'})
    source_id = register(client, tone(tmp_path / 'source.wav', seconds=.1))
    body, plan = preview(client, script, [source_id, source_id])
    assert plan['jobs'] == 2 and plan['disabled'] == ['operation-2']
    assert plan['sources'][0]['steps'][1]['depends_on'] == 'operation-1'
    assert plan['sources'][0]['steps'][1]['input_format'] == '.flac'
    assert not client.app.state.store.jobs()
    response = client.post('/api/black-scripting/run', json=body)
    assert response.status_code == 200, response.text
    jobs = response.json()['jobs']
    assert jobs[1]['depends_on'] == jobs[0]['id']
    assert [job['tool'] for job in jobs] == ['retune', 'metadata']
    assert [job['step'] for job in jobs] == [1, 2]
    reordered = copy.deepcopy(script)
    reordered['nodes'].reverse()
    ids = [node['id'] for node in reordered['nodes']]
    reordered['connections'] = [{'source': a, 'target': b} for a, b in zip(ids, ids[1:])]
    _, plan = preview(client, reordered, [source_id])
    assert [step['tool'] for step in plan['sources'][0]['steps']] == ['metadata', 'retune']


def test_media_compatibility_validates_entire_batch_before_queue(client, tmp_path):
    from PIL import Image
    audio = register(client, tone(tmp_path / 'voice.wav', seconds=.1))
    image = tmp_path / 'picture.png'
    Image.new('RGB', (8, 8)).save(image)
    script = definition({'tool': 'retune'})
    response = client.post('/api/black-scripting/preview', json={'script': script, 'sources': [audio, register(client, image)]})
    assert response.status_code == 400 and 'cannot accept' in response.text
    assert not client.app.state.store.jobs()


def test_changed_plan_and_source_rejected_before_queue(client, tmp_path):
    source = tone(tmp_path / 'voice.wav', seconds=.1)
    script = definition({'tool': 'retune'})
    body, _ = preview(client, script, [register(client, source)])
    altered = copy.deepcopy(body)
    altered['script']['nodes'][0]['options']['target_hz'] = 440
    assert client.post('/api/black-scripting/run', json=altered).status_code == 400
    source.write_bytes(source.read_bytes() + b'changed')
    assert client.post('/api/black-scripting/run', json=body).status_code == 400
    del body['preview_token']
    assert client.post('/api/black-scripting/run', json=body).status_code == 422
    assert not client.app.state.store.jobs()


def test_protected_api_requires_existing_browser_authorization(client):
    assert client.get('/api/black-scripting/scripts', headers={'X-Forge-Token': ''}).status_code == 401
    script = definition({'tool': 'metadata'})
    assert client.post('/api/black-scripting/save', json=script, headers={'Origin': 'https://attacker.invalid'}).status_code == 403


def test_real_black_scripting_chain_preserves_original_and_routes_published_file(live, tmp_path):
    source = tone(tmp_path / 'source.wav', seconds=.3)
    original = source.read_bytes()
    script = definition({'tool': 'retune', 'options': {'format': 'flac'}},
                        {'tool': 'metadata', 'options': {'mode': 'edit', 'tags': {'title': 'Black Scripting test'}}})
    script['output'] = {'mode': 'custom', 'directory': str(tmp_path / 'results')}
    body, plan = preview(live, script, [register(live, source)])
    response = live.post('/api/black-scripting/run', json=body)
    assert response.status_code == 200, response.text
    first, second = response.json()['jobs']
    a = wait_job(live, first['id'])
    b = wait_job(live, second['id'])
    assert a['status'] == b['status'] == 'completed', (a, b)
    payload = live.app.state.store.job(b['id'])['payload']
    assert payload['source']['path'] == a['outputs'][0]['path']
    output = Path(b['outputs'][0]['path'])
    assert output.is_file() and output.suffix == '.flac'
    tags = {key.lower(): value for key, value in probe(output, {})['format']['tags'].items()}
    assert tags['title'] == 'Black Scripting test'
    assert source.read_bytes() == original
    assert payload['script_id'] == script['id'] and payload['node_id'] == 'operation-2'
    assert len(plan['sources'][0]['steps']) == 2
    # Existing group retry reconstructs this new representation's queue dependencies.
    retry = live.post('/api/batches/' + response.json()['batch_id'] + '/retry', json={})
    assert retry.status_code == 200, retry.text
    rerun = retry.json()['jobs']
    assert rerun[1]['depends_on'] == rerun[0]['id']
    assert wait_job(live, rerun[1]['id'])['status'] == 'completed'
    assert output.is_file() and source.read_bytes() == original
