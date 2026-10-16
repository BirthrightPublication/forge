'use strict';
// Extends the established tool IDs; private previews bypass persistent jobs.
Object.assign(defaults.metadata,{cleaning_level:'maximum',remaining_report:false});
Object.assign(defaults.qr,{kind:'text',foreground:'#111111',background:'#ffffff',contact:{},wifi:{}});
let qrLocalURL='',qrLocalTimer;

function qrContentHTML(o){
 const c=o.contact||{},w=o.wifi||{};
 let content='';
 if(o.kind==='contact')content=['name','organization','phone','email','url','address','note'].map(k=>field(k==='name'?'Contact name':k[0].toUpperCase()+k.slice(1),'contact_'+k,c[k]||'',{full:['address','note'].includes(k)})).join('');
 else if(o.kind==='wifi')content=field('Network name (SSID)','wifi_ssid',w.ssid||'')+field('Network security','wifi_security',w.security||'WPA',{options:[['WPA','WPA / WPA2 / WPA3 personal'],['WEP','WEP · legacy'],['nopass','Open network']]})+field('Network password','wifi_password',w.password||'',{type:'password'})+`<div class="field">${check('Hidden network','wifi_hidden',w.hidden||false)}</div><div class="field full"><p class="note">Wi-Fi codes contain network credentials. Preview stays in memory and clears after 60 seconds. Download saves those credentials in the QR image; share it only with intended recipients.</p></div>`;
 else content=field(o.kind==='url'?'Website URL':'Text to encode','text',o.text,{type:'textarea',full:true,placeholder:o.kind==='url'?'https://example.com':'Enter content…'});
 return field('Content type','kind',o.kind,{options:[['text','Plain text'],['url','Website URL'],['contact','Contact card'],['wifi','Wi-Fi network']]})+content;
}
function qrStyleHTML(o){
 return field('Output name','name',o.name)+field('Output format','format',o.format,{options:[['png','PNG · image'],['svg','SVG · vector']]})+field('Error correction','error_correction',o.error_correction,{options:[['L','L · 7%'],['M','M · 15%'],['Q','Q · 25%'],['H','H · 30%']]})+field('Pixels per module','box_size',o.box_size,{type:'number',min:4,max:30})+field('Quiet zone (modules)','border',o.border,{type:'number',min:4,max:16})+field('Foreground color','foreground',o.foreground,{type:'color'})+field('Background color','background',o.background,{type:'color'});
}
function qrOptionsHTML(o){
 return `<div class="fields">${qrContentHTML(o)}${qrStyleHTML(o)}</div><p class="note">Use a dark foreground and light background. Verify readability on the devices that will scan your code. Queued text/contact content and saved presets remain in local history.</p><div class="inline"><button class="secondary" id="qr-local-preview">Preview locally</button><button class="secondary" id="qr-clear-preview">Clear</button><a class="secondary hidden" id="qr-save-preview" download>Download code</a></div><div id="qr-local-preview-area" aria-live="polite"></div><details class="qr-bulk"><summary>Bulk text / URL export</summary><div class="field"><label for="qr-bulk-lines">One text value or URL per line · up to 50</label><textarea id="qr-bulk-lines" placeholder="https://example.com/one&#10;https://example.com/two"></textarea></div><button class="secondary" id="qr-bulk-export">Download ZIP</button><p class="note">Uses the selected colors, size and format. Created in memory; downloaded files contain their encoded content.</p></details>`;
}
const mediaAdvancedOptionsBeforeV4=advancedOptions;
advancedOptions=function(tool,o){
 if(tool==='qr')return qrOptionsHTML({...defaults.qr,...o});
 let html=mediaAdvancedOptionsBeforeV4(tool,o);
 if(tool==='metadata')html=html.replace('<div class="fields">',`<div class="fields">${field('Privacy cleaning level','cleaning_level',o.cleaning_level||'maximum',{options:[['standard','Standard · retain color and subtitles'],['maximum','Maximum supported removal']]})}<div class="field full">${check('Export remaining-metadata report','remaining_report',o.remaining_report,'Reports fields observed after cleaning; remaining values may still be private.')}</div>`);
 return html;
};
const mediaOptionsBeforeV4=optionsHTML;
optionsHTML=function(){return view==='qr'?qrOptionsHTML(draft().options):mediaOptionsBeforeV4();};
const mediaCaptureBeforeV4=capture;
capture=function(){
 mediaCaptureBeforeV4();
 if(view!=='qr'||!$('#tool-options'))return;
 const o=draft().options;
 if(o.kind==='contact')o.contact=Object.fromEntries(['name','organization','phone','email','url','address','note'].map(k=>[k,$(`[name="contact_${k}"]`)?.value||'']));
 if(o.kind==='wifi')o.wifi={ssid:$('[name="wifi_ssid"]')?.value||'',password:$('[name="wifi_password"]')?.value||'',security:$('[name="wifi_security"]')?.value||'WPA',hidden:!!$('[name="wifi_hidden"]')?.checked};
 else o.wifi={};
};
function qrClearLocal(clearInputs=false){
 clearTimeout(qrLocalTimer);
 if(qrLocalURL)URL.revokeObjectURL(qrLocalURL);
 qrLocalURL='';
 $('#qr-local-preview-area')?.replaceChildren();
 const download=$('#qr-save-preview');if(download){download.removeAttribute('href');download.classList.add('hidden');}
 if(clearInputs){
  if(drafts.qr){drafts.qr.options.wifi={};if(/^\s*WIFI:/i.test(drafts.qr.options.text))drafts.qr.options.text='';}
  for(const key of ['wifi_ssid','wifi_password']){const e=$(`[name="${key}"]`);if(e)e.value='';}
  const text=$('[name="text"]');if(text&&/^\s*WIFI:/i.test(text.value))text.value='';
 }
}
async function qrPreviewLocal(){
 capture();const options=structuredClone(draft('qr').options);
 try{
  const response=await api('/qr/preview',options,true),blob=await response.blob();
  if(view!=='qr')return;
  qrClearLocal();qrLocalURL=URL.createObjectURL(blob);
  const image=document.createElement('img');image.src=qrLocalURL;image.alt='Locally generated QR code';image.className='qr-preview';image.style.maxWidth='240px';
  $('#qr-local-preview-area')?.append(image);
  const link=$('#qr-save-preview');if(link){link.href=qrLocalURL;link.download=(options.name||'QR Code').replace(/[<>:"/\\|?*\x00-\x1f]/g,'_')+'.'+options.format;link.classList.remove('hidden');}
  qrLocalTimer=setTimeout(()=>qrClearLocal(true),60000);
 }catch(e){notify(e.message,true);}
}
const mediaEnhanceBeforeV4=enhanceTool;
enhanceTool=function(){
 mediaEnhanceBeforeV4();
 if(view==='metadata'){
  const level=$('[name="cleaning_level"]');if(level){const update=()=>level.disabled=$('[name="mode"]')?.value!=='wipe';$('[name="mode"]')?.addEventListener('change',update);update();}
 }
 if(view!=='qr')return;
 $('[name="kind"]')?.addEventListener('change',()=>{capture();qrClearLocal(true);renderTool();});
 $('#qr-local-preview')?.addEventListener('click',qrPreviewLocal);
 $('#qr-clear-preview')?.addEventListener('click',()=>qrClearLocal(true));
 $('#qr-bulk-export')?.addEventListener('click',async()=>{
  capture();const o=draft().options;
  if(!['text','url'].includes(o.kind))return notify('Bulk export uses plain text or URL mode.',true);
  const lines=($('#qr-bulk-lines')?.value||'').split(/\r?\n/).filter(t=>t.trim());
  if(!lines.length||lines.length>50)return notify('Enter 1–50 values, one per line.',true);
  try{const response=await api('/qr/bulk-preview',{items:lines.map(text=>({...o,text,contact:{},wifi:{}}))},true);const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download='FORGE-QR-codes.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}catch(e){notify(e.message,true);}
 });
 if(draft().options.kind==='wifi'){
  for(const id of ['enqueue','enqueue-top']){const b=$('#'+id);if(b){b.innerHTML=icon('qr')+'Preview locally';b.onclick=qrPreviewLocal;}}
  if($('#output-preview'))$('#output-preview').textContent='Wi-Fi preview stays in memory. Use Download code to save explicitly through the browser.';
 }
};
const mediaEnqueueBeforeV4=enqueue;
enqueue=async function(){
 capture();if(view==='qr'&&(draft().options.kind==='wifi'||/^\s*WIFI:/i.test(draft().options.text)))return qrPreviewLocal();
 return mediaEnqueueBeforeV4();
};
const mediaNavigateBeforeV4=navigate;
navigate=function(next){const wasQR=view==='qr';const result=mediaNavigateBeforeV4(next);if(wasQR&&next!=='qr')qrClearLocal(true);return result;};
