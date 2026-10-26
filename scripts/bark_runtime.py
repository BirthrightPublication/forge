"""FORGE's offline Bark recognition subprocess; no remote code execution.

Compressed tensor decoding is adapted from Conway Research's Bark 3.0 loader
(CC BY 4.0), through the published VoiceFlow implementation. See docs/BARK-ASR.md
for attribution, upstream revisions and changes. No model loader is downloaded.
"""
from __future__ import annotations

import contextlib
import json
import lzma
import os
from pathlib import Path
import sys

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['DO_NOT_TRACK'] = '1'


def _decompress(raw, compressed):
    if not compressed:
        return raw
    decoder = lzma.LZMADecompressor(memlimit=256 * 1024**2)
    result = decoder.decompress(raw, max_length=256 * 1024**2 + 1)
    if not decoder.eof or decoder.unused_data or len(result) > 256 * 1024**2:
        raise ValueError('Invalid or oversized Bark compressed tensor.')
    return result


def decode_weights(path):
    """Decode verified publisher data. Only JSON + numeric tensors are accepted."""
    import numpy as np
    import constriction
    import torch
    size = path.stat().st_size
    if not 8 <= size <= 8 * 1024**3:
        raise ValueError('Bark model is empty or oversized.')
    with path.open('rb') as f:
        count = int.from_bytes(f.read(8), 'little')
        if not 0 < count <= min(size - 8, 8 * 1024**2):
            raise ValueError('Invalid Bark tensor header size.')
        head = json.loads(f.read(count))
        if head.get('format') not in ('bark-levels-v2', 'bark-levels-v3') or not isinstance(head.get('index'), list) or len(head['index']) > 5000:
            raise ValueError('Unsupported Bark compressed model format.')
        def read(n):
            if not isinstance(n, int) or not 0 <= n <= 256 * 1024**2:
                raise ValueError('Invalid Bark tensor data length.')
            raw = f.read(n)
            if len(raw) != n:
                raise ValueError('Truncated Bark tensor.')
            return raw
        state = {}
        total = 0
        for entry in head['index']:
            name, shape = entry['n'], entry['shape']
            if not isinstance(name, str) or len(name) > 500 or name in state or not isinstance(shape, list) or len(shape) > 5 or any(not isinstance(v, int) or v < 0 for v in shape):
                raise ValueError('Invalid Bark tensor identity/shape.')
            elements = 1
            for v in shape:
                elements *= v
            if elements > 64 * 1024**2:
                raise ValueError('Bark tensor exceeds the bounded decoder budget.')
            total += elements * 4
            if total > 8 * 1024**3:
                raise ValueError('Bark decoded weights exceed 8 GiB.')
            kind = entry['k']
            if kind == 'levels2':
                levels = entry['K']
                if not 1 <= levels <= 127 or not shape or not shape[0]:
                    raise ValueError('Invalid Bark quantization levels.')
                rows, cols = shape[0], elements // shape[0]
                lev = np.frombuffer(_decompress(read(entry['lev']), entry.get('zl')), np.float16).reshape(rows, levels).astype(np.float32)
                meta = read(entry['meta'])
                nibble_bytes = (cols * 4 + 7) // 8
                bits = np.unpackbits(np.frombuffer(meta[:nibble_bytes], np.uint8))[:cols*4].reshape(cols, 4)
                classes = np.packbits(np.concatenate([np.zeros((cols, 4), np.uint8), bits], 1), axis=1)[:, 0]
                counts = np.frombuffer(meta[nibble_bytes:], np.uint32).reshape(16, 2*levels+1).astype(np.float64)
                words = np.frombuffer(read(entry['b']), np.uint32).copy()
                decoder = constriction.stream.stack.AnsCoder(words)
                codes = np.empty((rows, cols), np.uint8)
                for c in range(16):
                    idx = np.nonzero(classes == c)[0]
                    if not len(idx):
                        continue
                    if not counts[c].sum():
                        raise ValueError('Bark quantization histogram is empty.')
                    distribution = constriction.stream.model.Categorical(counts[c] / counts[c].sum(), perfect=False)
                    codes[:, idx] = decoder.decode(distribution, rows*len(idx)).reshape(len(idx), rows).T
                magnitudes = np.where(codes == 0, 0, np.where(codes <= levels, codes, codes-levels)).astype(np.int64)
                values = np.take_along_axis(np.concatenate([np.zeros((rows, 1), np.float32), lev], 1), magnitudes, 1)
                value = np.where(codes > levels, -values, values).reshape(shape)
            elif isinstance(kind, str) and kind.startswith('ansint'):
                bits = int(kind[6:])
                if not 2 <= bits <= 8:
                    raise ValueError('Unsupported Bark quantized integer width.')
                scale = np.frombuffer(_decompress(read(entry['s']), entry.get('zs')), np.float16).astype(np.float32)
                counts = np.frombuffer(read(entry['meta']), np.uint32).astype(np.float64)
                words = np.frombuffer(read(entry['b']), np.uint32).copy()
                if not counts.sum():
                    raise ValueError('Bark quantization histogram is empty.')
                distribution = constriction.stream.model.Categorical(counts/counts.sum(), perfect=False)
                symbols = constriction.stream.stack.AnsCoder(words).decode(distribution, elements)
                # Cast before subtracting: unsigned symbol arrays must not wrap
                # negative quantized values into large positive integers.
                quantized = symbols.astype(np.float32) - (2**(bits-1)-1)
                value = (quantized.reshape(len(scale), -1) * scale[:, None]).reshape(shape)
            elif kind == 'raw':
                dtype = {'torch.int64': np.int64, 'torch.int32': np.int32, 'torch.float16': np.float16, 'torch.float32': np.float16}.get(entry['dtype'])
                if dtype is None:
                    raise ValueError('Unsupported Bark raw tensor type.')
                value = np.frombuffer(_decompress(read(entry['b']), entry.get('zr')), dtype).copy().reshape(shape)
            else:
                raise ValueError('Unsupported Bark tensor encoding.')
            state[name] = torch.from_numpy(value)
        if f.read(1):
            raise ValueError('Trailing Bark model data is not supported.')
    config = json.loads(head['config']) if isinstance(head['config'], str) else head['config']
    if not isinstance(config, dict):
        raise ValueError('Invalid Bark model configuration.')
    return state, config


def load_model(directory, device):
    import torch
    from transformers import AutoProcessor, GenerationConfig, ParakeetForTDT, ParakeetTDTConfig
    from transformers.initialization import no_init_weights
    processor_path = directory / 'processor'
    processor = AutoProcessor.from_pretrained(str(processor_path), local_files_only=True, trust_remote_code=False)
    if (directory / 'model.bark').is_file():
        state, cfg = decode_weights(directory / 'model.bark')
        with no_init_weights():
            model = ParakeetForTDT(ParakeetTDTConfig.from_dict(cfg))
        missing, unexpected = model.load_state_dict(state, strict=False, assign=True)
        if unexpected or any('num_batches_tracked' not in v for v in missing):
            raise ValueError('Bark model tensors do not match the supported Parakeet runtime.')
        del state
        model.generation_config = GenerationConfig.from_pretrained(str(processor_path), local_files_only=True)
    else:
        model = ParakeetForTDT.from_pretrained(str(directory), local_files_only=True, trust_remote_code=False, use_safetensors=True)
    return model.eval().to(device=device, dtype=torch.float32), processor


def peak_memory():
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in (
                    'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage',
                    'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
            counters = Counters(); counters.cb = ctypes.sizeof(counters)
            if ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return int(counters.PeakWorkingSetSize)
        else:
            import resource
            return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * (1 if sys.platform == 'darwin' else 1024)
    except (ImportError, OSError, AttributeError):
        pass
    return None


def main(request):
    import numpy as np
    import soundfile as sf
    import torch
    directory = Path(request['model_directory'])
    device = request.get('device', 'cpu')
    if device == 'auto':
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
    if device not in ('cpu', 'cuda') or device == 'cuda' and not torch.cuda.is_available():
        raise ValueError('The selected Bark CUDA runtime is unavailable; choose CPU.')
    torch.set_num_threads(max(1, min(8, os.cpu_count() or 1)))
    if device == 'cuda':
        torch.cuda.reset_peak_memory_stats()
    model, processor = load_model(directory, device)
    recognized = []
    for index, item in enumerate(request['chunks']):
        if item['index'] != index:
            raise ValueError('Bark audio chunks are out of sequence.')
        waveform, rate = sf.read(item['path'], dtype='float32', always_2d=True)
        if rate != 16000 or len(waveform) > 30 * rate or not np.isfinite(waveform).all():
            raise ValueError('Bark expects bounded, finite 16 kHz audio chunks.')
        wave = np.concatenate([np.zeros(4000, np.float32), waveform.mean(axis=1)])
        inputs = processor([wave], sampling_rate=16000, return_tensors='pt', padding=True)
        inputs = {k: v.to(device=device, dtype=torch.float32) if v.dtype.is_floating_point else v.to(device) for k, v in inputs.items()}
        with torch.inference_mode():
            output = model.generate(**inputs)
        text = processor.batch_decode(output.sequences, skip_special_tokens=True)[0].strip()
        recognized.append({'index': index, 'text': text, 'audio_window': item['audio_window']})
    result = {'chunks': recognized, 'device': device, 'peak_process_memory_bytes': peak_memory(),
              'peak_cuda_allocated_bytes': torch.cuda.max_memory_allocated() if device == 'cuda' else None}
    destination = Path(request['result'])
    with destination.open('x', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False)


if __name__ == '__main__':
    try:
        request_path = Path(sys.argv[1])
        if request_path.stat().st_size > 8 * 1024**2:
            raise ValueError('Bark runtime request is oversized.')
        with contextlib.redirect_stdout(sys.stderr):
            main(json.loads(request_path.read_text('utf-8')))
    except Exception as error:
        # Full local diagnostics stay in the worker's private stderr, never API state.
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        raise SystemExit(1)
