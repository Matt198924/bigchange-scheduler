// Supplier catalogues (FES, Parts Town): browse page images, search the text across all of them.
(function () {
  let C = null, loading = null;
  const $ = id => document.getElementById(id);
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const img = (slug, i) => `catalogues/${slug}/p-${String(i).padStart(3, '0')}.webp`;
  function listView() {
    $('catView').classList.add('hidden'); $('catList').classList.remove('hidden'); $('catSum').parentElement.classList.remove('hidden');
    $('catList').innerHTML = C.map(c => `<article class="result" data-s="${c.slug}" style="cursor:pointer"><div class="part">${esc(c.title)}</div><div class="meta" style="display:flex;justify-content:space-between;gap:8px"><span>${c.pages.length} pages</span><span>Tap to open ›</span></div></article>`).join('');
  }
  function open(slug, page) {
    const c = C.find(x => x.slug === slug);
    $('catList').classList.add('hidden'); $('catSum').parentElement.classList.add('hidden'); $('catView').classList.remove('hidden');
    $('catPages').innerHTML = `<div class="lk-title">${esc(c.title)}</div>` + c.pages.map((_, i) => `<div id="cp${i + 1}" style="margin:0 0 10px;background:#fff;border:1px solid #d8e0ea;border-radius:6px;min-height:240px;scroll-margin-top:70px"><img loading="lazy" style="display:block;width:100%;height:auto" src="${img(slug, i + 1)}" alt="${esc(c.title)} page ${i + 1}"></div>`).join('');
    window.scrollTo(0, 0);
    if (page > 1) setTimeout(() => { const el = $('cp' + page); if (el) { el.scrollIntoView({behavior: 'smooth'}); el.style.outline = '3px solid #00C2FF'; setTimeout(() => el.style.outline = '', 1800); } }, 200);
  }
  function search() {
    const terms = $('catQ').value.toLowerCase().split(/\s+/).filter(t => t.length > 1);
    if (!terms.length) return listView();
    const re = new RegExp('(' + terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'gi'), out = [];
    C.forEach(c => c.pages.forEach((t, i) => {
      const low = t.toLowerCase(); if (!terms.every(w => low.includes(w))) return;
      const at = Math.max(0, low.indexOf(terms[0]) - 60);
      out.push(`<article class="result" data-s="${c.slug}" data-p="${i + 1}" style="cursor:pointer"><div class="meta" style="display:flex;justify-content:space-between;gap:8px"><span>${esc(c.title)}</span><span>PAGE ${i + 1}</span></div><div style="margin-top:4px;font-size:14px;line-height:1.4">${esc((at ? '…' : '') + t.slice(at, at + 200) + '…').replace(re, '<mark>$1</mark>')}</div></article>`);
    }));
    $('catView').classList.add('hidden'); $('catList').classList.remove('hidden'); $('catSum').parentElement.classList.remove('hidden');
    $('catSum').textContent = out.length ? `${out.length} page${out.length === 1 ? '' : 's'} match${out.length > 25 ? ' (showing the first 25)' : ''}` : 'Nothing matches that search.';
    $('catList').innerHTML = out.slice(0, 25).join('');
  }
  window.initCats = function () {
    if (C) return Promise.resolve();
    $('catSum').textContent = 'Loading catalogues…';
    return fetch('catalogues/catalogues.json').then(r => r.json()).then(j => { C = j.catalogues; $('catSum').textContent = 'Supplier catalogues: tap one to browse, or search across all of them.'; $('catQ').value ? search() : listView(); })
      .catch(() => { $('catSum').textContent = 'Could not load the catalogues - try again.'; });
  };
  $('catQ').addEventListener('input', () => C ? search() : window.initCats());
  $('catList').addEventListener('click', e => { const a = e.target.closest('[data-s]'); if (a) open(a.dataset.s, +a.dataset.p || 1); });
  $('catBack').addEventListener('click', () => { $('catQ').value ? search() : listView(); window.scrollTo(0, 0); });
})();
