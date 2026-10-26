'use strict';

Object.assign(paths,{
 copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
 edit:'<path d="M4 16L16 4l4 4L8 20H4zM14 6l4 4M4 20h16"/>'
});

// Stored operation options are immutable snapshots of a selected tool preset.
// The old view key remains available to bookmarks and existing navigation hooks.
let blackScript = null, blackEditor = null, blackChoosing = false;
let blackSaved = [], blackLoaded = false, blackLoading = false;
const blackTools = ['retune', 'metadata', 'watermark', 'audio_fingerprint', 'video_fingerprint', 'tracking_audit', 'privacy_export', 'provenance', 'privacy_verify', 'transcribe'];
const blackId = () => 'n-' + crypto.randomUUID().replaceAll('-', '');

function blackEmpty() {
 return {schema_version:1,id:blackId(),name:'Untitled script',nodes:[],connections:[],output:structuredClone(draft('workflows').output)};
}
function blackConnect(script) {
 script.connections=script.nodes.slice(1).map((node,i)=>({source:script.nodes[i].id,target:node.id}));
 return script;
}
function blackButton(id,label,symbol,extra='') {
 return `<button type="button" class="secondary black-icon-action" id="${id}" title="${escape(label)}" aria-label="${escape(label)}" ${extra}>${symbol}</button>`;
}
function blackCaptureOptions() {
 const root=$('#black-node-options');if(!root||!blackEditor)return;
 const options=blackEditor.node.options;
 if(window.ForgePrivacy?.captureOptions&&['watermark','audio_fingerprint','video_fingerprint','tracking_audit','privacy_export','provenance','privacy_verify'].includes(blackEditor.node.tool)){
  window.ForgePrivacy.captureOptions(root,blackEditor.node.tool,options);return;
 }
 Object.entries(options).forEach(([key,old])=>{
  if(['formats','remove_tags'].includes(key)){options[key]=$$(`input[name="${key}"]:checked`,root).map(el=>el.value);return;}
  if(key==='tags'){options.tags=Object.fromEntries($$('[data-meta-tag]',root).filter(el=>el.value).map(el=>[el.dataset.metaTag,el.value]));return;}
  const el=$(`[name="${key}"]`,root);if(!el)return;
  options[key]=el.type==='checkbox'?el.checked:typeof old==='number'?Number(el.value):el.value;
 });
}
captureWorkflow=function() {
 if(!blackScript)return;
 if($('#black-name'))blackScript.name=$('#black-name').value;
 const out=draft('workflows').output;
 if($('#out-mode'))out.mode=$('#out-mode').value;
 if($('#out-path'))out.directory=$('#out-path').value;
 if($('#out-subfolder'))out.subfolder=$('#out-subfolder').value;
 blackScript.output=structuredClone(out);blackCaptureOptions();
};
function blackSpec() {captureWorkflow();return blackConnect(structuredClone(blackScript));}
function blackRequest() {return {script:blackSpec(),sources:draft('workflows').sources.map(source=>source.id)};}

async function blackLoadSaved() {
 if(blackLoading)return;blackLoading=true;
 try{const result=await api('/black-scripting/scripts');blackSaved=result.scripts;blackLoaded=true;blackUpdateSaved();if(result.warnings.length)notify(result.warnings[0],true);}
 catch(error){notify(error.message,true);}finally{blackLoading=false;}
}
function blackUpdateSaved() {
 const select=$('#black-presets');if(!select)return;
 select.innerHTML='<option value="">Load a script preset…</option>'+blackSaved.map(script=>`<option value="${escape(script.id)}">${escape(script.name)}</option>`).join('');
 if(blackSaved.some(script=>script.id===blackScript?.id))select.value=blackScript.id;
}
function blackSourceHTML() {
 return `<div class="black-source"><div id="dropzone" class="dropzone" tabindex="0" role="button" aria-label="Source files: drop files or press Enter for native file selection"><div class="drop-icon">${icon('upload')}</div><strong>Bring source files to the workbench</strong><p>Audio, video or images · originals are preserved</p><div class="actions"><button class="secondary" id="native-browse">${icon('folder')}Native browse</button><button class="secondary" id="paste-paths">Paste paths</button></div></div><input id="browser-files" type="file" multiple hidden><div class="black-source-actions"><button class="text-button" id="browser-browse">Import through browser</button><button class="text-button" id="add-folder">Add folder</button></div><p class="note">Native browse reads original paths. Browser imports create a staged copy.</p><div id="upload-status" role="status"></div><div id="file-list" class="file-list"></div></div>`;
}
function blackOutputHTML() {
 const output=draft('workflows').output;
 return `<section class="black-output" aria-label="Output destination"><div class="black-output-title">${icon('folder')}<strong>Output destination</strong></div><div class="black-output-fields"><label class="field">Save results to<select id="out-mode">${[['source','Beside each original'],['custom','Custom folder'],['last','Last custom folder'],['default','FORGE export folder']].map(([value,label])=>`<option value="${value}" ${output.mode===value?'selected':''}>${label}</option>`).join('')}</select></label><div class="field ${output.mode==='custom'?'':'hidden'}" id="out-path-wrap"><label for="out-path">Directory</label><div class="path-input"><input id="out-path" value="${escape(output.directory)}" placeholder="Choose a local folder"><button class="secondary" id="choose-output" title="Choose output folder" aria-label="Choose output folder">${icon('folder')}</button></div></div><label class="field">Subfolder<input id="out-subfolder" maxlength="80" value="${escape(output.subfolder)}" placeholder="Optional"></label></div><div id="output-preview" class="note"></div><p class="note">Transformations save separate media. Inspections pass media unchanged and save private reports only when enabled. Existing names receive a numbered suffix.</p></section>`;
}
function blackNodeHTML(node,index) {
 const selected=blackEditor?.node.id===node.id;
 return `<div class="black-link ${node.enabled?'':'is-disabled'}"><button class="black-node ${selected?'is-selected':''}" data-black-node="${index}" title="Edit ${escape(titles[node.tool])}${node.enabled?'':' (disabled)'}" aria-label="Operation ${index+1}: ${escape(titles[node.tool])}${node.enabled?'':' — disabled'}" aria-pressed="${selected}">${icon(node.tool)}<span class="black-node-number">${index+1}</span>${node.enabled?'':'<span class="black-disabled-mark" aria-hidden="true">Ⅱ</span>'}</button><span class="black-node-label">${escape(titles[node.tool])}</span><small>${node.enabled?(node.preset_name?escape(node.preset_name):'Custom settings'):'Disabled'}</small></div>`;
}
function blackEditorHTML() {
 if(blackChoosing)return `<section class="black-selector" aria-label="Choose an operation"><h2>Forge the next link</h2><div>${blackTools.map(tool=>`<button class="secondary black-operation" data-black-tool="${tool}">${icon(tool)}<span>${escape(titles[tool])}</span></button>`).join('')}</div><p class="note">Media flows from left to right. Transcribe finishes the processing chain.</p></section>`;
 if(!blackEditor)return '<div class="black-editor-empty">Select + to add an operation, or select an existing node to edit it.</div>';
 const {node,index}=blackEditor;
 return `<section class="black-editor" aria-label="Operation settings"><header><div class="black-editor-heading">${icon(node.tool)}<h2>${escape(titles[node.tool])}</h2><span>${index===null?'New operation':`Operation ${index+1}`}</span></div><div class="black-editor-actions">${index!==null?blackButton('black-left','Move operation left','←',index===0?'disabled':'')+blackButton('black-right','Move operation right','→',index===blackScript.nodes.length-1?'disabled':'')+blackButton('black-duplicate-node','Duplicate operation',icon('copy'))+blackButton('black-toggle-node',node.enabled?'Disable operation':'Enable operation',icon(node.enabled?'pause':'play'))+blackButton('black-remove-node','Remove operation','×'):''}<button class="secondary" id="black-cancel-node">Cancel</button><button class="primary" id="black-confirm-node">${index===null?'Add operation':'Apply changes'}</button></div></header><div class="black-preset-row"><label for="black-tool-preset">Tool preset</label><select id="black-tool-preset"><option value="">Current settings</option><option value="__defaults__">Built-in defaults</option>${state.presets.filter(preset=>preset.tool===node.tool).map(preset=>`<option value="${escape(preset.name)}">${escape(preset.name)}</option>`).join('')}</select><small>Settings are copied into this script. Later preset edits will not change it.</small></div><div id="black-node-options">${advancedOptions(node.tool,node.options)}</div><p class="black-node-error" id="black-node-error" role="alert"></p></section>`;
}

renderWorkflow=function() {
 if(!blackScript)blackScript=blackEmpty();
 const count=blackScript.nodes.length;
 $('#main').innerHTML=`<section class="black-workbench"><header class="black-workbench-header"><div><div class="eyebrow">ENTER THE FORGE</div><h1>Black Scripting</h1></div><div class="black-workbench-actions"><button class="secondary" id="black-back">← Studio</button><button class="secondary" id="black-new">New script</button><button class="primary" id="black-preview">${icon('play')}Preview execution</button></div></header><div class="black-script-toolbar"><label class="field black-name-field">Script name<input id="black-name" maxlength="80" value="${escape(blackScript.name)}"></label><label class="field">Saved scripts<select id="black-presets" aria-label="Load Black Scripting preset"><option>Loading presets…</option></select></label><button class="secondary" id="black-save">Save preset</button>${blackButton('black-rename','Rename script',icon('edit'))}${blackButton('black-copy','Duplicate script',icon('copy'))}${blackButton('black-import','Import script definition',icon('upload'))}${blackButton('black-export','Export script definition',icon('export'))}<input id="black-import-file" type="file" accept="application/json,.json" hidden></div>${blackSourceHTML()}<div class="black-chain-caption"><span>LINEAR PRODUCTION CHAIN</span><div>${blackButton('black-pan-left','Scroll chain left','←')}${blackButton('black-pan-right','Scroll chain right','→')}</div></div><div class="black-chain" tabindex="0" aria-label="Linear operation chain, scroll horizontally for more operations">${blackScript.nodes.map((node,index)=>blackNodeHTML(node,index)+'<span class="black-connector" aria-hidden="true">⟶</span>').join('')}<div class="black-link"><button class="black-plus" id="black-add-node" title="Add operation" aria-label="Add operation" ${count>=24?'disabled':''}>+</button><span class="black-node-label">${count?'Add operation':'Forge the first link'}</span></div>${count?'<span class="black-connector" aria-hidden="true">⟶</span><button class="black-export-node" id="black-output-node" title="Configure output destination" aria-label="Configure output destination">'+icon('folder')+'<span>Output</span></button>':''}</div>${blackEditorHTML()}${blackOutputHTML()}<footer class="black-workbench-footer"><span>${count} operation${count===1?'':'s'} · ${blackScript.nodes.filter(node=>node.enabled).length} enabled</span><button class="primary" id="black-preview-bottom">Preview & add to queue ${icon('play')}</button></footer></section>`;
 blackUpdateSaved();if(!blackLoaded)blackLoadSaved();
 bindFiles();fileList();updateOutputPreview();
 $('#black-back').onclick=()=>window.ForgeShell?window.ForgeShell.exitBlack():navigate('downloads');
 $('#black-new').onclick=()=>{captureWorkflow();blackScript=blackEmpty();blackEditor=null;blackChoosing=false;renderWorkflow();};
 $('#black-save').onclick=blackSave;
 $('#black-rename').onclick=async()=>{if(blackEditor)return notify('Apply or cancel the open operation settings before renaming.',true);captureWorkflow();const name=prompt('Rename this Black Scripting preset:',blackScript.name);if(!name?.trim())return;blackScript.name=name.trim();renderWorkflow();await blackSave();};
 $('#black-copy').onclick=async()=>{if(blackEditor)return notify('Apply or cancel the open operation settings before duplicating.',true);captureWorkflow();const name=prompt('Name the duplicate script:',blackScript.name+' copy');if(!name?.trim())return;blackScript.id=blackId();blackScript.name=name.trim();renderWorkflow();await blackSave();};
 $('#black-presets').onchange=event=>{const saved=blackSaved.find(script=>script.id===event.target.value);if(!saved)return;blackScript=structuredClone(saved);draft('workflows').output=structuredClone(saved.output);blackEditor=null;blackChoosing=false;renderWorkflow();};
 $('#black-import').onclick=()=>$('#black-import-file').click();
 $('#black-import-file').onchange=blackImport;
 $('#black-export').onclick=blackExport;
 $('#black-add-node').onclick=()=>{captureWorkflow();blackEditor=null;blackChoosing=true;renderWorkflow();$('.black-selector button')?.focus();};
 $$('[data-black-tool]').forEach(button=>button.onclick=()=>{blackChoosing=false;blackEditor={index:null,node:{id:blackId(),type:'operation',node_version:1,tool:button.dataset.blackTool,enabled:true,options:structuredClone(defaults[button.dataset.blackTool]),preset_name:''}};renderWorkflow();$('#black-tool-preset')?.focus();});
 $$('[data-black-node]').forEach(button=>button.onclick=()=>{captureWorkflow();const index=Number(button.dataset.blackNode);blackChoosing=false;blackEditor={index,node:structuredClone(blackScript.nodes[index])};renderWorkflow();$('#black-tool-preset')?.focus();});
 $('#black-pan-left').onclick=()=>$('.black-chain').scrollBy({left:-300,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
 $('#black-pan-right').onclick=()=>$('.black-chain').scrollBy({left:300,behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
 $('#black-output-node')?.addEventListener('click',()=>$('#out-mode').focus());
 $('#out-mode').onchange=()=>{captureWorkflow();$('#out-path-wrap').classList.toggle('hidden',draft('workflows').output.mode!=='custom');updateOutputPreview();};
 $('#out-path').oninput=$('#out-subfolder').oninput=()=>{captureWorkflow();updateOutputPreview();};
 $('#choose-output').onclick=async()=>{try{const result=await nativePick('folder');if(result.paths?.length){$('#out-path').value=result.paths[0];captureWorkflow();updateOutputPreview();}}catch(error){notify(error.message,true);}};
 $('#black-preview').onclick=$('#black-preview-bottom').onclick=blackPreview;
 $('#black-name').oninput=()=>{blackScript.name=$('#black-name').value;};
 $('#dropzone').onkeydown=event=>{if((event.key==='Enter'||event.key===' ')&&event.target===$('#dropzone')){event.preventDefault();$('#native-browse').click();}};
 blackBindEditor();
};

function blackBindEditor() {
 if(!blackEditor)return;
 if(window.ForgeUI?.compactOptions)window.ForgeUI.compactOptions($('#black-node-options'),blackEditor.node.tool,{key:'black-'+blackEditor.node.id});
 if(window.ForgePrivacy?.bindOptions)window.ForgePrivacy.bindOptions($('#black-node-options'),blackEditor.node.tool,blackEditor.node.options,{onChange:()=>blackCaptureOptions()});
 $('#black-tool-preset').onchange=event=>{blackCaptureOptions();const value=event.target.value;const preset=state.presets.find(item=>item.tool===blackEditor.node.tool&&item.name===value);if(value==='__defaults__'){blackEditor.node.options=structuredClone(defaults[blackEditor.node.tool]);blackEditor.node.preset_name='';}else if(preset){blackEditor.node.options={...structuredClone(defaults[blackEditor.node.tool]),...structuredClone(preset.options)};blackEditor.node.preset_name=preset.name;}renderWorkflow();};
 $('#black-cancel-node').onclick=()=>{blackEditor=null;renderWorkflow();};
 $('#black-confirm-node').onclick=async()=>{
  captureWorkflow();const candidate=blackSpec();const editor=blackEditor;
  if(editor.index===null)candidate.nodes.push(structuredClone(editor.node));else candidate.nodes[editor.index]=structuredClone(editor.node);
  try{const result=await api('/black-scripting/export',blackConnect(candidate));blackScript=result.definition;blackEditor=null;renderWorkflow();notify('Operation settings applied.');}
  catch(error){if($('#black-node-error'))$('#black-node-error').textContent=error.message;}
 };
 for(const [id,delta] of [['black-left',-1],['black-right',1]])$('#'+id)?.addEventListener('click',()=>{captureWorkflow();const index=blackEditor.index;blackScript.nodes[index]=structuredClone(blackEditor.node);[blackScript.nodes[index],blackScript.nodes[index+delta]]=[blackScript.nodes[index+delta],blackScript.nodes[index]];blackEditor.index+=delta;blackConnect(blackScript);renderWorkflow();});
 $('#black-duplicate-node')?.addEventListener('click',()=>{if(blackScript.nodes.length>=24)return notify('A script supports up to 24 operations.',true);captureWorkflow();const node=structuredClone(blackEditor.node);node.id=blackId();blackScript.nodes.splice(blackEditor.index+1,0,node);blackEditor={index:blackEditor.index+1,node:structuredClone(node)};blackConnect(blackScript);renderWorkflow();});
 $('#black-toggle-node')?.addEventListener('click',()=>{captureWorkflow();blackEditor.node.enabled=!blackEditor.node.enabled;blackScript.nodes[blackEditor.index]=structuredClone(blackEditor.node);renderWorkflow();});
 $('#black-remove-node')?.addEventListener('click',()=>{blackScript.nodes.splice(blackEditor.index,1);blackEditor=null;blackConnect(blackScript);renderWorkflow();});
 $('#pick-diarization')?.addEventListener('click',async()=>{try{const result=await nativePick('folder');if(result.paths?.length)$('[name="diarization_model"]',$('#black-node-options')).value=result.paths[0];}catch(error){notify(error.message,true);}});
 $('#metadata-inspect')?.addEventListener('click',async()=>{const source=draft('workflows').sources[0];if(!source)return notify('Add a source file first.',true);try{const result=await api('/metadata/inspect',{source:source.id});$('#dialog-content').innerHTML='<h2>Source metadata inspection</h2><p>'+escape(result.note||'Current source file; downstream output does not exist until processing.')+'</p><pre>'+escape(JSON.stringify(result.metadata,null,2))+'</pre>';$('#dialog').showModal();}catch(error){notify(error.message,true);}});
}
async function blackSave() {
 if(blackEditor)return notify('Apply or cancel the open operation settings before saving.',true);
 try{const result=await api('/black-scripting/save',blackSpec());blackScript=result.script;blackSaved=result.scripts;blackUpdateSaved();notify('Black Scripting preset saved locally.');}catch(error){notify(error.message,true);}
}
async function blackImport(event) {
 const file=event.target.files[0];if(!file)return;
 if(file.size>512000)return notify('Choose a JSON script definition smaller than 512 KB.',true);
 try{const definition=JSON.parse(await file.text());const result=await api('/black-scripting/import',{definition});blackScript=result.script;blackSaved=result.scripts;draft('workflows').output=structuredClone(blackScript.output);blackEditor=null;blackChoosing=false;renderWorkflow();notify('Script imported as a new preset.');}catch(error){notify('Import failed: '+error.message,true);}
}
async function blackExport() {
 if(blackEditor)return notify('Apply or cancel the open operation settings before exporting.',true);
 try{const result=await api('/black-scripting/export',blackSpec());const url=URL.createObjectURL(new Blob([JSON.stringify(result.definition,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download=(blackScript.name.replace(/[^a-zA-Z0-9 _-]/g,'_')||'Black Scripting')+'.forge-script.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notify('Configuration exported, including any configured local folder paths.');}catch(error){notify(error.message,true);}
}
async function blackPreview() {
 if(blackEditor)return notify('Apply or cancel the open operation settings before previewing.',true);
 const request=blackRequest();
 try{
  const result=await api('/black-scripting/preview',request);
  $('#dialog-content').innerHTML=`<h2>Black Scripting execution plan</h2><p>${escape(result.name)} · ${result.sources.length} source${result.sources.length===1?'':'s'} · ${result.jobs} queued operation${result.jobs===1?'':'s'}</p><div class="black-plan">${result.sources.map(source=>`<section><h3>${escape(source.name)}</h3><ol>${source.steps.map(step=>`<li><strong>${escape(step.title)}</strong><span>${escape(step.input_format)} → ${escape(step.output_formats.join(', '))}</span><small>Input: ${step.depends_on==='source'?'original source':'media from operation '+(step.step-1)}</small>${step.passthrough?'<small>Inspection: media passes through unchanged.</small>':''}${(step.disclosures||[]).map(note=>'<small>'+escape(note)+'</small>').join('')}</li>`).join('')}</ol><p>Destination: ${escape(source.destination)}</p></section>`).join('')}</div>${result.disabled.length?`<p>${result.disabled.length} disabled operation${result.disabled.length===1?' is':'s are'} skipped.</p>`:''}<p class="note">${escape(result.note)}</p><div class="black-plan-actions"><button class="secondary" id="black-plan-back">Back to workbench</button><button class="primary" id="black-plan-queue">${icon('queue')}Add to queue</button><button class="primary" id="black-plan-start">${icon('play')}Start</button></div>${state.settings.queue_paused?'<p class="note">Dispatch is paused. Start resumes dispatch for the universal queue.</p>':''}`;
  $('#dialog').showModal();$('#black-plan-back').onclick=()=>$('#dialog').close();
  const submit=async start=>{const queueButton=$('#black-plan-queue'),startButton=$('#black-plan-start');queueButton.disabled=startButton.disabled=true;try{const response=await api('/black-scripting/run',{...request,preview_token:result.preview_token});if(start&&state.settings.queue_paused)await api('/queue',{paused:false});$('#dialog').close();await refresh(false);navigate('queue');notify(`${response.jobs.length} Black Scripting operations queued.`);}catch(error){notify(error.message,true);queueButton.disabled=startButton.disabled=false;}};
  $('#black-plan-queue').onclick=()=>submit(false);$('#black-plan-start').onclick=()=>submit(true);
 }catch(error){notify(error.message,true);}
}
