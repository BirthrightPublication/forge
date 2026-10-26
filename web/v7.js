/* Compact shell. Working drafts stay in their original owners; only a route is remembered. */
(() => {
 'use strict';
 titles.downloads='Media Downloads';
 paths.settings='<path d="m9 3-1 3-3 1 1 3-2 2 2 2-1 3 3 1 1 3h4l1-3 3-1-1-3 2-2-2-2 1-3-3-1-1-3z"/><circle cx="11" cy="12" r="3"/>';
 let previous='downloads',previousScroll=0;
 const tooltip=document.createElement('div');tooltip.id='navigation-tooltip';tooltip.className='navigation-tooltip';tooltip.role='tooltip';tooltip.hidden=true;document.body.append(tooltip);
 let tooltipOwner=null;
 function hideTip(){tooltip.hidden=true;tooltipOwner?.removeAttribute('aria-describedby');tooltipOwner=null;}
 function showTip(button){
  const label=button.dataset.navLabel;if(!label)return;hideTip();tooltipOwner=button;
  tooltip.textContent=label;tooltip.hidden=false;button.setAttribute('aria-describedby',tooltip.id);
  const box=button.getBoundingClientRect();tooltip.style.left=$('#sidebar').getBoundingClientRect().right+10+'px';
  tooltip.style.top=Math.max(8,Math.min(innerHeight-tooltip.offsetHeight-8,box.top+(box.height-tooltip.offsetHeight)/2))+'px';
 }
 $('#sidebar').addEventListener('pointerover',event=>{const b=event.target.closest('.nav-item');if(b)showTip(b);});
 $('#sidebar').addEventListener('pointerout',event=>{if(!tooltipOwner?.contains(event.relatedTarget))hideTip();});
 $('#sidebar').addEventListener('focusin',event=>{const b=event.target.closest('.nav-item');if(b)showTip(b);});
 $('#sidebar').addEventListener('focusout',hideTip);$('.nav-scroll').addEventListener('scroll',hideTip);
 document.addEventListener('keydown',event=>{if(event.key==='Escape')hideTip();});window.addEventListener('resize',hideTip);
 function sync(){
  if(!state)return;
  $('#home-brand').setAttribute('aria-label','FORGE · Scripting Environment Engine V'+state.version);
  $$('#sidebar .nav-item').forEach(button=>{
   const label=$('.nav-text',button)?.textContent||button.title||button.getAttribute('aria-label');
   if(label){button.dataset.navLabel=label;button.setAttribute('aria-label',label);button.removeAttribute('title');}
   if(button.classList.contains('active'))button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
  });
  const count=busyJobs().length;let badge=$('#queue-top-count');
  if(!badge){badge=document.createElement('span');badge.id='queue-top-count';$('#queue-top').append(badge);}
  badge.textContent=String(count);badge.hidden=!count;
  $('#queue-top').setAttribute('aria-label','Processing queue'+(count?' · '+count+' active or queued':''));
  $('#collapse').setAttribute('aria-expanded',String(!document.body.classList.contains('compact')));
 }
 const beforeNav=renderNav;renderNav=function(){beforeNav();hideTip();sync();};
 // Later extensions append their own tools after this wrapper. Keep their
 // accessible names and selected states consistent in collapsed navigation.
 let navSyncPending=false;
 new MutationObserver(()=>{if(navSyncPending)return;navSyncPending=true;queueMicrotask(()=>{navSyncPending=false;sync();});}).observe($('#sidebar'),{childList:true,subtree:true});
 const beforeDock=renderDock;renderDock=function(){beforeDock();sync();};
 const beforeNavigate=navigate;
 navigate=function(next){
  if(next==='workflows'&&view!=='workflows'){previous=view==='overview'?'downloads':view;previousScroll=scrollY;}
  hideTip();const result=beforeNavigate(next);
  if(!['workflows','overview','queue','settings'].includes(next)){
   previous=next;try{localStorage.setItem('forge-last-tool',next);}catch{}
  }
  return result;
 };
 function exitBlack(){const target=previous||'downloads',top=previousScroll;navigate(target);requestAnimationFrame(()=>scrollTo({top}));}
 window.ForgeShell={exitBlack,toggleBlack:()=>view==='workflows'?exitBlack():navigate('workflows'),lastTool:()=>previous};
 $('#collapse').addEventListener('click',()=>{hideTip();sync();});
 const beforeSettings=renderSettings;renderSettings=function(){beforeSettings();const h=$('.page-heading h1');if(h)h.textContent='Settings';};
})();

/* V0.9 download rows share the Universal Queue and its existing actions. */
(() => {
 'use strict';
 const isDownload=job=>String(job?.tool||'').startsWith('download_');
 const finite=(...values)=>values.find(value=>typeof value==='number'&&Number.isFinite(value)&&value>=0);
 function duration(seconds){
  if(!Number.isFinite(seconds)||seconds<0)return '—';
  const total=Math.floor(seconds),hours=Math.floor(total/3600),minutes=Math.floor(total%3600/60),remaining=total%60;
  return hours?`${hours}h ${minutes}m ${remaining}s`:minutes?`${minutes}m ${remaining}s`:`${remaining}s`;
 }
 function metadata(job){
  const media=job.media_preview||job.media||job.media_metadata||job.download_media||{};
  return {title:job.media_title||media.title||job.title||job.label||'Download',
   channel:job.channel||job.channel_title||job.source_name||media.channel||media.uploader||media.source_title||'',
   url:job.source_url||media.url||media.webpage_url||job.url||'',
   thumbnail:job.thumbnail||job.thumbnail_url||media.thumbnail||'',
   thumbnail_data:job.thumbnail_data||media.thumbnail_data||'',
   thumbnail_preview:job.thumbnail_preview||media.thumbnail_preview||'',
   id:job.media_id||media.id||''};
 }
 function sourceURL(value){
  try{const url=new URL(value);return ['https:','http:'].includes(url.protocol)&&!url.username&&!url.password?url.href:'';}catch{return '';}
 }
 function sourceLabel(media){
  if(media.channel)return String(media.channel);
  try{return new URL(sourceURL(media.url)).hostname;}catch{return 'Source unknown';}
 }
 function thumbnail(media){
  return window.ForgeDownloads?.thumbnail?.(media,'v9-queue-thumbnail')||`<span class="v9-queue-thumbnail v9-queue-placeholder" aria-hidden="true">${icon('download_video')}</span>`;
 }
 function mainOutput(job){
  const outputs=job.outputs||[];
  let index=outputs.findIndex(output=>/\.(?:mp4|mkv|webm|mov|avi|mp3|m4a|flac|wav|ogg|opus|aac)$/i.test(output.name||output.path||''));
  if(index<0&&outputs.length)index=0;
  return {output:index<0?null:outputs[index],index};
 }
 function elapsed(job){
  const observed=finite(job.elapsed_seconds,job.transfer_elapsed_seconds);
  if(['running','finalizing','cancelling'].includes(job.status)&&Number.isFinite(job.started))return Math.max(observed||0,Date.now()/1000-job.started,0);
  return finite(job.execution_seconds,observed);
 }
 function transferFacts(job){
  const downloaded=finite(job.downloaded_bytes),total=finite(job.total_bytes),speed=finite(job.download_speed_bps),facts=[];
  if(downloaded!==undefined)facts.push(bytes(downloaded)+(total>0?' / '+bytes(total):' received'));
  if(speed>0)facts.push(bytes(speed)+'/s');else if(job.speed)facts.push(String(job.speed));
  const time=elapsed(job);if(time!==undefined)facts.push('Elapsed '+duration(time));
  if(job.status==='running'&&Number.isFinite(job.eta_seconds)&&job.eta_seconds>=0)facts.push('Transfer ETA '+duration(job.eta_seconds));
  return facts;
 }
 const beforeProgress=v4ProgressHTML;
 v4ProgressHTML=function(job){
  if(!isDownload(job))return beforeProgress(job);
  if(job.status==='completed')return `<span class="v4-completed">${icon('check')}Completed</span>${Number.isFinite(job.execution_seconds)?`<small>${duration(job.execution_seconds)} processing</small>`:''}`;
  if(!v4ActiveStatuses.includes(job.status))return beforeProgress(job);
  const percent=job.progress_measured===true&&Number.isFinite(job.progress)?Math.max(0,Math.min(99,job.progress)):null;
  const stage=job.stage||'Preparing',facts=transferFacts(job),current=finite(job.stage_progress);
  const stream=job.stream_count>1&&job.stream_index>0?`Stream ${job.stream_index}/${job.stream_count}`:'Current stream';
  const streamLabel=percent===null&&current!==undefined?`${stream} · ${Math.round(Math.min(100,current))}%`:'';
  return `<div class="v9-download-progress"><div class="v4-progress-label"><span>${escape(stage)}</span>${percent!==null?`<span>${Math.round(percent)}%</span>`:''}</div><progress max="100" ${percent===null?'':`value="${percent}"`} aria-label="${escape(metadata(job).title)} download transfer progress" aria-valuetext="${escape(percent===null?stage:stage+' · '+Math.round(percent)+'%')}"></progress>${streamLabel?`<small class="v9-current-transfer">${escape(streamLabel)}</small>`:''}${facts.length?`<div class="v9-transfer-facts">${facts.map(fact=>`<small>${escape(fact)}</small>`).join('')}</div>`:''}</div>`;
 };
 const beforeRow=jobRow;
 jobRow=function(job){
  if(!isDownload(job))return beforeRow(job);
  const id=escape(job.id),media=metadata(job),label=media.title,source=sourceLabel(media),url=sourceURL(media.url);
  const active=['queued',...v4ActiveStatuses].includes(job.status),retry=['failed','cancelled','interrupted','blocked','completed'].includes(job.status);
  const {output,index}=mainOutput(job),directory=output?.path?.replace(/[/\\][^/\\]*$/,'')||job.destination||'';
  const finished=job.status==='completed'&&Number.isFinite(job.finished)?when(job.finished):'';
  const finalSize=job.status==='completed'&&Number.isFinite(output?.size)?bytes(output.size):'';
  return `<div class="queue3-row v9-download-job" role="row" data-row-job="${id}"><div role="cell"><input type="checkbox" class="job-select" data-select-job="${id}" ${selectedJobs.has(job.id)?'checked':''} aria-label="Select ${escape(label)}"></div><div class="v4-job-file" role="cell">${thumbnail(media)}<div class="v4-job-name"><strong title="${escape(label)}">${escape(label)}</strong><small class="v9-job-source" title="${escape(source)}">${escape(source)}</small><span class="v4-job-status ${escape(job.status)}">${escape(job.status)}${['failed','blocked','interrupted','cancelled'].includes(job.status)&&job.message?' · '+escape(v4SafeLabel(job.message)):''}</span>${finished||finalSize?`<small>${[finalSize,finished].filter(Boolean).map(escape).join(' · ')}</small>`:''}${job.workflow_name?`<small>${escape(job.workflow_name)} · step ${Number(job.step)||1}/${Number(job.steps)||1}</small>`:''}</div></div><div class="v4-job-operation" role="cell"><strong>${escape(titles[job.tool]||job.tool)}</strong><small>${escape(v4JobInfo(job)||when(job.created))}</small>${directory?`<small class="v9-output-directory" title="${escape(directory)}">${escape(directory)}</small>`:''}</div><div class="v4-job-progress" role="cell">${v4ProgressHTML(job)}</div><div class="v4-row-actions" role="cell">${output?v4IconButton('open','Open output file',`data-open-output="${id}" data-output-index="${index}"`)+v4IconButton('folder','Open output folder',`data-reveal-output="${id}" data-output-index="${index}"`):''}${url?`<a class="v4-icon" href="${escape(url)}" target="_blank" rel="noopener noreferrer" aria-label="Open source for ${escape(label)}" title="Open original source" data-tooltip="Open original source">${icon('download_video')}</a>`:''}${v4IconButton('info','Job details',`data-job="${id}"`)}${active&&!['cancelling','finalizing'].includes(job.status)?v4IconButton('cancel','Cancel download',`data-cancel="${id}"`):''}${retry?v4IconButton('refresh','Request download retry',`data-retry="${id}"`):''}${!active?v4IconButton('trash','Remove queue record; keep output files',`data-remove-job="${id}"`):''}</div></div>`;
 };
 const beforeVisible=v4VisibleJobs;
 v4VisibleJobs=function(){return beforeVisible().filter(job=>job.status!=='planned');};
 const beforeDetails=jobDetail;
 jobDetail=async function(id){
  await beforeDetails(id);const job=state.jobs.find(item=>item.id===id);if(!isDownload(job)||!$('#dialog').open)return;
  const media=metadata(job),url=sourceURL(media.url),{output}=mainOutput(job),facts=transferFacts(job);
  const heading=$('#dialog-content > h2');if(heading)heading.textContent=media.title;
  const section=document.createElement('section');section.className='v9-download-details';
  section.innerHTML=`<h3>Download</h3><div class="v9-download-detail-identity">${thumbnail(media)}<div><strong>${escape(media.title)}</strong><small>${escape(sourceLabel(media))}</small></div></div><dl class="v6-durations"><dt>Stage</dt><dd>${escape(job.stage||job.status)}</dd>${media.id?`<dt>Media ID</dt><dd>${escape(media.id)}</dd>`:''}${facts.length?`<dt>Transfer</dt><dd>${facts.map(escape).join(' · ')}</dd>`:''}${output?`<dt>Verified output</dt><dd>${escape(output.name)} · ${Number.isFinite(output.size)?bytes(output.size):'Unknown size'}</dd>`:''}</dl>${url?`<a class="secondary" href="${escape(url)}" target="_blank" rel="noopener noreferrer">Open original source ${icon('open')}</a>`:''}${['interrupted','cancelled','failed'].includes(job.status)?'<p class="note">Retry uses preserved transfer data where the source supports resuming.</p>':''}`;
  $('#dialog-content').append(section);
 };
 window.ForgeMediaUI={metadata,sourceURL,transferFacts,mainOutput};
})();
