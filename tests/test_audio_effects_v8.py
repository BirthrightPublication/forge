"""Real FFmpeg signal-response tests; inputs are synthetic and test-owned."""
from array import array
import hashlib
import math
from pathlib import Path
import shutil
import struct
import subprocess
import wave

import pytest

from forge.audio_effects import EFFECTS, levels, process, render_chain, schema, validate_chain, waveform

pytestmark=pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg media tools required')
RATE=48000


def write(path,samples):
    with wave.open(str(path),'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(RATE)
        out.writeframes(b''.join(struct.pack('<h',max(-32767,min(32767,round(x*32767)))) for x in samples))
    return path


def tone(path,frequency=440,seconds=1,amplitude=.2):
    return write(path,[amplitude*math.sin(2*math.pi*frequency*i/RATE) for i in range(int(seconds*RATE))])


def samples(path):
    raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(path),'-ac','1','-ar',str(RATE),'-f','f32le','-'])
    result=array('f');result.frombytes(raw);return result


def rms(values):return math.sqrt(sum(x*x for x in values)/max(1,len(values)))


def response(values,frequency):
    # Coherent projection on the test tone, independent of a FFT dependency.
    values=values[RATE//10:RATE//2]
    real=sum(x*math.cos(2*math.pi*frequency*i/RATE) for i,x in enumerate(values))
    imag=sum(x*math.sin(2*math.pi*frequency*i/RATE) for i,x in enumerate(values))
    return math.hypot(real,imag)/len(values)


def test_gain_normalization_order_bypass_and_original(tmp_path):
    source=tone(tmp_path/'original.wav');digest=hashlib.sha256(source.read_bytes()).digest()
    render_chain(source,tmp_path/'gain.wav',[{'type':'gain','params':{'db':-6}}],{})
    assert rms(samples(tmp_path/'gain.wav'))/rms(samples(source))==pytest.approx(10**(-6/20),rel=.01)
    render_chain(source,tmp_path/'peak.wav',[{'type':'gain','params':{'db':-12}},{'type':'peak_normalize','params':{'target_db':-3}}],{})
    assert levels(tmp_path/'peak.wav',{})['peak_dbfs']==pytest.approx(-3,abs=.1)
    render_chain(source,tmp_path/'bypass.wav',[{'type':'gain','params':{'db':-12}}],{},bypass=True)
    assert rms(samples(tmp_path/'bypass.wav'))==pytest.approx(rms(samples(source)),rel=.001)
    assert hashlib.sha256(source.read_bytes()).digest()==digest
    with pytest.raises(ValueError,match='new file'):render_chain(source,source,[],{})


def test_independent_pitch_and_tempo(tmp_path):
    filters=subprocess.check_output(['ffmpeg','-hide_banner','-filters'],stderr=subprocess.DEVNULL,text=True)
    if 'rubberband' not in filters:pytest.skip('Rubber Band unavailable in this build')
    source=tone(tmp_path/'source.wav',seconds=2)
    meta=render_chain(source,tmp_path/'pitch.wav',[{'type':'pitch','params':{'semitones':12}}],{})
    data=samples(tmp_path/'pitch.wav')
    assert meta['output']['duration']==pytest.approx(2,abs=.025)
    assert response(data,880)>response(data,440)*20
    meta=render_chain(source,tmp_path/'tempo.wav',[{'type':'pitch','params':{'tempo':2}}],{})
    assert meta['output']['duration']==pytest.approx(1,abs=.03)
    assert response(samples(tmp_path/'tempo.wav'),440)>response(samples(tmp_path/'tempo.wav'),880)*10


def test_equalizer_changes_frequency_response(tmp_path):
    source=write(tmp_path/'two-tones.wav',[(.1*math.sin(2*math.pi*300*i/RATE)+.1*math.sin(2*math.pi*4000*i/RATE)) for i in range(RATE)])
    render_chain(source,tmp_path/'eq.wav',[{'type':'eq','params':{'bands':[{'frequency':300,'gain':12,'q':2}]}}],{})
    a,b=samples(source),samples(tmp_path/'eq.wav')
    assert response(b,300)/response(a,300)>3.5
    assert response(b,4000)/response(a,4000)<1.2


def test_envelopes_and_bypass_are_real(tmp_path):
    source=tone(tmp_path/'source.wav',seconds=2)
    render_chain(source,tmp_path/'envelope.wav',[{'type':'envelope','params':{'points':[[0,-18],[.5,-18],[1,-3],[2,-3]]}}],{})
    out=samples(tmp_path/'envelope.wav')
    assert rms(out[60000:70000])>rms(out[5000:15000])*4
    render_chain(source,tmp_path/'off.wav',[{'type':'gain','enabled':False,'params':{'db':-12}}],{})
    assert rms(samples(tmp_path/'off.wav'))==pytest.approx(rms(samples(source)),rel=.001)
    if 'rubberband' in subprocess.check_output(['ffmpeg','-hide_banner','-filters'],stderr=subprocess.DEVNULL,text=True):
        render_chain(source,tmp_path/'automated.wav',[{'type':'pitch','params':{'automation':[[0,12],[1,0]]}}],{})
        audio=samples(tmp_path/'automated.wav')
        assert response(audio[:RATE],880)>response(audio[:RATE],440)*5
        assert response(audio[RATE:],440)>response(audio[RATE:],880)*5


def test_speech_sample_rate_is_supported(tmp_path):
    source=tmp_path/'speech-rate.wav'
    original=tone(tmp_path/'48k.wav')
    subprocess.run(['ffmpeg','-v','error','-i',str(original),'-ar','24000',str(source)],check=True)
    result=render_chain(source,tmp_path/'processed.wav',[{'type':'lowpass'}],{})
    assert result['input']['sample_rate']==24000 and result['output']['sample_rate']==48000


def test_compression_and_order_change_actual_samples(tmp_path):
    source=write(tmp_path/'dynamic.wav',[(.02 if i<RATE//2 else .7)*math.sin(2*math.pi*440*i/RATE) for i in range(RATE)])
    compression={'type':'compressor','params':{'threshold_db':-24,'ratio':10,'attack':1,'release':20}}
    gain={'type':'gain','params':{'db':6}}
    render_chain(source,tmp_path/'compress.wav',[compression],{})
    a,b=samples(source),samples(tmp_path/'compress.wav')
    assert rms(b[30000:40000])/rms(b[10000:20000])<rms(a[30000:40000])/rms(a[10000:20000])/2
    render_chain(source,tmp_path/'one.wav',[gain,compression],{})
    render_chain(source,tmp_path/'two.wav',[compression,gain],{})
    assert abs(rms(samples(tmp_path/'one.wav'))-rms(samples(tmp_path/'two.wav')))>0.01


def test_reverb_has_a_real_tail(tmp_path):
    source=write(tmp_path/'impulse.wav',[.5]+[0]*(RATE//10-1))
    render_chain(source,tmp_path/'room.wav',[{'type':'reverb','params':{'mix':.5}}],{})
    data=samples(tmp_path/'room.wav')
    assert len(data)>RATE//10
    assert max(abs(x) for x in data[RATE//10:])>.001


def test_edge_trim_keeps_interior_pauses_without_reverse_buffering(tmp_path):
    audio=[0]*(RATE//4)+[.2*math.sin(2*math.pi*440*i/RATE) for i in range(RATE//4)]+[0]*(RATE//4)+[.2*math.sin(2*math.pi*440*i/RATE) for i in range(RATE//4)]+[0]*(RATE//4)
    source=write(tmp_path/'pauses.wav',audio)
    result=render_chain(source,tmp_path/'trimmed.wav',[{'type':'trim'}],{})
    assert result['output']['duration']==pytest.approx(.75,abs=.015)
    output=samples(tmp_path/'trimmed.wav')
    assert rms(output[14000:20000])<.001


def test_region_preserves_audio_outside_selection(tmp_path):
    source=tone(tmp_path/'source.wav',seconds=2)
    render_chain(source,tmp_path/'region.wav',[{'type':'gain','params':{'db':-12}}],{},region={'start':.5,'end':1.5})
    a,b=samples(source),samples(tmp_path/'region.wav')
    assert len(a)==len(b)
    assert rms(b[:20000])==pytest.approx(rms(a[:20000]),rel=.001)
    assert rms(b[40000:60000])/rms(a[40000:60000])==pytest.approx(10**(-12/20),rel=.01)
    assert rms(b[80000:])==pytest.approx(rms(a[80000:]),rel=.001)


@pytest.mark.parametrize('kind',list(EFFECTS))
def test_every_advertised_effect_produces_valid_media(tmp_path,kind):
    if kind=='pitch' and 'rubberband' not in subprocess.check_output(['ffmpeg','-hide_banner','-filters'],stderr=subprocess.DEVNULL,text=True):pytest.skip('Rubber Band unavailable')
    source=tone(tmp_path/'source.wav',seconds=.4)
    chain=[{'type':kind}]
    if kind=='trim':chain=[{'type':kind,'params':{'threshold_db':-60}}]
    result=render_chain(source,tmp_path/(kind+'.wav'),chain,{})
    assert result['output']['duration']>0


@pytest.mark.parametrize('fmt',['wav','flac','mp3','opus','m4b'])
def test_export_codec_and_identical_preview_parameters(tmp_path,fmt):
    source=tone(tmp_path/'source.wav',seconds=.3)
    options={'chain':[{'type':'gain','params':{'db':-3}}],'format':fmt}
    files,warnings=process(source,tmp_path/fmt,options,{})
    assert len(files)==2 and not warnings and files[0].suffix=='.'+fmt
    assert files[1].is_file()


def test_waveform_is_bounded_and_measured(tmp_path):
    source=tone(tmp_path/'source.wav',amplitude=.2)
    result=waveform(source,{},64)
    assert len(result['peaks'])==64 and max(result['peaks'])==pytest.approx(.2,abs=.01)
    assert result['duration']==pytest.approx(1,abs=.01)


def test_preview_renders_only_selection_and_removes_private_clip(tmp_path):
    source=tone(tmp_path/'original.wav',seconds=3)
    original=hashlib.sha256(source.read_bytes()).hexdigest()
    files,_=process(source,tmp_path/'preview',{'chain':[{'type':'gain','params':{'db':-6}}],
                   'preview':True,'region':{'start':1,'end':1.25}}, {})
    import json
    receipt=json.loads(files[1].read_text())
    assert receipt['output']['duration']==pytest.approx(.25,abs=.005)
    assert receipt['preview']['start']==1 and receipt['preview']['end']==1.25
    assert receipt['preview']['source_duration']==pytest.approx(3,abs=.005)
    assert receipt['output_levels']['rms_dbfs']-receipt['input_levels']['rms_dbfs']==pytest.approx(-6,abs=.1)
    assert not list((tmp_path/'preview').glob('.preview-*'))
    assert hashlib.sha256(source.read_bytes()).hexdigest()==original


def test_reject_untrusted_effects_and_bad_values():
    for chain in [[{'type':'volume=movie=/private'}],[{'type':'gain','params':{'db':'1;amovie=secret'}}],[{'type':'gain','params':{'db':float('nan')}}],[{'type':'pitch','params':{'formant_shift':2}}],[{'type':'envelope','params':{'points':[[1,0],[0,1]]}}]]:
        with pytest.raises(ValueError):validate_chain(chain)
    assert len(schema())==len(EFFECTS)
    assert validate_chain([{'type':'gain','enabled':False}])[0]['enabled'] is False
