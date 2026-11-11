"""Return unwanted parts: form in the Parts tool (/parts/returns.html), log + email to the office.

Staff fill in the form; each return is stored (with its photo) in the leave database, emailed to RETURNS_EMAIL
(default: the office address) and listed in the office-only Staff admin > Part returns tab.
"""
import json
import os
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request

import leave

returns_bp = Blueprint('returns', __name__)
RETURNS_EMAIL = os.environ.get('RETURNS_EMAIL', '').strip() or leave.OFFICE_EMAIL
REASONS = {'faulty': 'Faulty', 'not_required': 'No longer required', 'incorrect': 'Incorrect part'}
MAX_PHOTOS, MAX_PHOTO_CHARS, MAX_PARTS = 4, 2_200_000, 10
_state = {'done': False}
_lock = threading.Lock()


def _ensure_tables():
    if _state['done']: return
    with _lock:
        if _state['done']: return
        leave.init_db()
        pk = 'SERIAL PRIMARY KEY' if leave.IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        leave.execute(f'''CREATE TABLE IF NOT EXISTS part_returns (
            id {pk}, created_at TEXT NOT NULL, submitted_by TEXT NOT NULL, job_ref TEXT NOT NULL,
            packaged_at TEXT NOT NULL, reason TEXT NOT NULL, hp_controller INTEGER NOT NULL DEFAULT 0,
            hp_serial TEXT DEFAULT '', parts TEXT NOT NULL)''')
        leave.execute(f'''CREATE TABLE IF NOT EXISTS part_return_photos (
            id {pk}, return_id INTEGER NOT NULL, name TEXT DEFAULT '', data TEXT NOT NULL)''')
        _state['done'] = True


def _s(v, n=300):
    return str(v if v is not None else '').strip()[:n]


def _ref(rid, created):
    return f"RET-{str(created)[:4]}-{int(rid):04d}"


def _who():
    import hub
    s = hub._session()
    return s['name'] if s else ''


@returns_bp.route('/api/parts/returns', methods=['POST'])
def submit():
    if request.content_length and request.content_length > 10_000_000:
        return jsonify({'error': 'Photo is too large - try again'}), 413
    if leave._throttled('returns'):
        return jsonify({'error': 'Too many submissions from this connection - try again in 10 minutes'}), 429
    b = request.get_json(silent=True) or {}
    job, packaged, reason = _s(b.get('jobRef'), 80), _s(b.get('packagedAt'), 30), _s(b.get('reason'), 20)
    hp = b.get('hpController') in (True, 'yes', 'true', 1, '1')
    serial = _s(b.get('hpSerial'), 120)
    parts = []
    for p in (b.get('parts') or [])[:MAX_PARTS]:
        if not isinstance(p, dict): continue
        pn, q = _s(p.get('part'), 200), _s(p.get('qty'), 10)
        if pn: parts.append({'part': pn, 'qty': q})
    photos = [p for p in (b.get('photos') or [])[:MAX_PHOTOS] if isinstance(p, dict)
              and _s(p.get('data'), MAX_PHOTO_CHARS + 1).startswith('data:image/') and len(_s(p.get('data'), MAX_PHOTO_CHARS + 1)) <= MAX_PHOTO_CHARS]
    errs = []
    if not job: errs.append('the job reference')
    if not parts: errs.append('a part number')
    if any(not (p['qty'].isdigit() and int(p['qty']) > 0) for p in parts): errs.append('a quantity for each part')
    if not packaged: errs.append('when the parts were packaged')
    if not photos: errs.append('a photo of the packaged, labelled parts')
    if reason not in REASONS: errs.append('the reason for returning')
    if hp and not serial: errs.append('the controller serial number')
    if errs: return jsonify({'error': 'Please give ' + ', '.join(errs)}), 400
    try: datetime.fromisoformat(packaged)
    except ValueError: return jsonify({'error': 'That date and time is not valid'}), 400
    _ensure_tables()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    who = _who() or _s(b.get('name'), 120) or 'Unknown'
    rid = leave.execute('''INSERT INTO part_returns (created_at, submitted_by, job_ref, packaged_at, reason, hp_controller, hp_serial, parts)
                           VALUES (?,?,?,?,?,?,?,?)''', (now, who, job, packaged, reason, 1 if hp else 0, serial if hp else '',
                                                        json.dumps(parts, ensure_ascii=False)), returning_id=True)
    for p in photos:
        leave.execute('INSERT INTO part_return_photos (return_id, name, data) VALUES (?,?,?)', (rid, _s(p.get('name'), 120), p['data']))
    leave._fail('returns')
    ref = _ref(rid, now)
    base = (os.environ.get('PUBLIC_BASE_URL') or request.host_url).rstrip('/')
    if base.startswith('http://') and 'localhost' not in base and '127.0.0.1' not in base:
        base = 'https://' + base[len('http://'):]
    lines = '\n'.join(f"  - {p['part']} x {p['qty']}" for p in parts)
    leave._dispatch([(RETURNS_EMAIL, f"Part return {ref}: job {job}",
                      f"A part return has been logged.\n\nReference: {ref}\nJob: {job}\nReturned by: {who}\nReason: {REASONS[reason]}\n"
                      f"Packaged: {packaged.replace('T', ' ')}\nParts:\n{lines}\n"
                      + (f"Henny Penny controller serial: {serial}\n" if hp else '')
                      + f"\nPhoto and full details: {base}/staff-admin (Part returns tab)\n")])
    return jsonify({'success': True, 'ref': ref})


def _out(r):
    return {'id': r['id'], 'ref': _ref(r['id'], r['created_at']), 'createdAt': r['created_at'], 'by': r['submitted_by'],
            'jobRef': r['job_ref'], 'packagedAt': r['packaged_at'], 'reason': REASONS.get(r['reason'], r['reason']),
            'hpController': bool(r['hp_controller']), 'hpSerial': r['hp_serial'] or '', 'parts': json.loads(r['parts'] or '[]')}


@returns_bp.route('/api/parts/returns/admin/list')
def admin_list():
    if not leave.is_admin(): return leave._auth_error()
    _ensure_tables()
    rows = leave.query('SELECT * FROM part_returns ORDER BY id DESC LIMIT 300')
    out = []
    for r in rows:
        o = _out(r)
        o['photos'] = [p['id'] for p in leave.query('SELECT id FROM part_return_photos WHERE return_id = ? ORDER BY id', (r['id'],))]
        out.append(o)
    return jsonify({'returns': out})


@returns_bp.route('/api/parts/returns/admin/<int:rid>/photo/<int:pid>')
def admin_photo(rid, pid):
    if not leave.is_admin(): return leave._auth_error()
    _ensure_tables()
    rows = leave.query('SELECT data FROM part_return_photos WHERE id = ? AND return_id = ?', (pid, rid))
    if not rows: return jsonify({'error': 'Photo not found'}), 404
    return jsonify({'data': rows[0]['data']})
