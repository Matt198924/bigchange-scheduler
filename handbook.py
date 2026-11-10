"""Company handbook for the staff hub (/handbook), editable by the office at /handbook/office.

Sections live in the leave database. The office can type sections in directly or import a Word (.docx) file,
which is split into sections at its main headings. Reading is open to anyone with the link; editing needs the office password.
"""
import io
import os
import re
import threading
import zipfile
from datetime import datetime
from xml.etree import ElementTree as ET

from flask import Blueprint, jsonify, redirect, request, send_from_directory

import leave

handbook_bp = Blueprint('handbook', __name__)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'handbook')
_tables = {'done': False}
_lock = threading.Lock()
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def _ensure():
    if _tables['done']: return
    with _lock:
        if _tables['done']: return
        leave.init_db()
        pk = 'SERIAL PRIMARY KEY' if leave.IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        leave.execute(f'''CREATE TABLE IF NOT EXISTS handbook_sections (
            id {pk}, position INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL)''')
        _tables['done'] = True


def _now(): return datetime.now().strftime('%Y-%m-%d %H:%M')
def _s(v, n): return str(v if v is not None else '').strip()[:n]
def _out(r): return {'id': r['id'], 'title': r['title'], 'body': r['body'], 'updatedAt': r['updated_at']}


@handbook_bp.route('/handbook')
def hb_redirect(): return redirect('/handbook/', code=301)

@handbook_bp.route('/handbook/')
def hb_page(): return send_from_directory(STATIC_DIR, 'index.html')

@handbook_bp.route('/policies')
def pol_redirect(): return redirect('/policies/', code=301)

@handbook_bp.route('/policies/')
def pol_page(): return send_from_directory(os.path.join(os.path.dirname(STATIC_DIR), 'policies'), 'index.html')


@handbook_bp.route('/handbook/office')
def hb_office(): return send_from_directory(STATIC_DIR, 'office.html')


@handbook_bp.route('/api/handbook')
def hb_get():
    _ensure()
    rows = leave.query('SELECT * FROM handbook_sections ORDER BY position, id')
    return jsonify({'sections': [_out(r) for r in rows], 'version': leave.get_setting('handbook_version') or ''})


def _next_pos():
    r = leave.query('SELECT MAX(position) AS m FROM handbook_sections')
    return int((r[0]['m'] if r and r[0]['m'] is not None else 0)) + 1


@handbook_bp.route('/api/handbook/admin/section', methods=['POST'])
def hb_create():
    if not leave.is_admin(): return leave._auth_error()
    _ensure()
    b = request.get_json(silent=True) or {}
    title = _s(b.get('title'), 200)
    if not title: return jsonify({'error': 'Give the section a title'}), 400
    rid = leave.execute('INSERT INTO handbook_sections (position, title, body, updated_at) VALUES (?,?,?,?)',
                        (_next_pos(), title, _s(b.get('body'), 60000), _now()), returning_id=True)
    return jsonify({'success': True, 'id': rid})


@handbook_bp.route('/api/handbook/admin/section/<int:sid>', methods=['PUT'])
def hb_update(sid):
    if not leave.is_admin(): return leave._auth_error()
    _ensure()
    b = request.get_json(silent=True) or {}
    title = _s(b.get('title'), 200)
    if not title: return jsonify({'error': 'Give the section a title'}), 400
    if not leave.query('SELECT id FROM handbook_sections WHERE id = ?', (sid,)):
        return jsonify({'error': 'Section not found'}), 404
    leave.execute('UPDATE handbook_sections SET title = ?, body = ?, updated_at = ? WHERE id = ?',
                  (title, _s(b.get('body'), 60000), _now(), sid))
    return jsonify({'success': True})


@handbook_bp.route('/api/handbook/admin/section/<int:sid>', methods=['DELETE'])
def hb_delete(sid):
    if not leave.is_admin(): return leave._auth_error()
    _ensure()
    leave.execute('DELETE FROM handbook_sections WHERE id = ?', (sid,))
    return jsonify({'success': True})


@handbook_bp.route('/api/handbook/admin/section/<int:sid>/move', methods=['POST'])
def hb_move(sid):
    if not leave.is_admin(): return leave._auth_error()
    _ensure()
    up = (request.get_json(silent=True) or {}).get('dir') == 'up'
    rows = leave.query('SELECT id FROM handbook_sections ORDER BY position, id')
    ids = [r['id'] for r in rows]
    if sid not in ids: return jsonify({'error': 'Section not found'}), 404
    i = ids.index(sid); j = i - 1 if up else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        for pos, x in enumerate(ids, 1):
            leave.execute('UPDATE handbook_sections SET position = ? WHERE id = ?', (pos, x))
    return jsonify({'success': True})


@handbook_bp.route('/api/handbook/admin/version', methods=['PUT'])
def hb_version():
    if not leave.is_admin(): return leave._auth_error()
    leave.init_db()
    leave.set_setting('handbook_version', _s((request.get_json(silent=True) or {}).get('version'), 100))
    return jsonify({'success': True})


# ── Word import (standard library only) ──────────────────────────────────
def _runs(p):
    out = []
    for r in p.iter(W + 'r'):
        bold = r.find(f'{W}rPr/{W}b') is not None and r.find(f'{W}rPr/{W}b').get(W + 'val') not in ('0', 'false')
        t = ''.join((n.text or '') if n.tag == W + 't' else ('\t' if n.tag == W + 'tab' else '\n' if n.tag == W + 'br' else '')
                    for n in r)
        if t.strip() and bold: t = f"**{t.strip()}** "
        out.append(t)
    return ''.join(out).replace('\t', ' ').strip()


def _para(p):
    style = (p.find(f'{W}pPr/{W}pStyle').get(W + 'val') if p.find(f'{W}pPr/{W}pStyle') is not None else '') or ''
    text = _runs(p)
    listed = p.find(f'{W}pPr/{W}numPr') is not None or 'List' in style
    m = re.match(r'(?i)heading\s*(\d)|title', style)
    level = int(m.group(1)) if m and m.group(1) else (1 if m else 0)
    return style, text, listed, level


def parse_docx(data):
    """-> [(title, body)] split at Heading 1 (or Heading 2 when there is no Heading 1)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read('word/document.xml'))
    body = root.find(W + 'body')
    items = []                                           # (level, text) with level 0 = body text
    for el in body:
        if el.tag == W + 'p':
            _, text, listed, level = _para(el)
            if not text: items.append((0, '')); continue
            items.append((level, text if level else ('- ' + text if listed else text)))
        elif el.tag == W + 'tbl':
            for tr in el.iter(W + 'tr'):
                cells = [' '.join(_runs(p) for p in tc.iter(W + 'p')).strip() for tc in tr.iter(W + 'tc')]
                if any(cells): items.append((0, ' | '.join(cells)))
            items.append((0, ''))
    top = 1 if any(l == 1 for l, _ in items) else (2 if any(l == 2 for l, _ in items) else 1)
    secs, cur = [], None
    for level, text in items:
        if level and level <= top:
            cur = [text, []]; secs.append(cur)
        else:
            if cur is None:
                if not text: continue
                cur = ['Introduction', []]; secs.append(cur)
            cur[1].append(f"## {text}" if level else text)
    # tidy: a heading written in ALL CAPS inside a section starts a new section of its own
    # (policies like EMAIL AND INTERNET POLICY often sit under a bigger heading), the contents
    # page is dropped, and a lone sub-heading repeating its section title is removed.
    split = []
    for title, lines in secs:
        cur = [title, []]; split.append(cur)
        for ln in lines:
            m = re.match(r'^## \**(.+?)\**\s*$', ln)
            if m and m.group(1).isupper() and len(m.group(1)) > 8:
                cur = [m.group(1), []]; split.append(cur)
            else:
                cur[1].append(ln)

    def nice(t):
        t = t.replace('**', '').strip()
        if not t.isupper(): return t
        w = t.replace('&', 'and').title().split()
        w = [x.lower() if i and x.lower() in ('and', 'of', 'to', 'with', 'for', 'the', 'in', 'our') else x for i, x in enumerate(w)]
        return ' '.join(w).replace('Randands', 'R&S')

    out = []
    for title, lines in split:
        if re.search(r'\bcontents\b', title, re.I): continue
        title = nice(title)
        lines = [re.sub(r'^## \*\*(.+?)\*\*\s*$', r'## \1', l) for l in lines]
        if lines and lines[0].strip().lower() == f'## {title}'.lower(): lines = lines[1:]
        body_txt = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()
        if body_txt: out.append((title[:200], body_txt))
    return out


@handbook_bp.route('/api/handbook/admin/import', methods=['POST'])
def hb_import():
    if not leave.is_admin(): return leave._auth_error()
    _ensure()
    data = request.get_data()
    if not data: return jsonify({'error': 'No file received'}), 400
    try: secs = parse_docx(data)
    except Exception as ex: return jsonify({'error': f'Could not read that Word file (use a .docx): {ex}'}), 400
    if not secs: return jsonify({'error': 'Nothing found in that document'}), 400
    if request.args.get('mode') == 'replace':
        leave.execute('DELETE FROM handbook_sections')
    pos = _next_pos()
    for i, (t, b) in enumerate(secs):
        leave.execute('INSERT INTO handbook_sections (position, title, body, updated_at) VALUES (?,?,?,?)',
                      (pos + i, t or 'Untitled', b[:60000], _now()))
    return jsonify({'success': True, 'count': len(secs)})
