"""Context/protection contracts with an explicit synthetic decoder boundary."""
from contextlib import nullcontext
import copy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def translator(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'scripts/dubbing_runtime.py'
    spec = importlib.util.spec_from_file_location('owned_context_runtime', path)
    runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(runtime)
    calls = []; answers = {}
    class Tensor:
        shape = (1, 4)
        def __init__(self, text): self.text = text
        def to(self, device): return self
    class Tokenizer:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            assert kwargs['local_files_only'] and not kwargs['trust_remote_code']
            return cls()
        def __call__(self, text, **kwargs): return {'input_ids': Tensor(text)}
        def batch_decode(self, generated, **kwargs): return [generated]
    class Model:
        config = SimpleNamespace(max_position_embeddings=512)
        @classmethod
        def from_pretrained(cls, *args, **kwargs): return cls()
        def to(self, device): return self
        def eval(self): return self
        def generate(self, input_ids, **kwargs):
            calls.append(input_ids.text)
            return answers.get(input_ids.text, 'Перевод')
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(float32='fixture', inference_mode=nullcontext))
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(MarianMTModel=Model, MarianTokenizer=Tokenizer))
    def run(source, responses, glossary=None, segments=None):
        calls.clear(); answers.clear(); answers.update(responses)
        return runtime.translate({'model_paths': {'translation': str(tmp_path)}, 'device': 'cpu',
            'model_revision': 'b' * 40, 'mode': 'faithful', 'glossary': glossary or {},
            'segments': segments or [{'id': 'stable-1', 'start': 0, 'end': 4, 'speaker': 'speaker1', 'source_text': source}]})
    return run, calls


def test_context_first_retains_sentence_and_only_substitutes_unchanged_glossary_spelling(translator):
    run, calls = translator
    source = 'FORGE keeps the original video. The project has 3 segments.'
    decoded = 'FORGE сохраняет оригинальное видео. Проект содержит 3 сегмента.'
    result = run(source, {source: decoded}, {'FORGE': 'ФОРДЖ'})
    assert calls == [source]
    assert result['segments'][0]['text'] == decoded.replace('FORGE', 'ФОРДЖ')
    assert result['warnings'] == [] and result['generation_calls'] == 1
    assert result['segments'][0]['id'] == 'stable-1' and result['segments'][0]['start'] == 0
    assert not result['segments'][0]['reviewed']


def test_already_correct_target_is_not_rewritten_when_it_contains_the_source_term(translator):
    run, calls = translator
    source = 'Use FORGE today.'; decoded = 'Используйте FORGE Studio сегодня.'
    result = run(source, {source: decoded}, {'FORGE': 'FORGE Studio'})
    assert calls == [source] and result['segments'][0]['text'] == decoded
    assert result['warnings'] == []


def test_duplicate_terms_and_numbers_must_preserve_exact_occurrence_sequence(translator):
    run, calls = translator
    source = 'FORGE has 3 clips and FORGE has 3 copies.'
    decoded = 'ФОРДЖ содержит 3 клипа, а ФОРДЖ содержит 3 копии.'
    result = run(source, {source: decoded}, {'FORGE': 'ФОРДЖ'})
    assert calls == [source] and result['segments'][0]['text'] == decoded
    assert result['warnings'] == []


@pytest.mark.parametrize(('source', 'target', 'decoded'), [
    ('Use three and 3 clips.', '3', 'Используйте 3 и 3 клипа.'),
    ('Use three and [3] clips.', '[3]', 'Используйте [3] и [3] клипа.'),
    ('Use three and https://example.org/3 today.', 'https://example.org/3',
     'Используйте https://example.org/3 и https://example.org/3 сегодня.'),
    ('Use three today.', 'https://example.org/3 ready', 'Используйте https://example.org/3 ready сегодня.'),
    ('Use three today.', 'ready [3]', 'Используйте ready [3] сегодня.'),
])
def test_glossary_targets_that_are_or_contain_literal_protections_remain_complete(translator, source, target, decoded):
    run, calls = translator
    result = run(source, {source: decoded}, {'three': target})
    assert calls == [source] and result['segments'][0]['text'] == decoded
    assert result['warnings'] == []


@pytest.mark.parametrize('decoded', [
    'Используйте https://example.org/3/changed ready сегодня.',
    'Используйте https://example.org/3 сегодня.',
])
def test_url_target_prefix_or_missing_target_suffix_is_rejected(translator, decoded):
    run, calls = translator
    source = 'Use three today.'
    result = run(source, {source: decoded, 'Use': 'Используйте', 'today.': 'сегодня.'},
                 {'three': 'https://example.org/3 ready'})
    assert calls == [source, 'Use', 'today.']
    assert result['segments'][0]['text'] == 'Используйте https://example.org/3 ready сегодня.'
    assert 'requires manual review' in result['warnings'][0]


@pytest.mark.parametrize('decoded', [
    'ФОРДЖ сохраняет клипы на 2026-11-13.',
    'ФОРДЖ сохраняет 30 клипов на 2026-11-13.',
    'ФОРДЖ сохраняет 3 клипа на 13-11-2026.',
    'ФОРДЖ на 2026-11-13 сохраняет 3 клипа.',
    'ФОРДЖ сохраняет 3 и 3 клипа на 2026-11-13.',
    'ФОРДЖ ФОРДЖ сохраняет 3 клипа на 2026-11-13.',
    'Forge сохраняет 3 клипа на 2026-11-13.',
    'ФОРДЖ сохраняет 3 клипа на 2026-11-13 с RTX5080.',
])
def test_invalid_context_protections_fall_back_with_explicit_manual_review_warning(translator, decoded):
    run, calls = translator
    source = 'FORGE keeps 3 clips on 2026-11-13.'
    result = run(source, {source: decoded, 'keeps': 'сохраняет', 'clips on': 'клипа на'}, {'FORGE': 'ФОРДЖ'})
    assert calls == [source, 'keeps', 'clips on']
    assert result['segments'][0]['text'] == 'ФОРДЖ сохраняет 3 клипа на 2026-11-13.'
    assert len(result['warnings']) == 1 and 'requires manual review' in result['warnings'][0]
    assert result['segments'][0]['source_text'] == source and not result['segments'][0]['reviewed']


@pytest.mark.parametrize('extra', ['', ' https://invented.test/9', ' [7]', ' 9'])
def test_urls_citations_and_numbers_are_checked_including_invented_extras(translator, extra):
    run, calls = translator
    source = 'Read https://example.org/3 and [4] for 5 models.'
    decoded = 'Читайте https://example.org/3 и [4] для 5 моделей.' + extra
    result = run(source, {source: decoded}, {'example': 'пример'})
    text = result['segments'][0]['text']
    assert 'https://example.org/3' in text and '[4]' in text and '5' in text
    assert 'https://invented.test/9' not in text and '[7]' not in text
    if extra:
        assert len(calls) > 1 and 'requires manual review' in result['warnings'][0]
    else:
        assert calls == [source] and text == decoded and result['warnings'] == []


@pytest.mark.parametrize('citation', ['[1, 2]', '[1; 2]', '[1 - 2]', '[ 1, 2 ]', '[1–3]', '[1—3]'])
@pytest.mark.parametrize('change', ['unchanged', 'remove_brackets', 'change_spacing'])
def test_numeric_citations_retain_complete_bracketed_literals(translator, citation, change):
    run, calls = translator
    source = 'Read ' + citation + ' today.'
    changed = citation if change == 'unchanged' else citation[1:-1] if change == 'remove_brackets' else '[ ' + citation[1:-1] + ' ]'
    decoded = 'Читайте ' + changed + ' сегодня.'
    result = run(source, {source: decoded, 'Read': 'Читайте', 'today.': 'сегодня.'})
    assert result['segments'][0]['text'] == 'Читайте ' + citation + ' сегодня.'
    if change == 'unchanged':
        assert calls == [source] and result['warnings'] == []
    else:
        assert calls == [source, 'Read', 'today.']
        assert 'requires manual review' in result['warnings'][0]


def test_unsafe_fragment_decoder_cannot_publish_invented_numbers(translator):
    run, _ = translator
    source = 'FORGE keeps 3 clips.'
    with pytest.raises(RuntimeError, match='translate this segment manually'):
        run(source, {source: 'Утраченное значение.', 'keeps': 'модель99'}, {'FORGE': 'ФОРДЖ'})


def test_manual_or_reviewed_translation_still_bypasses_generation(translator):
    run, calls = translator
    segments = [{'id': 'edited', 'source_text': 'FORGE has 3 clips.', 'text': 'Ручная редакция.', 'manual_edit': True},
                {'id': 'reviewed', 'source_text': 'FORGE has 5 clips.', 'text': 'Проверенная редакция.', 'reviewed': True}]
    before = copy.deepcopy(segments)
    result = run('', {}, {'FORGE': 'ФОРДЖ'}, segments=segments)
    assert calls == [] and segments == before
    assert [item['text'] for item in result['segments']] == [item['text'] for item in before]
    assert all(item['translation_skipped'] for item in result['segments'])
    assert result['inference_tested'] is False
