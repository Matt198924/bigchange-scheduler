// Tiny safe renderer: ## sub-heading, - bullets, 1. numbered, **bold**, blank line = new paragraph.
function hbEsc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function hbInline(s) { return hbEsc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>'); }
function hbRender(text) {
  const out = []; let list = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trimEnd(); let m;
    if (!line.trim()) { close(); continue; }
    if ((m = line.match(/^##\s+(.*)/))) { close(); out.push(`<h3>${hbInline(m[1])}</h3>`); }
    else if ((m = line.match(/^\s*[-•*]\s+(.*)/))) { if (list !== 'ul') { close(); out.push('<ul>'); list = 'ul'; } out.push(`<li>${hbInline(m[1])}</li>`); }
    else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== 'ol') { close(); out.push('<ol>'); list = 'ol'; } out.push(`<li>${hbInline(m[1])}</li>`); }
    else { close(); out.push(`<p>${hbInline(line)}</p>`); }
  }
  close(); return out.join('');
}
