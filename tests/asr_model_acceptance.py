"""Opt-in real CPU ASR acceptance; downloads weights and never substitutes inference.

Run on an isolated Windows runner with FFmpeg/FFprobe and core requirements:
python tests/asr_model_acceptance.py --data <new-dir> --output <new-dir> \
    --audio acceptance-reference.wav --reference acceptance-reference.txt \
    --fixture-manifest acceptance-reference.json --allow-model-downloads

Only a harness-owned synthetic recording is accepted. Reports compare production
FORGE pipelines on that one fixture, not human/accent/noise quality or GPU speed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import venv
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def sanitized(value, roots=()):
    text = str(value)
    for index, path in enumerate(roots):
        text = text.replace(str(path), f'<test-path-{index}>')
        text = text.replace(str(path).replace('\\', '/'), f'<test-path-{index}>')
    text = re.sub(r'https?://[^\s<>\"\']+', lambda m: m[0].split('?', 1)[0], text)
    text = re.sub(r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}', '<redacted-token>', text)
    text = re.sub(r'(?i)(authorization|password|proxy-password|access_token|token)\s*[:=]\s*[^\s,;]+', r'\1=<redacted>', text)
    return text[-8000:]


def json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def clean_words(text):
    """Lowercase Unicode words; retain numeric spelling rather than hide errors."""
    text = unicodedata.normalize('NFKC', text).lower().replace('’', "'")
    return ' '.join(re.findall(r"[^\W_]+(?:'[^\W_]+)*", text, flags=re.UNICODE))


def measured_wer(reference, hypothesis):
    from forge.bark_asr import word_error_rate
    return {'case_punctuation_sensitive': word_error_rate(reference, hypothesis),
            'normalized_words': word_error_rate(clean_words(reference), clean_words(hypothesis)),
            'normalization': 'NFKC/lowercase/word punctuation only; no number substitutions'}


def fresh_folder(path):
    path = Path(path).resolve()
    if path == ROOT or ROOT.is_relative_to(path):
        raise ValueError('Acceptance folders cannot contain the source repository.')
    if path.exists() and any(path.iterdir()):
        raise ValueError('Use a new or empty disposable acceptance folder.')
    path.mkdir(parents=True, exist_ok=True)
    return path


def environment(*, offline=False):
    result = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1',
              'DO_NOT_TRACK': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
              'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '4'}
    for key in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE'):
        if offline:
            result[key] = '1'
        else:
            result.pop(key, None)
    return result


def run_observed(command, log, *, roots=(), timeout=3600, offline=False):
    """Bounded external process; memory is sampled RSS, not an invented OS peak."""
    import psutil
    started = time.monotonic()
    peak, cpu_seconds, expired = 0, 0.0, threading.Event()
    with Path(log).open('w', encoding='utf-8') as output:
        with subprocess.Popen(list(map(str, command)), cwd=ROOT, env=environment(offline=offline),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding='utf-8', errors='replace') as process:
            def stop_tree():
                expired.set()
                try:
                    tree = psutil.Process(process.pid)
                    for child in reversed(tree.children(recursive=True)):
                        try: child.kill()
                        except psutil.Error: pass
                    tree.kill()
                except psutil.Error:
                    pass
            timer = threading.Timer(timeout, stop_tree)
            timer.daemon = True
            timer.start()
            def sample():
                nonlocal peak, cpu_seconds
                while process.poll() is None:
                    try:
                        tree = psutil.Process(process.pid)
                        values = [tree, *tree.children(recursive=True)]
                        memory, used_cpu = 0, 0.0
                        for child in values:
                            try:
                                memory += child.memory_info().rss
                                c = child.cpu_times(); used_cpu += c.user + c.system
                            except psutil.Error:
                                pass
                        peak = max(peak, memory)
                        cpu_seconds = max(cpu_seconds, used_cpu)
                    except psutil.Error:
                        pass
                    time.sleep(.05)
            monitor = threading.Thread(target=sample, daemon=True)
            monitor.start()
            try:
                for line in process.stdout:
                    output.write(sanitized(line, roots) + '\n')
                code = process.wait(timeout=30)
            finally:
                timer.cancel()
                if process.poll() is None:
                    stop_tree(); process.wait(timeout=30)
                monitor.join(timeout=5)
    if expired.is_set():
        raise RuntimeError('Model acceptance operation exceeded its timeout; the test process tree was stopped.')
    if code:
        detail = Path(log).read_text(encoding='utf-8')[-5000:]
        raise RuntimeError(f'Acceptance child exited {code}: {detail}')
    return {'wall_seconds': time.monotonic() - started,
            'sampled_process_tree_peak_rss_bytes': peak,
            'sampled_process_tree_cpu_seconds': cpu_seconds,
            'memory_method': '50ms sampled process-tree RSS; not an OS peak or GPU usage'}


def fixture(audio, reference, manifest, output, settings):
    """Require explicit synthetic provenance and immutable audio/text identities."""
    from forge.worker import probe, duration
    source = json.loads(Path(manifest).read_text(encoding='utf-8'))
    text = Path(reference).read_text(encoding='utf-8').strip()
    if not 20 <= len(text) <= 10000 or not 5 <= len(text.split()) <= 2000:
        raise ValueError('A bounded nonempty reference transcript is required.')
    if source.get('audio_sha256') != digest(audio) or source.get('reference_sha256') != digest(reference):
        raise ValueError('Fixture bytes differ from the generating acceptance manifest.')
    if source.get('engine') != 'kokoro' or source.get('voice') != 'am_onyx':
        raise ValueError('Use the harness-owned original Kokoro Onyx acceptance fixture.')
    if not re.fullmatch(r'[0-9a-f]{40}', source.get('model_revision', '')):
        raise ValueError('The synthetic fixture must identify its actual immutable model revision.')
    info = probe(Path(audio), settings)
    seconds = duration(info)
    if not math.isfinite(seconds) or not 2 <= seconds <= 120:
        raise ValueError('Synthetic ASR fixture must contain 2–120 seconds of audio.')
    # Repeat known speech in the same order to cross more than two Bark windows.
    repetitions = max(2, math.ceil(65 / seconds))
    long_audio = output / 'acceptance-long.wav'
    ffmpeg = settings['ffmpeg']
    subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-n', '-stream_loop', str(repetitions - 1),
                    '-protocol_whitelist', 'file,pipe', '-i', str(audio), '-vn', '-ac', '1', '-ar', '16000',
                    '-c:a', 'pcm_s16le', str(long_audio)], check=True, timeout=180, capture_output=True)
    with wave.open(str(long_audio), 'rb') as stream:
        long_seconds = stream.getnframes() / stream.getframerate()
    if not 60 <= long_seconds <= 240:
        raise ValueError('Long acceptance fixture did not meet its bounded duration contract.')
    return text, long_audio, ' '.join([text] * repetitions), {
        'source': 'Harness-owned real Kokoro Onyx synthesis; not a human speech corpus',
        'audio_sha256': digest(audio), 'reference_sha256': digest(reference),
        'tts_model_revision': source['model_revision'], 'voice': source['voice'],
        'duration_seconds': seconds, 'long_duration_seconds': long_seconds,
        'long_repetitions': repetitions,
    }


def prepare_whisper(data, output, roots):
    path = data / 'environments' / 'acceptance-whisper'
    venv.EnvBuilder(with_pip=True).create(path)
    python = path / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    run_observed([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--timeout', '30',
                  '--retries', '1', '--only-binary=:all:', '-r', ROOT / 'requirements-transcription.txt'],
                 output / 'whisper-install.log', roots=roots, timeout=1200)
    run_observed([python, '-m', 'pip', 'check'], output / 'whisper-pip-check.log', roots=roots, timeout=60)
    return python


def worker(request):
    """Run a real existing FORGE pipeline in a fresh isolated process."""
    data, stage = Path(request['data']), Path(request['stage'])
    stage.mkdir(parents=True, exist_ok=True)
    model = request['model']
    if request['engine'] == 'whisper':
        from forge.model_cache import ensure_model
        path = ensure_model(data, model)
        if request.get('download_only'):
            json_write(request['result'], {'model_revision': path.name})
            return
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        from forge.worker import transcribe
        options = {'model': model, 'model_revision': path.name, 'device': 'cpu', 'language': 'en',
                   'task': 'transcribe', 'timestamps': 'segment', 'formats': ['txt', 'json'],
                   'beam_size': 5, 'vad': True, 'vocabulary': '', 'diarization': False,
                   'style': 'raw', 'remove_fillers': False, 'flag_uncertain': False}
        started = time.monotonic()
        files, warnings = transcribe(Path(request['audio']), stage, 'transcript', options, data)
        report = json.loads(next(p for p in files if p.suffix == '.json').read_text(encoding='utf-8'))
        text = ' '.join(s['text'].strip() for s in report['segments'])
        if any(not isinstance(s.get('start'), (int, float)) or s['start'] > s.get('end', -1) for s in report['segments']):
            raise ValueError('Whisper returned invalid recognized segment times.')
        result = {'text': text, 'model_revision': report['info']['model_revision'],
                  'recognized_segments': len(report['segments']), 'timing': 'Whisper estimated speech segments',
                  'processing_seconds': time.monotonic() - started, 'device': report['info']['device'],
                  'warnings': warnings}
    else:
        from forge.bark_asr import transcribe
        options = {'engine': 'bark', 'model': model, 'device': 'cpu', 'language': 'en',
                   'task': 'transcribe', 'timestamps': 'none', 'formats': ['txt', 'json'],
                   'diarization': False, 'vocabulary': '', 'style': 'raw',
                   'chunk_seconds': 20, 'overlap_seconds': 0}
        files, warnings = transcribe(Path(request['audio']), stage, 'transcript', options, data, request['settings'])
        result = json.loads(next(p for p in files if p.suffix == '.json').read_text(encoding='utf-8'))
        if result.get('segments') != [] or result.get('timing') != 'unavailable':
            raise ValueError('Bark must not fabricate recognized speech/word timings.')
        windows = result.get('chunks', [])
        if not windows or [v['index'] for v in windows] != list(range(len(windows))):
            raise ValueError('Bark returned incomplete or disordered chunks.')
        previous = 0.0
        for item in windows:
            begin, end = item['audio_window']
            if abs(begin - previous) > .002 or end <= begin or end - begin > 20.01:
                raise ValueError('Bark decoding windows are not ordered contiguous bounded audio.')
            previous = end
        if request.get('long') and len(windows) < 3:
            raise ValueError('Long-recording acceptance did not exercise at least three decoding windows.')
        result['decoded_chunks'] = len(windows)
        result['warnings'] = warnings
    if not result['text'].strip() or result.get('device') != 'cpu':
        raise ValueError('CPU inference returned empty speech or did not run on the requested CPU.')
    json_write(request['result'], result)


def run_child(python, engine, model, audio, data, output, settings, *, long=False, download_only=False):
    key = engine + '-' + model + ('-download' if download_only else '-long' if long else '-short')
    folder = output / key
    folder.mkdir()
    request_path, result_path = folder / 'request.json', folder / 'result.json'
    json_write(request_path, {'engine': engine, 'model': model, 'audio': str(audio), 'data': str(data),
                             'stage': str(folder / 'exports'), 'result': str(result_path),
                             'settings': settings, 'long': long, 'download_only': download_only})
    observed = run_observed([python, __file__, '--worker', request_path], folder / 'process.log',
                            roots=(data, output, ROOT, Path.home()), timeout=3600,
                            offline=not download_only)
    if not result_path.is_file():
        raise RuntimeError('Acceptance process did not produce its result.')
    return {**json.loads(result_path.read_text(encoding='utf-8')), 'observed_process': observed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--fixture-manifest', type=Path, required=True)
    parser.add_argument('--bark-model', action='append', choices=['bark-3.0', 'bark-3.0-plus', 'bark-3.0-pro', 'bark-3.0-clean'])
    parser.add_argument('--whisper-model', action='append', choices=['tiny', 'base', 'small', 'medium', 'large-v3', 'large-v3-turbo'])
    parser.add_argument('--skip-bark', action='store_true')
    parser.add_argument('--skip-whisper', action='store_true')
    parser.add_argument('--allow-model-downloads', action='store_true')
    parser.add_argument('--maximum-wer', type=float, default=.35)
    parser.add_argument('--ffmpeg', default=shutil.which('ffmpeg'))
    parser.add_argument('--ffprobe', default=shutil.which('ffprobe'))
    args = parser.parse_args(argv)
    if not args.allow_model_downloads:
        parser.error('Pass --allow-model-downloads to authorize these test-only model/runtime downloads.')
    if not args.ffmpeg or not args.ffprobe:
        parser.error('Real FFmpeg and FFprobe are required.')
    if not math.isfinite(args.maximum_wer) or not 0 <= args.maximum_wer <= .5:
        parser.error('The synthetic fixture WER bound must be finite and between zero and 0.5.')
    bark_models = [] if args.skip_bark else list(dict.fromkeys(args.bark_model or ['bark-3.0-plus', 'bark-3.0-pro']))
    whisper_models = [] if args.skip_whisper else list(dict.fromkeys(args.whisper_model or ['large-v3', 'large-v3-turbo']))
    if not bark_models and not whisper_models:
        parser.error('Select at least one real recognition model.')
    data_path, output_path = args.data.resolve(), args.output.resolve()
    if data_path == output_path or data_path.is_relative_to(output_path) or output_path.is_relative_to(data_path):
        parser.error('Model data and public evidence folders must be separate, non-nested directories.')
    data, output = fresh_folder(data_path), fresh_folder(output_path)
    settings = {'ffmpeg': str(Path(args.ffmpeg).resolve()), 'ffprobe': str(Path(args.ffprobe).resolve())}
    report = {'schema': 1, 'status': 'failed', 'source_commit': os.getenv('GITHUB_SHA', ''),
              'platform': platform.platform(), 'python': platform.python_version(),
              'native_windows_execution': os.name == 'nt', 'device': 'cpu', 'physical_gpu_tested': False,
              'inference_substituted': False, 'results': [], 'failures': [],
              'limits': ['Only one clean synthetic English voice is tested.',
                         'No human/accent/background-noise corpus or owner hardware is measured.',
                         'Results do not establish comparative general accuracy or GPU support.']}
    roots = (data, output, ROOT, Path.home())
    try:
        reference, long_audio, long_reference, fixture_info = fixture(args.audio.resolve(), args.reference.resolve(), args.fixture_manifest.resolve(), output, settings)
        report['fixture'] = fixture_info
        from forge import bark_asr
        catalog = bark_asr.catalog(data)
        if {v['id'] for v in catalog} != {'bark-3.0', 'bark-3.0-plus', 'bark-3.0-pro', 'bark-3.0-clean'}:
            raise ValueError('Bark catalog is incomplete.')
        report['bark_catalog'] = [{'id': item['id'], 'repository': item['repository'], 'non_verbatim': item['non_verbatim']} for item in catalog]
        tasks = [('bark', model) for model in bark_models] + [('whisper', model) for model in whisper_models]
        whisper_python = None
        for engine, model in tasks:
            phase = 'installation'
            try:
                print('ASR acceptance: ' + engine + '/' + model + ' installation', flush=True)
                started = time.monotonic()
                if engine == 'bark':
                    # Workflow offline flags must not accidentally disable the explicit install.
                    for key in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE'):
                        os.environ.pop(key, None)
                    installed = bark_asr.install(model, data, consent=True, device='cpu')
                    python = Path(sys.executable)
                else:
                    if whisper_python is None:
                        whisper_python = prepare_whisper(data, output, roots)
                    python = whisper_python
                    installed = run_child(python, engine, model, args.audio.resolve(), data, output, settings, download_only=True)
                install_seconds = time.monotonic() - started
                phase = 'offline inference'
                for is_long, audio, expected in [(False, args.audio.resolve(), reference), (True, long_audio, long_reference)]:
                    result = run_child(python, engine, model, audio, data, output, settings, long=is_long)
                    rates = measured_wer(expected, result['text'])
                    item = {'engine': engine, 'model': model, 'scope': 'long-repeated' if is_long else 'short',
                            'installation_seconds': install_seconds,
                            'model_revision': result.get('model_revision', installed.get('revision')),
                            'reference_words': len(expected.split()), 'hypothesis': result['text'],
                            'wer': rates, 'processing_seconds': result['processing_seconds'],
                            'sampled_process_tree_peak_rss_bytes': result['observed_process']['sampled_process_tree_peak_rss_bytes'],
                            'sampled_process_tree_cpu_seconds': result['observed_process']['sampled_process_tree_cpu_seconds'],
                            'peak_process_memory_bytes': result.get('peak_process_memory_bytes'),
                            'peak_cuda_allocated_bytes': result.get('peak_cuda_allocated_bytes'),
                            'device': result['device'], 'offline_inference': True,
                            'decoded_chunks': result.get('decoded_chunks'), 'timing': result.get('timing'),
                            'warnings': result.get('warnings', [])}
                    report['results'].append(item)
                    if rates['normalized_words'] is None or rates['normalized_words'] > args.maximum_wer:
                        raise ValueError(f'Synthetic-fixture normalized WER exceeded the declared {args.maximum_wer:.2f} acceptance bound.')
                    if not re.fullmatch(r'[0-9a-f]{40}', item['model_revision'] or ''):
                        raise ValueError('Inference did not identify an immutable model revision.')
                    notice = {key: item[key] for key in ('engine', 'model', 'scope', 'model_revision', 'wer',
                              'processing_seconds', 'sampled_process_tree_peak_rss_bytes', 'decoded_chunks')}
                    message = json.dumps(notice, ensure_ascii=True).replace('%', '%25')
                    print('::notice title=Actual ASR acceptance::' + message, flush=True)
                print('ASR acceptance: ' + engine + '/' + model + ' passed actual CPU inference', flush=True)
            except Exception as error:
                failure = {'engine': engine, 'model': model, 'phase': phase, 'error': sanitized(type(error).__name__ + ': ' + str(error), roots)}
                report['failures'].append(failure)
                print('::error title=ASR model acceptance::' + failure['error'].replace('%', '%25').replace('\n', '%0A').replace('\r', '%0D'), flush=True)
        if report['failures']:
            raise RuntimeError('One or more actual recognition acceptance checks failed; no release approval receipt was produced.')
        report['status'] = 'passed'
    except Exception as error:
        if not report['failures']:
            report['failures'].append({'phase': 'fixture/setup', 'error': sanitized(type(error).__name__ + ': ' + str(error), roots)})
    finally:
        json_write(output / 'ASR_MODEL_ACCEPTANCE.json', report)
    print(json.dumps({'status': report['status'], 'actual_inference_results': len(report['results']), 'failures': len(report['failures'])}), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        request = Path(sys.argv[2])
        if request.stat().st_size > 64000:
            raise SystemExit('Acceptance worker request exceeds its size limit.')
        worker(json.loads(request.read_text(encoding='utf-8')))
    else:
        raise SystemExit(main())
