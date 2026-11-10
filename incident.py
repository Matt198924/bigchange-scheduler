"""Accident / incident report form for the staff hub (/incident) and the office view (/incident/office).

Anyone with the link can submit a report (write-only). Reading reports needs the office password (same login as
the leave admin). Reports and photos live in the leave database so they survive deploys.
"""
import json
import os
import threading
from datetime import datetime

from flask import Blueprint, jsonify, redirect, request, send_from_directory

import leave

incident_bp = Blueprint('incident', __name__)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'incident')
INCIDENT_EMAIL = os.environ.get('INCIDENT_EMAIL', '').strip() or leave.OFFICE_EMAIL
TYPES = {
    'accident': 'Accident / injury',
    'near_miss': 'Near miss',
    'dangerous': 'Dangerous occurrence',
    'damage': 'Property or equipment damage',
    'vehicle': 'Vehicle incident',
    'hazard': 'Unsafe condition / hazard',
    'abuse': 'Verbal or physical abuse',
    'other': 'Other',
}
STATUSES = ('new', 'review', 'closed')
RIDDOR = ('', 'not_reportable', 'reportable')
MAX_PHOTOS, MAX_PHOTO_CHARS = 6, 2_200_000       # ~1.6 MB each once decoded
_tables = {'done': False}
_lock = threading.Lock()


def _ensure_tables():
    if _tables['done']: return
    with _lock:
        if _tables['done']: return
        leave.init_db()
        pk = 'SERIAL PRIMARY KEY' if leave.IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        leave.execute(f'''CREATE TABLE IF NOT EXISTS incident_reports (
            id {pk}, created_at TEXT NOT NULL, occurred_at TEXT NOT NULL, type TEXT NOT NULL,
            reporter TEXT NOT NULL, person TEXT DEFAULT '', site TEXT DEFAULT '', injured INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'new', riddor TEXT NOT NULL DEFAULT '', office_notes TEXT DEFAULT '',
            data TEXT NOT NULL)''')
        leave.execute(f'''CREATE TABLE IF NOT EXISTS incident_photos (
            id {pk}, incident_id INTEGER NOT NULL, name TEXT DEFAULT '', data TEXT NOT NULL)''')
        _tables['done'] = True


def _ref(rid, created):
    return f"INC-{str(created)[:4]}-{int(rid):04d}"


def _s(v, n=2000):
    return str(v if v is not None else '').strip()[:n]


@incident_bp.route('/incident')
def incident_redirect():
    return redirect('/incident/', code=301)


@incident_bp.route('/incident/')
def incident_form():
    return send_from_directory(STATIC_DIR, 'index.html')


@incident_bp.route('/incident/office')
def incident_office():
    return send_from_directory(STATIC_DIR, 'office.html')


@incident_bp.route('/api/incident/config')
def config():
    return jsonify({'types': TYPES})


@incident_bp.route('/api/incident/report', methods=['POST'])
def submit():
    if request.content_length and request.content_length > 14_000_000:
        return jsonify({'error': 'Too much photo data - send fewer or smaller photos'}), 413
    body = request.get_json(silent=True) or {}
    if _s(body.get('website')):                      # honeypot: real people never fill this in
        return jsonify({'success': True, 'ref': 'INC-0000'})
    if leave._throttled('incident'):
        return jsonify({'error': 'Too many reports from this connection - try again in 10 minutes'}), 429
    reporter, typ = _s(body.get('reporter'), 120), _s(body.get('type'), 30)
    occurred, desc = _s(body.get('occurredAt'), 30), _s(body.get('description'), 5000)
    site = _s(body.get('site'), 300)
    errs = []
    if not reporter: errs.append('your name')
    if typ not in TYPES: errs.append('what type of incident it was')
    if not occurred: errs.append('when it happened')
    if not site: errs.append('where it happened')
    if not desc: errs.append('a description of what happened')
    if errs:
        return jsonify({'error': 'Please give ' + ', '.join(errs)}), 400
    try: datetime.fromisoformat(occurred)
    except ValueError: return jsonify({'error': 'That date and time is not valid'}), 400
    keys = ('reporterPhone', 'reporterRole', 'personRole', 'injuryBodyPart', 'injuryNature', 'firstAid', 'treatment',
            'offWork', 'witnesses', 'equipment', 'immediateAction', 'informedWho', 'otherNotes')
    data = {k: _s(body.get(k), 2000) for k in keys}
    data['description'] = desc
    injured = 1 if body.get('injured') in (True, 'yes', 'true', 1, '1') else 0
    photos = [p for p in (body.get('photos') or [])[:MAX_PHOTOS] if isinstance(p, dict)]
    _ensure_tables()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rid = leave.execute('''INSERT INTO incident_reports (created_at, occurred_at, type, reporter, person, site, injured, data)
                           VALUES (?,?,?,?,?,?,?,?)''',
                        (now, occurred, typ, reporter, _s(body.get('person'), 200), site, injured,
                         json.dumps(data, ensure_ascii=False)), returning_id=True)
    for p in photos:
        img = _s(p.get('data'), MAX_PHOTO_CHARS + 1)
        if img.startswith('data:image/') and len(img) <= MAX_PHOTO_CHARS:
            leave.execute('INSERT INTO incident_photos (incident_id, name, data) VALUES (?,?,?)',
                          (rid, _s(p.get('name'), 120), img))
    leave._fail('incident')                          # counts towards the per-connection limit
    ref = _ref(rid, now)
    base = (os.environ.get('PUBLIC_BASE_URL') or request.host_url).rstrip('/')
    if base.startswith('http://') and 'localhost' not in base and '127.0.0.1' not in base:
        base = 'https://' + base[len('http://'):]
    # No injury details in the email itself - those stay in the system.
    leave._dispatch([(INCIDENT_EMAIL, f"{'INJURY ' if injured else ''}Incident report {ref}: {TYPES[typ]}",
                      f"A new incident report has been submitted.\n\nReference: {ref}\nType: {TYPES[typ]}\n"
                      f"When: {occurred.replace('T', ' ')}\nWhere: {site}\nReported by: {reporter}\n"
                      f"Someone injured: {'YES' if injured else 'no'}\n\nOpen it: {base}/incident/office\n")])
    return jsonify({'success': True, 'ref': ref})


def _row_out(r, full=False):
    out = {'id': r['id'], 'ref': _ref(r['id'], r['created_at']), 'createdAt': r['created_at'], 'occurredAt': r['occurred_at'],
           'type': r['type'], 'typeLabel': TYPES.get(r['type'], r['type']), 'reporter': r['reporter'], 'person': r['person'] or '',
           'site': r['site'] or '', 'injured': bool(r['injured']), 'status': r['status'], 'riddor': r['riddor'] or ''}
    if full:
        out['data'] = json.loads(r['data'] or '{}')
        out['officeNotes'] = r['office_notes'] or ''
    return out


@incident_bp.route('/api/incident/admin/list')
def admin_list():
    if not leave.is_admin(): return leave._auth_error()
    _ensure_tables()
    rows = leave.query('SELECT id, created_at, occurred_at, type, reporter, person, site, injured, status, riddor, data FROM incident_reports ORDER BY occurred_at DESC, id DESC')
    return jsonify({'reports': [_row_out(r) for r in rows], 'types': TYPES})


@incident_bp.route('/api/incident/admin/<int:rid>')
def admin_one(rid):
    if not leave.is_admin(): return leave._auth_error()
    _ensure_tables()
    rows = leave.query('SELECT * FROM incident_reports WHERE id = ?', (rid,))
    if not rows: return jsonify({'error': 'Report not found'}), 404
    out = _row_out(rows[0], full=True)
    out['photos'] = [{'id': p['id'], 'name': p['name']} for p in
                     leave.query('SELECT id, name FROM incident_photos WHERE incident_id = ? ORDER BY id', (rid,))]
    return jsonify(out)


@incident_bp.route('/api/incident/admin/<int:rid>/photo/<int:pid>')
def admin_photo(rid, pid):
    if not leave.is_admin(): return leave._auth_error()
    _ensure_tables()
    rows = leave.query('SELECT data FROM incident_photos WHERE id = ? AND incident_id = ?', (pid, rid))
    if not rows: return jsonify({'error': 'Photo not found'}), 404
    return jsonify({'data': rows[0]['data']})


@incident_bp.route('/api/incident/admin/<int:rid>', methods=['PUT'])
def admin_update(rid):
    if not leave.is_admin(): return leave._auth_error()
    _ensure_tables()
    b = request.get_json(silent=True) or {}
    status, riddor = _s(b.get('status'), 10), _s(b.get('riddor'), 20)
    if status not in STATUSES or riddor not in RIDDOR:
        return jsonify({'error': 'Invalid status'}), 400
    if not leave.query('SELECT id FROM incident_reports WHERE id = ?', (rid,)):
        return jsonify({'error': 'Report not found'}), 404
    leave.execute('UPDATE incident_reports SET status = ?, riddor = ?, office_notes = ? WHERE id = ?',
                  (status, riddor, _s(b.get('officeNotes'), 5000), rid))
    return jsonify({'success': True})
