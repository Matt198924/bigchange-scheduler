"""Annual leave booking - runs inside the scheduler app as a Flask blueprint.

Staff open /leave, pick their name, enter a PIN, request leave. Each employee
has a manager (is_manager employee) who approves their requests from their own
login; employees with no manager go to the office, who can also decide anything
from the admin tab (password in LEAVE_ADMIN_PASSWORD).
The scheduler can ask /api/leave/off?date=YYYY-MM-DD who is away.

Storage: Postgres when DATABASE_URL is set (use this on Render - the free
web-service disk is wiped on every deploy), otherwise a local SQLite file
for development.

Env vars
  DATABASE_URL            Postgres connection string (Render/Supabase)
  LEAVE_ADMIN_PASSWORD    office admin password (admin tab disabled if unset)
  LEAVE_SECRET            signing key for login tokens (falls back to
                          BIGCHANGE_CLIENT_SECRET)
  LEAVE_DEFAULT_ALLOWANCE default days for new employees (default 20)
  LEAVE_YEAR_START_MONTH  1-12, month the leave year starts (default 1)
"""
import os
import hmac
import secrets
import time
import sqlite3
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request, send_from_directory
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash, check_password_hash

leave_bp = Blueprint('leave', __name__)

DATABASE_URL     = os.environ.get('DATABASE_URL', '')
SQLITE_PATH      = os.environ.get('LEAVE_SQLITE_PATH', 'leave.db')
ADMIN_PASSWORD   = os.environ.get('LEAVE_ADMIN_PASSWORD', '')
SECRET           = (os.environ.get('LEAVE_SECRET')
                    or os.environ.get('BIGCHANGE_CLIENT_SECRET') or 'dev-secret-change-me')
DEFAULT_ALLOWANCE = float(os.environ.get('LEAVE_DEFAULT_ALLOWANCE', '20'))
YEAR_START_MONTH = min(12, max(1, int(os.environ.get('LEAVE_YEAR_START_MONTH', '1'))))

LEAVE_TYPES = ['annual', 'unpaid', 'other']
IS_PG = bool(DATABASE_URL)
_serializer = URLSafeTimedSerializer(SECRET, salt='leave-auth')

# England & Wales bank holidays. Editable in the admin tab; seeded once.
_SEED_BANK_HOLIDAYS = [
    ('2026-12-25', 'Christmas Day'), ('2026-12-28', 'Boxing Day (substitute)'),
    ('2027-01-01', "New Year's Day"), ('2027-03-26', 'Good Friday'),
    ('2027-03-29', 'Easter Monday'), ('2027-05-03', 'Early May bank holiday'),
    ('2027-05-31', 'Spring bank holiday'), ('2027-08-30', 'Summer bank holiday'),
    ('2027-12-27', 'Christmas Day (substitute)'), ('2027-12-28', 'Boxing Day (substitute)'),
]

# ── DB ────────────────────────────────────────────────────────────────────
def _connect():
    if IS_PG:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL)
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        return conn
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _sql(sql):
    return sql.replace('?', '%s') if IS_PG else sql

def query(sql, params=()):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(_sql(sql), params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def execute(sql, params=(), returning_id=False):
    conn = _connect()
    try:
        cur = conn.cursor()
        if returning_id and IS_PG:
            cur.execute(_sql(sql) + ' RETURNING id', params)
            new_id = cur.fetchone()['id']
        else:
            cur.execute(_sql(sql), params)
            new_id = cur.lastrowid if not IS_PG else None
        conn.commit()
        return new_id
    finally:
        conn.close()

_inited = False
def init_db():
    global _inited
    if _inited:
        return
    pk = 'SERIAL PRIMARY KEY' if IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    execute(f'''CREATE TABLE IF NOT EXISTS leave_employees (
        id {pk}, name TEXT NOT NULL, pin_hash TEXT, allowance REAL NOT NULL,
        supervisor TEXT DEFAULT '', bc_resource_id TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1)''')
    execute(f'''CREATE TABLE IF NOT EXISTS leave_requests (
        id {pk}, employee_id INTEGER NOT NULL, start_date TEXT NOT NULL,
        end_date TEXT NOT NULL, half_day TEXT NOT NULL DEFAULT '',
        days REAL NOT NULL, type TEXT NOT NULL DEFAULT 'annual', note TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', admin_note TEXT DEFAULT '',
        created_at TEXT NOT NULL, decided_at TEXT)''')
    for col, ddl in (('manager_id', 'INTEGER'), ('is_manager', 'INTEGER NOT NULL DEFAULT 0')):
        try:
            execute(f'ALTER TABLE leave_employees ADD COLUMN {col} {ddl}')
        except Exception:
            pass  # column already exists
    execute('CREATE TABLE IF NOT EXISTS leave_bank_holidays (date TEXT PRIMARY KEY, name TEXT NOT NULL)')
    if not query('SELECT date FROM leave_bank_holidays LIMIT 1'):
        for d, n in _SEED_BANK_HOLIDAYS:
            execute('INSERT INTO leave_bank_holidays (date, name) VALUES (?, ?)', (d, n))
    _inited = True

@leave_bp.before_request
def _ensure_db():
    init_db()

# ── Dates / day counting ──────────────────────────────────────────────────
def parse_date(s):
    return datetime.strptime(s, '%Y-%m-%d').date()

def iso(d):
    return d.strftime('%Y-%m-%d')

def bank_holiday_set():
    return {r['date'] for r in query('SELECT date FROM leave_bank_holidays')}

def working_days(start, end, half_day='', bhs=None, lo=None, hi=None):
    """Mon-Fri, excluding bank holidays. lo/hi clip the range (leave-year maths)."""
    bhs = bank_holiday_set() if bhs is None else bhs
    if lo and start < lo: start = lo
    if hi and end > hi: end = hi
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5 and iso(d) not in bhs:
            n += 1
        d += timedelta(days=1)
    if half_day and start == end and n == 1:
        return 0.5
    return float(n)

def year_bounds(on):
    y = on.year if on.month >= YEAR_START_MONTH else on.year - 1
    lo = date(y, YEAR_START_MONTH, 1)
    hi = date(y + 1, YEAR_START_MONTH, 1) - timedelta(days=1)
    return lo, hi

def balance_for(emp, on=None):
    lo, hi = year_bounds(on or date.today())
    bhs = bank_holiday_set()
    rows = query('''SELECT start_date, end_date, half_day, status FROM leave_requests
                    WHERE employee_id = ? AND type = 'annual'
                    AND status IN ('approved','pending')''', (emp['id'],))
    taken = pending = 0.0
    for r in rows:
        d = working_days(parse_date(r['start_date']), parse_date(r['end_date']),
                         r['half_day'], bhs, lo, hi)
        if r['status'] == 'approved': taken += d
        else: pending += d
    return {'yearStart': iso(lo), 'yearEnd': iso(hi), 'allowance': emp['allowance'],
            'taken': taken, 'pending': pending,
            'remaining': emp['allowance'] - taken - pending}

# ── Auth ──────────────────────────────────────────────────────────────────
_fails = {}  # (ip, key) -> [timestamps]

def _throttled(key):
    k = (request.remote_addr, key)
    now = time.time()
    _fails[k] = [t for t in _fails.get(k, []) if now - t < 600]
    return len(_fails[k]) >= 5

def _fail(key):
    _fails.setdefault((request.remote_addr, key), []).append(time.time())

def _token(payload):
    return _serializer.dumps(payload)

def _read_token():
    h = request.headers.get('Authorization', '')
    if not h.startswith('Bearer '):
        return None
    try:
        return _serializer.loads(h[7:], max_age=60 * 60 * 12)
    except (BadSignature, SignatureExpired):
        return None

def _auth_error():
    return jsonify({'error': 'Not signed in'}), 401

def current_employee():
    t = _read_token()
    if not t or t.get('role') != 'employee':
        return None
    rows = query('SELECT * FROM leave_employees WHERE id = ? AND active = 1', (t['id'],))
    return rows[0] if rows else None

def is_admin():
    t = _read_token()
    return bool(t and t.get('role') == 'admin')

def _pin_ok(pin):
    return isinstance(pin, str) and pin.isdigit() and 4 <= len(pin) <= 6

# ── Request helpers ───────────────────────────────────────────────────────
def off_on(start, end, exclude_id=None, statuses=('approved',)):
    """Who else is off in this range: {date: [names]}."""
    ph = ','.join('?' * len(statuses))
    rows = query(f'''SELECT r.id, r.start_date, r.end_date, e.name FROM leave_requests r
                     JOIN leave_employees e ON e.id = r.employee_id
                     WHERE r.status IN ({ph}) AND r.start_date <= ? AND r.end_date >= ?''',
                 (*statuses, iso(end), iso(start)))
    out = {}
    for r in rows:
        if exclude_id and r['id'] == exclude_id: continue
        d = max(parse_date(r['start_date']), start)
        e = min(parse_date(r['end_date']), end)
        while d <= e:
            if d.weekday() < 5:
                out.setdefault(iso(d), []).append(r['name'])
            d += timedelta(days=1)
    return out

def clash_summary(start, end, employee_id=None, request_id=None):
    off = off_on(start, end, exclude_id=request_id)
    if employee_id:  # don't flag someone clashing with themselves
        own = {r['name'] for r in query('SELECT name FROM leave_employees WHERE id = ?', (employee_id,))}
        off = {d: [n for n in names if n not in own] for d, names in off.items()}
    off = {d: n for d, n in off.items() if n}
    names = sorted({n for v in off.values() for n in v})
    return {'maxOff': max((len(v) for v in off.values()), default=0), 'names': names}

def validate_range(body):
    try:
        start, end = parse_date(body.get('startDate', '')), parse_date(body.get('endDate') or body.get('startDate', ''))
    except ValueError:
        return None, 'Dates must be YYYY-MM-DD'
    if end < start:
        return None, 'End date is before start date'
    if (end - start).days > 60:
        return None, 'That range is too long - split it into separate requests'
    half = body.get('halfDay') or ''
    if half not in ('', 'am', 'pm'):
        return None, 'halfDay must be am or pm'
    if half and start != end:
        return None, 'Half days only apply to a single day'
    typ = body.get('type') or 'annual'
    if typ not in LEAVE_TYPES:
        return None, 'Unknown leave type'
    days = working_days(start, end, half)
    if days == 0:
        return None, 'No working days in that range (weekends and bank holidays are not counted)'
    return {'start': start, 'end': end, 'half': half, 'type': typ, 'days': days}, None

def overlaps_own(emp_id, start, end, exclude_id=None):
    rows = query('''SELECT id FROM leave_requests WHERE employee_id = ?
                    AND status IN ('approved','pending')
                    AND start_date <= ? AND end_date >= ?''', (emp_id, iso(end), iso(start)))
    return any(r['id'] != exclude_id for r in rows)

def fmt_request(r):
    return {k: r[k] for k in ('id', 'start_date', 'end_date', 'half_day', 'days', 'type',
                               'note', 'status', 'admin_note', 'created_at', 'decided_at')} | {
        'employeeId': r['employee_id'], 'employeeName': r.get('name'),
        'managerName': r.get('manager_name')}

def create_request(emp, v, note, status='pending', admin_note=''):
    now = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')
    decided = now if status == 'approved' else None
    return execute('''INSERT INTO leave_requests
        (employee_id, start_date, end_date, half_day, days, type, note, status,
         admin_note, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (emp['id'], iso(v['start']), iso(v['end']), v['half'], v['days'], v['type'],
         (note or '')[:500], status, admin_note, now, decided), returning_id=True)

# ── Pages ─────────────────────────────────────────────────────────────────
@leave_bp.route('/leave')
def leave_page():
    return send_from_directory('static', 'leave.html')

# ── Public ────────────────────────────────────────────────────────────────
@leave_bp.route('/api/leave/config')
def config():
    return jsonify({'types': LEAVE_TYPES, 'adminEnabled': bool(ADMIN_PASSWORD),
                    'bankHolidays': sorted(bank_holiday_set()),
                    'yearStartMonth': YEAR_START_MONTH})

@leave_bp.route('/api/leave/employees')
def list_employees_public():
    rows = query('SELECT id, name FROM leave_employees WHERE active = 1 ORDER BY name')
    return jsonify({'employees': rows})

@leave_bp.route('/api/leave/off')
def off_today():
    """For the scheduler: approved leave covering a date or range.
    ?date=YYYY-MM-DD or ?from=...&to=... (defaults to today)."""
    try:
        a = parse_date(request.args.get('from') or request.args.get('date') or iso(date.today()))
        b = parse_date(request.args.get('to') or iso(a))
    except ValueError:
        return jsonify({'error': 'bad date'}), 400
    rows = query('''SELECT e.id, e.name, e.bc_resource_id, r.start_date, r.end_date, r.half_day
                    FROM leave_requests r JOIN leave_employees e ON e.id = r.employee_id
                    WHERE r.status = 'approved' AND r.start_date <= ? AND r.end_date >= ?
                    ORDER BY e.name''', (iso(b), iso(a)))
    return jsonify({'off': [{'employeeId': r['id'], 'name': r['name'],
                             'resourceId': r['bc_resource_id'], 'start': r['start_date'],
                             'end': r['end_date'], 'halfDay': r['half_day']} for r in rows]})

# ── Login ─────────────────────────────────────────────────────────────────
@leave_bp.route('/api/leave/login', methods=['POST'])
def login():
    body = request.get_json(silent=True) or {}
    key = f"emp{body.get('employeeId')}"
    if _throttled(key):
        return jsonify({'error': 'Too many attempts - try again in 10 minutes'}), 429
    rows = query('SELECT * FROM leave_employees WHERE id = ? AND active = 1', (body.get('employeeId'),))
    emp = rows[0] if rows else None
    if not emp or not emp['pin_hash'] or not check_password_hash(emp['pin_hash'], str(body.get('pin', ''))):
        _fail(key)
        return jsonify({'error': 'Wrong PIN (ask the office if you have not been given one)'}), 403
    return jsonify({'token': _token({'role': 'employee', 'id': emp['id']}), 'name': emp['name']})

@leave_bp.route('/api/leave/admin/login', methods=['POST'])
def admin_login():
    if not ADMIN_PASSWORD:
        return jsonify({'error': 'Admin is not enabled (LEAVE_ADMIN_PASSWORD not set)'}), 503
    if _throttled('admin'):
        return jsonify({'error': 'Too many attempts - try again in 10 minutes'}), 429
    pw = (request.get_json(silent=True) or {}).get('password', '')
    if not hmac.compare_digest(str(pw).encode(), ADMIN_PASSWORD.encode()):
        _fail('admin')
        return jsonify({'error': 'Wrong password'}), 403
    return jsonify({'token': _token({'role': 'admin'})})

# ── Employee endpoints ────────────────────────────────────────────────────
@leave_bp.route('/api/leave/me')
def me():
    emp = current_employee()
    if not emp: return _auth_error()
    rows = query('''SELECT r.*, e.name FROM leave_requests r JOIN leave_employees e ON e.id = r.employee_id
                    WHERE r.employee_id = ? ORDER BY r.start_date DESC''', (emp['id'],))
    mgr = query('SELECT name FROM leave_employees WHERE id = ?', (emp['manager_id'],)) if emp.get('manager_id') else []
    return jsonify({'name': emp['name'], 'isManager': bool(emp.get('is_manager')),
                    'managerName': mgr[0]['name'] if mgr else None, 'balance': balance_for(emp),
                    'requests': [fmt_request(r) for r in rows]})

@leave_bp.route('/api/leave/preview', methods=['POST'])
def preview():
    emp = current_employee()
    if not emp and not is_admin(): return _auth_error()
    body = request.get_json(silent=True) or {}
    v, err = validate_range(body)
    if err: return jsonify({'error': err}), 400
    target = emp
    if not target and body.get('employeeId'):
        rows = query('SELECT * FROM leave_employees WHERE id = ?', (body['employeeId'],))
        target = rows[0] if rows else None
    out = {'days': v['days'], 'clash': clash_summary(v['start'], v['end'], target['id'] if target else None)}
    if target and v['type'] == 'annual':
        out['remainingAfter'] = balance_for(target, v['start'])['remaining'] - v['days']
    return jsonify(out)

@leave_bp.route('/api/leave/requests', methods=['POST'])
def request_leave():
    emp = current_employee()
    if not emp: return _auth_error()
    body = request.get_json(silent=True) or {}
    v, err = validate_range(body)
    if err: return jsonify({'error': err}), 400
    if v['start'] < date.today():
        return jsonify({'error': 'Cannot request leave in the past - call the office'}), 400
    if overlaps_own(emp['id'], v['start'], v['end']):
        return jsonify({'error': 'You already have leave booked or pending on those dates'}), 409
    lo, hi = year_bounds(v['start'])
    if year_bounds(v['end'])[0] != lo:
        return jsonify({'error': 'That spans two leave years - please make two requests'}), 400
    if v['type'] == 'annual' and balance_for(emp, v['start'])['remaining'] < v['days']:
        return jsonify({'error': 'Not enough annual leave left for that request'}), 400
    rid = create_request(emp, v, body.get('note'))
    return jsonify({'success': True, 'id': rid})

@leave_bp.route('/api/leave/requests/<int:rid>/cancel', methods=['POST'])
def cancel_own(rid):
    emp = current_employee()
    if not emp: return _auth_error()
    rows = query('SELECT * FROM leave_requests WHERE id = ? AND employee_id = ?', (rid, emp['id']))
    if not rows: return jsonify({'error': 'Not found'}), 404
    r = rows[0]
    if r['status'] not in ('pending', 'approved'):
        return jsonify({'error': 'Already closed'}), 400
    if r['status'] == 'approved' and parse_date(r['start_date']) <= date.today():
        return jsonify({'error': 'Leave has started - call the office'}), 400
    execute("UPDATE leave_requests SET status = 'cancelled', decided_at = ? WHERE id = ?",
            (datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'), rid))
    return jsonify({'success': True})

@leave_bp.route('/api/leave/calendar')
def calendar():
    """Who is off, per day, for ?month=YYYY-MM. Signed-in staff or admin."""
    if not current_employee() and not is_admin(): return _auth_error()
    try:
        y, m = map(int, (request.args.get('month') or date.today().strftime('%Y-%m')).split('-'))
        first = date(y, m, 1)
    except ValueError:
        return jsonify({'error': 'month must be YYYY-MM'}), 400
    last = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
    approved = off_on(first, last)
    pending = off_on(first, last, statuses=('pending',)) if is_admin() else {}
    return jsonify({'month': first.strftime('%Y-%m'), 'approved': approved, 'pending': pending,
                    'bankHolidays': [d for d in sorted(bank_holiday_set()) if iso(first) <= d <= iso(last)]})

# ── Admin endpoints ───────────────────────────────────────────────────────
def _request_list(status=None, manager_id=None):
    sql = '''SELECT r.*, e.name, m.name AS manager_name FROM leave_requests r
           JOIN leave_employees e ON e.id = r.employee_id
           LEFT JOIN leave_employees m ON m.id = e.manager_id'''
    where, params = [], []
    if status:
        where.append('r.status = ?'); params.append(status)
    if manager_id:
        where.append('e.manager_id = ?'); params.append(manager_id)
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY r.start_date'
    out = []
    for r in query(sql, tuple(params)):
        f = fmt_request(r)
        if r['status'] == 'pending':
            f['clash'] = clash_summary(parse_date(r['start_date']), parse_date(r['end_date']),
                                       r['employee_id'], r['id'])
            emp = query('SELECT * FROM leave_employees WHERE id = ?', (r['employee_id'],))[0]
            f['balance'] = balance_for(emp, parse_date(r['start_date']))
        out.append(f)
    return out

def _decide(rid, body, actor=None):
    """actor=None means office admin (can decide anything). Otherwise actor is a
    manager and may only decide their own team's requests, never their own."""
    action = body.get('action')
    new_status = {'approve': 'approved', 'reject': 'rejected', 'cancel': 'cancelled'}.get(action)
    if not new_status: return jsonify({'error': 'action must be approve, reject or cancel'}), 400
    rows = query('''SELECT r.id, r.employee_id, e.manager_id FROM leave_requests r
                    JOIN leave_employees e ON e.id = r.employee_id WHERE r.id = ?''', (rid,))
    if not rows: return jsonify({'error': 'Not found'}), 404
    if actor and (rows[0]['manager_id'] != actor['id'] or rows[0]['employee_id'] == actor['id']):
        return jsonify({'error': 'That request is not yours to decide'}), 403
    execute('UPDATE leave_requests SET status = ?, admin_note = ?, decided_at = ? WHERE id = ?',
            (new_status, (body.get('note') or '')[:300],
             datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'), rid))
    return jsonify({'success': True})

@leave_bp.route('/api/leave/admin/requests')
def admin_requests():
    if not is_admin(): return _auth_error()
    return jsonify({'requests': _request_list(request.args.get('status'))})

@leave_bp.route('/api/leave/admin/requests/<int:rid>/decide', methods=['POST'])
def admin_decide(rid):
    if not is_admin(): return _auth_error()
    return _decide(rid, request.get_json(silent=True) or {})

# Managers (supervisors) handle requests from the people assigned to them.
def current_manager():
    emp = current_employee()
    return emp if emp and emp.get('is_manager') else None

@leave_bp.route('/api/leave/team/requests')
def team_requests():
    mgr = current_manager()
    if not mgr: return _auth_error()
    return jsonify({'requests': _request_list(request.args.get('status'), manager_id=mgr['id'])})

@leave_bp.route('/api/leave/team/requests/<int:rid>/decide', methods=['POST'])
def team_decide(rid):
    mgr = current_manager()
    if not mgr: return _auth_error()
    return _decide(rid, request.get_json(silent=True) or {}, actor=mgr)

@leave_bp.route('/api/leave/admin/requests', methods=['POST'])
def admin_book():
    """Office books leave on someone's behalf (phone call, sickness-adjacent
    admin, past dates). Auto-approved."""
    if not is_admin(): return _auth_error()
    body = request.get_json(silent=True) or {}
    rows = query('SELECT * FROM leave_employees WHERE id = ?', (body.get('employeeId'),))
    if not rows: return jsonify({'error': 'Pick an employee'}), 400
    v, err = validate_range(body)
    if err: return jsonify({'error': err}), 400
    if overlaps_own(rows[0]['id'], v['start'], v['end']):
        return jsonify({'error': 'They already have leave booked or pending on those dates'}), 409
    rid = create_request(rows[0], v, body.get('note'), status='approved', admin_note='Booked by office')
    return jsonify({'success': True, 'id': rid})

@leave_bp.route('/api/leave/admin/employees')
def admin_employees():
    if not is_admin(): return _auth_error()
    out = []
    for e in query('SELECT * FROM leave_employees ORDER BY active DESC, name'):
        out.append({'id': e['id'], 'name': e['name'], 'allowance': e['allowance'],
                    'managerId': e.get('manager_id'), 'isManager': bool(e.get('is_manager')),
                    'bcResourceId': e['bc_resource_id'],
                    'active': bool(e['active']), 'hasPin': bool(e['pin_hash']),
                    'balance': balance_for(e)})
    return jsonify({'employees': out})

@leave_bp.route('/api/leave/admin/employees', methods=['POST'])
def admin_add_employee():
    if not is_admin(): return _auth_error()
    b = request.get_json(silent=True) or {}
    name = (b.get('name') or '').strip()
    if not name: return jsonify({'error': 'Name required'}), 400
    pin = b.get('pin')
    if pin and not _pin_ok(str(pin)): return jsonify({'error': 'PIN must be 4-6 digits'}), 400
    mid = b.get('managerId') or None
    if mid and not query('SELECT id FROM leave_employees WHERE id = ? AND is_manager = 1', (mid,)):
        return jsonify({'error': 'Pick a valid manager'}), 400
    eid = execute('''INSERT INTO leave_employees
                     (name, pin_hash, allowance, bc_resource_id, manager_id, is_manager)
                     VALUES (?,?,?,?,?,?)''',
                  (name, generate_password_hash(str(pin)) if pin else None,
                   float(b.get('allowance', DEFAULT_ALLOWANCE)),
                   str(b.get('bcResourceId', '') or ''), mid, 1 if b.get('isManager') else 0),
                  returning_id=True)
    return jsonify({'success': True, 'id': eid})

@leave_bp.route('/api/leave/admin/employees/bulk', methods=['POST'])
def admin_bulk_add():
    """Paste a list of names (one per line); each gets a random 4-digit PIN,
    returned once so the office can hand them out."""
    if not is_admin(): return _auth_error()
    b = request.get_json(silent=True) or {}
    names = [n.strip() for n in (b.get('names') or []) if n and n.strip()]
    if not names: return jsonify({'error': 'Paste at least one name'}), 400
    if len(names) > 200: return jsonify({'error': 'Max 200 at a time'}), 400
    mid = b.get('managerId') or None
    if mid and not query('SELECT id FROM leave_employees WHERE id = ? AND is_manager = 1', (mid,)):
        return jsonify({'error': 'Pick a valid manager'}), 400
    allowance = float(b.get('allowance') or DEFAULT_ALLOWANCE)
    is_mgr = 1 if b.get('isManager') else 0
    existing = {r['name'].lower() for r in query('SELECT name FROM leave_employees')}
    created, skipped = [], []
    for n in names:
        if n.lower() in existing:
            skipped.append(n); continue
        pin = f'{secrets.randbelow(10000):04d}'
        execute('''INSERT INTO leave_employees (name, pin_hash, allowance, manager_id, is_manager)
                    VALUES (?,?,?,?,?)''', (n, generate_password_hash(pin), allowance, mid, is_mgr))
        existing.add(n.lower())
        created.append({'name': n, 'pin': pin})
    return jsonify({'created': created, 'skipped': skipped})

def _clean(x):
    return ' '.join(str(x or '').split())

@leave_bp.route('/api/leave/admin/employees/import', methods=['POST'])
def admin_import():
    """Import a staff sheet: rows of {name, manager, allowance?, bcResourceId?}.
    Anyone named in the manager column becomes a manager. New people get a
    random PIN (returned once); people who already exist just get their
    manager updated."""
    if not is_admin(): return _auth_error()
    rows, seen = [], set()
    for r in (request.get_json(silent=True) or {}).get('rows') or []:
        name = _clean(r.get('name'))
        if not name or name.lower() in seen: continue
        seen.add(name.lower())
        rows.append({'name': name, 'manager': _clean(r.get('manager')),
                     'allowance': _clean(r.get('allowance')), 'bc': _clean(r.get('bcResourceId'))})
    if not rows: return jsonify({'error': 'No rows found'}), 400
    if len(rows) > 300: return jsonify({'error': 'Max 300 rows at a time'}), 400

    problems, created, updated = [], [], []
    existing = {e['name'].lower(): e for e in query('SELECT id, name FROM leave_employees')}
    for r in rows:
        if r['name'].lower() in existing:
            updated.append(r['name']); continue
        try: allowance = float(r['allowance']) if r['allowance'] else DEFAULT_ALLOWANCE
        except ValueError:
            allowance = DEFAULT_ALLOWANCE; problems.append(f"{r['name']}: bad allowance '{r['allowance']}', used default")
        pin = f'{secrets.randbelow(10000):04d}'
        execute('''INSERT INTO leave_employees (name, pin_hash, allowance, bc_resource_id)
                    VALUES (?,?,?,?)''', (r['name'], generate_password_hash(pin), allowance, r['bc']))
        created.append({'name': r['name'], 'pin': pin, 'manager': r['manager']})
    ids = {e['name'].lower(): e['id'] for e in query('SELECT id, name FROM leave_employees')}
    for r in rows:
        if not r['manager']: continue
        mid = ids.get(r['manager'].lower())
        if not mid:
            problems.append(f"{r['name']}: manager '{r['manager']}' is not in the staff list - left with the office"); continue
        if mid == ids[r['name'].lower()]:
            problems.append(f"{r['name']}: cannot be their own manager"); continue
        execute('UPDATE leave_employees SET is_manager = 1 WHERE id = ?', (mid,))
        execute('UPDATE leave_employees SET manager_id = ? WHERE id = ?', (mid, ids[r['name'].lower()]))
    return jsonify({'created': created, 'updated': updated, 'problems': problems})

@leave_bp.route('/api/leave/admin/employees/<int:eid>', methods=['PUT'])
def admin_update_employee(eid):
    if not is_admin(): return _auth_error()
    b = request.get_json(silent=True) or {}
    if not query('SELECT id FROM leave_employees WHERE id = ?', (eid,)):
        return jsonify({'error': 'Not found'}), 404
    mid = b.get('managerId')
    if mid not in (None, ''):
        if int(mid) == eid:
            return jsonify({'error': 'Someone cannot be their own manager'}), 400
        if not query('SELECT id FROM leave_employees WHERE id = ? AND is_manager = 1', (int(mid),)):
            return jsonify({'error': 'Pick a valid manager (mark them as a manager first)'}), 400
    sets, params = [], []
    for field, col, cast in (('name', 'name', lambda x: str(x).strip()),
                             ('allowance', 'allowance', float),
                             ('managerId', 'manager_id', lambda x: int(x) if x not in (None, '') else None),
                             ('isManager', 'is_manager', lambda x: 1 if x else 0),
                             ('bcResourceId', 'bc_resource_id', str),
                             ('active', 'active', lambda x: 1 if x else 0)):
        if field in b:
            sets.append(f'{col} = ?'); params.append(cast(b[field]))
    if 'pin' in b:
        if not _pin_ok(str(b['pin'])): return jsonify({'error': 'PIN must be 4-6 digits'}), 400
        sets.append('pin_hash = ?'); params.append(generate_password_hash(str(b['pin'])))
    if not sets: return jsonify({'error': 'Nothing to update'}), 400
    execute(f"UPDATE leave_employees SET {', '.join(sets)} WHERE id = ?", (*params, eid))
    return jsonify({'success': True})

@leave_bp.route('/api/leave/admin/employees/<int:eid>', methods=['DELETE'])
def admin_delete_employee(eid):
    """Permanently remove one person and their leave history. People they
    manage fall back to the office."""
    if not is_admin(): return _auth_error()
    if not query('SELECT id FROM leave_employees WHERE id = ?', (eid,)):
        return jsonify({'error': 'Not found'}), 404
    execute('DELETE FROM leave_requests WHERE employee_id = ?', (eid,))
    execute('UPDATE leave_employees SET manager_id = NULL WHERE manager_id = ?', (eid,))
    execute('DELETE FROM leave_employees WHERE id = ?', (eid,))
    return jsonify({'success': True})

@leave_bp.route('/api/leave/admin/employees', methods=['DELETE'])
def admin_delete_all_employees():
    """Wipe every employee and every leave request (bank holidays are kept).
    Requires {"confirm": "DELETE ALL"} so it cannot happen by accident."""
    if not is_admin(): return _auth_error()
    if (request.get_json(silent=True) or {}).get('confirm') != 'DELETE ALL':
        return jsonify({'error': 'Confirmation missing'}), 400
    n = len(query('SELECT id FROM leave_employees'))
    execute('DELETE FROM leave_requests')
    execute('DELETE FROM leave_employees')
    return jsonify({'success': True, 'deleted': n})

@leave_bp.route('/api/leave/admin/bank-holidays', methods=['POST'])
def admin_add_bh():
    if not is_admin(): return _auth_error()
    b = request.get_json(silent=True) or {}
    try: d = parse_date(b.get('date', ''))
    except ValueError: return jsonify({'error': 'date must be YYYY-MM-DD'}), 400
    name = (b.get('name') or 'Bank holiday').strip()
    execute('DELETE FROM leave_bank_holidays WHERE date = ?', (iso(d),))
    execute('INSERT INTO leave_bank_holidays (date, name) VALUES (?, ?)', (iso(d), name))
    return jsonify({'success': True})

@leave_bp.route('/api/leave/admin/bank-holidays/<d>', methods=['DELETE'])
def admin_del_bh(d):
    if not is_admin(): return _auth_error()
    execute('DELETE FROM leave_bank_holidays WHERE date = ?', (d,))
    return jsonify({'success': True})

@leave_bp.route('/api/leave/admin/bank-holidays')
def admin_list_bh():
    if not is_admin(): return _auth_error()
    return jsonify({'bankHolidays': query('SELECT date, name FROM leave_bank_holidays ORDER BY date')})
