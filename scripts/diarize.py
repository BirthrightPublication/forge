"""Run only in the dedicated optional environment, with a complete trusted local model."""
import json
import os
import sys
import wave
from pathlib import Path
os.environ.update(HF_HUB_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',PYANNOTE_METRICS_ENABLED='0',DO_NOT_TRACK='1')

def main():
    request=json.load(sys.stdin)
    import numpy as np
    import torch
    from pyannote.audio import Pipeline
    # Windows file decoding uses the already-decoded mono PCM, not a TorchCodec DLL path.
    with wave.open(request['audio'],'rb') as w:
        samples=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype('float32')/32768
        rate=w.getframerate()
    pipeline=Pipeline.from_pretrained(request['model'])
    output=pipeline({'waveform':torch.from_numpy(samples).unsqueeze(0),'sample_rate':rate},
                    min_speakers=request['min_speakers'],max_speakers=request['max_speakers'])
    annotation=getattr(output,'exclusive_speaker_diarization',None)
    if annotation is None:annotation=output.speaker_diarization
    names={};turns=[]
    if hasattr(annotation,'itertracks'):
        source=((turn,label) for turn,_,label in annotation.itertracks(yield_label=True))
    else:source=iter(annotation)
    for turn,label in source:
        names.setdefault(label,'Speaker '+str(len(names)+1))
        turns.append({'start':float(turn.start),'end':float(turn.end),'speaker':names[label]})
    Path(request['output']).write_text(json.dumps(turns),encoding='utf-8')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(str(exc),file=sys.stderr);sys.exit(1)
