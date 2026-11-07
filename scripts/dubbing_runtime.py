"""Private, offline JSON-file protocol for isolated Dubbing Studio models.

No HTTP server is started. Only approved local snapshots are loaded; model-host
Python, arbitrary voice packs and remote audio URLs are never accepted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import sys
import time
import uuid


def progress(value, message, stage='Local model inference', measured=False, **metrics):
    print(json.dumps({'kind': 'progress', 'progress': max(0, min(99, value)), 'message': message,
                      'stage': stage, 'progress_measured': measured, **metrics}), flush=True)


def file_hash(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _local_file(value):
    if not isinstance(value, str) or '\x00' in value or re.match(r'^[A-Za-z]+://', value):
        raise ValueError('Choose a permitted local media file, not a URL or voice pack.')
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError('The selected local media file is missing or linked outside its approved location.')
    return path.resolve()


def _output(value):
    if not isinstance(value, str) or '\x00' in value:
        raise ValueError('Choose a complete new local output path.')
    path = Path(value).expanduser()
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValueError('A new output path is required; original and existing files are never overwritten.')
    if path.suffix.lower() not in {'.wav', '.flac'}:
        raise ValueError('The isolated voice adapter writes WAV or FLAC audio.')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _part(request, name):
    path = Path(request.get('model_paths', {}).get(name, ''))
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError('The installed model part is missing. Use Verify or Repair in Dubbing Studio.')
    return path


def _device(request):
    device = request.get('device', 'cpu')
    if device not in {'cpu', 'cuda'}:
        raise ValueError('Choose the installed CPU or CUDA runtime.')
    if device == 'cuda':
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA is unavailable in this isolated runtime. Use compatibility diagnostics or select CPU.')
        capability = torch.cuda.get_device_capability()
        if capability[0] >= 12 and 'sm_120' not in torch.cuda.get_arch_list():
            raise RuntimeError('This CUDA wheel does not include the GPU architecture. Repair with the approved CUDA 12.8 runtime.')
        torch.cuda.reset_peak_memory_stats()
    return device


def _metrics(device):
    result = {'actual_device': device, 'physical_gpu_tested': device == 'cuda'}
    if device == 'cuda':
        import torch
        torch.cuda.synchronize()
        result.update(gpu_name=torch.cuda.get_device_name(), gpu_capability=list(torch.cuda.get_device_capability()),
                      peak_vram_bytes=torch.cuda.max_memory_allocated(), peak_reserved_vram_bytes=torch.cuda.max_memory_reserved())
    return result


def _glossary(value):
    if isinstance(value, dict):
        value = value.get('entries', value)
    if isinstance(value, dict):
        value = [{'source': key, 'target': target} for key, target in value.items()]
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError('The translation glossary must contain at most 1,000 source/target entries.')
    result = {}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('Invalid glossary entry.')
        source, target = item.get('source', ''), item.get('target', '')
        if not isinstance(source, str) or not isinstance(target, str) or not 0 < len(source) <= 200 or not 0 < len(target) <= 400:
            raise ValueError('Glossary terms must contain a source and Russian target.')
        result[source] = target
    return result


def protected_pieces(text, glossary):
    """Separate exact glossary terms, URLs, citations and numeric/date tokens.

    Their content never enters a generation model, so placeholder hallucination
    cannot erase or alter an explicitly protected name or number.
    """
    terms = sorted(glossary, key=lambda item: (-len(item), item))
    patterns = [r'(?<!\w)' + re.escape(term) + r'(?!\w)' for term in terms]
    patterns += [r'https?://[^\s<>]+', r'\[[0-9]+(?:[,; -][0-9]+)*\]',
                 r'(?<!\w)[+-]?\d+(?:[.,:/-]\d+)*(?:%|\b)']
    expression = re.compile('|'.join('(?:' + pattern + ')' for pattern in patterns), re.IGNORECASE)
    lower = {key.casefold(): value for key, value in glossary.items()}
    position = 0
    for match in expression.finditer(text):
        if match.start() > position:
            yield False, text[position:match.start()]
        value = match.group()
        yield True, lower.get(value.casefold(), value)
        position = match.end()
    if position < len(text):
        yield False, text[position:]


def translate(request):
    import torch
    from transformers import MarianMTModel, MarianTokenizer
    model_dir = _part(request, 'translation'); device = _device(request)
    progress(0, 'Loading the installed English–Russian translation model', 'Translation')
    tokenizer = MarianTokenizer.from_pretrained(str(model_dir), local_files_only=True, trust_remote_code=False)
    model = MarianMTModel.from_pretrained(str(model_dir), local_files_only=True, trust_remote_code=False,
                                         use_safetensors=(model_dir / 'model.safetensors').is_file(),
                                         weights_only=True, dtype=torch.float32).to(device).eval()
    segments = request.get('segments', [])
    if not isinstance(segments, list) or not 0 < len(segments) <= 10000:
        raise ValueError('Select a bounded list of transcript segments to translate.')
    glossary = _glossary(request.get('glossary') or [])
    mode = request.get('mode', 'localized')
    if mode not in {'faithful', 'localized', 'alternative'}:
        raise ValueError('Choose a supported editable dubbing mode.')
    warnings = []
    if mode != 'faithful':
        warnings.append('This compact engine creates a translation draft. Idiomatic or alternative commentary requires manual review; it does not invent scene commentary.')
    result = []; generations = 0

    def unit(value):
        nonlocal generations
        if not value.strip():
            return value
        leading = value[:len(value) - len(value.lstrip())]
        trailing = value[len(value.rstrip()):]
        stripped = value.strip()
        inputs = tokenizer(stripped, return_tensors='pt', truncation=False)
        token_count = int(inputs['input_ids'].shape[-1])
        maximum = min(512, int(getattr(model.config, 'max_position_embeddings', 512)))
        if token_count > maximum:
            # Recurse on real text boundaries rather than silently truncating.
            words = re.findall(r'\S+\s*', stripped)
            if len(words) < 2:
                raise ValueError('A source token exceeds the translation model context. Split that segment before translating.')
            midpoint = len(words) // 2
            return leading + unit(''.join(words[:midpoint])).rstrip() + ' ' + unit(''.join(words[midpoint:])).lstrip() + trailing
        inputs = {key: value.to(device) for key, value in inputs.items()}
        with torch.inference_mode():
            generated = model.generate(**inputs, do_sample=False, num_beams=4, max_new_tokens=512)
        generations += 1
        decoded = tokenizer.batch_decode(generated, skip_special_tokens=True)[0].strip()
        if not decoded:
            raise RuntimeError('The local translation model returned no text. The source segment was preserved.')
        return leading + decoded + trailing

    for index, segment in enumerate(segments):
        if not isinstance(segment, dict) or not isinstance(segment.get('id'), str):
            raise ValueError('Each translation segment requires its stable source identity.')
        source = segment.get('source_text', '')
        if not isinstance(source, str) or len(source) > 50000:
            raise ValueError('A source transcript segment is invalid or too large.')
        if (segment.get('manual_edit') or segment.get('reviewed')) and not request.get('overwrite_edited', False):
            result.append({**segment, 'translation_skipped': True})
            warnings.append('Reviewed or manually edited segments were retained; explicit selected regeneration is required to replace them.')
            continue
        paragraphs = re.split(r'(\r?\n)', source)
        translated = []
        for paragraph in paragraphs:
            if paragraph in {'\n', '\r\n'}:
                translated.append(paragraph); continue
            translated.append(''.join(value if protected else unit(value) for protected, value in protected_pieces(paragraph, glossary)))
        result.append({**segment, 'source_text': source, 'text': ''.join(translated),
                       'reviewed': False, 'manual_edit': False, 'translation_skipped': False})
        progress(100 * (index + 1) / len(segments), f'Translated segment {index + 1}/{len(segments)}', 'Translation', True)
    return {'success': True, 'segments': result, 'source_language': 'en', 'target_language': 'ru',
            'model_revision': request['model_revision'], 'warnings': list(dict.fromkeys(warnings)),
            'inference_tested': generations > 0, 'generation_calls': generations, **_metrics(device)}


def _reference(request):
    if request.get('consent') is not True:
        raise ValueError('Approve use of this identifiable voice reference before synthesis.')
    path = _local_file(request.get('ref_audio', ''))
    if path.suffix.lower() not in {'.wav', '.flac', '.mp3', '.m4a', '.ogg', '.opus', '.aac'}:
        raise ValueError('Select a permitted recording, not a Python/pickle voice pack.')
    before = path.stat(); digest = file_hash(path)
    expected = request.get('ref_audio_sha256') or request.get('reference_sha256')
    if expected and expected != digest:
        raise ValueError('The permitted reference changed after approval. Review the recording again.')
    import numpy as np
    import soundfile as sf
    try:
        samples, rate = sf.read(str(path), dtype='float32', always_2d=True)
    except Exception:
        import librosa
        samples, rate = librosa.load(str(path), sr=None, mono=False)
        samples = np.asarray(samples).reshape(1, -1).T if samples.ndim == 1 else samples.T
    if not isinstance(rate, int) or not 8000 <= rate <= 192000 or not samples.size or not np.isfinite(samples).all():
        raise ValueError('The reference recording is empty, nonfinite or has an unsupported sample rate.')
    duration = len(samples) / rate
    if not 1 <= duration <= 30:
        raise ValueError('Prepare a clear 1–30 second reference clip in Use My Voice before synthesis.')
    if float(np.max(np.abs(samples))) < .001:
        raise ValueError('The reference contains no measurable speech-level audio. Prepare another clip.')
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('The reference recording changed during inspection.')
    return path, np.mean(samples, axis=1), rate, digest


def _audio_output(wave, rate, value):
    import numpy as np
    import soundfile as sf
    path = _output(value); wave = np.asarray(wave, dtype=np.float32).squeeze()
    if wave.ndim != 1 or not wave.size or not np.isfinite(wave).all() or not 8000 <= int(rate) <= 192000:
        raise RuntimeError('The speech model produced no valid finite mono audio.')
    duration = len(wave) / int(rate)
    if not .05 <= duration <= 7200:
        raise RuntimeError('The generated speech duration is outside the supported production range.')
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.partial' + path.suffix)
    try:
        sf.write(str(temporary), wave, int(rate), format='FLAC' if path.suffix.lower() == '.flac' else 'WAV',
                 subtype='PCM_24' if path.suffix.lower() == '.flac' else 'FLOAT')
        info = sf.info(str(temporary))
        if info.frames != len(wave) or info.samplerate != int(rate):
            raise RuntimeError('Generated audio failed its saved-file validation.')
        with temporary.open('r+b') as durable:
            durable.flush(); os.fsync(durable.fileno())
        if os.name == 'nt':
            os.rename(temporary, path)  # Windows rename is atomic and refuses an existing destination.
        else:
            os.link(temporary, path)  # Atomic no-replace publication on the same filesystem.
    finally:
        temporary.unlink(missing_ok=True)
    return {'path': str(path), 'sample_rate': int(rate), 'duration': duration,
            'size': path.stat().st_size, 'sha256': file_hash(path), 'peak': float(np.max(np.abs(wave)))}


def synthesize(request):
    import torch
    device = _device(request); component = request['component']; model_dir = _part(request, 'voice')
    segments = request.get('segments')
    batch = segments is not None
    if not batch:
        segments = [{'id': request.get('id', 'segment'), **request}]
    if not isinstance(segments, list) or not 0 < len(segments) <= 1000:
        raise ValueError('Select at most 1,000 voice segments per model batch.')
    progress(0, 'Loading the installed Russian voice model', 'Russian voice generation')
    qwen = component.startswith('qwen3-tts-')
    if qwen:
        from qwen_tts import Qwen3TTSModel
        model = Qwen3TTSModel.from_pretrained(str(model_dir), device_map=device,
            dtype=torch.bfloat16 if device == 'cuda' else torch.float32,
            attn_implementation='sdpa', local_files_only=True, trust_remote_code=False,
            use_safetensors=True, weights_only=True)
        languages = {str(value).lower() for value in model.get_supported_languages()}
        if 'russian' not in languages:
            raise RuntimeError('This installed model variant does not report Russian support. Repair the registered Base model.')
    elif component == 'chatterbox-multilingual-v3':
        if device != 'cpu':
            raise ValueError('This optional archived runtime is registered for CPU only.')
        # No permissive pickle loads may enter this isolated process.
        original_load = torch.load
        def safe_load(*args, **kwargs):
            if kwargs.get('weights_only') is False:
                raise ValueError('Unsafe pickle loading is disabled for this component.')
            kwargs['weights_only'] = True
            return original_load(*args, **kwargs)
        torch.load = safe_load
        import perth
        if not callable(getattr(perth, 'PerthImplicitWatermarker', None)):
            raise RuntimeError('The archived model watermark dependency is not usable. Repair the isolated runtime; no dummy watermarker is substituted.')
        import chatterbox.models.tokenizers.tokenizer as text_tokenizer
        class RussianOnlyConverter:
            def __init__(self, model_dir=None):
                pass
            def __call__(self, text):
                raise ValueError('Chinese tokenization is outside this reviewed Russian-only profile.')
        text_tokenizer.ChineseCangjieConverter = RussianOnlyConverter
        if hasattr(text_tokenizer, 'add_russian_stress'):
            text_tokenizer.add_russian_stress = lambda text: text
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        model = ChatterboxMultilingualTTS.from_local(model_dir, device, t3_model='v3')
    else:
        raise ValueError('Choose a registered Russian voice model.')
    prompts = {}; results = []
    for index, segment in enumerate(segments):
        value = {**request, **segment}; value.pop('segments', None)
        text = value.get('text', '')
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise ValueError('Enter nonempty Russian text within one production segment.')
        if value.get('instructions') or value.get('instruct') or value.get('native_emotion'):
            raise ValueError('The installed Base model does not support instruction or native-emotion tasks; use the separate Audio Effects controls.')
        path, samples, rate, reference_hash = _reference(value)
        reference_text = value.get('ref_text', '')
        if not isinstance(reference_text, str) or len(reference_text) > 5000:
            raise ValueError('The permitted reference transcript is invalid.')
        with torch.inference_mode():
            if qwen:
                x_vector_only = bool(value.get('x_vector_only_mode', not bool(reference_text.strip())))
                key = (reference_hash, reference_text, x_vector_only)
                if key not in prompts:
                    prompts[key] = model.create_voice_clone_prompt(ref_audio=(samples, rate), ref_text=reference_text or None,
                                                                   x_vector_only_mode=x_vector_only)
                waves, output_rate = model.generate_voice_clone(text=text, language='Russian', voice_clone_prompt=prompts[key],
                    max_new_tokens=max(256, min(4096, int(value.get('max_new_tokens', 2048)))), do_sample=False)
                if len(waves) != 1:
                    raise RuntimeError('The voice model returned an unexpected number of audio segments.')
                wave = waves[0]
            else:
                wave = model.generate(text, language_id='ru', audio_prompt_path=str(path),
                    exaggeration=max(0, min(2, float(value.get('exaggeration', .5)))),
                    cfg_weight=max(0, min(1, float(value.get('cfg_weight', .5)))))
                wave = wave.squeeze().detach().cpu().numpy(); output_rate = model.sr
        if file_hash(path) != reference_hash:
            raise ValueError('The permitted reference changed during inference. Previously completed segments remain available.')
        result = _audio_output(wave, output_rate, value.get('output'))
        result.update(id=str(segment.get('id', index)), model_revision=request['model_revision'],
                      reference_sha256=reference_hash, text_sha256=hashlib.sha256(text.encode()).hexdigest(), language='ru')
        results.append(result)
        progress(100 * (index + 1) / len(segments), f'Generated voice segment {index + 1}/{len(segments)}',
                 'Russian voice generation', True, segment_complete=True,
                 segment_id=str(segment.get('id', index))[:100], completed_segments=index + 1)
    result = {'success': True, 'model_revision': request['model_revision'], 'segments': results,
              'inference_tested': True, **_metrics(device)}
    if not batch:
        result.update(results[0])
    return result


def transcribe(request):
    from faster_whisper import WhisperModel
    source = _local_file(request.get('source') or request.get('path', ''))
    if request.get('diarization'):
        raise ValueError('This isolated Whisper adapter does not invent speaker labels. Assign speakers in review or use an installed diarization engine.')
    before = source.stat(); device = _device(request)
    if device != 'cpu':
        raise ValueError('The isolated Whisper fallback is currently registered for CPU inference.')
    model_dir = _part(request, 'asr')
    progress(0, 'Loading the installed speech-recognition model', 'English transcription')
    model = WhisperModel(str(model_dir), device='cpu', compute_type='int8', local_files_only=True,
                         cpu_threads=max(1, min(8, int(request.get('cpu_threads', 4)))))
    segments, info = model.transcribe(str(source), language=request.get('language', 'en'), task='transcribe',
        beam_size=max(1, min(10, int(request.get('beam_size', 5)))), vad_filter=bool(request.get('vad', True)),
        word_timestamps=request.get('timestamps', 'word') in {'word', True})
    duration = float(getattr(info, 'duration', 0) or 0); result = []
    for index, segment in enumerate(segments):
        text = str(segment.text).strip()
        if not text:
            continue
        start, end = float(segment.start), float(segment.end)
        if not math.isfinite(start + end) or start < 0 or end <= start:
            raise RuntimeError('The recognizer returned invalid speech timing.')
        words = [{'start': float(word.start), 'end': float(word.end), 'word': str(word.word),
                  'probability': float(word.probability)} for word in segment.words or []]
        result.append({'id': 'asr-' + str(index + 1).zfill(6), 'start': start, 'end': end, 'speaker': 'speaker1',
                       'source_text': text, 'text': '', 'words': words, 'reviewed': False, 'manual_edit': False})
        progress(100 * min(end, duration) / duration if duration > 0 else 0,
                 f'Transcribed segment {len(result)}', 'English transcription', duration > 0)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('The original source changed during transcription.')
    return {'success': True, 'segments': result, 'duration': duration, 'language': str(info.language),
            'language_probability': float(info.language_probability), 'model_revision': request['model_revision'],
            'speaker_labels': 'single-speaker placeholder; review assignments', 'inference_tested': bool(result), **_metrics(device)}


def _offline_network(*args, **kwargs):
    raise OSError('Network access is disabled during local model inference. Repair missing installed artifacts through Dubbing Studio.')


def run(request):
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['DO_NOT_TRACK'] = '1'
    # Libraries outside Hugging Face must obey the same offline boundary.
    socket.socket.connect = _offline_network
    socket.socket.connect_ex = _offline_network
    socket.create_connection = _offline_network
    task = request.get('task')
    if task == 'translate' and request.get('component') == 'opus-en-ru':
        return translate(request)
    if task == 'synthesize':
        return synthesize(request)
    if task == 'transcribe' and request.get('component', '').startswith('whisper-'):
        return transcribe(request)
    raise ValueError('Choose a supported installed translation, voice or recognition task.')


def _private_error(error, request):
    detail = str(error)
    values = [request.get('data'), request.get('source'), request.get('path'), request.get('text'), request.get('ref_audio'), request.get('ref_text')]
    for segment in request.get('segments', []):
        if isinstance(segment, dict):
            values.extend(segment.get(key) for key in ('text', 'source_text', 'ref_audio', 'ref_text'))
    for value in sorted((value for value in values if isinstance(value, str) and value), key=len, reverse=True):
        detail = detail.replace(value, '<private>')
    detail = re.sub(r'https?://\S+', '<publisher URL>', detail)
    return type(error).__name__ + ': ' + detail[:700]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True, type=Path)
    parser.add_argument('--response', type=Path)
    args = parser.parse_args()
    if args.request.stat().st_size > 32_000_000:
        raise ValueError('The private model request exceeds its safety bound.')
    request = json.loads(args.request.read_text('utf-8'))
    response = args.response or Path(request.get('response_path') or request['response'])
    started = time.monotonic()
    try:
        result = run(request)
        result['elapsed_seconds'] = time.monotonic() - started
        response.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return 0
    except Exception as error:
        response.write_text(json.dumps({'success': False, 'status': 'failed', 'error': _private_error(error, request)}, ensure_ascii=False), encoding='utf-8')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
