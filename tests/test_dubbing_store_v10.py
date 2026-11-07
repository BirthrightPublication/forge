"""Persistent revision and late-worker races over a disposable real SQLite store."""
import copy
import hashlib
import json

import pytest

from forge.core import Store
from forge.dubbing_store import DubbingStore, RevisionConflict, review_fingerprint, segment_fingerprint, stage_fingerprint


@pytest.fixture
def library(tmp_path):
    store=Store(tmp_path/'data')
    value=DubbingStore(store)
    yield value
    store.close()


def project(library):
    return library.save_project({'name':'Synthetic Russian project','source':{'path':'/synthetic/owned-video.mp4','size':20},
        'segments':[{'id':'first','start':0,'end':2,'speaker':'speaker1','source_text':'Hello','text':'Привет','reviewed':True},
                    {'id':'second','start':3,'end':5,'speaker':'speaker1','source_text':'World','text':'Мир','reviewed':True}]},trusted=True)


def test_revisions_require_current_identity_and_reopen_preserves_original_records(library):
    library.store.set('user-owned-fixture',{'presets':['keep'],'models':'unchanged'})
    original=project(library)
    saved=library.save_project({**original,'name':'User edit'},original['revision'])
    with pytest.raises(RevisionConflict):
        library.save_project({**original,'name':'Stale edit'},original['revision'])
    assert library.project(original['id'])['name']=='User edit'
    assert saved['revision']==original['revision']+1
    assert library.store.get('user-owned-fixture')=={'presets':['keep'],'models':'unchanged'}
    # A second library wrapper opens additive tables without Store's process-recovery side effects.
    again=DubbingStore(library.store)
    assert again.project(original['id'])['segments']==original['segments']
    assert library.store.db.execute('SELECT COUNT(*) FROM dubbing_revisions').fetchone()[0]==2


def test_source_and_derived_audio_cannot_be_replaced_by_a_browser_save(library):
    original=project(library)
    with pytest.raises(ValueError,match='original source'):
        library.save_project({**original,'source':{'path':'/different.mp4'}},original['revision'])
    edit=copy.deepcopy(original)
    edit['segments'][0]['audio']={'path':'/private/unselected.wav','stale':True}
    changed=library.save_project(edit,original['revision'])
    assert 'audio' not in changed['segments'][0]


def test_manual_text_edit_invalidates_only_its_segment_media(library):
    original=project(library)
    for row in original['segments']:
        row['audio']={'path':'/synthetic/'+row['id']+'.wav','sha256':row['id'],'stale':False}
    prepared=library.save_project(original,original['revision'],trusted=True)
    edit=copy.deepcopy(prepared)
    edit['segments'][0]['text']='Новый текст'
    saved=library.save_project(edit,prepared['revision'])
    assert saved['segments'][0]['manual_edit'] and not saved['segments'][0]['reviewed']
    assert saved['segments'][0]['audio']['stale'] is True
    assert saved['segments'][1]['audio']==prepared['segments'][1]['audio']
    assert saved['segments'][1]['reviewed'] is True


def test_manual_window_edit_keeps_valid_voice_and_invalidates_derived_timing(library,tmp_path):
    from forge.dubbing_pipeline import subtitles
    original=project(library)
    original['segments'][0]['audio']={'path':'/synthetic/unchanged.wav','sha256':'owned-fixture','stale':False}
    original['segments'][0]['timing']={'start':0,'end':2,'strategy':'manual'}
    saved=library.save_project(original,original['revision'],trusted=True)
    edit=copy.deepcopy(saved);edit['segments'][0].update(start=2,end=3)
    changed=library.save_project(edit,saved['revision'])
    assert changed['segments'][0]['audio']==saved['segments'][0]['audio']
    assert changed['segments'][0]['timing']['stale'] is True
    assert changed['segments'][0]['reviewed'] is False
    output=tmp_path/'owned.srt';subtitles([changed['segments'][0]],output)
    assert '00:00:02,000 --> 00:00:03,000' in output.read_text()


def test_late_worker_preserves_changed_segment_and_applies_unaffected_work(library):
    original=project(library)
    run=library.save_run({'project_id':original['id'],'snapshot':original,'status':'running'})
    user=copy.deepcopy(original);user['segments'][0]['text']='Ручная правка'
    latest=library.save_project(user,original['revision'])
    updates=[]
    for row in original['segments']:
        updates.append({'id':row['id'],'expected_input_hash':segment_fingerprint(row),
                        'audio':{'path':'/synthetic/generated.wav','sha256':'owned-test','duration':1,'stale':False}})
    library.apply_result({'id':'synthetic-job','tool':'dubbing_voice',
                         'payload':{'dubbing':{'project':original,'run_id':run['id']}}},
        {'dubbing_result':{'segment_updates':updates,'stage_settings_hash':stage_fingerprint(original,'dubbing_voice')}})
    after=library.project(original['id'])
    assert after['segments'][0]['text']=='Ручная правка' and 'audio' not in after['segments'][0]
    assert after['segments'][1]['audio']['duration']==1
    assert library.run(run['id'])['results']['dubbing_voice']['stale_segments']==['first']


def test_panel_changes_do_not_change_review_but_voice_or_text_edits_do(library):
    value=project(library);before=review_fingerprint(value)
    value['options']['panels']['left_collapsed']=True
    value['options']['publishing']['title']='Russian publication metadata'
    assert review_fingerprint(value)==before
    value['options']['tts']['speed']=1.1
    assert review_fingerprint(value)!=before


def test_only_registered_verified_artifacts_are_served_and_edits_mark_downstream_stale(library,tmp_path):
    value=project(library)
    output=tmp_path/'owned-fixture.wav';output.write_bytes(b'Synthetic owned artifact; file-serving unit fixture only')
    with pytest.raises(ValueError,match='SHA-256'):
        library.add_artifact(value['id'],{'kind':'mix','path':str(output)})
    artifact=library.add_artifact(value['id'],{'kind':'mix','stage':'dubbing_mix','path':str(output),
                                            'sha256':hashlib.sha256(output.read_bytes()).hexdigest()})
    assert library.artifact(value['id'],artifact['id'])['verified'] is True
    edit=copy.deepcopy(value);edit['segments'][0]['text']='Изменено'
    library.save_project(edit,value['revision'])
    assert library.artifact(value['id'],artifact['id'])['stale'] is True
    assert output.read_bytes().startswith(b'Synthetic')


def test_unapproved_reference_and_credentials_are_rejected(library):
    with pytest.raises(ValueError,match='permission'):
        library.save_record('voices',{'name':'Unapproved voice','consent':False})
    with pytest.raises(ValueError,match='Prepare'):
        library.save_record('voices',{'name':'Unprepared voice','consent':True,'approved':True})
    with pytest.raises(ValueError,match='Credentials'):
        library.save_record('presets',{'name':'Invalid preset','nested':{'refresh_token':'synthetic-never-store'}})


def test_missing_or_reversed_segment_bounds_and_conflicting_ids_are_not_silently_coerced(library):
    original=project(library)
    edit=copy.deepcopy(original);edit['segments'][0]['end']=0
    with pytest.raises(ValueError,match='positive duration'):
        library.save_project(edit,original['revision'])
    edit=copy.deepcopy(original);edit['segments'][1]['id']='first'
    with pytest.raises(ValueError,match='unique'):
        library.save_project(edit,original['revision'])
