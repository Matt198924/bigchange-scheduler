import os
import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from datetime import datetime, timezone
import time

app = Flask(__name__, static_folder='static')
CORS(app)

CLIENT_ID     = os.environ.get('BIGCHANGE_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('BIGCHANGE_CLIENT_SECRET', '')
CUSTOMER_ID   = os.environ.get('BIGCHANGE_CUSTOMER_ID', '1564')
API_BASE      = 'https://api.bigchange.com/v1'
TOKEN_URL     = 'https://api.bigchange.com/auth/tokens'

_token_cache = {'token': None, 'expires_at': 0}

def get_token():
    now = time.time()
    if _token_cache['token'] and now < _token_cache['expires_at'] - 30:
        return _token_cache['token']

    print(f"[AUTH] Requesting token, CLIENT_ID set: {bool(CLIENT_ID)}, SECRET set: {bool(CLIENT_SECRET)}")
    try:
        resp = requests.post(TOKEN_URL, data={
            'grant_type':    'client_credentials',
            'client_id':     CLIENT_ID,
            'client_secret': CLIENT_SECRET,
        }, timeout=15)
        print(f"[AUTH] Status: {resp.status_code} Body: {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()
        _token_cache['token']      = data['access_token']
        _token_cache['expires_at'] = now + data.get('expires_in', 3600)
        print("[AUTH] Token OK")
        return _token_cache['token']
    except Exception as e:
        print(f"[AUTH] FAILED: {e}")
        raise

def bc_get(path, params=None):
    token = get_token()
    url = f'{API_BASE}{path}'
    print(f"[API] GET {url} params={params}")
    resp = requests.get(url, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type':  'application/json',
        'customer-id':   CUSTOMER_ID,
    }, params=params, timeout=15)
    print(f"[API] {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()
    return resp.json()

def bc_post(path, body):
    token = get_token()
    url = f'{API_BASE}{path}'
    print(f"[API] POST {url}")
    resp = requests.post(url, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type':  'application/json',
        'customer-id':   CUSTOMER_ID,
    }, json=body, timeout=15)
    print(f"[API] {resp.status_code}: {resp.text[:300]}")
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}

def parse_sla(job):
    for field in ['slaDeadline', 'requiredByDate', 'dueDate']:
        val = job.get(field)
        if val:
            return val
    sla = job.get('sla') or {}
    return sla.get('deadline')

def get_priority(job):
    deadline = parse_sla(job)
    if not deadline:
        return 'ok'
    try:
        dt = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
        hours_left = (dt - datetime.now(timezone.utc)).total_seconds() / 3600
        if hours_left < 0:  return 'breach'
        if hours_left < 2:  return 'urgent'
        return 'ok'
    except Exception:
        return 'ok'

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/status')
def status():
    try:
        get_token()
        return jsonify({'status': 'connected', 'customer_id': CUSTOMER_ID})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/jobs')
def get_jobs():
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        try:
            data = bc_get('/jobs', {'status': 'unassigned', 'date': today, 'pageSize': 100})
        except Exception as e1:
            print(f"[JOBS] First attempt failed: {e1}, trying without filters")
            try:
                data = bc_get('/jobs', {'date': today, 'pageSize': 100})
            except Exception as e2:
                print(f"[JOBS] Second attempt failed: {e2}, trying bare endpoint")
                data = bc_get('/jobs')

        raw = data if isinstance(data, list) else (
            data.get('items') or data.get('data') or data.get('jobs') or []
        )
        print(f"[JOBS] Got {len(raw)} raw jobs")

        jobs = []
        for j in raw:
            if j.get('assignedResourceId') or j.get('engineerId'):
                continue
            priority = get_priority(j)
            jobs.append({
                'id':       str(j.get('id') or j.get('jobId') or j.get('reference', '')),
                'ref':      str(j.get('reference') or j.get('jobNumber') or j.get('id', '')),
                'desc':     j.get('description') or j.get('jobType') or 'Job',
                'client':   j.get('customerName') or j.get('contactName') or j.get('client') or '—',
                'area':     j.get('postcode') or (j.get('address') or {}).get('postcode') or j.get('location') or '—',
                'type':     j.get('jobType') or j.get('type') or 'Reactive',
                'sla':      parse_sla(j),
                'priority': priority,
            })

        order = {'breach': 0, 'urgent': 1, 'ok': 2}
        jobs.sort(key=lambda j: (order.get(j['priority'], 2), j['sla'] or '9999'))
        return jsonify({'jobs': jobs, 'total': len(jobs)})

    except Exception as e:
        print(f"[JOBS] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/engineers')
def get_engineers():
    try:
        try:
            data = bc_get('/resources', {'type': 'engineer', 'pageSize': 200})
        except Exception as e1:
            print(f"[ENG] First attempt failed: {e1}")
            try:
                data = bc_get('/resources', {'pageSize': 200})
            except Exception as e2:
                print(f"[ENG] Second attempt failed: {e2}")
                data = bc_get('/resources')

        raw = data if isinstance(data, list) else (
            data.get('items') or data.get('data') or data.get('resources') or []
        )
        print(f"[ENG] Got {len(raw)} raw engineers")

        engineers = []
        for i, e in enumerate(raw):
            status_raw = (e.get('status') or e.get('currentStatus') or '').lower()
            if 'travel' in status_raw or 'driving' in status_raw:
                status = 'travelling'
            elif 'busy' in status_raw or 'on site' in status_raw or 'working' in status_raw:
                status = 'busy'
            else:
                status = 'available'

            name = e.get('name') or f"{e.get('firstName','')} {e.get('lastName','')}".strip() or 'Engineer'
            engineers.append({
                'id':     str(e.get('id') or e.get('resourceId', i)),
                'name':   name,
                'region': e.get('region') or e.get('area') or e.get('homePostcode') or '—',
                'status': status,
                'jobs':   e.get('jobCount') or e.get('scheduledJobs') or 0,
                'cap':    e.get('capacity') or 8,
                'skills': e.get('skills') or e.get('certifications') or [],
                'lat':    e.get('latitude') or (e.get('currentLocation') or {}).get('latitude'),
                'lng':    e.get('longitude') or (e.get('currentLocation') or {}).get('longitude'),
            })

        return jsonify({'engineers': engineers, 'total': len(engineers)})

    except Exception as e:
        print(f"[ENG] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/jobs/<job_id>/assign', methods=['POST'])
def assign_job(job_id):
    try:
        body = request.get_json()
        resource_id = body.get('resourceId')
        if not resource_id:
            return jsonify({'error': 'resourceId required'}), 400
        result = bc_post(f'/jobs/{job_id}/assign', {'resourceId': resource_id})
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        print(f"[ASSIGN] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
