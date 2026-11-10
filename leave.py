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
  LEAVE_DEFAULT_ALLOWANCE default days for new employees (default 28, incl. bank holidays)
  LEAVE_YEAR_START_MONTH  1-12, month the leave year starts (default 1)
"""
import re
import json
import os
import hmac
import secrets
import time
import sqlite3
import threading
import requests
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, g, has_request_context, jsonify, request, send_from_directory
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash, check_password_hash

leave_bp = Blueprint('leave', __name__)

DATABASE_URL     = os.environ.get('DATABASE_URL', '')
SQLITE_PATH      = os.environ.get('LEAVE_SQLITE_PATH', 'leave.db')
ADMIN_PASSWORD   = os.environ.get('LEAVE_ADMIN_PASSWORD', '')
SECRET           = (os.environ.get('LEAVE_SECRET')
                    or os.environ.get('BIGCHANGE_CLIENT_SECRET') or 'dev-secret-change-me')
DEFAULT_ALLOWANCE = float(os.environ.get('LEAVE_DEFAULT_ALLOWANCE', '28'))   # bank holidays are booked and deducted
YEAR_START_MONTH = min(12, max(1, int(os.environ.get('LEAVE_YEAR_START_MONTH', '1'))))

# BigChange absence sync. Set BIGCHANGE_ABSENCE_TYPE_ID (annual leave) to switch it on;
# BIGCHANGE_ABSENCE_TYPE_ID_OTHER is used for unpaid/other leave (skipped if unset).
BC_TYPE_ANNUAL = os.environ.get('BIGCHANGE_ABSENCE_TYPE_ID', '').strip()
BC_TYPE_OTHER  = os.environ.get('BIGCHANGE_ABSENCE_TYPE_ID_OTHER', '').strip()
BC_TYPE_SICK_UNPAID = os.environ.get('BIGCHANGE_ABSENCE_TYPE_ID_SICK_UNPAID', '').strip()   # optional; unpaid sick is not pushed without it
BC_TYPE_SICK   = os.environ.get('BIGCHANGE_ABSENCE_TYPE_ID_SICK', '').strip()   # sick days logged by managers
DEFAULT_SICK = float(os.environ.get('LEAVE_SICK_ALLOWANCE', '7'))
bc_hooks = {'post': None, 'delete': None, 'get': None}   # filled in by app.py
UK = ZoneInfo('Europe/London')

# Email (via Resend's HTTPS API - Render's free tier blocks normal SMTP).
# Switched on by RESEND_API_KEY + LEAVE_EMAIL_FROM, e.g. "R&S Leave <leave@randsservice.co.uk>".
RESEND_API_KEY   = os.environ.get('RESEND_API_KEY', '').strip()
EMAIL_FROM       = os.environ.get('LEAVE_EMAIL_FROM', '').strip()
EMAIL_REPLY_TO   = os.environ.get('LEAVE_EMAIL_REPLY_TO', '').strip()
OFFICE_EMAIL     = os.environ.get('LEAVE_OFFICE_EMAIL', '').strip()  # gets requests from people with no manager
SYNC_EMAIL       = False   # tests set True; normally emails go out on a background thread

LEAVE_TYPES = ['annual', 'unpaid', 'other', 'sick', 'sickunpaid']
SICK_TYPES = ('sick', 'sickunpaid')
IS_PG = bool(DATABASE_URL)

def _storage_persistent():
    """True when data survives a redeploy: Postgres, or a SQLite file on a mounted disk."""
    if IS_PG or os.environ.get('LEAVE_PERSISTENT') == '1':
        return True
    try:
        d = os.path.dirname(os.path.abspath(SQLITE_PATH))
        return os.path.ismount(d) or d.startswith('/var/data')
    except Exception:
        return False
_serializer = URLSafeTimedSerializer(SECRET, salt='leave-auth')

# England & Wales bank holidays. Editable in the admin tab; seeded once.
_SEED_BANK_HOLIDAYS = [
    ('2026-01-01', "New Year's Day"), ('2026-04-03', 'Good Friday'), ('2026-04-06', 'Easter Monday'),
    ('2026-05-04', 'Early May bank holiday'), ('2026-05-25', 'Spring bank holiday'),
    ('2026-08-31', 'Summer bank holiday'),
    ('2026-12-25', 'Christmas Day'), ('2026-12-28', 'Boxing Day (substitute)'),
    ('2027-01-01', "New Year's Day"), ('2027-03-26', 'Good Friday'),
    ('2027-03-29', 'Easter Monday'), ('2027-05-03', 'Early May bank holiday'),
    ('2027-05-31', 'Spring bank holiday'), ('2027-08-30', 'Summer bank holiday'),
    ('2027-12-27', 'Christmas Day (substitute)'), ('2027-12-28', 'Boxing Day (substitute)'),
]

# ── DB ────────────────────────────────────────────────────────────────────
_pool = None
_pool_lock = __import__('threading').Lock()

def _connect():
    if IS_PG:
        # Keep connections open between requests: a fresh TLS connection to a hosted
        # database costs about a second, which made every page feel slow.
        global _pool
        import psycopg2, psycopg2.extras, psycopg2.pool
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 8, DATABASE_URL, keepalives=1,
                            keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
            conn = _pool.getconn()
        if conn.closed:
            with _pool_lock: _pool.putconn(conn, close=True)
            return _connect()
        try:                                    # drop a connection the server has closed
            conn.rollback(); cur = conn.cursor(); cur.execute('SELECT 1'); conn.rollback()
        except Exception:
            with _pool_lock: _pool.putconn(conn, close=True)
            conn = psycopg2.connect(DATABASE_URL)   # one-off; closed on release
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        return conn
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _sql(sql):
    return sql.replace('?', '%s') if IS_PG else sql

def _conn():
    """One database connection per web request (opening a fresh one for every query is
    very slow against a hosted database). Outside a request, a one-off connection."""
    if has_request_context():
        c = g.get('_leave_conn')
        if c is not None and not (IS_PG and c.closed):
            return c, True
        c = _connect()
        g._leave_conn = c
        return c, True
    return _connect(), False

@leave_bp.teardown_request
def _close_conn(exc=None):
    c = g.pop('_leave_conn', None)
    if c is not None:
        _release(c)

def _release(conn):
    """Hand a connection back to the pool (or close it if it is not from the pool)."""
    if IS_PG and _pool is not None:
        try:
            if not conn.closed: conn.rollback()
            with _pool_lock: _pool.putconn(conn, close=bool(conn.closed))
            return
        except Exception:
            pass
    try: conn.close()
    except Exception: pass

def _finish(conn, shared):
    if not shared:
        _release(conn)

def query(sql, params=()):
    conn, shared = _conn()
    try:
        cur = conn.cursor()
        cur.execute(_sql(sql), params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        if IS_PG and shared:
            try: conn.rollback()      # end the read-only transaction
            except Exception: pass
        _finish(conn, shared)

def execute(sql, params=(), returning_id=False):
    conn, shared = _conn()
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
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        _finish(conn, shared)

def execute_many(sql, rows):
    if not rows: return
    conn, shared = _conn()
    try:
        cur = conn.cursor()
        for r in rows:
            cur.execute(_sql(sql), r)
        conn.commit()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        _finish(conn, shared)

_inited = False
_init_lock = __import__('threading').Lock()
def init_db():
    """Create tables once. Locked, and retried, because on a brand-new Postgres several
    requests arriving together can collide while creating the same table."""
    global _inited
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        for attempt in range(4):
            try:
                _init_db_once()
                break
            except Exception:
                if attempt == 3: raise
                time.sleep(0.7)
        _inited = True

def _init_db_once():
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
    for tbl, col, ddl in (('leave_employees', 'manager_id', 'INTEGER'),
                          ('leave_employees', 'is_manager', 'INTEGER NOT NULL DEFAULT 0'),
                          ('leave_employees', 'email', 'TEXT'),
                          ('leave_requests', 'bc_absence_id', 'TEXT'),
                          ('leave_requests', 'bc_sync', 'TEXT')):
        try:
            execute(f'ALTER TABLE {tbl} ADD COLUMN {col} {ddl}')
        except Exception:
            pass  # column already exists
    execute('CREATE TABLE IF NOT EXISTS leave_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL)')
    execute('CREATE TABLE IF NOT EXISTS leave_bank_holidays (date TEXT PRIMARY KEY, name TEXT NOT NULL)')
    if not query('SELECT date FROM leave_bank_holidays LIMIT 1'):
        for d, n in _SEED_BANK_HOLIDAYS:
            try: execute('INSERT INTO leave_bank_holidays (date, name) VALUES (?, ?)', (d, n))
            except Exception: pass          # another worker seeded it first

@leave_bp.errorhandler(Exception)
def _leave_error(ex):
    from werkzeug.exceptions import HTTPException
    if isinstance(ex, HTTPException):
        return jsonify({'error': f'{ex.code} {ex.name}'}), ex.code
    import traceback; traceback.print_exc()
    return jsonify({'error': f'Server error: {type(ex).__name__}: {str(ex).strip().splitlines()[0][:200] if str(ex).strip() else ""}'}), 500

@leave_bp.before_request
def _ensure_db():
    if request.endpoint == 'leave.leave_page':
        return                      # the page itself needs no database
    try:
        init_db()
    except Exception as ex:
        print(f'[LEAVE] database error: {type(ex).__name__}: {ex}')
        msg = str(ex).strip().splitlines()[0][:200] if str(ex).strip() else type(ex).__name__
        return jsonify({'error': f'Database connection problem: {msg}'}), 503

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

def _balance(emp, rows, bhs, lo, hi, sick_allow):
    taken = pending = sick = 0.0
    rec = {'sickunpaid': 0.0, 'unpaid': 0.0, 'other': 0.0}     # recorded only, never deducted
    for r in rows:
        if r['type'] in ('sick', 'sickunpaid', 'unpaid', 'other'):
            if r['status'] == 'approved':
                d = working_days(parse_date(r['start_date']), parse_date(r['end_date']),
                                 r['half_day'], bhs, lo, hi)
                if r['type'] == 'sick': sick += d      # only paid sick counts towards the sick allowance
                else: rec[r['type']] += d
            continue
        if r['status'] not in ('approved', 'pending'):
            continue
        if r['type'] == 'bankholiday':       # always exactly one day, counted in its own leave year
            d = 1.0 if iso(lo) <= r['start_date'] <= iso(hi) else 0.0
        elif r['type'] == 'annual':
            d = working_days(parse_date(r['start_date']), parse_date(r['end_date']),
                             r['half_day'], bhs, lo, hi)
        else:
            continue
        if r['status'] == 'approved': taken += d
        else: pending += d
    return {'yearStart': iso(lo), 'yearEnd': iso(hi), 'allowance': emp['allowance'],
            'taken': taken, 'pending': pending,
            'remaining': emp['allowance'] - taken - pending,
            'sickAllowance': sick_allow, 'sickTaken': sick, 'sickRemaining': sick_allow - sick,
            'sickUnpaidTaken': rec['sickunpaid'], 'unpaidTaken': rec['unpaid'], 'appointmentTaken': rec['other']}

def balance_for(emp, on=None):
    lo, hi = year_bounds(on or date.today())
    rows = query('''SELECT start_date, end_date, half_day, status, type FROM leave_requests
                    WHERE employee_id = ? AND type IN ('annual', 'bankholiday', 'sick', 'sickunpaid', 'unpaid', 'other')''', (emp['id'],))
    return _balance(emp, rows, bank_holiday_set(), lo, hi, sick_allowance())

def balances_for(emps, on=None):
    """Balances for many people using three queries in total (not three each)."""
    lo, hi = year_bounds(on or date.today())
    bhs, sa = bank_holiday_set(), sick_allowance()
    by = {}
    for r in query('''SELECT employee_id, start_date, end_date, half_day, status, type FROM leave_requests
                      WHERE type IN ('annual', 'bankholiday', 'sick', 'sickunpaid', 'unpaid', 'other')'''):
        by.setdefault(r['employee_id'], []).append(r)
    return {e['id']: _balance(e, by.get(e['id'], []), bhs, lo, hi, sa) for e in emps}

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
LEAVE_LABELS = {'annual': 'Annual leave', 'sick': 'Sick (paid)', 'sickunpaid': 'Sick (unpaid)', 'unpaid': 'Unpaid leave', 'other': 'Appointment / other'}

def off_on(start, end, exclude_id=None, statuses=('approved',), info=None):
    """Who else is off in this range: {date: [names]}."""
    ph = ','.join('?' * len(statuses))
    rows = query(f'''SELECT r.id, r.type, r.start_date, r.end_date, e.name FROM leave_requests r
                     JOIN leave_employees e ON e.id = r.employee_id
                     WHERE r.status IN ({ph}) AND r.type != 'bankholiday'
                     AND r.start_date <= ? AND r.end_date >= ?''',
                 (*statuses, iso(end), iso(start)))
    out = {}
    for r in rows:
        if exclude_id and r['id'] == exclude_id: continue
        d = max(parse_date(r['start_date']), start)
        e = min(parse_date(r['end_date']), end)
        while d <= e:
            if d.weekday() < 5:
                out.setdefault(iso(d), []).append(r['name'])
                if info is not None: info.setdefault(iso(d), {})[r['name']] = {'k': r['type'], 't': LEAVE_LABELS.get(r['type'], 'Leave')}
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
    rows = query('''SELECT id FROM leave_requests WHERE employee_id = ? AND type != 'bankholiday'
                    AND status IN ('approved','pending')
                    AND start_date <= ? AND end_date >= ?''', (emp_id, iso(end), iso(start)))
    return any(r['id'] != exclude_id for r in rows)

def fmt_request(r):
    return {k: r[k] for k in ('id', 'start_date', 'end_date', 'half_day', 'days', 'type',
                               'note', 'status', 'admin_note', 'created_at', 'decided_at')} | {
        'employeeId': r['employee_id'], 'employeeName': r.get('name'),
        'managerName': r.get('manager_name'),
        'bcSync': r.get('bc_sync')}

def email_enabled():
    return bool(RESEND_API_KEY and EMAIL_FROM)

def send_email(to, subject, text):
    """Send one email. Raises on failure (callers on the background path swallow it)."""
    body = {'from': EMAIL_FROM, 'to': [to], 'subject': subject, 'text': text}
    if EMAIL_REPLY_TO: body['reply_to'] = EMAIL_REPLY_TO
    r = requests.post('https://api.resend.com/emails', json=body, timeout=15,
                      headers={'Authorization': f'Bearer {RESEND_API_KEY}'})
    print(f'[EMAIL] to={to} {r.status_code}: {r.text[:200]}')
    if not r.ok:
        raise RuntimeError(f'{r.status_code}: {r.text[:200]}')

def _dispatch(messages):
    """messages: list of (to, subject, text). Never blocks or breaks the request."""
    messages = [m for m in messages if m[0] and '@' in m[0]]
    if not messages or not email_enabled(): return
    def run():
        for to, subj, text in messages:
            try: send_email(to, subj, text)
            except Exception as ex: print(f'[EMAIL] failed to {to}: {ex}')
    if SYNC_EMAIL: run()
    else: threading.Thread(target=run, daemon=True).start()

def _range_text(r):
    """e.g. 'Mon 14 Dec 2026 to Fri 18 Dec 2026 (back at work Mon 21 Dec 2026) - 5 working days'"""
    bhs = bank_holiday_set()
    s, e = parse_date(r['start_date']), parse_date(r['end_date'])
    f = lambda d: d.strftime('%a %d %b %Y')
    off = lambda d: d.weekday() >= 5 or iso(d) in bhs
    d = r['days']
    days = f"{d:g} working day{'' if d == 1 else 's'}"
    half = {'am': ' (morning only)', 'pm': ' (afternoon only)'}.get(r.get('half_day') or '', '')
    if half:
        return f'{f(s)}{half} - {days}'
    last = e
    while last > s and off(last): last -= timedelta(days=1)
    back = e + timedelta(days=1)
    while off(back): back += timedelta(days=1)
    span = f(s) if s == last else f'{f(s)} to {f(last)}'
    return f'{span} (back at work {f(back)}) - {days}'

def notify_request(rid, event, base_url, note=''):
    """event: submitted | approved | rejected | cancelled | booked"""
    rows = query('''SELECT r.*, e.name, e.email, e.manager_id FROM leave_requests r
                    JOIN leave_employees e ON e.id = r.employee_id WHERE r.id = ?''', (rid,))
    if not rows: return
    r = rows[0]
    base = base_url.rstrip('/')
    if base.startswith('http://') and 'localhost' not in base and '127.0.0.1' not in base:
        base = 'https://' + base[len('http://'):]   # Render terminates TLS in front of the app
    if r['type'] in SICK_TYPES:
        return                              # sick days are recorded quietly, no email
    link = base + '/leave'
    when = _range_text(r)
    label = (r['type'] if r['type'] != 'annual' else 'annual leave')
    if event == 'submitted':
        mgr = query('SELECT email FROM leave_employees WHERE id = ?', (r['manager_id'],)) if r['manager_id'] else []
        to = (mgr[0]['email'] if mgr else None) or OFFICE_EMAIL
        _dispatch([(to, f"Leave request: {r['name']}",
                    f"{r['name']} has requested {label}:\n\n{when}\n"
                    + (f"Note: {r['note']}\n" if r['note'] else '')
                    + f"\nReview it here: {link}\n")])
        return
    subject, intro = {
        'approved':  ('Your holiday has been approved', f'Your {label} has been approved:'),
        'booked':    ('Holiday booked for you', f'The office has booked {label} for you:'),
        'rejected':  ('Your holiday request was not approved', f'Your {label} request was not approved:'),
        'cancelled': ('Your holiday has been cancelled', f'Your {label} has been cancelled:'),
    }[event]
    text = f"Hi {r['name'].split()[0]},\n\n{intro}\n\n{when}\n"
    if event == 'rejected' and note: text += f"\nReason: {note}\n"
    text += f"\nYou can check your leave and balance here: {link}\n\nR&S Services"
    _dispatch([(r['email'], subject, text)])

def book_bank_holidays(since, employee_ids=None):
    """Book every bank holiday on/after `since` as approved leave for all active staff
    (or just employee_ids). Idempotent: anything already booked - even if later
    cancelled by the office - is left alone. Returns the number of bookings made."""
    bhs = [b for b in query('SELECT date, name FROM leave_bank_holidays WHERE date >= ?', (iso(since),))
           if parse_date(b['date']).weekday() < 5]
    emps = query('SELECT id FROM leave_employees WHERE active = 1')
    if employee_ids is not None:
        keep = set(employee_ids); emps = [e for e in emps if e['id'] in keep]
    have = {(r['employee_id'], r['start_date'])
            for r in query("SELECT employee_id, start_date FROM leave_requests WHERE type = 'bankholiday'")}
    now = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')
    rows = [(e['id'], b['date'], b['date'], '', 1.0, 'bankholiday', b['name'][:200], 'approved',
             'Bank holiday', now, now)
            for e in emps for b in bhs if (e['id'], b['date']) not in have]
    execute_many('''INSERT INTO leave_requests (employee_id, start_date, end_date, half_day, days, type,
                    note, status, admin_note, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)''', rows)
    return len(rows)

def _uk_iso(d, hour=0):
    return datetime(d.year, d.month, d.day, hour, tzinfo=UK).isoformat()

def _set_sync(rid, absence_id, msg):
    execute('UPDATE leave_requests SET bc_absence_id = ?, bc_sync = ? WHERE id = ?', (absence_id, msg[:300], rid))

def bc_sync_create(rid):
    """Create the matching absence in BigChange for an approved request.
    Never raises - the result is stored on the request so the office can see it."""
    rows = query('''SELECT r.*, e.bc_resource_id FROM leave_requests r
                    JOIN leave_employees e ON e.id = r.employee_id WHERE r.id = ?''', (rid,))
    if not rows: return None
    r = rows[0]
    if r['type'] == 'bankholiday':
        return None                        # bank holidays are not pushed to BigChange
    type_id = {'annual': BC_TYPE_ANNUAL, 'sick': BC_TYPE_SICK, 'sickunpaid': BC_TYPE_SICK_UNPAID}.get(r['type'], BC_TYPE_OTHER)
    if r.get('bc_absence_id') or not bc_hooks['post'] or not type_id:
        return r.get('bc_sync')            # already synced, or sync switched off
    resource = _bcid(r.get('bc_resource_id'))
    if not resource.isdigit():
        _set_sync(rid, None, 'skipped: no BigChange ID set for this person')
        return 'skipped'
    start, end = parse_date(r['start_date']), parse_date(r['end_date'])
    if r['half_day'] == 'am':   s, e = _uk_iso(start, 0), _uk_iso(start, 12)
    elif r['half_day'] == 'pm': s, e = _uk_iso(start, 12), _uk_iso(start + timedelta(days=1))
    else:                       s, e = _uk_iso(start), _uk_iso(end + timedelta(days=1))
    body = {'resourceId': int(resource), 'startAt': s, 'endAt': e, 'absenceTypeId': int(type_id),
            'daysRequested': r['days'], 'reason': ((r['type'].capitalize() + ' leave') + (f" - {r['note']}" if r['note'] else ''))[:200],
            'isPaidLeave': r['type'] in ('annual', 'sick')}
    try:
        res = bc_hooks['post']('/absences', body)
        _set_sync(rid, str(res.get('id') or ''), 'synced to BigChange')
        return 'synced'
    except Exception as ex:
        _set_sync(rid, None, f'failed: {ex}')
        return 'failed'

def bc_sync_remove(rid):
    """Remove the BigChange absence when approved leave is cancelled/rejected."""
    rows = query('SELECT bc_absence_id FROM leave_requests WHERE id = ?', (rid,))
    if not rows or not rows[0].get('bc_absence_id') or not bc_hooks['delete']:
        return None
    try:
        bc_hooks['delete'](f"/absences/{rows[0]['bc_absence_id']}")
        _set_sync(rid, None, 'removed from BigChange')
        return 'removed'
    except Exception as ex:
        execute('UPDATE leave_requests SET bc_sync = ? WHERE id = ?',
                (f'failed to remove - delete it in BigChange manually ({ex})'[:300], rid))
        return 'failed'

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
                    'yearStartMonth': YEAR_START_MONTH, 'persistent': _storage_persistent()})

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
                    WHERE r.status = 'approved' AND r.type != 'bankholiday'
                    AND r.start_date <= ? AND r.end_date >= ?
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
    if v['type'] in SICK_TYPES:
        return jsonify({'error': 'Sick days are logged by your manager - tell them or call the office'}), 400
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
    notify_request(rid, 'submitted', request.host_url)
    return jsonify({'success': True, 'id': rid})

@leave_bp.route('/api/leave/requests/<int:rid>/cancel', methods=['POST'])
def cancel_own(rid):
    emp = current_employee()
    if not emp: return _auth_error()
    rows = query('SELECT * FROM leave_requests WHERE id = ? AND employee_id = ?', (rid, emp['id']))
    if not rows: return jsonify({'error': 'Not found'}), 404
    r = rows[0]
    if r['type'] == 'bankholiday':
        return jsonify({'error': 'Bank holidays cannot be cancelled'}), 400
    if r['status'] not in ('pending', 'approved'):
        return jsonify({'error': 'Already closed'}), 400
    if r['status'] == 'approved' and parse_date(r['start_date']) <= date.today():
        return jsonify({'error': 'Leave has started - call the office'}), 400
    execute("UPDATE leave_requests SET status = 'cancelled', decided_at = ? WHERE id = ?",
            (datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'), rid))
    bc_sync_remove(rid)
    return jsonify({'success': True})



def _bc_status(a):
    return str(a.get('status') or '').strip().lower()

def _bc_type(a):
    """BigChange's absence type id, whichever way the field is named."""
    for k in ('absenceTypeId', 'typeId'):
        if a.get(k) is not None: return str(a[k])
    t = a.get('absenceType') or a.get('type')
    if isinstance(t, dict) and t.get('id') is not None: return str(t['id'])
    if isinstance(t, (int, str)) and str(t): return str(t)
    return None

def _bc_type_name(a):
    t = a.get('absenceType') or a.get('type')
    return (t.get('name') if isinstance(t, dict) else None) or a.get('typeName') or a.get('absenceTypeName') or ''

# ── BigChange absences shown on the calendar (read-only, not counted against allowances) ──
_bc_cal_cache = {}

def _bc_month_absences(first, last):
    """Absences from BigChange touching this month. Cached for five minutes."""
    key = iso(first)
    hit = _bc_cal_cache.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    items, page = [], 1
    while page <= 5:
        data = bc_hooks['get']('/absences', {'dateFrom': iso(first) + 'T00:00:00Z',
                                             'dateTo': iso(last + timedelta(days=1)) + 'T00:00:00Z',
                                             'pageSize': 500, 'pageNumber': page})
        got = data if isinstance(data, list) else (data.get('items') or [])
        items.extend(got)
        if len(got) < 500: break
        page += 1
    _bc_cal_cache[key] = (time.time(), items)
    return items

BANKHOL_TYPES = {t.strip() for t in os.environ.get('LEAVE_BC_BANKHOL_TYPES', '3701,30157').split(',') if t.strip()}
_bc_names_cache = {'t': 0, 'names': {}}

def _bc_resource_names():
    """{resourceId: name} from BigChange, cached for an hour. Empty if it cannot be read."""
    if time.time() - _bc_names_cache['t'] < 3600:
        return _bc_names_cache['names']
    names = {}
    try:
        for r in _bc_resources():
            rid = r.get('id') if r.get('id') is not None else r.get('resourceId')
            if rid is not None and _bc_res_name(r):
                names[_bcid(rid)] = _bc_res_name(r)
    except Exception as ex:
        print(f'[LEAVE] could not read BigChange resource names: {ex}')
    if names:                                   # never cache a failure or an empty list
        _bc_names_cache.update(t=time.time(), names=names)
    return names

def bc_calendar_overlay(first, last, include_unmatched=False, info=None):
    """({date: [names]} approved, {date: [names]} requested, other_people_count, error).
    include_unmatched also shows people with no BigChange ID here, under their BigChange name."""
    if not bc_hooks.get('get'):
        return {}, {}, 0, None
    try:
        items = _bc_month_absences(first, last)
    except Exception as ex:
        print(f'[LEAVE] BigChange calendar overlay failed: {ex}')
        return {}, {}, 0, 'BigChange could not be reached'
    emps = {_bcid(e['bc_resource_id']): e['name'] for e in query('SELECT name, bc_resource_id FROM leave_employees WHERE active = 1')
            if _bcid(e.get('bc_resource_id'))}
    have = {str(r['bc_absence_id']) for r in query('SELECT bc_absence_id FROM leave_requests WHERE bc_absence_id IS NOT NULL')}
    bhs = bank_holiday_set()
    approved, requested, unmatched = {}, {}, 0
    for a in items:
        status = _bc_status(a)
        if status not in ('approved', 'requested') or str(a.get('id')) in have:
            continue
        if _bc_type(a) in BANKHOL_TYPES or 'bank holiday' in _bc_type_name(a).lower():
            continue                                      # bank holidays are shown as shaded days instead
        name = emps.get(_bcid(a.get('resourceId')))
        if not name:
            unmatched += 1
            if not include_unmatched: continue
            rid = _bcid(a.get('resourceId'))
            name = _bc_resource_names().get(rid) or f'BigChange #{rid}'
        try:
            d = max(_uk_date(a['startAt']).date(), first)
            e = min(max(_uk_date(a['startAt']).date(), _uk_date(a['endAt'], True).date()), last)
        except Exception:
            continue
        target = approved if status == 'approved' else requested
        while d <= e:
            if d.weekday() < 5 and iso(d) not in bhs:
                lst = target.setdefault(iso(d), [])
                if name not in lst: lst.append(name)
                if info is not None:
                    tn = _bc_type_name(a) or ''
                    kind = IMPORT_TYPE_MAP.get(str(_bc_type(a)), 'other')
                    if kind == 'skip': kind = 'other'
                    why = str(a.get('reason') or '').strip()
                    info.setdefault(iso(d), {}).setdefault(name, {'k': kind, 't': (tn or LEAVE_LABELS.get(kind, 'Leave')) + (f' – {why}' if why else '') + ' (BigChange)'})
            d += timedelta(days=1)
    return approved, requested, unmatched, None

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
    info = {}
    approved = off_on(first, last, info=info)
    pending = off_on(first, last, statuses=('pending',), info=info) if is_admin() else {}
    bc_ok, bc_req, bc_unmatched, bc_err = bc_calendar_overlay(first, last, include_unmatched=False, info=info)
    for src, dst in ((bc_ok, approved), (bc_req if is_admin() else {}, pending)):
        for d, names in src.items():
            lst = dst.setdefault(d, [])
            lst.extend(n for n in names if n not in lst)
    return jsonify({'month': first.strftime('%Y-%m'), 'approved': approved, 'pending': pending, 'info': info,
                    'bcShown': bool(bc_hooks.get('get')) and not bc_err, 'bcError': bc_err,
                    'bcUnmatched': bc_unmatched if is_admin() else 0, 'bcUnmatchedShown': False,
                    'bankHolidays': [d for d in sorted(bank_holiday_set()) if iso(first) <= d <= iso(last)]})

# ── Admin endpoints ───────────────────────────────────────────────────────
def _request_list(status=None, manager_id=None):
    sql = '''SELECT r.*, e.name, m.name AS manager_name FROM leave_requests r
           JOIN leave_employees e ON e.id = r.employee_id
           LEFT JOIN leave_employees m ON m.id = e.manager_id'''
    where, params = ["r.type != 'bankholiday'"], []
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
    sync = bc_sync_create(rid) if new_status == 'approved' else bc_sync_remove(rid)
    notify_request(rid, {'approved': 'approved', 'rejected': 'rejected', 'cancelled': 'cancelled'}[new_status],
                   request.host_url, (body.get('note') or '')[:300])
    return jsonify({'success': True, 'bcSync': sync})

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

@leave_bp.route('/api/leave/team/sick', methods=['POST'])
def team_log_sick():
    """A manager logs sick days for someone on their team. Auto-approved, past dates fine,
    counted against the separate sick allowance (not annual leave)."""
    mgr = current_manager()
    if not mgr: return _auth_error()
    body = request.get_json(silent=True) or {}
    rows = query('SELECT * FROM leave_employees WHERE id = ? AND manager_id = ? AND active = 1',
                 (body.get('employeeId'), mgr['id']))
    if not rows: return jsonify({'error': 'Pick someone on your team'}), 400
    body['type'] = 'sickunpaid' if str(body.get('paid', True)).lower() in ('false', '0', 'no') else 'sick'
    v, err = validate_range(body)
    if err: return jsonify({'error': err}), 400
    if overlaps_own(rows[0]['id'], v['start'], v['end']):
        return jsonify({'error': 'They already have leave booked or pending on those dates'}), 409
    rid = create_request(rows[0], v, body.get('note'), status='approved',
                         admin_note=f"Sick - logged by {mgr['name']}")
    sync = bc_sync_create(rid)
    return jsonify({'success': True, 'id': rid, 'bcSync': sync,
                    'balance': balance_for(rows[0], v['start'])})

@leave_bp.route('/api/leave/team/members')
def team_members():
    mgr = current_manager()
    if not mgr: return _auth_error()
    team = query('SELECT * FROM leave_employees WHERE manager_id = ? AND active = 1 ORDER BY name', (mgr['id'],))
    bals = balances_for(team)
    return jsonify({'members': [{'id': e['id'], 'name': e['name'], 'balance': bals[e['id']]} for e in team]})

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
    sync = bc_sync_create(rid)
    notify_request(rid, 'booked', request.host_url)
    return jsonify({'success': True, 'id': rid, 'bcSync': sync})

@leave_bp.route('/api/leave/admin/requests/<int:rid>/bc-sync', methods=['POST'])
def admin_retry_sync(rid):
    if not is_admin(): return _auth_error()
    rows = query('SELECT status FROM leave_requests WHERE id = ?', (rid,))
    if not rows: return jsonify({'error': 'Not found'}), 404
    if rows[0]['status'] != 'approved': return jsonify({'error': 'Only approved leave is synced'}), 400
    execute('UPDATE leave_requests SET bc_absence_id = NULL WHERE id = ?', (rid,))
    return jsonify({'success': True, 'bcSync': bc_sync_create(rid)})

@leave_bp.route('/api/leave/admin/employees')
def admin_employees():
    if not is_admin(): return _auth_error()
    out = []
    emps = query('SELECT * FROM leave_employees ORDER BY active DESC, name')
    bals = balances_for(emps)
    for e in emps:
        out.append({'id': e['id'], 'name': e['name'], 'allowance': e['allowance'],
                    'managerId': e.get('manager_id'), 'isManager': bool(e.get('is_manager')),
                    'bcResourceId': e['bc_resource_id'], 'email': e.get('email') or '',
                    'active': bool(e['active']), 'hasPin': bool(e['pin_hash']),
                    'balance': bals[e['id']]})
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
                     (name, pin_hash, allowance, bc_resource_id, manager_id, is_manager, email)
                     VALUES (?,?,?,?,?,?,?)''',
                  (name, generate_password_hash(str(pin)) if pin else None,
                   float(b.get('allowance') if b.get('allowance') not in (None, '') else default_allowance()),
                   _bcid(b.get('bcResourceId')), mid, 1 if b.get('isManager') else 0,
                   _clean(b.get('email')).lower()),
                  returning_id=True)
    book_bank_holidays(date.today(), [eid])
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
    allowance = float(b.get('allowance') or default_allowance())
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
    if created: book_bank_holidays(date.today())
    return jsonify({'created': created, 'skipped': skipped})

def get_setting(key, default=None):
    rows = query('SELECT setting_value FROM leave_settings WHERE setting_key = ?', (key,))
    return rows[0]['setting_value'] if rows else default

def set_setting(key, value):
    execute('DELETE FROM leave_settings WHERE setting_key = ?', (key,))
    execute('INSERT INTO leave_settings (setting_key, setting_value) VALUES (?, ?)', (key, str(value)))

def sick_allowance():
    try: return float(get_setting('sick_allowance', DEFAULT_SICK))
    except (TypeError, ValueError): return DEFAULT_SICK

def default_allowance():
    """Yearly allowance given to new staff: the admin-set value, else LEAVE_DEFAULT_ALLOWANCE."""
    try: return float(get_setting('default_allowance'))
    except (TypeError, ValueError): return DEFAULT_ALLOWANCE

def _bcid(x):
    """Normalise a BigChange resource ID: '88935', ' 88935 ', '88935.0', 'BC 88935' all become '88935'."""
    t = str(x if x is not None else '').strip()
    t = re.sub(r'\.0+$', '', t)
    return re.sub(r'\D', '', t)

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
                     'allowance': _clean(r.get('allowance')), 'bc': _bcid(r.get('bcResourceId')),
                     'email': _clean(r.get('email')).lower()})
    if not rows: return jsonify({'error': 'No rows found'}), 400
    if len(rows) > 300: return jsonify({'error': 'Max 300 rows at a time'}), 400

    problems, created, updated = [], [], []
    existing = {e['name'].lower(): e for e in query('SELECT id, name FROM leave_employees')}
    for r in rows:
        if r['name'].lower() in existing:
            updated.append(r['name']); continue
        try: allowance = float(r['allowance']) if r['allowance'] else default_allowance()
        except ValueError:
            allowance = default_allowance(); problems.append(f"{r['name']}: bad allowance '{r['allowance']}', used default")
        pin = f'{secrets.randbelow(10000):04d}'
        execute('''INSERT INTO leave_employees (name, pin_hash, allowance, bc_resource_id, email)
                    VALUES (?,?,?,?,?)''', (r['name'], generate_password_hash(pin), allowance, r['bc'], r['email']))
        created.append({'name': r['name'], 'pin': pin, 'manager': r['manager']})
    ids = {e['name'].lower(): e['id'] for e in query('SELECT id, name FROM leave_employees')}
    for r in rows:
        if r['email'] and '@' not in r['email']:
            problems.append(f"{r['name']}: '{r['email']}' does not look like an email address")
            execute('UPDATE leave_employees SET email = NULL WHERE id = ?', (ids[r['name'].lower()],))
        elif r['name'].lower() in existing:      # re-import fills in email / BigChange ID for existing people
            if r['email']: execute('UPDATE leave_employees SET email = ? WHERE id = ?', (r['email'], ids[r['name'].lower()]))
            if r['bc']: execute('UPDATE leave_employees SET bc_resource_id = ? WHERE id = ?', (r['bc'], ids[r['name'].lower()]))
    for r in rows:
        if not r['manager']: continue
        mid = ids.get(r['manager'].lower())
        if not mid:
            problems.append(f"{r['name']}: manager '{r['manager']}' is not in the staff list - left with the office"); continue
        if mid == ids[r['name'].lower()]:
            problems.append(f"{r['name']}: cannot be their own manager"); continue
        execute('UPDATE leave_employees SET is_manager = 1 WHERE id = ?', (mid,))
        execute('UPDATE leave_employees SET manager_id = ? WHERE id = ?', (mid, ids[r['name'].lower()]))
    if created: book_bank_holidays(date.today())
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
                             ('email', 'email', lambda x: _clean(x).lower()),
                             ('managerId', 'manager_id', lambda x: int(x) if x not in (None, '') else None),
                             ('isManager', 'is_manager', lambda x: 1 if x else 0),
                             ('bcResourceId', 'bc_resource_id', _bcid),
                             ('active', 'active', lambda x: 1 if x else 0)):
        if field in b:
            sets.append(f'{col} = ?'); params.append(cast(b[field]))
    if 'pin' in b:
        if not _pin_ok(str(b['pin'])): return jsonify({'error': 'PIN must be 4-6 digits'}), 400
        sets.append('pin_hash = ?'); params.append(generate_password_hash(str(b['pin'])))
    if not sets: return jsonify({'error': 'Nothing to update'}), 400
    execute(f"UPDATE leave_employees SET {', '.join(sets)} WHERE id = ?", (*params, eid))
    return jsonify({'success': True})

@leave_bp.route('/api/leave/admin/employees/<int:eid>/requests')
def admin_employee_requests(eid):
    """Every request for one person in the current leave year, with the days each one counts for."""
    if not is_admin(): return _auth_error()
    lo, hi = year_bounds(date.today())
    rows = query('''SELECT id, type, status, start_date, end_date, half_day, days, bc_absence_id, note FROM leave_requests
                    WHERE employee_id = ? AND end_date >= ? AND start_date <= ? ORDER BY start_date''', (eid, iso(lo), iso(hi)))
    bhs = bank_holiday_set()
    out = []
    for r in rows:
        counted = 1.0 if r['type'] == 'bankholiday' else working_days(parse_date(r['start_date']), parse_date(r['end_date']),
                                                                      r['half_day'], bhs, lo, hi)
        out.append({'id': r['id'], 'type': r['type'], 'status': r['status'], 'start': r['start_date'], 'end': r['end_date'],
                    'half': r['half_day'], 'counted': counted, 'fromBigChange': bool(r.get('bc_absence_id')),
                    'note': (r.get('note') or '')[:60]})
    return jsonify({'requests': out, 'yearStart': iso(lo), 'yearEnd': iso(hi)})

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

@leave_bp.route('/api/leave/admin/settings')
def admin_get_settings():
    if not is_admin(): return _auth_error()
    return jsonify({'defaultAllowance': default_allowance(), 'sickAllowance': sick_allowance()})

@leave_bp.route('/api/leave/admin/settings', methods=['PUT'])
def admin_put_settings():
    if not is_admin(): return _auth_error()
    b = request.get_json(silent=True) or {}
    if 'defaultAllowance' in b:
        try: a = float(b['defaultAllowance'])
        except (TypeError, ValueError): return jsonify({'error': 'Enter a number of days'}), 400
        if not 0 <= a <= 60: return jsonify({'error': 'Allowance must be between 0 and 60'}), 400
        set_setting('default_allowance', a)
    if 'sickAllowance' in b:
        try: a = float(b['sickAllowance'])
        except (TypeError, ValueError): return jsonify({'error': 'Enter a number of days'}), 400
        if not 0 <= a <= 60: return jsonify({'error': 'Sick allowance must be between 0 and 60'}), 400
        set_setting('sick_allowance', a)
    return jsonify({'success': True, 'defaultAllowance': default_allowance(), 'sickAllowance': sick_allowance()})

@leave_bp.route('/api/leave/admin/bc-absences')
def admin_bc_absences():
    """Read recent absences from BigChange so the office can see which absenceTypeId values
    exist (to set the env vars) and confirm synced leave really arrived."""
    if not is_admin(): return _auth_error()
    if not bc_hooks.get('get'): return jsonify({'error': 'BigChange is not connected'}), 400
    since = (date.today() - timedelta(days=365)).isoformat() + 'T00:00:00Z'
    try:
        data = bc_hooks['get']('/absences', {'dateFrom': since, 'pageSize': 200, 'pageNumber': 1})
    except Exception as ex:
        return jsonify({'error': f'BigChange said: {ex}'}), 502
    items = data if isinstance(data, list) else (data.get('items') or [])
    types = {}
    for a in items:
        t = _bc_type(a)
        if t is not None:
            types.setdefault(t, {'count': 0, 'example': _bc_type_name(a) or a.get('reason') or ''})['count'] += 1
    return jsonify({'count': len(items), 'types': types,
                    'fields': sorted(items[0].keys()) if items else [],
                    'recent': [{'id': a.get('id'), 'resourceId': a.get('resourceId'), 'startAt': a.get('startAt'),
                                'endAt': a.get('endAt'), 'status': _bc_status(a), 'absenceTypeId': _bc_type(a),
                                'reason': a.get('reason')} for a in items[-15:]],
                    'configured': {'annual': BC_TYPE_ANNUAL, 'other': BC_TYPE_OTHER, 'sick': BC_TYPE_SICK}})


# ── Import existing absences from BigChange ───────────────────────────────
def _uk_date(ts, minus_second=False):
    dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    if minus_second: dt -= timedelta(seconds=1)
    return dt.astimezone(UK)

# Only these BigChange absence types are imported (id -> how it counts here). Anything else is skipped.
# 3701 "bank holiday" is deliberately left out: bank holidays are already booked for everyone here.
IMPORT_TYPE_MAP = {'3700': 'annual',       # Approved Holiday
                   '37186': 'sick',        # Sick day paid
                   '37187': 'sickunpaid',  # Sick pay unpaid (recorded, not counted)
                   '37409': 'unpaid',      # Unpaid leave
                   '37331': 'other'}       # Appointment
for _pair in os.environ.get('LEAVE_IMPORT_TYPES', '').split(','):
    if '=' in _pair:
        _k, _v = _pair.split('=', 1)
        IMPORT_TYPE_MAP[_k.strip()] = _v.strip()
IMPORT_FROM = os.environ.get('LEAVE_IMPORT_FROM', '2026-01-01')   # import absences starting on or after this date

def _bc_fetch_absences():
    out, page = [], 1
    while page <= 20:
        data = bc_hooks['get']('/absences', {'dateFrom': IMPORT_FROM + 'T00:00:00Z',
                                             'pageSize': 500, 'pageNumber': page})
        items = data if isinstance(data, list) else (data.get('items') or [])
        out.extend(items)
        if len(items) < 500: break
        page += 1
    return out

def _bc_import_plan(type_map=None):
    """Work out what each BigChange absence would become. Nothing is written."""
    type_map = type_map or {}
    emps = {_bcid(e['bc_resource_id']): e for e in query('SELECT * FROM leave_employees WHERE active = 1')
            if _bcid(e.get('bc_resource_id'))}
    have = {str(r['bc_absence_id']) for r in query("SELECT bc_absence_id FROM leave_requests WHERE bc_absence_id IS NOT NULL")}
    bhs = bank_holiday_set()
    rows = []
    for a in _bc_fetch_absences():
        tid = str(_bc_type(a))
        typ = type_map.get(tid, IMPORT_TYPE_MAP.get(tid, 'skip'))
        row = {'id': str(a.get('id')), 'resourceId': str(a.get('resourceId')), 'typeId': tid,
               'status': _bc_status(a), 'type': typ, 'action': 'import', 'why': ''}
        emp = emps.get(_bcid(row['resourceId']))
        try:
            s_dt, e_dt = _uk_date(a['startAt']), _uk_date(a['endAt'], True)
            start, end = s_dt.date(), max(s_dt.date(), e_dt.date())
        except Exception:
            row.update(action='skip', why='unreadable dates'); rows.append(row); continue
        half = ''
        if start == end and (a.get('daysRequested') or 1) <= 0.5:
            half = 'am' if e_dt.hour <= 12 else 'pm'
        days = working_days(start, end, half, bhs)
        if not half and days == 1 and 0 < (a.get('daysRequested') or 1) <= 0.5:
            half = 'am' if e_dt.hour <= 12 else 'pm'           # half day whose end falls on a weekend/bank holiday
            days = 0.5
            end = start                                          # half days are stored as a single day
        row['typeName'] = _bc_type_name(a)
        row.update(name=emp['name'] if emp else None, start=iso(start), end=iso(end), half=half, days=days,
                   note=(a.get('reason') or '')[:200])
        if not emp: row.update(action='skip', why='no employee has this BigChange ID')
        elif row['id'] in have: row.update(action='skip', why='already in the leave system')
        elif _bc_status(a) != 'approved': row.update(action='skip', why=f"status is {_bc_status(a) or 'unknown'}")
        elif typ == 'skip': row.update(action='skip', why='type is not on the import list')
        elif days == 0: row.update(action='skip', why='no working days')
        elif overlaps_own(emp['id'], start, end): row.update(action='skip', why='overlaps leave already booked')
        row['_emp'] = emp['id'] if emp else None
        rows.append(row)
    return rows


# ── Match staff to BigChange resources by name ────────────────────────────
def _norm_name(n):
    n = ' '.join(str(n or '').replace(',', ' ').lower().split())
    return n

def _bc_resources():
    out, page = [], 1
    while page <= 10:
        data = bc_hooks['get']('/resources', {'pageSize': 500, 'pageNumber': page})
        got = data if isinstance(data, list) else (data.get('items') or [])
        out.extend(got)
        if len(got) < 500: break
        page += 1
    return out

def _bc_res_name(r):
    n = r.get('name') or r.get('resourceName') or r.get('fullName') or r.get('displayName')
    if not n:
        n = f"{r.get('firstName') or r.get('forename') or ''} {r.get('lastName') or r.get('surname') or ''}".strip()
    return n

@leave_bp.route('/api/leave/admin/bc-match')
def admin_bc_match():
    """Suggest BigChange resource IDs for staff who have none, or whose saved ID is not a BigChange resource, by matching names. Writes nothing."""
    if not is_admin(): return _auth_error()
    if not bc_hooks.get('get'): return jsonify({'error': 'BigChange is not connected'}), 400
    try: resources = _bc_resources()
    except Exception as ex: return jsonify({'error': f'BigChange said: {ex}'}), 502
    try:                                   # staff may not be in /resources; absences also carry the person's name
        known = {_bcid(r.get('id')) for r in resources}
        for a in _bc_fetch_absences():
            rid, o = _bcid(a.get('resourceId')), a.get('owner')
            nm = (o if isinstance(o, str) else _bc_res_name(o) if isinstance(o, dict) else '') or ''
            if rid and rid not in known and nm.strip():
                resources.append({'id': rid, 'name': nm.strip()}); known.add(rid)
    except Exception as ex:
        print(f'[LEAVE] could not read absence owners: {ex}')
    by = {}
    for r in resources:
        key = _norm_name(_bc_res_name(r))
        if key and r.get('id') is not None:
            by.setdefault(key, []).append({'id': str(r['id']), 'name': _bc_res_name(r)})
    valid = {_bcid(r.get('id')) for r in resources if r.get('id') is not None}
    proposals, missing, ambiguous, have = [], [], [], 0
    for e in query('SELECT id, name, bc_resource_id FROM leave_employees WHERE active = 1 ORDER BY name'):
        old = _bcid(e.get('bc_resource_id'))
        if old and (old in valid or not valid):
            have += 1; continue
        words = _norm_name(e['name']).split()
        keys = {' '.join(words), ' '.join(reversed(words))}
        found = {c['id']: c for k in keys for c in by.get(k, [])}
        if not found and len(words) >= 2:       # fallback: same surname and same first initial, e.g. Matt / Matthew
            ini = (words[0][:1], words[-1])
            found = {c['id']: c for k, cs in by.items() for c in cs
                     if len(k.split()) >= 2 and (k.split()[0][:1], k.split()[-1]) == ini}
        if len(found) == 1:
            c = list(found.values())[0]
            proposals.append({'employeeId': e['id'], 'name': e['name'], 'resourceId': c['id'], 'bcName': c['name'], 'old': old})
        elif found:
            ambiguous.append(e['name'])
        else:
            missing.append(e['name'] + (f' (saved ID {old} is not a BigChange resource)' if old else ''))
    return jsonify({'proposals': proposals, 'notFound': missing, 'ambiguous': ambiguous,
                    'alreadySet': have, 'bcResources': len(resources),
                    'fields': sorted({k for r in resources[:20] for k in r}),
                    'sample': [{k: (v if not isinstance(v, (dict, list)) else '…') for k, v in r.items()} for r in resources[:3]],
                    'bcNames': [_bc_res_name(r) or '(no name)' for r in resources]})

@leave_bp.route('/api/leave/admin/bc-match', methods=['POST'])
def admin_bc_match_apply():
    if not is_admin(): return _auth_error()
    n = 0
    for a in (request.get_json(silent=True) or {}).get('assign') or []:
        rid = str(a.get('resourceId') or '').strip()
        if rid.isdigit() and a.get('employeeId') is not None:
            execute('UPDATE leave_employees SET bc_resource_id = ? WHERE id = ?', (rid, a['employeeId']))
            n += 1
    return jsonify({'success': True, 'updated': n})

@leave_bp.route('/api/leave/admin/bc-import')
def admin_bc_import_preview():
    if not is_admin(): return _auth_error()
    if not bc_hooks.get('get'): return jsonify({'error': 'BigChange is not connected'}), 400
    try:
        tm = json.loads(request.args.get('types') or '{}')
        rows = _bc_import_plan({str(k): v for k, v in tm.items() if v in LEAVE_TYPES + ['skip']})
    except Exception as ex: return jsonify({'error': f'BigChange said: {ex}'}), 502
    unmatched = sum(1 for r in rows if r['why'] == 'no employee has this BigChange ID')
    rows = [r for r in rows if r['why'] != 'no employee has this BigChange ID']   # not our staff: don't list them
    types = {}
    for r in rows:
        t = types.setdefault(r['typeId'], {'count': 0, 'type': r['type'], 'example': r.get('typeName') or r.get('note', '')})
        t['count'] += 1
    return jsonify({'types': types, 'unmatched': unmatched,
                    'rows': [{k: v for k, v in r.items() if k != '_emp'} for r in rows]})

@leave_bp.route('/api/leave/admin/bc-import', methods=['POST'])
def admin_bc_import():
    if not is_admin(): return _auth_error()
    if not bc_hooks.get('get'): return jsonify({'error': 'BigChange is not connected'}), 400
    tm = {str(k): v for k, v in ((request.get_json(silent=True) or {}).get('types') or {}).items()
          if v in LEAVE_TYPES + ['skip']}
    try: rows = _bc_import_plan(tm)
    except Exception as ex: return jsonify({'error': f'BigChange said: {ex}'}), 502
    now = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')
    new, skipped = [], 0
    for r in rows:
        if r['action'] != 'import': skipped += 1; continue
        new.append((r['_emp'], r['start'], r['end'], r['half'], r['days'], r['type'], r['note'], 'approved',
                    'Imported from BigChange', now, now, r['id'], 'imported from BigChange'))
    if new:
        execute_many('''INSERT INTO leave_requests
            (employee_id, start_date, end_date, half_day, days, type, note, status, admin_note,
             created_at, decided_at, bc_absence_id, bc_sync) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', new)
    return jsonify({'success': True, 'imported': len(new), 'skipped': skipped})

@leave_bp.route('/api/leave/admin/email-status')
def admin_email_status():
    if not is_admin(): return _auth_error()
    return jsonify({'enabled': email_enabled(), 'from': EMAIL_FROM, 'office': OFFICE_EMAIL})

@leave_bp.route('/api/leave/admin/test-email', methods=['POST'])
def admin_test_email():
    if not is_admin(): return _auth_error()
    to = _clean((request.get_json(silent=True) or {}).get('to'))
    if '@' not in to: return jsonify({'error': 'Enter a valid email address'}), 400
    if not email_enabled(): return jsonify({'error': 'Email is not set up (RESEND_API_KEY / LEAVE_EMAIL_FROM missing)'}), 400
    try: send_email(to, 'R&S leave system test', 'If you can read this, leave emails are working.')
    except Exception as ex: return jsonify({'error': f'Email provider said: {ex}'}), 502
    return jsonify({'success': True})

@leave_bp.route('/api/leave/admin/bank-holidays', methods=['POST'])
def admin_add_bh():
    if not is_admin(): return _auth_error()
    b = request.get_json(silent=True) or {}
    try: d = parse_date(b.get('date', ''))
    except ValueError: return jsonify({'error': 'date must be YYYY-MM-DD'}), 400
    name = (b.get('name') or 'Bank holiday').strip()
    execute('DELETE FROM leave_bank_holidays WHERE date = ?', (iso(d),))
    execute('INSERT INTO leave_bank_holidays (date, name) VALUES (?, ?)', (iso(d), name))
    booked = book_bank_holidays(date.today())   # no-op for past dates
    return jsonify({'success': True, 'booked': booked})

@leave_bp.route('/api/leave/admin/bank-holidays/<d>', methods=['DELETE'])
def admin_del_bh(d):
    if not is_admin(): return _auth_error()
    execute('DELETE FROM leave_bank_holidays WHERE date = ?', (d,))
    execute("DELETE FROM leave_requests WHERE type = 'bankholiday' AND start_date = ?", (d,))
    return jsonify({'success': True})

@leave_bp.route('/api/leave/admin/bank-holidays/book-all', methods=['POST'])
def admin_book_all_bank_holidays():
    """Book the bank holidays in the current leave year (and later) for every active employee."""
    if not is_admin(): return _auth_error()
    for d, n in _SEED_BANK_HOLIDAYS:      # make sure the standard dates exist
        if not query('SELECT date FROM leave_bank_holidays WHERE date = ?', (d,)):
            execute('INSERT INTO leave_bank_holidays (date, name) VALUES (?, ?)', (d, n))
    lo, _ = year_bounds(date.today())
    booked = book_bank_holidays(lo)
    return jsonify({'success': True, 'booked': booked, 'since': iso(lo),
                    'staff': len(query('SELECT id FROM leave_employees WHERE active = 1'))})

@leave_bp.route('/api/leave/admin/employees/set-allowance', methods=['POST'])
def admin_set_allowance_all():
    if not is_admin(): return _auth_error()
    try: a = float((request.get_json(silent=True) or {}).get('allowance'))
    except (TypeError, ValueError): return jsonify({'error': 'Enter a number of days'}), 400
    if not 0 <= a <= 60: return jsonify({'error': 'Allowance must be between 0 and 60'}), 400
    n = len(query('SELECT id FROM leave_employees'))
    execute('UPDATE leave_employees SET allowance = ?', (a,))
    return jsonify({'success': True, 'updated': n})

@leave_bp.route('/api/leave/admin/bank-holidays')
def admin_list_bh():
    if not is_admin(): return _auth_error()
    return jsonify({'bankHolidays': query('SELECT date, name FROM leave_bank_holidays ORDER BY date')})
