"""Optional isolated CPU speaker-label adapter. No model or authentication token downloads."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import wave
from pathlib import Path
from .core import ROOT,no_window


def validate(options):
    model=Path(options['diarization_model']).expanduser()
    if not model.is_absolute() or not model.is_dir() or not (model/'config.yaml').is_file():
        raise RuntimeError('Select the complete local Community-1 model folder containing config.yaml. Obtain the gated model through its publisher first; no account token is stored in FORGE.')
    python=ROOT/'.diarization-venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not python.is_file():
        raise RuntimeError('Run Install Speaker Labels.bat first. Speaker labeling uses a separate optional environment.')
    return model.resolve(),python


def diarize(source,stage,options):
    model,python=validate(options)
    from faster_whisper.audio import decode_audio
    import numpy as np
    samples=decode_audio(str(source),sampling_rate=16000)
    path=stage/'_private_speaker_audio.wav'
    output=stage/'_private_speaker_turns.json'
    with wave.open(str(path),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000)
        w.writeframes((np.clip(samples,-1,1)*32767).astype('<i2').tobytes())
    request={'model':str(model),'audio':str(path),'output':str(output),'min_speakers':options['min_speakers'],'max_speakers':options['max_speakers']}
    env={**os.environ,'PYTHONUTF8':'1','HF_HUB_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1','PYANNOTE_METRICS_ENABLED':'0','DO_NOT_TRACK':'1'}
    result=subprocess.run([str(python),str(ROOT/'scripts'/'diarize.py')],input=json.dumps(request),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        encoding='utf-8',errors='replace',env=env,timeout=43200,**no_window())
    if result.returncode or not output.is_file():
        raise RuntimeError('Local speaker labeling failed: '+result.stderr[-1800:])
    turns=json.loads(output.read_text(encoding='utf-8'))
    if not isinstance(turns,list):raise RuntimeError('Invalid speaker-label output.')
    return turns
