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
            hp_serial TEXT DEFAULT '', parts TEXT NOT NULL,
            bc_job_id TEXT DEFAULT '', bc_status TEXT DEFAULT '')''')
        for col in ('bc_job_id', 'bc_status'):          # tables created before the BigChange link existed
            try: leave.execute(f"ALTER TABLE part_returns ADD COLUMN {col} TEXT DEFAULT ''")
            except Exception: pass
        leave.execute(f'''CREATE TABLE IF NOT EXISTS part_return_photos (
            id {pk}, return_id INTEGER NOT NULL, name TEXT DEFAULT '', data TEXT NOT NULL)''')
        _state['done'] = True


def _s(v, n=300):
    return str(v if v is not None else '').strip()[:n]


def _ref(rid, created):
    return f"RET-{str(created)[:4]}-{int(rid):04d}"


def _cfg():
    try: return json.loads(leave.get_setting('returns_bc') or '{}')
    except ValueError: return {}


def raise_job(rid):
    """Create the BigChange job for a return and attach its photos. Never raises; the outcome is stored on the return."""
    h = leave.bc_hooks
    cfg = _cfg()
    rows = leave.query('SELECT * FROM part_returns WHERE id = ?', (rid,))
    if not rows: return 'missing'
    r = rows[0]
    if r.get('bc_job_id'): return 'already raised'
    type_id = (cfg.get('types') or {}).get(r['reason']) or (cfg.get('types') or {}).get('default')
    contact = (cfg.get('contact') or {}).get('id')
    if not (h.get('post') and type_id and contact):
        leave.execute('UPDATE part_returns SET bc_status = ? WHERE id = ?', ('not raised: job type / contact not set up yet', rid))
        return 'not configured'
    o = _out(r)
    lines = '\n'.join(f"{p['part']} x {p['qty']}" for p in o['parts'])
    desc = (f"Part return {o['ref']} - job {o['jobRef']}\nReason: {o['reason']}\nReturned by: {o['by']}\n"
            f"Packaged: {o['packagedAt'].replace('T', ' ')}\n\nParts:\n{lines}"
            + (f"\n\nHenny Penny controller serial: {o['hpSerial']}" if o['hpController'] else ''))
    try:
        res = h['post']('/jobs', {'typeId': int(type_id), 'contactId': int(contact), 'reference': o['ref'], 'description': desc[:4000]})
        jid = str(res.get('id') or res.get('jobId') or '')
        if not jid: raise RuntimeError('BigChange did not return a job id')
    except Exception as ex:
        leave.execute('UPDATE part_returns SET bc_status = ? WHERE id = ?', (f'failed: {ex}'[:300], rid))
        return 'failed'
    status = 'job raised'
    if h.get('upload'):
        import base64
        n = 0
        for p in leave.query('SELECT id, data FROM part_return_photos WHERE return_id = ? ORDER BY id', (rid,)):
            try:
                n += 1
                h['upload'](f'/jobs/{jid}/attachments', f"{o['ref']}-{n}.jpg", base64.b64decode(p['data'].split(',', 1)[1]))
            except Exception as ex:
                status = f'job raised, but photo {n} did not upload: {ex}'[:300]
    leave.execute('UPDATE part_returns SET bc_job_id = ?, bc_status = ? WHERE id = ?', (jid, status, rid))
    return 'raised'


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
    try: raise_job(rid)
    except Exception: pass
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
            'hpController': bool(r['hp_controller']), 'hpSerial': r['hp_serial'] or '', 'parts': json.loads(r['parts'] or '[]'),
            'bcJobId': r.get('bc_job_id') or '', 'bcStatus': r.get('bc_status') or ''}


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


def _bc_items(path, params):
    data = leave.bc_hooks['get'](path, params)
    return data if isinstance(data, list) else (data.get('items') or [])


@returns_bp.route('/api/parts/returns/admin/bc-config')
def bc_config():
    if not leave.is_admin(): return leave._auth_error()
    out = {'config': _cfg(), 'jobTypes': [], 'error': ''}
    try: out['jobTypes'] = [{'id': t['id'], 'name': t.get('name') or t.get('typeName') or str(t['id'])} for t in _bc_items('/jobTypes', {'pageSize': 500})]
    except Exception as ex: out['error'] = f'Could not load job types from BigChange: {ex}'[:300]
    return jsonify(out)


@returns_bp.route('/api/parts/returns/admin/bc-contacts')
def bc_contacts():
    if not leave.is_admin(): return leave._auth_error()
    q = _s(request.args.get('q'), 80)
    if len(q) < 2: return jsonify({'contacts': []})
    try: items = _bc_items('/contacts', {'name': q, 'pageSize': 20})
    except Exception as ex: return jsonify({'error': f'Could not search BigChange: {ex}'[:300]}), 502
    return jsonify({'contacts': [{'id': c['id'], 'name': c.get('name') or c.get('contactName') or str(c['id'])} for c in items]})


@returns_bp.route('/api/parts/returns/admin/bc-config', methods=['PUT'])
def bc_config_save():
    if not leave.is_admin(): return leave._auth_error()
    b = request.get_json(silent=True) or {}
    types = {k: str(v) for k, v in (b.get('types') or {}).items() if k in (*REASONS, 'default') and str(v).isdigit()}
    c = b.get('contact') or {}
    contact = {'id': str(c.get('id')), 'name': _s(c.get('name'), 200)} if str(c.get('id') or '').isdigit() else {}
    leave.set_setting('returns_bc', json.dumps({'types': types, 'contact': contact}))
    return jsonify({'success': True})


@returns_bp.route('/api/parts/returns/admin/<int:rid>/raise-job', methods=['POST'])
def admin_raise(rid):
    if not leave.is_admin(): return leave._auth_error()
    _ensure_tables()
    res = raise_job(rid)
    row = leave.query('SELECT bc_status FROM part_returns WHERE id = ?', (rid,))
    return jsonify({'result': res, 'status': row[0]['bc_status'] if row else ''})
