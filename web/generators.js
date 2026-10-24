/* Shared offline generator workspace. Generated values exist only in this closure
   and the current page; presets contain validated options, never result rows. */
(() => {
 'use strict';
 const ids = ['identity','obfuscation','addresses','strings'];
 const names = {identity:'Identity Studio',obfuscation:'Obfuscation Studio',addresses:'Random Address Generator',strings:'Random String Generator'};
 Object.assign(titles,names);
 Object.assign(paths,{
  identity:'<circle cx="12" cy="8" r="4"/><path d="M4 22v-3a8 8 0 0116 0v3M3 3h3M18 3h3"/>',
  obfuscation:'<rect x="2" y="6" width="20" height="12" rx="3"/><path d="M6 10v4M10 10v4M14 10v4M18 10v4"/>',
  addresses:'<path d="M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1116 0z"/><circle cx="12" cy="10" r="3"/>',
  strings:'<path d="M8 6L2 12l6 6M16 6l6 6-6 6M14 3l-4 18"/>'
 });
 ids.forEach(id=>{defaults[id]={};});
 const originalReady=ready;
 ready=function(tool){return ids.includes(tool)||originalReady(tool);};
 const labels={count:'Batch size',kind:'Generate',region:'Region inspiration',convention:'Name order',separator:'Separator',suffix_digits:'Suffix digits',length:'Length',lower:'Lowercase letters',upper:'Uppercase letters',numbers:'Digits',symbols:'Allowed symbols',exclude:'Exclude characters',exclude_ambiguous:'Exclude ambiguous I l 1 O 0 o',alphabet:'Alphabet',custom_chars:'Custom alphabet',domain:'Reserved email domain',pattern:'Identifier pattern',country:'Country format',include_unit:'Include apartment / unit',include_county:'Include county',include_phone:'Include synthetic test phone',fields:'Address fields'};
 const values={legal:'Legal-style fictional name',pseudonym:'Online pseudonym',normie:'Ordinary / Normie alias',international:'International',anglophone:'Anglophone',hispanic:'Hispanic',japanese:'Japanese',given_family:'Given · Family',family_given:'Family · Given',initial_family:'Initial · Family',password:'Password',username:'Username',localpart:'Email local part',email:'Synthetic email',token:'Token',identifier:'Random identifier',plate:'Fictional plate-like',pattern:'Custom pattern',policy:'Character policy',hex:'Hexadecimal',base64url:'URL-safe base64 alphabet',custom:'Custom alphabet',US:'United States',GB:'United Kingdom',CA:'Canada',DE:'Germany',AU:'Australia',JP:'Japan',street:'Street',unit:'Unit',city:'City',region:'Region',postal_code:'Postal code',country:'Country',' ':'Space','_':'Underscore','-':'Hyphen','.':'Period'};
 const groups={
  identity:[['identity','Naming','identity',['kind','region','convention']],['style','Style','settings',['separator','suffix_digits']]],
  obfuscation:[['pattern','Identifier','obfuscation',['kind','pattern','length']],['policy','Character policy','password',['lower','upper','numbers','symbols']],['exclusions','Exclusions','metadata',['exclude_ambiguous','exclude']]],
  addresses:[['country','Country format','addresses',['country','include_unit','include_county','include_phone']],['fields','Address fields','file',['fields']]],
  strings:[['type','String type','strings',['kind','length','domain']],['alphabet','Alphabet','password',['alphabet','lower','upper','numbers','symbols','custom_chars']],['exclusions','Exclusions','metadata',['exclude_ambiguous','exclude']]]
 };
 let schema=null,presets=[],result=null,resultTimer=null,renderEpoch=0,requestEpoch=0,addressPage=0,addressSaved=null;
 let addressLocks=new Map();
 const cacheKey=id=>'generator-'+id;
 function clearValues(message='No values generated.'){
  result=null;requestEpoch++;clearTimeout(resultTimer);
  const output=$('#generator-output');if(output)output.textContent=message;
  const status=$('#generator-result-status');if(status)status.textContent='Memory only';
  $$('[data-generator-export],#generator-copy,#generator-clear').forEach(button=>button.disabled=true);
  const generateButton=$('#generator-generate');if(generateButton)generateButton.disabled=false;
 }
 function captureGenerator(){
  if(!ids.includes(view)||!$('#generator-options'))return;
  const options=draft().options,root=$('#generator-workspace');
  for(const [key,property] of Object.entries(schema.tools[view].schema.properties)){
   if(property.type==='array'){if(!$$(`[name="${key}"]`,root).length)continue;options[key]=$$(`[name="${key}"]:checked`,root).map(input=>input.value);continue;}
   const input=$(`[name="${key}"]`,root);if(!input)continue;
   options[key]=property.type==='boolean'?input.checked:property.type==='integer'?Number(input.value):input.value;
  }
 }
 const previousCapture=capture;
 capture=function(){if(ids.includes(view)){captureGenerator();return;}return previousCapture();};
 const previousNavigate=navigate;
 navigate=function(next){if(view==='addresses'&&result)addressSaved={result,locks:addressLocks,page:addressPage};clearValues();renderEpoch++;return previousNavigate(next);};
 document.addEventListener('visibilitychange',()=>{if(document.hidden&&view!=='addresses')clearValues('Values cleared when this tab was hidden.');});
 window.addEventListener('pagehide',()=>{addressSaved=null;clearValues();});
 $('#search')?.addEventListener('input',()=>{clearValues();renderEpoch++;},true);

 function optionField(key,property,options){
  const label=labels[key]||key,value=options[key];
  if(property.type==='boolean')return `<div class="generator-field" data-generator-field="${key}">${check(label,key,value)}</div>`;
  if(property.type==='array')return `<fieldset class="generator-field generator-field-wide" data-generator-field="${key}"><legend>${escape(label)}</legend><div class="generator-checks">${property.items.enum.map(item=>`<label class="checkbox"><input name="${key}" type="checkbox" value="${item}" ${value.includes(item)?'checked':''}>${escape(values[item]||item)}</label>`).join('')}</div></fieldset>`;
  const enumOptions=property.enum?.map(item=>[item,values[item]||item]);
  return `<div class="generator-field ${['pattern','symbols','exclude','custom_chars'].includes(key)?'generator-field-wide':''}" data-generator-field="${key}">${field(label,key,value,{type:property.type==='integer'?'number':'text',options:enumOptions,min:property.minimum??null,max:property.maximum??null})}</div>`;
 }
 function visibleFields(){
  const options=draft().options,pattern=options.kind==='plate'||options.kind==='pattern',portable=['email','localpart','username'].includes(options.kind);
  const show=(key,condition)=>{const element=$(`[data-generator-field="${key}"]`);if(element)element.hidden=!condition;};
  if(view==='obfuscation'){
   show('pattern',pattern);show('length',!pattern);
   for(const key of ['lower','upper','numbers','symbols'])show(key,!pattern&&(key!=='symbols'||!portable));
  }
  if(view==='strings'){
   show('domain',options.kind==='email');show('custom_chars',options.alphabet==='custom');
   for(const key of ['lower','upper','numbers','symbols'])show(key,options.alphabet==='policy'&&(key!=='symbols'||!portable));
  }
 }
 function presetOptions(tool){return '<option value="">Load a preset…</option>'+presets.filter(p=>p.tool===tool).map(p=>`<option value="${escape(p.id)}">${escape(p.name)}</option>`).join('');}
 const addressLabels={street:'Street Address 1',unit:'Street Address 2 / Apt / Unit',city:'City',region:'State / Region / Province',postal_code:'ZIP / Postal Code',country:'Country',county:'County',phone:'Synthetic test phone'};
 const countryCodes={'United States':'US','United Kingdom':'GB','Canada':'CA','Australia':'AU','Germany':'DE','Japan':'JP'};
 paths.lock='<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 018 0v4M12 14v3"/>';
 function locksFor(index){if(!addressLocks.has(index))addressLocks.set(index,new Set(draft('addresses').options.locked_fields||[]));return addressLocks.get(index);}
 async function copyAddress(value){try{await navigator.clipboard.writeText(value);notify('Copied to your system clipboard.');}catch{notify('Clipboard unavailable. Select the field and copy manually.',true);}}
 function renderAddressResult(){
  const output=$('#generator-output');output.replaceChildren();
  const list=document.createElement('ol');list.className='generator-values address-values';
  result.rows.slice(addressPage*10,addressPage*10+10).forEach((row,offset)=>{
   const index=addressPage*10+offset,item=document.createElement('li'),grid=document.createElement('div');item.className='address-record';grid.className='address-grid';
   for(const [key,label] of Object.entries(addressLabels)){
    if(!(key in row))continue;
    const field=document.createElement('div');field.className='address-field';
    const name=document.createElement('label'),input=document.createElement('input'),controls=document.createElement('div');
    input.id='address-'+index+'-'+key;input.value=row[key];input.readOnly=true;input.autocomplete='off';name.htmlFor=input.id;name.textContent=label;controls.className='address-field-controls';controls.append(input);
    const button=(text,symbol,handler)=>{const b=document.createElement('button');b.type='button';b.title=text;b.setAttribute('aria-label',text);b.innerHTML=icon(symbol);b.onclick=handler;controls.append(b);return b;};
    button('Copy '+label,'copy',()=>copyAddress(input.value));
    const regenerate=button('Regenerate '+label,'refresh',()=>regenerateAddresses(index,key));regenerate.disabled=locksFor(index).has(key);
    const lock=button('Lock '+label,'lock',()=>{const set=locksFor(index);if(set.has(key))set.delete(key);else set.add(key);draft('addresses').options.locked_fields=[...set];renderAddressResult();});lock.setAttribute('aria-pressed',String(locksFor(index).has(key)));
    field.append(name,controls);grid.append(field);
   }
   const actions=document.createElement('div');actions.className='address-actions';
   for(const [label,handler] of [['Copy Full Address',()=>copyAddress(row.value)],['Copy All Fields',()=>copyAddress(Object.keys(addressLabels).filter(k=>k in row).map(k=>addressLabels[k]+': '+row[k]).join('\n'))],['Regenerate All',()=>regenerateAddresses(index)]]){
    const b=document.createElement('button');b.className='secondary';b.textContent=label;b.onclick=handler;actions.append(b);
   }
   item.append(grid,actions);list.append(item);
  });output.append(list);
  if(result.rows.length>10){const controls=document.createElement('div');controls.className='address-actions';for(const [label,delta] of [['Previous',-1],['Next',1]]){const b=document.createElement('button');b.textContent=label;b.className='secondary';b.disabled=addressPage+delta<0||(addressPage+delta)*10>=result.rows.length;b.onclick=()=>{addressPage+=delta;renderAddressResult();};controls.append(b);}output.append(controls);}
  $('#generator-result-status').textContent=`${result.count} synthetic address${result.count===1?'':'es'} · memory only`;
  $('#generator-generate').textContent='Regenerate All';$('#generator-copy').textContent='Copy Full Address';
  $$('[data-generator-export],#generator-copy,#generator-clear').forEach(b=>b.disabled=false);
 }
 async function regenerateAddresses(index=null,target=null){
  captureGenerator();if(!result||view!=='addresses')return;
  if(draft().options.count!==result.rows.length){notify('Clear this batch before changing its size; existing locked fields are preserved until then.',true);return;}
  const request=++requestEpoch,snapshot=result,indices=index===null?result.rows.map((_,i)=>i):[index];
  $$('#generator-output button,#generator-generate').forEach(b=>b.disabled=true);
  try{
   const items=indices.map(i=>({options:{...draft().options,...(target?{country:countryCodes[snapshot.rows[i].country]||draft().options.country}:{}),count:1},
    current:Object.fromEntries(Object.keys(addressLabels).filter(k=>k in snapshot.rows[i]).map(k=>[k,snapshot.rows[i][k]])),locked:[...locksFor(i)],field:target}));
   const response=await api('/generators/addresses/regenerate-batch',{items});
   if(request!==requestEpoch||view!=='addresses')return;
   const rows=[...snapshot.rows];response.rows.forEach((row,j)=>{rows[indices[j]]=row;});result={...snapshot,rows,columns:[...new Set(rows.flatMap(r=>Object.keys(r)))]};
   if(target==='country'&&rows.length===1){draft().options.country=countryCodes[rows[0].country];$('[name="country"]').value=draft().options.country;}
   if(['city','region','postal_code','country','county'].includes(target))notify('Related location fields updated together; locked fields kept.');
  }catch(error){if(request===requestEpoch)notify(error.message,true);}
  finally{if(request===requestEpoch&&view==='addresses'){renderAddressResult();$('#generator-generate').disabled=false;}}
 }
 function renderResult(){
  if(!result)return;
  if(result.tool==='addresses'){renderAddressResult();return;}
  const output=$('#generator-output');output.replaceChildren();
  const list=document.createElement('ol');list.className='generator-values';
  // Avoid inserting output through HTML, attributes, browser storage or logs.
  result.rows.forEach(row=>{const item=document.createElement('li');item.textContent=row.value;list.append(item);});
  output.append(list);
  $('#generator-result-status').textContent=`${result.count} values · clears in 60 seconds`;
  $$('[data-generator-export],#generator-copy,#generator-clear').forEach(button=>button.disabled=false);
 }
 function spreadsheetCell(value){
  let text=String(value??'');
  if(/^[\s]*[=+\-@]/u.test(text)||/^[\t\r\n]/u.test(text))text="'"+text;
  return '"'+text.replace(/"/g,'""')+'"';
 }
 function serialize(data,format){
  if(format==='json')return JSON.stringify(data,null,2)+'\n';
  if(format==='txt')return (data.fictional?data.notice+'\n':'')+data.rows.map(row=>row.value).join('\n')+'\n';
  if(format!=='csv')throw new Error('Choose JSON, CSV or TXT.');
  return [data.columns.map(spreadsheetCell).join(','),...data.rows.map(row=>data.columns.map(key=>spreadsheetCell(row[key])).join(','))].join('\r\n')+'\r\n';
 }
 function exportCurrent(format){
  if(!result)return;
  const content=serialize(result,format),mime={json:'application/json',csv:'text/csv',txt:'text/plain'}[format];
  const url=URL.createObjectURL(new Blob([content],{type:mime+';charset=utf-8'}));
  const link=document.createElement('a');link.href=url;link.download=`FORGE-${result.tool}.${format}`;document.body.append(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
 }
 async function savePreset(existing=false){
  captureGenerator();const id=$('#generator-presets').value,previous=presets.find(p=>p.id===id);
  if(existing&&!previous){notify('Load a preset to rename it.',true);return;}
  const name=prompt(existing?'Rename this generator preset:':'Save these options as a preset:',existing?previous.name:'');if(!name?.trim())return;
  try{const response=await api('/generators/presets',{tool:view,name:name.trim(),options:draft().options,...(existing?{id}: {})});presets=response.presets;
   $('#generator-presets').innerHTML=presetOptions(view);$('#generator-presets').value=response.preset.id;notify('Generator options saved locally.');
  }catch(error){notify(error.message,true);}
 }
 async function renderGenerator(optionsOverride=null){
  const tool=view,epoch=++renderEpoch;clearValues();
  $('#main').innerHTML=heading('FORGE OS',names[tool],'Local generator')+'<p class="note" role="status">Loading generator settings…</p>';
  try{
   const loaded=await Promise.all([schema?Promise.resolve(schema):api('/generators/catalog'),api('/generators/presets')]);
   if(epoch!==renderEpoch||view!==tool)return;
   schema=loaded[0];presets=loaded[1].presets;
   defaults[tool]=structuredClone(schema.tools[tool].defaults);if(tool==='addresses')defaults[tool].count=1;
   const d=draft(tool);if(tool==='addresses'&&!('count' in d.options))d.options.count=1;d.options={...structuredClone(defaults[tool]),...structuredClone(optionsOverride??d.options)};
   const options=d.options,properties=schema.tools[tool].schema.properties;
   const safety=tool==='identity'?'Fictional names and privacy aliases. Names may coincide with real people.':tool==='addresses'?'Synthetic test addresses. Postal patterns are format examples; addresses are not verified or intended for delivery.':tool==='obfuscation'?'Plate-like values are fictional test identifiers, not official registrations.':'Passwords and tokens stay in memory until you explicitly copy or export them.';
   $('#main').innerHTML=heading('FORGE OS',names[tool],schema.tools[tool].description,
    `<select id="generator-presets" class="preset-select" aria-label="Load generator preset">${presetOptions(tool)}</select><button class="secondary" id="generator-save" title="Save options as a preset">Save preset</button><button class="secondary" id="generator-rename" title="Rename selected preset" aria-label="Rename selected preset">${icon('format')}</button><button class="secondary" id="generator-delete" title="Remove selected preset" aria-label="Remove selected preset">${icon('trash')}</button>`)+
    `<div id="generator-workspace" class="generator-workspace"><section class="generator-input" aria-label="Generator configuration"><div class="generator-input-icon">${icon(tool)}</div><div><strong>${escape(names[tool])}</strong><span>Offline generation · up to 1,000 values</span></div>${optionField('count',properties.count,options)}<button id="generator-generate" class="primary">${icon('play')}Generate</button></section><p class="generator-disclosure">${escape(safety)}</p><div id="generator-options"></div><section class="generator-results" aria-label="Generated values"><div class="generator-result-heading"><h2>Generated values</h2><span id="generator-result-status" role="status">Memory only</span><button class="secondary" id="generator-copy" disabled>Copy</button><button class="secondary" id="generator-clear" disabled>Clear</button></div><div id="generator-output" class="generator-output" tabindex="0" aria-label="Generated values, cleared after sixty seconds">No values generated.</div><div class="generator-export"><span>Save current batch</span><button class="secondary" data-generator-export="json" disabled>JSON</button><button class="secondary" data-generator-export="csv" disabled>CSV</button><button class="secondary" data-generator-export="txt" disabled>TXT</button><small>Explicit exports are saved by your browser.</small></div><p class="note">Values clear after 60 seconds, on navigation, or when this tab is hidden. Copying uses the system clipboard; clipboard history may retain values. Presets save options only.</p></section></div>`;
   $('.page-heading h1').insertAdjacentHTML('afterbegin',icon(tool));
   const sections=groups[tool].map(([id,label,sectionIcon,fields])=>{
    const element=document.createElement('div');element.className='generator-fields';element.innerHTML=fields.map(key=>optionField(key,properties[key],options)).join('');
    if(tool==='obfuscation'&&id==='pattern')element.insertAdjacentHTML('beforeend','<p class="generator-pattern-help">Pattern: A = uppercase · a = lowercase · 9 = digit · X = alphanumeric. Separators: - _ . space.</p>');
    return {id,label,icon:sectionIcon,element};
   });
   if(window.ForgeUI?.mountTabs)window.ForgeUI.mountTabs($('#generator-options'),sections,{key:cacheKey(tool),ariaLabel:names[tool]+' settings'});
   else{
    const root=$('#generator-options');sections.forEach(section=>{const heading=document.createElement('h2');heading.textContent=section.label;root.append(heading,section.element);});
   }
   visibleFields();
   $('#generator-options').addEventListener('change',()=>{captureGenerator();visibleFields();});
   if(tool==='addresses'){$('#generator-generate').lastChild.textContent='Generate Address';$('#generator-output').setAttribute('aria-label','Synthetic address fields');$('#generator-workspace .generator-results > .note').textContent='Synthetic values stay in this tab while you fill external forms. Clear removes them. Clipboard history may retain copied values. Presets save options only.';if(addressSaved&&!optionsOverride){result=addressSaved.result;addressLocks=addressSaved.locks;addressPage=addressSaved.page;renderResult();}}
   $('#generator-generate').onclick=async()=>{
    captureGenerator();if(tool==='addresses'&&result){await regenerateAddresses();return;}
    clearValues();const request=++requestEpoch;$('#generator-generate').disabled=true;
    try{const generated=await api('/generators/generate',{tool,options:draft(tool).options});
     if(request!==requestEpoch||view!==tool||(document.hidden&&tool!=='addresses'))return;
     result=generated;if(tool==='addresses'){addressLocks=new Map();addressPage=0;addressSaved=null;}renderResult();if(tool!=='addresses')resultTimer=setTimeout(()=>clearValues('Values cleared after 60 seconds.'),60000);
    }catch(error){if(request===requestEpoch)notify(error.message,true);}
    finally{if(request===requestEpoch&&$('#generator-generate'))$('#generator-generate').disabled=false;}
   };
   $('#generator-clear').onclick=()=>{addressSaved=null;addressLocks=new Map();clearValues();if(tool==='addresses')$('#generator-generate').textContent='Generate Address';};
   $('#generator-copy').onclick=async()=>{if(!result)return;try{await navigator.clipboard.writeText(result.rows.map(row=>row.value).join('\n'));notify('Copied to your system clipboard.');}catch{notify('Clipboard unavailable. Select and copy values manually.',true);}};
   $$('[data-generator-export]').forEach(button=>button.onclick=()=>exportCurrent(button.dataset.generatorExport));
   $('#generator-save').onclick=()=>savePreset();$('#generator-rename').onclick=()=>savePreset(true);
   $('#generator-delete').onclick=async()=>{const id=$('#generator-presets').value;if(!id){notify('Select a preset to remove.',true);return;}
    try{presets=(await api('/generators/presets/delete',{id})).presets;$('#generator-presets').innerHTML=presetOptions(tool);notify('Generator preset removed.');}catch(error){notify(error.message,true);}};
   $('#generator-presets').onchange=async event=>{const preset=presets.find(p=>p.id===event.target.value);if(!preset)return;await renderGenerator(preset.options);if(view===tool&&$('#generator-presets'))$('#generator-presets').value=preset.id;};
  }catch(error){if(epoch===renderEpoch&&view===tool){$('#main').innerHTML=heading('FORGE OS',names[tool],error.message);notify(error.message,true);}}
 }
 const previousRenderTool=renderTool;
 renderTool=function(){if(ids.includes(view)){void renderGenerator();return;}return previousRenderTool();};
 // Serializers are pure and contain no result references; exposed for regression checks.
 window.ForgeGenerators={serialize,csvCell:spreadsheetCell,clear:()=>{addressSaved=null;clearValues();}};
})();
