"""Negative release-gate contracts; fixture receipts are not real inference proof."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('speech_release_gate', ROOT / 'scripts/collect_speech_acceptance.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
COMMIT = 'a' * 40
MODEL = 'b' * 40
RUN_ID = '12345'


def write_record(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def mutate(path, change):
    value = json.loads(path.read_text())
    change(value)
    write_record(path, value)


@pytest.fixture
def records(tmp_path):
    """A complete synthetic contract envelope; never uploaded as acceptance."""
    evidence = tmp_path / 'evidence'
    paths = {name: evidence / f'speech-{name}-{RUN_ID}'
             for name in ('publishers', 'kokoro', 'ming', *gate.BARK_MODELS, 'kokoro-pipeline')}
    for folder in paths.values():
        folder.mkdir(parents=True)
    common = {'source_commit': COMMIT, 'status': 'passed', 'native_windows_execution': True,
              'device': 'cpu', 'physical_gpu_tested': False, 'inference_substituted': False}
    entries = []
    for engine in ('kokoro', 'ming', *gate.BARK_MODELS):
        notice = paths['publishers'] / engine / 'LICENSE'
        notice.parent.mkdir()
        notice.write_text('Contract fixture notice; not publisher evidence.')
        entries.append({'engine': engine, 'status': 'passed', 'revision': MODEL,
                        'notice_sha256': {'LICENSE': gate.digest(notice)}})
    write_record(paths['publishers'] / 'publisher-access.json',
                 {'source_commit': COMMIT, 'platform': 'win32', 'all_publishers_accessible': True,
                  'model_inference_tested': False, 'weights_downloaded': False, 'entries': entries})
    for engine, names in [('kokoro', gate.KOKORO_AUDIO), ('ming', gate.MING_AUDIO)]:
        tests = []
        runtime = f'speech-{engine}-' + 'c' * 16
        for name in names:
            audio = paths[engine] / (name + '.wav')
            audio.write_bytes(b'Contract fixture, not neural inference.')
            tests.append({'name': name, 'status': 'passed', 'audio': audio.name,
                          'model_revision': MODEL, 'runtime_revision': runtime,
                          'metrics': {'sha256': gate.digest(audio), 'duration_seconds': 3,
                                      'peak': .2, 'clipped_fraction': 0}})
        write_record(paths[engine] / f'tts-{engine}-acceptance.json',
                     {**common, 'engine': engine, 'scope': 'full', 'model_revision': MODEL,
                      'runtime_revision': runtime, 'tests': tests,
                      'phases': [{'name': 'contract-fixture', 'status': 'passed'}],
                      'acceptance_complete': False, 'pending': ['Physical CUDA acceptance']})
    reference = paths['kokoro'] / 'acceptance-reference.txt'
    reference.write_text('Original contract-only fixture prose; no inference was performed.')
    fixture = {'model_revision': MODEL, 'audio_sha256': gate.digest(paths['kokoro'] / 'acceptance-reference.wav'),
               'reference_sha256': gate.digest(reference), 'synthetic_test_owned': True}
    write_record(paths['kokoro'] / 'acceptance-reference.json', fixture)
    for model in gate.BARK_MODELS:
        pairs = [('bark', model)]
        if model == 'bark-3.0-plus':
            pairs.append(('whisper', 'large-v3'))
        if model == 'bark-3.0-pro':
            pairs.append(('whisper', 'large-v3-turbo'))
        results = [{'engine': engine, 'model': name, 'scope': scope, 'model_revision': MODEL,
                    'wer': {'normalized_words': 0}, 'hypothesis': 'Contract-only fixture.',
                    'device': 'cpu', 'offline_inference': True, 'processing_seconds': 1,
                    'sampled_process_tree_peak_rss_bytes': 1000, 'timing': 'unavailable', 'decoded_chunks': 4}
                   for engine, name in pairs for scope in sorted(gate.CASES)]
        write_record(paths[model] / 'ASR_MODEL_ACCEPTANCE.json',
                     {**common, 'failures': [], 'results': results,
                      'fixture': {'tts_model_revision': MODEL, 'voice': 'am_onyx',
                                  'audio_sha256': fixture['audio_sha256'], 'reference_sha256': fixture['reference_sha256'],
                                  'long_duration_seconds': 65},
                      'synthetic_british_fixture': {'status': 'available'},
                      'controlled_noise_fixture': {'target_snr_db': 20, 'measured_snr_db': 20, 'clipped_samples': 0}})
    write_record(paths['kokoro-pipeline'] / 'PIPELINE_ACCEPTANCE.json',
                 {**common, 'model_inference_tested': True, 'model_revisions': [MODEL],
                  'first_segments': 2, 'edited_segments': 2, 'reused_segments': 1,
                  'checks': ['API', 'queue', 'playback', 'edit and reuse']})
    return evidence, paths, tmp_path / 'result'


def collect(records):
    evidence, _, output = records
    return gate.collect(evidence, output, source_commit=COMMIT, run_id=RUN_ID)


def test_combined_receipt_is_explicit_cpu_only_and_preserves_artifact_boundaries(records):
    result = collect(records)
    assert result['status'] == 'passed'
    assert result['physical_gpu_tested'] is False and result['full_hardware_acceptance'] is False
    assert len(result['receipts']) == 8
    assert 'CUDA' in result['limitations'][0]
    archive = records[2] / result['evidence_archive']['name']
    assert result['evidence_archive']['sha256'] == gate.digest(archive)
    assert not re.search(r'(win(dows)?|x64|amd64)', archive.name, re.I)
    with zipfile.ZipFile(archive) as zipped:
        asr = [name for name in zipped.namelist() if name.endswith('/ASR_MODEL_ACCEPTANCE.json')]
        assert len(asr) == 4 and len({name.split('/')[0] for name in asr}) == 4
    assert json.loads((records[2] / 'SPEECH_ACCEPTANCE.json').read_text()) == result


@pytest.mark.parametrize('field,value', [('source_commit', 'd' * 40), ('status', 'failed'),
                                        ('native_windows_execution', False), ('physical_gpu_tested', True),
                                        ('device', 'cuda'), ('inference_substituted', True)])
def test_failed_foreign_or_non_native_records_cannot_pass(records, field, value):
    path = records[1]['bark-3.0-pro'] / 'ASR_MODEL_ACCEPTANCE.json'
    mutate(path, lambda record: record.update({field: value}))
    with pytest.raises(gate.EvidenceError):
        collect(records)
    assert not records[2].exists()


@pytest.mark.parametrize('name', ['kokoro', 'ming', *gate.BARK_MODELS, 'kokoro-pipeline', 'publishers'])
def test_missing_receipt_blocks_publication(records, name):
    filename = ('publisher-access.json' if name == 'publishers' else 'PIPELINE_ACCEPTANCE.json'
                if name == 'kokoro-pipeline' else f'tts-{name}-acceptance.json'
                if name in ('kokoro', 'ming') else 'ASR_MODEL_ACCEPTANCE.json')
    (records[1][name] / filename).unlink()
    with pytest.raises(gate.EvidenceError, match='Missing regular receipt'):
        collect(records)


@pytest.mark.parametrize('model', ['bark-3.0-plus', 'bark-3.0-pro'])
def test_bark_success_without_actual_whisper_comparison_is_insufficient(records, model):
    mutate(records[1][model] / 'ASR_MODEL_ACCEPTANCE.json',
           lambda record: record.update(results=[item for item in record['results'] if item['engine'] == 'bark']))
    with pytest.raises(gate.EvidenceError, match='missing Bark/Whisper'):
        collect(records)


@pytest.mark.parametrize('scope', sorted(gate.CASES))
def test_every_fixture_case_is_required(records, scope):
    mutate(records[1]['bark-3.0'] / 'ASR_MODEL_ACCEPTANCE.json',
           lambda record: record.update(results=[item for item in record['results'] if item['scope'] != scope]))
    with pytest.raises(gate.EvidenceError, match='fixture case'):
        collect(records)


def test_basic_tts_or_missing_language_or_modified_synthetic_audio_cannot_pass(records):
    receipt = records[1]['kokoro'] / 'tts-kokoro-acceptance.json'
    original = receipt.read_bytes()
    mutate(receipt, lambda record: record.update(scope='basic'))
    with pytest.raises(gate.EvidenceError, match='full engine scope'):
        collect(records)
    receipt.write_bytes(original)
    mutate(receipt, lambda record: record.update(tests=[item for item in record['tests'] if item['audio'] != 'kokoro-language-z.wav']))
    with pytest.raises(gate.EvidenceError, match='missing actual synthesis'):
        collect(records)
    receipt.write_bytes(original)
    (records[1]['kokoro'] / 'acceptance-reference.wav').write_bytes(b'changed')
    with pytest.raises(gate.EvidenceError, match='differs from measured receipt'):
        collect(records)


def test_wrong_reference_excessive_wer_and_unexercised_pipeline_fail(records):
    receipt = records[1]['bark-3.0'] / 'ASR_MODEL_ACCEPTANCE.json'
    original = receipt.read_bytes()
    mutate(receipt, lambda record: record['fixture'].update(tts_model_revision='d' * 40))
    with pytest.raises(gate.EvidenceError, match='fixture differs'):
        collect(records)
    receipt.write_bytes(original)
    mutate(receipt, lambda record: record['results'][0]['wer'].update(normalized_words=.36))
    with pytest.raises(gate.EvidenceError, match='error bound'):
        collect(records)
    receipt.write_bytes(original)
    mutate(records[1]['kokoro-pipeline'] / 'PIPELINE_ACCEPTANCE.json', lambda record: record.update(reused_segments=0))
    with pytest.raises(gate.EvidenceError, match='pipeline evidence is incomplete'):
        collect(records)


@pytest.mark.parametrize('relative', ['weights.safetensors', 'model.pt', 'envs/private.json', 'models/private.json'])
def test_model_weights_environments_and_extra_artifacts_are_never_published(records, relative):
    extra = records[1]['kokoro'] / relative
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text('Forbidden data')
    with pytest.raises(gate.EvidenceError, match='models/environments are forbidden'):
        collect(records)
    assert not records[2].exists()


@pytest.mark.parametrize('filename', ['release.yml', 'windows-release.yml'])
def test_release_gate_semantic_version_threshold_executes_actual_resolver(tmp_path, filename):
    workflow = (ROOT / '.github/workflows' / filename).read_text()
    script = re.search(r"          python3 - <<'PY'\n(.*?)          PY", workflow, re.S).group(1)
    script = '\n'.join(line.removeprefix('          ') for line in script.splitlines())
    for tag, expected in [('v0.7.99', 'false'), ('v0.8.0', 'true'), ('v0.10.0', 'true'), ('v1.0.0', 'true')]:
        output = tmp_path / tag
        subprocess.run([sys.executable, '-c', script], check=True,
                       env={**os.environ, 'GITHUB_REF': f'refs/tags/{tag}', 'GITHUB_EVENT_NAME': 'push',
                            'GITHUB_OUTPUT': str(output), 'REQUESTED_TAG': tag, 'BUILD_ONLY': 'false'}, capture_output=True)
        assert f'speech_required={expected}\n' in output.read_text()


def test_release_workflows_pin_checkout_and_gate_publisher_on_actual_models():
    workflow = (ROOT / '.github/workflows/speech-acceptance.yml').read_text()
    release = (ROOT / '.github/workflows/release.yml').read_text()
    assert 'workflow_call:' in workflow and 'source_ref:' in workflow
    assert workflow.count('uses: actions/checkout@') == workflow.count('ref: ${{ inputs.source_ref || github.sha }}') == 4
    assert "branches: ['development/v0.8-speech-studio']" in workflow
    assert 'merge-multiple: false' in workflow and 'speech-verified-${{ github.run_id }}' in workflow
    assert 'needs: [publishers, tts, asr]' in workflow
    assert 'needs: [resolve, build, windows, speech]' in release
    assert "needs.resolve.outputs.speech_required == 'false'" in release
    assert "needs.speech.result == 'success'" in release
    assert 'source_ref: refs/tags/${{ needs.resolve.outputs.tag }}' in release
    assert 'name: ${{ needs.speech.outputs.evidence_artifact }}' in release
    assert "required += ('SPEECH_ACCEPTANCE.json', 'SPEECH_EVIDENCE.zip', 'CHECKSUMS.sha256')" in release
    assert "speech.get('source_commit') != legacy['source_commit']" in release
    windows = (ROOT / '.github/workflows/windows-release.yml').read_text()
    assert "if: needs.resolve.outputs.build_only == 'false' && needs.resolve.outputs.speech_required == 'true'" in windows
    assert 'needs: [resolve, build, windows, finalize, speech]' in windows
    assert "needs.speech.result == 'success'" in windows
    assert 'source_ref: refs/tags/${{ needs.resolve.outputs.tag }}' in windows
    assert 'name: ${{ needs.speech.outputs.evidence_artifact }}' in windows
    assert "speech.get('source_commit') != linux['source_commit']" in windows
    assert "speech.get('actions_run_id') != os.environ['GITHUB_RUN_ID']" in windows
    for text in (workflow, release, windows):
        assert all(re.fullmatch(r'[a-f0-9]{40}', version) for version in re.findall(r'uses: actions/[^@]+@([^\s]+)', text))
