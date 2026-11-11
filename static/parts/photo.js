// Photo search: take a photo of a part, match it against the part photos (CLIP, runs in the phone's browser)
(function(){
const btn=document.getElementById('photoBtn'),file=document.getElementById('photoFile'),out=document.getElementById('photoOut');
if(!btn)return;
let proc=null,model=null,gallery=null,loading=null;
const LIB='https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.5';
async function load(say){
  if(loading)return loading;
  loading=(async()=>{
    say('Loading photo matcher (first time only, about 90 MB)…');
    const [T,g]=await Promise.all([import(LIB),fetch('embeddings.json').then(r=>r.json())]);
    gallery=Object.entries(g);
    proc=await T.AutoProcessor.from_pretrained('Xenova/clip-vit-base-patch32');
    model=await T.CLIPVisionModelWithProjection.from_pretrained('Xenova/clip-vit-base-patch32',{dtype:'q8',progress_callback:p=>{if(p.status==='progress'&&p.total>5e6)say('Loading photo matcher… '+Math.round(p.loaded/p.total*100)+'%')}});
    return T;
  })().catch(e=>{loading=null;throw e});
  return loading;
}
function top(vec,n){
  const s=gallery.map(([k,v])=>{let d=0;for(let i=0;i<512;i++)d+=v[i]*vec[i];return [k,d]});
  s.sort((a,b)=>b[1]-a[1]);return s.slice(0,n);
}
function card(p,sim){
  const im=imgFor(p.part);
  return `<article class="result hi"><button class="th" data-img="${im[0]}" data-name="${escapeHtml(p.part)}" data-chk="${im[1]}" aria-label="Enlarge photo"><img src="${im[0]}" alt=""></button><div class="rb"><div class="part">${escapeHtml(p.part)}</div><div class="meta"><span class="click-show">${Math.round(sim*100)}% match</span><button class="loc" data-loc="${escapeHtml(p.location)}">${escapeHtml(p.location)}</button></div></div></article>`;
}
btn.addEventListener('click',()=>file.click());
file.addEventListener('change',async()=>{
  const f=file.files[0];file.value='';if(!f)return;
  const say=t=>{out.innerHTML=`<div class="summary" style="padding:8px 2px">${t}</div>`};
  try{
    const T=await load(say);say('Matching…');
    const img=await T.RawImage.fromBlob(f);
    const {image_embeds}=await model(await proc(img));
    const e=image_embeds.data,nrm=Math.hypot(...e),vec=Array.from(e,x=>x/nrm);
    const hits=top(vec,12),res=[],seen=new Set();
    for(const [k,sim] of hits){
      for(const p of parts){const im=imgFor(p.part);if(im&&im[0]===k&&!seen.has(p.part+p.location)){seen.add(p.part+p.location);res.push([p,sim])}}
      if(res.length>=6)break;
    }
    out.innerHTML=`<div class="summary" style="padding:8px 2px"><b>Best guesses from your photo</b> — check the part number or size before taking it. <button id="photoClear" class="clear" style="position:static;transform:none;width:auto;padding:0 10px;font-size:13px" type="button">Clear</button></div><div class="results">${res.map(([p,s])=>card(p,s)).join('')||'<div class="empty"><p>No close match found.</p></div>'}</div>`;
    document.getElementById('photoClear').onclick=()=>{out.innerHTML=''};
    out.querySelectorAll('.loc').forEach(b=>b.addEventListener('click',()=>{const p=parts.find(x=>x.location===b.dataset.loc);if(p)showMap(p)}));
  }catch(err){say('Photo search failed: '+(err.message||err)+'. Check your signal and try again.')}
});
})();
