/* Owner-operated readiness. No installation, upload or release is triggered. */
(() => {
 'use strict';
 let dialog, timer, playedJob='';
 const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
 async function request(route,body){
  const options={credentials:'same-origin',cache:'no-store',headers:{'X-Forge-CSRF':typeof csrf==='string'?csrf:''}};
  if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
  const response=await fetch('/api/dubbing/acceptance/'+route,options),value=await response.json();
  if(!response.ok)throw Error(typeof value.detail==='string'?value.detail:'Local verification could not continue.');
  return value;
 }
 function message(text){const node=dialog?.querySelector('[data-readiness-message]');if(node)node.textContent=text;}
 function render(state){
  const device=dialog.querySelector('[data-readiness-device]')?.value||'cpu',busy=state.latest_job&&['queued','running','finalizing','cancelling'].includes(state.latest_job.status);
  dialog.innerHTML=`<section class="dv-modal"><h2>Verify V0.10 readiness</h2>
   <p>This checks installed models with generated text and a synthetic reference. It does not download models, upload media, update FORGE or approve a release.</p>
   <dl class="dv-review-facts"><dt>Current blocker</dt><dd>${esc(state.current_blocker)}</dd><dt>Last completed action</dt><dd>${esc(state.last_completed_action)}</dd><dt>Next step</dt><dd>${esc(state.next_action)}</dd></dl>
   <ul>${state.checks.map(row=>`<li><strong>${esc(row.title)}: ${row.status==='passed'?'Passed':'Pending'}</strong>${row.status==='passed'?'':`<p>${esc(row.next_action)}</p>`}</li>`).join('')}</ul>
   <label class="dv-field">Test device<select data-readiness-device><option value="cpu" ${device==='cpu'?'selected':''}>CPU</option><option value="cuda" ${device==='cuda'?'selected':''}>NVIDIA CUDA on this computer</option></select></label>
   <p class="dv-note">CUDA needs actual matching CUDA model packs. If missing, finish active work, return to Set Up Dubbing Studio and review its CUDA Install/Repair quote first. Whisper tiny stays on CPU.</p>
   <label class="dv-check"><input type="checkbox" data-readiness-consent>I approve local inference and temporary outputs using generated text and the synthetic reference.</label>
   <div class="dv-actions"><button type="button" class="primary" data-readiness-run disabled>${busy?'Verification '+esc(state.latest_job.status):'Run local acceptance'}</button><button type="button" class="secondary" data-readiness-refresh>Refresh checks</button><button type="button" class="secondary" data-readiness-publishing>Verify Private YouTube upload</button><button type="button" class="secondary" data-readiness-export>Export readiness report</button><button type="button" class="secondary" data-readiness-notices ${state.runtime_notices_available?'':'disabled'}>Export runtime notices</button><button type="button" class="secondary" data-readiness-close>Close</button></div>
   <p class="dv-note">Runtime notices become available after a passing local run. This separate download contains verified public dependency license text for legal review; it does not approve redistribution.</p>
   ${state.translation_pairs?.length?`<h3>Generated Russian translations</h3><p>These are the exact synthetic inputs and OPUS drafts from this run. Compare meaning and grammar, including ФОРДЖ, 3, 2026-11-13 and 10:30. Successful generation does not establish translation quality.</p><dl>${state.translation_pairs.map(pair=>`<dt>${esc(pair.english)}</dt><dd>${esc(pair.russian)}</dd>`).join('')}</dl><label class="dv-check"><input type="checkbox" data-readiness-translations>I reviewed all three drafts and confirm their meaning, grammar, glossary, numbers and dates are acceptable.</label><div class="dv-actions"><button type="button" class="secondary" data-readiness-confirm-translations disabled>Record translation approval</button><button type="button" class="secondary" data-readiness-reject-translations>Translations need work</button></div>`:''}
   ${state.sample_url?`<h3>Generated Russian sample</h3><audio controls preload="metadata" src="${esc(state.sample_url)}" data-readiness-audio></audio><p>Listen to the complete sample. This is an intelligibility check with a synthetic reference, not human voice-likeness approval.</p><label class="dv-check"><input type="checkbox" data-readiness-listened>I listened and confirm the generated Russian sample is intelligible.</label><button type="button" class="secondary" data-readiness-confirm-listening disabled>Record listening review</button>`:''}
   <p data-readiness-message role="status"></p></section>`;
  const consent=dialog.querySelector('[data-readiness-consent]'),run=dialog.querySelector('[data-readiness-run]');
  consent.onchange=()=>run.disabled=busy||!consent.checked;
  dialog.querySelector('[data-readiness-close]').onclick=()=>dialog.close();
  dialog.querySelector('[data-readiness-refresh]').onclick=()=>refresh();
  dialog.querySelector('[data-readiness-publishing]').onclick=()=>{if(!window.ForgePublishingAcceptance?.open){message('Reload FORGE to open Private YouTube verification.');return;}dialog.close();void window.ForgePublishingAcceptance.open();};
  dialog.querySelector('[data-readiness-export]').onclick=()=>{window.location.assign('/api/dubbing/acceptance/export');};
  dialog.querySelector('[data-readiness-notices]').onclick=()=>{window.location.assign('/api/dubbing/acceptance/runtime-notices');};
  run.onclick=async()=>{run.disabled=true;try{await request('run',{device:dialog.querySelector('[data-readiness-device]').value,consent:consent.checked});await refresh();}catch(error){message(error.message);run.disabled=!consent.checked;}};
  const translated=dialog.querySelector('[data-readiness-translations]'),approveTranslations=dialog.querySelector('[data-readiness-confirm-translations]'),rejectTranslations=dialog.querySelector('[data-readiness-reject-translations]');
  if(translated&&approveTranslations&&rejectTranslations){
   translated.onchange=()=>approveTranslations.disabled=!translated.checked||!state.translation_review_available;
   const record=async approve=>{try{await request('translation-review',{job_id:state.latest_job.id,approve});await refresh();if(!approve)message('Translation quality remains pending. Keep this candidate unreleased until the drafts are acceptable.');}catch(error){message(error.message);}};
   approveTranslations.onclick=()=>record(translated.checked);rejectTranslations.onclick=()=>record(false);
  }
  const audio=dialog.querySelector('[data-readiness-audio]'),listened=dialog.querySelector('[data-readiness-listened]'),confirm=dialog.querySelector('[data-readiness-confirm-listening]');
  if(audio&&listened&&confirm){
   audio.onended=()=>{playedJob=state.latest_job.id;confirm.disabled=!listened.checked||!state.listening_confirmation_available;};
   listened.onchange=()=>confirm.disabled=!listened.checked||playedJob!==state.latest_job.id||!state.listening_confirmation_available;
   confirm.onclick=async()=>{try{await request('listening',{job_id:state.latest_job.id,approve:true});await refresh();}catch(error){message(error.message);}};
  }
  clearTimeout(timer);
  if(busy)timer=setTimeout(()=>{if(dialog.open)void refresh();},3000);
 }
 async function refresh(){try{render(await request('state'));}catch(error){message(error.message);}}
 async function open(){
  if(!dialog){dialog=document.createElement('dialog');dialog.id='forge-v10-readiness';dialog.addEventListener('close',()=>clearTimeout(timer));document.body.append(dialog);}
  if(!dialog.open){dialog.innerHTML='<p data-readiness-message>Checking this installation…</p>';dialog.showModal();}
  await refresh();
 }
 function addButton(){
  const target=document.querySelector('.dv-header-actions');
  if(!target||target.querySelector('[data-open-readiness]'))return;
  const button=document.createElement('button');button.type='button';button.className='secondary';button.dataset.openReadiness='';button.textContent='Verify V0.10 readiness';button.onclick=open;target.append(button);
 }
 document.addEventListener('DOMContentLoaded',()=>{addButton();new MutationObserver(addButton).observe(document.body,{childList:true,subtree:true});});
 window.ForgeDubbingAcceptance={open};
})();
