"""Synthetic provider metadata; real persistence and local preview-cache behavior."""
import io
import json
import os
from pathlib import Path
import time

import pytest
from PIL import Image

from forge.core import Store
from forge.download_plans import DownloadPlanner
from forge.download_studio import DownloadStudio, WatchRequest, MediaAction
from forge.queue import QueueManager
from forge.source_probe import normalized
from forge.source_watch import ThumbnailCache, SourceError, approved_thumbnail_url


def item(identity, *, views=None, duration=None, published=None, kind='video'):
    return {'id':identity,'url':'https://www.youtube.com/watch?v='+identity,
            'title':'Synthetic '+identity,'type':kind,'view_count':views,
            'duration':duration,'published':published,'upload_date':'',
            'thumbnail':'https://i.ytimg.com/vi/'+identity+'/default.jpg','availability':'public'}


class FixtureAdapter:
    platform='youtube'
    capabilities={'discovery':True,'polling':True}
    def __init__(self):
        self.items=[item('initial001',views=12,duration=40,published=100)]
        self.title='Synthetic Archive'
        self.error=None
        self.calls=0
    def inspect(self,url,**kwargs):
        self.calls+=1
        if self.error: raise self.error
        return {'source_id':'UC_V09_SYNTHETIC','url':'https://www.youtube.com/channel/UC_V09_SYNTHETIC',
                'title':self.title,'handle':'@synthetic','reported_count':None,'entries':self.items,
                'offset':kwargs.get('offset',0),'has_more':False}


@pytest.fixture
def watched(tmp_path):
    store=Store(tmp_path/'data')
    manager=QueueManager(store)
    planner=DownloadPlanner(store,manager)
    manager.download_planner=planner
    adapter=FixtureAdapter()
    service=DownloadStudio(store,manager,adapter)
    service.destinations.remember(tmp_path/'exports',default=True)
    yield service
    manager.stop_event.set()
    store.close()


def register(service,**config):
    service.register(WatchRequest(url='https://www.youtube.com/@synthetic',config={'initial':'review',**config}))
    return service.sources()[0]


def test_fresh_discovery_is_review_only_and_instant_applies_only_to_future(watched):
    source=register(watched)
    original=watched.planner.get_media('youtube:initial001')
    assert original['state']=='review' and watched.store.jobs()==[]
    assert not (Path(source['config']['directory'])/'YouTube').exists()
    watched.configure(source['id'],{'instant_download':True})
    watched.check_once(source['id'])
    assert watched.planner.get_media('youtube:initial001')['state']=='review'
    watched.adapter.items.append(item('future001',duration=20,published=200))
    watched.check_once(source['id'])
    assert watched.planner.get_media('youtube:future001')['state']=='ready'
    assert watched.planner.get_media('youtube:initial001')['state']=='review'
    assert watched.store.jobs()==[]
    watched.planner.approve_channel_review(source['id'])
    assert watched.planner.get_media('youtube:initial001')['state']=='ready'
    assert watched.store.jobs()==[]


def test_source_folders_are_stable_and_destination_changes_do_not_move_media(watched,tmp_path):
    source=register(watched)
    first=watched.planner.get_media('youtube:initial001')
    expected=Path(source['config']['directory'])/'YouTube'/source['folder']/'Videos'
    assert first['destination']==str(expected)
    expected.mkdir(parents=True)
    saved=expected/'owner-existing.mp4';saved.write_bytes(b'Existing user media stays here.')
    watched.adapter.title='Renamed Synthetic Archive'
    watched.check_once(source['id'])
    assert watched.sources()[0]['folder']==source['folder']
    new_root=tmp_path/'other-parent'
    watched.configure(source['id'],{'directory':str(new_root)})
    watched.adapter.items.append(item('newroot001',duration=1,published=300))
    watched.check_once(source['id'])
    assert watched.planner.get_media('youtube:newroot001')['destination']==str(new_root/'YouTube'/source['folder']/'Videos')
    assert saved.read_bytes()==b'Existing user media stays here.'


def test_untrusted_long_channel_titles_create_portable_single_component_folders(watched):
    watched.adapter.title='../CON:'+('😀'*300)
    source=register(watched)
    folder=source['folder']
    assert len(folder.encode('utf-8'))<=200 and len(folder.encode('utf-16-le'))//2<=255
    assert '/' not in folder and '\\' not in folder and ':' not in folder
    target=Path(source['config']['directory'])/'YouTube'/folder/'Videos'
    target.mkdir(parents=True)
    assert target.is_dir() and target.resolve().is_relative_to(Path(source['config']['directory']).resolve())


def test_filtered_upload_pages_are_bounded_and_unknown_metrics_sort_last(watched):
    watched.adapter.items=[item('known001',views=20,duration=4,published=100),
                           item('known002',views=10,duration=7,published=200),
                           item('unknown001')]
    source=register(watched)
    first=watched.media_page(source['id'],limit=1,order='most_viewed',status='review')
    assert first['total']==3 and first['has_more'] and first['media'][0]['id']=='known001'
    rest=watched.media_page(source['id'],limit=2,offset=1,order='most_viewed')
    assert [value['id'] for value in rest['media']]==['known002','unknown001']
    assert all(value['plan_id'] and value['thumbnail_preview'].startswith('/api/downloads/thumbnail?') for value in rest['media'])
    assert watched.media_page(source['id'],search='known_')['total']==0
    assert watched.media_page(source['id'],search='known002')['total']==1
    watched.queue_media('youtube:known002')
    assert watched.media_page(source['id'],status='ready')['total']==1
    assert watched.store.jobs()==[]


def test_metadata_unknowns_are_not_fabricated_or_sparse_updates_erased(watched):
    source=register(watched)
    watched.adapter.items=[{**item('initial001'),'duration':None,'view_count':None,'published':None}]
    watched.check_once(source['id'])
    value=watched.get('watch_media','youtube:initial001')
    assert value['view_count']==12 and value['duration']==40 and value['published']==100
    assert watched.snapshot()['sources'][0]['reported_count'] is None
    record=normalized({'id':'fixture001','webpage_url':'https://www.youtube.com/watch?v=fixture001',
                       'view_count':float('nan'),'duration':float('inf'),'timestamp':-1})
    assert record['view_count'] is None and record['duration'] is None and record['published'] is None


def test_latest_upload_uses_publication_order_and_survives_older_archive_pages(watched):
    watched.adapter.items=[item('older001',published=100),item('newest001',published=300),
                           item('middle001',published=200)]
    source=register(watched)
    assert source['latest']['id']=='newest001'
    watched.adapter.items=[item('archive001',published=50),item('undated001')]
    watched.check_once(source['id'],archive=True)
    current=watched.get('watch_sources',source['id'])
    assert current['latest']['id']=='newest001' and current['latest']['published']==300
    assert watched.get('watch_media','youtube:archive001')['status']=='review'
    assert watched.store.jobs()==[]


def test_latest_sparse_identity_update_keeps_known_date_and_updates_its_title(watched):
    source=register(watched)
    watched.adapter.items=[{**item('initial001'),'title':'Updated known publication','upload_date':''},
                           item('undated-later')]
    watched.check_once(source['id'])
    latest=watched.get('watch_sources',source['id'])['latest']
    assert latest['id']=='initial001' and latest['published']==100
    assert latest['title']=='Updated known publication'


def test_latest_unknown_dates_remain_unknown_until_a_publication_date_is_reported(watched):
    watched.adapter.items=[item('undated001'),{**item('invalid001'),'upload_date':'20261399'}]
    source=register(watched)
    assert source['latest'] is None
    watched.adapter.items=[{**item('dated001'),'upload_date':'20261023'},
                           {**item('dated002'),'upload_date':'20261022'},item('undated-new')]
    watched.check_once(source['id'])
    latest=watched.get('watch_sources',source['id'])['latest']
    assert latest['id']=='dated001' and latest['upload_date']=='20261023'
    assert latest.get('published') is None


def test_monitor_status_reports_suspended_checks_and_auth_without_leaking_errors(watched):
    source=register(watched)
    # A test server without background workers must not claim live monitoring.
    assert watched.snapshot()['sources'][0]['monitoring_state']=='paused'
    watched.started=True
    assert watched.snapshot()['sources'][0]['monitoring_state']=='monitoring'
    watched.store.set('watch_paused',True)
    public=watched.snapshot()['sources'][0]
    assert public['status']=='paused' and public['next_check'] is None
    watched.store.set('watch_paused',False)
    watched.adapter.error=SourceError({'code':'authentication','message':'Authorized cookies required.','retryable':False})
    watched.check_once(source['id'])
    assert watched.snapshot()['sources'][0]['monitoring_state']=='authentication_required'
    watched.adapter.error=SourceError({'code':'dependency','message':'Repair the supported download engine.','retryable':False})
    watched.check_once(source['id'])
    assert watched.snapshot()['sources'][0]['monitoring_state']=='error'


def test_priority_edits_do_not_retry_authentication_or_release_auto_backlog(watched):
    source=register(watched,instant_download=True,initial='latest')
    watched.adapter.error=SourceError({'code':'authentication','message':'Authorized cookies required.','retryable':False})
    watched.check_once(source['id'])
    calls=watched.adapter.calls
    watched.configure(source['id'],{'priority':'ultra','video_order':'oldest'})
    assert watched.adapter.calls==calls
    assert watched.sources()[0]['status']=='access_required'
    assert watched.planner.admit_one(lambda candidate:True) is None
    assert watched.store.jobs()==[]


def test_retry_policy_changes_apply_to_cached_plans_without_network(watched):
    source=register(watched)
    watched.queue_media('youtube:initial001')
    job=watched.planner.admit_one(lambda candidate:True)
    watched.store.update_job(job['id'],status='failed',message='The source connection timed out.')
    watched.store.update_meta(job['id'],error_retryable=True,error_code='network',finished=time.time()-3600)
    watched.planner.reconcile(force=True)
    assert watched.planner.get_media('youtube:initial001')['state']=='failed'
    assert watched.planner.retry_due()==0
    calls=watched.adapter.calls
    watched.configure(source['id'],{'auto_retry':True})
    assert watched.planner.retry_due()==1
    assert watched.planner.get_media('youtube:initial001')['state']=='ready'
    assert watched.adapter.calls==calls


def test_explicit_live_ignore_retains_discovery_without_scheduling(watched):
    watched.adapter.items=[item('ignoredlive001',kind='live')]
    source=register(watched,instant_download=True,initial='latest',live_action='ignore')
    assert watched.planner.get_media('youtube:ignoredlive001')['state']=='skipped'
    watched.adapter.items=[item('ignoredlive001',kind='replay')]
    watched.check_once(source['id'])
    assert watched.planner.get_media('youtube:ignoredlive001')['state']=='skipped'
    assert watched.store.jobs()==[]


def test_paused_channel_plans_require_explicit_approval_and_live_transition(watched):
    watched.adapter.items=[item('upcoming001',kind='upcoming')]
    source=register(watched,instant_download=True,enabled=False)
    assert watched.planner.get_media('youtube:upcoming001')['state']=='unavailable'
    watched.adapter.items=[item('upcoming001',kind='premiere')]
    watched.check_once(source['id'])
    plan=watched.planner.get_media('youtube:upcoming001')
    assert plan['state']=='ready' and watched.store.jobs()==[]
    assert plan['historical'] is False
    watched.check_once(source['id'])
    assert watched.planner.get_media('youtube:upcoming001')['historical'] is False


def test_refresh_does_not_revoke_explicit_approval_when_channel_is_paused(watched):
    source=register(watched)
    watched.queue_media('youtube:initial001')
    watched.configure(source['id'],{'enabled':False})
    watched.check_once(source['id'])
    plan=watched.planner.get_media('youtube:initial001')
    assert plan['state']=='ready' and plan['approved'] and not plan['automatic']
    admitted=watched.planner.admit_one(lambda job:True)
    assert admitted and watched.media_page(source['id'],status='active')['total']==1


def test_watched_replay_policy_does_not_disable_explicit_manual_live_capture(watched,tmp_path):
    metadata=item('sharedlive001',kind='live')
    watched.planner.upsert({**metadata,'eligible':True},origin='manual_single',approved=True,
        payload={'tool':'download_video','url':metadata['url'],'options':{'format':'mp4','capture_live':True},
                 'settings':watched.store.settings(),'destination':str(tmp_path/'manual')},
        destination=str(tmp_path/'manual'))
    watched.adapter.items=[metadata]
    register(watched,instant_download=True,live_action='replay')
    plan=watched.planner.get_media('youtube:sharedlive001')
    assert plan['state']=='ready' and plan['origin']=='manual_single'
    assert plan['destination']==str(tmp_path/'manual')


def test_existing_v7_source_migration_preserves_approved_automation_and_folder(tmp_path):
    store=Store(tmp_path/'data');manager=QueueManager(store);adapter=FixtureAdapter()
    legacy=DownloadStudio(store,manager,adapter)
    legacy.destinations.remember(tmp_path/'exports',default=True)
    source=register(legacy,initial='future',priority='critical')
    old=legacy.get('watch_sources',source['id'])
    for field in ('instant_download','video_order','backfill_order'): old['config'].pop(field,None)
    legacy.put('watch_sources',source['id'],old)
    before=old['folder']
    planner=DownloadPlanner(store,manager);manager.download_planner=planner
    upgraded=DownloadStudio(store,manager,adapter)
    current=upgraded.sources()[0]
    assert current['config']['instant_download'] is True
    assert current['config']['priority']=='ultra' and current['folder']==before
    assert current['config']['options']==old['config']['options']
    manager.stop_event.set();store.close()


def png(color='gold'):
    output=io.BytesIO();Image.new('RGB',(240,135),color).save(output,format='PNG')
    return output.getvalue()


def test_thumbnail_cache_is_internal_bounded_and_keeps_offline_previews(tmp_path,monkeypatch):
    from forge import source_watch
    calls=[]
    def fetch(url): calls.append(url);return png()
    monkeypatch.setattr(source_watch,'thumbnail_bytes',fetch)
    cache=ThumbnailCache(tmp_path/'data',max_files=2,max_bytes=100000,ttl=10)
    first='https://i.ytimg.com/vi/first/default.jpg'
    assert cache.get(first)==png() and cache.get(first)==png() and len(calls)==1
    old=next(cache.root.glob('*.png'));os.utime(old,(0,0))
    monkeypatch.setattr(source_watch,'thumbnail_bytes',lambda url: (_ for _ in ()).throw(OSError('offline')))
    assert cache.get(first)==png()
    monkeypatch.setattr(source_watch,'thumbnail_bytes',fetch)
    for identity in ('second','third'): cache.get('https://i.ytimg.com/vi/'+identity+'/default.jpg')
    assert len(list(cache.root.glob('*.png')))<=2
    assert list((tmp_path/'data').iterdir())==[cache.root]


@pytest.mark.parametrize('url',['file:///private/photo.png','http://i.ytimg.com/photo.png',
                              'https://i.ytimg.com.attacker.test/photo.png','https://user:secret@i.ytimg.com/photo.png',
                              'https://i.ytimg.com:8443/photo.png','https://127.0.0.1/photo.png'])
def test_thumbnail_proxy_refuses_arbitrary_or_credentialed_sources(tmp_path,monkeypatch,url):
    from forge import source_watch
    monkeypatch.setattr(source_watch,'thumbnail_bytes',lambda value:pytest.fail('Unapproved source was fetched'))
    assert not approved_thumbnail_url(url)
    assert ThumbnailCache(tmp_path/'data').get(url)==b''
    assert not (tmp_path/'data').exists()


def test_thumbnail_endpoint_uses_browser_cookie_and_never_exports_previews(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from forge.app import create_app
    from forge import source_watch
    monkeypatch.setattr(source_watch,'thumbnail_bytes',lambda url:png())
    app=create_app(tmp_path/'data','synthetic-thumbnail-token',19649,workers=False)
    with TestClient(app,base_url='http://127.0.0.1:19649') as client:
        path='/api/downloads/thumbnail?url=https%3A%2F%2Fi.ytimg.com%2Fvi%2Ffixture%2Fdefault.jpg'
        assert client.get(path).status_code==401
        client.headers['X-Forge-Token']='synthetic-thumbnail-token'
        ticket=client.post('/api/launch-ticket').json()['ticket']
        client.headers.pop('X-Forge-Token')
        assert client.post('/api/session',json={'ticket':ticket},headers={'X-Forge-Bootstrap':'1'}).status_code==200
        response=client.get(path)
        assert response.status_code==200 and response.headers['content-type']=='image/png'
        assert response.content==png()
        assert client.get('/api/downloads/thumbnail?url=file%3A%2F%2F%2Fprivate%2Fphoto.png').status_code==404
        assert list((tmp_path/'data'/'thumbnail-cache').glob('*.png'))
        assert not (tmp_path/'exports').exists()
    app.state.store.close()
