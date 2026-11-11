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
            bc_job_id TEXT DEFAULT '', bc_status TEXT DEFAULT '', contact_id TEXT DEFAULT '', contact_name TEXT DEFAULT '',
            submitted_by_id INTEGER, bc_ref TEXT DEFAULT '', related_job TEXT DEFAULT '')''')
        for col in ('bc_job_id', 'bc_status', 'contact_id', 'contact_name'):          # tables created before the BigChange link existed
            try: leave.execute(f"ALTER TABLE part_returns ADD COLUMN {col} TEXT DEFAULT ''")
            except Exception: pass
        leave.execute(f'''CREATE TABLE IF NOT EXISTS part_return_photos (
            id {pk}, return_id INTEGER NOT NULL, name TEXT DEFAULT '', data TEXT NOT NULL)''')
        _state['done'] = True


def _s(v, n=300):
    return str(v if v is not None else '').strip()[:n]


def _ref(rid, created):
    return f"RET-{str(created)[:4]}-{int(rid):04d}"


DEFAULT_CATEGORY_ID = os.environ.get('BIGCHANGE_RETURN_CATEGORY_ID', '').strip() or '17045'
DEFAULT_FLAG_ID = os.environ.get('BIGCHANGE_RETURN_FLAG_ID', '').strip() or '128123'
DEFAULT_WORKSHEET_ID = os.environ.get('BIGCHANGE_RETURN_WORKSHEET_ID', '').strip() or '123284'
DEFAULT_TYPE_ID = os.environ.get('BIGCHANGE_RETURN_TYPE_ID', '').strip() or '81698'     # 'Parts to be returned'


def _cfg():
    try: c = json.loads(leave.get_setting('returns_bc') or '{}')
    except ValueError: c = {}
    c['typeId'] = c.get('typeId') or DEFAULT_TYPE_ID
    return c


# ---- worksheet: fill the job's worksheet from the form answers ------------------------------------------------
SKIP_TYPES = {'photo', 'signature', 'statement', 'file', 'barcode', 'appLaunch', 'ocr', 'cost', 'Unknown'}


def _pick(items, want):
    """Best matching entry of a list question for the wanted text (case-insensitive, either-way contains)."""
    w = want.lower()
    for i in items or []:
        if i.lower() == w: return i
    for i in items or []:
        if w in i.lower() or i.lower() in w: return i
    return None


def _guess(q, o, n):
    """What should this worksheet question hold? Returns (kind, value) or None. n counts questions already used per kind."""
    t = (q.get('questionText') or '').lower()
    ty = q.get('type')
    if ty in SKIP_TYPES: return None
    if 'serial' in t: return ('text', o['hpSerial']) if o['hpController'] else None
    if 'controller' in t or 'henny' in t: return ('bool', o['hpController'])
    if 'reason' in t: return ('choice', o['reason'])
    if ('additional' in t or 'another' in t or 'more than one' in t) and not ('number' in t or 'code' in t):
        return ('bool', len(o['parts']) > 1)
    if 'quant' in t or 'qty' in t:
        i = n['qty']; n['qty'] += 1
        return ('int', int(o['parts'][i]['qty'])) if i < len(o['parts']) else None
    if 'part' in t and ('number' in t or 'no' in t.split() or 'code' in t or t.strip() in ('part', 'part?')):
        i = n['part']; n['part'] += 1
        return ('text', o['parts'][i]['part']) if i < len(o['parts']) else None
    if any(w in t for w in ('packag', 'date', 'when', 'time')):
        return ('dt', o['packagedAt'])
    if 'site' in t or 'customer' in t: return ('text', o['site'])
    if 'returned by' in t or 'engineer' in t or 'name' in t: return ('text', o['by'])
    return None


def _answer(q, kind, val):
    ty = q.get('type'); items = q.get('validItems') or []
    if ty == 'boolean': return {'booleanValue': bool(val)} if kind == 'bool' else None
    if ty in ('listSingle', 'listIconSingle'):
        want = ('Yes' if val else 'No') if kind == 'bool' else str(val)
        pick = _pick(items, want)
        return {'selectedItem': pick} if pick else None
    if ty == 'numberInteger': return {'integerValue': int(val)} if kind == 'int' else None
    if ty == 'numberDecimal': return {'decimalValue': float(val)} if kind == 'int' else None
    if ty == 'text':
        s = ('Yes' if val else 'No') if kind == 'bool' else str(val).replace('T', ' ')
        return {'stringValue': s[:(q.get('maxLength') or 4000)]}
    if ty == 'date' and kind == 'dt': return {'date': str(val)[:10], 'time': str(val)[11:16]} if len(str(val)) >= 16 else {'date': str(val)[:10]}
    if ty == 'time' and kind == 'dt' and len(str(val)) >= 16: return {'time': str(val)[11:16]}
    return None


def _ws_questions(h, wsid):
    qs, page = [], 1
    while page < 10:
        d = h['get'](f'/worksheets/{wsid}/worksheetQuestions', {'pageNumber': page, 'pageSize': 100})
        items = d if isinstance(d, list) else (d.get('items') or [])
        qs += items
        if len(items) < 100: break
        page += 1
    return sorted(qs, key=lambda q: q.get('order') or 0)


def fill_worksheet(h, jid, o):
    """Attach the returns worksheet to the job (if the job type has not already) and answer what we can. Returns a short note."""
    wsid = int(DEFAULT_WORKSHEET_ID)
    qs = _ws_questions(h, wsid)
    try:
        cur = h['get'](f'/jobs/{jid}/worksheets', {})
        ids = [w['id'] for w in (cur if isinstance(cur, list) else (cur.get('items') or []))]
        if wsid not in ids and h.get('put'): h['put'](f'/jobs/{jid}/worksheets', {'worksheetIds': ids + [wsid]})
    except Exception:
        pass
    n, answers, left = {'qty': 0, 'part': 0}, [], []
    for q in qs:
        g = _guess(q, o, n)
        a = _answer(q, *g) if g else None
        if a is not None: answers.append({'questionId': q['id'], 'answer': a})
        elif q.get('type') not in SKIP_TYPES and not (g is None and q.get('type') in ('statement',)):
            left.append((q.get('questionText') or '')[:40])
    if answers: h['put'](f'/worksheets/{wsid}/answers', {'entityType': 'job', 'entityId': int(jid), 'answers': answers})
    return f"worksheet: {len(answers)} answered" + (f", left blank: {'; '.join(left)}" if left else '')


def raise_job(rid):
    """Create the BigChange job for a return and attach its photos. Never raises; the outcome is stored on the return."""
    h = leave.bc_hooks
    cfg = _cfg()
    rows = leave.query('SELECT * FROM part_returns WHERE id = ?', (rid,))
    if not rows: return 'missing'
    r = rows[0]
    if r.get('bc_job_id'): return 'already raised'
    type_id = cfg.get('typeId')
    contact = r.get('contact_id')
    if not (h.get('post') and type_id and contact):
        leave.execute('UPDATE part_returns SET bc_status = ? WHERE id = ?', ('not raised: job type not set up yet' if not type_id else 'not raised: no site chosen', rid))
        return 'not configured'
    o = _out(r)
    lines = '\n'.join(f"{p['part']} x {p['qty']}" for p in o['parts'])
    desc = (f"Part return {o['ref']}\nSite: {o['site']}\n" + (f"Related job: {o['relatedJob']}\n" if o['relatedJob'] else '') + f"Reason: {o['reason']}\nReturned by: {o['by']}\n"
            f"Packaged: {o['packagedAt'].replace('T', ' ')}\n\nParts:\n{lines}"
            + (f"\n\nHenny Penny controller serial: {o['hpSerial']}" if o['hpController'] else ''))
    try:
        res = h['post']('/jobs', {'typeId': int(type_id), 'contactId': int(contact), 'description': desc[:4000],
                                 'categoryId': int(DEFAULT_CATEGORY_ID)})
        jid = str(res.get('id') or res.get('jobId') or '')
        if not jid: raise RuntimeError('BigChange did not return a job id')
        bcref = str(res.get('reference') or '')
        if not bcref and h.get('get'):
            try: bcref = str((h['get'](f'/jobs/{jid}') or {}).get('reference') or '')
            except Exception: bcref = ''
    except Exception as ex:
        leave.execute('UPDATE part_returns SET bc_status = ? WHERE id = ?', (f'failed: {ex}'[:300], rid))
        return 'failed'
    notes = []
    try: notes.append(fill_worksheet(h, jid, o))
    except Exception as ex: notes.append(f'worksheet not filled: {ex}'[:200])
    try: h['post'](f'/jobs/{jid}/flags', {'flagId': int(DEFAULT_FLAG_ID), 'comments': f"Part return {o['ref']}"})
    except Exception as ex: notes.append(f'flag not added: {ex}'[:200])
    if h.get('upload'):
        import base64
        n = 0
        for p in leave.query('SELECT id, data FROM part_return_photos WHERE return_id = ? ORDER BY id', (rid,)):
            try:
                n += 1
                h['upload'](f'/jobs/{jid}/attachments', f"{o['ref']}-{n}.jpg", base64.b64decode(p['data'].split(',', 1)[1]))
            except Exception as ex:
                notes.append(f'photo {n} not uploaded: {ex}'[:200])
    status = 'job raised' + (' - ' + ' | '.join(notes) if notes else '')[:600]
    leave.execute('UPDATE part_returns SET bc_job_id = ?, bc_ref = ?, bc_status = ? WHERE id = ?', (jid, bcref or jid, status, rid))
    return 'raised'


def _session():
    import hub
    return hub._session()


def _who():
    s = _session()
    return s['name'] if s else ''


@returns_bp.route('/api/parts/returns', methods=['POST'])
def submit():
    if request.content_length and request.content_length > 10_000_000:
        return jsonify({'error': 'Photo is too large - try again'}), 413
    if leave._throttled('returns'):
        return jsonify({'error': 'Too many submissions from this connection - try again in 10 minutes'}), 429
    b = request.get_json(silent=True) or {}
    packaged, reason = _s(b.get('packagedAt'), 30), _s(b.get('reason'), 20)
    hp = b.get('hpController') in (True, 'yes', 'true', 1, '1')
    serial = _s(b.get('hpSerial'), 120)
    parts = []
    for p in (b.get('parts') or [])[:MAX_PARTS]:
        if not isinstance(p, dict): continue
        pn, q = _s(p.get('part'), 200), _s(p.get('qty'), 10)
        if pn: parts.append({'part': pn, 'qty': q})
    photos = [p for p in (b.get('photos') or [])[:MAX_PHOTOS] if isinstance(p, dict)
              and _s(p.get('data'), MAX_PHOTO_CHARS + 1).startswith('data:image/') and len(_s(p.get('data'), MAX_PHOTO_CHARS + 1)) <= MAX_PHOTO_CHARS]
    cid, cname = _s(b.get('contactId'), 20), _s(b.get('contactName'), 200)
    related = _s(b.get('relatedJob'), 80)
    errs = []
    if not cid.isdigit(): errs.append('the site / customer (pick it from the list)')
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
    sess = _session()
    who = (sess or {}).get('name') or _s(b.get('name'), 120) or 'Unknown'
    uid = (sess or {}).get('id')
    rid = leave.execute('''INSERT INTO part_returns (created_at, submitted_by, job_ref, packaged_at, reason, hp_controller, hp_serial, parts, contact_id, contact_name, submitted_by_id, related_job)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (now, who, '', packaged, reason, 1 if hp else 0, serial if hp else '',
                                                        json.dumps(parts, ensure_ascii=False), cid, cname, uid, related), returning_id=True)
    for p in photos:
        leave.execute('INSERT INTO part_return_photos (return_id, name, data) VALUES (?,?,?)', (rid, _s(p.get('name'), 120), p['data']))
    leave._fail('returns')
    ref = _ref(rid, now)
    try: raise_job(rid)
    except Exception: pass
    bcref = (leave.query('SELECT bc_ref FROM part_returns WHERE id = ?', (rid,)) or [{}])[0].get('bc_ref') or ''
    base = (os.environ.get('PUBLIC_BASE_URL') or request.host_url).rstrip('/')
    if base.startswith('http://') and 'localhost' not in base and '127.0.0.1' not in base:
        base = 'https://' + base[len('http://'):]
    lines = '\n'.join(f"  - {p['part']} x {p['qty']}" for p in parts)
    leave._dispatch([(RETURNS_EMAIL, f"Part return {ref}: {cname}",
                      f"A part return has been logged.\n\nReference: {ref}\nBigChange job: {bcref or 'not raised - see Staff admin'}\nReturned by: {who}\nReason: {REASONS[reason]}\n"
                      f"Packaged: {packaged.replace('T', ' ')}\nParts:\n{lines}\n"
                      + (f"Henny Penny controller serial: {serial}\n" if hp else '')
                      + f"\nPhoto and full details: {base}/staff-admin (Part returns tab)\n")])
    return jsonify({'success': True, 'ref': ref, 'bcRef': bcref})


def _out(r):
    return {'id': r['id'], 'ref': _ref(r['id'], r['created_at']), 'createdAt': r['created_at'], 'by': r['submitted_by'],
            'packagedAt': r['packaged_at'], 'reason': REASONS.get(r['reason'], r['reason']),
            'hpController': bool(r['hp_controller']), 'hpSerial': r['hp_serial'] or '', 'parts': json.loads(r['parts'] or '[]'),
            'site': r.get('contact_name') or '', 'relatedJob': r.get('related_job') or '', 'bcRef': r.get('bc_ref') or '', 'bcJobId': r.get('bc_job_id') or '', 'bcStatus': r.get('bc_status') or ''}


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


_contacts = {'ts': 0, 'rows': []}


def _all_contacts():
    """Every BigChange contact, cached for 10 minutes (paged fetch)."""
    import time
    if _contacts['rows'] and time.time() - _contacts['ts'] < 600: return _contacts['rows']
    rows, page = [], 1
    while page <= 40:
        items = _bc_items('/contacts', {'pageNumber': page, 'pageSize': 1000})
        rows += [{'id': c['id'], 'name': c.get('name') or c.get('contactName') or str(c['id']),
                  'extra': ', '.join(x for x in (c.get('reference'), (c.get('address') or '') if isinstance(c.get('address'), str) else '') if x)[:90]}
                 for c in items if c.get('id')]
        if len(items) < 1000: break
        page += 1
    _contacts.update(ts=time.time(), rows=rows)
    return rows


@returns_bp.route('/api/parts/returns/contacts')
def contacts():
    """Type-ahead for the return form: BigChange contacts whose name (or reference/address) contains every word typed."""
    q = _s(request.args.get('q'), 80).lower().split()
    if not q: return jsonify({'contacts': []})
    try: rows = _all_contacts()
    except Exception as ex: return jsonify({'error': f'Could not load sites from BigChange: {ex}'[:200]}), 502
    hit = [c for c in rows if all(w in (c['name'] + ' ' + c['extra']).lower() for w in q)]
    hit.sort(key=lambda c: (not c['name'].lower().startswith(q[0]), c['name'].lower()))
    return jsonify({'contacts': hit[:25], 'total': len(rows)})


@returns_bp.route('/api/parts/returns/admin/bc-config')
def bc_config():
    if not leave.is_admin(): return leave._auth_error()
    out = {'config': _cfg(), 'jobTypes': [], 'error': ''}
    try: out['jobTypes'] = [{'id': t['id'], 'name': t.get('name') or t.get('typeName') or str(t['id'])} for t in _bc_items('/jobTypes', {'pageSize': 500})]
    except Exception as ex: out['error'] = f'Could not load job types from BigChange: {ex}'[:300]
    return jsonify(out)


@returns_bp.route('/api/parts/returns/admin/bc-config', methods=['PUT'])
def bc_config_save():
    if not leave.is_admin(): return leave._auth_error()
    b = request.get_json(silent=True) or {}
    tid = str(b.get('typeId') or '')
    leave.set_setting('returns_bc', json.dumps({'typeId': tid if tid.isdigit() else ''}))
    return jsonify({'success': True})


@returns_bp.route('/api/parts/returns/admin/<int:rid>/raise-job', methods=['POST'])
def admin_raise(rid):
    if not leave.is_admin(): return leave._auth_error()
    _ensure_tables()
    res = raise_job(rid)
    row = leave.query('SELECT bc_status FROM part_returns WHERE id = ?', (rid,))
    return jsonify({'result': res, 'status': row[0]['bc_status'] if row else ''})


@returns_bp.route('/api/parts/returns/mine')
def mine():
    """The signed-in person's own returns, with the BigChange job reference, for their records."""
    s = _session()
    if not s or not s.get('id'): return jsonify({'returns': []})
    _ensure_tables()
    rows = leave.query('SELECT * FROM part_returns WHERE submitted_by_id = ? ORDER BY id DESC LIMIT 50', (s['id'],))
    return jsonify({'returns': [{k: v for k, v in _out(r).items() if k in ('ref', 'createdAt', 'site', 'reason', 'parts', 'bcRef', 'bcStatus', 'packagedAt')} for r in rows]})


@returns_bp.route('/api/parts/returns/admin/worksheet-check')
def worksheet_check():
    """Shows the worksheet questions and which form answer each one would get (using a sample return)."""
    if not leave.is_admin(): return leave._auth_error()
    sample = {'by': 'Sample Engineer', 'site': 'Sample Site', 'reason': 'Faulty', 'hpController': True, 'hpSerial': 'SN123',
              'packagedAt': '2026-11-11T12:00', 'parts': [{'part': 'PART-1', 'qty': '2'}, {'part': 'PART-2', 'qty': '1'}]}
    try: qs = _ws_questions(leave.bc_hooks, int(DEFAULT_WORKSHEET_ID))
    except Exception as ex: return jsonify({'error': f'Could not read the worksheet from BigChange: {ex}'[:300]}), 502
    n, out = {'qty': 0, 'part': 0}, []
    for q in qs:
        g = _guess(q, sample, n); a = _answer(q, *g) if g else None
        out.append({'text': q.get('questionText'), 'type': q.get('type'), 'items': q.get('validItems') or [], 'filled': a is not None,
                    'example': json.dumps(a) if a else ''})
    return jsonify({'worksheetId': DEFAULT_WORKSHEET_ID, 'questions': out})


STATUS_LABEL = {'completedOk': 'Completed', 'completedWithIssues': 'Completed with issues'}


@returns_bp.route('/api/parts/returns/recent-jobs')
def recent_jobs():
    """Jobs the signed-in engineer completed (OK or with issues) in the last 48 hours, from BigChange."""
    from datetime import timedelta, timezone
    s = _session()
    if not s or not s.get('id'): return jsonify({'jobs': [], 'reason': 'no-resource'})
    row = leave.query('SELECT bc_resource_id FROM leave_employees WHERE id = ?', (s['id'],))
    res = leave._bcid((row[0] if row else {}).get('bc_resource_id'))
    if not str(res).isdigit(): return jsonify({'jobs': [], 'reason': 'no-resource'})
    since = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        items = _bc_items('/jobs', {'resourceId': [int(res)], 'status': ['completedOk', 'completedWithIssues'],
                                    'statusModifiedAtFrom': since, 'pageSize': 100, 'sortBy': 'createdAt', 'direction': 'desc'})
    except Exception as ex:
        return jsonify({'jobs': [], 'reason': 'error', 'error': f'Could not load your recent jobs: {ex}'[:200]})
    jobs = [{'id': j.get('id'), 'reference': j.get('reference') or str(j.get('id')), 'contactId': j.get('contactId'),
             'contactName': j.get('contactName') or '', 'address': (j.get('contactAddress') or '')[:90] if isinstance(j.get('contactAddress'), str) else '',
             'type': j.get('typeName') or '', 'status': STATUS_LABEL.get(j.get('status'), j.get('status') or ''),
             'at': j.get('statusModifiedAt') or j.get('actualEndAt') or ''} for j in items]
    jobs.sort(key=lambda j: j['at'], reverse=True)
    return jsonify({'jobs': jobs})
