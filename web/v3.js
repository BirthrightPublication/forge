'use strict';

// Local package checks run only when requested; ordinary Studio polling never hashes ZIPs.
let applicationUpdates=null,applicationUpdateError='',applicationUpdateScanning=false;
let applicationUpdateOpening=false,applicationUpdateOpened='';

function applicationUpdateBusy(){
 const recorded=applicationUpdates?.busy_jobs;
 return Math.max(Array.isArray(recorded)?recorded.length:Number(recorded)||0,busyJobs().length);
}
function applicationUpdateCount(value){
 return Array.isArray(value)?value.length:Number.isFinite(Number(value))?Number(value):0;
}
function applicationUpdateCandidate(candidate,selected=false){
 const counts=['changed_files','new_files','preserved_files'].filter(k=>candidate[k]!==undefined)
  .map(k=>`${applicationUpdateCount(candidate[k])} ${k.replace('_files','')}`).join(' · ');
 return `<article class="update-candidate"><div class="update-candidate-heading"><strong>${escape(candidate.name||'Unnamed package')}</strong><span class="tag ${candidate.status==='available'?'':'warn'}">${escape(candidate.status||'unknown')}${selected?' · selected':''}</span></div>${candidate.version?`<p class="note">Version ${escape(candidate.version)}${candidate.minimum_from?` · requires ${escape(candidate.minimum_from)} or later`:''}</p>`:''}${counts?`<p class="note">Application files: ${escape(counts)}</p>`:''}${candidate.error?`<p class="note update-error">${escape(candidate.error)}</p>`:''}${candidate.sha256?`<div class="update-hash"><span>SHA-256</span><code>${escape(candidate.sha256)}</code></div>`:''}</article>`;
}
function applicationUpdatesHTML(){
 const u=applicationUpdates;
 const intro='<p class="note update-intro">Place a FORGE release ZIP in the build folder, then check it here. Updates back up application files and the local database. Media, outputs, models and private environments are preserved.</p>';
 const scan=`<button class="secondary" id="scan-application-updates" ${applicationUpdateScanning||applicationUpdateOpening?'disabled':''}>${icon('refresh')}${applicationUpdateScanning?'Checking local ZIPs…':'Check local build folder'}</button>`;
 if(!u)return `${intro}<p class="note">Installed version: <strong>${escape(state.version)}</strong>. No local package check has been run.</p><div class="update-actions">${scan}</div>${applicationUpdateError?`<p class="callout error" role="alert">${escape(applicationUpdateError)}</p>`:''}<p class="note">This check reads local files. Application updates run through a Windows console with a final confirmation.</p><p class="note">SHA-256 checks integrity; publisher signatures are not available. Use release ZIPs from a source you trust.</p>`;
 const candidates=u.candidates||[],selected=u.selected;
 const rest=candidates.filter(c=>!selected||c.name!==selected.name);
 const checked=Number.isFinite(Number(u.checked))&&u.checked?when(Number(u.checked)):u.checked||'just now';
 const locations=[['Application',u.installation],['FORGE home',u.home],['Build folder',u.build_directory],['Backups',u.backup_directory]].filter(([,value])=>value);
 return `${intro}<dl class="update-locations"><div><dt>Installed version</dt><dd>${escape(u.version||state.version)}</dd></div>${locations.map(([label,value])=>`<div><dt>${label}</dt><dd>${escape(value)}</dd></div>`).join('')}</dl><p class="note">Last local check: ${escape(checked)}</p><div class="update-actions">${scan}</div>${applicationUpdateError?`<p class="callout error" role="alert">${escape(applicationUpdateError)}</p>`:''}${u.interrupted?'<p class="callout error">An interrupted update needs recovery. Run the external Update FORGE.bat to recover this installation.</p>':''}${(u.warnings||[]).map(w=>`<p class="callout neutral">${escape(w)}</p>`).join('')}${selected?applicationUpdateCandidate(selected,true):'<p class="note">No installable newer release was found in the build folder.</p>'}${rest.length?`<details class="update-other-packages"><summary>Other local packages (${rest.length})</summary>${rest.map(c=>applicationUpdateCandidate(c)).join('')}</details>`:''}<p class="note">SHA-256 checks integrity; publisher signatures are not available. Use release ZIPs from a source you trust.</p>${u.rollback?.available?`<p class="note">Rollback available${u.rollback.from?` from ${escape(u.rollback.from)}`:''}${u.rollback.to?` to ${escape(u.rollback.to)}`:''}. The console explains which backup will be restored.</p>`:`<p class="note">Rollback is unavailable${u.rollback?.error?`: ${escape(u.rollback.error)}`:': no recoverable update backup was found.'}</p>`}${u.platform_supported?'<div class="update-actions"><button class="primary" id="open-application-update">Review update in console</button><button class="secondary" id="open-application-rollback">Review rollback in console</button></div>':'<p class="callout neutral">The updater console is available on Windows only.</p>'}<p class="note" id="application-update-readiness" aria-live="polite"></p>${applicationUpdateOpened?`<p class="callout neutral" role="status">${escape(applicationUpdateOpened)}</p>`:''}`;
}
function updateApplicationControls(){
 if(!$('#application-updates-panel'))return;
 const u=applicationUpdates,busy=applicationUpdateBusy();
 const blocked=applicationUpdateScanning||applicationUpdateOpening||Boolean(applicationUpdateOpened)||Boolean(u?.maintenance)||Boolean(u?.interrupted)||busy>0;
 const update=$('#open-application-update'),rollback=$('#open-application-rollback');
 if(update)update.disabled=blocked||!u?.platform_supported||u?.selected?.status!=='available'||Boolean(u?.interrupted);
 if(rollback)rollback.disabled=blocked||!u?.platform_supported||!u?.rollback?.available;
 const readiness=$('#application-update-readiness');
 if(readiness)readiness.textContent=applicationUpdateOpening?'Opening the updater console…':u?.interrupted?'Run the external Update FORGE.bat to recover this installation.':u?.maintenance?'An updater console is already open. Complete or close it before trying again.':busy?`${busy} queued or active job${busy===1?'':'s'} must finish or be cancelled before updating. Check the build folder again once the queue is idle.`:'The server rechecks packages and the queue when opening the console. Review and confirm there before FORGE stops.';
}
function renderApplicationUpdates(){
 const section=$('#application-updates-panel');if(!section)return;
 section.querySelector('.panel-body').innerHTML=applicationUpdatesHTML();
 $('#scan-application-updates').onclick=scanApplicationUpdates;
 $('#open-application-update')?.addEventListener('click',()=>openApplicationUpdater('update'));
 $('#open-application-rollback')?.addEventListener('click',()=>openApplicationUpdater('rollback'));
 updateApplicationControls();
}
async function scanApplicationUpdates(){
 if(applicationUpdateScanning||applicationUpdateOpening)return;
 applicationUpdateScanning=true;applicationUpdateError='';renderApplicationUpdates();
 try{applicationUpdates=await api('/application-updates');applicationUpdateOpened='';}
 catch(e){applicationUpdates=null;applicationUpdateError=e.message;notify(e.message,true);}
 finally{applicationUpdateScanning=false;renderApplicationUpdates();}
}
async function openApplicationUpdater(action){
 const button=$(action==='update'?'#open-application-update':'#open-application-rollback');
 if(!button||button.disabled||applicationUpdateOpening)return;
 const message=action==='update'?'Open the Windows updater console to review the selected local release? FORGE stays available until you confirm in that console. The update then stops FORGE and backs up application files and the database.':'Open the Windows updater console to review rollback? FORGE stays available until you confirm in that console. Rollback restores backed-up application files; current media and database are preserved.';
 if(!confirm(message))return;
 applicationUpdateOpening=true;updateApplicationControls();
 try{const r=await api('/application-updates/open',{action,consent:true});if(r.started)applicationUpdateOpened=r.message||'Updater console opened. Review and confirm there before FORGE stops.';notify(r.message||'Updater console opened.');}
 catch(e){applicationUpdateError=e.message;notify(e.message,true);}
 finally{applicationUpdateOpening=false;renderApplicationUpdates();}
}

const renderSettingsBeforeV3=renderSettings;
renderSettings=function(){
 renderSettingsBeforeV3();
 const section=$$('.panel').find(p=>p.querySelector('.panel-header h2')?.textContent==='Local build updates');
 if(!section)return;
 section.id='application-updates-panel';
 section.querySelector('.panel-header h2').textContent='Application updates';
 section.querySelector('.panel-header span').textContent='LOCAL RELEASES';
 renderApplicationUpdates();
};
const setupStateBeforeV3=setupV2State;
setupV2State=function(previous){setupStateBeforeV3(previous);updateApplicationControls();};
