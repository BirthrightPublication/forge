"""Run real isolated Dubbing model smoke checks; never publish release approval.

The hosted Windows workflow uses CPU. A local owner can select CUDA after
installing the reviewed CUDA packs through FORGE. Model installation is refused
unless --approve-install is explicitly supplied. A sanitized JSON report and a
generated listening sample are retained locally. CI uploads only the JSON;
references, environments and model checkpoints never become evidence artifacts.
"""
from __future__ import annotations

import argparse
from array import array
from datetime import datetime, timezone
import difflib
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from forge import dubbing_models as models

REPORT_NAME = 'DUBBING_MODEL_SMOKE.json'
COMPONENTS = ('opus-en-ru', 'qwen3-tts-0.6b-base', 'whisper-tiny')
SOURCE_FILES = ('forge/dubbing_models.py', 'scripts/dubbing_runtime.py', 'scripts/dubbing_cpu_acceptance.py')
REFERENCE_TEXT = 'Hello. This is a local speech recognition test. The next meeting is on Friday at ten in the morning.'
RUSSIAN_TEXT = 'Привет. Это проверка русского голоса.'


class AcceptanceBlocked(ValueError):
    """A required identity, dependency, approval or real device is unavailable."""


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def source_identity(root=None, additional_files=()):
    root = Path(root or ROOT).resolve()
    names = tuple(dict.fromkeys((*SOURCE_FILES, *additional_files)))
    if any(not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
           or not (root / name).resolve().is_relative_to(root) for name in names):
        raise AcceptanceBlocked('Source identity requires ordinary application-relative source files.')
    files = {name: digest(root / name) for name in names}
    manifest = root / 'release-manifest.json'
    value = json.loads(manifest.read_text('utf-8'))
    if any(value.get('files', {}).get(name) != sha for name, sha in files.items()):
        raise AcceptanceBlocked('The installed adapter bytes do not match the application manifest. Use the normal verified FORGE updater.')
    identity = {'source_files_sha256': files, 'source_manifest_sha256': digest(manifest)}
    if (root / '.git').exists():
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
        committed_manifest = subprocess.check_output(['git', 'show', 'HEAD:release-manifest.json'], cwd=root)
        if committed_manifest != manifest.read_bytes():
            raise AcceptanceBlocked('The source manifest differs from its committed identity. Commit verified source before acceptance.')
        identity.update(source_commit=commit, source_kind='git', source_checkout_clean=not bool(
            subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, text=True).strip()))
    else:
        package = value.get('package', {})
        commit = package.get('source_commit', '')
        original = package.get('source_manifest_sha256', '')
        if not isinstance(original, str) or not re.fullmatch('[a-f0-9]{64}', original):
            raise AcceptanceBlocked('This package has no verified original source manifest identity.')
        identity.update(installed_manifest_sha256=identity['source_manifest_sha256'], source_manifest_sha256=original)
        identity.update(source_commit=commit, source_kind='installed_manifest', source_checkout_clean=None)
    if not isinstance(identity['source_commit'], str) or not re.fullmatch('[a-f0-9]{40}', identity['source_commit']):
        raise AcceptanceBlocked('This application has no exact source commit binding. Use a FORGE package carrying its immutable source identity.')
    return identity


def _desired_device(component, device):
    return 'cpu' if component == 'whisper-tiny' else device


def installed_identities(data_root, device='cpu'):
    if device not in {'cpu', 'cuda'}:
        raise AcceptanceBlocked('Choose CPU or CUDA.')
    result = {}
    for component in COMPONENTS:
        selected = models.resolve(component, data_root)
        if selected['device'] != _desired_device(component, device):
            raise AcceptanceBlocked('Install the selected device profile through the reviewed FORGE setup plan before testing: ' + component + '.')
        result[component] = {key: selected[key] for key in ('model_revision', 'runtime_revision', 'device')}
    return result


def _controller_identity():
    packages = sorted((str(item.metadata.get('Name') or ''), item.version) for item in importlib.metadata.distributions())
    value = {'python_sha256': digest(Path(sys.executable).resolve()),
             'packages_sha256': hashlib.sha256(json.dumps(packages, sort_keys=True).encode()).hexdigest()}
    config = Path(sys.prefix) / 'pyvenv.cfg'
    if config.exists():
        value['pyvenv_cfg_sha256'] = digest(config)
    return value


def _safe_error(error, data_root, output):
    detail = type(error).__name__ + ': ' + str(error)
    context = getattr(error, 'stderr', None)
    if isinstance(context, bytes):
        context = context.decode('utf-8', errors='replace')
    if isinstance(context, str) and context.strip():
        detail += '\n' + context[-2000:]
    for path in sorted({str(data_root), str(output), str(ROOT), str(Path.home()), str(sys.executable)}, key=len, reverse=True):
        if path:
            detail = detail.replace(path, '<local path>')
    detail = re.sub(r'https?://[^\s\"<>]+', '<publisher URL>', detail)
    detail = re.sub(r'(?i)(authorization|token|password|secret)\s*[=:]\s*[^\s,;]+', r'\1=<private>', detail)
    detail = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|hf_[A-Za-z0-9]+)\b', '<private>', detail)
    return detail[-1500:]


def media_probe(path):
    completed = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration:stream=codec_name,codec_type,sample_rate,channels', '-of', 'json', str(path)],
        check=True, capture_output=True, text=True, timeout=30)
    result = json.loads(completed.stdout)
    seconds = float(result.get('format', {}).get('duration', 0))
    if not math.isfinite(seconds) or not .05 <= seconds <= 30:
        raise AcceptanceBlocked('The generated smoke audio has no bounded playable duration.')
    streams = result.get('streams', [])
    if len(streams) != 1 or streams[0].get('codec_type') != 'audio' or streams[0].get('channels') != 1:
        raise AcceptanceBlocked('The smoke fixture/output must contain exactly one mono audio stream.')
    return {'duration': seconds, 'sample_rate': int(streams[0]['sample_rate']), 'channels': 1,
            'codec': streams[0]['codec_name'], 'bytes': Path(path).stat().st_size, 'sha256': digest(path)}


def decode_audio(path):
    meta = media_probe(path)
    completed = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:a:0',
        '-t', '30', '-ac', '1', '-ar', '24000', '-f', 'f32le', 'pipe:1'], check=True,
        capture_output=True, timeout=60)
    if not completed.stdout or len(completed.stdout) > 30 * 24000 * 4 or len(completed.stdout) % 4:
        raise AcceptanceBlocked('Decoded smoke audio exceeded its bounds or contained no samples.')
    samples = array('f'); samples.frombytes(completed.stdout)
    if sys.byteorder != 'little':
        samples.byteswap()
    if not all(math.isfinite(value) for value in samples):
        raise AcceptanceBlocked('Decoded speech contains nonfinite audio samples.')
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    if peak < .001 or rms < .0001:
        raise AcceptanceBlocked('The model output is silent; no audible inference pass is recorded.')
    return {**meta, 'decoded_and_probed': True, 'finite_samples': True, 'peak': peak, 'rms': rms}


def make_reference(directory):
    directory.mkdir(parents=True, exist_ok=False)
    text = directory / 'generated-reference.txt'; text.write_text(REFERENCE_TEXT, encoding='utf-8')
    target = directory / 'generated-reference.wav'
    if os.name == 'nt':
        raw = directory / 'sapi-reference.wav'
        script = """
$ErrorActionPreference='Stop'
$voice=New-Object -ComObject SAPI.SpVoice
$voices=$voice.GetVoices('Language=409')
if ($voices.Count -lt 1) { throw 'No installed English SAPI voice; generate no substitute recording.' }
$voice.Voice=$voices.Item(0)
$stream=New-Object -ComObject SAPI.SpFileStream
$stream.Format.Type=22
try {
  $stream.Open($env:FORGE_SMOKE_REFERENCE,3,$false)
  $voice.AudioOutputStream=$stream
  [void]$voice.Speak($env:FORGE_SMOKE_TEXT)
} finally { $stream.Close() }
"""
        env = {**os.environ, 'FORGE_SMOKE_REFERENCE': str(raw), 'FORGE_SMOKE_TEXT': REFERENCE_TEXT}
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                       check=True, capture_output=True, timeout=60, env=env)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-i', str(raw), '-ar', '24000',
                        '-ac', '1', '-c:a', 'pcm_s16le', str(target)], check=True, capture_output=True, timeout=30)
        provenance = 'Windows installed English SAPI voice, generated locally from fixed harness text.'
    else:
        # Run from the owned fixture directory so FFmpeg sees a simple relative
        # text filename instead of parsing private Windows/path punctuation.
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-f', 'lavfi', '-i',
            'flite=textfile=generated-reference.txt:voice=slt', '-ar', '24000', '-ac', '1',
            '-c:a', 'pcm_s16le', str(target)], cwd=directory, check=True, capture_output=True, timeout=60)
        provenance = 'FFmpeg libflite slt, generated locally from fixed harness text.'
    meta = decode_audio(target)
    if not 1 <= meta['duration'] <= 30:
        raise AcceptanceBlocked('The generated reference is outside the model reference limits.')
    return target, provenance, meta


def _metrics(value, desired):
    if value.get('inference_tested') is not True or value.get('actual_device') != desired:
        raise AcceptanceBlocked('A real inference result from the requested device is required.')
    actual_gpu = value.get('physical_gpu_tested') is True
    if actual_gpu != (desired == 'cuda'):
        raise AcceptanceBlocked('The inference result does not prove the requested physical device.')
    result = {key: value[key] for key in ('model_revision', 'runtime_revision', 'elapsed_seconds', 'actual_device', 'physical_gpu_tested')}
    result['inference_tested'] = True
    if desired == 'cuda':
        for key in ('gpu_name', 'gpu_capability', 'peak_vram_bytes', 'peak_reserved_vram_bytes'):
            if key not in value:
                raise AcceptanceBlocked('Physical GPU evidence is incomplete: ' + key + '.')
            result[key] = value[key]
        if not result['gpu_name'] or type(result['peak_vram_bytes']) is not int or result['peak_vram_bytes'] <= 0:
            raise AcceptanceBlocked('The GPU result has no positive actual allocation evidence.')
    return result


def _install(data_root, device, progress):
    groups = [(COMPONENTS, 'cpu')] if device == 'cpu' else [(COMPONENTS[:2], 'cuda'), (COMPONENTS[2:], 'cpu')]
    receipts = []
    for components, chosen in groups:
        change_device = False
        for component in components:
            try:
                change_device |= models.resolve(component, data_root)['device'] != chosen
            except (models.ComponentUnavailable, ValueError):
                pass
        action = 'repair' if change_device else 'install'
        plan = models.installation_plan(list(components), data_root, device=chosen, action=action)
        receipts.append({'components': list(components), 'device': chosen, 'action': action,
                         'quoted_download_bytes': plan['download_bytes'], 'quoted_storage_bytes': plan['required_storage_bytes']})
        for component in components:
            models.install(component, data_root, consent=True, plan=plan, device=chosen, action=action,
                           repair=action == 'repair', progress=progress)
    return receipts


def run_acceptance(data_root, output, *, device='cpu', approve_install=False, require_native_windows=False,
                   require_clean_source=False, expected_models=None, expected_source=None, progress=None):
    data_root, output = Path(data_root).resolve(), Path(output).resolve()
    if data_root == ROOT or data_root in ROOT.parents or data_root == data_root.parent:
        raise AcceptanceBlocked('Choose the dedicated model data directory, not the application/source root.')
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema': 1, 'kind': 'dubbing_model_smoke', 'status': 'blocked',
              'observed_at': datetime.now(timezone.utc).isoformat(), 'requested_device': device,
              'native_windows_execution': os.name == 'nt', 'physical_gpu_tested': False,
              'inference_substituted': False, 'private_inputs_used': False, 'reference_consent': True,
              'through_forge_gui_install': False, 'satisfies_release_acceptance': False,
              'platform': {'system': platform.system(), 'architecture': platform.machine(), 'python': platform.python_version()},
              'components': {}, 'remaining_acceptance': ['GUI install/repair', 'native updater/upgrade/rollback',
                  'human translation and voice quality review', 'live reviewed YouTube OAuth/private upload', 'final release inventory and owner approval']}
    protected = _controller_identity()
    try:
        if device not in {'cpu', 'cuda'}:
            raise AcceptanceBlocked('Choose CPU or CUDA.')
        if require_native_windows and os.name != 'nt':
            raise AcceptanceBlocked('This check requires actual native Windows execution; Linux/cross-platform output is not substituted.')
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise AcceptanceBlocked('Install the verified FFmpeg and FFprobe prerequisites before downloading models.')
        extra_files = tuple(expected_source.get('source_files_sha256', {})) if expected_source is not None else ()
        source = source_identity(additional_files=extra_files); report.update(source)
        if require_clean_source and (source['source_kind'] != 'git' or source['source_checkout_clean'] is not True):
            raise AcceptanceBlocked('The native Actions check requires a clean checkout of its exact source commit.')
        if expected_source is not None and source != expected_source:
            raise AcceptanceBlocked('Application source changed after the acceptance request; review and queue it again.')
        if approve_install:
            if expected_models is not None:
                raise AcceptanceBlocked('A frozen installed-model check cannot authorize an installation.')
            report['installation_plans'] = _install(data_root, device, progress)
        identities = installed_identities(data_root, device)
        if expected_models is not None and identities != expected_models:
            raise AcceptanceBlocked('Installed model/runtime identities changed after approval; queue a fresh check.')
        report['installed_identities'] = identities
        for component in COMPONENTS:
            report['components'][component] = {'verification': models.verify(component, data_root)}
        fixture_root = data_root / 'dubbing' / 'acceptance-fixtures' / uuid.uuid4().hex
        reference, provenance, reference_media = make_reference(fixture_root)
        report.update(reference_provenance=provenance, reference_text=REFERENCE_TEXT, reference_media=reference_media)
        base = {'model_revision': identities['opus-en-ru']['model_revision'], 'device': device}
        translation = models.test('opus-en-ru', data_root, request={**base, 'mode': 'faithful',
            'glossary': {'FORGE': 'ФОРДЖ'}, 'segments': [
                {'id': 'owned-1', 'start': 0, 'end': 3, 'speaker': 'test-speaker', 'source_text': 'Hello. This is a local translation test.'},
                {'id': 'owned-2', 'start': 3, 'end': 7, 'speaker': 'test-speaker', 'source_text': 'FORGE keeps the original video. The project has 3 segments.'},
                {'id': 'owned-3', 'start': 7, 'end': 10, 'speaker': 'test-speaker', 'source_text': 'The meeting starts on 2026-11-13 at 10:30.'}]}, progress=progress)
        translated = translation.get('segments', [])
        if len(translated) != 3 or translation.get('generation_calls', 0) <= 0 or any(not re.search('[А-Яа-яЁё]', row.get('text', '')) for row in translated):
            raise AcceptanceBlocked('The translation model produced no actual Russian draft for every fixture segment.')
        for index, row in enumerate(translated):
            if (row.get('id'), row.get('start'), row.get('end'), row.get('speaker')) != ('owned-' + str(index + 1), [0, 3, 7][index], [3, 7, 10][index], 'test-speaker'):
                raise AcceptanceBlocked('Translation changed the stable source identity/timeline.')
            if re.search(r'(?:\.\s*){4,}', row['text']):
                raise AcceptanceBlocked('Punctuation-only decoder repetition is still present.')
        if 'ФОРДЖ' not in translated[1]['text'] or '3' not in translated[1]['text'] or '2026-11-13' not in translated[2]['text'] or '10:30' not in translated[2]['text']:
            raise AcceptanceBlocked('A protected glossary term, number or date was lost.')
        report['components']['opus-en-ru']['inference'] = {**_metrics(translation, device), 'segments': translated,
            'generation_calls': translation['generation_calls'], 'human_quality_approved': False}
        english = models.test('whisper-tiny', data_root, request={'source': str(reference), 'language': 'en',
            'device': 'cpu', 'model_revision': identities['whisper-tiny']['model_revision']}, progress=progress)
        english_meta = _metrics(english, 'cpu')
        segments = english.get('segments', [])
        if not segments:
            raise AcceptanceBlocked('The recognizer produced no real speech segments.')
        for segment in segments:
            if not 0 <= segment['start'] < segment['end'] <= reference_media['duration'] + .15 or not segment.get('words'):
                raise AcceptanceBlocked('Actual segment/word timings are missing or outside the source clip.')
            if any(not math.isfinite(word['start'] + word['end']) or not 0 <= word['start'] <= word['end'] <= reference_media['duration'] + .15 for word in segment['words']):
                raise AcceptanceBlocked('Actual word timings are invalid.')
        recognized = ' '.join(row['source_text'] for row in segments)
        expected_words = set(re.findall('[a-z]+', REFERENCE_TEXT.lower()))
        coverage = len(expected_words & set(re.findall('[a-z]+', recognized.lower()))) / len(expected_words)
        if coverage < .65:
            raise AcceptanceBlocked('The generated English fixture was not recognized with sufficient smoke-test coverage.')
        report['components']['whisper-tiny']['inference'] = {**english_meta, 'segments': segments,
            'timings_verified': True, 'owned_fixture_word_coverage': coverage}
        voice = models.test('qwen3-tts-0.6b-base', data_root, request={'device': device, 'consent': True,
            'ref_audio': str(reference), 'ref_audio_sha256': reference_media['sha256'], 'ref_text': REFERENCE_TEXT,
            'text': RUSSIAN_TEXT, 'max_new_tokens': 256,
            'model_revision': identities['qwen3-tts-0.6b-base']['model_revision']}, progress=progress)
        voice_meta = _metrics(voice, device)
        generated = Path(voice['path']); speech_media = decode_audio(generated)
        if not .5 <= speech_media['duration'] <= 30 or voice.get('sha256') != speech_media['sha256']:
            raise AcceptanceBlocked('The actual saved Russian speech output failed decoding/hash checks.')
        if voice.get('reference_sha256') != reference_media['sha256'] or digest(reference) != reference_media['sha256']:
            raise AcceptanceBlocked('The approved generated reference changed during synthesis.')
        shutil.copyfile(generated, output / 'Russian-acceptance.wav')
        russian = models.invoke('whisper-tiny', data_root, {'task': 'transcribe', 'source': str(generated),
            'language': 'ru', 'device': 'cpu', 'model_revision': identities['whisper-tiny']['model_revision']}, progress=progress)
        _metrics(russian, 'cpu')
        recovered = ' '.join(row['source_text'] for row in russian.get('segments', []))
        normalize = lambda text: ' '.join(re.findall('[а-яё]+', text.lower()))
        similarity = difflib.SequenceMatcher(None, normalize(RUSSIAN_TEXT), normalize(recovered)).ratio()
        if similarity < .6:
            raise AcceptanceBlocked('Independent local recognition did not recover the generated Russian smoke phrase.')
        report['components']['qwen3-tts-0.6b-base']['inference'] = {**voice_meta, 'text': RUSSIAN_TEXT,
            'media': speech_media, 'reference_sha256': reference_media['sha256'], 'human_quality_approved': False,
            'independent_recognition': {'text': recovered, 'normalized_character_similarity': similarity,
                                        'model_revision': russian['model_revision']}}
        if installed_identities(data_root, device) != identities or source_identity(additional_files=extra_files) != source:
            raise AcceptanceBlocked('Source or model/runtime identities changed during acceptance; retain the outputs and queue a fresh check.')
        # A separate controller process must read the persisted real receipts.
        code = "import json,sys;from forge import dubbing_models as m;v=m.catalog(sys.argv[1]);print(json.dumps([{k:r[k] for k in ('id','ready','inference_verified','model_revision','runtime_revision','device')} for r in v if r['id'] in " + repr(COMPONENTS) + "]))"
        reopened = json.loads(subprocess.check_output([sys.executable, '-c', code, str(data_root)], cwd=ROOT, text=True, timeout=180))
        if len(reopened) != 3 or not all(row['ready'] and row['inference_verified'] and row['model_revision'] == identities[row['id']]['model_revision'] and row['runtime_revision'] == identities[row['id']]['runtime_revision'] for row in reopened):
            raise AcceptanceBlocked('A fresh controller process did not retain the verified installed/inference receipts.')
        report.update(status='passed', reference_unchanged=True, verified_after_fresh_controller=True,
                      reopened_models=reopened, physical_gpu_tested=device == 'cuda')
        if device == 'cpu':
            report['remaining_acceptance'].append('Physical GPU/owner RTX 5080 acceptance')
    except Exception as error:
        report.update(status='blocked' if isinstance(error, (AcceptanceBlocked, models.ComponentUnavailable)) else 'failed',
                      error=_safe_error(error, data_root, output))
    finally:
        unchanged = protected == _controller_identity()
        report['controller_environment_unchanged'] = unchanged
        if not unchanged:
            report.update(status='failed', error='The application controller environment changed during acceptance.')
        (output / REPORT_NAME).write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--device', choices=['cpu', 'cuda'], default='cpu')
    parser.add_argument('--approve-install', action='store_true')
    parser.add_argument('--require-native-windows', action='store_true')
    parser.add_argument('--require-clean-source', action='store_true')
    args = parser.parse_args()
    last = [0.0, '']
    def progress(number, message, **details):
        now = time.monotonic()
        if now - last[0] >= 10 or message != last[1]:
            print(json.dumps({'kind': 'progress', 'progress': round(number, 2), 'message': message,
                              'stage': details.get('stage', 'Model acceptance')}), flush=True)
            last[:] = [now, message]
    report = run_acceptance(args.data_root, args.output, device=args.device, approve_install=args.approve_install,
        require_native_windows=args.require_native_windows, require_clean_source=args.require_clean_source, progress=progress)
    print(json.dumps({key: report.get(key) for key in ('kind', 'status', 'source_commit', 'native_windows_execution',
        'physical_gpu_tested', 'controller_environment_unchanged', 'error')}, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
