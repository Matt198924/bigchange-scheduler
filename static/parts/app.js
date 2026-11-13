const els={search:document.getElementById('search'),results:document.getElementById('results'),summary:document.getElementById('summary'),status:document.getElementById('status'),empty:document.getElementById('empty'),clear:document.getElementById('clearBtn'),modal:document.getElementById('mapModal'),close:document.getElementById('closeMap'),mapTitle:document.getElementById('mapTitle'),mapNote:document.getElementById('mapNote'),install:document.getElementById('installBtn'),refresh:document.getElementById('refreshBtn'),map:document.getElementById('lockerMap')};
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
let coords={}, IMGS={};
fetch('images.json').then(r=>r.json()).then(j=>{IMGS=j;render()}).catch(()=>{});
function imgKey(t){return String(t).replace('** vs **','').trim().replace(/^\([^)]*\)/,'').toLowerCase().replace(/[^a-z0-9]+/g,' ').trim()}
function imgFor(part){return IMGS[imgKey(part)]||null}
function showImg(src,name,chk){
  let m=document.getElementById('imgModal'); if(!m){m=document.createElement('div');m.id='imgModal';m.className='img-modal';m.addEventListener('click',()=>m.classList.add('hidden'));document.body.appendChild(m)}
  m.innerHTML=`<div class="img-card"><img src="${src}" alt=""><div class="img-cap">${name}</div><div class="img-note">${chk==='1'?'Match not fully confirmed - check against the part number. ':''}Photo for identification only.</div></div>`;m.classList.remove('hidden');
}
document.addEventListener('click',e=>{const t=e.target.closest('.th');if(t)showImg(t.dataset.img,t.dataset.name,t.dataset.chk)});
fetch('lockers.json').then(r=>r.json()).then(j=>{coords=j}).catch(()=>{});
function render(){const q=norm(els.search.value);if(!q){els.results.innerHTML='';els.empty.classList.remove('hidden');els.summary.textContent=`Type to search ${parts.length} parts`;return}els.empty.classList.add('hidden');const terms=q.split(/\s+/).filter(Boolean);const hits=parts.filter(p=>{const hay=norm(`${p.part} ${p.location}`);return terms.every(t=>hay.includes(t))});els.summary.textContent=`${hits.length} result${hits.length===1?'':'s'}`;els.results.innerHTML=hits.length?hits.map((p,i)=>{const im=imgFor(p.part);return `<article class="result${im?' hi':''}">${im?`<button class="th" data-img="${im[0]}" data-name="${escapeHtml(p.part)}" data-chk="${im[1]}" aria-label="Enlarge photo"><img loading="lazy" src="${im[0]}" alt="">${im[1]?'<i>check</i>':''}</button>`:''}<div class="rb"><div class="part">${escapeHtml(p.part)}</div><div class="meta"><span class="click-show">Click to show</span><button class="loc" data-index="${i}">${escapeHtml(p.location)}</button></div></div></article>`}).join(''):`<div class="empty"><div class="empty-icon">🔎</div><h2>No matches</h2><p>Try a part number, description, or locker such as C4 B1.</p></div>`;hits.forEach((p,i)=>els.results.querySelector(`[data-index="${i}"]`)?.addEventListener('click',()=>showMap(p)))}
let mapZoom=false,mapLoc='';
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
els.modal.addEventListener('click',e=>{if(e.target===els.modal)closeMap()});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeMap()});

window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();deferredPrompt=e;els.install.classList.remove('hidden')});
els.install.addEventListener('click',async()=>{if(!deferredPrompt)return;deferredPrompt.prompt();await deferredPrompt.userChoice;deferredPrompt=null;els.install.classList.add('hidden')});

if('serviceWorker' in navigator)window.addEventListener('load',async()=>{
  try{const reg=await navigator.serviceWorker.register('sw.js');await reg.update();}
  catch(e){console.warn('service worker update failed',e)}
});

// ── Lockers tab: tap a locker on the layout to see what should be in it ──
let lkSel='';
function showTab(which){
  const l=which==='lockers', c=which==='cats';
  document.getElementById('lockersMain').classList.toggle('hidden',!l);
  document.getElementById('catsMain').classList.toggle('hidden',!c);
  document.getElementById('appMain').classList.toggle('hidden',l||c);
  document.getElementById('tabLockers').classList.toggle('on',l);
  document.getElementById('tabCats').classList.toggle('on',c);
  document.getElementById('tabSearch').classList.toggle('on',!l&&!c);
  if(l) drawLockers();
  if(c&&window.initCats) window.initCats();
}
function drawLockers(){
  const counts={}; parts.forEach(p=>{counts[p.location]=(counts[p.location]||0)+1});
  const wrap=document.getElementById('lkMap');
  wrap.innerHTML=`<div class="map-inner"><img src="locker-layout.png" alt="Van locker layout">`+Object.entries(coords).map(([k,b])=>
    `<div class="lk ${k===lkSel?'sel':''} ${counts[k]?'':'empty-l'}" data-loc="${escapeHtml(k)}" style="left:${b.left}%;top:${b.top}%;width:${b.width}%;height:${b.height}%"><i>${counts[k]||0}</i></div>`).join('')+'</div>';
  wrap.querySelectorAll('.lk').forEach(el=>el.addEventListener('click',()=>{lkSel=el.dataset.loc;drawLockers()}));
  const list=document.getElementById('lkList');
  if(!lkSel){list.innerHTML='';return}
  const inside=parts.filter(p=>p.location===lkSel);
  list.innerHTML=`<div class="lk-title">${escapeHtml(lkSel)} · ${inside.length} part${inside.length===1?'':'s'}</div>`+(inside.length?inside.map(p=>{const im=imgFor(p.part);return `<article class="result${im?' hi':''}">${im?`<button class="th" data-img="${im[0]}" data-name="${escapeHtml(p.part)}" data-chk="${im[1]}" aria-label="Enlarge photo"><img loading="lazy" src="${im[0]}" alt="">${im[1]?'<i>check</i>':''}</button>`:''}<div class="rb"><div class="part">${escapeHtml(p.part)}</div><div class="meta"><span class="click-show">Qty ${escapeHtml(p.qty)}</span></div></div></article>`}).join(''):'<div class="empty"><p>Nothing is listed for this locker.</p></div>');
}
document.getElementById('tabSearch').addEventListener('click',()=>showTab('search'));
document.getElementById('tabLockers').addEventListener('click',()=>showTab('lockers'));
document.getElementById('tabCats').addEventListener('click',()=>showTab('cats'));
if(location.hash==='#lockers')showTab('lockers');else if(location.hash==='#cats')showTab('cats');
