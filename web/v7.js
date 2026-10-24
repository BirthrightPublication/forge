/* Compact shell. Working drafts stay in their original owners; only a route is remembered. */
(() => {
 'use strict';
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
