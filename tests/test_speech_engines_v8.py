"""Speech adapter contracts without downloading weights or claiming inference."""
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge import speech_engines as engines
from forge import speech_runtime as runtime


def test_offline_catalog_distinguishes_original_onyx_from_installed(tmp_path):
    catalog={v['id']:v for v in engines.catalog(tmp_path)}
    assert not catalog['kokoro']['installed']
    assert catalog['kokoro']['capabilities']['blend']
    assert not catalog['kokoro']['capabilities']['native_emotion']
    assert catalog['ming']['task_controls']['IP']==['voice']
    assert catalog['ming-tiny']['unavailable_reason']
    onyx=engines.voices(tmp_path,'kokoro')[0]
    assert onyx['id']=='am_onyx' and onyx['name']=='Onyx'
    assert onyx['gender']=='Male' and onyx['language']=='en' and onyx['accent']=='American'
    assert not onyx['installed']


def test_kokoro_inventory_enumerates_all_installed_original_files(tmp_path):
    revision='a'*40;path=tmp_path/'models/speech/kokoro'/revision
    path.mkdir(parents=True)
    (path/'forge-receipt.json').write_text(json.dumps({'repository':engines.SPECS['kokoro']['repository'],
        'revision':revision,'files':{'voices/am_onyx.pt':{},'voices/af_newvoice.pt':{},'voices/jf_newvoice.pt':{},'not-a-voice.pt':{}}}))
    (path.parent.parent/'selected.json').write_text(json.dumps({'kokoro':revision}))
    assert {v['id'] for v in engines.voices(tmp_path,'kokoro')}=={'am_onyx','af_newvoice','jf_newvoice'}


def test_blend_normalization_reproducible_and_language_checked():
    original=[{'voice':'am_onyx','weight':70},{'voice':'am_fenrir','weight':20},{'voice':'am_adam','weight':10}]
    assert engines.normalize_blend(original)==[{'voice':'am_onyx','weight':.7},{'voice':'am_fenrir','weight':.2},{'voice':'am_adam','weight':.1}]
    assert original[0]['weight']==70
    with pytest.raises(ValueError,match='language'):
        engines.normalize_blend([{'voice':'am_onyx','weight':1},{'voice':'jf_alpha','weight':1}])


@pytest.mark.parametrize('weight',[float('inf'),float('nan'),-1,True,101,'70'])
def test_invalid_blend_weights_rejected(weight):
    with pytest.raises(ValueError):engines.normalize_blend([{'voice':'am_onyx','weight':weight}])


def test_zero_and_duplicate_blends_rejected():
    with pytest.raises(ValueError):engines.normalize_blend([{'voice':'am_onyx','weight':0}])
    with pytest.raises(ValueError):engines.normalize_blend([{'voice':'am_onyx','weight':1},{'voice':'am_onyx','weight':1}])


def test_blend_and_selected_phonemizer_language_must_match():
    with pytest.raises(ValueError,match='phonemizer'):
        engines.validate_options({'engine':'kokoro','voice':'am_onyx','blend':[{'voice':'jf_alpha','weight':1}]})


def test_uninstalled_voice_frontends_are_not_advertised_as_ready(tmp_path):
    onyx=engines.voices(tmp_path,'kokoro')[0]
    assert not onyx['synthesis_ready'] and 'Install' in onyx['availability_reason']
    assert engines.environment_path(tmp_path,'kokoro',device='cpu')!=engines.environment_path(tmp_path,'kokoro',device='cuda')


@pytest.mark.parametrize('options',[
    {'engine':'kokoro','emotion':'angry'}, {'engine':'kokoro','reference_audio':'/tmp/a.wav'},
    {'engine':'kokoro','instructions':'sound angry'}, {'engine':'kokoro','speed':float('nan')},
    {'engine':'ming','task':'IP','voice':'US_James','emotion':'happy'},
    {'engine':'ming','task':'FREEFORM','instructions':'warm','voice':'US_James'},
    {'engine':'ming','task':'VOICE_DESIGN','instructions':'warm','reference_audio':'/tmp/a.wav'},
    {'engine':'ming','task':'EMOTION','reference_audio':'/tmp/a.wav','emotion':'happy','consent':False},
    {'engine':'ming','task':'EMOTION','reference_audio':'/tmp/a.pkl','emotion':'happy','consent':True},
    {'engine':'ming','task':'TTS','reference_audio':'/tmp/a.wav','reference_text':'yes','language':'de','consent':True},
    {'engine':'ming','task':'MULTILINGUAL','reference_audio':'/tmp/a.wav','reference_text':'yes','language':'en','consent':True},
    {'engine':'ming','task':'IP','voice':'US_James','blend':[{'voice':'am_onyx','weight':1}]},
    {'engine':'ming-tiny'}, {'engine':'qwen3-tts'},
])
def test_incompatible_native_controls_and_unavailable_engines_fail_closed(options):
    with pytest.raises(ValueError):engines.validate_options(options)


def test_supported_modes_and_normal_neutral_defaults(tmp_path):
    assert engines.validate_options({'engine':'kokoro','emotion':'neutral'})['voice']=='am_onyx'
    assert engines.validate_options({'engine':'ming','task':'voice','voice':'US_James','emotion':'neutral'})['task']=='IP'
    assert engines.validate_options({'engine':'ming','task':'FREEFORM','instructions':'warm'})['task']=='FREEFORM'
    assert engines.validate_options({'engine':'ming','task':'BASIC','reference_audio':str(tmp_path/'voice.wav'),
        'consent':True,'attribute_speed':'slow','attribute_pitch':'low','attribute_volume':'medium'})['task']=='BASIC'


def test_ming_catalog_only_invocation_names_no_inferred_sensitive_traits():
    text='''### Chinese voices without emotion variants
| ip | Gender | Age | Suggested roles | Voice description | Suggested uses |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Alpha | Male | Adult | narrator | Published description | books |
### Chinese voices with emotion variants
| Base voice | Gender | Age | Suggested roles | Voice description | Uses | Emotion / invocation name |
| Base | Female | Adult | narrator | Description | books | Happy: `Base_happy`<br/>Sad: `Base_sad` |
'''
    voices=engines.parse_ming_voice_catalog(text)
    assert {v['id'] for v in voices}=={'Alpha','Base_happy','Base_sad'}


def test_runtime_fingerprint_changes_without_replacing_existing_environment(tmp_path,monkeypatch):
    root=tmp_path/'app';(root/'scripts').mkdir(parents=True)
    requirement=root/'scripts/requirements-speech-kokoro.txt';requirement.write_text('kokoro==0.9.4')
    monkeypatch.setattr(engines,'ROOT',root)
    before=engines.environment_path(tmp_path,'kokoro')
    before.mkdir(parents=True);(before/'preserved').write_text('old dependency environment')
    requirement.write_text('kokoro==0.9.5')
    after=engines.environment_path(tmp_path,'kokoro')
    assert before!=after and (before/'preserved').read_text()=='old dependency environment'


def test_model_receipt_rechecks_content_and_rejects_paths(tmp_path):
    (tmp_path/'voice.pt').write_bytes(b'not executed')
    receipt={'revision':'b'*40,'files':{'voice.pt':{'size':12,'sha256':hashlib.sha256(b'not executed').hexdigest()}}}
    (tmp_path/'forge-receipt.json').write_text(json.dumps(receipt))
    assert runtime.verify_model(tmp_path)['revision']=='b'*40
    (tmp_path/'voice.pt').write_bytes(b'bad content!')
    with pytest.raises(ValueError,match='integrity'):runtime.verify_model(tmp_path)
    receipt['files']={'../private':{'size':1,'sha256':'a'*64}}
    (tmp_path/'forge-receipt.json').write_text(json.dumps(receipt))
    with pytest.raises(ValueError,match='Unsafe'):runtime.verify_model(tmp_path)


def test_download_verifies_all_files_and_keeps_original_onyx(tmp_path,monkeypatch):
    content={'config.json':b'{}','kokoro-v1_0.pth':b'model data','voices/am_onyx.pt':b'original Onyx','voices/af_extra.pt':b'extra voice'}
    class API:
        def model_info(self,repo,revision,files_metadata):
            assert repo=='hexgrad/Kokoro-82M' and files_metadata
            return SimpleNamespace(sha='c'*40,siblings=[SimpleNamespace(rfilename=name,size=len(data),lfs={'sha256':hashlib.sha256(data).hexdigest()}) for name,data in content.items()])
    def download(repo_id,filename,revision,local_dir,etag_timeout):
        assert revision=='c'*40
        path=Path(local_dir)/filename;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content[filename]);return str(path)
    monkeypatch.setitem(sys.modules,'huggingface_hub',SimpleNamespace(HfApi=API,hf_hub_download=download))
    result=runtime.download_model({'engine':'kokoro','data':str(tmp_path),'revision':''})
    assert result['model_revision']=='c'*40
    model=tmp_path/'models/speech/kokoro'/('c'*40)
    assert (model/'voices/am_onyx.pt').read_bytes()==b'original Onyx'
    assert not (tmp_path/'models/speech/selected.json').exists() # no activation before runtime check
    assert runtime.verify_model(model)['publisher_blob_verified']
    runtime.activate(tmp_path,'kokoro','c'*40)
    assert engines.resolve_options({'engine':'kokoro'},tmp_path)['model_revision']=='c'*40


def test_install_requires_explicit_approval_and_does_not_create_env(tmp_path):
    with pytest.raises(ValueError,match='Approve'):runtime.install_engine('kokoro',tmp_path)
    assert not (tmp_path/'environments').exists()


def test_no_model_or_runtime_is_synthesized_by_missing_dependencies(tmp_path):
    with pytest.raises(RuntimeError,match='Install'):engines.synthesize('hello',{'engine':'kokoro'},tmp_path,tmp_path/'out.wav')
    assert not (tmp_path/'out.wav').exists()
