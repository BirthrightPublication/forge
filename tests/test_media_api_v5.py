"""Cross-module API contracts and privacy boundaries for the V0.5 Studio."""
import json
from pathlib import Path

from PIL import Image

from forge.core import PRIVACY_TOOLS, TOOLS
from forge.entitlements import EntitlementService
from test_forge import client, tone


def registered(client, path):
    response = client.post('/api/register', json={'paths': [str(path)]})
    assert response.status_code == 200
    return response.json()['sources'][0]['id']


def test_catalog_matches_registry_and_entitlements(client):
    response = client.get('/api/media/privacy/catalog')
    assert response.status_code == 200
    body = response.json()
    assert set(body['tools']) == PRIVACY_TOOLS
    assert len(TOOLS) == 20
    assert {'tts','audio_effects'} <= set(TOOLS)
    assert all(value['schema']['additionalProperties'] is False for value in body['tools'].values())
    state = client.get('/api/state').json()
    assert set(TOOLS) <= {item['key'] for item in state['entitlements']['modules'] if item['enabled']}
    assert body['capabilities']['provenance']['network'] == 'disabled'
    assert response.headers['cache-control'] == 'no-store'


def test_plan_is_read_only_and_discloses_stream_removal(client, tmp_path):
    source = tone(tmp_path/'original.wav', seconds=.2)
    original = source.read_bytes()
    sid = registered(client, source)
    result = client.post('/api/media/privacy/plan', json={'tool':'privacy_export', 'source':sid,
                         'options':{'preset':'advanced','reports':True}})
    assert result.status_code == 200, result.text
    plan = result.json()
    assert plan['output_suffix'] == '.wav' and plan['preserves_original']
    assert any('report' in entry.lower() for entry in plan['disclosures'])
    assert any('provenance' in entry.lower() for entry in plan['disclosures'])
    assert not client.app.state.store.jobs()
    assert source.read_bytes() == original


def test_comparison_jobs_group_sources_and_snapshot_every_input(client, tmp_path):
    a = tone(tmp_path/'a.wav', seconds=.2)
    b = tone(tmp_path/'b.wav', seconds=.2)
    ids = [registered(client, p) for p in [a,b]]
    response = client.post('/api/jobs', json={'tool':'audio_fingerprint','sources':ids,
                 'options':{'report_json':False,'report_html':False},'output':{'mode':'custom','directory':str(tmp_path/'exports')}})
    assert response.status_code == 200, response.text
    assert len(response.json()['jobs']) == 1
    payload = client.app.state.store.job(response.json()['jobs'][0]['id'])['payload']
    assert payload['original_source']['id'] == ids[0]
    assert payload['comparison_sources'][0]['id'] == ids[1]
    assert payload['comparison_sources'][0]['source_stat']['size'] == b.stat().st_size


def test_references_are_explicit_local_json_and_limited(client, tmp_path):
    audio = registered(client, tone(tmp_path/'input.wav', seconds=.1))
    wrong = registered(client, tone(tmp_path/'wrong.wav', seconds=.1))
    body = {'tool':'audio_fingerprint','sources':[audio],'reference_sources':[wrong],
            'output':{'mode':'custom','directory':str(tmp_path/'exports')}}
    response = client.post('/api/jobs', json=body)
    assert response.status_code == 400 and not client.app.state.store.jobs()
    ref = tmp_path/'reference.json';ref.write_text('{}')
    body['reference_sources'] = [registered(client, ref)]
    response = client.post('/api/jobs', json=body)
    assert response.status_code == 200
    payload = client.app.state.store.job(response.json()['jobs'][0]['id'])['payload']
    assert payload['reference_sources'][0]['source_stat']['size'] == 2
    body['tool'] = 'tracking_audit'
    assert client.post('/api/jobs', json=body).status_code == 400


def test_verification_requires_original_and_variant(client, tmp_path):
    sid = registered(client, tone(tmp_path/'original.wav', seconds=.1))
    response = client.post('/api/jobs', json={'tool':'privacy_verify','sources':[sid],
                                            'output':{'mode':'custom','directory':str(tmp_path/'exports')}})
    assert response.status_code == 400 and not client.app.state.store.jobs()


def test_new_presets_keep_exact_user_settings(client):
    options = {'preset':'custom','retain_subtitles':False,'retain_chapters':False,'reports':False}
    response = client.post('/api/presets', json={'tool':'privacy_export','name':'My private export',
                                               'options':options,'output':{'mode':'source'}})
    assert response.status_code == 200
    saved = response.json()['presets'][0]
    assert all(saved['options'][key] == value for key,value in options.items())


def test_content_only_serves_registered_supported_media(client, tmp_path):
    path = tmp_path/'image.png';Image.new('RGB',(12,12),'red').save(path)
    sid = registered(client,path)
    response = client.get('/api/media/privacy/sources/'+sid+'/content')
    assert response.status_code == 200 and response.content == path.read_bytes()
    unsafe = tmp_path/'document.html';unsafe.write_text('<script>wrong</script>')
    uid = registered(client,unsafe)
    assert client.get('/api/media/privacy/sources/'+uid+'/content').status_code == 400
    assert client.get('/api/media/privacy/sources/'+sid+'/content',headers={'X-Forge-Token':'wrong'}).status_code == 401


def test_report_reader_honors_recorded_outputs_and_destination(client, tmp_path):
    out=tmp_path/'output';out.mkdir()
    report=out/'report.json';report.write_text(json.dumps({'status':'detected','private':'local fixture'}))
    external=tmp_path/'outside.json';external.write_text('{"must_not_read":true}')
    store=client.app.state.store
    job=store.add_job('tracking_audit','fixture',{'options':{},'destination':str(out),'source':None})
    store.update_job(job['id'],status='completed',outputs=[{'path':str(report),'name':report.name},
                                                        {'path':str(external),'name':external.name}])
    response=client.get('/api/media/privacy/jobs/'+job['id']+'/report')
    assert response.status_code==200
    assert len(response.json()['reports'])==1 and 'must_not_read' not in response.text
    assert response.headers['cache-control']=='no-store'


def test_disabled_report_is_honest_and_entitlement_checks_apply(client, tmp_path):
    store=client.app.state.store
    job=store.add_job('tracking_audit','fixture',{'options':{'reports':False},'destination':str(tmp_path),'source':None})
    store.update_job(job['id'],status='completed')
    response=client.get('/api/media/privacy/jobs/'+job['id']+'/report')
    assert response.json()['reports']==[] and 'No private report' in response.json()['note']
    class Denied:
        provider_id='test';mode='test'
        def allows(self, feature):return False
    client.app.state.entitlements=EntitlementService(Denied())
    assert client.get('/api/media/privacy/jobs/'+job['id']+'/report').status_code==403
    assert client.post('/api/jobs',json={'tool':'tracking_audit'}).status_code==403
