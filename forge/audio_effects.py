"""Allowlisted, ordered, non-destructive audio DSP using the local FFmpeg build.

Preview and export share this renderer. No user supplied filter expressions or
commands are accepted. Rubber Band, when installed in FFmpeg, handles independent
pitch/tempo; the renderer never substitutes sample-rate changes for that feature.
"""
from __future__ import annotations

from array import array
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from .core import executable, no_window, safe_name


def number(default, minimum, maximum):
    return {'type': 'number', 'default': default, 'min': minimum, 'max': maximum}


def choice(default, values):
    return {'type': 'select', 'default': default, 'options': values}


def effect(name, parameters, method='FFmpeg audio filter'):
    return {'label': name, 'parameters': parameters, 'method': method}


EFFECTS = {
    'pitch': effect('Pitch / tempo', {
        'semitones': number(0,-24,24), 'cents': number(0,-100,100),
        'tempo': number(1,.25,4), 'formant': choice('preserved',['preserved','shifted']),
        'automation': {'type':'points','default':[],'max_points':32},
    }, 'Rubber Band independent pitch and tempo; stepped pitch automation'),
    'gain': effect('Gain', {'db':number(0,-60,24)}),
    'highpass': effect('High-pass', {'frequency':number(80,10,20000),'poles':choice(2,[1,2])}),
    'lowpass': effect('Low-pass', {'frequency':number(14000,10,20000),'poles':choice(2,[1,2])}),
    'bandpass': effect('Band-pass', {'frequency':number(1000,20,20000),'q':number(1,.1,20)}),
    'notch': effect('Notch / hum reduction', {'frequency':number(60,10,20000),'q':number(5,.1,50)}),
    'eq': effect('Parametric / graphic EQ', {'bands':{'type':'bands','default':[{'frequency':1000,'gain':0,'q':1,'enabled':True}],'max_bands':16}}),
    'lowshelf': effect('Low shelf', {'frequency':number(200,10,20000),'gain':number(0,-24,24),'q':number(.707,.1,10)}),
    'highshelf': effect('High shelf', {'frequency':number(5000,10,20000),'gain':number(0,-24,24),'q':number(.707,.1,10)}),
    'peak_normalize': effect('Peak normalize', {'target_db':number(-1,-30,0)}),
    'loudness': effect('Loudness normalize', {'lufs':number(-16,-36,-5),'true_peak':number(-1,-9,0),'range':number(11,1,20)}, 'FFmpeg EBU R128 single-pass loudnorm; final target is measured, not guaranteed'),
    'fade': effect('Fade', {'in_seconds':number(.05,0,60),'out_seconds':number(.1,0,60)}),
    'envelope': effect('Volume envelope', {'points':{'type':'points','default':[[0,0]],'max_points':32}}, 'Piecewise linear gain automation in dB'),
    'pan': effect('Stereo balance', {'balance':number(0,-1,1)}),
    'width': effect('Stereo width', {'width':number(1,0,2)}),
    'compressor': effect('Compressor', {'threshold_db':number(-18,-60,0),'ratio':number(4,1,20),'attack':number(10,.01,2000),'release':number(150,.01,8000),'knee':number(2,1,8),'makeup_db':number(0,0,24)}),
    'limiter': effect('Limiter', {'ceiling_db':number(-1,-24,0),'attack':number(5,.1,80),'release':number(50,1,8000)}),
    'gate': effect('Noise gate', {'threshold_db':number(-40,-90,0),'ratio':number(8,1,100),'attack':number(10,.01,2000),'release':number(150,.01,8000)}),
    'expander': effect('Downward expander', {'threshold_db':number(-30,-90,0),'ratio':number(2,1,20),'attack':number(10,.01,2000),'release':number(150,.01,8000)}),
    'deesser': effect('De-esser', {'intensity':number(.5,0,1),'frequency':number(.5,0,1)}),
    'reverb': effect('Echo-room reverb', {'room_size':number(.5,0,1),'decay':number(.5,.05,.95),'predelay_ms':number(20,1,200),'damping':number(.4,0,1),'mix':number(.2,0,1)}, 'Synthetic multi-tap room response, not a measured convolution impulse response'),
    'echo': effect('Echo / delay', {'delay_ms':number(180,1,2000),'feedback':number(.35,0,.9),'mix':number(.25,0,1)}),
    'chorus': effect('Chorus', {'rate':number(.4,.1,5),'depth':number(2,.1,10),'mix':number(.3,0,1)}),
    'flanger': effect('Flanger', {'delay':number(0,0,30),'depth':number(2,0,10),'feedback':number(0,-95,95),'mix':number(.5,0,1),'rate':number(.5,.1,10),'phase':number(25,0,100)}),
    'phaser': effect('Phaser', {'rate':number(.5,.1,2),'decay':number(.4,0,.99),'delay':number(3,.1,5)}),
    'tremolo': effect('Tremolo', {'rate':number(5,.1,20),'depth':number(.5,0,1)}),
    'vibrato': effect('Vibrato', {'rate':number(5,.1,20),'depth':number(.5,0,1)}),
    'ring': effect('Ring modulation', {'frequency':number(30,.1,2000),'mix':number(.5,0,1)}),
    'saturation': effect('Saturation / soft clip', {'drive':number(2,1,10)}, 'Soft saturation using tanh with gain compensation'),
    'distortion': effect('Hard clipping', {'drive':number(2,1,10),'ceiling':number(.8,.1,1)}),
    'trim': effect('Trim edge silence', {'threshold_db':number(-45,-90,-5),'minimum':number(.1,.01,5)}),
}


def schema():
    return [dict(id=name,**deepcopy(value)) for name,value in EFFECTS.items()]


def _numeric(value, minimum, maximum):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'Effect parameter must be a finite number between {minimum} and {maximum}.')
    return float(value)


def _points(value, low, high):
    if not isinstance(value,list) or len(value)>32:
        raise ValueError('Automation accepts at most 32 time/value pairs.')
    last=-1
    result=[]
    for item in value:
        if not isinstance(item,(list,tuple)) or len(item)!=2:
            raise ValueError('Automation requires [seconds,value] pairs.')
        at=_numeric(item[0],0,86400); val=_numeric(item[1],low,high)
        if at<=last:raise ValueError('Automation times must increase strictly.')
        result.append([at,val]);last=at
    return result


def validate_chain(chain):
    if not isinstance(chain,list) or len(chain)>32:
        raise ValueError('Use at most 32 ordered effects.')
    result=[]
    for item in chain:
        if not isinstance(item,dict) or set(item)-{'type','enabled','params','id','collapsed'}:
            raise ValueError('Invalid effect definition.')
        kind=item.get('type')
        if kind not in EFFECTS:raise ValueError('Unsupported audio effect: '+str(kind))
        enabled=item.get('enabled',True)
        if not isinstance(enabled,bool):raise ValueError('Effect enabled must be boolean.')
        params=item.get('params',{})
        fields=EFFECTS[kind]['parameters']
        if not isinstance(params,dict) or set(params)-set(fields):raise ValueError('Unsupported parameters for '+kind+'.')
        parsed={}
        for name,spec in fields.items():
            value=params.get(name,deepcopy(spec['default']))
            if spec['type']=='number':value=_numeric(value,spec['min'],spec['max'])
            elif spec['type']=='select':
                if value not in spec['options']:raise ValueError('Unsupported '+name+'.')
            elif spec['type']=='points':value=_points(value,-24 if name=='automation' else -60,24)
            elif spec['type']=='bands':
                if not isinstance(value,list) or len(value)>16:raise ValueError('EQ supports at most 16 bands.')
                bands=[]
                for band in value:
                    if not isinstance(band,dict) or set(band)-{'frequency','gain','q','enabled'}:raise ValueError('Invalid EQ band.')
                    if not isinstance(band.get('enabled',True),bool):raise ValueError('Band enabled must be boolean.')
                    bands.append({'frequency':_numeric(band.get('frequency',1000),10,20000),'gain':_numeric(band.get('gain',0),-24,24),'q':_numeric(band.get('q',1),.1,20),'enabled':band.get('enabled',True)})
                value=bands
            parsed[name]=value
        result.append({'type':kind,'enabled':enabled,'params':parsed})
    return result


def _run(args,settings,*,timeout=7200):
    binary=executable('ffmpeg',settings)
    if not binary:raise RuntimeError('FFmpeg is required for Audio Effects. Configure its local path.')
    p=subprocess.run([binary,'-nostdin','-hide_banner','-nostats',*args],capture_output=True,timeout=timeout,**no_window())
    if p.returncode:raise RuntimeError('Audio Effects failed: '+p.stderr.decode('utf-8','replace')[-1800:])
    return p


def _probe(source,settings):
    source=Path(source).resolve()
    if not source.is_file():raise ValueError('Audio input was not found.')
    binary=executable('ffprobe',settings)
    if not binary:raise RuntimeError('FFprobe is required for Audio Effects.')
    p=subprocess.run([binary,'-v','error','-protocol_whitelist','file,pipe','-show_format','-show_streams','-of','json',str(source)],capture_output=True,timeout=60,**no_window())
    if p.returncode:raise ValueError('The source cannot be inspected as audio.')
    data=json.loads(p.stdout)
    audio=[s for s in data.get('streams',[]) if s.get('codec_type')=='audio']
    if not audio:raise ValueError('The source contains no audio stream.')
    duration=float(data.get('format',{}).get('duration') or audio[0].get('duration') or 0)
    if not math.isfinite(duration) or duration<=0 or duration>86400:raise ValueError('Audio duration must be known and no longer than 24 hours.')
    return {'duration':duration,'sample_rate':int(audio[0].get('sample_rate',48000)),'channels':int(audio[0].get('channels',1))}


def levels(source,settings):
    p=_run(['-loglevel','info','-protocol_whitelist','file,pipe','-i',str(source),'-map','0:a:0','-af','astats=metadata=0:reset=0','-f','null','-'],settings)
    text=p.stderr.decode('utf-8','replace')
    def measure(name):
        found=re.findall(re.escape(name)+r':\s*([-\w.]+)',text)
        if not found:return None
        value=float(found[-1])
        return value if math.isfinite(value) else None
    return {'peak_dbfs':measure('Peak level dB'),'rms_dbfs':measure('RMS level dB'),'method':'FFmpeg astats overall sample peak and RMS; not oversampled true peak'}


def _db(value):return 10**(value/20)
def _n(value):return format(value,'.10g')


def _edge_trim(source,settings,filters,region,p):
    """Measure leading/trailing silence without buffering/reversing a book."""
    prefix=[]
    if region:
        prefix.append(f'atrim=start={_n(region["start"])}:end={_n(region["end"])},asetpts=PTS-STARTPTS')
    prefix.extend(filters)
    prefix.append(f'silencedetect=noise={_n(p["threshold_db"])}dB:d={_n(p["minimum"])}')
    measured=_run(['-loglevel','info','-progress','pipe:1','-protocol_whitelist','file,pipe','-i',str(source),'-map','0:a:0','-af',','.join(prefix),'-f','null','-'],settings)
    times=re.findall(r'out_time_us=(\d+)',measured.stdout.decode('utf-8','replace'))
    if not times:raise RuntimeError('Could not measure silence boundaries.')
    duration=int(times[-1])/1_000_000
    events=[(kind,float(value)) for kind,value in re.findall(r'silence_(start|end):\s*([\d.eE+-]+)',measured.stderr.decode('utf-8','replace'))]
    start,end=0.0,duration
    if len(events)>=2 and events[0][0]=='start' and events[0][1]<=.001 and events[1][0]=='end':start=events[1][1]
    if len(events)>=2 and events[-2][0]=='start' and events[-1][0]=='end' and abs(events[-1][1]-duration)<.03:end=events[-2][1]
    if end<=start:raise ValueError('No non-silent audio remains at the selected trimming threshold.')
    return f'atrim=start={_n(start)}:end={_n(end)},asetpts=PTS-STARTPTS'


def _filter(kind,p,duration,input_levels,index=0):
    n=lambda key:_n(p[key])
    if kind=='pitch':
        ratio=2**((p['semitones']+p['cents']/100)/12)
        prefix=''
        if p['automation']:
            commands=';'.join(f'{_n(at)} rubberband pitch {_n(2**((value+p["cents"]/100)/12))}' for at,value in p['automation'])
            prefix=f"asendcmd=commands='{commands}',"
        return prefix+f'rubberband=tempo={n("tempo")}:pitch={_n(ratio)}:formant={p["formant"]}:pitchq=quality'
    if kind=='gain':return f'volume={n("db")}dB'
    if kind in {'highpass','lowpass'}:return f'{kind}=f={n("frequency")}:p={int(p["poles"])}'
    if kind in {'bandpass','notch'}:return f'{"bandreject" if kind=="notch" else kind}=f={n("frequency")}:t=q:w={n("q")}'
    if kind=='eq':return ','.join(f'equalizer=f={_n(b["frequency"])}:t=q:w={_n(b["q"])}:g={_n(b["gain"])}' for b in p['bands'] if b['enabled']) or 'anull'
    if kind in {'lowshelf','highshelf'}:return f'{"bass" if kind=="lowshelf" else "treble"}=f={n("frequency")}:g={n("gain")}:t=q:w={n("q")}'
    if kind=='peak_normalize':return f'volume={_n(p["target_db"]-(input_levels["peak_dbfs"] or 0))}dB'
    if kind=='loudness':return f'loudnorm=I={n("lufs")}:TP={n("true_peak")}:LRA={n("range")},aresample=48000'
    if kind=='fade':return f'afade=t=in:d={n("in_seconds")},afade=t=out:st={_n(max(0,duration-p["out_seconds"]))}:d={n("out_seconds")}'
    if kind=='envelope':
        pts=p['points']
        if not pts:return 'anull'
        expression=_n(pts[-1][1])
        for (at,a),(end,b) in reversed(list(zip(pts,pts[1:]))):expression=f'if(lt(t,{_n(end)}),{_n(a)}+({_n(b-a)})*(t-{_n(at)})/{_n(end-at)},{expression})'
        expression=f'if(lt(t,{_n(pts[0][0])}),{_n(pts[0][1])},{expression})'
        return f"volume='pow(10,({expression})/20)':eval=frame"
    if kind=='pan':return f'aformat=channel_layouts=stereo,stereotools=balance_out={n("balance")}'
    if kind=='width':return f'aformat=channel_layouts=stereo,extrastereo=m={n("width")}:c=0'
    if kind=='compressor':return f'acompressor=threshold={_n(_db(p["threshold_db"]))}:ratio={n("ratio")}:attack={n("attack")}:release={n("release")}:knee={n("knee")}:makeup={_n(_db(p["makeup_db"]))}'
    if kind=='limiter':return f'alimiter=limit={_n(_db(p["ceiling_db"]))}:attack={n("attack")}:release={n("release")}:level=false:latency=true'
    if kind in {'gate','expander'}:return f'agate=threshold={_n(_db(p["threshold_db"]))}:ratio={n("ratio")}:attack={n("attack")}:release={n("release")}:range=0'
    if kind=='deesser':return f'deesser=i={n("intensity")}:f={n("frequency")}'
    if kind=='reverb':
        if not p['mix']:return 'anull'
        delays=[p['predelay_ms']+(17+i*29)*(1+p['room_size']*2) for i in range(6)]
        decay=[p['decay']**(i+1) for i in range(6)]
        dry,wet,out=f'rd{index}',f'rw{index}',f'ro{index}'
        return (f'asplit=2[{dry}][{wet}];[{wet}]aecho=0:1:'+ '|'.join(map(_n,delays))+':'+ '|'.join(map(_n,decay))+
                f',lowpass=f={_n(20000-p["damping"]*10000)}[{out}];[{dry}][{out}]amix=inputs=2:weights=\'{_n(1-p["mix"])} {n("mix")}\':normalize=0')
    if kind=='echo':return f'aecho=1:{_n(1/(1+p["mix"]))}:{n("delay_ms")}:{_n(p["feedback"]*p["mix"])}'
    if kind=='chorus':return f'chorus=0.8:0.9:40|55:{_n(p["mix"]*.4)}|{_n(p["mix"]*.3)}:{n("rate")}|{_n(p["rate"]*.8)}:{n("depth")}|{_n(p["depth"]*.8)}' if p['mix'] else 'anull'
    if kind=='flanger':return f'flanger=delay={n("delay")}:depth={n("depth")}:regen={n("feedback")}:width={_n(p["mix"]*100)}:speed={n("rate")}:phase={n("phase")}'
    if kind=='phaser':return f'aphaser=in_gain=0.8:out_gain=0.9:delay={n("delay")}:decay={n("decay")}:speed={n("rate")}'
    if kind in {'tremolo','vibrato'}:return f'{kind}=f={n("rate")}:d={n("depth")}'
    if kind=='ring':return f"aeval=exprs='val(ch)*({_n(1-p['mix'])}+{n('mix')}*sin(2*PI*{n('frequency')}*t))':c=same"
    if kind=='saturation':return f"aeval=exprs='tanh(val(ch)*{n('drive')})/{n('drive')}':c=same"
    if kind=='distortion':return f"aeval=exprs='clip(val(ch)*{n('drive')},-{n('ceiling')},{n('ceiling')})':c=same"
    raise ValueError('Unimplemented effect.')


CODECS={'wav':['-c:a','pcm_s24le'],'flac':['-c:a','flac'],'mp3':['-c:a','libmp3lame','-q:a','2'],'opus':['-c:a','libopus','-b:a','128k'],'m4b':['-c:a','aac','-b:a','192k','-f','mp4']}


def render_chain(source,output,chain,settings,region=None,bypass=False):
    source,output=Path(source).resolve(),Path(output).resolve()
    if source==output:raise ValueError('Effects must render to a new file; the original is preserved.')
    if output.exists():raise ValueError('Output already exists; choose a new destination.')
    if not isinstance(bypass,bool):raise ValueError('Bypass must be boolean.')
    ext=output.suffix.lower().lstrip('.')
    if ext not in CODECS:raise ValueError('Unsupported audio export format.')
    info=_probe(source,settings);chain=validate_chain(chain);before=levels(source,settings)
    # Production effects use a common 48 kHz processing rate, including sources
    # synthesized at 24 kHz. This keeps the advertised 20 kHz EQ range valid.
    filters=['aresample=48000']
    elapsed=info['duration']
    if region is not None:
        if not isinstance(region,dict) or set(region)-{'start','end'}:raise ValueError('Selection requires start and end seconds.')
        start=_numeric(region.get('start'),0,info['duration']);end=_numeric(region.get('end'),0,info['duration'])
        if end<=start:raise ValueError('Selection end must follow its start.')
        region={'start':start,'end':end};elapsed=end-start
    for index,item in enumerate(chain):
        if not bypass and item['enabled']:
            if item['type']=='trim':
                filters.append(_edge_trim(source,settings,filters,region,item['params']))
                continue
            if item['type']=='pitch':
                available=_run(['-filters'],settings).stdout.decode('utf-8','replace')
                if 'rubberband' not in available:raise ValueError('This FFmpeg build lacks Rubber Band. Install a compatible build to use independent pitch/tempo.')
            effect_levels=before
            # Global peak normalization is a measured two-pass operation at
            # this point in the chain, including all earlier enabled effects.
            if item['type'] in {'peak_normalize','fade'}:
                with tempfile.TemporaryDirectory(prefix='forge-effect-meter-') as temp:
                    measured=Path(temp)/'measured.wav'
                    args=['-loglevel','error','-n','-protocol_whitelist','file,pipe','-i',str(source)]
                    if region is not None:
                        if not isinstance(region,dict) or set(region)-{'start','end'}:raise ValueError('Selection requires start and end seconds.')
                        start=_numeric(region.get('start'),0,info['duration']);end=_numeric(region.get('end'),0,info['duration'])
                        if end<=start:raise ValueError('Selection end must follow its start.')
                        args+=['-ss',_n(start),'-t',_n(end-start)]
                    args+=['-map','0:a:0','-af',','.join(filters) or 'anull','-c:a','pcm_f32le',str(measured)]
                    _run(args,settings)
                    if item['type']=='peak_normalize':effect_levels=levels(measured,settings)
                    if item['type']=='fade':elapsed=_probe(measured,settings)['duration']
            filters.append(_filter(item['type'],item['params'],elapsed,effect_levels,index))
            if item['type']=='pitch':elapsed/=item['params']['tempo']
    filt=','.join(filters) or 'anull'
    args=['-loglevel','error','-n','-protocol_whitelist','file,pipe','-i',str(source)]
    if region is not None:
        if not isinstance(region,dict) or set(region)-{'start','end'}:raise ValueError('Selection requires start and end seconds.')
        start=_numeric(region.get('start'),0,info['duration']);end=_numeric(region.get('end'),0,info['duration'])
        if end<=start:raise ValueError('Selection end must follow its start.')
        graph=(f'[0:a:0]asplit=3[pre][mid][post];[pre]atrim=end={_n(start)},asetpts=PTS-STARTPTS[a];'
               f'[mid]atrim=start={_n(start)}:end={_n(end)},asetpts=PTS-STARTPTS,{filt}[b];'
               f'[post]atrim=start={_n(end)},asetpts=PTS-STARTPTS[c];[a][b][c]concat=n=3:v=0:a=1[out]')
        args+=['-filter_complex',graph,'-map','[out]']
    else:args+=['-map','0:a:0','-af',filt]
    args+=['-map_metadata','-1',*CODECS[ext],str(output)]
    output.parent.mkdir(parents=True,exist_ok=True)
    try:
        _run(args,settings)
        after=_probe(output,settings)
        return {'chain':chain,'bypass':bypass,'region':region,'input':info,'output':after,'input_levels':before,'output_levels':levels(output,settings),'renderer':'FFmpeg; preview/export share parameters'}
    except Exception:
        if output.exists():output.unlink()
        raise


def waveform(source,settings,max_points=1000):
    if isinstance(max_points,bool) or not isinstance(max_points,int) or not 16<=max_points<=4096:raise ValueError('Waveform points must be between 16 and 4096.')
    info=_probe(source,settings);rate=8000
    binary=executable('ffmpeg',settings)
    if not binary:raise RuntimeError('FFmpeg is required for waveform analysis.')
    # Stream the complete file into fixed-width bins. Memory use does not grow
    # with audiobook length and no private audio is written to a new cache.
    width=max(1,math.ceil(info['duration']*rate/max_points));peaks=[0.0]*max_points;position=0
    with subprocess.Popen([binary,'-nostdin','-v','error','-protocol_whitelist','file,pipe','-i',str(source),'-map','0:a:0','-ac','1','-ar',str(rate),'-f','f32le','-'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,**no_window()) as proc:
        try:
            while True:
                chunk=proc.stdout.read(65536)
                if not chunk:break
                samples=array('f');samples.frombytes(chunk)
                if sys.byteorder!='little':samples.byteswap()
                for sample in samples:
                    index=min(max_points-1,position//width);position+=1
                    if math.isfinite(sample):peaks[index]=max(peaks[index],abs(sample))
                if position>rate*86401:raise ValueError('Decoded waveform exceeds the duration limit.')
            if proc.wait(timeout=30):raise ValueError('Waveform decoding failed.')
        except Exception:
            proc.kill();proc.wait(timeout=5);raise
    return {**info,'peaks':[round(p,6) for p in peaks],'points':max_points,'sample_rate_preview':rate,'method':'Measured peak bins of downsampled mono playback audio, not true-peak metering'}


def process(source,stage,options,settings):
    allowed={'chain','format','bypass','region','name','write_report','preview'}
    if not isinstance(options,dict) or set(options)-allowed:raise ValueError('Unsupported Audio Effects options.')
    stage=Path(stage);stage.mkdir(parents=True,exist_ok=True)
    fmt=options.get('format','wav')
    if fmt not in CODECS:raise ValueError('Unsupported audio export format.')
    output=stage/(safe_name(options.get('name') or Path(source).stem+'_processed')+'.'+fmt)
    preview=options.get('preview',False)
    if not isinstance(preview,bool):raise ValueError('Preview must be boolean.')
    if preview:
        info=_probe(source,settings)
        region=options.get('region') or {'start':0,'end':min(10,info['duration'])}
        if not isinstance(region,dict) or set(region)-{'start','end'}:raise ValueError('Preview requires start and end seconds.')
        start=_numeric(region.get('start'),0,info['duration']);end=_numeric(region.get('end'),0,info['duration'])
        if not 0<end-start<=30:raise ValueError('Effects previews must select between zero and 30 seconds.')
        # A preview is a short, private clip, rather than the complete recording
        # with one processed interval. The same effect parameters render the clip.
        with tempfile.TemporaryDirectory(prefix='.preview-',dir=stage) as scratch:
            clip=Path(scratch)/'selection.wav'
            _run(['-loglevel','error','-n','-protocol_whitelist','file,pipe','-i',str(source),'-ss',_n(start),'-t',_n(end-start),'-map','0:a:0','-c:a','pcm_f32le',str(clip)],settings)
            report=render_chain(clip,output,options.get('chain',[]),settings,bypass=options.get('bypass',False))
        report['preview']={'start':start,'end':end,'source_duration':info['duration'],
                           'method':'Only the selected clip is rendered; duration-changing effects and tails remain audible. Analysis-based effects measure this preview selection.'}
    else:
        report=render_chain(source,output,options.get('chain',[]),settings,region=options.get('region'),bypass=options.get('bypass',False))
    files=[output]
    if options.get('write_report',True):
        path=stage/(output.stem+'_effects.json');path.write_text(json.dumps(report,indent=2),encoding='utf-8');files.append(path)
    return files,[]
