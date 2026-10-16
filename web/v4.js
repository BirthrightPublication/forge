'use strict';
// Shared Studio presentation. Existing form nodes and capture IDs stay intact.
Object.assign(paths,{
 script:'<path d="M4 4v4c0 4 8 4 8 8v4M20 4v4c0 4-8 4-8 8"/><circle cx="4" cy="3" r="2"/><circle cx="20" cy="3" r="2"/><circle cx="12" cy="21" r="2"/>',
 language:'<path d="M3 5h12M9 3v2M6 5c0 5 4 9 9 11M13 5c0 5-4 9-9 11M14 21l4-11 4 11M16 17h4"/>',
 accuracy:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><path d="M12 10v4M10 12h4"/>',
 speakers:'<circle cx="9" cy="7" r="3"/><path d="M3 21v-4a6 6 0 0112 0v4M16 4a3 3 0 010 6M18 13a5 5 0 013 4v4"/>',
 format:'<path d="M4 5h16M4 10h11M4 15h16M4 20h11"/>',
 export:'<path d="M12 3v12m-4-4l4 4 4-4M4 17v4h16v-4"/>',
 info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
 select:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M7 12l3 3 7-7"/>',
 clear:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M8 8l8 8M16 8l-8 8"/>',
 cancel:'<circle cx="12" cy="12" r="9"/><path d="M8 8l8 8M16 8l-8 8"/>',
 trash:'<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"/>',
 up:'<path d="M12 20V4m-6 6l6-6 6 6"/>',down:'<path d="M12 4v16m-6-6l6 6 6-6"/>',
 first:'<path d="M4 3h16M12 21V7m-6 6l6-6 6 6"/>',last:'<path d="M4 21h16M12 3v14m-6-6l6 6 6-6"/>',
 more:'<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
 open:'<path d="M14 3h7v7M21 3L10 14M10 3H4v17h17v-7"/>',
 pin:'<path d="M8 3h8l-1 7 4 4H5l4-4zM12 14v8"/>'
});
titles.workflows='Black Scripting';titles.metadata='Metadata Hyper Cleaner';titles.qr='QR Studio';
filter='active';
const v4ActiveStatuses=['running','finalizing','cancelling'],v4FailureStatuses=['failed','blocked','interrupted'];
const v4TabState=new Map();
let v4TabSequence=0;
function v4SafeLabel(value){return String(value??'').replace(/\bworkflows\b/gi,'Black Scripting scripts').replace(/\bworkflow\b/gi,'Black Scripting');}
function v4IconButton(name,label,attrs=''){
 return `<button type="button" class="v4-icon" aria-label="${escape(label)}" title="${escape(label)}" data-tooltip="${escape(label)}" ${attrs}>${icon(name)}</button>`;
}
function v4MountTabs(container,sections,{key='settings',ariaLabel='Tool settings'}={}){
 if(!container||!sections.length)return null;
 const uid='v4-tabs-'+(++v4TabSequence),toolbar=document.createElement('div');
 toolbar.className='v4-tabs';toolbar.setAttribute('role','tablist');toolbar.setAttribute('aria-label',ariaLabel);
 const content=document.createElement('div');content.className='v4-tab-content';
 const remembered=v4TabState.get(key),initial=sections.some(s=>s.id===remembered)?remembered:sections[0].id;
 const activate=(id,focus=false)=>{
  sections.forEach(s=>{const selected=s.id===id;s.element.hidden=!selected;s.button.setAttribute('aria-selected',String(selected));s.button.tabIndex=selected?0:-1;if(selected&&focus)s.button.focus();});
  v4TabState.set(key,id);return id;
 };
 sections.forEach((section,index)=>{
  const button=document.createElement('button');button.type='button';button.className='v4-tab v4-icon';button.id=uid+'-tab-'+index;
  button.setAttribute('role','tab');button.setAttribute('aria-controls',uid+'-panel-'+index);button.setAttribute('aria-label',section.label);button.title=section.label;button.dataset.tooltip=section.label;button.innerHTML=icon(section.icon||'settings');
  section.element.id=uid+'-panel-'+index;section.element.classList.add('v4-tab-panel');section.element.setAttribute('role','tabpanel');section.element.setAttribute('aria-labelledby',button.id);section.element.tabIndex=0;
  const label=document.createElement('div');label.className='v4-section-label';label.textContent=section.label;section.element.prepend(label);
  section.button=button;button.onclick=()=>activate(section.id);
  button.onkeydown=e=>{let next=index;if(e.key==='ArrowRight')next=(index+1)%sections.length;else if(e.key==='ArrowLeft')next=(index+sections.length-1)%sections.length;else if(e.key==='Home')next=0;else if(e.key==='End')next=sections.length-1;else return;e.preventDefault();activate(sections[next].id,true);};
  toolbar.append(button);content.append(section.element);
 });
 container.replaceChildren(toolbar,content);activate(initial);
 return {activate,get active(){return v4TabState.get(key);}};
}
const v4ToolGroups={
 transcribe:[['model','Recognition model','cpu',['model','device','allow_model_download']],['language','Language and vocabulary','language',['language','task','vocabulary']],['accuracy','Recognition accuracy','accuracy',['beam_size','vad','flag_uncertain']],['timestamps','Timestamps and subtitles','clock',['timestamps','subtitle_chars','subtitle_lines','subtitle_seconds']],['speakers','Speaker labels','speakers',['diarization','diarization_model','min_speakers','max_speakers']],['formatting','Transcript formatting','format',['style','remove_fillers']],['export','Export formats','export',['formats']]],
 retune:[['tuning','Pitch and tempo','retune',['source_hz','target_hz','preserve_tempo']],['format','Format and quality','export',['format','quality','mp3_bitrate','wav_bits','keep_tags']],['loudness','Loudness normalization','settings',['normalize','loudness_lufs','true_peak_db']]],
 metadata:[['operation','Cleaning mode and inspection','metadata',['mode','cleaning_level','profile','max_removal']],['remove','Remove selected metadata','clear',['remove_tags']],['edit','Edit metadata','format',['tags']],['verification','Verification and private reports','check',['audit_report','remaining_report','verify','inspect_previews','preview_report']]],
 qr:[['content','QR content','qr',['kind','text','content_type','wifi_ssid','wifi_password','wifi_security','wifi_hidden','contact_name','contact_phone','contact_email','contact_org','contact_organization','contact_url','contact_address','contact_note','bulk_text','bulk']],['appearance','Size and colors','settings',['box_size','border','fill_color','back_color','foreground','background']],['correction','Error correction','accuracy',['error_correction']],['export','Output format','export',['name','format']]],
 download_audio:[['quality','Format and audio quality','retune',['format','quality']],['playlist','Playlist range','queue',['playlist','playlist_start','playlist_end']],['subtitles','Subtitles','format',['subtitles','auto_subtitles','subtitle_languages']],['advanced','Naming and authorization','settings',['filename_style','embed_metadata','cookies']]],
 download_video:[['quality','Format and resolution','download_video',['format','max_height']],['playlist','Playlist range','queue',['playlist','playlist_start','playlist_end']],['subtitles','Subtitles','format',['subtitles','auto_subtitles','subtitle_languages']],['advanced','Naming and authorization','settings',['filename_style','embed_metadata','cookies']]],
 password:[['policy','Length and character policy','password',['length','count','upper','numbers','symbols']],['advanced','Character exclusions','settings',['exclude_ambiguous','unicode_chars']]]
};
function v4CompactOptions(root,tool,{key=tool}={}){
 if(!root||root.dataset.compacted)return;root.dataset.compacted='true';
 const definitions=v4ToolGroups[tool]||[['settings','Configuration','settings',[]]];
 const groups=definitions.map(([id,label,ic,names])=>({id,label,icon:ic,names,element:document.createElement('section'),fields:document.createElement('div')}));
 groups.forEach(g=>{g.fields.className='fields';g.element.append(g.fields);});
 const groupFor=names=>groups.find(g=>names.some(n=>g.names.includes(n)))||groups[0];
 // Move whole top-level fields, retaining nested metadata editors and every input.
 const fields=$$('.field',root).filter(f=>!f.parentElement.closest('.field')&&!f.closest('.qr-bulk'));
 fields.forEach(f=>{const names=$$('[name]',f).map(n=>n.name);if($('[data-meta-tag]',f))names.push('tags');groupFor(names).fields.append(f);});
 const extras=[...root.children];
 const auxiliary=document.createElement('section');auxiliary.className='v4-context';
 extras.forEach(node=>{
  if(node.matches('.fields,.separator'))return;
  if(node.matches('.qr-bulk')){const bulk=document.createElement('section');bulk.append(node);groups.push({id:'bulk',label:'Bulk QR export',icon:'queue',names:[],element:bulk,fields:document.createElement('div')});}
  else if(node.matches('details')){
   const target=groups.find(g=>g.id==='speakers')||groups.at(-1);$$('button',node).forEach(b=>target.element.append(b));$$('p.note,.callout',node).forEach(p=>target.element.append(p));
  }else if(node.id==='metadata-inspect'){groups[0].element.append(node);}
  else if(node.textContent.trim()||node.querySelector('input,button'))auxiliary.append(node);
 });
 // Explanatory hints remain available on keyboard focus. Safety and privacy notes stay visible.
 groups.forEach(g=>$$('.hint',g.element).forEach(h=>{
  if(/private|original|staging|history|internet|download|cookie|overwrite|secret/i.test(h.textContent))return;
  const b=document.createElement('button');b.type='button';b.className='v4-help v4-icon';b.setAttribute('aria-label',h.textContent);b.title=h.textContent;b.dataset.tooltip=h.textContent;b.innerHTML=icon('info');h.replaceWith(b);
 }));
 const nonempty=groups.filter(g=>g.fields.children.length||g.element.children.length>1||g.id==='bulk');
 if(auxiliary.children.length){const advanced=document.createElement('section');advanced.append(auxiliary);nonempty.push({id:'notes',label:'Capabilities and processing notes',icon:'info',element:advanced});}
 v4MountTabs(root,nonempty,{key,ariaLabel:(titles[tool]||tool)+' settings'});
}
function v4CompactTool(){
 const layout=$('.studio-layout'),options=$('#tool-options');if(!layout||!options||layout.classList.contains('studio-v4'))return;
 const output=$('#out-mode')?.closest('.panel'),configuration=options.closest('.panel'),source=layout.querySelector('.column > .panel');
 if(!output||!configuration||!source)return;
 layout.classList.add('studio-v4');source.classList.add('v4-source');output.classList.add('v4-output');configuration.classList.add('v4-configuration');
 // QR uses its actual content input as the focal input, while retaining it under capture().
 if(view==='qr'){
  const sourceBody=source.querySelector('.panel-body'),sourceFields=document.createElement('div');sourceFields.className='fields';
  const names=v4ToolGroups.qr[0][3];
  $$('.field',options).filter(f=>!f.parentElement.closest('.field')&&!f.closest('.qr-bulk')).forEach(f=>{if($$('[name]',f).some(el=>names.includes(el.name))||f.textContent.includes('Wi-Fi codes contain'))sourceFields.append(f);});
  sourceBody.replaceChildren(sourceFields);source.querySelector('.panel-header h2').textContent='QR content';
  $('[name="text"]',sourceBody)?.closest('.field').classList.add('v4-primary-text');
  const preview=$('#qr-local-preview',options)?.closest('.inline'),area=$('#qr-local-preview-area',options);if(preview)sourceBody.append(preview);if(area)sourceBody.append(area);const disclosure=document.createElement('p');disclosure.className='note v4-qr-privacy';disclosure.textContent='Queued content and presets are stored locally. Wi-Fi uses private preview only.';sourceBody.append(disclosure);
 }
 layout.replaceChildren(source,configuration,output);
 v4CompactOptions(options,view);
 const title=$('.page-heading h1');if(title&&!title.querySelector('svg'))title.insertAdjacentHTML('afterbegin',icon(view));
 $('#favorite-tool')?.classList.add('v4-quick-action');
 const pin=$('#favorite-tool');if(pin){const label=pin.textContent;pin.setAttribute('aria-label',label);pin.title=label;pin.dataset.tooltip=label;pin.innerHTML=icon('pin');}
 const save=$('#save-preset');if(save){save.setAttribute('aria-label','Save tool preset');save.title='Save tool preset';save.dataset.tooltip='Save tool preset';save.classList.add('v4-quick-action');save.innerHTML=icon('export');}
 if(!$('#presets')&&view==='qr'){
  $('.heading-actions')?.insertAdjacentHTML('afterbegin',`<select class="preset-select" id="presets" aria-label="Load QR preset"><option value="">Load a preset…</option>${state.presets.filter(p=>p.tool===view).map(p=>`<option value="${escape(p.name)}">${escape(p.name)}</option>`).join('')}</select>${v4IconButton('export','Save QR preset','id="save-preset"')}`);
  $('#save-preset').onclick=savePreset;$('#presets').onchange=e=>{const p=state.presets.find(p=>p.tool===view&&p.name===e.target.value);if(p){draft().options={...structuredClone(defaults[view]),...structuredClone(p.options)};draft().output=structuredClone(p.output);renderTool();}};
 }
 const add=$('#enqueue');add.classList.replace('primary','secondary');add.onclick=()=>v4Submit(false);
 const start=$('#enqueue-top');if(start){start.innerHTML=icon('play')+'Start';start.onclick=()=>v4Submit(true);start.title='Add this job and resume dispatch';}
 const bottom=document.createElement('button');bottom.id='start-tool';bottom.className='primary';bottom.innerHTML=icon('play')+'Start';bottom.title='Add this job and resume dispatch';bottom.onclick=()=>v4Submit(true);add.after(bottom);
 if(view==='qr'&&draft().options.kind==='wifi'){for(const control of [add,start,bottom].filter(Boolean)){control.innerHTML=icon('qr')+'Preview locally';control.onclick=qrPreviewLocal;control.title='Preview Wi-Fi credentials in memory';}$('#save-preset')?.setAttribute('disabled','');}
 const drop=$('#dropzone');if(drop)drop.onkeydown=e=>{if((e.key==='Enter'||e.key===' ')&&e.target===drop){e.preventDefault();$('#native-browse').click();}};
}
async function v4Submit(start){
 capture();const tool=view,d=draft();if(tool==='qr'&&(d.options.kind==='wifi'||/^\s*WIFI:/i.test(d.options.text)))return qrPreviewLocal();const controls=[$('#enqueue'),$('#enqueue-top'),$('#start-tool')].filter(Boolean);controls.forEach(b=>b.disabled=true);
 try{
  const r=await api('/jobs',{tool,sources:d.sources.map(s=>s.id),urls:d.urls.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),options:d.options,output:d.output});
  if(start&&state.settings.queue_paused)await api('/queue',{paused:false});
  if(mediaViews.includes(tool))d.sources=[];if(tool.startsWith('download'))d.urls='';
  await refresh(false);navigate('queue');notify(`${r.jobs.length} job${r.jobs.length===1?'':'s'} ${start?'submitted; dispatch active':'added to the queue'}.`);
 }catch(e){notify(e.message,true);}finally{controls.forEach(b=>{if(b.isConnected)b.disabled=false;});}
}
const v4CaptureBefore=capture;
capture=function(){if(view==='qr'){for(const key of ['kind','text']){const input=$('.v4-source [name="'+key+'"]');if(input)draft().options[key]=input.value;}}v4CaptureBefore();};
const v4RenderToolBefore=renderTool;
renderTool=function(){v4RenderToolBefore();v4CompactTool();};
const v4RenderPasswordBefore=renderPassword;
renderPassword=function(){v4RenderPasswordBefore();v4CompactOptions($('#tool-options'),'password');$('.studio-layout')?.classList.add('v4-password');};
function v4MeasuredProgress(j){
 const measured=j.progress_measured===true||j.progress_kind==='measured'||(j.progress_measured===undefined&&Boolean(j.speed)&&Number.isFinite(j.eta_seconds));
 return measured&&typeof j.progress==='number'&&Number.isFinite(j.progress)?Math.min(100,Math.max(0,j.progress)):null;
}
function v4ProgressHTML(j){
 const measured=v4MeasuredProgress(j),active=v4ActiveStatuses.includes(j.status);
 if(j.status==='completed')return `<span class="v4-completed">${icon('check')}Complete</span>`;
 if(!active)return `<span class="v4-progress-note">${j.status==='queued'?(j.depends_on?'Waiting for prior step':'Awaiting dispatch'):'—'}</span>`;
 return `${measured!==null?`<div class="v4-progress-label"><span>${Math.round(measured)}%</span>${j.speed?`<span>${escape(j.speed)}</span>`:''}</div><progress max="100" value="${measured}" aria-label="${escape(j.label)} processing progress"></progress>`:`<span class="v4-progress-note">${escape(j.stage||'Processing')}${j.speed?' · '+escape(j.speed):''}</span>`}${Number.isFinite(j.eta_seconds)&&j.eta_seconds>=0?`<small>${etaText(j.eta_seconds)}</small>`:''}`;
}
function v4JobInfo(j){
 const o=j.options_summary||j.display_options||j.options||j.payload?.options||{},values=[];
 if(o.model)values.push(o.model);if(o.device)values.push(o.device.toUpperCase());if(o.format)values.push(String(o.format).toUpperCase());if(o.formats?.length)values.push(o.formats.join(' / ').toUpperCase());if(o.max_height)values.push(o.max_height+'p');if(o.target_hz)values.push(o.target_hz+' Hz');if(o.quality)values.push(o.quality);if(o.method)values.push(o.method);if(j.resource)values.push(j.resource==='download'?'Network':j.resource.toUpperCase());
 return values.join(' · ');
}
jobRow=function(j){
 const id=escape(j.id),active=['queued',...v4ActiveStatuses].includes(j.status),retry=['failed','cancelled','interrupted','blocked','completed'].includes(j.status),outputs=j.outputs||[];
 return `<div class="queue3-row" role="row" data-row-job="${id}"><div role="cell"><input type="checkbox" class="job-select" data-select-job="${id}" ${selectedJobs.has(j.id)?'checked':''} aria-label="Select ${escape(j.label)}"></div><div class="v4-job-file" role="cell"><span class="tool-icon">${icon(j.tool)}</span><div class="v4-job-name"><strong title="${escape(j.label)}">${escape(j.label)}</strong><span class="v4-job-status ${escape(j.status)}">${escape(j.status)}${j.message?' · '+escape(v4SafeLabel(j.message)):''}</span>${j.workflow_name?`<small>${escape(j.workflow_name)} · step ${Number(j.step)||1}/${Number(j.steps)||1}</small>`:''}</div></div><div class="v4-job-operation" role="cell"><strong>${escape(titles[j.tool]||j.tool)}</strong><small>${escape(v4JobInfo(j)||when(j.created))}</small></div><div class="v4-job-progress" role="cell">${v4ProgressHTML(j)}</div><div class="v4-row-actions" role="cell">${outputs.length?v4IconButton('open',outputs.length>1?'Open first output; all outputs in details':'Open output file',`data-open-output="${id}" data-output-index="0"`)+v4IconButton('folder','Open output folder',`data-reveal-output="${id}" data-output-index="0"`):''}${v4IconButton('info','Job details',`data-job="${id}"`)}${active&&!['cancelling','finalizing'].includes(j.status)?v4IconButton('cancel','Cancel job',`data-cancel="${id}"`):''}${retry?v4IconButton('refresh','Retry job',`data-retry="${id}"`):''}${!active?v4IconButton('trash','Remove queue record; keep output files',`data-remove-job="${id}"`):''}</div></div>`;
};
function v4VisibleJobs(){
 const jobs=state.jobs.filter(j=>filter==='active'?['queued',...v4ActiveStatuses].includes(j.status):filter==='failed'?v4FailureStatuses.includes(j.status):['completed','cancelled'].includes(j.status));
 const queued=jobs.filter(j=>j.status==='queued').sort((a,b)=>(a.rank??a.created)-(b.rank??b.created));
 return [...jobs.filter(j=>v4ActiveStatuses.includes(j.status)),...queued,...jobs.filter(j=>!['queued',...v4ActiveStatuses].includes(j.status))];
}
renderQueue=function(){
 if(!['active','failed','history'].includes(filter))filter='active';
 $('#main').innerHTML=heading('STUDIO / PROCESSING','Processing queue','Local jobs. Measured progress. Your files remain yours.')+
 `<div class="v4-queue-tabs" role="tablist" aria-label="Queue views">${[['active','Active & Queued'],['failed','Failed'],['history','History']].map(([key,label])=>`<button class="filter ${filter===key?'active':''}" role="tab" aria-selected="${filter===key}" tabindex="${filter===key?0:-1}" data-filter="${key}" aria-controls="queue-list">${label}${key==='failed'?'<span id="failed-count" class="v4-count"></span>':''}</button>`).join('')}<span id="queue-resource-summary" class="queue-limits"></span></div>`+
 `<div class="v4-queue-toolbar" role="toolbar" aria-label="Queue actions">${v4IconButton('select','Select all visible jobs','id="select-visible"')}${v4IconButton('clear','Clear selection','id="clear-selection"')}<i class="v4-toolbar-separator"></i>${v4IconButton('play','Start or resume dispatch','id="resume-queue"')}${v4IconButton('pause','Pause new dispatch; active jobs continue','id="pause-queue"')}<i class="v4-toolbar-separator"></i>${v4IconButton('cancel','Cancel selected jobs','id="cancel-selected" data-selection-action')}${v4IconButton('refresh','Retry selected jobs','id="retry-selected" data-selection-action')}${v4IconButton('up','Move selected queued jobs up','id="up-selected" data-selection-action')}${v4IconButton('down','Move selected queued jobs down','id="down-selected" data-selection-action')}${v4IconButton('trash','Remove selected records; keep physical output files','id="remove-selected" data-selection-action')}<details class="v4-overflow"><summary aria-label="Additional queue actions" title="Additional queue actions">${icon('more')}</summary><div class="v4-overflow-menu"><button id="first-selected">Move selected to first</button><button id="last-selected">Move selected to last</button><button id="queue-resources">Resource settings</button></div></details><span id="selection-count" aria-live="polite"></span><span id="queue-dispatch-state"></span></div>`+
 `<section id="queue-list" class="queue3-table" role="table" aria-label="${filter==='active'?'Active and queued':filter==='failed'?'Failed':'Historical'} jobs" tabindex="0"></section><p class="note v4-queue-note">Pause affects new dispatch. Black Scripting dependencies take precedence over order. Removing a record keeps physical output files.</p>`;
 const tabs=$$('[data-filter]');tabs.forEach((b,i)=>{b.onclick=()=>{filter=b.dataset.filter;renderQueue();$(`[data-filter="${filter}"]`).focus();};b.onkeydown=e=>{let n=i;if(e.key==='ArrowRight')n=(i+1)%tabs.length;else if(e.key==='ArrowLeft')n=(i+tabs.length-1)%tabs.length;else if(e.key==='Home')n=0;else if(e.key==='End')n=tabs.length-1;else return;e.preventDefault();tabs[n].click();};});
 $('#select-visible').onclick=()=>{v4VisibleJobs().forEach(j=>selectedJobs.add(j.id));updateQueueList();};$('#clear-selection').onclick=()=>{selectedJobs.clear();updateQueueList();};
 $('#resume-queue').onclick=()=>v4SetPaused(false);$('#pause-queue').onclick=()=>v4SetPaused(true);
 for(const action of ['cancel','retry','remove','up','down','first','last'])$('#'+action+'-selected').onclick=()=>v4QueueAction(action,[...selectedJobs]);
 $('#queue-resources').onclick=()=>navigate('settings');updateQueueList();
};
async function v4SetPaused(paused){try{await api('/queue',{paused});await refresh();notify(paused?'Dispatch paused. Active jobs continue.':'Dispatch resumed.');}catch(e){notify(e.message,true);}}
async function v4QueueAction(action,ids){
 if(!ids.length)return notify('Select at least one job.');
 if(action==='remove'&&!confirm(`Remove ${ids.length} queue record${ids.length===1?'':'s'}? Physical input and output files are kept.`))return;
 if(action==='cancel'&&ids.length>1&&!confirm(`Cancel ${ids.length} selected jobs?`))return;
 try{
  const r=await api('/queue/actions',{action,ids}),results=r.results||[],failures=results.filter(item=>item.ok===false||item.error);
  notify(failures.length?`${results.length-failures.length} accepted. ${v4SafeLabel(failures[0].error||'Some selected records could not be changed.')}`:`${action==='remove'?'Queue records removed. Physical files kept.':action==='retry'?'Retries added to the queue.':'Queue updated.'}`,Boolean(failures.length));
  if(!['up','down','first','last'].includes(action))selectedJobs.clear();await refresh();
 }catch(e){notify(e.message,true);}
}
function v4UpdateQueueControls(){
 const count=selectedJobs.size;if($('#selection-count'))$('#selection-count').textContent=count?`${count} selected`:'';
 $$('[data-selection-action]').forEach(b=>b.disabled=!count);
 if($('#failed-count'))$('#failed-count').textContent=state.scheduler?.failed_count??state.jobs.filter(j=>v4FailureStatuses.includes(j.status)).length;
 const paused=state.settings.queue_paused;if($('#pause-queue'))$('#pause-queue').disabled=paused;if($('#resume-queue'))$('#resume-queue').disabled=!paused;
 if($('#queue-dispatch-state'))$('#queue-dispatch-state').textContent=paused?'Dispatch paused':'Dispatch active';
 const scheduler=state.scheduler||{},s=state.settings;
 const limits=scheduler.limits||{};if($('#queue-resource-summary')){$('#queue-resource-summary').textContent=`${s.concurrency_mode==='manual'?'Manual':'Auto'} · up to ${scheduler.effective_workers||scheduler.effective_limit||s.workers||1} jobs · CPU ${limits.cpu||s.cpu_workers||1} / GPU ${limits.gpu||s.gpu_workers||1} / Network ${limits.network||s.download_workers||1} / Disk ${limits.disk||s.disk_workers||1}`;$('#queue-resource-summary').title=scheduler.metrics?.gpu?.available?'GPU admission uses live memory information and conservative model budgets.':'Live GPU memory data unavailable; conservative GPU admission applies.';}
}
updateQueueList=function(){
 const list=$('#queue-list');if(!list)return;
 const focused=document.activeElement,focusJob=focused?.closest('[data-row-job]')?.dataset.rowJob,focusLabel=focused?.getAttribute('aria-label');
 const records=new Set(state.jobs.map(j=>j.id));[...selectedJobs].forEach(id=>{if(!records.has(id))selectedJobs.delete(id);});
 const jobs=v4VisibleJobs();
 list.innerHTML=`<div class="queue3-header" role="row"><span role="columnheader" aria-label="Selection"></span><span role="columnheader">File / operation</span><span role="columnheader">Processing</span><span role="columnheader">Progress</span><span role="columnheader">Actions</span></div>`+(jobs.length?jobs.map(jobRow).join(''):`<div class="empty-state"><span>${icon(filter==='failed'?'check':'queue')}</span><h3>${filter==='failed'?'No failed jobs.':filter==='history'?'No history yet.':'Ready for your next operation.'}</h3>${filter==='active'?'Add files or configure a tool to begin.':''}</div>`);
 bindJobActions();v4UpdateQueueControls();renderDock();
 if(focusJob&&focusLabel){const row=$$('[data-row-job]',list).find(r=>r.dataset.rowJob===focusJob);$$('[aria-label]',row||document.createElement('div')).find(b=>b.getAttribute('aria-label')===focusLabel)?.focus({preventScroll:true});}
};
const v4BindJobsBefore=bindJobActions;
bindJobActions=function(){
 v4BindJobsBefore();
 $$('[data-select-job]').forEach(b=>b.onchange=()=>{if(b.checked)selectedJobs.add(b.dataset.selectJob);else selectedJobs.delete(b.dataset.selectJob);v4UpdateQueueControls();});
 $$('[data-remove-job]').forEach(b=>b.onclick=()=>v4QueueAction('remove',[b.dataset.removeJob]));
 $$('[data-open-output]').forEach(b=>b.onclick=()=>v4OpenOutput(b.dataset.openOutput,b.dataset.outputIndex));
 $$('[data-reveal-output]').forEach(b=>b.onclick=async()=>{try{const r=await api(`/jobs/${b.dataset.revealOutput}/reveal/${b.dataset.outputIndex}`,{});if(!r.opened)notify(r.path);}catch(e){notify(e.message,true);}});
};
async function v4OpenOutput(id,index){try{const r=await api(`/jobs/${id}/open-output/${Number(index)}`,{});if(!r.opened)notify(r.message||r.path||'Native file opening is unavailable on this system.');}catch(e){notify(e.message,true);}}
const v4JobDetailBefore=jobDetail;
jobDetail=async function(id){
 await v4JobDetailBefore(id);const j=state.jobs.find(j=>j.id===id);if(!j)return;
 $$('.output-item',$('#dialog-content')).forEach((row,i)=>{const button=document.createElement('button');button.className='secondary';button.innerHTML=icon('open')+'Open file';button.setAttribute('aria-label','Open '+(j.outputs[i]?.name||'output'));button.onclick=()=>v4OpenOutput(id,i);row.append(button);});
 if(j.workflow_name&&j.batch_id){const group=document.createElement('div');group.className='v4-detail-actions';group.innerHTML=`<p>Black Scripting: ${escape(j.workflow_name)} · step ${Number(j.step)||1}/${Number(j.steps)||1}</p><button class="secondary" id="retry-script-group">${icon('refresh')}Retry entire script</button><button class="secondary" id="cancel-script-group">${icon('cancel')}Cancel script group</button>`;$('#dialog-content').append(group);
  $('#retry-script-group').onclick=async()=>{if(!confirm('Create a fresh run of this entire Black Scripting chain? Completed outputs are kept.'))return;try{await api(`/batches/${j.batch_id}/retry`,{});await refresh();notify('Black Scripting chain queued.');}catch(e){notify(e.message,true);}};
  $('#cancel-script-group').onclick=async()=>{if(!confirm('Cancel all queued and running steps in this Black Scripting group?'))return;try{await api(`/batches/${j.batch_id}/cancel`,{});await refresh();}catch(e){notify(e.message,true);}};
 }
 const message=$('#dialog-content > pre');if(message)message.textContent=v4SafeLabel(message.textContent);
};
renderDock=function(){
 if(!state)return;const active=state.jobs.filter(j=>v4ActiveStatuses.includes(j.status)),queued=state.jobs.filter(j=>j.status==='queued').length,j=active[0],progress=j?v4MeasuredProgress(j):null;
 $('#dock-title').innerHTML=icon('queue')+'<span>PROCESSING QUEUE</span>';
 $('#dock-status').innerHTML=`${j?`<strong title="${escape(j.label)}">${escape(j.label||titles[j.tool]||j.tool)}</strong><span class="v4-dock-tool">${escape(titles[j.tool]||j.tool)}</span>`:'<strong>Queue ready</strong>'}<span>${active.length} running · ${queued} queued</span>${progress!==null?`<progress value="${progress}" max="100" aria-label="Current job progress"></progress><span>${Math.round(progress)}%</span>`:''}${state.settings.queue_paused?'<span class="v4-paused">Paused</span>':''}`;
 const c=$('.counter');if(c)c.textContent=busyJobs().length;
};
function v4SyncShell(){
 if(!state)return;$('#black-scripting-launcher').innerHTML=icon('script')+'<span>Black Scripting</span>';$('#black-scripting-launcher').onclick=()=>navigate('workflows');$('#black-scripting-launcher').classList.toggle('active',view==='workflows');
 document.body.classList.toggle('black-scripting-workspace',view==='workflows');document.body.classList.toggle('black-scripting-active',view==='workflows');
 $$('#sidebar [data-view="workflows"], #sidebar [data-favorite-nav="workflows"]').forEach(b=>b.remove());
 let osNav=$('#forge-os-nav');if(!osNav){const caption=document.createElement('div');caption.className='nav-caption';caption.textContent='FORGE OS';osNav=document.createElement('nav');osNav.id='forge-os-nav';osNav.setAttribute('aria-label','FORGE OS utilities');$('#tool-nav').after(caption,osNav);}
 const osButtons=['metadata','qr','password','identity','obfuscation','addresses','strings'].map(tool=>$(`#tool-nav [data-view="${tool}"]`)||$(`#forge-os-nav [data-view="${tool}"]`)).filter(Boolean);osNav.replaceChildren(...osButtons);
 if($('#crumb'))$('#crumb').textContent=titles[view]||view;
}
const v4NavBefore=renderNav;renderNav=function(){v4NavBefore();v4SyncShell();};
const v4FavoritesBefore=renderFavorites;renderFavorites=function(){v4FavoritesBefore();$$('#sidebar [data-favorite-nav="workflows"]').forEach(b=>b.remove());};
const v4RenderBefore=render;render=function(){v4RenderBefore();v4SyncShell();};
const v4OverviewBefore=renderOverview;renderOverview=function(search=''){v4OverviewBefore(search);$('#recent-jobs')?.setAttribute('role','table');const toolStat=$$('.stat strong')[3];if(toolStat)toolStat.textContent=String(Object.keys(defaults).length);};
const v4SettingsBefore=renderSettings;
renderSettings=function(){
 v4SettingsBefore();const s=state.settings,fields=$('#settings-fields');if(!fields)return;
 const workers=$('[name="workers"]');workers.innerHTML=[1,2,3,4,5].map(n=>`<option value="${n}" ${Number(s.workers)===n?'selected':''}>${n}${n===1?' · sequential':''}</option>`).join('');
 workers.closest('.field').querySelector('label').textContent='Maximum simultaneous jobs';
 workers.closest('.field').querySelector('.hint').textContent='Maximum five independent jobs. Resource budgets and dependencies may admit fewer.';
 fields.insertAdjacentHTML('afterbegin',field('Concurrency mode','concurrency_mode',s.concurrency_mode||'auto',{options:[['auto','Automatic · recommended'],['manual','Manual limits']]}));
 fields.insertAdjacentHTML('beforeend',field('Disk-heavy job limit','disk_workers',s.disk_workers||1,{type:'number',min:1,max:5})+field('GPU memory reserve (MB)','gpu_memory_reserve_mb',s.gpu_memory_reserve_mb??1024,{type:'number',min:256,max:16384,step:256,hint:'Free memory left outside admitted GPU jobs when live memory data is available.'}));
 for(const name of ['cpu_workers','gpu_workers','download_workers']){const el=$(`[name="${name}"]`);if(el)el.max=5;}
 const mode=$('[name="concurrency_mode"]');mode.onchange=()=>{const hint=workers.closest('.field').querySelector('.hint');workers.disabled=false;hint.textContent=mode.value==='auto'?'Automatic mode adapts to available resources, up to this ceiling of five.':'Manual ceiling. CPU, GPU, network and disk limits still apply.';};mode.onchange();
 $('#save-settings').onclick=async()=>{const values={...state.settings};$$('[name]',fields).forEach(el=>{values[el.name]=el.type==='checkbox'?el.checked:el.type==='number'||['workers'].includes(el.name)?Number(el.value):el.value;});try{await api('/settings',values);await refresh(false);renderSettings();notify('Settings saved. Resource limits apply at dispatch.');}catch(e){notify(e.message,true);}};
 // One environment section at a time, preserving v3 update and installer handlers.
 const layout=$('.settings-grid'),sections=[['runtime','Runtime and resources','settings',['Runtime settings']],['engines','Engines and models','cpu',['Installed components','Install optional engines','GPU diagnostics','Model cache','Guided optional setup']],['updates','Application and package updates','refresh',['Update discovery','Application updates']],['privacy','Storage, browser and service','metadata',['Local storage','Remembered browser','Service controls']]].map(([id,label,ic,names])=>({id,label,icon:ic,names,element:document.createElement('section')}));
 $$('.settings-grid > .column > .panel').forEach(p=>{const title=p.querySelector('.panel-header h2')?.textContent;const group=sections.find(g=>g.names.includes(title))||sections[0];group.element.append(p);});
 layout.className='v4-environment';sections.forEach(s=>s.element.className='v4-environment-section');v4MountTabs(layout,sections,{key:'environment',ariaLabel:'Environment categories'});
};
window.ForgeUI={iconButton:v4IconButton,mountTabs:v4MountTabs,compactOptions:v4CompactOptions,compactTool:v4CompactTool,toolGroups:v4ToolGroups,safeLabel:v4SafeLabel,measuredProgress:v4MeasuredProgress};
