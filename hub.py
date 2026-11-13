"""Staff hub sign-in and per-app access.

Everyone signs in once at /login (name + PIN, the same PINs as the leave system; the office signs in with the
office password). A signed cookie then lets them into the tools they are allowed to use. The office decides, per
tool, whether it is open to every signed-in person or only to selected people (Staff tab > Hub access).
Office-only pages keep their own office-password protection.
"""
import hmac
import json
import os

from flask import Blueprint, jsonify, redirect, request, send_from_directory
from itsdangerous import BadSignature, SignatureExpired
from werkzeug.security import check_password_hash

import leave

hub_bp = Blueprint('hub', __name__)
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
COOKIE = 'rs_hub'
MAX_AGE = 60 * 60 * 24 * 30
APPS = [('scheduler', 'Scheduler'), ('leave', 'Annual leave'), ('parts', 'Parts'),
        ('incident', 'Accident / incident report'), ('callout', 'Call out rota'), ('handbook', 'Company handbook'), ('policies', 'Company policies')]
APP_KEYS = [k for k, _ in APPS]
RULE_APPS = {'callout'}      # default: managers plus the people who report to Chris Scott
RULE_MANAGER_NAME = ('chris', 'scott')

# (match, app). 'exact' paths must match whole; others match as a prefix.
EXACT = {'/callout': 'callout', '/callout/': 'callout', '/staff-admin': 'office', '/staff-admin/': 'office', '/scheduler': 'scheduler', '/leave': 'leave', '/leave/': 'leave', '/incident/': 'incident', '/incident': 'incident',
         '/handbook/': 'handbook', '/handbook': 'handbook', '/api/handbook': 'handbook', '/api/parts/stock': 'parts',
         '/api/incident/report': 'incident', '/static/handbook/pages.json': 'handbook', '/static/handbook/handbook.pdf': 'handbook'}
PREFIX = [('/api/jobs', 'scheduler'), ('/api/schedule', 'scheduler'), ('/api/categories', 'scheduler'),
          ('/parts', 'parts'), ('/static/parts/', 'parts'), ('/policies', 'policies'), ('/static/policies/', 'policies'),
          ('/static/handbook/pages/', 'handbook'), ('/static/callout/', 'callout')]
OPEN_PREFIX = ('/static/parts/icons/', '/static/parts/manifest')          # needed before sign-in or by the browser


def _app_for(path):
    if path in EXACT: return EXACT[path]
    if path.startswith(OPEN_PREFIX): return None
    for p, a in PREFIX:
        if path == p or path.startswith(p if p.endswith('/') else p + '/'):
            return a
    return None


def _session():
    raw = request.cookies.get(COOKIE)
    if not raw: return None
    try: t = leave._serializer.loads(raw, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired): return None
    if not isinstance(t, dict) or not t.get('hub'): return None
    if t.get('role') == 'admin': return {'role': 'admin', 'id': None, 'name': 'Office'}
    rows = leave.query('SELECT * FROM leave_employees WHERE id = ? AND active = 1', (t.get('id'),))
    return {'role': 'employee', 'id': rows[0]['id'], 'name': rows[0]['name'], 'is_manager': bool(rows[0].get('is_manager')), 'manager_id': rows[0].get('manager_id')} if rows else None


def _access():
    try: a = json.loads(leave.get_setting('hub_access') or '{}')
    except ValueError: a = {}
    def mode(k):
        m = (a.get(k) or {}).get('mode')
        if k in RULE_APPS and m in (None, 'rule'): return 'rule'
        return 'some' if m == 'some' else 'all'
    return {k: {'mode': mode(k),
                'ids': [int(i) for i in (a.get(k) or {}).get('ids', []) if str(i).isdigit()]} for k in APP_KEYS}


def _allowed(sess, acc=None):
    if not sess: return []
    if sess['role'] == 'admin': return list(APP_KEYS) + ['office']
    acc = acc or _access()
    return [k for k in APP_KEYS if acc[k]['mode'] == 'all' or sess['id'] in acc[k]['ids'] or (acc[k]['mode'] == 'rule' and _in_rule(sess))]


def _in_rule(sess):
    """Managers, and anyone whose manager is Chris Scott."""
    if sess.get('is_manager'): return True
    if not sess.get('manager_id'): return False
    m = leave.query('SELECT name FROM leave_employees WHERE id = ?', (sess['manager_id'],))
    n = (m[0]['name'] if m else '').lower()
    return all(w in n for w in RULE_MANAGER_NAME)


@hub_bp.before_app_request
def gate():
    app = _app_for(request.path)
    if not app: return None
    if leave.is_admin(): return None                      # the office, signed in with its password
    sess = _session()
    if app == 'office':                                   # office-only pages: the office password, nobody else
        if sess and sess['role'] == 'admin': return None
    elif sess and app in _allowed(sess): return None
    is_page = request.method == 'GET' and not request.path.startswith(('/api/', '/static/'))
    if not sess:
        if is_page: return redirect('/login?next=' + request.path)
        return jsonify({'error': 'Not signed in'}), 401
    if is_page:
        return ('<meta name="viewport" content="width=device-width,initial-scale=1"><body style="font-family:system-ui;padding:2rem;max-width:480px;margin:auto">'
                '<h2>No access</h2><p style="margin:.8rem 0">You do not have access to this tool. Ask the office if you need it.</p>'
                '<a href="/" style="color:#0B2A4A;font-weight:600">&larr; Back to the hub</a></body>'), 403
    return jsonify({'error': 'No access'}), 403


def _cookie(resp, payload):
    resp.set_cookie(COOKIE, leave._serializer.dumps(dict(payload, hub=1)), max_age=MAX_AGE, httponly=True,
                    samesite='Lax', secure=request.headers.get('X-Forwarded-Proto', request.scheme) == 'https')
    return resp


@hub_bp.route('/staff-admin')
@hub_bp.route('/staff-admin/')
def staff_admin_page(): return send_from_directory(STATIC, 'leave.html')


@hub_bp.route('/callout')
@hub_bp.route('/callout/')
def callout_page(): return send_from_directory(os.path.join(STATIC, 'callout'), 'index.html')


@hub_bp.route('/login')
def login_page(): return send_from_directory(STATIC, 'login.html')


@hub_bp.route('/api/hub/login', methods=['POST'])
def hub_login():
    b = request.get_json(silent=True) or {}
    if b.get('admin'):
        if not leave.ADMIN_PASSWORD: return jsonify({'error': 'Office sign-in is not enabled'}), 503
        if leave._throttled('admin'): return jsonify({'error': 'Too many attempts - try again in 10 minutes'}), 429
        if not hmac.compare_digest(str(b.get('password', '')).encode(), leave.ADMIN_PASSWORD.encode()):
            leave._fail('admin'); return jsonify({'error': 'Wrong password'}), 403
        resp = jsonify({'name': 'Office', 'role': 'admin', 'leaveToken': leave._token({'role': 'admin'})})
        return _cookie(resp, {'role': 'admin'})
    key = f"hub{b.get('employeeId')}"
    if leave._throttled(key): return jsonify({'error': 'Too many attempts - try again in 10 minutes'}), 429
    rows = leave.query('SELECT * FROM leave_employees WHERE id = ? AND active = 1', (b.get('employeeId'),))
    emp = rows[0] if rows else None
    if not emp or not emp['pin_hash'] or not check_password_hash(emp['pin_hash'], str(b.get('pin', ''))):
        leave._fail(key); return jsonify({'error': 'Wrong PIN (ask the office if you have not been given one)'}), 403
    resp = jsonify({'name': emp['name'], 'role': 'employee', 'leaveToken': leave._token({'role': 'employee', 'id': emp['id']})})
    return _cookie(resp, {'role': 'employee', 'id': emp['id']})


@hub_bp.route('/api/hub/logout', methods=['POST'])
def hub_logout():
    resp = jsonify({'success': True}); resp.delete_cookie(COOKIE); return resp


@hub_bp.route('/api/hub/me')
def hub_me():
    s = _session()
    return jsonify({'signedIn': bool(s), 'name': s['name'] if s else '', 'role': s['role'] if s else '', 'apps': _allowed(s)})


@hub_bp.route('/api/hub/leave-token')
def hub_leave_token():
    s = _session()
    if not s: return jsonify({'error': 'Not signed in'}), 401
    return jsonify({'role': s['role'], 'token': leave._token({'role': 'admin'} if s['role'] == 'admin' else {'role': 'employee', 'id': s['id']})})


@hub_bp.route('/api/hub/admin/access', methods=['GET', 'POST'])
def hub_access():
    if not leave.is_admin(): return leave._auth_error()
    if request.method == 'POST':
        b = (request.get_json(silent=True) or {}).get('access') or {}
        def md(k):
            m = (b.get(k) or {}).get('mode')
            return m if (m in ('some', 'all') or (m == 'rule' and k in RULE_APPS)) else 'all'
        clean = {k: {'mode': md(k),
                     'ids': sorted({int(i) for i in (b.get(k) or {}).get('ids', []) if str(i).isdigit()})} for k in APP_KEYS}
        leave.set_setting('hub_access', json.dumps(clean))
    acc = _access()
    emps = leave.query('SELECT id, name FROM leave_employees WHERE active = 1 ORDER BY name')
    return jsonify({'apps': [{'key': k, 'label': l, **acc[k]} for k, l in APPS],
                    'employees': [{'id': e['id'], 'name': e['name']} for e in emps]})


# ── Email PINs ─────────────────────────────────────────────────────────────
import secrets
from werkzeug.security import generate_password_hash


def _pin_email(name, pin, url):
    first = (name or '').split(' ')[0] or 'there'
    return (f"Hi {first},\n\n"
            "Welcome to the R&S Staff Hub - one place on your phone or computer for the things you need at work:\n\n"
            "  - Annual leave: book time off, check your allowance and see your team's calendar\n"
            "  - Van parts finder: search van stock by part number and see which locker it is in\n"
            "  - Accident / incident reports: report an accident, near miss or hazard in a couple of minutes\n"
            "  - Company handbook and policies: read and search them any time\n"
            "  - Scheduler: job scheduling (for those who use it)\n\n"
            "HOW TO SIGN IN\n"
            f"  1. Go to {url}\n"
            "  2. Choose your name from the list\n"
            f"  3. Enter your PIN: {pin}\n\n"
            "Tip: once you are in, add the page to your phone's home screen so it is always one tap away.\n\n"
            "Please keep your PIN to yourself - it is your personal sign-in. If you lose it or think someone else knows it, "
            "ask the office and we will send you a new one. Anything you cannot see in the hub is not available to your role.\n\n"
            "R&S Services (Yorkshire) Ltd")


@hub_bp.route('/api/hub/admin/email-pins', methods=['POST'])
def hub_email_pins():
    """Give people a NEW random PIN and email it to them (existing PINs are stored scrambled, so cannot be re-sent)."""
    if not leave.is_admin(): return leave._auth_error()
    if not leave.email_enabled():
        return jsonify({'error': 'Email is not set up yet (RESEND_API_KEY / LEAVE_EMAIL_FROM missing on Render)'}), 400
    b = request.get_json(silent=True) or {}
    rows = leave.query('SELECT id, name, email FROM leave_employees WHERE active = 1 ORDER BY name')
    if not b.get('all'):
        want = {int(i) for i in b.get('ids', []) if str(i).isdigit()}
        rows = [r for r in rows if r['id'] in want]
    url = f"{request.headers.get('X-Forwarded-Proto', request.scheme)}://{request.host}/"
    sent, skipped, failed = [], [], []
    for r in rows:
        email = (r['email'] or '').strip()
        if '@' not in email: skipped.append(r['name']); continue
        pin = f'{secrets.randbelow(10000):04d}'
        try: leave.send_email(email, 'Your R&S Staff Hub sign-in', _pin_email(r['name'], pin, url))
        except Exception as ex: failed.append({'name': r['name'], 'error': str(ex)[:120]}); continue
        leave.execute('UPDATE leave_employees SET pin_hash = ? WHERE id = ?', (generate_password_hash(pin), r['id']))
        sent.append(r['name'])
    return jsonify({'sent': sent, 'noEmail': skipped, 'failed': failed})
