"""Opt-in real local TTS acceptance; run in a disposable Windows CI installation.

This is deliberately not part of pytest. It downloads actual approved models and
isolated dependencies, synthesizes original test prose, and fails on installation
or inference errors. Model weights/environments must never be uploaded as evidence.
"""
from __future__ import annotations

import argparse
from array import array
from contextlib import contextmanager
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
import traceback
import urllib.request
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forge import __version__
from forge import speech_engines as engines
from forge import speech_runtime as runtime

REFERENCE_TEXT = (
    'The quiet library opens at nine each morning. A reader places three blue books '
    'beside the window and writes a short note. Outside, the rain stops, the clouds '
    'move away, and a small bird begins to sing.'
)
SHORT_TEXT = 'The little lantern glows beside the quiet window.'


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def redact(value, paths=()):
    text = str(value)
    for path in sorted((str(p) for p in paths if p), key=len, reverse=True):
        text = text.replace(path, '<acceptance-path>')
    text = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1<redacted>@', text)
    text = re.sub(r'(https?://[^\s?]+)\?[^\s]+', r'\1?<redacted>', text)
    text = re.sub(r'(?i)([?&](?:token|key|signature|sig|credential)=)[^\s&]+', r'\1<redacted>', text)
    text = re.sub(r'(?i)\b(?:hf_[A-Za-z0-9]{15,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b', '<redacted>', text)
    return text


def audio_metrics(path):
    """Inspect actual uncompressed signed-16 output without a new audio dependency."""
    with wave.open(str(path), 'rb') as audio:
        rate, channels, width = audio.getframerate(), audio.getnchannels(), audio.getsampwidth()
        frames = audio.getnframes()
        if audio.getcomptype() != 'NONE' or width != 2 or not 8000 <= rate <= 96000 or channels not in (1, 2):
            raise AssertionError('Generated speech is not a supported PCM16 WAV.')
        if not rate * .25 <= frames <= rate * 90:
            raise AssertionError('Generated speech duration is empty or outside the bounded test window.')
        count, total, peak, clipped = 0, 0.0, 0, 0
        while block := audio.readframes(rate):
            values = array('h')
            values.frombytes(block)
            if sys.byteorder != 'little':
                values.byteswap()
            for sample in values:
                peak = max(peak, abs(sample))
                total += sample * sample
                count += 1
                clipped += abs(sample) >= 32760
    rms = math.sqrt(total / max(count, 1)) / 32768
    if peak < 100 or rms < .0003:
        raise AssertionError('Generated speech is silent or too close to zero to establish audio output.')
    if clipped / count > .1:
        raise AssertionError('More than ten percent of generated samples are clipped.')
    return {'sha256': digest(path), 'sample_rate': rate, 'channels': channels,
            'duration_seconds': round(frames / rate, 4), 'peak': round(peak / 32768, 6),
            'rms_dbfs': round(20 * math.log10(rms), 3), 'clipped_fraction': round(clipped / count, 6)}


@contextmanager
def offline_environment():
    """Use the adapter's offline paths while making ordinary HTTP proxies unreachable."""
    changes = {'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_DATASETS_OFFLINE': '1',
               'HTTP_PROXY': 'http://127.0.0.1:9', 'HTTPS_PROXY': 'http://127.0.0.1:9',
               'ALL_PROXY': 'http://127.0.0.1:9', 'NO_PROXY': '',
               'http_proxy': 'http://127.0.0.1:9', 'https_proxy': 'http://127.0.0.1:9',
               'all_proxy': 'http://127.0.0.1:9', 'no_proxy': ''}
    old = {key: os.environ.get(key) for key in changes}
    os.environ.update(changes)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Acceptance:
    def __init__(self, engine, data, output, scope):
        self.engine, self.data, self.output, self.scope = engine, data, output, scope
        self.private_paths = (ROOT, data, output, Path.home(), os.getenv('RUNNER_TEMP', ''))
        self.receipt = {'schema': 1, 'application': 'FORGE', 'version': __version__,
                        'engine': engine, 'scope': scope, 'status': 'running',
                        'native_windows_execution': os.name == 'nt',
                        'physical_gpu_tested': False, 'device': 'cpu',
                        'python': platform.python_version(), 'platform': platform.system(),
                        'tests': [], 'phases': [], 'pending': [],
                        'prose_origin': 'Original synthetic test prose; no private or publisher sample audio.',
                        'scope_note': 'Actual model and mode smoke acceptance. Audio metrics establish non-silent output, not pronunciation, emotion quality or speaker identity.'}
        try:
            self.receipt['source_commit'] = subprocess.check_output(
                ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, timeout=10).strip()
        except (OSError, subprocess.SubprocessError):
            self.receipt['source_commit'] = 'unavailable'
        self.current_phase = 'initialization'

    def save(self):
        path = self.output / ('tts-' + self.engine + '-acceptance.json')
        path.write_text(json.dumps(self.receipt, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    def log(self, value):
        message = redact(value, self.private_paths)
        print(message, flush=True)
        with (self.output / 'acceptance.log').open('a', encoding='utf-8') as stream:
            stream.write(message[:32000] + '\n')

    def annotation(self, title, value, *, error=False):
        text = redact(value, self.private_paths).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print('::' + ('error' if error else 'notice') + ' title=' + title + '::' + text[:12000], flush=True)

    def publisher(self):
        repo = engines.SPECS[self.engine]['repository']
        approved=engines.SPECS[self.engine]['approved_revision']
        url = 'https://huggingface.co/api/models/' + repo + '/revision/' + approved + '?blobs=true'
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'FORGE-model-acceptance'}), timeout=30) as response:
            data = response.read(4_000_001)
        if len(data) > 4_000_000:
            raise ValueError('Publisher metadata exceeded its bounded size.')
        metadata = json.loads(data)
        revision = metadata.get('sha', '')
        if not isinstance(revision, str) or not engines.REVISION.fullmatch(revision):
            raise ValueError('Publisher did not return an immutable model revision.')
        if revision!=approved:
            raise ValueError('Publisher returned a revision different from the approved acceptance target.')
        card = metadata.get('cardData') or {}
        result = {'repository': repo, 'revision': revision, 'license': card.get('license'),
                  'license_name': card.get('license_name'), 'license_link': card.get('license_link'),
                  'artifact_count': len(metadata.get('siblings', []))}
        self.receipt['publisher'] = result
        self.annotation(self.engine + ' pinned publisher metadata', json.dumps(result, ensure_ascii=False))
        self.save()
        return revision

    @contextmanager
    def phase(self, name, seconds):
        """Sample process-family memory and kill only owned children on a deadline."""
        import psutil
        self.current_phase = name
        started = time.monotonic()
        stopped = threading.Event()
        state = {'peak': 0, 'timed_out': False}

        def monitor():
            parent = psutil.Process()
            while not stopped.wait(.2):
                try:
                    children = parent.children(recursive=True)
                    rss = parent.memory_info().rss
                    for child in children:
                        try:
                            rss += child.memory_info().rss
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                    state['peak'] = max(state['peak'], rss)
                    if time.monotonic() - started > seconds:
                        state['timed_out'] = True
                        for child in reversed(children):
                            try:
                                child.kill()
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                        return
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return

        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        self.log('START ' + name)
        record = {'name': name, 'status': 'failed'}
        try:
            yield
            if state['timed_out']:
                raise TimeoutError('The real model operation exceeded its bounded acceptance deadline.')
            record['status'] = 'passed'
        finally:
            stopped.set()
            thread.join(timeout=2)
            record.update(duration_seconds=round(time.monotonic() - started, 3),
                          peak_process_family_rss_mb=round(state['peak'] / 1048576, 2),
                          timed_out=state['timed_out'])
            self.receipt['phases'].append(record)
            self.save()

    def check(self, name, condition, **evidence):
        if not condition:
            raise AssertionError(name)
        self.receipt['tests'].append({'name': name, 'status': 'passed', **evidence})
        self.log('PASS ' + name)
        self.annotation(self.engine + ' real model acceptance', name + ': ' + json.dumps(evidence, ensure_ascii=False))
        self.save()

    def install(self):
        # Preserve sanitized installer stderr, which the product intentionally
        # hides from its browser. This is a disposable CI process only.
        original = runtime._command

        def diagnostic_command(args, progress=None, timeout=7200):
            result = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=min(timeout, 1800),
                                    **runtime.no_window())
            if result.returncode:
                tail = redact((result.stdout + '\n' + result.stderr)[-24000:], self.private_paths)
                self.receipt['command_failure_tail'] = tail
                self.log('Installer subprocess failed:\n' + tail)
                # Check annotations are bounded by GitHub; keep the terminating
                # pip error visible instead of filling the limit with progress.
                self.annotation(self.engine + ' isolated installation diagnostics', tail[-3500:], error=True)
                raise RuntimeError('Isolated runtime command failed; sanitized diagnostics are in acceptance.log.')
            return result.stdout

        runtime._command = diagnostic_command
        try:
            with self.phase('publisher-metadata-and-pin', 60):
                revision = self.publisher()
            with self.phase('approved-isolated-installation', 2400):
                installed = engines.install(self.engine, self.data, consent=True, device='cpu', revision=revision,
                    progress=lambda value, message, **kw: self.log(str(kw.get('stage', 'Install')) + ': ' + message))
        finally:
            runtime._command = original
        resolved = engines.resolve_options({'engine': self.engine, 'voice': 'am_onyx' if self.engine == 'kokoro' else 'US_James',
                                           'task': 'TTS' if self.engine == 'kokoro' else 'IP', 'device': 'cpu'}, self.data)
        self.receipt.update(model_revision=resolved['model_revision'], runtime_revision=resolved['runtime_revision'],
                            installed=installed)
        self.check('Installation activates a verified immutable model and isolated runtime',
                   bool(engines.REVISION.fullmatch(resolved['model_revision'])) and
                   bool(engines.environment_python(self.data, self.engine).is_file()))
        path = engines.model_path(self.data, self.engine)
        verified = runtime.verify_model(path)
        self.receipt['model_receipt'] = {'repository': verified['repository'], 'revision': verified['revision'],
                                          'artifact_count': len(verified['files']), 'bytes': verified['bytes'],
                                          'publisher_blob_verified': verified.get('publisher_blob_verified', False)}
        notices = []
        notice_dir = self.output / 'publisher-notices'
        for root, prefix in [(path, 'checkpoint'),
                             (engines.environment_path(self.data, self.engine) / 'upstream', 'runtime')]:
            if not root.is_dir():
                continue
            for file in root.iterdir():
                if file.is_file() and re.fullmatch(r'(?:LICENSE|COPYING|NOTICE)(?:\.[A-Za-z0-9_-]+)?', file.name, re.I):
                    if file.stat().st_size > 2_000_000:
                        raise AssertionError('Publisher notice exceeded its bounded evidence size.')
                    notice_dir.mkdir(exist_ok=True)
                    target = notice_dir / (prefix + '-' + file.name)
                    shutil.copyfile(file, target)
                    notices.append({'name': target.name, 'sha256': digest(target), 'bytes': target.stat().st_size})
        self.receipt['publisher_notices'] = notices
        self.annotation(self.engine + ' obtained publisher notices', json.dumps(notices))
        self.save()
        return path

    def synthesize(self, name, text, options, *, offline=False):
        target = self.output / (name + '.wav')
        with self.phase(name, 900):
            if offline:
                with offline_environment():
                    metadata = engines.synthesize(text, {'engine': self.engine, 'device': 'cpu', **options},
                                                  self.data, target)
            else:
                metadata = engines.synthesize(text, {'engine': self.engine, 'device': 'cpu', **options},
                                              self.data, target)
            metrics = audio_metrics(target)
        self.check(name + ' produced non-silent verified PCM speech', metadata.get('success') is True,
                   audio=target.name, metrics=metrics, model_revision=metadata['model_revision'],
                   runtime_revision=metadata['runtime_revision'], task=metadata['task'])
        return target, metadata, metrics

    def kokoro(self):
        model = self.install()
        inventory = engines.voices(self.data, 'kokoro')
        ids = {voice['id'] for voice in inventory}
        self.check('Original Onyx is present as am_onyx', 'am_onyx' in ids)
        source = model / 'voices' / 'am_onyx.pt'
        before = digest(source)
        target, metadata, first = self.synthesize('acceptance-reference', REFERENCE_TEXT,
                                                {'voice': 'am_onyx', 'task': 'TTS', 'language': 'en', 'seed': 1234})
        (self.output / 'acceptance-reference.txt').write_text(REFERENCE_TEXT + '\n', encoding='utf-8')
        reference = {'audio_sha256': first['sha256'], 'reference_sha256': digest(self.output / 'acceptance-reference.txt'),
                     'engine': 'kokoro', 'voice': 'am_onyx', 'model_revision': metadata['model_revision'],
                     'runtime_revision': metadata['runtime_revision'], 'synthetic_test_owned': True,
                     'text_origin': 'Original test prose', 'audio': target.name}
        (self.output / 'acceptance-reference.json').write_text(json.dumps(reference, indent=2) + '\n', encoding='utf-8')
        second_voice = next((v for v in ['am_fenrir', 'am_adam', 'am_michael'] if v in ids), None)
        if not second_voice:
            second_voice = next((v for v in sorted(ids) if v.startswith(('am_', 'af_', 'bm_', 'bf_')) and v != 'am_onyx'), None)
        self.check('A second compatible official English voice is available for an actual tensor blend', bool(second_voice))
        recipe = {'voice': 'am_onyx', 'task': 'TTS', 'language': 'en', 'seed': 1234,
                  'blend': [{'voice': 'am_onyx', 'weight': 70}, {'voice': second_voice, 'weight': 30}]}
        normalized = engines.normalize_blend(recipe['blend'])
        (self.output / 'blend-recipe.json').write_text(json.dumps({**recipe, 'normalized_weights': normalized,
                'model_revision': metadata['model_revision']}, indent=2) + '\n', encoding='utf-8')
        _, mixed_metadata, mixed = self.synthesize('onyx-compatible-blend', REFERENCE_TEXT, recipe)
        self.check('Weighted blending changes actual generated audio', mixed['sha256'] != first['sha256'],
                   weights=normalized, baseline_sha256=first['sha256'], mixed_sha256=mixed['sha256'])
        _, repeat_metadata, repeated = self.synthesize('onyx-offline-repeat', REFERENCE_TEXT,
            {'voice': 'am_onyx', 'task': 'TTS', 'language': 'en', 'seed': 1234}, offline=True)
        self.check('Original Onyx synthesis works with offline flags and unreachable HTTP proxies',
                   repeat_metadata['model_revision'] == metadata['model_revision'],
                   same_pcm_sha256=repeated['sha256'] == first['sha256'])
        self.check('Original voice embedding remains unchanged after synthesis and blending', digest(source) == before,
                   source_voice_sha256=before)
        if self.scope=='full':
            self.check('Original British benchmark voice is present', 'bm_george' in ids)
            british_audio,british_metadata,british=self.synthesize('acceptance-reference-british',REFERENCE_TEXT,
                {'voice':'bm_george','task':'TTS','language':'en','seed':1234},offline=True)
            (self.output/'acceptance-reference-british.txt').write_text(REFERENCE_TEXT+'\n',encoding='utf-8')
            british_reference={**reference,'audio_sha256':british['sha256'],'voice':'bm_george',
                               'provider_accent':'British','audio':british_audio.name}
            (self.output/'acceptance-reference-british.json').write_text(json.dumps(british_reference,indent=2)+'\n')
            samples={'a':('en','The window is open.'),'b':('en','The window is open.'),
                     'e':('es','La ventana está abierta.'),'f':('fr','La fenêtre est ouverte.'),
                     'h':('hi','खिड़की खुली है।'),'i':('it','La finestra è aperta.'),
                     'p':('pt','A janela está aberta.'),'j':('ja','窓が開いています。'),'z':('zh','窗户开着。')}
            for code,(language,text) in samples.items():
                voice=next((item['id'] for item in inventory if item['id'].startswith(code) and item.get('synthesis_ready')),None)
                self.check('An installed ready official voice exists for Kokoro language code '+code,bool(voice))
                self.synthesize('kokoro-language-'+code,text,{'voice':voice,'task':'TTS','language':language,'seed':1234},offline=True)
        else:
            self.receipt['pending'].append('All nine official Kokoro language-code synthesis smoke checks')
        self.receipt['verified_voice_inventory_count'] = len(ids)
        self.receipt['pending'].extend(['Pronunciation/listening quality review',
                                       'Physical CUDA inference and memory acceptance'])

    def ming(self):
        self.install()
        ids = {voice['id'] for voice in engines.voices(self.data, 'ming')}
        voice = 'US_James' if 'US_James' in ids else next((v for v in sorted(ids) if v.startswith('US_')), '')
        self.check('The selected built-in voice is present in the pinned official catalog', bool(voice), voice=voice)
        reference, first_metadata, first = self.synthesize('ming-builtin', SHORT_TEXT,
                                                          {'voice': voice, 'task': 'IP', 'seed': 1234})
        common = {'reference_audio': str(reference), 'consent': True, 'seed': 1234}
        self.synthesize('ming-reference', SHORT_TEXT,
                        {**common, 'task': 'TTS', 'reference_text': SHORT_TEXT, 'language': 'en'})
        self.synthesize('ming-design', SHORT_TEXT,
                        {'task': 'VOICE_DESIGN', 'instructions': 'A calm warm English narrator with a clear steady delivery.', 'seed': 1234})
        self.synthesize('ming-emotion', SHORT_TEXT, {**common, 'task': 'EMOTION', 'emotion': 'happy'})
        if self.scope == 'full':
            self.synthesize('ming-freeform', SHORT_TEXT,
                            {'task': 'FREEFORM', 'instructions': 'Speak clearly and calmly.', 'seed': 1234})
            self.synthesize('ming-basic', SHORT_TEXT,
                            {**common, 'task': 'BASIC', 'attribute_speed': 'slow', 'attribute_pitch': 'low', 'attribute_volume': 'medium'})
            self.synthesize('ming-multilingual', 'Bonjour, la petite lampe éclaire la fenêtre tranquille.',
                            {**common, 'task': 'MULTILINGUAL', 'reference_text': SHORT_TEXT, 'language': 'fr'})
            self.synthesize('ming-dialect', '小灯照亮了安静的窗户。', {**common, 'task': 'DIALECT', 'dialect': 'cantonese'})
        else:
            self.receipt['pending'].append('Full FREEFORM/BASIC/MULTILINGUAL/DIALECT mode acceptance')
        _, repeated_metadata, repeated = self.synthesize('ming-offline-repeat', SHORT_TEXT,
                                                         {'voice': voice, 'task': 'IP', 'seed': 1234}, offline=True)
        self.check('Ming built-in synthesis works offline after installation',
                   repeated_metadata['model_revision'] == first_metadata['model_revision'],
                   same_pcm_sha256=repeated['sha256'] == first['sha256'])
        self.receipt['pending'].extend(['Listening/emotion/pronunciation and reference voice quality review',
                                       'Physical CUDA inference and memory acceptance'])

    def run(self):
        os.environ.update({'PYTHONUTF8': '1', 'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1',
                           'HF_HUB_OFFLINE': '0', 'TRANSFORMERS_OFFLINE': '0',
                           'HF_HUB_DISABLE_IMPLICIT_TOKEN': '1',
                           'HF_HOME': str(self.data / 'publisher-cache'), 'PIP_NO_CACHE_DIR': '1',
                           'OMP_NUM_THREADS': '2', 'MKL_NUM_THREADS': '2', 'TOKENIZERS_PARALLELISM': 'false'})
        try:
            self.kokoro() if self.engine == 'kokoro' else self.ming()
            self.receipt['status'] = 'passed'
            self.receipt['acceptance_complete'] = False
            self.receipt['note'] = 'Real CPU installation and named smoke checks passed; pending checks are not waived.'
            self.annotation(self.engine + ' CPU smoke result', json.dumps({
                'status': 'passed', 'scope': self.scope, 'source_commit': self.receipt['source_commit'],
                'model_revision': self.receipt['model_revision'], 'runtime_revision': self.receipt['runtime_revision'],
                'checks': len(self.receipt['tests']), 'native_windows_execution': os.name == 'nt',
                'pending': self.receipt['pending']}, ensure_ascii=False))
            return 0
        except Exception as error:
            self.receipt.update(status='failed', acceptance_complete=False, failed_phase=self.current_phase,
                                error=redact(error, self.private_paths)[:2000])
            self.log(traceback.format_exc())
            message = redact(error, self.private_paths).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
            print('::error title=Real ' + self.engine + ' acceptance failed::' + message[:3000], flush=True)
            return 1
        finally:
            self.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', required=True, choices=['kokoro', 'ming'])
    parser.add_argument('--data', required=True, type=Path, help='New disposable model/environment folder; never an owner installation.')
    parser.add_argument('--output', required=True, type=Path, help='New evidence folder; contains synthetic audio, not weights.')
    parser.add_argument('--scope', choices=['basic', 'full'], default='full')
    parser.add_argument('--allow-model-downloads', action='store_true')
    args = parser.parse_args(argv)
    if not args.allow_model_downloads:
        parser.error('Explicit --allow-model-downloads approval is required for real model/environment downloads.')
    data, output = args.data.resolve(), args.output.resolve()
    if data == ROOT or data == data.parent or output == output.parent or data == output or data.is_relative_to(output) or output.is_relative_to(data):
        parser.error('Model data and evidence must be separate disposable folders outside the source tree.')
    if data.is_relative_to(ROOT) or output.is_relative_to(ROOT):
        parser.error('Keep model data and acceptance outputs outside the source tree.')
    if data.exists() or output.exists() and any(output.iterdir()):
        parser.error('Use new disposable data/evidence folders; existing installations are never reused or overwritten.')
    data.mkdir(parents=True)
    output.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(data).free < 12 * 1024**3:
        parser.error('At least 12 GiB of free space is required for isolated speech acceptance.')
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True,timeout=10).strip()
    (data/'.forge-acceptance-owned.json').write_text(json.dumps({'purpose':'FORGE disposable speech acceptance',
        'engine':args.engine,'source_commit':commit})+'\n')
    return Acceptance(args.engine, data, output, args.scope).run()


if __name__ == '__main__':
    raise SystemExit(main())
