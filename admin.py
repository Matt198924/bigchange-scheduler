"""Password-protected admin section: stock reorder levels via the legacy
BigChange (JobWatch) web services.

Everything under /admin/api/ except /login and /session requires a logged-in
admin session, enforced here on the server. The legacy credentials are only
read by this module and are never sent to the browser.

Env vars (all set on Render, never in code):
  ADMIN_PASSWORD   password for the admin section (admin is disabled if unset)
  BC_LEGACY_USER   JobWatch username for the web services
  BC_LEGACY_PASS   JobWatch password
  BC_LEGACY_KEY    the 'key' header value issued by BigChange
  AUDIT_LOG_PATH   optional, defaults to audit.log next to this file
"""
import csv
import hmac
import io
import json
import os
import time
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Blueprint, Response, jsonify, request, send_from_directory, session

admin_bp = Blueprint('admin', __name__)

LEGACY_URL = 'https://webservice.bigchange.com/v01/services.ashx'
AUDIT_LOG_PATH = os.environ.get(
    'AUDIT_LOG_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audit.log'))

# Wrong-password throttle: max attempts per IP per window.
_LOGIN_WINDOW = 15 * 60
_LOGIN_MAX = 5
_login_attempts = {}

_list_cache = {'ts': 0, 'rows': None}


def _admin_password():
    return os.environ.get('ADMIN_PASSWORD', '')


def _client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    return (fwd.split(',')[0].strip() if fwd else request.remote_addr) or 'unknown'


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _admin_password():
            return jsonify({'error': 'Admin section is not configured'}), 503
        if not session.get('admin'):
            return jsonify({'error': 'Not logged in'}), 401
        return fn(*args, **kwargs)
    return wrapper


def audit(event, **details):
    entry = {'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
             'ip': _client_ip(), 'event': event, **details}
    line = json.dumps(entry)
    print(f"[AUDIT] {line}")
    try:
        with open(AUDIT_LOG_PATH, 'a') as f:
            f.write(line + '\n')
    except OSError as e:
        print(f"[AUDIT] could not write log file: {e}")


# ---- legacy web service client -------------------------------------------

def legacy_call(action, **params):
    user = os.environ.get('BC_LEGACY_USER', '')
    pwd = os.environ.get('BC_LEGACY_PASS', '')
    key = os.environ.get('BC_LEGACY_KEY', '')
    if not (user and pwd and key):
        raise RuntimeError('Legacy BigChange credentials are not configured')
    resp = requests.get(
        LEGACY_URL,
        params={'action': action, **params},
        auth=(user, pwd),
        headers={'key': key},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get('Code') != 0:
        raise RuntimeError(f"BigChange returned code {data.get('Code')}: "
                           f"{data.get('Message') or data.get('Result') or 'no message'}")
    return data


def _norm(row):
    """Case-insensitive view of a reorder level row (the docs mix casing)."""
    low = {k.lower(): v for k, v in row.items()}
    return {
        'reorderId': low.get('stockreorderid'),
        'stockId': low.get('stockreorderstockid'),
        'quantity': low.get('stockreorderquantity'),
        'entityId': low.get('stockreorderentityid'),
        'entityType': low.get('stockreorderentitytype'),
    }


def list_reorder_levels(force=False):
    if not force and _list_cache['rows'] is not None and time.time() - _list_cache['ts'] < 60:
        return _list_cache['rows']
    data = legacy_call('ListStockReorderLevel')
    rows = [_norm(r) for r in (data.get('Result') or [])]
    _list_cache.update(ts=time.time(), rows=rows)
    return rows


# ---- routes ---------------------------------------------------------------

@admin_bp.route('/admin')
def admin_page():
    # The page itself holds no secrets; every API call behind it is protected.
    return send_from_directory('static', 'admin.html')


@admin_bp.route('/admin/api/session')
def admin_session():
    return jsonify({'loggedIn': bool(session.get('admin')),
                    'configured': bool(_admin_password())})


@admin_bp.route('/admin/api/login', methods=['POST'])
def admin_login():
    if not _admin_password():
        return jsonify({'error': 'Admin section is not configured'}), 503
    ip = _client_ip()
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW]
    if len(attempts) >= _LOGIN_MAX:
        audit('login_blocked', reason='too many attempts')
        return jsonify({'error': 'Too many attempts. Try again later.'}), 429
    body = request.get_json(silent=True) or {}
    supplied = str(body.get('password', ''))
    if hmac.compare_digest(supplied.encode(), _admin_password().encode()):
        _login_attempts.pop(ip, None)
        session.clear()
        session['admin'] = True
        session.permanent = False
        audit('login_ok')
        return jsonify({'loggedIn': True})
    attempts.append(now)
    _login_attempts[ip] = attempts
    audit('login_failed')
    return jsonify({'error': 'Wrong password'}), 401


@admin_bp.route('/admin/api/logout', methods=['POST'])
def admin_logout():
    session.clear()
    return jsonify({'loggedIn': False})


@admin_bp.route('/admin/api/ping')
@require_admin
def admin_ping():
    """Checks the legacy web service login works."""
    try:
        legacy_call('ping')
        return jsonify({'status': 'connected'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 502


@admin_bp.route('/admin/api/reorder-levels')
@require_admin
def get_reorder_levels():
    """All reorder levels, or only those for ?contactId=. ?format=csv downloads."""
    contact_id = request.args.get('contactId', type=int)
    try:
        rows = list_reorder_levels(force=request.args.get('refresh') == '1')
    except Exception as e:
        print(f"[ADMIN] list failed: {e}")
        return jsonify({'error': str(e)}), 502

    if contact_id is not None:
        rows = [r for r in rows
                if r['entityId'] == contact_id
                and str(r['entityType'] or '').lower() == 'contact']
    audit('reorder_levels_read', contactId=contact_id, rows=len(rows),
          csv=request.args.get('format') == 'csv')

    if request.args.get('format') == 'csv':
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['ReorderId', 'StockId', 'ReorderQuantity', 'EntityId', 'EntityType'])
        for r in rows:
            w.writerow([r['reorderId'], r['stockId'], r['quantity'],
                        r['entityId'], r['entityType']])
        name = f"reorder-levels-{contact_id if contact_id is not None else 'all'}.csv"
        return Response(buf.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename={name}'})
    return jsonify({'rows': rows, 'total': len(rows)})


@admin_bp.route('/admin/api/reorder-levels', methods=['POST'])
@require_admin
def add_reorder_level():
    """Push a reorder level to BigChange. dryRun defaults to TRUE: nothing is
    sent unless the body explicitly says dryRun: false."""
    if not request.is_json:
        return jsonify({'error': 'JSON body required'}), 415
    body = request.get_json(silent=True) or {}
    try:
        stock_id = int(body['stockId'])
        entity_id = int(body['entityId'])
        entity_type = int(body.get('entityType', 1))
        quantity = float(body['quantity'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'stockId, entityId and quantity are required numbers'}), 400
    if entity_type not in (1, 2):
        return jsonify({'error': 'entityType must be 1 (Contact) or 2 (Asset)'}), 400
    dry_run = body.get('dryRun', True) is not False

    request_params = {'StockId': stock_id, 'EntityType': entity_type,
                      'EntityId': entity_id, 'Quantity': quantity}
    type_name = 'contact' if entity_type == 1 else 'asset'

    try:
        existing = [r for r in list_reorder_levels(force=True)
                    if r['stockId'] == stock_id and r['entityId'] == entity_id
                    and str(r['entityType'] or '').lower() == type_name]
    except Exception as e:
        return jsonify({'error': f'Could not read current levels: {e}'}), 502

    if dry_run:
        audit('reorder_level_dry_run', **{k.lower(): v for k, v in request_params.items()})
        return jsonify({
            'dryRun': True,
            'wouldSend': request_params,
            'currentLevels': existing,
            'note': 'Nothing was sent to BigChange.',
        })

    try:
        result = legacy_call('AddStockReorderLevel', **request_params)
    except Exception as e:
        audit('reorder_level_write_failed', error=str(e),
              **{k.lower(): v for k, v in request_params.items()})
        return jsonify({'error': str(e)}), 502
    _list_cache['rows'] = None
    audit('reorder_level_written', before=existing, result=result.get('Result'),
          **{k.lower(): v for k, v in request_params.items()})
    return jsonify({'dryRun': False, 'sent': request_params,
                    'previousLevels': existing, 'result': result.get('Result')})


@admin_bp.route('/admin/api/audit')
@require_admin
def read_audit():
    try:
        with open(AUDIT_LOG_PATH) as f:
            lines = f.readlines()[-100:]
    except OSError:
        lines = []
    entries = []
    for ln in lines:
        try:
            entries.append(json.loads(ln))
        except ValueError:
            pass
    return jsonify({'entries': list(reversed(entries))})
