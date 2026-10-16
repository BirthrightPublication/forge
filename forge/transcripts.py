"""Transcript exports retain raw recognition separately from optional editorial cleanup."""
from __future__ import annotations
import copy
import csv
import html
import io
import json
import math
import re
import textwrap
from pathlib import Path


def timestamp(seconds: float,comma=False):
    n=max(0,round(float(seconds)*1000));hours,n=divmod(n,3600000);minutes,n=divmod(n,60000);secs,ms=divmod(n,1000)
    return f'{hours:02}:{minutes:02}:{secs:02}{"," if comma else "."}{ms:03}'

def review_flags(segment):
    flags=[]
    if segment.get('avg_logprob',0)<-1:flags.append('Low recognition score')
    if segment.get('no_speech_prob',0)>.6:flags.append('Possible non-speech')
    if any(w.get('probability',1)<.45 for w in segment.get('words',[])):flags.append('Low-confidence word')
    return flags

def clean_segments(segments,options,language):
    """Conservative derived output: whitespace, plus opt-in English um/uh removal only."""
    result=copy.deepcopy(segments)
    for s in result:
        text=re.sub(r'\s+',' ',s['text']).strip()
        if options.get('remove_fillers',True) and language=='en':
            text=re.sub(r'(?i)\b(?:um+|uh+)\b[,;:]?\s*','',text)
            if s.get('words'):
                s['words']=[w for w in s['words'] if not re.fullmatch(r'(?i)\s*(?:um+|uh+)\W*',w['word'])]
        s['text']=text.strip()
    return [s for s in result if s['text']]

def sentence_segments(segments):
    output=[]
    for s in segments:
        words=s.get('words',[])
        if not words:
            output.append(s);continue
        group=[]
        for word in words:
            if group and word.get('speaker',s.get('speaker'))!=group[-1].get('speaker',s.get('speaker')):
                output.append(word_group(s,group));group=[]
            group.append(word)
            if re.search(r'[.!?。！？]["\'”’]*\s*$',word['word']):
                output.append(word_group(s,group));group=[]
        if group:output.append(word_group(s,group))
    return output

def word_group(segment,words):
    # Whisper tokens include spacing for Latin languages and omit it for CJK.
    text=''.join(w['word'] for w in words).strip()
    if len(words)>1 and all(not re.search(r'\s',w['word']) for w in words) and re.search('[A-Za-z]',text):
        text=' '.join(w['word'] for w in words)
    return {**segment,'start':words[0]['start'],'end':max(words[0]['start']+.001,words[-1]['end']),
            'text':text,'words':words,'speaker':words[0].get('speaker',segment.get('speaker',''))}

def assign_speakers(segments,turns):
    """Assign greatest temporal overlap. Do not infer real-world identities."""
    def label(start,end):
        overlaps={}
        for turn in turns:
            overlap=max(0,min(end,turn['end'])-max(start,turn['start']))
            if overlap:overlaps[turn['speaker']]=overlaps.get(turn['speaker'],0)+overlap
        return max(overlaps,key=overlaps.get) if overlaps else 'Unknown speaker'
    output=copy.deepcopy(segments)
    for segment in output:
        segment['speaker']=label(segment['start'],segment['end'])
        for word in segment.get('words',[]):word['speaker']=label(word['start'],word['end'])
    return output

def subtitle_cues(segments,options):
    width=options.get('subtitle_chars',42);lines=options.get('subtitle_lines',2);seconds=options.get('subtitle_seconds',6)
    result=[]
    for s in segments:
        prefix=(s.get('speaker','')+': ') if s.get('speaker') else ''
        words=s.get('words',[])
        if words:
            group=[]
            for word in words:
                trial=word_group(s,group+[word])
                label=trial.get('speaker','')
                caption=((label+': ') if label else '')+trial['text']
                changed=group and word.get('speaker',s.get('speaker'))!=group[-1].get('speaker',s.get('speaker'))
                if group and (len(textwrap.wrap(caption,width,break_long_words=True,break_on_hyphens=False))>lines or trial['end']-trial['start']>seconds or changed):
                    result.append(word_group(s,group));group=[]
                group.append(word)
            if group:result.append(word_group(s,group))
        else:
            # Rare without word timing: split text, explicitly mark interpolation.
            text=prefix+s['text'].strip()
            wrapped=textwrap.wrap(text,width,break_long_words=True,break_on_hyphens=False) or ['']
            pieces=['\n'.join(wrapped[i:i+lines]) for i in range(0,len(wrapped),lines)]
            if len(pieces)==1:
                result.append({**s,'text':pieces[0],'speaker':'','caption_ready':True});continue
            duration=max(.001,s['end']-s['start']);total=sum(len(p) for p in pieces) or 1;offset=0
            for piece in pieces:
                portion=len(piece)/total
                result.append({**s,'start':s['start']+duration*offset,'end':s['start']+duration*(offset+portion),'text':piece,'speaker':'','caption_ready':True,'timing':'interpolated'})
                offset+=portion
    for s in result:
        text=s['text'] if s.get('caption_ready') else ((s.get('speaker','')+': ') if s.get('speaker') else '')+s['text']
        s['caption']='\n'.join(textwrap.wrap(text,width,break_long_words=True,break_on_hyphens=False))
    return result

# Standalone viewer needs no running FORGE service. The user explicitly selects local media.
VIEWER_SCRIPT=r'''const data=JSON.parse(document.getElementById('transcript-data').textContent),player=document.getElementById('player'),list=document.getElementById('segments');let objectURL;
function draw(){const q=document.getElementById('search').value.toLowerCase();list.replaceChildren();for(const s of data.segments){if(q&&!((s.text+' '+(s.speaker||'')).toLowerCase().includes(q)))continue;const row=document.createElement('button');row.className='row';const time=document.createElement('span');{const ms=Math.max(0,Math.round(s.start*1000));time.textContent=String(Math.floor(ms/3600000)).padStart(2,'0')+':'+String(Math.floor(ms/60000)%60).padStart(2,'0')+':'+String(Math.floor(ms/1000)%60).padStart(2,'0')+'.'+String(ms%1000).padStart(3,'0');}const text=document.createElement('span');text.textContent=(s.speaker?s.speaker+': ':'')+s.text;row.append(time,text);row.onclick=()=>{player.currentTime=s.start;player.play().catch(()=>{});};list.append(row);}}
document.getElementById('search').oninput=draw;document.getElementById('media').onchange=e=>{if(objectURL)URL.revokeObjectURL(objectURL);const f=e.target.files[0];if(f){objectURL=URL.createObjectURL(f);player.src=objectURL;}};draw();'''

def standalone_html(segments,info,stem):
    payload=json.dumps({'segments':segments,'info':info},ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'''+html.escape(stem)+''' — FORGE transcript</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; media-src blob:; connect-src 'none'; base-uri 'none'; form-action 'none'">
<style>body{background:#11110f;color:#eeebe2;max-width:1100px;margin:auto;padding:32px;font:16px system-ui}h1{font-family:Georgia;font-weight:400}label,p{color:#b0ab9b}input{background:#20201b;color:inherit;padding:12px;border:1px solid #514a35;max-width:95%}video{width:100%;max-height:360px;margin:20px 0}.row{display:flex;gap:24px;width:100%;text-align:left;padding:16px;background:#1a1a16;color:inherit;border:0;border-bottom:1px solid #39362c;font:inherit}.row span:first-child{color:#c1ad73;font:13px monospace}.row:hover{background:#2a261b}</style>
<h1>'''+html.escape(stem)+'''</h1><p>Local interactive transcript. Select the matching audio or video to use timestamp playback. No upload or internet connection is needed. Timing and recognition are estimates.</p>
<label>Choose local media <input id="media" type="file" accept="audio/*,video/*"></label><video id="player" controls preload="metadata"></video><input id="search" placeholder="Search transcript" aria-label="Search transcript"><div id="segments"></div>
<script id="transcript-data" type="application/json">'''+payload+'''</script><script>'''+VIEWER_SCRIPT+'''</script></html>'''

def csv_cell(value):
    value=str(value)
    return "'"+value if value.lstrip().startswith(('=','+','-','@','\t','\r')) else value

def export_transcript(segments,info,options,stage_dir,stem):
    raw=copy.deepcopy(segments)
    for s in raw:
        if options.get('flag_uncertain',True):s['review_flags']=review_flags(s)
    clean=options.get('style','raw')=='clean'
    working=clean_segments(raw,options,info.get('language','')) if clean else copy.deepcopy(raw)
    if options['timestamps']=='sentence':working=sentence_segments(working)
    result=[]
    if clean:
        # Raw remains intact even when the user selects only cleaned TXT/HTML.
        raw_options={**options,'style':'raw','timestamps':'segment','formats':['txt','json']}
        result.extend(export_transcript(raw,info,raw_options,stage_dir,stem+'_raw'))
        stem+='_clean'
    for fmt in dict.fromkeys(options['formats']):
        path=stage_dir/(stem+'.'+fmt)
        if fmt=='json':
            text=json.dumps({'engine':'faster-whisper','model':options['model'],'info':info,'segments':working,'style':'clean' if clean else 'raw',
                'note':'ASR output is not a guaranteed verbatim record. Timing and review flags are estimates. Clean mode changes whitespace and optionally English um/uh fillers; its raw outputs are saved separately.'},ensure_ascii=False,indent=2)
        elif fmt=='txt':
            rows=[]
            for s in working:
                if options['timestamps']=='word' and s.get('words'):
                    rows.extend(f'[{timestamp(w["start"])}] '+((w.get('speaker')+': ') if w.get('speaker') else '')+w['word'].strip() for w in s['words'])
                else:
                    rows.append((f'[{timestamp(s["start"])}] ' if options['timestamps']!='none' else '')+((s.get('speaker')+': ') if s.get('speaker') else '')+s['text'].strip())
            text='\n'.join(rows)+'\n' if rows else '[No speech detected]\n'
        elif fmt in {'srt','vtt'}:
            blocks=[]
            for i,s in enumerate(subtitle_cues(working,options),1):
                cue=f'{timestamp(s["start"],fmt=="srt")} --> {timestamp(max(s["start"]+.001,s["end"]),fmt=="srt")}\n{s["caption"]}\n'
                blocks.append((f'{i}\n' if fmt=='srt' else '')+cue)
            text=('WEBVTT\n\n' if fmt=='vtt' else '')+'\n'.join(blocks)
        elif fmt=='csv':
            buf=io.StringIO(newline='');writer=csv.writer(buf)
            writer.writerow(['start','end','speaker','text','review_flags'])
            for s in working:writer.writerow([timestamp(s['start']),timestamp(s['end']),csv_cell(s.get('speaker','')),csv_cell(s['text'].strip()),'; '.join(s.get('review_flags',[]))])
            text=buf.getvalue()
        elif fmt=='html':text=standalone_html(working,info,stem)
        else:raise ValueError('Unsupported transcript format.')
        path.write_text(text,encoding='utf-8');result.append(path)
    return result
