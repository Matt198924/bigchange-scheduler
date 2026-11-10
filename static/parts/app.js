const els={search:document.getElementById('search'),results:document.getElementById('results'),summary:document.getElementById('summary'),status:document.getElementById('status'),empty:document.getElementById('empty'),clear:document.getElementById('clearBtn'),modal:document.getElementById('mapModal'),close:document.getElementById('closeMap'),mapTitle:document.getElementById('mapTitle'),mapNote:document.getElementById('mapNote'),install:document.getElementById('installBtn'),refresh:document.getElementById('refreshBtn'),sheet:document.getElementById('sheetBtn'),map:document.getElementById('lockerMap')};
let parts=[];

function norm(v){return String(v ?? '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9]+/g,' ').trim()}
function setStatus(message,type=''){els.status.textContent=message;els.status.className='status'+(type?' '+type:'')}
async function loadStock(){
  els.refresh.disabled=true; els.refresh.textContent="Loading…";
  try{
    const r=await fetch('/api/parts/stock',{cache:'no-store'}); if(!r.ok) throw new Error('HTTP '+r.status);
    const j=await r.json(); parts=j.parts||[];
    setStatus(j.updated?`Stock list updated ${j.updated}.`:'Stock list loaded.');
    els.summary.textContent=`${parts.length} parts loaded`;
  }catch(e){ setStatus('Could not load the stock list: '+e.message,'error'); }
  render(); els.refresh.disabled=false; els.refresh.textContent="Reload stock";
}
loadStock();
function escapeHtml(v){return String(v).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}
let coords={};
fetch('lockers.json').then(r=>r.json()).then(j=>{coords=j}).catch(()=>{});
function render(){const q=norm(els.search.value);if(!q){els.results.innerHTML='';els.empty.classList.remove('hidden');els.summary.textContent=`Type to search ${parts.length} parts`;return}els.empty.classList.add('hidden');const terms=q.split(/\s+/).filter(Boolean);const hits=parts.filter(p=>{const hay=norm(`${p.part} ${p.location}`);return terms.every(t=>hay.includes(t))});els.summary.textContent=`${hits.length} result${hits.length===1?'':'s'}`;els.results.innerHTML=hits.length?hits.map((p,i)=>`<article class="result"><div class="part">${escapeHtml(p.part)}</div><div class="meta"><span class="click-show">Click to show</span><button class="loc" data-index="${i}">${escapeHtml(p.location)}</button></div></article>`).join(''):`<div class="empty"><div class="empty-icon">🔎</div><h2>No matches</h2><p>Try a part number, description, or locker such as C4 B1.</p></div>`;hits.forEach((p,i)=>els.results.querySelector(`[data-index="${i}"]`)?.addEventListener('click',()=>showMap(p)))}
let mapZoom=true,mapLoc='';
function drawMap(location){
  mapLoc=location;
  const box=coords[location], wrap=document.getElementById('mapWrap');
  const hl=box?`<div class="hl" style="left:${box.left}%;top:${box.top}%;width:${box.width}%;height:${box.height}%"></div>`:'';
  let style='';
  if(box&&mapZoom){
    const S=2.6, cx=(box.left+box.width/2)/100, cy=(box.top+box.height/2)/100;
    const clamp=(v)=>Math.max((1-S)*100,Math.min(0,v));
    style=`width:${S*100}%;left:${clamp(50-cx*S*100)}%;top:${clamp(50-cy*S*100)}%`;
  }
  wrap.className='map-wrap'+(box&&mapZoom?' zoomed':'');
  wrap.innerHTML=`<div class="map-inner" style="${style}"><img id="lockerMap" src="locker-layout.png" alt="Van locker layout">${hl}</div>`;
  const b=document.getElementById('zoomBtn'); if(b){b.classList.toggle('hidden',!box); b.textContent=mapZoom?'Show whole van':'Zoom in';}
}

function showMap(p){
  els.mapTitle.textContent=p.location;
  els.mapNote.innerHTML=`<span class="locker-label">${escapeHtml(p.location)}</span>. ${coords[p.location]?'Highlighted on the supplied locker layout.':'This location is listed in stock but is not numbered on the supplied locker layout.'}`;
  els.modal.classList.remove('hidden');
  drawMap(p.location);
}

function closeMap(){els.modal.classList.add('hidden')}
els.search.addEventListener('input',render);
els.refresh.addEventListener('click',loadStock);
els.clear.addEventListener('click',()=>{els.search.value='';els.search.focus();render()});
els.close.addEventListener('click',closeMap);
document.getElementById('zoomBtn').addEventListener('click',()=>{mapZoom=!mapZoom;drawMap(mapLoc)});
els.modal.addEventListener('click',e=>{if(e.target===els.modal)closeMap()});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeMap()});

window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();deferredPrompt=e;els.install.classList.remove('hidden')});
els.install.addEventListener('click',async()=>{if(!deferredPrompt)return;deferredPrompt.prompt();await deferredPrompt.userChoice;deferredPrompt=null;els.install.classList.add('hidden')});

if('serviceWorker' in navigator)window.addEventListener('load',async()=>{
  try{const reg=await navigator.serviceWorker.register('sw.js');await reg.update();}
  catch(e){console.warn('service worker update failed',e)}
});
