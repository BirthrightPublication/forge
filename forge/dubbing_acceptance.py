"""Owner-driven local model readiness over the existing isolated job queue.

This checklist never installs models, uploads media or declares a release ready.
Only source/model-bound completed worker receipts count as model evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time

from fastapi.responses import JSONResponse

from . import __version__
from .core import ROOT, json_write
from .queue import public_job

REPORT_NAME = 'dubbing-local-acceptance.json'
RUNTIME_NOTICES_NAME = 'DUBBING_RUNTIME_NOTICES.json'
NOTICE_LIMIT = 2 * 1024 * 1024
COMPONENTS = ('opus-en-ru', 'qwen3-tts-0.6b-base', 'whisper-tiny')
ACTIVE = {'queued', 'running', 'finalizing', 'cancelling'}
# Only these fixed synthetic harness inputs may be shown for translation review.
# Recognition text, private project inputs and arbitrary runner diagnostics are
# never copied into the owner checklist or its downloadable report.
TRANSLATION_INPUTS = (
    'Hello. This is a local translation test.',
    'FORGE keeps the original video. The project has 3 segments.',
    'The meeting starts on 2026-11-13 at 10:30.',
)


def _runner():
    from scripts import dubbing_cpu_acceptance
    return dubbing_cpu_acceptance


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _source():
    try:
        value = _runner().source_identity(additional_files=('forge/dubbing_acceptance.py', 'forge/worker.py'))
    except OSError:
        raise ValueError('The verified source files are unavailable. Use the compatible FORGE candidate through the normal updater.') from None
    if not re.fullmatch('[a-f0-9]{40}', str(value.get('source_commit', ''))):
        raise ValueError('This installation lacks an exact source identity. Use a source-verified FORGE candidate package.')
    if value.get('source_kind') == 'git' and value.get('source_checkout_clean') is not True:
        raise ValueError('Local source edits are not a verified candidate. Preserve the edits, then use the clean committed FORGE candidate or its verified Windows package before running acceptance.')
    return value


def _models(data, device):
    try:
        value = _runner().installed_identities(data, device)
    except (OSError, RuntimeError):
        raise ValueError('Install and verify OPUS, Qwen 0.6B and Whisper tiny for the selected CPU/CUDA profile through Set Up Dubbing Studio first.') from None
    if set(value) != set(COMPONENTS):
        raise ValueError('Install the three verified test components through Set Up Dubbing Studio first.')
    for component, row in value.items():
        required = 'cpu' if component == 'whisper-tiny' else device
        if row.get('device') != required or not re.fullmatch('[a-f0-9]{40}(?:[a-f0-9]{24})?', str(row.get('model_revision', ''))):
            raise ValueError('The installed model/device differs from this test. Review the correct CPU/CUDA install or Repair quote first.')
    return value


def _facts(receipt):
    """Require component-level evidence, not a summary pass/GPU flag alone."""
    if not isinstance(receipt, dict):
        return False, False
    report = receipt.get('report', {})
    source, identities = receipt.get('source', {}), receipt.get('models', {})
    if not all(isinstance(value, dict) for value in (report, source, identities)):
        return False, False
    if (report.get('status') != 'passed' or report.get('inference_substituted') is not False
            or report.get('private_inputs_used') is not False
            or report.get('source_commit') != source.get('source_commit')
            or report.get('source_manifest_sha256') != source.get('source_manifest_sha256')
            or report.get('source_files_sha256') != source.get('source_files_sha256')
            or report.get('installed_identities') != identities):
        return False, False
    rows = report.get('components', {})
    if not isinstance(rows, dict) or set(rows) != set(COMPONENTS):
        return False, False
    gpu = receipt.get('device') == 'cuda'
    for component in COMPONENTS:
        row = rows[component]
        if not isinstance(row, dict):
            return False, False
        verification, inference = row.get('verification', {}), row.get('inference', {})
        expected = identities.get(component, {})
        if not all(isinstance(value, dict) for value in (verification, inference, expected)):
            return False, False
        if (verification.get('status') != 'passed' or type(verification.get('files_verified')) is not int
                or verification['files_verified'] <= 0 or inference.get('inference_tested') is not True
                or any(inference.get(key) != expected.get(key) for key in ('model_revision', 'runtime_revision'))
                or inference.get('actual_device') != expected.get('device')):
            return False, False
        if component == 'whisper-tiny':
            if inference.get('physical_gpu_tested') is not False or inference.get('timings_verified') is not True:
                return False, False
        elif gpu:
            capability = inference.get('gpu_capability', [])
            if (inference.get('physical_gpu_tested') is not True or not isinstance(inference.get('gpu_name'), str)
                    or not inference['gpu_name'] or not isinstance(capability, list) or len(capability) != 2
                    or any(type(value) is not int or value < 0 for value in capability)
                    or type(inference.get('peak_vram_bytes')) is not int or inference['peak_vram_bytes'] <= 0):
                return False, False
        elif inference.get('physical_gpu_tested') is not False:
            return False, False
    return True, gpu and report.get('physical_gpu_tested') is True


def _translation_pairs(receipt):
    if not _facts(receipt)[0]:
        return []
    rows = receipt['report']['components']['opus-en-ru']['inference'].get('segments')
    if not isinstance(rows, list) or len(rows) != len(TRANSLATION_INPUTS):
        return []
    pairs = []
    for index, (row, expected) in enumerate(zip(rows, TRANSLATION_INPUTS), start=1):
        if (not isinstance(row, dict) or row.get('id') != 'owned-' + str(index)
                or row.get('source_text') != expected or not isinstance(row.get('text'), str)
                or not 0 < len(row['text']) <= 2000 or any(ord(char) < 32 and char not in '\n\t' for char in row['text'])):
            return []
        pairs.append({'english': expected, 'russian': row['text']})
    return pairs


def _runtime_notices(path, artifact, identities, data):
    """Validate a fixed owned sidecar, then copy only public notice fields."""
    invalid = 'The runtime notices are unavailable or differ from the verified local run. Run local acceptance again.'
    if (not isinstance(artifact, dict) or artifact.get('filename') != RUNTIME_NOTICES_NAME
            or not re.fullmatch('[a-f0-9]{64}', str(artifact.get('sha256', '')))
            or type(artifact.get('size')) is not int or not 0 < artifact['size'] <= NOTICE_LIMIT
            or artifact.get('legal_review_status') != 'pending'):
        raise ValueError(invalid)
    try:
        path, root = Path(path).absolute(), Path(data).resolve()
        if path.name != RUNTIME_NOTICES_NAME or not path.resolve().is_relative_to(root):
            raise ValueError(invalid)
        for item in (path, *path.parents):
            if item.is_symlink() or getattr(item.stat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError(invalid)
            if item == root:
                break
        if not path.is_file() or path.stat().st_size != artifact['size']:
            raise ValueError(invalid)
        raw = path.read_bytes()
        if len(raw) != artifact['size'] or hashlib.sha256(raw).hexdigest() != artifact['sha256']:
            raise ValueError(invalid)
        value = json.loads(raw)
    except (OSError, UnicodeError, TypeError, json.JSONDecodeError):
        raise ValueError(invalid) from None
    if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('kind') != 'installed_runtime_notices'
            or value.get('legal_review_status') != 'pending' or not isinstance(value.get('notice_texts'), list)
            or not isinstance(value.get('omitted_texts'), list) or len(value['notice_texts']) > 10000
            or len(value['omitted_texts']) > 64 or type(value.get('omitted_text_count')) is not int
            or not len(value['omitted_texts']) <= value['omitted_text_count'] <= 10000
            or type(artifact.get('included_texts')) is not int or type(artifact.get('omitted_texts')) is not int
            or artifact.get('included_texts') != len(value['notice_texts'])
            or artifact.get('omitted_texts') != value['omitted_text_count']):
        raise ValueError(invalid)
    runtimes = {row.get('runtime_revision') for row in identities.values()}

    def public_identity(row):
        if not isinstance(row, dict):
            raise ValueError(invalid)
        relative = row.get('path', '')
        parts = relative.split('/') if isinstance(relative, str) else []
        if (row.get('runtime_revision') not in runtimes
                or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', str(row.get('package', '')))
                or not parts or any(not part or part in {'.', '..'} or '\\' in part or ':' in part
                    or any(ord(char) < 32 for char in part) for part in parts)
                or not (re.sub(r'[^A-Z0-9]', '', parts[-1].upper()).startswith(
                    ('LICENSE', 'LICENCE', 'COPYING', 'NOTICE', 'COPYRIGHT', 'AUTHORS',
                     'THIRDPARTYNOTICE', 'THIRDPARTYLICENSE', 'THIRDPARTYLICENCE'))
                    or any(re.sub(r'[^a-z0-9]', '', part.lower()) in {'licenses', 'license', 'licences', 'licence',
                        'copying', 'notice', 'notices', 'thirdpartylicenses', 'thirdpartylicences', 'thirdpartynotices'}
                        for part in parts[:-1]))
                or not re.fullmatch('[a-f0-9]{64}', str(row.get('sha256', '')))
                or type(row.get('size')) is not int or not 0 <= row['size'] <= 4 * 1024 * 1024):
            raise ValueError(invalid)
        return {key: row[key] for key in ('runtime_revision', 'package', 'path', 'sha256', 'size')}

    texts, omissions = [], []
    for row in value['notice_texts']:
        public = public_identity(row)
        text = row.get('text')
        if (row.get('integrity') != 'record_verified' or row.get('scope') != 'installed_distribution'
                or not re.fullmatch(r'[A-Za-z0-9_.+!-]+', str(row.get('version', '')))
                or not isinstance(text, str) or len(text.encode('utf-8')) > 256 * 1024
                or any(ord(char) < 32 and char not in '\n\r\t' for char in text)
                or str(root).lower() in text.lower()
                or re.search(r'(?i)(?:[a-z]:[\\/]users[\\/]|/(?:home|Users)/)', text)):
            raise ValueError(invalid)
        encoded = text.encode('utf-8')
        if not any(len(candidate) == row['size'] and hashlib.sha256(candidate).hexdigest() == row['sha256']
                   for candidate in (encoded, b'\xef\xbb\xbf' + encoded)):
            raise ValueError(invalid)
        texts.append({**public, 'version': row['version'], 'scope': 'installed_distribution',
                      'integrity': 'record_verified', 'text': text})
    for row in value['omitted_texts']:
        public = public_identity(row)
        if row.get('text_omission') != 'sidecar_total_text_bound':
            raise ValueError(invalid)
        omissions.append({**public, 'text_omission': 'sidecar_total_text_bound'})
    return {'schema': 1, 'kind': 'installed_runtime_notices', 'legal_review_status': 'pending',
            'notice_texts': texts, 'omitted_texts': omissions, 'omitted_text_count': value['omitted_text_count']}


def run_worker(payload, stage_dir, data, progress=None):
    """Called only for a server-created existing model-test job."""
    request = payload.get('dubbing_acceptance_run')
    if (not isinstance(request, dict) or set(request) != {'schema', 'device', 'source', 'models', 'reference_consent'}
            or request.get('schema') != 1 or request.get('reference_consent') is not True
            or payload.get('options', {}).get('consent') is not True or request.get('device') not in {'cpu', 'cuda'}):
        raise ValueError('Approve a new local readiness test through FORGE first.')
    device = request['device']
    if request['source'] != _source() or request['models'] != _models(data, device):
        raise ValueError('Source or model settings changed after approval. Review and queue a new local readiness test.')
    stage_dir = Path(stage_dir)
    # Honor the application's configured media utilities without changing the
    # owner's PATH. This environment belongs only to the isolated job process.
    from .core import executable
    tools = [executable(name, payload.get('settings', {})) for name in ('ffmpeg', 'ffprobe')]
    if not all(tools):
        raise ValueError('Select FFmpeg and FFprobe in FORGE Settings before running local acceptance.')
    previous_path = os.environ.get('PATH', '')
    folders = list(dict.fromkeys(str(Path(path).resolve().parent) for path in tools))
    try:
        os.environ['PATH'] = os.pathsep.join([*folders, previous_path])
        result = _runner().run_acceptance(Path(data), stage_dir / 'acceptance', device=device, approve_install=False,
            expected_models=request['models'], expected_source=request['source'], progress=progress)
    finally:
        os.environ['PATH'] = previous_path
    if request['source'] != _source() or request['models'] != _models(data, device):
        raise ValueError('Source or model settings changed during verification; no passing receipt was published.')
    value = {'schema': 1, 'kind': 'local_dubbing_readiness', 'device': device,
             'source': request['source'], 'models': request['models'], 'observed_at': time.time(),
             'report': result, 'report_sha256': _digest(result), 'satisfies_release_acceptance': False}
    notice_path = stage_dir / 'acceptance' / RUNTIME_NOTICES_NAME
    notice_artifact = result.get('runtime_notice_artifact')
    if notice_artifact is not None and result.get('status') == 'passed':
        _runtime_notices(notice_path, notice_artifact, request['models'], data)
    report = stage_dir / REPORT_NAME
    json_write(report, value)
    files = [report]
    if notice_artifact is not None and result.get('status') == 'passed':
        import shutil
        target = stage_dir / RUNTIME_NOTICES_NAME
        shutil.copyfile(notice_path, target)
        files.append(target)
    sample = stage_dir / 'acceptance' / 'Russian-acceptance.wav'
    if sample.is_file() and not sample.is_symlink():
        # Generated synthetic-reference output only, retained locally for the
        # owner's listening review. JSON exports never contain its bytes/path.
        import shutil
        target = stage_dir / 'Russian-acceptance.wav'
        shutil.copyfile(sample, target)
        files.append(target)
    warnings = [] if result.get('status') == 'passed' else ['Local model acceptance did not pass. Open Verify V0.10 readiness for the remaining checks.']
    return files, warnings, {}


class LocalAcceptance:
    def __init__(self, app, store, manager):
        self.app, self.store, self.manager = app, store, manager

    def jobs(self):
        return [job for job in self.store.jobs(limit=200) if job['tool'] == 'dubbing_model_test'
                and isinstance(job.get('payload', {}).get('dubbing_acceptance_run'), dict)]

    def receipt(self, job):
        if job['status'] != 'completed':
            return None
        for item in job.get('outputs', []):
            path = Path(item.get('path', ''))
            if path.name != REPORT_NAME or path.is_symlink() or not path.resolve().is_relative_to(self.store.data.resolve()):
                continue
            if not path.is_file() or not 0 < path.stat().st_size <= 2_000_000:
                continue
            try:
                value = json.loads(path.read_text('utf-8'))
                expected = job['payload']['dubbing_acceptance_run']
                if (isinstance(value, dict) and isinstance(value.get('report'), dict)
                        and value.get('source') == expected['source'] and value.get('models') == expected['models']
                        and value.get('device') == expected['device'] and value.get('report_sha256') == _digest(value.get('report'))):
                    return value
            except (OSError, ValueError, TypeError):
                pass
        return None

    def current(self):
        try:
            source = _source()
        except (OSError, ValueError, RuntimeError):
            source = None
        jobs = self.jobs()
        latest = jobs[0] if jobs else None
        receipt = self.receipt(latest) if latest else None
        valid = False
        if receipt and source == receipt['source']:
            try:
                valid = receipt['models'] == _models(self.store.data, receipt['device'])
            except (OSError, ValueError, RuntimeError):
                pass
        return source, jobs, latest, receipt if valid else None

    def sample_index(self, job, receipt):
        if not job or not receipt:
            return None
        rows = receipt.get('report', {}).get('components', {})
        voice = rows.get('qwen3-tts-0.6b-base', {}) if isinstance(rows, dict) else {}
        inference = voice.get('inference', {}) if isinstance(voice, dict) else {}
        media = inference.get('media', {}) if isinstance(inference, dict) else {}
        expected = media.get('sha256') if isinstance(media, dict) else None
        if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
            return None
        for index, item in enumerate(job.get('outputs', [])):
            path = Path(item.get('path', ''))
            if (path.name == 'Russian-acceptance.wav' and path.is_file() and not path.is_symlink()
                    and path.resolve().is_relative_to(self.store.data.resolve())
                    and path.stat().st_size <= 32_000_000):
                with path.open('rb') as stream:
                    actual = hashlib.file_digest(stream, 'sha256').hexdigest()
                if actual == expected:
                    return index
        return None

    def notices(self, job, receipt):
        if not job or not _facts(receipt)[0]:
            return None
        parents = {Path(item.get('path', '')).parent for item in job.get('outputs', [])
                   if Path(item.get('path', '')).name == REPORT_NAME}
        for item in job.get('outputs', []):
            path = Path(item.get('path', ''))
            if path.name != RUNTIME_NOTICES_NAME or path.parent not in parents:
                continue
            try:
                return _runtime_notices(path, receipt['report'].get('runtime_notice_artifact'), receipt['models'], self.store.data)
            except (ValueError, TypeError):
                pass
        return None

    def state(self):
        from . import dubbing_models
        source, jobs, latest, receipt = self.current()
        models = [item for item in dubbing_models.catalog(self.store.data) if item['id'] in COMPONENTS]
        report = receipt.get('report', {}) if receipt else {}
        model_pass, gpu = _facts(receipt)
        native = model_pass and report.get('native_windows_execution') is True and os.name == 'nt'
        review = self.store.get('v10_dubbing_quality_review', {})
        listened = bool(model_pass and review.get('job_id') == latest['id'] and review.get('report_sha256') == receipt['report_sha256'])
        translation_pairs = _translation_pairs(receipt)
        translation_review = self.store.get('v10_dubbing_translation_review', {})
        translation_approved = bool(translation_pairs and translation_review.get('approved') is True
            and translation_review.get('job_id') == latest['id'] and translation_review.get('report_sha256') == receipt['report_sha256'])
        youtube = self.store.get('v10_youtube_private_acceptance', {})
        youtube_identity = youtube.get('source_identity', {})
        youtube_current = False
        if isinstance(youtube_identity, dict) and source:
            try:
                actual_youtube_source = _runner().source_identity(
                    additional_files=tuple(youtube_identity.get('source_files_sha256', {})))
                youtube_current = (youtube_identity.get('verified') is True
                    and all(youtube_identity.get(key) == value for key, value in actual_youtube_source.items())
                    and youtube_identity.get('installed_manifest_sha256') == actual_youtube_source.get(
                        'installed_manifest_sha256', actual_youtube_source.get('source_manifest_sha256'))
                    and youtube_identity.get('source_commit') == source.get('source_commit')
                    and youtube_identity.get('source_manifest_sha256') == source.get('source_manifest_sha256'))
            except (OSError, ValueError, RuntimeError, TypeError):
                pass
        channel_hash = youtube.get('approved_channel_sha256')
        live_youtube = bool(source and youtube.get('kind') == 'youtube_private_acceptance'
                            and youtube.get('version') == __version__ and youtube.get('status') == 'passed'
                            and youtube.get('live_google') is True and youtube.get('native_windows_execution') is True
                            and youtube_current and youtube.get('private_visibility_verified') is True
                            and youtube.get('channel_verified') is True and youtube.get('captions_verified') is True
                            and youtube.get('explicit_private_review') is True and isinstance(channel_hash, str)
                            and re.fullmatch('[a-f0-9]{64}', channel_hash)
                            and youtube.get('observed_channel_sha256') == channel_hash
                            and all(re.fullmatch('[a-f0-9]{64}', str(youtube.get(key, '')))
                                    for key in ('media_sha256', 'subtitle_sha256', 'video_id_sha256')))
        inventory_ready = False
        if source:
            try:
                from scripts.check_dubbing_release import static_inventory
                inventory_ready = static_inventory(ROOT, 'v' + __version__, source['source_commit']).get('status') == 'inventory_verified'
            except (OSError, ValueError, RuntimeError, ImportError):
                pass
        checks = [
            ('installed_models', 'Exact installed test models', all(item.get('ready') for item in models) and len(models) == 3,
             'Install/Repair OPUS, Qwen 0.6B and Whisper tiny through the existing reviewed setup plan.'),
            ('model_inventory', 'Reviewed model licenses and native dependency inventory', inventory_ready,
             'Use the candidate with the complete reviewed production model/license inventory; metadata discovery alone does not approve distribution.'),
            ('actual_inference', 'Actual translation, timed recognition and Russian synthesis', model_pass,
             'Run local acceptance with the installed CPU or CUDA packs.'),
            ('native_windows', 'Native Windows model execution', native,
             'Run this same verification on Windows; Linux and downloaded wheels do not prove native execution.'),
            ('physical_gpu', 'Physical GPU model execution', gpu,
             'On the GPU computer, approve the correct CUDA pack quote, then run local acceptance with NVIDIA CUDA.'),
            ('human_translation', 'Owner review of the generated Russian translations', translation_approved,
             'Compare all three fixed English inputs with their actual Russian drafts below. Confirm meaning, grammar, glossary, numbers and dates; generation alone does not approve translation quality.'),
            ('human_listening', 'Owner listening review of the generated sample', listened,
             'Play the generated Russian sample, then explicitly confirm its intelligibility.'),
            ('gui_installation', 'Native GUI installation/repair acceptance', False,
             'Retain separate native GUI install/repair evidence. This button tests existing packs and never initiates an installation.'),
            ('youtube_private', 'Live private YouTube acceptance', live_youtube,
             'Use Language Publishing acceptance to connect the intended channel and review its private test upload.'),
            ('native_upgrade', 'V0.9 updater, preservation and rollback acceptance', False,
             'Retain separate exact-source Windows updater/rollback evidence; this model test does not replace it.'),
            ('final_release', 'Final source, package and owner release approval', False,
             'Keep this candidate unmerged and unpublished until the complete release gate and owner approval pass.'),
        ]
        rows = [{'id': key, 'title': title, 'status': 'passed' if passed else 'pending', 'next_action': action}
                for key, title, passed, action in checks]
        pending = next((row for row in rows if row['status'] != 'passed'), None)
        sample_index = self.sample_index(latest, receipt)
        sample_url = f"/api/jobs/{latest['id']}/outputs/{sample_index}" if sample_index is not None else None
        notices = self.notices(latest, receipt)
        finished = next((job for job in jobs if job['status'] not in ACTIVE), None)
        return {'schema': 1, 'version': __version__, 'source': source, 'checks': rows,
                'current_blocker': pending['title'] if pending else 'Owner release approval',
                'last_completed_action': ('Local acceptance ' + finished['status']) if finished else 'No local acceptance check has finished',
                'next_action': pending['next_action'] if pending else 'Review final release evidence.',
                'models': [{key: item.get(key) for key in ('id', 'label', 'ready', 'device', 'model_revision', 'runtime_revision')} for item in models],
                'latest_job': {key: latest[key] for key in ('id', 'status', 'progress')} if latest else None,
                'receipt_current': receipt is not None, 'sample_url': sample_url,
                'listening_confirmation_available': bool(model_pass and sample_url),
                'translation_pairs': translation_pairs, 'translation_review_available': bool(translation_pairs),
                'runtime_notices_available': notices is not None,
                'satisfies_release_acceptance': False}

    def run(self, body):
        if set(body) != {'device', 'consent'} or body.get('consent') is not True or body.get('device') not in {'cpu', 'cuda'}:
            raise ValueError('Select CPU or NVIDIA CUDA and explicitly approve the synthetic local verification first.')
        source = _source(); models = _models(self.store.data, body['device'])
        request = {'schema': 1, 'device': body['device'], 'source': source, 'models': models, 'reference_consent': True}
        with self.manager.lock:
            active = next((job for job in self.jobs() if job['status'] in ACTIVE), None)
            if active:
                return {'job': public_job(active), 'jobs': [public_job(active)], 'already_queued': True}
            return self.app.state.dubbing.simple_job('dubbing_model_test',
                {'component': 'qwen3-tts-0.6b-base', 'action': 'test', 'device': body['device'], 'consent': True,
                 'name': 'V0.10 local ' + body['device'].upper() + ' acceptance'}, extra={'dubbing_acceptance_run': request})

    def listen(self, body):
        if set(body) != {'job_id', 'approve'} or body.get('approve') is not True:
            raise ValueError('Confirm listening to the actual generated sample explicitly.')
        _, _, latest, receipt = self.current()
        if not latest or latest['id'] != body.get('job_id') or not receipt or not _facts(receipt)[0]:
            raise ValueError('A current passing local model run is required before recording listening review.')
        if self.sample_index(latest, receipt) is None:
            raise ValueError('The generated sample is unavailable; run local acceptance again.')
        self.store.set('v10_dubbing_quality_review', {'job_id': latest['id'], 'report_sha256': receipt['report_sha256'],
                       'confirmed_at': time.time(), 'scope': 'Owner intelligibility review of generated synthetic-reference speech only.'})
        return self.state()

    def translation_review(self, body):
        if set(body) != {'job_id', 'approve'} or type(body.get('approve')) is not bool:
            raise ValueError('Explicitly approve or reject the displayed synthetic translation drafts.')
        _, _, latest, receipt = self.current()
        if not latest or latest['id'] != body.get('job_id') or not _translation_pairs(receipt):
            raise ValueError('Current verified synthetic translation pairs are required before recording quality review.')
        self.store.set('v10_dubbing_translation_review', {'job_id': latest['id'], 'report_sha256': receipt['report_sha256'],
            'approved': body['approve'], 'confirmed_at': time.time(),
            'scope': 'Owner meaning, grammar, glossary, number and date review of three generated synthetic translations only.'})
        return self.state()

    def export_notices(self):
        _, _, latest, receipt = self.current()
        notices = self.notices(latest, receipt)
        if notices is None:
            raise ValueError('A current passing local run with verified runtime notices is required. Run local acceptance again.')
        artifact = receipt['report']['runtime_notice_artifact']
        return {**notices, 'version': __version__, 'source': receipt['source'], 'models': receipt['models'],
                'device': receipt['device'], 'job_id': latest['id'], 'local_report_sha256': receipt['report_sha256'],
                'sidecar_sha256': artifact['sha256'], 'sidecar_size': artifact['size'],
                'satisfies_release_acceptance': False}

    def export(self):
        state = self.state()
        _, _, latest, receipt = self.current()
        public_receipt = None
        if receipt and _facts(receipt)[0]:
            report = receipt['report']
            components = {}
            for component in COMPONENTS:
                row = report.get('components', {}).get(component, {})
                inference = row.get('inference', {})
                facts = {key: inference[key] for key in ('model_revision', 'runtime_revision', 'elapsed_seconds',
                    'actual_device', 'physical_gpu_tested', 'inference_tested', 'gpu_name', 'gpu_capability',
                    'peak_vram_bytes', 'peak_reserved_vram_bytes', 'generation_calls', 'timings_verified',
                    'owned_fixture_word_coverage', 'reference_sha256') if key in inference}
                if isinstance(inference.get('media'), dict):
                    facts['media'] = {key: inference['media'][key] for key in ('sha256', 'duration', 'sample_rate',
                                     'channels', 'codec', 'bytes', 'decoded') if key in inference['media']}
                if isinstance(inference.get('segments'), list):
                    facts['segment_count'] = len(inference['segments'])
                components[component] = facts
            # Positive field lists prevent future runner diagnostics, filenames,
            # recognized text, tokens or private recording fields leaking into
            # a report the owner may share. Audio remains a local job output.
            public_receipt = {'kind': 'sanitized_local_model_receipt', 'source': receipt['source'],
                'models': receipt['models'], 'device': receipt['device'], 'observed_at': receipt['observed_at'],
                'local_report_sha256': receipt['report_sha256'], 'status': report.get('status'),
                'native_windows_execution': report.get('native_windows_execution') is True,
                'physical_gpu_tested': _facts(receipt)[1], 'components': components}
            if self.notices(latest, receipt) is not None:
                artifact = report['runtime_notice_artifact']
                public_receipt['runtime_notice_artifact'] = {key: artifact[key] for key in
                    ('sha256', 'size', 'included_texts', 'omitted_texts', 'legal_review_status')}
        return {**{key: state[key] for key in ('schema', 'version', 'source', 'checks', 'current_blocker',
                    'last_completed_action', 'next_action', 'satisfies_release_acceptance')},
                'kind': 'local_readiness_checklist', 'model_evidence': public_receipt,
                'human_review': {'confirmed': any(row['id'] == 'human_listening' and row['status'] == 'passed'
                    for row in state['checks']), 'scope': 'Owner intelligibility review of generated synthetic-reference speech only.'},
                'translation_review': {'confirmed': any(row['id'] == 'human_translation' and row['status'] == 'passed'
                    for row in state['checks']), 'scope': 'Owner meaning, grammar, glossary, number and date review of three generated synthetic translations only.'}}


def mount(app, store, manager):
    service = LocalAcceptance(app, store, manager)
    app.state.dubbing_acceptance = service

    @app.get('/api/dubbing/acceptance/state')
    def state(): return service.state()

    @app.post('/api/dubbing/acceptance/run')
    def run(body: dict): return service.run(body)

    @app.post('/api/dubbing/acceptance/listening')
    def listening(body: dict): return service.listen(body)

    @app.post('/api/dubbing/acceptance/translation-review')
    def translation_review(body: dict): return service.translation_review(body)

    @app.get('/api/dubbing/acceptance/export')
    def export():
        return JSONResponse(service.export(), headers={'Content-Disposition': 'attachment; filename="FORGE-v0.10-readiness.json"'})

    @app.get('/api/dubbing/acceptance/runtime-notices')
    def export_notices():
        return JSONResponse(service.export_notices(), headers={'Content-Disposition': 'attachment; filename="FORGE-v0.10-runtime-notices.json"'})
