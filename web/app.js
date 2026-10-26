'use strict';
const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
const escape = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
 grid:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
 queue:'<path d="M8 5h13M8 12h13M8 19h13"/><path d="M3 5h.01M3 12h.01M3 19h.01"/>',
 transcribe:'<rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0014 0v-2M12 19v3M8 22h8"/>',
 retune:'<path d="M3 9v6M7 5v14M12 2v20M17 5v14M21 9v6"/>',
 download_audio:'<path d="M12 3v12m-4-4l4 4 4-4M4 17v4h16v-4"/>',
 download_video:'<rect x="3" y="4" width="18" height="14" rx="2"/><path d="M9 21h6M12 18v3M10 8l5 3-5 3z"/>',
 metadata:'<path d="M12 2l8 3v6c0 5-5 9-8 11-3-2-8-6-8-11V5zM8 12l3 3 5-6"/>',
 qr:'<path d="M3 3h6v6H3zM15 3h6v6h-6zM3 15h6v6H3zM15 15h3v3h3v3h-6zM12 3v3M12 10v3H7M3 12h2M12 17v4M21 12h-4"/>',
 password:'<circle cx="8" cy="8" r="5"/><path d="M12 12l9 9M17 17l3-3M14 14l3-3"/>',
 settings:'<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
 upload:'<path d="M12 16V3m-5 5l5-5 5 5M3 16v5h18v-5"/>',
 folder:'<path d="M3 6a2 2 0 012-2h5l2 3h7a2 2 0 012 2v10a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>',
 file:'<path d="M5 2h9l5 5v15H5zM14 2v6h5M8 12h8M8 16h8"/>',
 check:'<path d="M5 12l4 4L20 5"/>',
 search:'<circle cx="10" cy="10" r="6"/><path d="M15 15l6 6"/>',
 arrow:'<path d="M5 12h14m-6-6l6 6-6 6"/>',
 chevron:'<path d="M14 6l-6 6 6 6"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
 cpu:'<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
 pause:'<path d="M8 5v14M16 5v14"/>',
 play:'<path d="M7 3l14 9-14 9z"/>',
 close:'<path d="M6 6l12 12M6 18L18 6"/>',
 refresh:'<path d="M20 4v6h-6M4 20v-6h6M5 8a8 8 0 0113-4l2 3M4 17l2 3a8 8 0 0013-4"/>',
 stop:'<rect x="5" y="5" width="14" height="14" rx="1"/>',
};
const icon = n => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${paths[n] || paths.file}</svg>`;
let token = '', csrf = '', launchTicket = '';
window.startSources = [];
try {
 const hash=new URLSearchParams(location.hash.slice(1));
 token=hash.get('token')||'';launchTicket=hash.get('launch')||'';
 window.startSources=(hash.get('sources')||'').split(',').filter(Boolean);
 // Retire the V0.1 tab-bound token; never persist the runtime secret in browser storage.
 if(!token&&!launchTicket){try{token=sessionStorage.getItem('forge-token')||'';}catch{}}
 try{sessionStorage.removeItem('forge-token');}catch{}
 history.replaceState(null,'',location.pathname);
} catch {}
async function authorizeBrowser(){
 if(token||launchTicket){
  const response=await fetch('/api/session',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Forge-Bootstrap':'1',...(token?{'X-Forge-Token':token}:{})},body:JSON.stringify({ticket:launchTicket})});
  token='';launchTicket='';
  if(response.ok){csrf=(await response.json()).csrf;return;}
 }
 const response=await fetch('/api/session',{credentials:'same-origin',cache:'no-store'});
 if(!response.ok)throw new Error('Open FORGE once from its launcher to remember this browser profile. Private windows or cleared site data need separate authorization.');
 csrf=(await response.json()).csrf;
}
let state = null, view = 'downloads', filter = 'all', pollBusy = false, lastError = false;
let toastTimer, secretTimer, previewURL;
const drafts = {};
const defaults = {
 transcribe:{model:'large-v3',device:'auto',language:'',task:'transcribe',timestamps:'segment',formats:['txt','srt','json'],beam_size:5,vad:true,vocabulary:'',allow_model_download:true},
 retune:{source_hz:440,target_hz:432,preserve_tempo:true,format:'mp3',keep_tags:true,quality:'high'},
 metadata:{audit_report:false},
 qr:{text:'',name:'QR Code',format:'png',error_correction:'M',box_size:12,border:4},
 download_audio:{format:'mp3',quality:'best',max_height:0,embed_metadata:true,cookies:''},
 download_video:{format:'mp4',quality:'best',max_height:0,embed_metadata:true,cookies:''},
 password:{length:24,count:1,upper:true,numbers:true,symbols:'!@#$%^-_=+[]{}?',unicode_chars:false,exclude_ambiguous:true}
};
const titles = {workflows:'Workflows',model_download:'Model download',overview:'Overview',queue:'Processing Queue',settings:'Settings',transcribe:'Transcribe',retune:'432Hz Conversion',download_audio:'Audio Download',download_video:'Video Download',metadata:'Metadata Studio',qr:'QR Codes',password:'Passwords'};
const mediaViews = ['transcribe','retune','metadata','workflows'];
function draft(tool=view) {
 if (!drafts[tool]) drafts[tool]={options:structuredClone(defaults[tool]||{}),sources:[],urls:'',output:{mode:mediaViews.includes(tool)?'source':'default',directory:'',subfolder:''}};
 return drafts[tool];
}
function notify(message,error=false) {
 const t=$('#toast'); t.textContent=message;t.className='toast'+(error?' error':'');
 clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.add('hidden'),error?12000:6000);
}
async function api(route,body,raw=false) {
 const opts={headers:{'X-Forge-CSRF':csrf},credentials:'same-origin',cache:'no-store'};
 if (body!==undefined){opts.method='POST';opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body);}
 const response=await fetch('/api'+route,opts);
 if(!response.ok){
  let message=`Local engine returned ${response.status}`;
  try{let d=(await response.json()).detail;message=typeof d==='string'?d:JSON.stringify(d);}catch{}
  throw new Error(message);
 }
 return raw?response:response.json();
}
function bytes(n){if(n<1024)return `${n} B`;if(n<1048576)return `${(n/1024).toFixed(1)} KB`;if(n<1073741824)return `${(n/1048576).toFixed(1)} MB`;return `${(n/1073741824).toFixed(2)} GB`;}
function when(n){return new Date(n*1000).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});}
function busyJobs(){return(state?.jobs||[]).filter(j=>['queued','running','cancelling','finalizing'].includes(j.status));}
function ready(tool){
 const deps=Object.fromEntries((state?.dependencies||[]).map(d=>[d.id,d.ready]));
 if(tool==='password')return true;
 if(tool==='qr')return deps.qrcode&&deps.pillow;
 if(tool==='metadata')return deps.pillow;
 if(tool==='transcribe')return deps['faster-whisper'];
 if(tool.startsWith('download'))return deps['yt-dlp']&&deps.ffmpeg&&deps.ffprobe;
 return deps.ffmpeg&&deps.ffprobe;
}
function renderNav(){
 $('#workspace-nav').innerHTML=`<button class="nav-item ${view==='settings'?'active':''}" data-view="settings" title="Settings">${icon('settings')}<span class="nav-text">Settings</span></button>`;
 $('#tool-nav').innerHTML=Object.keys(defaults).map(v=>`<button class="nav-item ${view===v?'active':''}" data-view="${v}" title="${titles[v]}">${icon(v)}<span class="nav-text">${titles[v]}</span></button>`).join('');
 $$('[data-view]', $('#sidebar')).forEach(b=>b.onclick=()=>navigate(b.dataset.view));
 if($('#crumb'))$('#crumb').textContent=titles[view];
}
function heading(eyebrow,title,description,actions=''){
 return `<div class="page-heading"><div><div class="eyebrow">${escape(eyebrow)}</div><h1>${escape(title)}</h1><p>${escape(description)}</p></div>${actions?`<div class="heading-actions">${actions}</div>`:''}</div>`;
}
function panel(title,body,caption='',footer=''){
 return `<section class="panel"><div class="panel-header"><h2>${escape(title)}</h2><span>${escape(caption)}</span></div><div class="panel-body">${body}</div>${footer}</section>`;
}
function field(label,name,value,{type='text',options=null,hint='',full=false,min=null,max=null,step=null,placeholder=''}={}) {
 const attrs=`name="${name}" id="field-${name}" ${min!==null?`min="${min}"`:''} ${max!==null?`max="${max}"`:''} ${step!==null?`step="${step}"`:''}`;
 const el=options?`<select ${attrs}>${options.map(([v,l])=>`<option value="${escape(v)}" ${String(v)===String(value)?'selected':''}>${escape(l)}</option>`).join('')}</select>`:
 type==='textarea'?`<textarea ${attrs} placeholder="${escape(placeholder)}">${escape(value)}</textarea>`:
 `<input ${attrs} type="${type}" value="${escape(value)}" placeholder="${escape(placeholder)}" autocomplete="off">`;
 return `<div class="field ${full?'full':''}"><label for="field-${name}">${escape(label)}</label>${el}${hint?`<p class="hint">${escape(hint)}</p>`:''}</div>`;
}
function check(label,name,value,hint=''){
 return `<div><label class="checkbox"><input type="checkbox" name="${name}" ${value?'checked':''}>${escape(label)}</label>${hint?`<p class="note">${escape(hint)}</p>`:''}</div>`;
}
function navigate(next){
 $('#clear-password')?.click();clearTimeout(secretTimer);capture();view=next;$('#search').value='';
 renderNav();render();window.scrollTo({top:0});
}
function capture(){
 if(view==='workflows'&&typeof captureWorkflow==='function'){captureWorkflow();return;}
 if(defaults[view]&&$('#tool-options')){
  const d=draft();const root=$('#tool-options');
  Object.entries(d.options).forEach(([key,old])=>{
   if(key==='formats'){d.options.formats=$$('input[name="formats"]:checked',root).map(e=>e.value);return;}
   if(key==='remove_tags'){d.options.remove_tags=$$('input[name="remove_tags"]:checked',root).map(e=>e.value);return;}
   if(key==='tags'){d.options.tags=Object.fromEntries($$('[data-meta-tag]',root).filter(e=>e.value).map(e=>[e.dataset.metaTag,e.value]));return;}
   const el=$(`[name="${key}"]`,root);if(!el)return;
   d.options[key]=el.type==='checkbox'?el.checked:typeof old==='number'?Number(el.value):el.value;
  });
  if($('#urls'))d.urls=$('#urls').value;
  if($('#out-mode'))d.output.mode=$('#out-mode').value;
  if($('#out-path'))d.output.directory=$('#out-path').value;
  if($('#out-subfolder'))d.output.subfolder=$('#out-subfolder').value;
 }
}
function render(){
 if(!state)return;
 if(view==='overview')renderOverview();
 else if(view==='queue')renderQueue();
 else if(view==='settings')renderSettings();
 else if(view==='workflows')renderWorkflow();
 else if(view==='password')renderPassword();
 else renderTool();
 renderDock();
}
function renderOverview(search=''){
 const jobs=state.jobs, active=jobs.filter(j=>['running','finalizing'].includes(j.status)).length;
 const completed=jobs.filter(j=>j.status==='completed').length,queued=jobs.filter(j=>j.status==='queued').length;
 const cards=Object.keys(defaults).filter(k=>(titles[k]+' '+state.tools[k].description).toLowerCase().includes(search.toLowerCase()));
 $('#main').innerHTML=heading('YOUR LOCAL WORKSPACE','Production, in one place.','Select a tool. Configure the work. Keep control of your files.',`<button class="secondary" id="env-open">${icon('settings')}Environment</button>`)+
 `<div class="stats"><div class="stat"><label>Processing now ${icon('cpu')}</label><strong>${active}<span style="font-size:15px;color:var(--muted)"> / ${state.settings.workers}</span></strong><small>${state.settings.queue_paused?'Dispatch paused':'Shared local processing slots'}</small></div><div class="stat"><label>In the queue ${icon('queue')}</label><strong>${queued}</strong><small>Ready when a slot is available</small></div><div class="stat"><label>Completed ${icon('check')}</label><strong>${completed}</strong><small>From the most recent 500 jobs</small></div><div class="stat"><label>Tool library ${icon('grid')}</label><strong>7</strong><small>${Object.keys(defaults).filter(ready).length} ready · optional engines available</small></div></div>`+
 `<div class="section-title"><h2>Production tools</h2><span>LOCAL-FIRST · NON-DESTRUCTIVE</span></div><div class="tool-grid">${cards.map(k=>`<button class="tool-card" data-tool="${k}"><div class="tool-card-top"><div class="tool-icon">${icon(k)}</div><span class="arrow">↗</span></div><h3>${titles[k]}</h3><p>${state.tools[k].description}</p><div class="card-meta">${ready(k)?'<span class="dot"></span>':'<span class="tag warn">SETUP</span>'}${k.startsWith('download')?'Internet required':k==='transcribe'?'Local models · CPU / GPU':k==='password'?'Memory only · not logged':k==='metadata'?'Images ready · media needs FFmpeg':'Local processing'}<span style="margin-left:auto">${escape(state.tools[k].group)}</span></div></button>`).join('')||'<p class="search-empty">No tools match this search.</p>'}</div>`+
 `<div class="section-title"><h2>Recent activity</h2><button class="text-button" id="recent-all">View queue ↗</button></div><section class="panel" id="recent-jobs">${jobs.length?jobs.slice(0,3).map(jobRow).join(''):emptyQueue()}</section>`;
 $('#env-open').onclick=()=>navigate('settings');$('#recent-all').onclick=()=>navigate('queue');
 $$('[data-tool]').forEach(b=>b.onclick=()=>navigate(b.dataset.tool));bindJobActions();
}
function emptyQueue(){return `<div class="empty-state">${icon('queue')}<h3>Your queue is clear.</h3>Add a task from any production tool. It will appear here with its status and outputs.</div>`;}
function filesPanel(){
 const d=draft();
 return panel('Source files',`<div class="dropzone" id="dropzone" tabindex="0" role="button" aria-label="Drop files or press Enter for native file selection"><div class="drop-icon">${icon('upload')}</div><h3>Drop your files here</h3><p>${view==='metadata'?'Supported images, audio or video':'Audio or video'} · multiple files accepted<br>Browser drops create a local staging copy.</p><div class="actions"><button type="button" class="secondary" id="native-browse">${icon('folder')}Native browse</button><button type="button" class="secondary" id="paste-paths">Paste paths</button></div></div><input id="browser-files" type="file" multiple hidden><div class="inline"><button class="text-button" id="browser-browse">Import through browser</button><button class="text-button" id="add-folder" style="margin-left:auto">Add folder</button></div><div id="upload-status" class="upload-status"></div><div class="file-list" id="file-list"></div><div class="callout neutral">Native browse and pasted Explorer paths read the original file directly. Use these for large media and reliable same-folder exports.</div>`,'ORIGINALS ARE KEPT');
}
function fileList(){
 if(!$('#file-list'))return;
 const d=draft();
 $('#file-list').innerHTML=d.sources.map((s,i)=>`<div class="file-item">${icon('file')}<div class="file-label"><strong title="${escape(s.name)}">${escape(s.name)}</strong><small title="${escape(s.path)}">${bytes(s.size)} · ${s.mode==='native'?'Original path · no copy':'Staged copy · source folder unknown'}</small></div><button class="remove-file" data-remove="${i}" aria-label="Remove ${escape(s.name)}">×</button></div>`).join('');
 $$('[data-remove]').forEach(b=>b.onclick=()=>{draft().sources.splice(Number(b.dataset.remove),1);fileList();updateOutputPreview();});
 updateOutputPreview();
}
function optionsHTML(){
 const o=draft().options;
 if(typeof advancedOptions==='function'&&view!=='qr')return advancedOptions(view,o);

 if(view==='transcribe')return `<div class="fields">${field('Recognition model','model',o.model,{options:[['large-v3','Large V3 · high accuracy'],['large-v3-turbo','Large V3 Turbo · faster'],['medium','Medium'],['small','Small'],['base','Base'],['tiny','Tiny · quick test']]})}${field('Processing device','device',o.device,{options:[['auto','Automatic · GPU / CPU'],['cuda','NVIDIA CUDA · GPU only'],['cpu','CPU · no CUDA required']]})}${field('Spoken language','language',o.language,{options:[['','Detect automatically'],['en','English'],['es','Spanish'],['de','German'],['fr','French'],['it','Italian'],['pt','Portuguese'],['ru','Russian'],['ar','Arabic'],['ja','Japanese'],['zh','Chinese'],['ko','Korean']]})}${field('Task','task',o.task,{options:[['transcribe','Original-language transcription'],['translate','Translate speech to English']]})}${field('TXT timestamp detail','timestamps',o.timestamps,{options:[['none','No timestamps in TXT'],['segment','Speech-segment timestamps'],['word','Word timestamps · TXT + JSON']]})}${field('Beam size','beam_size',o.beam_size,{type:'number',min:1,max:10,hint:'Higher search breadth can increase processing time.'})}<div class="field full"><label>Export formats</label><div class="checks">${['txt','srt','vtt','json'].map(f=>`<label class="checkbox"><input name="formats" type="checkbox" value="${f}" ${o.formats.includes(f)?'checked':''}>${f.toUpperCase()}</label>`).join('')}</div><p class="hint">SRT and VTT use speech-segment cues. Word timing is estimated, not frame-perfect.</p></div>${field('Names and custom vocabulary','vocabulary',o.vocabulary,{type:'textarea',full:true,placeholder:'Names, unusual spellings, technical terms…',hint:'A recognition hint, not online research. The transcript is not rewritten or censored.'})}<div class="field full">${check('Filter non-speech with voice activity detection','vad',o.vad)}</div></div><div class="separator"></div><div class="upcoming"><span class="tag">LATER</span> Speaker labels, editorial versions and alignment engines</div>`;
 if(view==='retune')return `<div class="fields">${field('Assumed source tuning (Hz)','source_hz',o.source_hz,{type:'number',min:400,max:480,step:.1})}${field('Target tuning (Hz)','target_hz',o.target_hz,{type:'number',min:400,max:480,step:.1})}${field('Output format','format',o.format,{options:[['mp3','MP3 · audio'],['flac','FLAC · lossless audio'],['wav','WAV · 24-bit audio'],['m4a','M4A · AAC audio'],['mp4','MP4 · copied video + retuned audio'],['mkv','MKV · copied video + retuned audio']]})}${field('Encoding quality','quality',o.quality,{options:[['high','High'],['standard','Standard']]})}<div class="field full">${check('Preserve playback speed and duration','preserve_tempo',o.preserve_tempo)}</div><div class="field full">${check('Retain compatible source metadata','keep_tags',o.keep_tags)}</div></div><div class="separator"></div><div class="callout">This shifts the first audio track by target ÷ source. It does not detect concert pitch or make every sound 432 Hz. Source sample rate is read from the actual file.</div><p class="note">FORGE uses sample-rate-aware pitch shifting and optional tempo compensation. Playback length can vary slightly at file boundaries. Video exports copy the primary video track. Originals are never replaced.</p>`;
 if(view==='metadata')return `<div class="callout neutral">Create a separate metadata-reduced file. Your original remains untouched.</div><div class="separator"></div><div class="detail-list"><strong>Images</strong><br>JPEG, PNG and WebP are re-encoded without EXIF, embedded text or color profiles. Orientation is applied.<br><br><strong>Audio & video</strong><br>Compatible streams are copied without re-encoding. Metadata, covers, attachments, subtitles and chapters are excluded.</div><div class="separator"></div>${check('Write a private before / after audit report','audit_report',o.audit_report)}<p class="note">The optional audit includes original metadata. Keep it private; do not share it beside the cleaned file.</p><div class="separator"></div><div class="callout">Metadata removal is not a guarantee of anonymity. Visible content, watermarks, technical container tags and filesystem history can remain. PDF, Office and animated-image cleaning are not enabled in V0.1.</div>`;
 if(view==='qr')return `<div class="fields">${field('Content to encode','text',o.text,{type:'textarea',full:true,placeholder:'Paste a link or enter plain text…',hint:'No QR website or API is contacted. The QR content is retained in local job history for retry.'})}${field('Output name','name',o.name)}${field('Output format','format',o.format,{options:[['png','PNG · image'],['svg','SVG · scalable vector']]})}${field('Error correction','error_correction',o.error_correction,{options:[['L','L · 7%'],['M','M · 15%'],['Q','Q · 25%'],['H','H · 30%']]})}${field('Pixels per module','box_size',o.box_size,{type:'number',min:4,max:30})}${field('Quiet-zone border (modules)','border',o.border,{type:'number',min:4,max:16,hint:'A minimum four-module clear border is preserved.'})}</div>`;
 const audio=view==='download_audio';
 return `<div class="fields">${field('Output format','format',o.format,{options:audio?[['mp3','MP3'],['m4a','M4A'],['flac','FLAC'],['wav','WAV']]:[['mp4','MP4 · compatible source formats'],['mkv','MKV · flexible codec support']]})}${audio?field('Audio quality','quality',o.quality,{options:[['best','Best available / encoder quality'],['320','320 kbps · MP3 target'],['192','192 kbps · MP3 target'],['128','128 kbps · MP3 target']]}):field('Maximum video height','max_height',o.max_height,{options:[[0,'Best available'],[720,'720p'],[1080,'1080p'],[1440,'1440p'],[2160,'2160p / 4K']]})}<div class="field full">${check('Embed available title and creator metadata','embed_metadata',o.embed_metadata)}</div>${field('Optional Netscape cookies file','cookies',o.cookies,{full:true,placeholder:'C:\\yt-dlp\\youtube_cookies.txt',hint:'Opt-in only. Cookies are used by yt-dlp for the supplied site; the original cookie file is not rewritten.'})}</div><div class="separator"></div><div class="callout neutral">Each URL becomes one job. Playlists and live broadcasts are not processed in V0.1. Download only media you have permission to save.</div><p class="note">YouTube may require the yt-dlp EJS package and a supported Deno or Node runtime. The included installer enables the download engine; website restrictions can still prevent particular downloads.</p>`;
}
function outputPanel(){
 const d=draft();
 const opts=(mediaViews.includes(view)?[['source','Same folder as original']]:[]).concat([['custom','Choose a custom folder'],['last','Use last custom folder'],['default','FORGE default export folder']]);
 return panel('Output destination',`<div class="fields"><div class="field full"><label for="out-mode">Save results to</label><select id="out-mode">${opts.map(([v,l])=>`<option value="${v}" ${d.output.mode===v?'selected':''}>${l}</option>`).join('')}</select></div><div class="field full ${d.output.mode==='custom'?'':'hidden'}" id="out-path-wrap"><label for="out-path">Output directory</label><div class="path-input"><input id="out-path" value="${escape(d.output.directory)}" placeholder="C:\\Production\\Exports"><button class="secondary" id="choose-output" title="Choose output directory" aria-label="Choose output directory">${icon('folder')}</button></div></div><div class="field full"><label for="out-subfolder">Optional subfolder</label><input id="out-subfolder" value="${escape(d.output.subfolder)}" placeholder="e.g. Transcripts"></div></div><div class="output-preview">${icon('folder')}<span id="output-preview"></span></div>`, 'NO SILENT OVERWRITES', `<div class="form-footer"><small>Originals preserved.<br>Existing names receive a numbered suffix.</small><button class="primary wide-button" id="enqueue">${icon('play')}Add to queue</button></div>`);
}
function renderTool(){
 const d=draft();
 const downloadable=view.startsWith('download_');
 const caption=state.tools[view].description;
 const presetButton=['qr'].includes(view)?'':`<select class="preset-select" id="presets" aria-label="Load saved preset"><option value="">Load a preset…</option>${state.presets.filter(p=>p.tool===view).map(p=>`<option value="${escape(p.name)}">${escape(p.name)}</option>`).join('')}</select><button class="secondary" id="save-preset">Save preset</button>`;
 const availability=ready(view)?'':`<div class="callout" style="margin-bottom:20px">${view==='transcribe'?'Transcription engine is not installed. Run Install Transcription.bat once.':view.startsWith('download')?'Install the download engine and configure FFmpeg before running this tool.':'Configure FFmpeg and FFprobe before processing media.'} <button class="text-button" id="tool-env">Environment ↗</button></div>`;
 const source=mediaViews.includes(view)?filesPanel():downloadable?panel('Media URLs',`<div class="field"><label for="urls">One URL per line</label><textarea id="urls" placeholder="https://www.youtube.com/watch?v=…" style="min-height:190px">${escape(d.urls)}</textarea><p class="hint">HTTP and HTTPS only. Video URLs are queued separately; the browser remains responsive while downloads run.</p></div><div class="separator"></div><div class="inline">${icon('download_audio')}<span class="note" style="margin:0">This tool uses the internet only when you submit a job.</span></div>`,'ONLINE TOOL'):
 panel('Local QR generation',`<div class="dropzone" style="border-style:solid;min-height:225px"><div class="drop-icon">${icon('qr')}</div><h3>From content to code.</h3><p>Encode a URL or plain text.<br>Export a print-ready SVG or a pixel-scaled PNG.</p><span class="tag">GENERATED LOCALLY</span></div><p class="note">Finished jobs include a QR preview and the output location. No third-party QR service is used.</p>`);
 $('#main').innerHTML=heading('PRODUCTION / '+state.tools[view].group,titles[view],caption,presetButton+`<button class="primary" id="enqueue-top">${icon('play')}Add to queue</button>`)+availability+
 `<div class="studio-layout"><div class="column">${source}${outputPanel()}</div><div class="column"><section class="panel"><div class="panel-header"><h2>Configuration</h2><span>${view==='transcribe'?'UNEDITED ASR OUTPUT':'BEFORE PROCESSING'}</span></div><div class="panel-body" id="tool-options">${optionsHTML()}</div></section>${panel('Shared processing',`<div class="step"><span>1</span><p><strong>Configure this operation.</strong><br>Settings are saved with each queued job.</p></div><div class="step"><span>2</span><p><strong>Continue working in any tool.</strong><br>The queue stays active after closing this browser tab.</p></div><div class="step" style="margin:0"><span>3</span><p><strong>Review outputs in the queue.</strong><br>Cancel during processing or retry a finished job.</p></div>`)}</div></div>`;
 $('#tool-env')?.addEventListener('click',()=>navigate('settings'));
 if(mediaViews.includes(view)){bindFiles();fileList();}
 $('#out-mode').onchange=()=>{capture();$('#out-path-wrap').classList.toggle('hidden',draft().output.mode!=='custom');updateOutputPreview();};
 $('#out-path').oninput=()=>{capture();updateOutputPreview();};$('#out-subfolder').oninput=()=>{capture();updateOutputPreview();};
 $('#choose-output').onclick=async()=>{try{const r=await nativePick('folder');if(r.paths?.length){$('#out-path').value=r.paths[0];capture();updateOutputPreview();}}catch(e){notify(e.message,true);}};
 $('#enqueue').onclick=enqueue;$('#enqueue-top').onclick=enqueue;
 $('#save-preset')?.addEventListener('click',savePreset);
 $('#presets')?.addEventListener('change',e=>{const p=state.presets.find(p=>p.tool===view&&p.name===e.target.value);if(p){d.options={...structuredClone(defaults[view]),...structuredClone(p.options)};d.output=structuredClone(p.output);renderTool();notify('Preset loaded.');}});
 updateOutputPreview();
 if(typeof enhanceTool==='function')enhanceTool();
}
function updateOutputPreview(){
 if(!$('#output-preview'))return;
 const d=draft();let text='';
 if(d.output.mode==='source'){
  text=d.sources.length?(d.sources.some(s=>s.mode==='staged')?'Original folder unavailable for a staged file. Choose a custom folder or use Native browse.':'Beside each original file. No source files will be overwritten.'):'Select an original file to use its folder.';
 }else if(d.output.mode==='custom')text=d.output.directory||'Choose a folder before submitting this job.';
 else if(d.output.mode==='last')text=state.settings.last_output||'No custom output folder has been used yet.';
 else text=state.settings.default_output||'Set a default output folder in Environment.';
 if(d.output.subfolder)text+=' / '+d.output.subfolder;
 $('#output-preview').textContent=text;
}
async function nativePick(kind){notify('Choose a '+(kind==='files'?'file':'folder')+' in the native Windows dialog.');return api('/pick',{kind});}
function addSources(sources,tool=view){
 const d=draft(tool);const extensions=state.tools[tool]?.accept||[];
 const accepted=sources.filter(s=>extensions.some(ext=>s.name.toLowerCase().endsWith(ext)));
 const rejected=sources.length-accepted.length;
 const known=new Set(d.sources.map(s=>s.path));
 for(const s of accepted){if(!known.has(s.path)){d.sources.push(s);known.add(s.path);}}
 if(rejected)notify(`${rejected} unsupported file(s) were not added to this tool.`,true);
 if(d.sources.some(s=>s.mode==='staged')&&d.output.mode==='source'){
  d.output.mode='default';notify('Browser drops hide the source folder. Output is set to the FORGE default; choose a custom folder to change it.');
 }
 if(view===tool){capturePreservingOutput(d);if(view==='workflows')renderWorkflow();else renderTool();}
}
function capturePreservingOutput(d){const mode=d.output.mode;capture();d.output.mode=mode;}
function bindFiles(){
 $('#native-browse').onclick=async e=>{e.stopPropagation();const tool=view;capture();try{addSources((await nativePick('files')).sources||[],tool);}catch(e){notify(e.message,true);}};
 $('#paste-paths').onclick=e=>{e.stopPropagation();openPaths();};
 $('#browser-browse').onclick=()=>$('#browser-files').click();
 $('#browser-files').onchange=e=>importFiles([...e.target.files]);
 $('#add-folder').onclick=async()=>{const tool=view;capture();try{const r=await nativePick('folder');if(r.paths?.length){const f=await api('/register-folder',{path:r.paths[0]});addSources(f.sources,tool);if(f.truncated)notify('The folder was limited to 500 supported files. Use smaller batches.');}}catch(e){notify(e.message,true);}};
 const zone=$('#dropzone');
 zone.onkeydown=e=>{if(e.key==='Enter'&&e.target===zone)$('#native-browse').click();};
 zone.ondragover=e=>{e.preventDefault();zone.classList.add('dragging');};
 zone.ondragleave=e=>{if(!zone.contains(e.relatedTarget))zone.classList.remove('dragging');};
 zone.ondrop=e=>{e.preventDefault();zone.classList.remove('dragging');importFiles([...e.dataTransfer.files]);};
}
async function importFiles(files){
 const tool=view;capture();let results=[];
 if(!files.length)return;
 for(let i=0;i<files.length;i++){
  const f=files[i];
  if(!state.tools[tool].accept.some(ext=>f.name.toLowerCase().endsWith(ext))){notify(`${f.name}: unsupported format or folder. Use Add folder for folders.`,true);continue;}
  if($('#upload-status'))$('#upload-status').textContent=`Staging ${i+1} / ${files.length}: ${f.name} (${bytes(f.size)})`;
  try{
   const r=await fetch('/api/upload?name='+encodeURIComponent(f.name),{method:'POST',headers:{'X-Forge-CSRF':csrf,'Content-Type':'application/octet-stream'},credentials:'same-origin',body:f});
   const data=await r.json();if(!r.ok)throw new Error(data.detail||'Staging failed.');results.push(data.source);
  }catch(e){notify(e.message,true);}
 }
 if($('#upload-status'))$('#upload-status').textContent='';
 addSources(results,tool);
}
function openPaths(){
 const tool=view;capture();
 const dialog=$('#dialog');$('#dialog-content').innerHTML=`<h2>Add original file paths</h2><p>In Explorer, select files and choose “Copy as path.” Paste one complete path per line. Quotes are accepted.</p><div class="field"><textarea id="path-list" class="file-paths" placeholder='"C:\\Production\\Interview.mp4"'></textarea></div><button class="primary" id="paths-submit" style="margin-top:16px">Add files</button>`;
 dialog.showModal();$('#path-list').focus();$('#paths-submit').onclick=async()=>{try{const paths=$('#path-list').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);const r=await api('/register',{paths});dialog.close();addSources(r.sources,tool);}catch(e){notify(e.message,true);}};
}
async function enqueue(){
 capture();const tool=view,d=draft(),button=$('#enqueue'),topButton=$('#enqueue-top');button.disabled=true;if(topButton)topButton.disabled=true;
 try{
  const request={tool,sources:d.sources.map(s=>s.id),urls:d.urls.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),options:d.options,output:d.output};
  const r=await api('/jobs',request);notify(`${r.jobs.length} job${r.jobs.length===1?'':'s'} added to the universal queue.`);
  if(mediaViews.includes(tool))d.sources=[];
  if(tool.startsWith('download'))d.urls='';
  await refresh(false);navigate('queue');
 }catch(e){notify(e.message,true);}finally{if(button.isConnected)button.disabled=false;if(topButton?.isConnected)topButton.disabled=false;}
}
async function savePreset(){
 capture();const name=prompt('Name this settings preset:');if(!name?.trim())return;
 try{await api('/presets',{tool:view,name:name.trim(),options:draft().options,output:draft().output});await refresh(false);renderTool();notify('Preset saved locally.');}catch(e){notify(e.message,true);}
}
function jobRow(j){
 const running=['running','finalizing','cancelling'].includes(j.status);
 return `<div class="job-row"><div class="tool-icon">${icon(j.tool)}</div><div class="job-main"><strong title="${escape(j.label)}">${escape(j.label)}</strong><small>${escape(titles[j.tool])} · ${when(j.created)}</small>${running?`<div class="progress"><i style="width:${Math.min(100,Math.max(0,j.progress))}%"></i></div>`:''}</div><div class="job-state ${j.status}">${running?`${Math.round(j.progress)}% · `:''}${escape(j.status)}</div><div class="job-actions"><button class="secondary" data-job="${j.id}">Details</button>${['queued','running'].includes(j.status)?`<button class="secondary" data-cancel="${j.id}">Cancel</button>`:['failed','cancelled','interrupted'].includes(j.status)?`<button class="secondary" data-retry="${j.id}">Retry</button>`:''}</div></div>`;
}
function renderQueue(){
 $('#main').innerHTML=heading('WORKSPACE / ACTIVITY','Universal processing queue','Manage local jobs across every production tool. Passwords are deliberately excluded from history.',`<button class="secondary" id="pause-queue">${icon(state.settings.queue_paused?'play':'pause')}${state.settings.queue_paused?'Resume queue':'Pause queue'}</button>`)+
 `<div class="callout neutral" style="margin-bottom:23px">Pause stops new jobs from starting; active jobs continue. Cancel stops a running job. Retry starts a new job from the beginning and preserves existing outputs.</div><div class="queue-toolbar">${[['all','All jobs'],['active','Active'],['completed','Completed'],['attention','Needs attention']].map(([k,l])=>`<button class="filter ${filter===k?'active':''}" data-filter="${k}">${l}</button>`).join('')}<span style="margin-left:auto;color:var(--muted);font-size:10px">${state.settings.workers} slots · one transcription job at a time</span></div><section class="panel" id="queue-list"></section><p class="note">Showing the most recent ${state.history_limit} jobs. Local history survives browser closure and application restarts.</p>`;
 $('#pause-queue').onclick=async()=>{try{await api('/queue',{paused:!state.settings.queue_paused});await refresh(false);renderQueue();}catch(e){notify(e.message,true);}};
 $$('[data-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.filter;renderQueue();});updateQueueList();
}
function updateQueueList(){
 if(!$('#queue-list'))return;
 const jobs=state.jobs.filter(j=>filter==='all'||filter==='active'&&['queued','running','cancelling','finalizing'].includes(j.status)||filter==='completed'&&j.status==='completed'||filter==='attention'&&['failed','interrupted','cancelled'].includes(j.status));
 $('#queue-list').innerHTML=jobs.length?jobs.map(jobRow).join(''):emptyQueue();bindJobActions();
}
function bindJobActions(){
 $$('[data-job]').forEach(b=>b.onclick=()=>jobDetail(b.dataset.job));
 $$('[data-cancel]').forEach(b=>b.onclick=async()=>{try{await api(`/jobs/${b.dataset.cancel}/cancel`,{});await refresh();}catch(e){notify(e.message,true);}});
 $$('[data-retry]').forEach(b=>b.onclick=async()=>{try{await api(`/jobs/${b.dataset.retry}/retry`,{});notify('A new retry job was queued.');await refresh();}catch(e){notify(e.message,true);}});
}
async function jobDetail(id){
 const j=state.jobs.find(j=>j.id===id);if(!j)return;
 const dialog=$('#dialog');
 $('#dialog-content').innerHTML=`<h2>${escape(j.label)}</h2><p>${escape(titles[j.tool])} · ${escape(j.status)} · ${when(j.created)}</p><pre>${escape(j.message)}</pre>${j.outputs.length?`<h3>Outputs</h3><div class="outputs">${j.outputs.map((f,i)=>`<div class="output-item"><div class="name">${escape(f.name)}<p style="margin:5px 0 0;font-size:9px">${bytes(f.size)} · ${escape(f.path)}</p></div><button class="secondary" data-reveal="${i}">In Explorer</button>${f.size<10000000?`<button class="secondary" data-save="${i}">Save a copy</button>`:''}</div>`).join('')}</div>`:''}${j.warnings.length?`<h3>Processing notes</h3>${j.warnings.map(w=>`<p>${escape(w)}</p>`).join('')}`:''}${['completed','failed','cancelled','interrupted'].includes(j.status)?'<button class="secondary" id="rerun-job" style="margin-top:17px">Run again as a new job</button>':''}<div id="preview-area"></div>`;
 if(!dialog.open)dialog.showModal();
 $$('[data-reveal]').forEach(b=>b.onclick=async()=>{try{const r=await api(`/jobs/${id}/reveal/${b.dataset.reveal}`,{});if(!r.opened)notify(r.path);}catch(e){notify(e.message,true);}});
 $$('[data-save]').forEach(b=>b.onclick=async()=>{try{const i=Number(b.dataset.save),r=await api(`/jobs/${id}/outputs/${i}`,undefined,true),u=URL.createObjectURL(await r.blob()),a=document.createElement('a');a.href=u;a.download=j.outputs[i].name;a.click();setTimeout(()=>URL.revokeObjectURL(u),10000);}catch(e){notify(e.message,true);}});
 $('#rerun-job')?.addEventListener('click',async()=>{try{await api(`/jobs/${id}/retry`,{});dialog.close();notify('A new job was queued.');await refresh();}catch(e){notify(e.message,true);}});
 if(j.tool==='qr'&&j.outputs.length){try{if(previewURL)URL.revokeObjectURL(previewURL);const r=await api(`/jobs/${id}/outputs/0`,undefined,true);previewURL=URL.createObjectURL(await r.blob());const img=document.createElement('img');img.src=previewURL;img.className='qr-preview';img.alt='Generated QR code';$('#preview-area')?.append(img);}catch{}}
}
function renderPassword(){
 const o=draft().options;
 $('#main').innerHTML=heading('UTILITIES / PRIVACY','Password generator','Cryptographic randomness. No password files, queue records, or saved values.')+
 `<div class="studio-layout"><div class="column">${panel('Character policy',`<div id="tool-options"><div class="fields">${field('Password length','length',o.length,{type:'number',min:8,max:256})}${field('Number of passwords','count',o.count,{type:'number',min:1,max:50})}<div class="field full">${check('Include uppercase letters','upper',o.upper)}</div><div class="field full">${check('Include numbers','numbers',o.numbers)}</div>${field('Allowed symbols / additional characters','symbols',o.symbols,{full:true,hint:'Leave blank for no symbols. Each selected non-empty group is represented.'})}<div class="field full">${check('Exclude ambiguous characters (I l 1 O 0 o)','exclude_ambiguous',o.exclude_ambiguous)}</div><div class="field full">${check('Include extended Unicode characters','unicode_chars',o.unicode_chars,'Some sites reject Unicode passwords. Enable only where supported.')}</div></div></div>`,'LOCAL / MEMORY ONLY',`<div class="form-footer"><small>Lowercase letters are always included.</small><button class="primary" id="generate-password">${icon('password')}Generate</button></div>`)}</div><div class="column">${panel('Generated values',`<div class="secret-output" id="secret-output">No password generated.</div><div class="inline" style="margin-top:14px"><button class="secondary" id="copy-password" disabled>Copy</button><button class="secondary" id="clear-password" disabled>Clear</button></div><p class="note">Values clear from this page after 60 seconds or when you leave this tool. Copying puts them on your system clipboard; FORGE cannot guarantee clipboard-history removal.</p>`)}<div class="callout neutral">Passwords are generated by Python’s secrets module. They bypass the universal queue so their values are not written to its database or job history.</div></div></div>`;
 let secret='';
 const clear=()=>{secret='';if($('#secret-output')){$('#secret-output').textContent='No password generated.';$('#copy-password').disabled=true;$('#clear-password').disabled=true;}};
 $('#generate-password').onclick=async()=>{capture();try{const r=await api('/passwords',draft().options);secret=r.passwords.join('\n');$('#secret-output').textContent=secret;$('#copy-password').disabled=false;$('#clear-password').disabled=false;clearTimeout(secretTimer);secretTimer=setTimeout(clear,60000);}catch(e){notify(e.message,true);}};
 $('#copy-password').onclick=async()=>{try{await navigator.clipboard.writeText(secret);notify('Copied to the system clipboard.');}catch{notify('Clipboard access was blocked. Select and copy the displayed value manually.',true);}};
 $('#clear-password').onclick=clear;
}
function renderSettings(){
 const s=state.settings;
 $('#main').innerHTML=heading('WORKSPACE / SYSTEM','Environment','Manage local dependencies, export preferences and controlled update discovery.',`<button class="secondary" id="scan-env">${icon('refresh')}Refresh checks</button>`)+
 `<div class="settings-grid"><div class="column">${panel('Installed components',`<div id="dependency-list">${state.dependencies.map(dep=>`<div class="dependency"><div><strong>${escape(dep.label)}</strong><small>${escape(dep.detail)}</small></div><span class="dep-status ${dep.ready?'':'missing'}">${dep.ready?'AVAILABLE':'SETUP NEEDED'}</span></div>`).join('')}</div><p class="note">These checks confirm presence, not a completed GPU or website compatibility test.</p>`)}${panel('Install optional engines',`<div class="step"><span>1</span><p><strong>Install Transcription.bat</strong><br>Installs faster-whisper into FORGE’s private environment. Model weights are downloaded only after your consent.</p></div><div class="step"><span>2</span><p><strong>Install Downloads.bat</strong><br>Installs yt-dlp with its standard extras. FFmpeg, FFprobe and a supported JS runtime are separate tools.</p></div><p class="note">Close FORGE before installing packages. Open it again after installation. CUDA drivers and runtime libraries are not installed by this application.</p>`)}${panel('Local storage',`<p class="note" style="margin-top:0">Settings, queue history, staged copies and models:</p><pre class="detail-list" style="overflow-wrap:anywhere;white-space:pre-wrap">${escape(state.data_directory)}</pre><p class="note">Paths, URLs, QR contents and job settings are stored locally and are not encrypted. Generated passwords are not stored.</p><button class="secondary" id="clear-staging" style="margin-top:15px">Remove unused staging copies</button>`)}</div><div class="column">${panel('Runtime settings',`<div id="settings-fields" class="fields">${field('Concurrent processing slots','workers',s.workers,{options:[[1,'1 · sequential'],[2,'2 · balanced'],[3,'3'],[4,'4'],[5,'5'],[6,'6'],[7,'7'],[8,'8']],full:true,hint:'Total slots cap all work; CPU/GPU/download limits further restrict each category.'})}${field('FFmpeg executable or bin folder','ffmpeg',s.ffmpeg,{full:true,placeholder:'C:\\ffmpeg\\bin\\ffmpeg.exe',hint:'Blank: search tools folder, known C: locations, then PATH.'})}<button class="text-button" data-pick-exe="ffmpeg">Browse for ffmpeg.exe</button>${field('FFprobe executable or bin folder','ffprobe',s.ffprobe,{full:true,placeholder:'C:\\ffmpeg\\bin\\ffprobe.exe',hint:'Blank: also check beside your configured FFmpeg executable.'})}<button class="text-button" data-pick-exe="ffprobe">Browse for ffprobe.exe</button>${field('Default export directory','default_output',s.default_output,{full:true})}<button class="text-button" id="pick-default">Choose default export folder</button><div class="field full">${check('Check releases once per day when FORGE opens','daily_update_check',s.daily_update_check,'Opt-in queries to PyPI only. No media, filenames or transcripts are sent.')}</div></div>`, 'NO AUTOMATIC INSTALLATIONS',`<div class="form-footer"><small>Changes apply to new jobs.</small><button class="primary" id="save-settings">Save settings</button></div>`)}${panel('Update discovery',`<div id="updates-info">${updatesHTML()}</div><button class="secondary" id="check-updates" style="margin-top:16px">${icon('refresh')}Check trusted releases</button><p class="note">Package checks report versions only. Application ZIP updates and code rollback use Update FORGE.bat; packages are not silently upgraded.</p>`)}${panel('Service controls',`<p class="note" style="margin-top:0">Closing the browser leaves the local queue running. This button stops FORGE itself. Unfinished jobs will require a retry when you reopen it.</p><button class="danger" id="stop-service" style="margin-top:17px">${icon('stop')}Stop local service</button>`)}</div></div>`;
 $('#scan-env').onclick=async()=>{await refresh(false);renderSettings();notify('Local component checks refreshed.');};
 $$('[data-pick-exe]').forEach(b=>b.onclick=async()=>{try{const r=await api('/pick',{kind:'executable'});if(r.paths?.length)$(`[name="${b.dataset.pickExe}"]`).value=r.paths[0];}catch(e){notify(e.message,true);}});
 $('#pick-default').onclick=async()=>{try{const r=await nativePick('folder');if(r.paths?.length)$('[name="default_output"]').value=r.paths[0];}catch(e){notify(e.message,true);}};
 $('#save-settings').onclick=async()=>{const values={...state.settings};for(const k of ['workers','ffmpeg','ffprobe','default_output','daily_update_check']){const el=$(`[name="${k}"]`);values[k]=el.type==='checkbox'?el.checked:k==='workers'?Number(el.value):el.value;}try{await api('/settings',values);await refresh(false);renderSettings();notify('Settings saved.');}catch(e){notify(e.message,true);}};
 $('#check-updates').onclick=async()=>{const b=$('#check-updates');b.disabled=true;b.textContent='Checking release metadata…';try{state.updates=await api('/updates/check',{});$('#updates-info').innerHTML=updatesHTML();notify('Release check finished. No packages were installed.');}catch(e){notify(e.message,true);}finally{b.disabled=false;b.textContent='Check trusted releases';}};
 $('#clear-staging').onclick=async()=>{if(!confirm('Remove unused browser staging copies? Failed jobs using those copies will need their inputs re-imported. Original files and completed outputs are never deleted.'))return;try{const r=await api('/staging/clear',{});notify(`${r.removed} unused staging folders removed.`);}catch(e){notify(e.message,true);}};
 $('#stop-service').onclick=async()=>{if(!confirm('Stop the local service? Running jobs will be interrupted and the queue paused.'))return;try{await api('/shutdown',{});$('#main').innerHTML='<div class="connection-error">'+heading('SERVICE STOPPED','FORGE is closed.','Open FORGE from your Start menu or taskbar to resume using the Studio.')+'</div>';lastError=true;}catch(e){notify(e.message,true);}};
}
function updatesHTML(){
 const u=state.updates;if(!u)return '<p class="note" style="margin:0">No online check has been performed. Local utilities work without one.</p>';
 return `<p class="note" style="margin:0 0 14px">Last check: ${when(u.checked)}</p>${(u.packages||[]).map(p=>`<div class="dependency"><strong style="font-size:10px">${escape(p.package)}</strong><small>${escape(p.installed||'Not installed')} → ${escape(p.latest||p.error||'Unknown')}</small></div>`).join('')}`;
}
function renderDock(){
 const active=state.jobs.filter(j=>['running','finalizing','cancelling'].includes(j.status)),queued=state.jobs.filter(j=>j.status==='queued').length;
 $('#dock-title').innerHTML=icon('queue')+' UNIVERSAL QUEUE';
 $('#dock-status').textContent=`${active.length} processing · ${queued} queued${state.settings.queue_paused?' · Dispatch paused':''}${active[0]?' · '+active[0].message:''}`;
 const c=$('.counter');if(c)c.textContent=busyJobs().length;
}
async function refresh(update=true){
 if(pollBusy)return;pollBusy=true;
 try{
  const previous=state;state=await api('/state');
  if(typeof setupV2State==='function')setupV2State(previous);
  if(update){renderDock();if(view==='queue')updateQueueList();if(view==='overview'&&!$('#search').value){renderOverview();}}
 }catch(e){if(!lastError)notify('Local engine connection lost. Reopen FORGE from its launcher to reconnect.',true);lastError=true;}finally{pollBusy=false;}
}
async function boot(){
 let bootPhase='authorization';
 $('#search-icon').innerHTML=icon('search');$('#queue-top').innerHTML=icon('queue');$('#collapse').innerHTML=icon('chevron');
 $('#home-brand').onclick=e=>{e.preventDefault();navigate(window.ForgeShell?.lastTool()||'downloads');};$('#queue-top').onclick=()=>navigate('queue');$('#dock-open').onclick=()=>navigate('queue');
 $('#collapse').onclick=()=>{document.body.classList.toggle('compact');try{localStorage.setItem('forge-compact',String(document.body.classList.contains('compact')));}catch{}};
 try{if(localStorage.getItem('forge-compact')!=='false')document.body.classList.add('compact');}catch{}
 $('#search').oninput=e=>{$('#clear-password')?.click();clearTimeout(secretTimer);capture();view='overview';renderNav();renderOverview(e.target.value);};
 document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('#search').focus();}});
 document.addEventListener('dragover',e=>e.preventDefault());
 document.addEventListener('drop',e=>{e.preventDefault();if(!$('#dropzone')||!$('#dropzone').contains(e.target))notify('Open a file-based tool and drop files into its source box.');});
 try{
  await authorizeBrowser();
  bootPhase='connection';
  state=await api('/state');
  bootPhase='interface';
  if(typeof setupV2State==='function')setupV2State(null);
  try{const saved=localStorage.getItem('forge-last-tool');if(saved&&titles[saved]&&!['overview','queue','workflows','settings'].includes(saved))view=saved;}catch{}
  if(window.startSources.length){const r=await api('/sources?ids='+encodeURIComponent(window.startSources.join(',')));view=r.sources.every(s=>/\.(png|jpe?g|webp)$/i.test(s.name))?'metadata':'transcribe';draft().sources=r.sources;}
  renderNav();render();setInterval(()=>refresh(),1800);
 }catch(e){
  const display=bootPhase==='interface'
   ? ['INTERFACE','FORGE could not display this page.','The local engine connected, but the interface encountered an error.','Press Ctrl+F5 to reload the interface. If the error continues, report the message below. Setup is not needed to repair a browser rendering error.']
   : bootPhase==='connection'
   ? ['LOCAL ENGINE','FORGE could not connect.','The local engine did not return its state.','Open FORGE.bat to reconnect, then reload this page. A bookmark cannot start a stopped Windows service.']
   : ['LOCAL AUTHORIZATION','Authorize this browser once.','Open FORGE.bat or the FORGE Start-menu shortcut to authorize this browser.','Use the launcher after restarting Windows or stopping FORGE. Opening index.html directly is not supported.'];
  $('#main').innerHTML=`<div class="connection-error">${heading(...display.slice(0,3))}<div class="callout">${escape(e.message)}</div><p class="note">${escape(display[3])}</p></div>`;
 }
}
// The V0.2 extension schedules boot() after all deferred scripts register.

