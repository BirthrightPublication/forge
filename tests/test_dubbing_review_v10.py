"""Real SQLite state races around review approval; no neural inference claims.

The source/reference bytes are deliberate state-only placeholders. Model
catalogues are explicit fixtures so these tests exercise queue intents and
revision boundaries without downloading or pretending to execute a model.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import threading
import time
from types import SimpleNamespace

import pytest

from forge.core import Store
from forge.dubbing_api import DubbingService
from forge.dubbing_store import RevisionConflict, stage_fingerprint


class ModelCatalogue:
    def __init__(self):
        self.values = {
            'opus-en-ru': {'ready': True, 'model_revision': 'a' * 40},
            'qwen3-tts-0.6b-base': {'ready': True, 'model_revision': 'b' * 64},
            'hdemucs-high': {'ready': False, 'model_revision': ''},
            'chatterbox-multilingual-v3': {'ready': False, 'model_revision': ''},
        }

    def catalog(self, data):
        return [{'id': key, **copy.deepcopy(value)} for key, value in self.values.items()]

    def existing_whisper_state(self, data, model):
        return {'ready': True, 'model_revision': 'c' * 40}

    def installation_plan(self, components, data, **kwargs):
        return {'id': 'synthetic-verified-plan', 'created': time.time(), 'expires': time.time() + 3600,
                'action': kwargs.get('action', 'install'), 'download_bytes': 300,
                'required_storage_bytes': 900, 'destination': str(data / 'dubbing' / 'models'),
                'components': [{'component': component, 'label': component, 'device': 'cpu',
                                'model_revision': self.values[component]['model_revision']}
                               for component in components]}


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / 'data')
    source = tmp_path / 'owned-source.mp4'
    source.write_bytes(b'Owned synthetic source placeholder for SQLite/queue state testing only.')
    stat = source.stat()
    manager = SimpleNamespace(lock=threading.RLock(), check_accepting=lambda: None)
    value = DubbingService(SimpleNamespace(), store, manager, lambda *args: None)
    models = ModelCatalogue()
    value.models = lambda: models
    value.fixture_source = {'id': 'state-source', 'path': str(source), 'name': source.name,
                            'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    value.fixture_models = models
    yield value
    store.close()


def project(service, *, voice=None, glossary=None, reviewed=True, segments=None, **extra):
    options = {}
    if voice:
        options['tts'] = {'voice_profile_id': voice['id']}
    values = segments if segments is not None else [
        {'id': 'first', 'start': 0, 'end': 1, 'speaker': 'speaker1',
         'source_text': 'Hello Alice', 'text': 'Здравствуйте, Алиса', 'reviewed': reviewed},
        {'id': 'second', 'start': 2, 'end': 3, 'speaker': 'speaker1',
         'source_text': 'Goodbye', 'text': 'До свидания', 'reviewed': reviewed},
    ]
    return service.library.save_project({'name': 'Synthetic Russian review project',
        'source': service.fixture_source, 'options': options, 'segments': values,
        'glossary_id': glossary['id'] if glossary else '', **extra}, trusted=True)


def voice(service, tmp_path, **extra):
    reference = tmp_path / 'permitted-reference.wav'
    reference.write_bytes(b'Owned synthetic reference placeholder for approval/snapshot state only.')
    return service.library.save_record('voices', {'name': 'Permitted state fixture', 'consent': True,
        'approved': True, 'engine': 'qwen3-tts-0.6b-base', 'speaking_rate': 1,
        'prepared_reference': {'path': str(reference), 'sha256': hashlib.sha256(reference.read_bytes()).hexdigest()},
        **extra}, trusted=True)


def glossary(service):
    return service.library.save_record('glossaries', {'name': 'Series names', 'entries': {'Alice': 'Алиса'}})


def test_cached_final_render_cannot_bypass_voice_approval(service,tmp_path):
    reference=voice(service,tmp_path,approved=False)
    saved=project(service,voice=reference)
    with pytest.raises(ValueError,match='approve|permitted|permission'):
        service.enqueue(saved,['dubbing_align'])
    assert not service.store.jobs()


def test_script_review_does_not_restore_voice_approval(service,tmp_path):
    reference=voice(service,tmp_path)
    saved=project(service,voice=reference)
    started=service.enqueue(saved,['dubbing_align'])
    revoked={**reference,'approved':False}
    service.library.save_record('voices',revoked,reference['revision'],trusted=True)
    current=service.library.project(saved['id'])
    service.review({'project_id':saved['id'],'expected_revision':current['revision'],
                    'run_id':started['run']['id'],'approve':True})
    assert service.prepare_job(first_job(service,started)) is False


def first_job(service, result):
    return service.store.job(result['jobs'][0]['id'])


def test_two_initial_asr_results_cannot_erase_a_manual_edit_between_completions(service):
    original = project(service, segments=[])
    run = service.library.save_run({'project_id': original['id'], 'snapshot': original, 'status': 'running'})
    update = {'id': 'stable-asr-segment', 'start': 0, 'end': 1, 'speaker': 'speaker1',
              'source_text': 'Original English', 'text': '', 'reviewed': False,
              'manual_edit': False, 'new_segment': True, 'expected_input_hash': None}

    def complete(identity, run_id=None):
        context = {'project': original}
        if run_id:
            context['run_id'] = run_id
        service.library.apply_result({'id': identity, 'tool': 'dubbing_transcribe',
            'payload': {'dubbing': context}}, {'dubbing_result': {
                'stage_settings_hash': stage_fingerprint(original, 'dubbing_transcribe'),
                'segment_updates': [copy.deepcopy(update)]}})

    complete('first-asr')
    current = service.library.project(original['id'])
    current['segments'][0]['text'] = 'Ручная правка должна сохраниться'
    current = service.library.save_project(current, current['revision'])
    assert current['segments'][0]['manual_edit'] is True
    complete('second-late-asr', run['id'])
    final = service.library.project(original['id'])
    assert final['segments'][0]['text'] == 'Ручная правка должна сохраниться'
    assert final['segments'][0]['manual_edit'] is True
    assert final['revision'] == current['revision']
    assert service.library.run(run['id'])['results']['dubbing_transcribe']['stale_segments'] == ['stable-asr-segment']


def test_queued_voice_profile_edit_blocks_dispatch_until_explicit_new_review(service, tmp_path):
    reference = voice(service, tmp_path)
    saved = project(service, voice=reference)
    created = service.enqueue(saved, ['dubbing_voice'])
    job = first_job(service, created)
    assert job['payload']['dubbing']['voice_profiles'][reference['id']]['revision'] == reference['revision']
    changed = service.library.save_record('voices', {**reference, 'speaking_rate': 1.2}, reference['revision'])
    assert service.prepare_job(job) is False
    blocked = service.store.job(job['id'])
    assert blocked['status'] == 'queued' and 'Review' in blocked['message']
    assert blocked['payload']['dubbing']['voice_profiles'][reference['id']]['revision'] == reference['revision']
    service.review({'project_id': saved['id'], 'run_id': created['run']['id'],
                    'expected_revision': saved['revision'], 'approve': True})
    assert service.prepare_job(service.store.job(job['id'])) is True
    dispatched = service.store.job(job['id'])['payload']['dubbing']
    assert dispatched['voice_profiles'][reference['id']]['revision'] == changed['revision']
    assert dispatched['voice_profiles'][reference['id']]['speaking_rate'] == 1.2
    assert dispatched['review_approved'] is True


def test_queued_translation_keeps_its_glossary_when_library_is_edited(service):
    terms = glossary(service)
    saved = project(service, glossary=terms, reviewed=False)
    created = service.enqueue(saved, ['dubbing_translate'])
    job = first_job(service, created)
    changed = service.library.save_record('glossaries', {**terms, 'entries': {'Alice': 'Другая версия имени'}}, terms['revision'])
    assert service.prepare_job(job) is True
    context = service.store.job(job['id'])['payload']['dubbing']
    assert context['glossary']['revision'] == terms['revision']
    assert context['glossary']['entries']['Alice'] == 'Алиса'
    assert service.library.run(created['run']['id'])['frozen_inputs']['glossary']['revision'] == terms['revision']
    future = service.plan({'project_id': saved['id'], 'stages': ['dubbing_translate']})
    quote = service.store.get('dubbing_plans')[future['token']]
    assert quote['frozen_inputs']['glossary']['revision'] == changed['revision']


def test_queued_translation_retains_its_immutable_model_pin(service):
    saved = project(service, reviewed=False)
    created = service.enqueue(saved, ['dubbing_translate'])
    original_pin = service.fixture_models.values['opus-en-ru']['model_revision']
    service.fixture_models.values['opus-en-ru']['model_revision'] = 'd' * 40
    job = first_job(service, created)
    assert service.prepare_job(job) is True
    assert service.store.job(job['id'])['payload']['dubbing']['model_revisions']['opus-en-ru'] == original_pin


def test_unavailable_separation_model_fails_before_allocating_any_work(service):
    saved = project(service, options={'audio': {'separation': 'hdemucs'}})
    with pytest.raises(ValueError, match='hdemucs-high'):
        service.enqueue(saved, ['dubbing_separate'])
    assert not service.store.jobs()
    assert service.store.db.execute('SELECT COUNT(*) FROM dubbing_runs').fetchone()[0] == 0


@pytest.mark.parametrize('override_field', ['model', 'engine'])
def test_profile_actual_engine_is_checked_instead_of_only_default_qwen(service, tmp_path, override_field):
    reference = voice(service, tmp_path, **{override_field: 'chatterbox-multilingual-v3'})
    saved = project(service, voice=reference)
    assert saved['options']['tts']['engine'] == 'qwen3-tts-0.6b-base'
    with pytest.raises(ValueError, match='chatterbox-multilingual-v3'):
        service.enqueue(saved, ['dubbing_voice'])
    assert not service.store.jobs()


def test_workflow_preview_token_rejects_a_new_project_revision(service):
    saved = project(service, reviewed=False)
    plan = service.plan({'project_id': saved['id'], 'stages': ['dubbing_translate']})
    service.library.save_project({**saved, 'name': 'Changed after preview'}, saved['revision'])
    with pytest.raises(RevisionConflict, match='project changed'):
        service.start({'plan_token': plan['token']})
    assert not service.store.jobs()


@pytest.mark.parametrize('changed_input', ['voice', 'glossary', 'model'])
def test_workflow_preview_rejects_changed_voice_glossary_or_model(service, tmp_path, changed_input):
    reference, terms = voice(service, tmp_path), glossary(service)
    saved = project(service, voice=reference, glossary=terms)
    plan = service.plan({'project_id': saved['id'], 'stages': ['dubbing_translate']})
    if changed_input == 'voice':
        service.library.save_record('voices', {**reference, 'speaking_rate': 1.15}, reference['revision'])
    elif changed_input == 'glossary':
        service.library.save_record('glossaries', {**terms, 'entries': {'Alice': 'Новое имя'}}, terms['revision'])
    else:
        service.fixture_models.values['opus-en-ru']['model_revision'] = 'e' * 40
    with pytest.raises(RevisionConflict, match='voice, glossary or model changed'):
        service.start({'plan_token': plan['token']})
    assert not service.store.jobs()


def test_two_concurrent_starts_of_one_approved_plan_allocate_exactly_one_run(service):
    saved = project(service, reviewed=False)
    plan = service.plan({'project_id': saved['id'], 'stages': ['dubbing_translate']})
    both = threading.Barrier(2)

    def start():
        both.wait(timeout=5)
        return service.start({'plan_token': plan['token']})

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(start) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0]['run']['id'] == results[1]['run']['id']
    assert [j['id'] for j in results[0]['jobs']] == [j['id'] for j in results[1]['jobs']]
    assert len(service.store.jobs()) == 1
    assert service.store.db.execute('SELECT COUNT(*) FROM dubbing_runs').fetchone()[0] == 1


def test_quoted_model_install_needs_consent_and_concurrent_approval_is_idempotent(service):
    plan = service.setup_plan({'components': ['opus-en-ru', 'qwen3-tts-0.6b-base'], 'device': 'cpu'})
    with pytest.raises(ValueError, match='approve'):
        service.setup_install({'token': plan['token'], 'consent': False})
    assert not service.store.jobs()
    both = threading.Barrier(2)

    def approve():
        both.wait(timeout=5)
        # The server-owned quote, not untrusted replacement component fields,
        # determines the approved installations.
        return service.setup_install({'token': plan['token'], 'consent': True,
                                      'components': ['unapproved-component']})

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(approve) for _ in range(2)]
        result = [future.result(timeout=10) for future in futures]
    assert [j['id'] for j in result[0]['jobs']] == [j['id'] for j in result[1]['jobs']]
    jobs = [service.store.job(j['id']) for j in result[0]['jobs']]
    assert len(service.store.jobs()) == 2
    assert [j['payload']['options']['component'] for j in jobs] == ['opus-en-ru', 'qwen3-tts-0.6b-base']
    assert jobs[1]['payload']['depends_on'] == jobs[0]['id']
    assert all(job['payload']['dubbing_install_plan']['download_bytes'] == 300 for job in jobs)


def test_review_rejects_stale_version_and_partial_review_does_not_release_final_voice(service, tmp_path):
    reference = voice(service, tmp_path)
    saved = project(service, voice=reference, reviewed=False)
    created = service.enqueue(saved, ['dubbing_review', 'dubbing_voice'])
    edited = copy.deepcopy(saved)
    edited['segments'][0]['text'] = 'Исправленный русский текст'
    latest = service.library.save_project(edited, saved['revision'])
    with pytest.raises(RevisionConflict, match='current translated script'):
        service.review({'project_id': saved['id'], 'run_id': created['run']['id'],
                        'expected_revision': saved['revision'], 'approve': True})
    first = service.review({'project_id': saved['id'], 'run_id': created['run']['id'],
                            'expected_revision': latest['revision'], 'segment_ids': ['first'], 'approve': True})
    assert first['approved'] is False
    gate = first_job(service, created)
    assert service.prepare_job(gate) is False and service.store.job(gate['id'])['status'] == 'queued'
    second = service.review({'project_id': saved['id'], 'run_id': created['run']['id'],
                             'expected_revision': first['project']['revision'], 'approve': True})
    assert second['approved'] is True
    assert service.prepare_job(service.store.job(gate['id'])) is False
    assert service.store.job(gate['id'])['status'] == 'completed'
    dependent = service.store.job(created['jobs'][1]['id'])
    assert service.prepare_job(dependent) is True
