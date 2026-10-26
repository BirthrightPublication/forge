"""Synthetic speech manuscripts and additive local persistence; no model claims."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from forge.core import Store
from forge.speech_documents import (EMOTIONS, READING_STYLES, compile_document,
                                    detect_structure, insert_marker, normalize_text, parse_script)
from forge.speech_store import RevisionConflict, SpeechStore


@pytest.fixture
def speech(tmp_path):
    store = Store(tmp_path / 'data')
    yield SpeechStore(store)
    store.close()


def speech_only(text, **options):
    return [segment for segment in parse_script(text, **options) if segment['kind'] == 'speech']


def test_markers_parse_speakers_emotion_scope_and_original_source():
    source = '[[speaker:Onyx]]The room. [[emotion:curious]]What happened?[[/emotion]] Next.'
    document = compile_document(source)
    assert document['source'] == source
    assert document['spoken_text'] == 'The room. What happened? Next.'
    spoken = [item for item in document['segments'] if item['kind'] == 'speech']
    assert [item['emotion'] for item in spoken] == ['neutral', 'curious', 'neutral']
    assert all(item['speaker'] == 'Onyx' for item in spoken)
    assert all(source[item['start']:item['end']] == item['original_text'] for item in spoken)
    assert '[[' not in document['spoken_text']


@pytest.mark.parametrize('source', ['[[exec:whoami]]', '[[pause:-1ms]]', '[[pause:30001ms]]',
                                   '[[pause:NaNms]]', '[[speaker:../../escape]]', '[[/emotion]]',
                                   '[[emotion:happy]]unclosed', '[[style:news]]unclosed',
                                   '[[unknown]]', 'unfinished [[pause:', 'orphan ]]',
                                   '[[emotion:x]][[emotion:y]][[/emotion]]'])
def test_malformed_or_unsupported_markup_is_never_read_aloud(source):
    with pytest.raises(ValueError): parse_script(source)


def test_nested_emotions_restore_previous_expression():
    spoken = speech_only('[[emotion:happy]]A [[emotion:curious]]B[[/emotion]] C[[/emotion]] D')
    assert [item['emotion'] for item in spoken] == ['happy', 'curious', 'happy', 'neutral']


def test_exact_manual_silence_replaces_automatic_paragraph_punctuation():
    segments = parse_script('Sentence.\n\n[[pause:1250ms]]\n\nNext sentence.')
    silence = next(item for item in segments if item['kind'] == 'silence')
    assert silence['pause_ms'] == 1250 and silence['method'] == 'manual'
    assert sum(item['kind'] == 'silence' for item in segments[:3]) == 1


def test_adjacent_explicit_manual_pauses_sum_intentional_silence():
    segments = parse_script('One[[pause:500ms]][[pause:250ms]]Two')
    assert [item['pause_ms'] for item in segments if item['kind'] == 'silence'] == [750]


def test_segment_ids_reuse_unchanged_sentence_after_other_sentence_edit():
    before = speech_only('First sentence. Unchanged sentence. Last sentence.')
    after = speech_only('Edited opening with many new words. Unchanged sentence. Last sentence.')
    assert [item['id'] for item in before[1:]] == [item['id'] for item in after[1:]]
    assert before[0]['id'] != after[0]['id']
    repeated = speech_only('Same sentence. Same sentence.')
    assert repeated[0]['id'] != repeated[1]['id']


def test_pacing_structure_heading_lists_and_poetry():
    document = compile_document('# Title\n\n- First item\n- Second item\n\n***\nEnd', style='document')
    kinds = {item['kind'] for item in document['structure']}
    assert {'heading', 'list', 'scene', 'paragraph'} <= kinds
    assert '***' not in document['spoken_text'] and '# Title' not in document['spoken_text']
    durations = [item['pause_ms'] for item in document['segments'] if item['kind'] == 'silence']
    assert 1050 in durations and 1500 in durations
    assert sum(item.get('pause_ms', 0) for item in parse_script('Line one\nLine two', style='poetry')) == 500


def test_custom_pacing_validates_unknown_nonfinite_and_out_of_range():
    for pauses in ({'period': float('nan')}, {'period': -1}, {'bogus': 100}, {'comma': 40_000}):
        with pytest.raises(ValueError): parse_script('Hello.', pauses=pauses)
    segments = parse_script('Hello. World.', style={'id': 'custom-a', 'pauses': {'period': 800}})
    assert all(item['pause_ms'] == 800 for item in segments if item['kind'] == 'silence')


def test_style_scope_restores_original_pacing_and_vocal_events_not_spoken():
    segments = parse_script('[[style:news]]News.[[/style]] Normal. [[event:laughter]]')
    assert [item['pause_ms'] for item in segments if item['kind'] == 'silence'] == [350, 425]
    assert segments[-1]['kind'] == 'event' and segments[-1]['event'] == 'laughter' and not segments[-1]['text']


def test_normalization_literal_dictionary_no_cascade_and_source_unchanged():
    text = 'Dr. Test paid $12.50 for FORGE 1.25.'
    spoken = normalize_text(text, {'FORGE': 'Forge', 'Forge': 'wrong'},
                            rules={'abbreviations': True, 'currency': True, 'decimals': True})
    assert spoken == 'Doctor Test paid twelve dollars and fifty cents for Forge one point two five.'
    document = compile_document('FORGE is ready.', pronunciation={'FORGE': 'Forge'})
    assert document['source'] == 'FORGE is ready.' and document['spoken_text'] == 'Forge is ready.'
    assert normalize_text('Dr. Test', language='fr', rules={'abbreviations': True}) == 'Dr. Test'


def test_abbreviations_and_decimal_numbers_do_not_split_sentences():
    assert [item['text'] for item in speech_only('Dr. Smith paid 12.50. Next.')] == ['Dr. Smith paid 12.50.', 'Next.']


def test_opt_in_normalization_uses_unambiguous_dates_and_explicit_english_rules():
    original = '2026-10-26 at 13:05: 2 kg cost £1.01.'
    spoken = normalize_text(original, rules={'dates': True, 'times': True, 'measurements': True, 'currency': True})
    assert spoken == 'October twenty six, two thousand twenty six at thirteen oh five: two kilograms cost one pound and one penny.'
    assert normalize_text('2026-02-30 03/04/2026', rules={'dates': True}) == '2026-02-30 03/04/2026'
    assert normalize_text('sample@example.invalid', rules={'email': True}) == 'sample at example dot invalid'
    with pytest.raises(ValueError, match='Unknown normalization'):
        normalize_text('Original', rules={'guess_identity': True})


def test_pronunciation_cannot_inject_markup_or_claim_generic_phonetic_support():
    for dictionary in ({'word': '[[speaker:other]]'}, [{'source': 'word', 'replacement': 'text', 'phonemes': 'wɜːd'}]):
        with pytest.raises(ValueError): normalize_text('word', dictionary)


def test_contextual_marker_wrap_and_insert_keep_unselected_text():
    assert insert_marker('Hello world', 6, 11, 'emotion', 'curious') == 'Hello [[emotion:curious]]world[[/emotion]]'
    assert insert_marker('Hello world', 6, 6, 'pause', 750) == 'Hello [[pause:750ms]]world'
    for start, end in ((-1, 2), (2, 1), (0, 999), (True, 2)):
        with pytest.raises(ValueError): insert_marker('text', start, end, 'speaker', 'Onyx')


def test_builtin_presets_do_not_claim_universal_native_emotion():
    assert len(EMOTIONS) == 26 and len(READING_STYLES) == 9
    assert all(item['method'] == 'engine-dependent' for item in EMOTIONS)
    assert all('not native' in item['fallback']['disclosure'] for item in EMOTIONS)
    assert all(item['method'] == 'segmentation-and-inserted-silence' for item in READING_STYLES)


def test_projects_persist_speakers_top_level_configuration_and_chapter_ids(speech):
    saved = speech.save_project({'name': 'Synthetic Book', 'engine': 'kokoro', 'voice': 'am_onyx',
                                'master_chain': [{'type': 'gain', 'params': {'db': -1}}],
                                'chapters': [{'title': 'One', 'text': 'A sentence.'}],
                                'speakers': {'narrator': {'voice': 'am_onyx', 'name': 'Narrator'}}})
    changed = speech.save_project({**saved, 'name': 'Renamed', 'chapters': [{**saved['chapters'][0], 'text': 'Edited.'}]})
    assert changed['chapters'][0]['id'] == saved['chapters'][0]['id']
    assert speech.project(saved['id'], revision=1)['chapters'][0]['text'] == 'A sentence.'
    other = Store(speech.store.data)
    try:
        restored = SpeechStore(other).project(saved['id'])
        assert restored['name'] == 'Renamed' and restored['voice'] == 'am_onyx'
        assert restored['master_chain'] and restored['speakers']['narrator']['name'] == 'Narrator'
    finally: other.close()


def test_stale_draft_never_overwrites_newer_revision(speech):
    saved = speech.save_project({'name': 'Draft', 'text': 'Original'})
    speech.save_project({**saved, 'text': 'Updated', 'chapters': [{'id': saved['chapters'][0]['id'], 'title': 'Chapter', 'text': 'Updated'}]})
    with pytest.raises(RevisionConflict): speech.save_project({**saved, 'name': 'Stale'})
    assert speech.project(saved['id'])['name'] == 'Draft'


def test_selection_processing_keeps_source_identity_and_marks_changed_text_stale(speech):
    saved = speech.save_project({'name': 'Selection fixture', 'chapters': [{'id': 'chapter-a', 'title': 'A', 'text': 'A synthetic sentence.'}],
                                 'segment_processing': [{'chapter_id': 'chapter-a', 'start': 2, 'end': 11,
                                                         'chain': [{'type': 'gain', 'params': {'db': -3}}]}]})
    selection = saved['segment_processing'][0]
    assert selection['id'] and selection['source_sha256'] and not selection['stale']
    changed = speech.save_project({**saved, 'chapters': [{'id': 'chapter-a', 'title': 'A', 'text': 'A different sentence.'}]})
    assert changed['segment_processing'][0]['id'] == selection['id']
    assert changed['segment_processing'][0]['stale'] is True


def test_concurrent_edits_have_one_winner(speech):
    initial = speech.save_project({'name': 'Concurrent', 'text': 'Original'})
    def edit(number):
        try: return speech.save_project({**initial, 'name': f'Edit {number}'})['revision']
        except RevisionConflict: return 'conflict'
    with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(edit, range(8)))
    assert results.count(2) == 1 and results.count('conflict') == 7


def test_voice_favorites_blend_presets_and_rename_survive(speech):
    saved = speech.save_record('voices', {'name': 'Synthetic Blend', 'engine': 'kokoro',
                                         'weights': {'am_onyx': .7, 'am_fenrir': .3},
                                         'favorite': True, 'model_revision': 'a' * 40})
    renamed = speech.save_record('profiles', {**saved, 'name': 'Renamed blend'})
    assert renamed['id'] == saved['id'] and renamed['favorite'] is True
    assert speech.record('voices', saved['id'], revision=1)['name'] == 'Synthetic Blend'
    with pytest.raises(RevisionConflict): speech.save_record('voices', saved)
    restored = json.loads(json.dumps(renamed))
    restored.pop('id'); restored.pop('revision'); restored['name'] = 'Imported duplicate'
    assert speech.save_record('voices', restored)['weights'] == saved['weights']


@pytest.mark.parametrize('body', [
    {'weights': {'am_onyx': -1}}, {'weights': {'am_onyx': 0}},
    {'weights': {'am_onyx': float('nan')}}, {'engine': 'ming', 'weights': {'am_onyx': 1}},
    {'reference': 'synthetic-reference.wav'}, {'weights': {'../../other': 1}},
    {'options': {'reference_audio': 'synthetic-reference.wav'}},
    {'options': {'engine': 'ming', 'blend': [{'voice': 'am_onyx', 'weight': 1}]}},
    {'engine': 'ming', 'blend': [{'voice': 'am_onyx', 'weight': 1}]},
    {'blend': [{'voice': 'am_onyx', 'weight': -1}]},
    {'options': ['invalid']},
])
def test_invalid_or_unconsented_custom_profiles_rejected(speech, body):
    with pytest.raises(ValueError): speech.save_record('voices', {'name': 'Invalid', **body})


def test_custom_emotions_reading_styles_and_effect_presets_preserve_user_fields(speech):
    for kind, body in [('emotions', {'instructions': 'A quiet fictional narrator', 'pitch': -1}),
                       ('styles', {'pauses': {'chapter': 2000}}),
                       ('effects', {'chain': [{'type': 'gain', 'params': {'db': -2}}]}),
                       ('pronunciations', {'entries': {'FORGE': 'Forge'}}),
                       ('settings', {'default_voice': 'am_onyx', 'preview_sentence': 'Synthetic test.'})]:
        saved = speech.save_record(kind, {'name': 'User preset', **body})
        assert any(item.get('id') == saved['id'] for item in speech.records(kind))
    with pytest.raises(ValueError, match='Duplicate'): speech.save_record('emotions', {'id': 'happy', 'name': 'Override'})


def test_additive_schema_does_not_modify_old_settings_jobs_or_presets(speech):
    store = speech.store
    store.set('presets', [{'name': 'Historical QR', 'tool': 'qr'}])
    settings = store.settings()
    job = store.add_job('qr', 'Historical job', {'options': {}, 'settings': settings})
    SpeechStore(store).save_project({'name': 'New project'})
    assert store.get('presets') == [{'name': 'Historical QR', 'tool': 'qr'}]
    assert store.settings() == settings and store.job(job['id'])['status'] == 'queued'


def test_render_history_preserves_immutable_recipe_after_draft_and_preset_edits(speech):
    project = speech.save_project({'name': 'Render fixture', 'text': 'Synthetic text.'})
    recipe = {'engine': 'kokoro', 'model_revision': 'a' * 40,
              'preset': {'id': 'custom-fixture', 'revision': 1, 'weights': {'am_onyx': .7, 'am_adam': .3}}}
    evidence = speech.record_render(project['id'], 'segment-a', {'source_sha256': 'b' * 64, 'recipe': recipe,
                                                               'output': 'synthetic-audio.wav', 'output_sha256': 'c' * 64})
    speech.save_project({**project, 'name': 'Later name'})
    assert speech.renders(project['id'])[0]['recipe'] == recipe
    assert speech.record_render(project['id'], 'segment-a', {'source_sha256': 'b' * 64, 'recipe': recipe})['id'] == evidence['id']
    with pytest.raises(ValueError, match='immutable'):
        speech.record_render(project['id'], 'segment-a', {**evidence, 'recipe': {**recipe, 'model_revision': 'd' * 40}})
    with pytest.raises(ValueError): speech.record_render(project['id'], 'segment-a', {'source_sha256': 'b' * 64, 'recipe': {'engine': 'kokoro', 'model_revision': 'main'}})


def test_delete_never_unlinks_audio_and_cannot_reuse_historical_identity(speech, tmp_path):
    output = tmp_path / 'keep.wav'; output.write_bytes(b'synthetic sentinel')
    project = speech.save_project({'name': 'Deleted', 'text': 'Text', 'audio': str(output)})
    speech.delete_project(project['id'], project['revision'])
    assert output.read_bytes() == b'synthetic sentinel'
    assert speech.project(project['id'], revision=1)['name'] == 'Deleted'
    with pytest.raises(ValueError, match='reserved'): speech.save_project({'id': project['id'], 'name': 'Reuse'})
    record = speech.save_record('voices', {'name': 'Saved custom'})
    speech.delete_record('voices', record['id'])
    assert speech.record('voices', record['id'], revision=1)['name'] == 'Saved custom'
    with pytest.raises(ValueError, match='reserved'): speech.save_record('voices', {'id': record['id'], 'name': 'Reuse'})


def test_only_bounded_json_data_is_accepted(speech):
    for body in ({'name': 'Object', 'payload': object()}, {'name': 'NaN', 'weight': float('nan')},
                 {'name': 'Huge', 'text': 'a' * 2_000_001}):
        with pytest.raises(ValueError): speech.save_record('voices', body)
    with pytest.raises(ValueError): speech.records('speech_records; DROP TABLE jobs')
