'use strict';
Object.assign(defaults.transcribe,{formats:['txt','srt','json','html'],style:'raw',remove_fillers:true,subtitle_chars:42,subtitle_lines:2,subtitle_seconds:6,flag_uncertain:true,diarization:false,diarization_model:'',min_speakers:1,max_speakers:6});
Object.assign(defaults.retune,{normalize:false,loudness_lufs:-16,true_peak_db:-1.5,mp3_bitrate:0,wav_bits:24});
Object.assign(defaults.metadata,{mode:'wipe',remove_tags:[],tags:{}});
for(const tool of ['download_audio','download_video'])Object.assign(defaults[tool],{playlist:false,playlist_start:1,playlist_end:25,subtitles:false,auto_subtitles:false,subtitle_languages:'en',filename_style:'title_id'});
const selectedJobs=new Set();
let workflowSteps=[{tool:'metadata',preset:''},{tool:'transcribe',preset:''}],workflowName='Publication preparation';
let notificationLeader=false;
if(navigator.locks)navigator.locks.request('forge-notification-leader',{ifAvailable:true},lock=>{if(lock){notificationLeader=true;return new Promise(()=>{});}}).catch(()=>{});
function setupV2State(previous){
 state.tools.workflows={accept:[...new Set(state.tools.transcribe.accept.concat(state.tools.metadata.accept))]};
 $('.version').textContent='V'+state.version.replace(/\.0$/,'');
 if(previous&&state.settings.desktop_notifications&&notificationLeader&&'Notification'in window&&Notification.permission==='granted'){
  for(const j of state.jobs){const old=previous.jobs.find(p=>p.id===j.id);if(old&&old.status!==j.status&&['completed','failed','blocked'].includes(j.status)){try{new Notification('FORGE · '+j.status,{body:j.label,tag:'forge-'+j.id});}catch{}}}
 }
}
function advancedOptions(tool,o){
 if(tool==='transcribe')return `<div class="fields">
 ${field('Recognition model','model',o.model,{options:[['large-v3','Large V3 · accuracy'],['large-v3-turbo','Large V3 Turbo · speed'],['medium','Medium'],['small','Small'],['base','Base'],['tiny','Tiny · short test']]})}
 ${field('Processing device','device',o.device,{options:[['auto','Automatic · GPU / CPU'],['cuda','NVIDIA CUDA only'],['cpu','CPU only']]})}
 ${field('Spoken language','language',o.language,{options:[['','Detect automatically'],['en','English'],['es','Spanish'],['de','German'],['fr','French'],['it','Italian'],['pt','Portuguese'],['ru','Russian'],['ar','Arabic'],['ja','Japanese'],['zh','Chinese'],['ko','Korean']]})}
 ${field('Task','task',o.task,{options:[['transcribe','Original-language transcription'],['translate','Translate to English']]})}
 ${field('Transcript style','style',o.style,{options:[['raw','Raw · unedited recognition'],['clean','Clean copy + preserved raw files']]})}
 ${field('Timestamp layout','timestamps',o.timestamps,{options:[['none','No TXT timestamps'],['segment','Speech segments'],['sentence','Sentences · from timed words'],['word','Individual words · TXT / JSON']]})}
 ${field('Search breadth / beam size','beam_size',o.beam_size,{type:'number',min:1,max:10})}
 ${field('Subtitle characters per line','subtitle_chars',o.subtitle_chars,{type:'number',min:20,max:80})}
 ${field('Subtitle lines per cue','subtitle_lines',o.subtitle_lines,{type:'number',min:1,max:3})}
 ${field('Target maximum cue duration (s)','subtitle_seconds',o.subtitle_seconds,{type:'number',min:1,max:15,step:.5})}
 <div class="field full"><label>Export formats</label><div class="checks">${['txt','srt','vtt','json','csv','html'].map(f=>`<label class="checkbox"><input name="formats" type="checkbox" value="${f}" ${o.formats.includes(f)?'checked':''}>${f.toUpperCase()}</label>`).join('')}</div><p class="hint">HTML opens an offline searchable viewer. Select the matching media there for click-to-seek playback. JSON enables review inside FORGE.</p></div>
 ${field('Names and custom vocabulary','vocabulary',o.vocabulary,{type:'textarea',full:true,placeholder:'Names, uncommon spellings, terminology…'})}
 <div class="field full">${check('Filter non-speech','vad',o.vad)}</div>
 <div class="field full">${check('Flag uncertain recognition for review','flag_uncertain',o.flag_uncertain,'Heuristic review flags are not calibrated accuracy scores.')}</div>
 <div class="field full">${check('Remove English “um/uh” fillers in the clean copy','remove_fillers',o.remove_fillers,'Clean mode changes whitespace and optional fillers only. It does not fact-check, censor or paraphrase. Raw TXT/JSON are kept.')}</div>
 <div class="field full">${check('Allow first model download','allow_model_download',o.allow_model_download,'Off: cached models only. On: Hugging Face may be contacted for model files; media processing remains local.')}</div>
 </div><div class="separator"></div><details><summary>Optional local speaker labels</summary><div class="fields" style="margin-top:16px"><div class="field full">${check('Separate speakers · optional engine','diarization',o.diarization,'Requires Install Speaker Labels.bat and a complete trusted offline Community-1 model. CPU labeling is separate from speech-recognition GPU settings.')}</div>
 ${field('Local Community-1 model folder','diarization_model',o.diarization_model,{full:true,placeholder:'B:\\Models\\pyannote-speaker-diarization-community-1'})}<button class="text-button" id="pick-diarization">Choose model folder</button>
 ${field('Minimum speakers','min_speakers',o.min_speakers,{type:'number',min:1,max:20})}${field('Maximum speakers','max_speakers',o.max_speakers,{type:'number',min:1,max:20})}</div><p class="note">Anonymous Speaker 1/2 labels are estimates, not identity recognition. Gated model access is handled with its publisher, not by storing account tokens in FORGE.</p></details>`;
 if(tool==='retune')return `<div class="fields">
 ${field('Assumed source tuning (Hz)','source_hz',o.source_hz,{type:'number',min:400,max:480,step:.1})}${field('Target tuning (Hz)','target_hz',o.target_hz,{type:'number',min:400,max:480,step:.1})}
 ${field('Output format','format',o.format,{options:[['mp3','MP3'],['flac','FLAC · lossless encoding'],['wav','WAV · PCM'],['m4a','M4A · AAC'],['mp4','MP4 · copied video'],['mkv','MKV · copied video']]})}
 ${field('Encoding quality','quality',o.quality,{options:[['high','High'],['standard','Standard']]})}
 ${field('MP3 bitrate','mp3_bitrate',o.mp3_bitrate,{options:[[0,'Variable bitrate · quality above'],[128,'128 kbps'],[192,'192 kbps'],[256,'256 kbps'],[320,'320 kbps']]})}${field('WAV bit depth','wav_bits',o.wav_bits,{options:[[16,'16-bit PCM'],[24,'24-bit PCM']]})}
 <div class="field full">${check('Preserve playback speed','preserve_tempo',o.preserve_tempo)}</div><div class="field full">${check('Keep source metadata','keep_tags',o.keep_tags)}</div>
 <div class="field full">${check('Normalize loudness · optional','normalize',o.normalize,'Single-pass normalization; target values are requested rather than certified measurements.')}</div>
 ${field('Target integrated loudness (LUFS)','loudness_lufs',o.loudness_lufs,{type:'number',min:-30,max:-9,step:.5})}${field('Target true-peak ceiling (dBTP)','true_peak_db',o.true_peak_db,{type:'number',min:-9,max:-1,step:.1})}</div>
 <div class="callout neutral">The source sample rate is detected automatically. Retuning changes audio and assumes your entered source tuning. Lossless output encoding cannot undo losses in the original recording.</div>`;
 if(tool==='metadata'){
  const keys=['title','artist','album','album_artist','comment','description','date','genre','track','disc','composer','copyright','location','creation_time','encoder'];
  return `<div class="fields">${field('Operation','mode',o.mode,{full:true,options:[['wipe','Reduce supported metadata · privacy copy'],['selective','Remove selected media tags only'],['edit','Edit selected media tags']]})}
  <div class="field full"><label>Fields to remove · selective/edit modes</label><div class="checks metadata-checks">${keys.map(k=>`<label class="checkbox"><input type="checkbox" name="remove_tags" value="${k}" ${o.remove_tags.includes(k)?'checked':''}>${k.replaceAll('_',' ')}</label>`).join('')}</div></div>
  <div class="field full"><label>Replacement values · edit mode</label><div class="fields">${['title','artist','album','album_artist','date','genre','composer','copyright'].map(k=>`<div class="field"><label for="meta-${k}">${k.replaceAll('_',' ')}</label><input id="meta-${k}" data-meta-tag="${k}" value="${escape(o.tags[k]||'')}" autocomplete="off"></div>`).join('')}<div class="field full"><label for="meta-comment">Comment</label><textarea id="meta-comment" data-meta-tag="comment">${escape(o.tags.comment||'')}</textarea></div></div></div>
  <div class="field full">${check('Export a PRIVATE before/after report','audit_report',o.audit_report,'The report contains metadata from the original file. Do not publish it with the reduced copy.')}</div></div>
  <div class="callout neutral">Selective removal/editing applies to supported audio/video containers. Images currently use full supported-field reduction only. Neither mode removes visible content, embedded watermarks, or filesystem history.</div><button class="secondary" id="metadata-inspect">Inspect first source · no changes</button>`;
 }
 if(tool.startsWith('download_')){
  const audio=tool==='download_audio';
  return `<div class="fields">${field('Output format','format',o.format,{options:audio?[['mp3','MP3'],['m4a','M4A'],['flac','FLAC'],['wav','WAV']]:[['mp4','MP4'],['mkv','MKV']]})}
  ${audio?field('Audio quality','quality',o.quality,{options:[['best','Best available / VBR'],['320','320 kbps MP3'],['192','192 kbps MP3'],['128','128 kbps MP3']]}):field('Maximum video height','max_height',o.max_height,{options:[[0,'Best available'],[720,'720p'],[1080,'1080p'],[1440,'1440p'],[2160,'2160p / 4K']]})}
  ${field('Filename layout','filename_style',o.filename_style,{full:true,options:[['title_id','Title [video ID]'],['channel_title','Channel - Title [video ID]'],['playlist_index','Playlist index - Title [video ID]']]})}
  <div class="field full">${check('Process a playlist · bounded range','playlist',o.playlist,'One URL is one queue job, including its selected playlist entries. Live broadcasts are excluded.')}</div>
  ${field('First playlist entry','playlist_start',o.playlist_start,{type:'number',min:1,max:10000})}${field('Last playlist entry','playlist_end',o.playlist_end,{type:'number',min:1,max:10000,hint:'At most 500 entries per job.'})}
  <div class="field full">${check('Save available human-written subtitles','subtitles',o.subtitles)}</div><div class="field full">${check('Also save automatic subtitles','auto_subtitles',o.auto_subtitles)}</div>
  ${field('Subtitle languages','subtitle_languages',o.subtitle_languages,{full:true,hint:'Comma-separated language codes, for example en,es. Availability depends on the source.'})}
  <div class="field full">${check('Embed available title/creator metadata','embed_metadata',o.embed_metadata)}</div>
  ${field('Optional Netscape cookies file','cookies',o.cookies,{full:true,placeholder:'C:\\yt-dlp\\youtube_cookies.txt',hint:'Only used for requested downloads. Original cookie files are not rewritten and cookie paths are excluded from saved presets.'})}</div><div class="callout neutral">Use media you have permission to save. Website availability, account requirements and codec/subtitle support can vary. No unsupported live-download guarantee is implied.</div>`;
 }
 return '';
}
function enhanceTool(){
 $('#pick-diarization')?.addEventListener('click',async()=>{try{const r=await nativePick('folder');if(r.paths?.length)$('[name="diarization_model"]').value=r.paths[0];}catch(e){notify(e.message,true);}});
 $('#metadata-inspect')?.addEventListener('click',async()=>{const source=draft().sources[0];if(!source)return notify('Add a file first.',true);try{const r=await api('/metadata/inspect',{source:source.id});$('#dialog-content').innerHTML='<h2>Metadata inspector</h2><p>'+escape(r.note)+'</p><pre>'+escape(JSON.stringify(r.metadata,null,2))+'</pre>';$('#dialog').showModal();}catch(e){notify(e.message,true);}});
 if(view==='metadata'){
  const update=()=>{const mode=$('[name="mode"]').value;$$('[name="remove_tags"]').forEach(e=>e.disabled=mode==='wipe');$$('[data-meta-tag]').forEach(e=>e.disabled=mode!=='edit');};
  $('[name="mode"]').addEventListener('change',update);update();
 }
 renderFavorites();
}

function captureWorkflow(){
 const d=draft('workflows');
 if($('#workflow-name'))workflowName=$('#workflow-name').value;
 if($('#out-mode'))d.output.mode=$('#out-mode').value;
 if($('#out-path'))d.output.directory=$('#out-path').value;
 if($('#out-subfolder'))d.output.subfolder=$('#out-subfolder').value;
 $$('[data-step-tool]').forEach(e=>workflowSteps[Number(e.dataset.stepTool)].tool=e.value);
 $$('[data-step-preset]').forEach(e=>workflowSteps[Number(e.dataset.stepPreset)].preset=e.value);
}
function workflowSpec(){
 captureWorkflow();return {name:workflowName.trim()||'Production preparation',steps:workflowSteps.map(s=>({tool:s.tool,options:structuredClone((s.preset==='__snapshot__'?s.options:state.presets.find(p=>p.tool===s.tool&&p.name===s.preset)?.options)||defaults[s.tool])})),output:draft('workflows').output};
}
function renderWorkflow(){
 const saved=state.workflows||[];
 $('#main').innerHTML=heading('WORKSPACE / AUTOMATION','Production workflows','Chain verified outputs into the next operation. Originals and completed intermediate files are kept.',`<select id="load-workflow" class="preset-select" aria-label="Load workflow"><option value="">Load workflow…</option>${saved.map(w=>`<option>${escape(w.name)}</option>`).join('')}</select><button class="secondary" id="save-workflow">Save workflow</button>`)+
 `<div class="studio-layout"><div class="column">${filesPanel()}${outputPanel()}</div><div class="column">${panel('Workflow steps',`<div class="field"><label for="workflow-name">Workflow name</label><input id="workflow-name" value="${escape(workflowName)}"></div><div id="workflow-steps">${workflowSteps.map((s,i)=>`<section class="workflow-step"><div class="step-number">${i+1}</div><div class="step-fields"><label>Operation</label><select data-step-tool="${i}">${['retune','metadata','transcribe'].map(t=>`<option value="${t}" ${s.tool===t?'selected':''}>${titles[t]}</option>`).join('')}</select><label>Configuration</label><select data-step-preset="${i}"><option value="">Built-in defaults</option>${s.options?`<option value="__snapshot__" ${s.preset==='__snapshot__'?'selected':''}>Saved workflow settings</option>`:''}${state.presets.filter(p=>p.tool===s.tool).map(p=>`<option ${p.name===s.preset?'selected':''}>${escape(p.name)}</option>`).join('')}</select><small>Save a preset on the tool’s page for custom settings.</small></div><div class="step-controls"><button class="secondary" data-step-up="${i}" title="Move up" ${i===0?'disabled':''}>↑</button><button class="secondary" data-step-remove="${i}" title="Remove step" ${workflowSteps.length===1?'disabled':''}>×</button></div></section>`).join('')}</div><button class="secondary" id="add-step" ${workflowSteps.length>=6?'disabled':''}>Add step</button><div class="callout neutral">Transcription must be last. Metadata steps currently use full supported-field reduction. If an earlier step fails, dependent steps are blocked rather than run on the wrong file.</div>`,'DEPENDENCY-AWARE')}${panel('How files move through the workflow','<p class="note">Each source creates its own chain in the universal queue. The next step receives the actual published media output, including collision-safe renamed files. Your chosen destination is used throughout the chain.</p><p class="note">Retry workflow creates a fresh chain; it does not silently replace completed outputs. Download-to-workflow chaining and visual branching are not included in this release.</p>')}</div></div>`;
 bindFiles();fileList();updateOutputPreview();
 $('#out-mode').onchange=()=>{captureWorkflow();$('#out-path-wrap').classList.toggle('hidden',draft('workflows').output.mode!=='custom');updateOutputPreview();};
 $('#out-path').oninput=$('#out-subfolder').oninput=()=>{captureWorkflow();updateOutputPreview();};
 $('#choose-output').onclick=async()=>{try{const r=await nativePick('folder');if(r.paths?.length){$('#out-path').value=r.paths[0];captureWorkflow();updateOutputPreview();}}catch(e){notify(e.message,true);}};
 $$('[data-step-tool]').forEach(e=>e.onchange=()=>{captureWorkflow();workflowSteps[Number(e.dataset.stepTool)].preset='';delete workflowSteps[Number(e.dataset.stepTool)].options;renderWorkflow();});
 $$('[data-step-up]').forEach(e=>e.onclick=()=>{captureWorkflow();const i=Number(e.dataset.stepUp);[workflowSteps[i-1],workflowSteps[i]]=[workflowSteps[i],workflowSteps[i-1]];renderWorkflow();});
 $$('[data-step-remove]').forEach(e=>e.onclick=()=>{captureWorkflow();workflowSteps.splice(Number(e.dataset.stepRemove),1);renderWorkflow();});
 $('#add-step').onclick=()=>{captureWorkflow();workflowSteps.push({tool:'transcribe',preset:''});renderWorkflow();};
 $('#save-workflow').onclick=async()=>{try{await api('/workflows/save',workflowSpec());await refresh(false);notify('Workflow saved locally.');renderWorkflow();}catch(e){notify(e.message,true);}};
 $('#load-workflow').onchange=e=>{const w=saved.find(w=>w.name===e.target.value);if(!w)return;workflowName=w.name;draft('workflows').output=structuredClone(w.output);workflowSteps=w.steps.map(step=>({tool:step.tool,preset:'__snapshot__',options:structuredClone(step.options)}));renderWorkflow();};
 $('#enqueue').textContent='Queue workflow';$('#enqueue').onclick=async()=>{const b=$('#enqueue');b.disabled=true;try{const r=await api('/workflows/run',{...workflowSpec(),sources:draft('workflows').sources.map(s=>s.id)});draft('workflows').sources=[];await refresh(false);navigate('queue');notify(`${r.jobs.length} dependent jobs queued.`);}catch(e){notify(e.message,true);}finally{if(b.isConnected)b.disabled=false;}};
}

function etaText(seconds){if(seconds==null)return '';if(seconds<60)return Math.ceil(seconds)+'s remaining';return Math.ceil(seconds/60)+'m remaining';}
jobRow=function(j){
 const running=['running','finalizing','cancelling'].includes(j.status);
 return `<div class="job-row"><input type="checkbox" class="job-select" data-select-job="${j.id}" ${selectedJobs.has(j.id)?'checked':''} aria-label="Select ${escape(j.label)}"><div class="tool-icon">${icon(j.tool)}</div><div class="job-main"><strong title="${escape(j.label)}">${escape(j.label)}</strong><small>${escape(titles[j.tool]||j.tool)}${j.workflow_name?` · step ${j.step}/${j.steps}`:''} · ${when(j.created)}</small>${running?`<div class="progress"><i style="width:${Math.min(100,Math.max(0,j.progress))}%"></i></div><small>${escape(j.stage||j.message)}${j.speed?' · '+escape(j.speed):''}${j.eta_seconds!=null?' · '+etaText(j.eta_seconds):''}</small>`:''}${j.status==='queued'&&j.depends_on?'<small>Waits for the previous workflow step</small>':''}</div><div class="job-state ${j.status}">${running?`${Math.round(j.progress)}% · `:''}${escape(j.status)}</div><div class="job-actions">${j.status==='queued'?`<button class="secondary small-button" data-order="${j.id}" data-direction="up" aria-label="Move job up">↑</button><button class="secondary small-button" data-order="${j.id}" data-direction="down" aria-label="Move job down">↓</button>`:''}<button class="secondary" data-job="${j.id}">Details</button>${['queued','running'].includes(j.status)?`<button class="secondary" data-cancel="${j.id}">Cancel</button>`:['failed','cancelled','interrupted','blocked'].includes(j.status)?`<button class="secondary" data-retry="${j.id}">Retry</button>`:''}</div></div>`;
};
renderQueue=function(){
 $('#main').innerHTML=heading('WORKSPACE / ACTIVITY','Universal processing queue','Dependency-aware jobs, shared resource limits and measured progress.',`<button class="secondary" id="pause-queue">${icon(state.settings.queue_paused?'play':'pause')}${state.settings.queue_paused?'Resume queue':'Pause dispatch'}</button>`)+
 `<div class="callout neutral" style="margin-bottom:20px">Pause affects new dispatch only. Retry restarts a job. Workflow dependencies take precedence over queue order. Time estimates appear only when the engine reports measurable progress.</div><div class="queue-toolbar">${[['all','All jobs'],['active','Active'],['completed','Completed'],['attention','Needs attention']].map(([k,l])=>`<button class="filter ${filter===k?'active':''}" data-filter="${k}">${l}</button>`).join('')}<span class="queue-limits">${state.settings.workers} total · ${state.settings.cpu_workers} CPU · ${state.settings.gpu_workers} GPU · ${state.settings.download_workers} downloads</span></div><div class="batch-actions"><button class="secondary" id="select-visible">Select visible</button><button class="secondary" id="clear-selection">Clear selection</button><button class="secondary" id="cancel-selected">Cancel selected</button><button class="secondary" id="retry-selected">Retry selected</button><span id="selection-count"></span></div><section class="panel" id="queue-list"></section><p class="note">All active jobs plus the latest ${state.history_limit} records. Closing this browser does not stop processing. Desktop notifications need an open authorized tab.</p>`;
 $('#pause-queue').onclick=async()=>{try{await api('/queue',{paused:!state.settings.queue_paused});await refresh(false);renderQueue();}catch(e){notify(e.message,true);}};
 $$('[data-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.filter;renderQueue();});
 $('#select-visible').onclick=()=>{$$('[data-select-job]').forEach(b=>selectedJobs.add(b.dataset.selectJob));updateQueueList();};
 $('#clear-selection').onclick=()=>{selectedJobs.clear();updateQueueList();};
 for(const action of ['cancel','retry'])$('#'+action+'-selected').onclick=async()=>{if(!selectedJobs.size)return notify('Select jobs first.');if(!confirm(`${action==='cancel'?'Cancel':'Retry'} ${selectedJobs.size} selected jobs? Workflow chains should normally be retried with their group button.`))return;try{const r=await api('/jobs/batch-action',{action,ids:[...selectedJobs]});const errors=r.results.filter(r=>!r.ok);notify(`${r.results.length-errors.length} operations accepted.${errors.length?' '+errors[0].error:''}`,!!errors.length);selectedJobs.clear();await refresh();}catch(e){notify(e.message,true);}};
 updateQueueList();
};
const baseBindJobActions=bindJobActions;
bindJobActions=function(){
 baseBindJobActions();
 $$('[data-select-job]').forEach(e=>e.onchange=()=>{if(e.checked)selectedJobs.add(e.dataset.selectJob);else selectedJobs.delete(e.dataset.selectJob);if($('#selection-count'))$('#selection-count').textContent=selectedJobs.size+' selected';});
 $$('[data-order]').forEach(b=>b.onclick=async()=>{try{await api(`/jobs/${b.dataset.order}/order`,{direction:b.dataset.direction});await refresh();}catch(e){notify(e.message,true);}});
 $$('[data-batch-cancel]').forEach(b=>b.onclick=async()=>{if(!confirm('Cancel all queued/running steps in this group?'))return;try{await api(`/batches/${b.dataset.batchCancel}/cancel`,{});await refresh();}catch(e){notify(e.message,true);}});
 $$('[data-batch-retry]').forEach(b=>b.onclick=async()=>{if(!confirm('Create a new copy of this entire workflow? Existing outputs remain unchanged.'))return;try{await api(`/batches/${b.dataset.batchRetry}/retry`,{});notify('New workflow chain queued.');await refresh();}catch(e){notify(e.message,true);}});
};
updateQueueList=function(){
 if(!$('#queue-list'))return;
 let jobs=state.jobs.filter(j=>filter==='all'||filter==='active'&&['queued','running','cancelling','finalizing'].includes(j.status)||filter==='completed'&&j.status==='completed'||filter==='attention'&&['failed','interrupted','cancelled','blocked'].includes(j.status));
 const queued=jobs.filter(j=>j.status==='queued').sort((a,b)=>(a.rank??a.created)-(b.rank??b.created));
 jobs=[...jobs.filter(j=>['running','finalizing','cancelling'].includes(j.status)),...queued,...jobs.filter(j=>!['queued','running','finalizing','cancelling'].includes(j.status))];
 const groups=new Set();
 $('#queue-list').innerHTML=jobs.length?jobs.map(j=>{let head='';if(j.workflow_name&&!groups.has(j.batch_id)){groups.add(j.batch_id);head=`<div class="batch-strip"><strong>${escape(j.workflow_name)}</strong><span>Workflow group</span><button class="text-button" data-batch-cancel="${j.batch_id}">Cancel group</button><button class="text-button" data-batch-retry="${j.batch_id}">Retry workflow</button></div>`;}return head+jobRow(j);}).join(''):emptyQueue();
 if($('#selection-count'))$('#selection-count').textContent=selectedJobs.size+' selected';bindJobActions();
};
const baseJobDetail=jobDetail;
jobDetail=async function(id){
 await baseJobDetail(id);
 const j=state.jobs.find(j=>j.id===id);if(!j)return;
 const transcript=j.outputs.findIndex(o=>o.name.endsWith('.json')&&!o.name.includes('PRIVATE')&&!o.name.includes('_raw.'));
 if(j.tool==='transcribe'&&transcript>=0){const b=document.createElement('button');b.className='primary';b.id='review-transcript';b.style.marginTop='16px';b.textContent='Review transcript with media';b.onclick=()=>reviewTranscript(j,transcript);$('#dialog-content').append(b);}
 const report=j.outputs.findIndex(o=>o.name.endsWith('_PRIVATE-audit.json'));
 if(report>=0){const b=document.createElement('button');b.className='secondary';b.style.marginTop='16px';b.textContent='Review PRIVATE before/after report';b.onclick=async()=>{try{const d=await api(`/jobs/${id}/outputs/${report}`);$('#dialog-content').innerHTML='<h2>PRIVATE metadata report</h2><p>'+escape(d.warning)+'</p><div class="audit-grid"><div><h3>Before</h3><pre>'+escape(JSON.stringify(d.before,null,2))+'</pre></div><div><h3>After</h3><pre>'+escape(JSON.stringify(d.after,null,2))+'</pre></div></div>';}catch(e){notify(e.message,true);}};$('#dialog-content').append(b);}
};
async function reviewTranscript(j,index){
 try{
  const doc=await api(`/jobs/${j.id}/outputs/${index}`);
  $('#dialog-content').innerHTML=`<h2>${escape(j.label)}</h2><p>Click a timestamp to seek. Source media stays local. Unsupported browser codecs may require a converted preview or the standalone HTML viewer.</p><video class="transcript-player" controls preload="metadata" src="/api/jobs/${j.id}/media"></video><input id="transcript-search" placeholder="Search the transcript" aria-label="Search transcript"><div id="transcript-rows" class="transcript-rows"></div><p class="note">Timing and speaker labels are estimates. Review flags highlight possible recognition problems, not verified errors.</p>`;
  const renderRows=()=>{const q=$('#transcript-search').value.toLowerCase();const rows=(doc.segments||[]).filter(s=>(s.text+' '+(s.speaker||'')).toLowerCase().includes(q)).slice(0,3000);$('#transcript-rows').innerHTML=rows.map(s=>`<button class="transcript-row" data-seek="${Number(s.start)}"><span>${new Date(Math.max(0,Number(s.start))*1000).toISOString().slice(11,23)}</span><span>${s.speaker?`<b>${escape(s.speaker)}</b> `:''}${escape(s.text)}${s.review_flags?.length?`<small>${escape(s.review_flags.join(' · '))}</small>`:''}</span></button>`).join('');$$('[data-seek]').forEach(b=>b.onclick=()=>{const v=$('.transcript-player');v.currentTime=Number(b.dataset.seek);v.play().catch(()=>{});});};
  $('#transcript-search').oninput=renderRows;renderRows();
 }catch(e){notify(e.message,true);}
}

const baseRenderSettings=renderSettings;
renderSettings=function(){
 baseRenderSettings();
 const fields=$('#settings-fields');
 fields.insertAdjacentHTML('beforeend',`<div class="field full"><div class="separator"></div></div>${field('CPU job limit','cpu_workers',state.settings.cpu_workers,{type:'number',min:1,max:8})}${field('GPU / Auto transcription limit','gpu_workers',state.settings.gpu_workers,{type:'number',min:1,max:2,hint:'Auto conservatively reserves a GPU slot even when it falls back to CPU.'})}${field('Download job limit','download_workers',state.settings.download_workers,{type:'number',min:1,max:4})}<div class="field full">${check('Desktop completion/error notifications','desktop_notifications',state.settings.desktop_notifications,'Requires permission and an open FORGE tab. No background Windows notification service is installed.')}</div><button class="text-button" id="notification-permission">Request notification permission</button>`);
 $('#notification-permission').onclick=async()=>{if(!('Notification'in window))return notify('This browser does not expose desktop notifications.',true);try{notify('Notification permission: '+await Notification.requestPermission());}catch(e){notify(e.message,true);}};
 $('#save-settings').onclick=async()=>{const values={...state.settings};for(const k of ['workers','cpu_workers','gpu_workers','download_workers','ffmpeg','ffprobe','default_output','daily_update_check','desktop_notifications']){const el=$(`[name="${k}"]`);values[k]=el.type==='checkbox'?el.checked:['workers','cpu_workers','gpu_workers','download_workers'].includes(k)?Number(el.value):el.value;}try{await api('/settings',values);await refresh(false);renderSettings();notify('Settings saved. Resource limits apply at dispatch.');}catch(e){notify(e.message,true);}};
 const columns=$$('.settings-grid > .column');
 columns[0].insertAdjacentHTML('beforeend',panel('GPU diagnostics',`<button class="secondary" id="gpu-diagnostics">Run read-only diagnostics</button><div id="gpu-results">${diagnosticsHTML(state.diagnostics)}</div><p class="note">No driver, CUDA runtime or environment changes are made by this check. Successful recognition on a short clip is the acceptance test.</p>`)+panel('Model cache',`<button class="secondary" id="model-scan">Inspect local model cache</button><div id="model-results"></div><div class="separator"></div><div class="field"><label for="download-model">Download an approved speech model</label><select id="download-model">${['tiny','base','small','medium','large-v3','large-v3-turbo'].map(m=>`<option>${m}</option>`).join('')}</select></div><button class="secondary" id="queue-model-download" style="margin-top:12px">Queue model download</button><p class="note">Explicit confirmation is required. Downloads use the universal queue; models are not replaced on ordinary startup.</p>`));
 columns[1].insertAdjacentHTML('beforeend',panel('Guided optional setup',`<p class="note" style="margin-top:0">The queue must be idle. FORGE stops, then opens the selected installer in a Windows console for your confirmation. Your other Python environments are not modified.</p><div class="actions">${[['transcription','Transcription'],['downloads','Downloads'],['speakers','Speaker labels']].map(([id,name])=>`<button class="secondary" data-install-engine="${id}">${name}</button>`).join('')}</div><button class="text-button" id="reveal-forge" style="margin-top:15px">Open application folder</button>`)+panel('Local build updates',`<p class="note" style="margin-top:0">Save the new FORGE ZIP in <b>B:\\Forge\\Build</b>, then run <b>Update FORGE.bat</b> in your main folder. The updater verifies the package, checks that jobs are idle, backs up the code and database, and replaces application files only.</p><p class="note">The first upgrade from V0.1 requires placing the supplied updater BAT in your main folder once. Future builds use the same action. It is not an unattended internet updater.</p>`)+panel('Remembered browser',`<p class="note" style="margin-top:0">After a launcher authorization, bookmarks and new normal tabs work in this browser profile for up to 90 days. Clearing site data, a private window or another browser requires authorization again.</p><button class="secondary" id="forget-browser">Forget this browser authorization</button><p class="note">A bookmark opens the interface only while the local service is running. Use the launcher after a Windows restart.</p>`));
 $('#gpu-diagnostics').onclick=async()=>{const b=$('#gpu-diagnostics');b.disabled=true;b.textContent='Reading runtime capabilities…';try{state.diagnostics=await api('/environment/diagnostics',{});if($('#gpu-results'))$('#gpu-results').innerHTML=diagnosticsHTML(state.diagnostics);}catch(e){notify(e.message,true);}finally{if(b.isConnected){b.disabled=false;b.textContent='Run read-only diagnostics';}}};
 $('#model-scan').onclick=async()=>{try{const r=await api('/environment/models');if($('#model-results'))$('#model-results').innerHTML=r.models.map(m=>`<div class="dependency"><div><strong>${escape(m.model)}</strong><small>${m.bytes?bytes(m.bytes):''}</small></div><span class="dep-status ${m.cached?'':'missing'}">${m.cached?'CACHED':'NOT CACHED'}</span></div>`).join('')+'<p class="note">Cache presence only; not an inference test.</p>';}catch(e){notify(e.message,true);}};
 $('#queue-model-download').onclick=async()=>{const model=$('#download-model').value;if(!confirm(`Download ${model} model files from Hugging Face? This uses internet and disk space. Audio is not uploaded.`))return;try{await api('/environment/models/download',{model,consent:true});await refresh(false);navigate('queue');}catch(e){notify(e.message,true);}};
 $$('[data-install-engine]').forEach(b=>b.onclick=async()=>{if(!confirm('Stop the idle FORGE service and open this optional package installer? The installer requests confirmation before downloading packages.'))return;try{const r=await api('/environment/install',{engine:b.dataset.installEngine});notify(r.message);if(r.started)lastError=true;}catch(e){notify(e.message,true);}});
 $('#reveal-forge').onclick=async()=>{try{const r=await api('/environment/reveal',{});if(!r.opened)notify(r.path);}catch(e){notify(e.message,true);}};
 $('#forget-browser').onclick=async()=>{if(!confirm('Forget this browser profile? Open the launcher again to reauthorize it.'))return;try{await api('/session/forget',{});location.reload();}catch(e){notify(e.message,true);}};
};
function diagnosticsHTML(d){if(!d)return '<p class="note">No diagnostics have been run.</p>';return `<p class="note">Read at ${when(d.checked)}</p><pre>${escape(d.gpu||'GPU details not available')}\nCUDA devices visible to CTranslate2: ${Number(d.cuda_devices)||0}\nCompute types: ${escape((d.compute_types||[]).join(', ')||'unavailable')}</pre>${(d.notes||[]).map(n=>`<p class="note">${escape(n)}</p>`).join('')}`;}

function renderFavorites(){
 let nav=$('#favorites-nav');if(!nav){nav=document.createElement('nav');nav.id='favorites-nav';nav.setAttribute('aria-label','Favorite tools');$('#workspace-nav').after(nav);}
 const favorites=state?.favorites||[];
 nav.innerHTML=favorites.length?'<div class="nav-caption">FAVORITES</div>'+favorites.map(t=>`<button class="nav-item" data-favorite-nav="${t}">${icon(t)}<span class="nav-text">${escape(titles[t])}</span></button>`).join(''):'';
 $$('[data-favorite-nav]').forEach(b=>b.onclick=()=>navigate(b.dataset.favoriteNav));
 if(defaults[view]&&!$('#favorite-tool')){
  let actions=$('.heading-actions');if(!actions){actions=document.createElement('div');actions.className='heading-actions';$('.page-heading')?.append(actions);}
  const b=document.createElement('button');b.id='favorite-tool';b.className='secondary';b.textContent=favorites.includes(view)?'Unpin tool':'Pin tool';
  b.onclick=async()=>{try{capture();const chosen=view;await api('/favorites',{tool:chosen,pinned:!(state.favorites||[]).includes(chosen)});await refresh(false);renderNav();render();}catch(e){notify(e.message,true);}};actions.prepend(b);
 }
}
const renderNavBase=renderNav;
renderNav=function(){renderNavBase();if(state)renderFavorites();};
const renderBase=render;
render=function(){renderBase();if(state)renderFavorites();};

boot();
