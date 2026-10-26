"""Fail-closed collection of actual native Windows CPU speech evidence.

This collector verifies receipts produced by the opt-in inference harnesses. Its
unit tests use synthetic contract records; those tests are not model acceptance.
CPU acceptance never establishes CUDA or owner-hardware acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import zipfile


SHA = re.compile(r'[a-f0-9]{40}')
BARK_MODELS = ('bark-3.0', 'bark-3.0-plus', 'bark-3.0-pro', 'bark-3.0-clean')
CASES = {'short', 'long-repeated', 'noise-20db', 'synthetic-british'}
KOKORO_AUDIO = ('acceptance-reference', 'onyx-compatible-blend', 'onyx-offline-repeat',
                'acceptance-reference-british', *(f'kokoro-language-{code}' for code in 'abefhipjz'))
MING_AUDIO = ('ming-builtin', 'ming-reference', 'ming-design', 'ming-emotion',
              'ming-freeform', 'ming-basic', 'ming-multilingual', 'ming-dialect', 'ming-offline-repeat')
EVIDENCE_NAMES = {'README.md', 'LICENSE', 'LICENSE.txt', 'COPYING', 'NOTICE'}
FORBIDDEN_PARTS = {'models', 'environments', 'envs', '.venv', 'venv', '__pycache__', '.cache', 'site-packages'}


class EvidenceError(ValueError):
    """Required actual inference evidence is missing or inconsistent."""


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def read_record(path):
    require(path.is_file() and not path.is_symlink(), f'Missing regular receipt: {path.name}')
    require(path.stat().st_size <= 10_000_000, f'Oversized receipt: {path.name}')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise EvidenceError(f'Invalid JSON receipt: {path.name}') from error
    require(isinstance(value, dict), f'Receipt must be an object: {path.name}')
    return value


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def cpu_record(record, commit, label):
    require(record.get('source_commit') == commit, f'{label}: source commit differs from checkout')
    require(record.get('status') == 'passed', f'{label}: actual acceptance did not pass')
    require(record.get('native_windows_execution') is True, f'{label}: native Windows execution required')
    require(record.get('device') == 'cpu' and record.get('physical_gpu_tested') is False,
            f'{label}: receipt must explicitly identify CPU-only acceptance')
    require(record.get('inference_substituted', False) is False, f'{label}: substituted inference is forbidden')


def validate_tts(record, folder, engine, commit):
    cpu_record(record, commit, engine)
    require(record.get('engine') == engine and record.get('scope') == 'full', f'{engine}: full engine scope required')
    require(SHA.fullmatch(record.get('model_revision', '')) is not None and
            re.fullmatch('speech-' + engine + r'-[a-f0-9]{16}', record.get('runtime_revision', '')) is not None,
            f'{engine}: immutable revisions required')
    tests = record.get('tests', [])
    phases = record.get('phases', [])
    require(tests and phases and all(item.get('status') == 'passed' for item in tests + phases),
            f'{engine}: all actual synthesis checks and phases must pass')
    audio = {item.get('audio'): item for item in tests if item.get('audio')}
    require(len(audio) == sum(bool(item.get('audio')) for item in tests), f'{engine}: duplicate audio checks')
    for name in KOKORO_AUDIO if engine == 'kokoro' else MING_AUDIO:
        item = audio.get(name + '.wav', {})
        path = folder / (name + '.wav')
        metrics = item.get('metrics', {})
        require(path.is_file() and item.get('model_revision') == record['model_revision'] and
                item.get('runtime_revision') == record['runtime_revision'], f'{engine}: missing actual synthesis {name}')
        require(metrics.get('sha256') == digest(path) and positive(metrics.get('duration_seconds')) and
                positive(metrics.get('peak')) and metrics.get('clipped_fraction', 1) <= .1,
                f'{engine}: synthesized audio differs from measured receipt: {name}')
    return record


def validate_asr(record, model, commit, fixture):
    cpu_record(record, commit, model)
    require(record.get('inference_substituted') is False and record.get('failures') == [],
            f'{model}: actual inference must not be substituted or failed')
    pairs = {('bark', model)}
    if model == 'bark-3.0-plus':
        pairs.add(('whisper', 'large-v3'))
    if model == 'bark-3.0-pro':
        pairs.add(('whisper', 'large-v3-turbo'))
    results = record.get('results', [])
    expected = {(engine, name, scope) for engine, name in pairs for scope in CASES}
    actual = {(item.get('engine'), item.get('model'), item.get('scope')) for item in results}
    require(actual == expected and len(results) == len(expected), f'{model}: missing Bark/Whisper model or fixture case')
    source = record.get('fixture', {})
    require(source.get('tts_model_revision') == fixture['model_revision'] and source.get('voice') == 'am_onyx'
            and source.get('audio_sha256') == fixture['audio_sha256']
            and source.get('reference_sha256') == fixture['reference_sha256']
            and positive(source.get('long_duration_seconds')) and source['long_duration_seconds'] >= 60,
            f'{model}: recognition fixture differs from actual Kokoro synthesis')
    require(record.get('synthetic_british_fixture', {}).get('status') == 'available',
            f'{model}: provider-labeled British fixture was not exercised')
    noise = record.get('controlled_noise_fixture', {})
    require(noise.get('target_snr_db') == 20 and positive(noise.get('measured_snr_db'))
            and abs(noise['measured_snr_db'] - 20) <= .1 and noise.get('clipped_samples') == 0,
            f'{model}: controlled-noise fixture is missing or invalid')
    for item in results:
        wer = item.get('wer', {}).get('normalized_words')
        require(type(wer) in (int, float) and math.isfinite(wer) and 0 <= wer <= .35
                and isinstance(item.get('hypothesis'), str) and item['hypothesis'].strip(),
                f'{model}: actual normalized recognition error bound failed')
        require(SHA.fullmatch(item.get('model_revision', '')) is not None
                and item.get('device') == 'cpu' and item.get('offline_inference') is True
                and positive(item.get('processing_seconds'))
                and positive(item.get('sampled_process_tree_peak_rss_bytes')),
                f'{model}: missing immutable offline CPU inference measurements')
        if item['engine'] == 'bark':
            require(item.get('timing') == 'unavailable', f'{model}: Bark must not claim speech timestamps')
            if item['scope'] == 'long-repeated':
                require(type(item.get('decoded_chunks')) is int and item['decoded_chunks'] >= 3,
                        f'{model}: long recording must exercise three decoding windows')
    return record


def collect(evidence, output, *, source_commit, run_id):
    require(SHA.fullmatch(source_commit) is not None, 'An exact checked-out source commit is required')
    require(re.fullmatch(r'[1-9]\d*', run_id) is not None, 'A numeric Actions run ID is required')
    evidence, output = Path(evidence).resolve(), Path(output).resolve()
    require(evidence.is_dir() and not output.is_relative_to(evidence), 'Evidence and output must be separate directories')
    names = ['publishers', 'kokoro', 'ming', *BARK_MODELS, 'kokoro-pipeline']
    folders = {name: evidence / f'speech-{name}-{run_id}' for name in names}
    require({path.name for path in evidence.iterdir()} == {path.name for path in folders.values()},
            'Expected exactly the eight separate model/publisher/pipeline artifact directories')
    for name, folder in folders.items():
        require(folder.is_dir() and not folder.is_symlink(), f'Missing artifact directory: {name}')
    publisher = read_record(folders['publishers'] / 'publisher-access.json')
    entries = publisher.get('entries', [])
    require(publisher.get('source_commit') == source_commit and publisher.get('platform') == 'win32'
            and publisher.get('all_publishers_accessible') is True
            and publisher.get('model_inference_tested') is False and publisher.get('weights_downloaded') is False,
            'Native publisher metadata check did not pass for the checked-out commit')
    require({item.get('engine') for item in entries} == {'kokoro', 'ming', *BARK_MODELS} and len(entries) == 6,
            'All six publisher repositories must be checked')
    for item in entries:
        require(item.get('status') == 'passed' and SHA.fullmatch(item.get('revision', '')) is not None
                and item.get('notice_sha256'), 'Publisher metadata or notices are missing')
        for name, expected in item['notice_sha256'].items():
            require(name in EVIDENCE_NAMES, 'Unexpected publisher notice filename')
            path = folders['publishers'] / item['engine'] / name
            require(path.is_file() and digest(path) == expected, 'Publisher notice checksum differs')
    receipts = {'publishers': publisher}
    for engine in ('kokoro', 'ming'):
        receipts[engine] = validate_tts(read_record(folders[engine] / f'tts-{engine}-acceptance.json'),
                                        folders[engine], engine, source_commit)
    fixture = read_record(folders['kokoro'] / 'acceptance-reference.json')
    require(fixture.get('model_revision') == receipts['kokoro']['model_revision']
            and fixture.get('audio_sha256') == digest(folders['kokoro'] / 'acceptance-reference.wav')
            and fixture.get('reference_sha256') == digest(folders['kokoro'] / 'acceptance-reference.txt')
            and fixture.get('synthetic_test_owned') is True, 'Synthetic reference provenance is inconsistent')
    for model in BARK_MODELS:
        receipts[model] = validate_asr(read_record(folders[model] / 'ASR_MODEL_ACCEPTANCE.json'), model, source_commit, fixture)
    pipeline = read_record(folders['kokoro-pipeline'] / 'PIPELINE_ACCEPTANCE.json')
    cpu_record(pipeline, source_commit, 'queued speech pipeline')
    require(pipeline.get('model_inference_tested') is True and pipeline.get('model_revisions') == [fixture['model_revision']]
            and positive(pipeline.get('first_segments')) and positive(pipeline.get('edited_segments'))
            and positive(pipeline.get('reused_segments')) and len(pipeline.get('checks', [])) >= 4,
            'Actual API/queue/playback/edit-and-reuse pipeline evidence is incomplete')
    receipts['kokoro-pipeline'] = pipeline
    files, total = [], 0
    for path in sorted(evidence.rglob('*')):
        relative = path.relative_to(evidence)
        require(not path.is_symlink(), 'Symlinks are forbidden in public inference evidence')
        if path.is_dir():
            continue
        require(path.is_file() and not FORBIDDEN_PARTS.intersection(part.lower() for part in relative.parts)
                and (path.suffix.lower() in {'.json', '.wav', '.txt', '.log', '.md'}
                     or path.name in EVIDENCE_NAMES or path.name.startswith(('checkpoint-LICENSE', 'runtime-LICENSE',
                                                                               'checkpoint-NOTICE', 'runtime-NOTICE',
                                                                               'checkpoint-COPYING', 'runtime-COPYING'))),
                'Only bounded speech evidence may be published; models/environments are forbidden')
        size = path.stat().st_size
        total += size
        require(size <= 120_000_000 and total <= 500_000_000, 'Speech evidence exceeds its bounded size limit')
        files.append({'path': relative.as_posix(), 'bytes': size, 'sha256': digest(path)})
    require(not output.exists() or not any(output.iterdir()), 'Refusing to overwrite existing acceptance output')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'SPEECH_EVIDENCE.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
        for item in files:
            zipped.write(evidence / item['path'], item['path'])
    result = {'schema': 1, 'status': 'passed', 'source_commit': source_commit,
              'actions_run_id': run_id, 'scope': 'native-windows-cpu-synthetic-speech',
              'native_windows_execution': True, 'device': 'cpu', 'physical_gpu_tested': False,
              'full_hardware_acceptance': False, 'inference_substituted': False,
              'limitations': ['CPU-only measured model and application-pipeline acceptance; CUDA and owner hardware remain untested.',
                              'Controlled synthetic English/noise/British-voice fixtures do not establish real-world comparative accuracy.',
                              'Listening quality and remaining owner review are not waived.'],
              'receipts': receipts, 'files': files,
              'evidence_archive': {'name': archive.name, 'sha256': digest(archive)}}
    (output / 'SPEECH_ACCEPTANCE.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    try:
        collect(args.evidence, args.output, source_commit=args.source_commit, run_id=args.run_id)
    except (EvidenceError, OSError) as error:
        parser.exit(1, f'Speech CPU acceptance gate failed: {error}\n')
    print('Actual native Windows CPU model and pipeline evidence passed; GPU/hardware acceptance is not implied.')


if __name__ == '__main__':
    main()
