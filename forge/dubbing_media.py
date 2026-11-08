"""Bounded local waveform measurement for the persistent dubbing workspace."""
from __future__ import annotations

import math
import subprocess

from .audio_effects import _probe
from .core import executable, no_window


def waveform(source, settings, max_points=800):
    if type(max_points) is not int or not 16<=max_points<=1600:
        raise ValueError('Choose between 16 and 1,600 waveform points.')
    info=_probe(source,settings)
    binary=executable('ffmpeg',settings)
    if not binary: raise ValueError('Install the compatible media utilities before measuring a waveform.')
    rate=8000
    width=max(1,math.ceil(info['duration']*rate/max_points))
    # FFmpeg aggregates each bounded bin in C. Only peak metadata reaches Python;
    # no decoded full-length audio is buffered or saved. A wall-clock deadline
    # also covers reading a malformed or stalled local input.
    filters=(f'aresample={rate},asetnsamples=n={width}:p=1,'
             'astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.Peak_level:file=-')
    try:
        result=subprocess.run([binary,'-nostdin','-v','error','-protocol_whitelist','file,pipe',
            '-i',str(source),'-t',str(info['duration']),'-map','0:a:0','-ac','1','-af',filters,
            '-f','null','-'],capture_output=True,text=True,encoding='utf-8',errors='replace',
            timeout=45,**no_window())
    except subprocess.TimeoutExpired as error:
        raise ValueError('Waveform analysis reached its time limit. The video and saved work are unchanged.') from error
    if result.returncode: raise ValueError('The selected audio could not be measured.')
    peaks=[]
    for line in result.stdout.splitlines():
        if line.startswith('lavfi.astats.Overall.Peak_level='):
            try: value=float(line.split('=',1)[1])
            except ValueError: raise ValueError('The waveform decoder returned an invalid peak.') from None
            peaks.append(round(10**(value/20),6) if math.isfinite(value) else 0.0)
    if not peaks or len(peaks)>max_points+1:
        raise ValueError('The waveform decoder returned an inconsistent duration.')
    if len(peaks)>max_points: peaks[-2]=max(peaks[-2],peaks.pop())
    peaks.extend([0.0]*(max_points-len(peaks)))
    return {**info,'peaks':peaks,'points':max_points,'sample_rate_preview':rate,
            'method':'Measured peak bins of downsampled mono playback audio, not true-peak metering'}
