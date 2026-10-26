"""Bark contract/real segmentation tests. Synthetic recognizer tests are not model inference."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import wave

import pytest

from forge import bark_asr as bark


def test_family_and_capability_catalog_is_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(bark, 'urlopen', lambda *a, **kw: pytest.fail('catalog must not make network requests'))
    entries = bark.catalog(tmp_path)
    assert [v['id'] for v in entries] == ['bark-3.0', 'bark-3.0-plus', 'bark-3.0-pro', 'bark-3.0-clean']
    assert all(not v['installed'] and v['download_bytes'] is None for v in entries)
    assert all(v['capabilities']['languages'] == ['en'] and not v['capabilities']['timestamps'] for v in entries)
    assert [v['id'] for v in entries if v['non_verbatim']] == ['bark-3.0-clean']
    assert {v['id']:v['available_revision'] for v in entries} == bark.APPROVED_REVISIONS
    assert set(bark.APPROVED_REVISIONS) == set(bark.MODELS)
    assert all(bark.REVISION.fullmatch(v) for v in bark.APPROVED_REVISIONS.values())


@pytest.mark.parametrize('override', [
    {'model': 'large-v3'}, {'language': 'fr'}, {'task': 'translate'}, {'timestamps': 'word'},
    {'timestamps': 'segment'}, {'formats': ['srt']}, {'formats': ['html']}, {'formats': []},
    {'diarization': True}, {'vocabulary': 'Whisper prompt'}, {'device': 'mps'},
    {'chunk_seconds': 0}, {'chunk_seconds': float('nan')}, {'overlap_seconds': 4},
    {'model_revision': 'main'}, {'style': 'clean'},
])
def test_incompatible_controls_fail_explicitly(override):
    with pytest.raises(ValueError):
        bark.validate_options(override)


def test_defaults_are_text_only_and_input_is_not_mutated():
    source = {'model': 'bark-3.0-plus'}
    result = bark.validate_options(source)
    assert source == {'model': 'bark-3.0-plus'}
    assert result['formats'] == ['txt', 'json'] and result['timestamps'] == 'none'
    assert result['language'] == 'en' and result['engine'] == 'bark'


def test_install_needs_approval_before_network_or_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(bark, '_publisher_info', lambda *a: pytest.fail('no network before approval'))
    with pytest.raises(ValueError, match='approval'):
        bark.install('bark-3.0', tmp_path)
    assert not (tmp_path / 'environments').exists()


def test_offline_install_preserves_existing_data(tmp_path, monkeypatch):
    (tmp_path / 'private.txt').write_text('owner data')
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    with pytest.raises(RuntimeError, match='connection'):
        bark.install('bark-3.0', tmp_path, consent=True)
    assert (tmp_path / 'private.txt').read_text() == 'owner data'


@pytest.mark.parametrize('model', list(bark.MODELS))
def test_default_install_selects_approved_commit_before_any_artifact_download(tmp_path, monkeypatch, model):
    monkeypatch.delenv('HF_HUB_OFFLINE', raising=False)
    calls=[]
    def publisher(repo, revision):
        calls.append((repo,revision))
        return {'sha':revision,'siblings':[]}
    def boundary(repo, revision, destination, **kwargs):
        assert revision==bark.APPROVED_REVISIONS[model]
        raise RuntimeError('Synthetic artifact boundary; no inference or download performed.')
    monkeypatch.setattr(bark,'_publisher_info',publisher)
    monkeypatch.setattr(bark,'_snapshot',boundary)
    with pytest.raises(RuntimeError,match='Synthetic artifact boundary'):
        bark.install(model,tmp_path,consent=True)
    assert calls==[('ConwayResearch/'+bark.MODELS[model][0],bark.APPROVED_REVISIONS[model])]
    assert not (tmp_path/'environments').exists()


def test_advanced_approved_commit_is_retained_and_publisher_cannot_substitute_sha(tmp_path, monkeypatch):
    monkeypatch.delenv('HF_HUB_OFFLINE', raising=False)
    requested='f'*40
    calls=[]
    def changed(repo,revision):
        calls.append(revision)
        return {'sha':'a'*40,'siblings':[]}
    monkeypatch.setattr(bark,'_publisher_info',changed)
    monkeypatch.setattr(bark,'_snapshot',lambda *a,**k:pytest.fail('Different publisher revision must fail before artifact writes'))
    with pytest.raises(ValueError,match='requested immutable'):
        bark.install('bark-3.0',tmp_path,revision=requested,consent=True)
    assert calls==[requested]
    assert not list((tmp_path/'models'/'bark'/'bark-3.0').glob('*'))


def test_publisher_metadata_request_is_exact_and_never_accepts_a_moving_branch(monkeypatch):
    import io
    requested=bark.APPROVED_REVISIONS['bark-3.0-pro'];urls=[]
    def opened(request,**kwargs):
        urls.append(request.full_url)
        return io.BytesIO(json.dumps({'sha':requested,'siblings':[]}).encode())
    monkeypatch.setattr(bark,'urlopen',opened)
    assert bark._publisher_info('ConwayResearch/Underdog-Bark-3.0-Pro',requested)['sha']==requested
    assert urls==['https://huggingface.co/api/models/ConwayResearch/Underdog-Bark-3.0-Pro/revision/'+requested+'?blobs=true']
    for value in ('','main','v3'):
        with pytest.raises(ValueError,match='immutable'):
            bark._publisher_info('ConwayResearch/Underdog-Bark-3.0-Pro',value)
    assert len(urls)==1
    monkeypatch.setattr(bark,'urlopen',lambda *a,**k:io.BytesIO(json.dumps({'sha':'0'*40,'siblings':[]}).encode()))
    with pytest.raises(ValueError,match='requested immutable'):
        bark._publisher_info('ConwayResearch/Underdog-Bark-3.0-Pro',requested)


@pytest.mark.parametrize('name', ['loader.py', 'model.pt', 'model.pkl', 'model.pickle', 'sub/model.safetensors', '../model.bark', 'native.dll'])
def test_download_contract_rejects_remote_executable_files(name):
    assert not bark._permitted(name)


def test_data_only_snapshot_checks_sha256_and_immutable_revision(tmp_path, monkeypatch):
    import io
    data = b'synthetic model bytes, not a neural checkpoint'
    revision = 'a'*40
    monkeypatch.setattr(bark, '_publisher_info', lambda *a: {
        'sha': revision, 'siblings': [
            {'rfilename': 'model.bark', 'size': len(data), 'lfs': {'sha256': hashlib.sha256(data).hexdigest()}},
            {'rfilename': 'loader.py', 'size': 1, 'blobId': 'b'*40},
        ]})
    calls = []
    def opened(request, **kwargs):
        calls.append(request.full_url)
        return io.BytesIO(data)
    monkeypatch.setattr(bark, 'urlopen', opened)
    chosen, files = bark._snapshot('ConwayResearch/Underdog-Bark-3.0', revision, tmp_path)
    assert chosen == revision and set(files) == {'model.bark'}
    assert all(revision in url for url in calls)
    assert not (tmp_path / 'loader.py').exists()
    with pytest.raises(ValueError, match='requested immutable'):
        bark._snapshot('ConwayResearch/Underdog-Bark-3.0', 'c'*40, tmp_path)


def test_bad_publisher_checksum_never_becomes_selected_model(tmp_path, monkeypatch):
    import io
    monkeypatch.setattr(bark, '_publisher_info', lambda *a: {'sha': 'a'*40, 'siblings': [
        {'rfilename': 'model.bark', 'size': 5, 'lfs': {'sha256': '0'*64}}]})
    monkeypatch.setattr(bark, 'urlopen', lambda *a, **kw: io.BytesIO(b'wrong'))
    with pytest.raises(ValueError, match='integrity'):
        bark._snapshot('ConwayResearch/Underdog-Bark-3.0', 'a'*40, tmp_path)
    assert not (tmp_path / 'model.bark').exists()
    assert not list(tmp_path.glob('*.download-*'))


def _licensing_fixture(tmp_path, *, notice=None, license_file=False, model_card=True):
    content = {'NOTICE': notice if notice is not None else (
        'Conway Research changes. NVIDIA and Moondream attribution.\n'
        'License: https://creativecommons.org/licenses/by/4.0/\n').encode('utf-8')}
    if model_card:
        content['README.md'] = b'---\nlicense: cc-by-4.0\n---\nPublished model card.'
    if license_file:
        content['LICENSE'] = b'Publisher-provided CC BY 4.0 license text retained exactly.'
    files = {}
    for name, raw in content.items():
        path = tmp_path / name
        path.write_bytes(raw)
        files[name] = {'size': len(raw), 'sha256': bark._hash(path)}
    return files


def test_official_notice_only_layout_retains_verified_license_evidence(tmp_path):
    files = _licensing_fixture(tmp_path)
    result = bark._licensing_evidence({'cardData': {'license': 'cc-by-4.0'}}, tmp_path, files)
    assert result['source'] == 'publisher-model-card-and-notice'
    assert result['license_files'] == [] and result['license'] == 'CC-BY-4.0'
    assert result['notice_sha256'] == bark._hash(tmp_path / 'NOTICE')
    assert result['model_card_sha256'] == bark._hash(tmp_path / 'README.md')
    assert not (tmp_path / 'LICENSE').exists()


def test_publisher_provided_license_is_preserved_and_recorded(tmp_path):
    files = _licensing_fixture(tmp_path, license_file=True)
    before = (tmp_path / 'LICENSE').read_bytes()
    result = bark._licensing_evidence({'cardData': {'license': 'cc-by-4.0'}}, tmp_path, files)
    assert result['source'] == 'publisher-license-file-and-notice'
    assert result['license_files'] == ['LICENSE']
    assert (tmp_path / 'LICENSE').read_bytes() == before


@pytest.mark.parametrize('license_value', [None, 'mit', 'cc-by-nc-4.0', ['cc-by-4.0']])
def test_missing_or_changed_publisher_license_declaration_stops_install(tmp_path, license_value):
    files = _licensing_fixture(tmp_path)
    with pytest.raises(ValueError, match='explicitly declare'):
        bark._licensing_evidence({'cardData': {'license': license_value}}, tmp_path, files)


def test_uninformative_notice_is_not_a_substitute_license_grant(tmp_path):
    files = _licensing_fixture(tmp_path, notice=b'notice')
    with pytest.raises(ValueError, match='explicitly reference'):
        bark._licensing_evidence({'cardData': {'license': 'cc-by-4.0'}}, tmp_path, files)


def test_notice_license_link_must_be_the_exact_license_not_a_prefix(tmp_path):
    files = _licensing_fixture(tmp_path, notice=(b'License: https://creativecommons.org/licenses/by/4.0/unrelated'))
    with pytest.raises(ValueError, match='explicitly reference'):
        bark._licensing_evidence({'cardData': {'license': 'cc-by-4.0'}}, tmp_path, files)


def test_missing_model_card_and_tampered_notice_are_rejected(tmp_path):
    files = _licensing_fixture(tmp_path, model_card=False)
    with pytest.raises(ValueError, match='README.md'):
        bark._licensing_evidence({'cardData': {'license': 'cc-by-4.0'}}, tmp_path, files)
    files = _licensing_fixture(tmp_path)
    (tmp_path / 'NOTICE').write_text('A different declaration cannot replace verified bytes.')
    with pytest.raises(ValueError, match='unverified: NOTICE'):
        bark._licensing_evidence({'cardData': {'license': 'cc-by-4.0'}}, tmp_path, files)


def test_git_blob_identity_is_also_verified(tmp_path, monkeypatch):
    import io
    data = b'{"version":"synthetic fixture"}'
    blob = hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()
    monkeypatch.setattr(bark, '_publisher_info', lambda *a: {'sha': 'a'*40, 'siblings': [
        {'rfilename': 'config.json', 'size': len(data), 'blobId': blob}]})
    monkeypatch.setattr(bark, 'urlopen', lambda *a, **kw: io.BytesIO(data))
    _, files = bark._snapshot('ConwayResearch/Underdog-Bark-3.0', 'a'*40, tmp_path)
    assert files['config.json']['sha256'] == hashlib.sha256(data).hexdigest()


def _cached(tmp_path):
    model, rev = 'bark-3.0', 'a'*40
    directory = tmp_path / 'models' / 'bark' / model
    file = directory / rev / 'model.bark'
    file.parent.mkdir(parents=True)
    file.write_bytes(b'synthetic checkpoint fixture')
    receipt = {'revision': rev, 'repository': 'ConwayResearch/Underdog-Bark-3.0', 'files': {
        rev + '/model.bark': {'size': file.stat().st_size, 'mtime_ns': file.stat().st_mtime_ns, 'sha256': bark._hash(file)}}}
    bark.json_write(directory / 'selected.json', receipt)
    return file, receipt


def test_corrupted_model_never_used(tmp_path):
    file, receipt = _cached(tmp_path)
    assert bark._verified(tmp_path, 'bark-3.0', rehash=True)
    file.write_bytes(b'corrupt bytes')
    assert not bark._verified(tmp_path, 'bark-3.0', rehash=True)


def test_receipt_escape_is_rejected(tmp_path):
    file, receipt = _cached(tmp_path)
    receipt['files']['../outside'] = next(iter(receipt['files'].values()))
    bark.json_write(file.parents[1] / 'selected.json', receipt)
    assert not bark._verified(tmp_path, 'bark-3.0')


def test_ordered_assembly_preserves_repeated_phrases_without_overlap():
    chunks = [{'index': 0, 'text': 'a repeated phrase'}, {'index': 1, 'text': 'a repeated phrase follows'}]
    assert bark.assemble(chunks) == 'a repeated phrase a repeated phrase follows'
    assert bark.assemble(chunks, overlapped=True) == 'a repeated phrase follows'
    with pytest.raises(ValueError, match='sequence'):
        bark.assemble(chunks[::-1])


def test_short_boundary_words_are_not_deduplicated():
    assert bark.assemble([{'index': 0, 'text': 'yes yes'}, {'index': 1, 'text': 'yes yes again'}], overlapped=True) == 'yes yes yes yes again'


def test_verified_reference_wer():
    assert bark.word_error_rate('one two three', 'one two three') == 0
    assert bark.word_error_rate('one two three', 'one four three') == pytest.approx(1/3)
    assert bark.word_error_rate('one two three', 'one two') == pytest.approx(1/3)
    assert bark.word_error_rate('', '') == 0
    assert bark.word_error_rate('', 'no reference') is None


def test_actual_ffmpeg_long_audio_splitting_is_bounded_and_lossless(tmp_path):
    ffmpeg = bark.executable('ffmpeg', {})
    if not ffmpeg:
        pytest.skip('FFmpeg required for real segmentation test')
    source = tmp_path / 'source.wav'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=17',
                    '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(source)], check=True)
    digest = bark._hash(source)
    chunks = bark.chunk_audio(source, tmp_path / 'chunks', {}, chunk_seconds=5)
    windows = [entry['audio_window'] for entry in chunks]
    assert windows[0][0] == 0 and windows[-1][1] == pytest.approx(17)
    assert all(a[1] == b[0] for a, b in zip(windows, windows[1:]))
    assert all(0 < end-start <= 5 for start, end in windows)
    with wave.open(str(source)) as f:
        expected = f.readframes(f.getnframes())
    actual = bytearray()
    for entry in chunks:
        with wave.open(entry['path']) as f:
            actual.extend(f.readframes(f.getnframes()))
    assert bytes(actual) == expected
    assert bark._hash(source) == digest


def test_actual_overlap_windows_are_declared_without_fake_recognition_timing(tmp_path):
    ffmpeg = bark.executable('ffmpeg', {})
    if not ffmpeg:
        pytest.skip('FFmpeg required')
    source = tmp_path / 'source.wav'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=220:duration=11',
                    '-ar', '16000', '-ac', '1', str(source)], check=True)
    chunks = bark.chunk_audio(source, tmp_path / 'chunks', {}, chunk_seconds=5, overlap_seconds=.5)
    assert all(abs(a['audio_window'][1]-b['audio_window'][0]-.5) < 1/16000 for a, b in zip(chunks, chunks[1:]))
    assert all('start' not in entry and 'end' not in entry for entry in chunks)


def test_synthetic_runtime_export_cannot_invent_timing(tmp_path, monkeypatch):
    _cached(tmp_path)
    monkeypatch.setattr(bark, 'runtime_python', lambda data: Path(sys.executable))
    stage = tmp_path / 'stage'; stage.mkdir()
    def fake_chunks(source, work, settings, **kwargs):
        work.mkdir()
        return [{'index': 0, 'path': str(work/'input.wav'), 'audio_window': [0, 5]}]
    monkeypatch.setattr(bark, 'chunk_audio', fake_chunks)
    def fake_runner(command, **kwargs):
        request = json.loads(Path(command[-1]).read_text())
        bark.json_write(Path(request['result']), {'chunks': [{'index': 0, 'text': 'Synthetic recognized words', 'audio_window': [0, 5]}],
                                                 'device': 'cpu', 'peak_process_memory_bytes': 100})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(bark.subprocess, 'run', fake_runner)
    files, warnings = bark.transcribe(tmp_path/'source.wav', stage, 'transcript', {'model': 'bark-3.0'}, tmp_path, {})
    report = json.loads(next(v for v in files if v.suffix == '.json').read_text())
    assert report['segments'] == [] and report['timing'] == 'unavailable'
    assert report['text'] == 'Synthetic recognized words'
    assert report['chunks'][0]['audio_window'] == [0, 5]
    assert all('start' not in chunk and 'end' not in chunk and 'path' not in chunk for chunk in report['chunks'])
    assert report['model_revision'] == 'a'*40 and report['peak_process_memory_bytes'] == 100
    assert not list(stage.glob('bark-*'))


def test_missing_chunk_does_not_publish_partial_text(tmp_path, monkeypatch):
    _cached(tmp_path)
    monkeypatch.setattr(bark, 'runtime_python', lambda data: Path(sys.executable))
    stage = tmp_path/'stage'; stage.mkdir()
    def fake_chunks(source, work, settings, **kwargs):
        work.mkdir()
        return [{'index': 0, 'path': str(work/'input.wav'), 'audio_window': [0, 5]}]
    monkeypatch.setattr(bark, 'chunk_audio', fake_chunks)
    def fake_runner(command, **kwargs):
        request = json.loads(Path(command[-1]).read_text())
        bark.json_write(Path(request['result']), {'chunks': [], 'device': 'cpu'})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(bark.subprocess, 'run', fake_runner)
    with pytest.raises(RuntimeError, match='every audio chunk'):
        bark.transcribe(tmp_path/'source.wav', stage, 'transcript', {'model': 'bark-3.0'}, tmp_path, {})
    assert not list(stage.iterdir())
