"""Fail-closed native downloader receipts; fixtures do not claim real execution."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('v9_windows_publisher',ROOT/'scripts/publish_windows_release.py')
publisher=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
SOURCE='a'*40
TOOLING='b'*40


def records(tmp_path,tag='v0.9.0'):
    archive_name,checksum_name,_=publisher.release_names(tag)
    archive=tmp_path/archive_name
    archive.write_bytes(b'Synthetic package-byte fixture; layout acceptance runs separately.')
    digest=publisher.digest(archive)
    (tmp_path/checksum_name).write_text(digest+'  '+archive_name+'\n',encoding='ascii')
    counts={'total':6,'passed':5,'skipped':1,'failed':0,'errors':0}
    linux={'status':'passed','tag':tag,'source_commit':SOURCE,'tooling_commit':TOOLING,
           'archive_name':archive_name,'archive_sha256':digest,'tagged_python_suite':counts,
           'windows_packager_suite':counts,'manifest_validation':'passed',
           'tagged_source_comparison':'passed','protected_paths_excluded':True}
    windows={'status':'passed','native_windows_execution':True,'archive_name':archive_name,
             'archive_sha256':digest,'updater_sha256':publisher.UPDATER_SHA256,
             'checks':['Native installation','Native rejection'],'passed':2,
             'cases':[{'name':name,'status':'passed'} for name in sorted(publisher.REQUIRED_CASES)],
             'installed_app_smoke':{'status':'passed','native_windows_execution':True,
                                    'expected_version':tag[1:],'installed_version':tag[1:],
                                    'checks':['Actual worker smoke'],'passed':1}}
    receipt={'status':'passed','source_commit':SOURCE,'working_tree_clean':True,
             'native_windows_execution':True,'platform':'win32','tests':2,'failed':0,'skipped':0,
             'cases':[{'name':'test_owned_media_transfer','status':'passed'},
                      {'name':'test_actual_resume_and_merge','status':'passed'}],
             'scope':'Owned localhost media and actual FFmpeg/worker transfer checks.',
             'physical_gpu_tested':False,'live_youtube_tested':False,
             'checked':'2026-01-01T00:00:00Z','path':'B:\\private-test-runner',
             'actions_run_id':'12345','yt_dlp':'one-tool-banner'}
    windows['download_acceptance']=receipt
    return linux,windows


def build(tmp_path,tag='v0.9.0',mutate=None):
    linux,windows=records(tmp_path,tag)
    if mutate: mutate(windows)
    return publisher.build_validation(tag,tmp_path,linux,windows)


def test_v9_native_download_receipt_is_stable_and_keeps_original_three_asset_contract(tmp_path):
    linux,windows=records(tmp_path)
    path=publisher.build_validation('v0.9.0',tmp_path,linux,windows)
    original=path.read_bytes()
    proof=json.loads(original)
    receipt=proof['download_acceptance']
    assert set(receipt)=={'status','source_commit','working_tree_clean','native_windows_execution','platform',
                          'tests','failed','skipped','cases','scope','physical_gpu_tested','live_youtube_tested'}
    assert receipt['source_commit']==SOURCE and receipt['tests']==len(receipt['cases'])==2
    assert receipt['physical_gpu_tested'] is False and receipt['live_youtube_tested'] is False
    assert 'private-test-runner' not in original.decode()
    assert sorted(path.name for path in tmp_path.iterdir())==sorted(publisher.release_names('v0.9.0'))
    windows['download_acceptance'].update(checked='2030-01-01T00:00:00Z',path='C:\\different-private-path',
                                         actions_run_id='99999',yt_dlp='another-tool-banner')
    windows['download_acceptance']['cases'].reverse()
    publisher.build_validation('v0.9.0',tmp_path,linux,windows)
    assert path.read_bytes()==original


@pytest.mark.parametrize('tag',['v0.9.0','v0.10.0','v1.0.0'])
def test_new_release_semantic_versions_require_download_evidence(tmp_path,tag):
    with pytest.raises(publisher.PublicationError,match='download acceptance is missing'):
        build(tmp_path,tag,lambda windows:windows.pop('download_acceptance'))
    assert not (tmp_path/publisher.release_names(tag)[2]).exists()


@pytest.mark.parametrize('tag',['v0.5.0','v0.7.99','v0.8.99'])
def test_historical_release_tags_keep_their_original_evidence_contract(tmp_path,tag):
    path=build(tmp_path,tag,lambda windows:windows.pop('download_acceptance'))
    proof=json.loads(path.read_text())
    assert 'download_acceptance' not in proof
    publisher.verify_existing_validation(proof,proof)


@pytest.mark.parametrize('field,value',[
    ('status','failed'),('source_commit','d'*40),('working_tree_clean',False),
    ('working_tree_clean',1),('native_windows_execution',False),('native_windows_execution',1),
    ('platform','linux'),('tests',0),('tests',True),('tests',3),
    ('failed',1),('failed',False),('skipped',1),('skipped',False),
    ('cases',[]),('scope',''),('physical_gpu_tested',True),('live_youtube_tested',True),
])
def test_incomplete_foreign_skipped_or_non_native_download_evidence_fails_closed(tmp_path,field,value):
    with pytest.raises(publisher.PublicationError):
        build(tmp_path,mutate=lambda windows:windows['download_acceptance'].update({field:value}))
    assert not (tmp_path/publisher.release_names('v0.9.0')[2]).exists()


@pytest.mark.parametrize('change',[
    lambda cases:cases[0].update(status='failed'),
    lambda cases:cases[0].update(status='skipped'),
    lambda cases:cases[0].pop('name'),
    lambda cases:cases[0].update(name=''),
    lambda cases:cases.__setitem__(0,'not-a-case-object'),
    lambda cases:cases.__setitem__(0,copy.deepcopy(cases[1])),
])
def test_case_counts_and_names_must_describe_distinct_executed_passing_tests(tmp_path,change):
    with pytest.raises(publisher.PublicationError):
        build(tmp_path,mutate=lambda windows:change(windows['download_acceptance']['cases']))


@pytest.mark.parametrize('change',[
    lambda receipt:receipt.pop('download_acceptance'),
    lambda receipt:receipt['download_acceptance'].update(source_commit='c'*40),
    lambda receipt:receipt['download_acceptance'].update(native_windows_execution=False),
    lambda receipt:receipt['download_acceptance'].update(working_tree_clean=False),
    lambda receipt:receipt['download_acceptance'].update(skipped=1),
    lambda receipt:receipt['download_acceptance']['cases'][0].update(name='test_different_scope'),
])
def test_existing_v9_asset_cannot_be_retained_with_missing_or_weaker_download_proof(tmp_path,change):
    proof=json.loads(build(tmp_path).read_text())
    existing=copy.deepcopy(proof)
    change(existing)
    with pytest.raises(publisher.PublicationError):
        publisher.verify_existing_validation(existing,proof)


def test_equivalent_original_v9_receipt_remains_valid_after_tooling_identity_changes(tmp_path):
    proof=json.loads(build(tmp_path).read_text())
    original=copy.deepcopy(proof)
    proof['tooling_commit']='c'*40
    publisher.verify_existing_validation(original,proof)
    assert original['tooling_commit']==TOOLING


def test_direct_publication_cannot_skip_required_download_validation(tmp_path):
    path=build(tmp_path)
    proof=json.loads(path.read_text());proof.pop('download_acceptance')
    path.write_text(json.dumps(proof))
    with pytest.raises(publisher.PublicationError,match='download acceptance is missing'):
        # No client method may run before local proof checks. This object has no
        # network/upload methods and is deliberately not a fake passing service.
        publisher.publish(object(),'v0.9.0',tmp_path,path,wait_seconds=0)
