/* Birthright Update Center and automatically remembered safe working configuration. */
(() => {
 'use strict';
 const timers=new Map(),pending=new Map(),saved=new Map(),inflight=new Map();
 const originalDraft=draft;
 draft=function(tool=view){
  const fresh=!drafts[tool],value=originalDraft(tool),remembered=state?.working_preferences?.[tool];
  if(fresh&&remembered){Object.assign(value.options,structuredClone(remembered.options));Object.assign(value.output,structuredClone(remembered.output));}
  return value;
 };
 function persist(tool){
  const value=drafts[tool],fields=state?.preference_fields?.[tool];if(!value||!fields)return;
  const body={options:Object.fromEntries(Object.entries(value.options).filter(([key])=>fields.includes(key))),output:value.output};
  const serialized=JSON.stringify(body);if(saved.get(tool)===serialized)return;
  pending.set(tool,body);clearTimeout(timers.get(tool));
  timers.set(tool,setTimeout(async()=>{
   const snapshot=pending.get(tool);pending.delete(tool);
   try{const operation=(inflight.get(tool)||Promise.resolve()).catch(()=>{}).then(()=>api('/preferences/'+tool,snapshot));inflight.set(tool,operation);await operation;saved.set(tool,JSON.stringify(snapshot));}
   catch(error){notify('Could not remember these settings: '+error.message,true);}
  },250));
 }
 const originalCapture=capture;
 capture=function(){const tool=view;originalCapture();persist(tool);};
 document.addEventListener('input',event=>{if(event.target.closest('#main'))capture();});
 document.addEventListener('change',event=>{if(event.target.closest('#main'))queueMicrotask(()=>capture());});
 window.addEventListener('pagehide',()=>{
  // Same-origin authenticated keepalive, without secret field values.
  for(const [tool,body] of pending)fetch('/api/preferences/'+tool,{method:'POST',credentials:'same-origin',keepalive:true,headers:{'Content-Type':'application/json','X-Forge-CSRF':csrf},body:JSON.stringify(body)}).catch(()=>{});
 });
 window.ForgePreferences={save:()=>persist(view)};
 function duration(seconds){
  if(!Number.isFinite(seconds)||seconds<0)return '';
  const total=Math.round(seconds),s=total%60,m=Math.floor(total/60)%60,h=Math.floor(total/3600);
  return h?`${h}h ${m}m ${s}s`:m?`${m}m ${s}s`:`${s}s`;
 }
 const progressBefore=v4ProgressHTML;
 v4ProgressHTML=function(job){return job.status==='completed'&&Number.isFinite(job.execution_seconds)?`<span class="v4-completed">${icon('check')}Completed · ${duration(job.execution_seconds)}</span>`:progressBefore(job);};
 const detailBefore=jobDetail;
 jobDetail=async function(id){await detailBefore(id);const job=state.jobs.find(j=>j.id===id);if(!job||!Number.isFinite(job.execution_seconds))return;
  $('#dialog-content').insertAdjacentHTML('beforeend',`<dl class="v6-durations"><dt>Execution</dt><dd>${duration(job.execution_seconds)}</dd><dt>Started / finished</dt><dd>${escape(when(job.started))} / ${job.finished?escape(when(job.finished)):'—'}</dd>${Object.entries(job.stage_seconds||{}).map(([stage,seconds])=>`<dt>${escape(stage)}</dt><dd>${duration(seconds)}</dd>`).join('')}</dl>`);
 };
 function refine(){
  if(state?.preference_fields?.[view]&&$('.heading-actions')&&!$('#reset-working-settings')){
   const button=document.createElement('button');button.id='reset-working-settings';button.className='text-button v6-reset';button.textContent='Reset defaults';
   button.onclick=async()=>{const tool=view;clearTimeout(timers.get(tool));pending.delete(tool);try{await (inflight.get(tool)||Promise.resolve()).catch(()=>{});await api('/preferences/'+tool+'/reset',{});delete state.working_preferences[tool];saved.delete(tool);const sources=draft(tool).sources;delete drafts[tool];draft(tool).sources=sources;render();}catch(error){notify(error.message,true);}};
   $('.heading-actions').append(button);
  }
  // Put secondary explanatory material behind a disclosure, retaining warnings.
  $$('.v4-context > .note,.v4-context > .callout.neutral').forEach(note=>{
   if(note.closest('details')||/warning|secret|credential|overwrite|original|privacy/i.test(note.textContent))return;
   const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Details';note.replaceWith(details);details.append(summary,note);
  });
 }
 new MutationObserver(refine).observe($('#main'),{childList:true,subtree:true});
 const updateButton=$('#birthright-updates');updateButton.innerHTML=icon('refresh')+'<span id="update-count" hidden></span>';
 const settingsBefore=renderSettings;
 renderSettings=function(){settingsBefore();
  const legacy=$('#check-updates')?.closest('.panel');if(legacy){legacy.querySelector('.panel-header h2').textContent='Birthright Updates';legacy.querySelector('.panel-body').innerHTML='<button class="secondary" id="open-update-center">Open Updates</button>';$('#open-update-center').onclick=()=>updateButton.click();}
  const guided=$('[data-install-engine]')?.closest('.panel');if(guided){const note=$('.note',guided);if(note)note.textContent='Approved installations wait for work to finish and restart FORGE automatically.';}
  const daily=$('[name="daily_update_check"]')?.closest('.field');if(daily)daily.remove();
 };
 let catalog={entries:[],count:0},center=null,refreshTimer,maintenanceTimer;
 function badge(){
  catalog=state?.update_catalog||catalog;
  const count=catalog.count||0;$('#update-count').textContent=String(count);$('#update-count').hidden=!count;
  updateButton.setAttribute('aria-label',count?`Updates: ${count} available`:'Updates');
 }
 const dockBefore=renderDock;
 renderDock=function(){dockBefore();badge();};
 function status(message,error=false){const el=$('#birthright-update-status');if(el){el.textContent=message;el.className='birthright-update-status'+(error?' birthright-update-error':'');}}
 const categories={application:'Application',models:'Transcription models',dependencies:'Python dependencies',tools:'Production utilities',gpu:'GPU runtime'};
 function row(entry){
  let action='';
  if(entry.id==='gpu-runtime')action='<button class="secondary" id="update-gpu-check">Diagnose</button>';
  else if(entry.action||entry.installable||entry.category==='models'&&!entry.installed)action=`<button class="${entry.action?'primary':'secondary'}" data-update-id="${escape(entry.id)}">${entry.action==='update'?'Update':entry.installed?'Repair':'Install'}</button>`;
  const revisions=(entry.revisions||[]).length>1?`<select data-model-revision="${escape(entry.id.slice(6))}" aria-label="${escape(entry.label)} cached revision">${entry.revisions.map(r=>`<option value="${escape(r.revision)}" ${r.revision===entry.installed?'selected':''}>${escape(r.revision.slice(0,12))} · cached</option>`).join('')}</select>`:'';
  const display=value=>/^[0-9a-f]{40}$/.test(value||'')?value.slice(0,12):value;
  return `<div class="birthright-update-row"><div><strong>${escape(entry.label)}</strong><small title="${escape(entry.installed)}">${escape(display(entry.installed)||'Not installed')}${entry.available?' → '+escape(display(entry.available)):''}</small>${entry.published?`<small>${escape(new Date(entry.published).toLocaleDateString())}${entry.bytes?' · '+bytes(entry.bytes):''}</small>`:''}${entry.notes||entry.rollback?`<details><summary>${entry.category==='application'?'Changelog':'Details'}</summary><pre>${escape(entry.notes||entry.available||'')}${entry.rollback?'\nRollback: '+escape(entry.rollback):''}</pre></details>`:''}${revisions}</div><div>${action}</div></div>`;
 }
 function draw(){
  if(!center?.open)return;
  const entries=catalog.entries||[],available=entries.filter(e=>e.action);
  center.innerHTML=`<div class="birthright-update-header"><div><small>BIRTHRIGHT</small><h2>Updates</h2><span class="birthright-update-status">FORGE ${escape(state.version)}</span></div><button class="text-button" id="updates-later">Later</button></div><div class="birthright-update-actions"><button class="secondary" id="updates-refresh" ${catalog.busy?'disabled':''}>${icon('refresh')}${catalog.busy?'Checking…':'Refresh'}</button><button class="primary" id="updates-all" ${available.length?'':'disabled'}>Update all compatible${available.length?' · '+available.length:''}</button></div><div class="birthright-update-list">${Object.entries(categories).map(([key,title])=>{const list=entries.filter(e=>e.category===key);return list.length?`<h3 class="birthright-update-group">${title}</h3>${list.map(row).join('')}`:'';}).join('')||'<p class="birthright-update-status">Checking installed components…</p>'}</div><p id="birthright-update-status" class="birthright-update-status" role="status">${escape(catalog.errors?.join(' ')||(!available.length&&!catalog.busy?'No compatible updates available.':''))}</p>`;
  $('#updates-later').onclick=()=>center.close();$('#updates-refresh').onclick=()=>check(true);
  $('#updates-all').onclick=()=>install(available);
  $$('[data-update-id]',center).forEach(button=>button.onclick=()=>install(entries.filter(e=>e.id===button.dataset.updateId)));
  $$('[data-model-revision]',center).forEach(select=>select.onchange=async()=>{try{await api('/environment/models/select',{model:select.dataset.modelRevision,revision:select.value});status('Cached revision selected.');check(true);}catch(error){status(error.message,true);}});
  $('#update-gpu-check')?.addEventListener('click',gpuCheck);
 }
 async function check(force=false){try{catalog=await api(force?'/updates/refresh':'/updates/catalog',force?{}:undefined);if(state)state.update_catalog=catalog;badge();draw();clearTimeout(refreshTimer);if(catalog.busy)refreshTimer=setTimeout(()=>check(),1000);}catch(error){status(error.message,true);}}
 async function monitorMaintenance(){
  try{const value=await api('/maintenance');if(value.message)status(value.message,value.status==='failed');
   if(['waiting','starting','installing'].includes(value.status)){clearTimeout(maintenanceTimer);maintenanceTimer=setTimeout(monitorMaintenance,2000);}
   if(value.status==='waiting'&&!$('#cancel-maintenance')){const button=document.createElement('button');button.id='cancel-maintenance';button.className='text-button';button.textContent='Cancel installation request';button.onclick=async()=>{await api('/maintenance/cancel',{});button.remove();monitorMaintenance();};center?.append(button);}
  }catch{status('Maintenance is running. FORGE will reopen automatically; use the external updater if recovery is needed.');}
 }
 async function install(entries){
  if(!entries.length)return;
  const descriptions=entries.map(e=>e.label).join(', ');
  if(!confirm(`Install compatible updates for ${descriptions}? Packages and models download from approved publishers. Existing jobs finish first; FORGE restarts if needed. Selected dependency classes also allow compatible repairs. New application versions always require approval.`))return;
  status('Preparing updates…');
  try{
   const result=await api('/updates/install',{ids:entries.map(e=>e.id),consent:true});
   status(result.message);monitorMaintenance();
  }catch(error){status(error.message,true);}
 }
 async function gpuCheck(){
  status('Checking CUDA runtime libraries…');
  try{
   const result=await api('/environment/diagnostics',{});
   const info=document.createElement('div');info.className='birthright-update-status';
   const pre=document.createElement('pre');pre.textContent=[result.gpu||'No GPU identified',...Object.entries(result.dlls||{}).map(([name,value])=>name+': '+(value.loaded?'loaded':value.error)),...result.notes].join('\n');info.append(pre);
   const models=catalog.entries.filter(e=>e.category==='models'&&e.installed);
   if(models.length&&result.runtime_available){const select=document.createElement('select');select.setAttribute('aria-label','Cached model for CUDA inference check');models.forEach(model=>{const option=document.createElement('option');option.value=model.id.slice(6);option.textContent=model.label;select.append(option);});const button=document.createElement('button');button.className='secondary';button.textContent='Test CUDA inference';button.onclick=async()=>{button.disabled=true;status('Running a short local inference test…');try{const test=await api('/environment/diagnostics',{inference:select.value});status(test.ready?'CUDA inference passed with '+select.value+'.':test.failure||'CUDA inference failed.',!test.ready);}catch(error){status(error.message,true);}finally{button.disabled=false;}};info.append(select,button);}
   for(const [url,label] of [['https://developer.nvidia.com/cuda-downloads','NVIDIA CUDA'],['https://developer.nvidia.com/cudnn-downloads','NVIDIA cuDNN']]){const link=document.createElement('a');link.href=url;link.target='_blank';link.rel='noopener noreferrer';link.textContent=label;info.append(' · ',link);}
   $('.birthright-update-list',center).append(info);status(result.runtime_available?'Runtime libraries available; inference has not yet been tested.':'CUDA runtime is incomplete. CPU remains available.');
  }catch(error){status(error.message,true);}
 }
 updateButton.onclick=()=>{
  capture();if(!center){center=document.createElement('dialog');center.className='birthright-updates';center.setAttribute('aria-label','Birthright Updates');document.body.append(center);center.addEventListener('close',()=>clearTimeout(refreshTimer));}
  center.showModal();draw();check(!catalog.checked);monitorMaintenance();
 };
})();
