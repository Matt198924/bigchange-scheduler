import os
import re
import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from datetime import datetime, timezone, timedelta
import time

app = Flask(__name__, static_folder='static')
CORS(app)

CLIENT_ID     = os.environ.get('BIGCHANGE_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('BIGCHANGE_CLIENT_SECRET', '')
CUSTOMER_ID   = os.environ.get('BIGCHANGE_CUSTOMER_ID', '1564')
API_BASE      = 'https://api.bigchange.com/v1'
TOKEN_URL     = 'https://api.bigchange.com/auth/tokens'

# Category keywords — if categoryName contains any of these it's valid
VALID_CATEGORY_KEYWORDS = [
    'new jobs',
    'allocations',
]

COMPLETED_STATUSES = {'completedok', 'completedwithissues', 'cancelled'}

_token_cache = {'token': None, 'expires_at': 0}

def get_token():
    now = time.time()
    if _token_cache['token'] and now < _token_cache['expires_at'] - 30:
        return _token_cache['token']
    resp = requests.post(TOKEN_URL, data={
        'grant_type': 'client_credentials',
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    _token_cache['token'] = data['access_token']
    _token_cache['expires_at'] = now + data.get('expires_in', 3600)
    return _token_cache['token']

def bc_get(path, params=None):
    token = get_token()
    url = f'{API_BASE}{path}'
    print(f"[API] GET {url} params={params}")
    resp = requests.get(url, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'customer-id': CUSTOMER_ID,
    }, params=params, timeout=30)
    print(f"[API] {resp.status_code}: {resp.text[:300]}")
    resp.raise_for_status()
    return resp.json()

def bc_post(path, body):
    token = get_token()
    url = f'{API_BASE}{path}'
    resp = requests.post(url, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'customer-id': CUSTOMER_ID,
    }, json=body, timeout=15)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}

def is_group_entry(name):
    return bool(re.match(r'^\(\d+\)', name.strip()))

def is_valid_category(job):
    cat = (job.get('categoryName') or '').lower().strip()
    if not cat:
        return True  # include if no category set
    return any(kw in cat for kw in VALID_CATEGORY_KEYWORDS)

def get_sla_from_custom_fields(job):
    for cf in (job.get('customFields') or []):
        caption = ((cf.get('definition') or {}).get('caption') or '').lower()
        val = cf.get('value')
        if 'sla' in caption and val:
            return val
    return None

def get_priority(job):
    deadline = get_sla_from_custom_fields(job) or job.get('dueAt') or job.get('dueDate')
    if not deadline:
        return 'ok'
    try:
        dt = datetime.fromisoformat(str(deadline).replace('Z', '+00:00'))
        hours_left = (dt - datetime.now(timezone.utc)).total_seconds() / 3600
        if hours_left < 0: return 'breach'
        if hours_left < 2: return 'urgent'
        return 'ok'
    except Exception:
        return 'ok'

def get_duration_minutes(job):
    for field in ['plannedDuration', 'actualDuration', 'duration']:
        val = job.get(field)
        if val:
            try:
                v = float(val)
                return int(v) if v > 24 else int(v * 60)
            except Exception:
                pass
    return 60

def format_job(j):
    planned_start = j.get('plannedStartAt') or j.get('startAt')
    actual_start  = j.get('actualStartAt')
    planned_end   = j.get('plannedEndAt') or j.get('endAt')
    actual_end    = j.get('actualEndAt')
    loc = j.get('contactLocation') or {}

    return {
        'id':           str(j.get('id', '')),
        'ref':          str(j.get('reference') or j.get('id', '')),
        'desc':         (j.get('description') or 'Job')[:120],
        'client':       j.get('contactName') or j.get('customerName') or '—',
        'area':         j.get('contactAddress') or '—',
        'type':         j.get('typeName') or j.get('jobType') or 'Reactive',
        'status':       j.get('status') or '',
        'category':     j.get('categoryName') or '',
        'sla':          get_sla_from_custom_fields(j),
        'priority':     get_priority(j),
        'resourceId':   str(j.get('resourceId') or ''),
        'resourceName': j.get('resourceName') or '',
        'plannedStart': planned_start,
        'startTime':    actual_start or planned_start,
        'endTime':      actual_end or planned_end,
        'durationMins': get_duration_minutes(j),
        'lat':          loc.get('latitude'),
        'lng':          loc.get('longitude'),
    }

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/status')
def status():
    try:
        get_token()
        return jsonify({'status': 'connected'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/engineers')
def get_engineers():
    try:
        data = bc_get('/resources', {'pageSize': 200})
        raw = data if isinstance(data, list) else (data.get('items') or [])
        engineers = []
        for i, e in enumerate(raw):
            name = e.get('name') or f"{e.get('firstName','')} {e.get('lastName','')}".strip() or 'Engineer'
            if is_group_entry(name):
                continue
            if e.get('type', '').lower() in ['vehicle', 'asset']:
                continue
            is_trainee = '(T)' in name or '(TS)' in name
            clean_name = name.replace('(T)', '').replace('(TS)', '').strip()
            engineers.append({
                'id':        str(e.get('id') or i),
                'name':      clean_name,
                'isTrainee': is_trainee,
                'region':    e.get('region') or e.get('homePostcode') or '—',
                'status':    'available',
            })
        print(f"[ENG] {len(engineers)} engineers")
        return jsonify({'engineers': engineers, 'total': len(engineers)})
    except Exception as e:
        print(f"[ENG] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/jobs/unassigned')
def get_unassigned_jobs():
    try:
        # Pull a wide window — jobs created recently that are unassigned
        # Use CreatedAt range as a fallback to catch all pending jobs
        two_weeks_ago = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%dT00:00:00')
        tomorrow_end  = (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%dT23:59:59')

        data = bc_get('/jobs', {
            'CreatedAtFrom': two_weeks_ago,
            'CreatedAtTo':   tomorrow_end,
            'pageSize':      1000,
        })

        raw = data if isinstance(data, list) else (data.get('items') or [])
        print(f"[UNASSIGNED] Got {len(raw)} raw jobs")

        # Log unique categories for debugging
        cats = set(j.get('categoryName','') for j in raw)
        print(f"[UNASSIGNED] Categories found: {cats}")

        jobs = []
        for j in raw:
            status = (j.get('status') or '').lower()
            if status in COMPLETED_STATUSES:
                continue
            if j.get('resourceId'):
                continue
            if not is_valid_category(j):
                print(f"[UNASSIGNED] Skipping category: {j.get('categoryName')}")
                continue
            jobs.append(format_job(j))

        order = {'breach': 0, 'urgent': 1, 'ok': 2}
        jobs.sort(key=lambda j: (order.get(j['priority'], 2), j['sla'] or '9999'))
        print(f"[UNASSIGNED] Returning {len(jobs)} jobs")
        return jsonify({'jobs': jobs, 'total': len(jobs)})

    except Exception as e:
        print(f"[UNASSIGNED] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/jobs/sample')
def get_sample_job():
    try:
        two_weeks_ago = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%dT00:00:00')
        today_end     = datetime.now().strftime('%Y-%m-%dT23:59:59')
        data = bc_get('/jobs', {
            'CreatedAtFrom': two_weeks_ago,
            'CreatedAtTo':   today_end,
            'pageSize':      5,
        })
        raw = data if isinstance(data, list) else (data.get('items') or [])
        # Return categories and sample
        cats = list(set(j.get('categoryName','none') for j in raw))
        unassigned = [j for j in raw if not j.get('resourceId') and (j.get('status') or '').lower() not in COMPLETED_STATUSES]
        return jsonify({'categories': cats, 'unassigned_count': len(unassigned), 'sample': raw[:2]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/schedule/tomorrow')
def get_tomorrow_schedule():
    try:
        tomorrow = datetime.now() + timedelta(days=1)
        tomorrow_start = tomorrow.strftime('%Y-%m-%dT00:00:00')
        tomorrow_end   = tomorrow.strftime('%Y-%m-%dT23:59:59')

        data = bc_get('/jobs', {
            'plannedAtFrom': tomorrow_start,
            'plannedAtTo':   tomorrow_end,
            'pageSize':      1000,
        })

        raw = data if isinstance(data, list) else (data.get('items') or [])
        print(f"[TOMORROW] Got {len(raw)} raw jobs")

        by_engineer = {}
        unassigned_tomorrow = []

        for j in raw:
            status = (j.get('status') or '').lower()
            if status in COMPLETED_STATUSES:
                continue
            job = format_job(j)
            rid = job['resourceId']
            if rid:
                if rid not in by_engineer:
                    by_engineer[rid] = []
                by_engineer[rid].append(job)
            else:
                unassigned_tomorrow.append(job)

        for rid in by_engineer:
            by_engineer[rid].sort(key=lambda j: j['startTime'] or '99:99')

        return jsonify({
            'byEngineer': by_engineer,
            'unassigned': unassigned_tomorrow,
            'date': tomorrow.strftime('%Y-%m-%d')
        })

    except Exception as e:
        print(f"[TOMORROW] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/jobs/<job_id>/assign', methods=['POST'])
def assign_job(job_id):
    try:
        body = request.get_json()
        resource_id   = body.get('resourceId')
        planned_start = body.get('plannedStart')
        if not resource_id:
            return jsonify({'error': 'resourceId required'}), 400
        payload = {'resourceId': int(resource_id)}
        if planned_start:
            payload['plannedStartAt'] = planned_start
        result = bc_post(f'/jobs/{job_id}/assign', payload)
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        print(f"[ASSIGN] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
