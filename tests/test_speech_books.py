"""Real document/FFmpeg checks; deterministic audio shim is not model inference."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import wave
import zipfile

import pytest

from forge import speech_books as books


def test_text_import_preserves_original_and_detects_chapters(tmp_path):
    path = tmp_path / 'Book.md'
    path.write_text('# Chapter One\n\nFirst paragraph.\n\n# Chapter Two\n\nSecond paragraph.', encoding='utf-8')
    original = path.read_bytes()
    result = books.import_document(path)
    assert [c['title'] for c in result['chapters']] == ['Chapter One', 'Chapter Two']
    assert 'First paragraph.' in result['text']
    assert path.read_bytes() == original


def test_html_ignores_active_and_external_content(tmp_path):
    path = tmp_path / 'book.html'
    path.write_text('<html><head><title>Private title</title></head><body><h1>Chapter One</h1><script>alert("bad")</script><style>hidden</style><p>Real &amp; readable.</p><iframe src="https://invalid.example">bad</iframe><img src="https://invalid.example/x"></body></html>')
    result = books.import_document(path)
    assert 'Real & readable.' in result['text']
    assert all(word not in result['text'] for word in ['alert', 'Private title', 'hidden', 'invalid.example', 'bad'])


@pytest.mark.parametrize('extension,content', [
    ('srt', '1\n00:00:01,000 --> 00:00:03,000\nHello <i>there</i>.\n\n2\n00:00:04,000 --> 00:00:05,000\nSecond line.'),
    ('vtt', 'WEBVTT\n\nNOTE private note\nnot spoken\n\n00:00.000 --> 00:01.000\nHello <b>there</b>.\n\n00:01.000 --> 00:02.000\nSecond line.'),
])
def test_subtitle_import_preserves_text_without_fake_alignment(tmp_path, extension, content):
    path = tmp_path / ('script.' + extension)
    path.write_text(content)
    result = books.import_document(path)
    assert 'Hello there.' in result['text'] and 'Second line.' in result['text']
    assert '-->' not in result['text'] and 'not spoken' not in result['text']
    assert any('not a TTS alignment' in warning for warning in result['warnings'])


def make_epub(path, extras=None, href='two.xhtml'):
    files = {
        'META-INF/container.xml': '<container><rootfiles><rootfile full-path="OPS/package.opf"/></rootfiles></container>',
        'OPS/package.opf': '<package><metadata><title>Fictional Book</title></metadata><manifest><item id="one" href="one.xhtml" media-type="application/xhtml+xml"/><item id="two" href="' + href + '" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="two"/><itemref idref="one"/></spine></package>',
        'OPS/one.xhtml': '<h1>One</h1><p>The first document.</p>',
        'OPS/two.xhtml': '<h1>Two</h1><p>The second document.</p>',
    }
    files.update(extras or {})
    with zipfile.ZipFile(path, 'w') as archive:
        for name, value in files.items():
            archive.writestr(name, value)


def test_epub_uses_spine_order_without_extracting_files(tmp_path):
    path = tmp_path / 'book.epub'
    make_epub(path)
    before = set(tmp_path.iterdir())
    result = books.import_document(path)
    assert result['name'] == 'Fictional Book'
    assert [c['title'] for c in result['chapters']] == ['Two', 'One']
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize('name', ['../outside.txt', '/absolute.txt', 'OPS\\bad.txt'])
def test_epub_rejects_unsafe_members(tmp_path, name):
    path = tmp_path / 'unsafe.epub'
    make_epub(path, {name: 'bad'})
    with pytest.raises(ValueError, match='Unsafe'):
        books.import_document(path)


@pytest.mark.parametrize('href', ['../escape.xhtml', 'https://example.com/book.xhtml', '%2e%2e/escape.xhtml'])
def test_epub_rejects_external_and_parent_references(tmp_path, href):
    path = tmp_path / 'unsafe.epub'
    make_epub(path, href=href)
    with pytest.raises(ValueError, match='External|Unsafe'):
        books.import_document(path)


def test_epub_rejects_xml_entities(tmp_path):
    path = tmp_path / 'entity.epub'
    make_epub(path, {'META-INF/container.xml': '<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><container>&x;</container>'})
    with pytest.raises(ValueError, match='entities'):
        books.import_document(path)


def test_unsupported_and_nontext_import_errors(tmp_path):
    path = tmp_path / 'image.png'
    path.write_bytes(b'not a document')
    with pytest.raises(ValueError, match='Supported documents'):
        books.import_document(path)
    empty = tmp_path / 'empty.txt'
    empty.write_text('  ')
    with pytest.raises(ValueError, match='no extractable text'):
        books.import_document(empty)


def test_real_pdf_extracts_text_without_ocr(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 72 700 Td (Synthetic PDF narration.) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    writer.add_metadata({'/Title': 'Synthetic PDF book'})
    path = tmp_path / 'book.pdf'
    writer.write(path)
    result = books.import_document(path)
    assert result['name'] == 'Synthetic PDF book'
    assert 'Synthetic PDF narration.' in result['text']
    assert any('OCR was not performed' in warning for warning in result['warnings'])


@pytest.fixture
def fake_model(monkeypatch):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Actual FFmpeg tools required.')
    from forge import speech_engines
    calls = []
    def resolve(options, data):
        return {**options, 'model_revision': options.get('model_revision') or 'a' * 40}
    def synthesize(text, options, data, output, progress):
        calls.append((text, options))
        assert '[[' not in text
        samples = [round(5000 * math.sin(2 * math.pi * (220 + len(text)) * n / 24000)) for n in range(2400)]
        with wave.open(str(output), 'wb') as audio:
            audio.setparams((1, 2, 24000, len(samples), 'NONE', 'not compressed'))
            audio.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
        return {'engine': options.get('engine', 'kokoro'), 'model_revision': options['model_revision'], 'sample_rate': 24000, 'test_scope': 'deterministic tone shim, not TTS model'}
    monkeypatch.setattr(speech_engines, 'resolve_options', resolve)
    monkeypatch.setattr(speech_engines, 'synthesize', synthesize)
    return calls


def run_process(tmp_path, options, project=None, name='render'):
    stage = tmp_path / name
    stage.mkdir()
    return books.process({'options': options, 'settings': {}, 'speech_project': project or {}}, stage, tmp_path / 'data')


def test_manual_silence_is_exact_and_markup_never_reaches_engine(tmp_path, fake_model):
    files, warnings = run_process(tmp_path, {'text': 'Hello[[pause:750ms]]world', 'engine': 'kokoro'})
    assert [text for text, _ in fake_model] == ['Hello', 'world']
    audio = next(path for path in files if path.suffix == '.wav')
    assert books._wave_info(audio) == pytest.approx(.95, abs=1 / 24000)
    with wave.open(str(audio), 'rb') as rendered:
        frames = struct.unpack('<' + 'h' * rendered.getnframes(), rendered.readframes(rendered.getnframes()))
    assert set(frames[2400:20400]) == {0}


def test_long_unpunctuated_prose_has_bounded_ordered_inference(tmp_path, fake_model):
    text = ' '.join(f'word{index:04d}' for index in range(1200))
    run_process(tmp_path, {'text': text, 'engine': 'kokoro'})
    assert all(len(part) <= 4000 for part, options in fake_model)
    assert ' '.join(part for part, options in fake_model) == text.strip()


def test_unchanged_sentences_reuse_verified_cache_after_edit(tmp_path, fake_model):
    options = {'text': 'First sentence. Second sentence.', 'engine': 'kokoro'}
    run_process(tmp_path, options, name='first')
    assert len(fake_model) == 2
    files, _ = run_process(tmp_path, {**options, 'text': 'Changed sentence. Second sentence.'}, name='second')
    assert len(fake_model) == 3
    report = json.loads(next(path for path in files if path.suffix == '.json').read_text())
    assert [segment['reused'] for segment in report['segments']] == [False, True]
    assert all(segment['inference']['model_revision'] == 'a' * 40 for segment in report['segments'])


def test_corrupted_cache_cannot_be_reused(tmp_path, fake_model):
    options = {'text': 'Saved segment', 'engine': 'kokoro'}
    run_process(tmp_path, options, name='first')
    cached = next((tmp_path / 'data' / 'speech' / 'audio').rglob('speech.wav'))
    cached.write_bytes(b'corrupted')
    run_process(tmp_path, options, name='second')
    assert len(fake_model) == 2


def test_export_only_never_synthesizes_missing_segments(tmp_path, fake_model):
    options = {'text': 'Saved segment', 'engine': 'kokoro'}
    run_process(tmp_path, options, name='generated')
    run_process(tmp_path, {**options, 'format': 'flac', 'assemble_only': True}, name='exported')
    assert len(fake_model) == 1
    with pytest.raises(ValueError, match='Generate the missing or changed segments first'):
        run_process(tmp_path, {**options, 'text': 'Changed segment', 'assemble_only': True}, name='missing')
    assert len(fake_model) == 1


def test_default_empty_text_does_not_override_project_snapshot(tmp_path, fake_model):
    project = {'id': 'project-one', 'revision': 7, 'name': 'Saved project', 'chapters': [{'id': 'one', 'title': 'Chapter One', 'text': 'Saved speech'}]}
    files, _ = run_process(tmp_path, {'text': '', 'scope': 'document', 'engine': 'kokoro'}, project)
    report = json.loads(next(path for path in files if path.suffix == '.json').read_text())
    assert fake_model[0][0] == 'Saved speech'
    assert report['project_id'] == 'project-one' and report['project_revision'] == 7


def test_queue_document_source_is_imported_in_worker(tmp_path, fake_model):
    document = tmp_path / 'source.txt'
    document.write_text('Imported source speech')
    output = tmp_path / 'rendered'
    output.mkdir()
    files, _ = books.process({'options': {'engine': 'kokoro', 'text': ''}, 'source': {'path': str(document)}, 'settings': {}}, output, tmp_path / 'data')
    assert fake_model[0][0] == 'Imported source speech'
    assert any(file.suffix == '.wav' for file in files)


@pytest.mark.parametrize('fmt', ['wav', 'flac', 'mp3', 'opus', 'm4b'])
def test_real_export_formats_metadata_and_chapters(tmp_path, fake_model, fmt):
    from forge.worker import probe
    project = {'name': 'Synthetic book', 'chapters': [{'id': 'one', 'title': 'First chapter', 'text': 'One'}, {'id': 'two', 'title': 'Second chapter', 'text': 'Two'}]}
    files, _ = run_process(tmp_path, {'engine': 'kokoro', 'format': fmt, 'metadata': {'artist': 'Synthetic narrator', 'title': 'Synthetic book'}}, project)
    output = next(path for path in files if path.suffix == '.' + fmt)
    inspected = probe(output, {})
    assert any(s['codec_type'] == 'audio' for s in inspected['streams'])
    tags = {k.lower(): v for k, v in inspected['format'].get('tags', {}).items()}
    if fmt != 'opus':
        assert tags.get('artist') == 'Synthetic narrator'
    if fmt == 'm4b':
        assert [c['tags']['title'] for c in inspected['chapters']] == ['First chapter', 'Second chapter']


def test_chapter_selection_ignores_other_chapters(tmp_path, fake_model):
    project = {'name': 'Two chapters', 'chapters': [{'id': 'one', 'title': 'One', 'text': 'First'}, {'id': 'two', 'title': 'Two', 'text': 'Second'}]}
    run_process(tmp_path, {'chapter_id': 'two', 'scope': 'chapter', 'engine': 'kokoro'}, project)
    assert [text for text, _ in fake_model] == ['Second']


@pytest.mark.parametrize('fmt', ['flac', 'mp3', 'm4b'])
def test_real_cover_export_keeps_original_cover(tmp_path, fake_model, fmt):
    from PIL import Image
    from forge.worker import probe
    cover = tmp_path / 'cover.jpg'
    Image.new('RGB', (64, 64), '#886633').save(cover)
    before = cover.read_bytes()
    files, _ = run_process(tmp_path, {'engine': 'kokoro', 'text': 'Synthetic covered narration', 'format': fmt, 'cover': str(cover)})
    output = next(file for file in files if file.suffix == '.' + fmt)
    info = probe(output, {})
    assert any(stream.get('disposition', {}).get('attached_pic') for stream in info['streams'])
    assert cover.read_bytes() == before


def test_selected_chapters_keep_document_order(tmp_path, fake_model):
    project = {'chapters': [{'id': 'one', 'title': 'One', 'text': 'First'}, {'id': 'two', 'title': 'Two', 'text': 'Second'}, {'id': 'three', 'title': 'Three', 'text': 'Third'}]}
    run_process(tmp_path, {'chapter_ids': ['three', 'one'], 'engine': 'kokoro'}, project)
    assert [text for text, _ in fake_model] == ['First', 'Third']


def test_saved_reading_style_controls_chapter_transition(tmp_path, fake_model):
    project = {'chapters': [{'id': 'one', 'title': 'One', 'text': 'First'}, {'id': 'two', 'title': 'Two', 'text': 'Second'}]}
    files, _ = run_process(tmp_path, {'engine': 'kokoro', 'style': {'id': 'custom-style', 'pauses': {'chapter': 300}}}, project)
    assert books._wave_info(next(file for file in files if file.suffix == '.wav')) == pytest.approx(.5, abs=1 / 24000)


def test_custom_native_emotion_maps_to_documented_base(tmp_path, fake_model):
    run_process(tmp_path, {'engine': 'ming', 'task': 'EMOTION', 'text': '[[emotion:melancholy]]Hello[[/emotion]]', 'emotion_profiles': {'melancholy': {'base_emotion': 'sad', 'method': 'native'}}})
    assert fake_model[0][1]['emotion'] == 'sad'


def test_unsupported_event_fails_without_silently_dropping_it(tmp_path, fake_model):
    with pytest.raises(ValueError, match='vocal event'):
        run_process(tmp_path, {'engine': 'kokoro', 'text': '[[event:laughter]]'})
    assert fake_model == []


def test_unknown_speaker_requires_assignment(tmp_path, fake_model):
    with pytest.raises(ValueError, match='Assign a voice to speaker'):
        run_process(tmp_path, {'engine': 'kokoro', 'text': '[[speaker:character]]Hello'})
    assert not fake_model


def test_speakers_use_distinct_selected_voices(tmp_path, fake_model):
    run_process(tmp_path, {'engine': 'kokoro', 'voice': 'am_onyx', 'text': 'Narrator[[speaker:character]]Dialogue', 'speakers': {'character': {'voice': 'am_adam'}}})
    assert [options['voice'] for text, options in fake_model] == ['am_onyx', 'am_adam']


def test_unknown_expression_does_not_get_silently_ignored(tmp_path, fake_model):
    with pytest.raises(ValueError, match='does not support native emotion'):
        run_process(tmp_path, {'engine': 'kokoro', 'text': '[[emotion:not-real]]Hello[[/emotion]]'})


def test_voice_segment_master_effects_apply_once(tmp_path, fake_model):
    from forge import audio_effects
    chains = []
    def render(source, output, chain, settings):
        chains.append(chain)
        shutil.copyfile(source, output)
    # This case checks layer ownership; real frequency/level DSP is tested by
    # test_audio_effects_v8.py rather than pretending copied audio is processing.
    from unittest.mock import patch
    with patch.object(audio_effects, 'render_chain', render):
        files, _ = run_process(tmp_path, {'engine': 'kokoro', 'text': 'Hello', 'voice_effects': [{'type': 'gain', 'params': {'db': -1}}], 'segment_effects': [{'type': 'gain', 'params': {'db': -2}}], 'master_effects': [{'type': 'gain', 'params': {'db': -3}}]})
    assert [[e['params']['db'] for e in chain] for chain in chains] == [[-1, -2], [-3]]
    assert files[1].name.endswith('_original.wav')
    assert books._wave_info(files[1]) > 0


def test_saved_selection_effects_apply_only_to_selected_sentence(tmp_path, fake_model, monkeypatch):
    from forge import audio_effects
    chains = []
    def render(source, output, chain, settings):
        chains.append(chain)
        shutil.copyfile(source, output)
    monkeypatch.setattr(audio_effects, 'render_chain', render)
    text = 'First sentence. Second sentence.'
    project = {'chapters': [{'id': 'chapter', 'title': 'Chapter', 'text': text}], 'segment_processing': [{'chapter_id': 'chapter', 'start': 0, 'end': 15, 'chain': [{'type': 'gain', 'params': {'db': -4}}]}]}
    run_process(tmp_path, {'engine': 'kokoro'}, project)
    assert len(chains) == 1 and chains[0][0]['params']['db'] == -4


def test_partial_sentence_effect_selection_rejected(tmp_path, fake_model):
    project = {'chapters': [{'id': 'chapter', 'title': 'Chapter', 'text': 'Complete sentence.'}], 'segment_processing': [{'chapter_id': 'chapter', 'start': 0, 'end': 4, 'chain': [{'type': 'gain', 'params': {'db': -4}}]}]}
    with pytest.raises(ValueError, match='whole spoken segments'):
        run_process(tmp_path, {'engine': 'kokoro'}, project)


def test_changed_selection_text_cannot_reuse_saved_effect_assignment(tmp_path, fake_model):
    project={'chapters':[{'id':'chapter','title':'Chapter','text':'Changed sentence.'}],
             'segment_processing':[{'chapter_id':'chapter','start':0,'end':17,'source_sha256':'f'*64,
                                    'chain':[{'type':'gain','params':{'db':-4}}]}]}
    with pytest.raises(ValueError,match='selection text changed'):
        run_process(tmp_path,{'engine':'kokoro'},project)
