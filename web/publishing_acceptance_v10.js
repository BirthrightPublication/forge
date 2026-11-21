/* Owner-driven live acceptance. Preparation and status never approve upload. */
(()=>{
 'use strict';
 const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
 let dialog,selected='',prepared=null,epoch=0;
 const query=selector=>dialog.querySelector(selector);
 async function request(path,body){
  const init={credentials:'same-origin',cache:'no-store',headers:{'X-Forge-CSRF':typeof csrf==='string'?csrf:''}};
  if(body!==undefined){init.method='POST';init.headers['Content-Type']='application/json';init.body=JSON.stringify(body);}
  const response=await fetch('/api/language-publishing/'+path,init);let value={};try{value=await response.json();}catch{}
  if(!response.ok)throw Error(typeof value.detail==='string'?value.detail:'The local verification request failed.');
  return value;
 }
 function ensure(){if(dialog)return;dialog=document.createElement('dialog');dialog.id='forge-publishing-acceptance';dialog.setAttribute('aria-label','Private YouTube verification');dialog.style.cssText='width:min(720px,94vw);max-height:90vh;overflow:auto;background:#171916;color:#e9e7de;border:1px solid #66614c;padding:24px';document.body.append(dialog);dialog.addEventListener('cancel',()=>epoch++);}
 function message(value,error=false){const node=query('[data-message]');if(node){node.textContent=value;node.style.color=error?'#f6a7a7':'';}}
 async function attempt(action){try{return await action();}catch(error){message(error.message,true);return null;}}
 function show(body){ensure();dialog.innerHTML='<header style="display:flex;justify-content:space-between;gap:16px"><h2>Private YouTube verification</h2><button data-close aria-label="Close verification">Close</button></header>'+body+'<p data-message role="status"></p>';query('[data-close]').onclick=()=>{epoch++;dialog.close();};if(!dialog.open)dialog.showModal();}
 async function open(profileId=''){
  const ticket=++epoch,value=await request('acceptance/state');if(ticket!==epoch)return;selected=profileId||selected||value.profiles[0]?.id||'';prepared=null;
  show('<p>This test uses a generated two-second video and Russian subtitles. It remains <strong>Private</strong>; public visibility and scheduling are not part of this test.</p><ol>'+value.requirements.map(row=>'<li>'+esc(row.ready?'Ready: ':'Required: ')+esc(row.action)+'</li>').join('')+'</ol><label>Language channel profile<select data-profile>'+value.profiles.map(profile=>'<option value="'+esc(profile.id)+'" '+(profile.id===selected?'selected':'')+'>'+esc(profile.name)+' · '+esc(profile.channel_name||'not connected')+'</option>').join('')+'</select></label><div class="dv-actions"><button data-new-profile>Create Russian profile</button><button data-connect>Connect / verify YouTube</button><button data-refresh>Refresh readiness</button></div><p>Google requires your Desktop OAuth client, enabled YouTube Data API and your consent. Keep the client JSON and credentials on your Windows computer.</p><button class="primary" data-prepare '+(!value.can_prepare?'disabled':'')+'>Prepare generated Private test</button><h3>Existing tests</h3><div>'+value.sessions.map(test=>'<button data-existing="'+esc(test.id)+'">Check saved test '+esc(test.id.slice(0,8))+'</button>').join(' ')+'</div>');
  query('[data-profile]').onchange=event=>selected=event.target.value;
  query('[data-refresh]').onclick=()=>attempt(()=>open(selected));
  query('[data-new-profile]').onclick=()=>attempt(async()=>{const name=prompt('Russian profile name','Russian YouTube verification');if(!name?.trim())return;const result=await request('profiles',{profile:{name:name.trim(),language:'ru',platform:'youtube',default_visibility:'private'}});selected=result.profile.id;await open(selected);});
  query('[data-connect]').onclick=()=>attempt(async()=>{if(!selected)throw Error('Create or select a Russian profile first.');epoch++;dialog.close();await window.ForgeDubbing.connectPublishingChannel(selected);});
  query('[data-prepare]').onclick=()=>attempt(async()=>{const ticket=epoch;query('[data-prepare]').disabled=true;message('Creating the local sample and verifying the selected channel…');const result=await request('acceptance/prepare',{profile_id:selected});if(ticket===epoch)review(result);});
  dialog.querySelectorAll('[data-existing]').forEach(button=>button.onclick=()=>attempt(()=>check(button.dataset.existing)));
 }
 function review(result){
  prepared=result;const facts=result.review;
  show('<video controls preload="metadata" style="width:100%;max-height:220px" src="'+esc(result.preview_url)+'"></video><dl>'+[['Channel',facts.channel_name],['Channel ID',facts.channel_id],['Title',facts.metadata.title],['Visibility',facts.metadata.visibility],['Schedule','None'],['Audience','Not made for kids'],['Synthetic disclosure','Yes'],['Media SHA-256',facts.file_sha256],['Subtitles',facts.subtitle_status]].map(([key,value])=>'<dt>'+esc(key)+'</dt><dd style="overflow-wrap:anywhere">'+esc(value)+'</dd>').join('')+'</dl><label><input type="checkbox" data-consent> I approve uploading this generated test and subtitles to this exact channel as Private.</label><div class="dv-actions"><button class="primary" data-upload disabled>Upload this Private test</button><button data-back>Back</button></div>');
  query('[data-consent]').onchange=()=>query('[data-upload]').disabled=!query('[data-consent]').checked;
  query('[data-back]').onclick=()=>attempt(()=>open(selected));
  query('[data-upload]').onclick=()=>attempt(async()=>{const ticket=epoch;query('[data-upload]').disabled=true;await request('upload',{review_token:prepared.review_token,approve:true});if(ticket===epoch)waitView(prepared.acceptance_id);});
 }
 function waitView(identity){show('<p>The reviewed transfer is in the normal processing queue. Wait for its video and subtitles to finish; retry this saved test rather than creating a duplicate upload.</p><div class="dv-actions"><button data-check>Check Google read-back</button><button data-queue>Open processing queue</button></div>');query('[data-check]').onclick=()=>attempt(()=>check(identity));query('[data-queue]').onclick=()=>attempt(async()=>{epoch++;dialog.close();await window.ForgeDubbing.openPublishingQueue();});}
 async function check(identity){
  message('Checking the saved transfer, exact channel, Private visibility and served subtitles…');
  const ticket=epoch,result=await request('acceptance/'+encodeURIComponent(identity)+'/verify',{});if(ticket!==epoch)return;
  if(result.status==='awaiting_private_review'){show('<p>'+esc(result.message)+'</p><button data-back>Return to preparation</button>');query('[data-back]').onclick=()=>attempt(()=>open(selected));return;}
  if(!result.export_ready){waitView(identity);message(result.message);return;}
  show('<p>'+esc(result.message)+'</p><p>Receipt identities are pseudonymized. It contains source and media hashes, not credentials, upload-session URLs, private paths or media.</p><button data-export>Download sanitized acceptance receipt</button><button data-back>Back</button>');
  query('[data-back]').onclick=()=>attempt(()=>open(selected));
  query('[data-export]').onclick=()=>attempt(async()=>{const exported=await request('acceptance/'+encodeURIComponent(identity)+'/export',{approve:true});const blob=new Blob([JSON.stringify(exported.receipt,null,2)+'\n'],{type:'application/json'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=exported.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message('Saved the sanitized receipt. The test video remains Private.');});
 }
 window.ForgePublishingAcceptance={open:(profileId)=>{ensure();return attempt(()=>open(profileId));}};
})();
