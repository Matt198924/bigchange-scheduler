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
    print(f"[AUTH] {resp.status_code}: {resp.text[:200]}")
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
    }, params=params, timeout=15)
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
    print(f"[POST] {resp.status_code}: {resp.text[:300]}")
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}

def is_group_entry(name):
    """Filter out vehicle/group entries like (000) Dylan Mason"""
    return bool(re.match(r'^\(\d+\)', name.strip()))

def parse_sla(job):
    for field in ['slaDeadline', 'requiredByDate', 'dueDate']:
        val = job.get(field)
        if val:
            return val
    return (job.get('sla') or {}).get('deadline')

def get_priority(job):
    deadline = parse_sla(job)
    if not deadline:
        return 'ok'
    try:
        dt = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
        hours_left = (dt - datetime.now(timezone.utc)).total_seconds() / 3600
        if hours_left < 0: return 'breach'
        if hours_left < 2: return 'urgent'
        return 'ok'
    except Exception:
        return 'ok'

def get_duration_minutes(job):
    """Extract job duration in minutes from various possible fields"""
    for field in ['plannedDuration', 'duration', 'estimatedDuration', 'durationMinutes']:
        val = job.get(field)
        if val:
            try:
                v = float(val)
                # If value looks like hours (< 24), convert to minutes
                if v < 24:
                    return int(v * 60)
                return int(v)
            except Exception:
                pass
    return 60  # default 60 mins

def format_job(j):
    planned_start = j.get('plannedStartAt') or j.get('startAt') or j.get('scheduledStart')
    actual_start  = j.get('actualStartAt')
    planned_end   = j.get('plannedEndAt') or j.get('endAt') or j.get('scheduledEnd')
    actual_end    = j.get('actualEndAt')

    return {
        'id':           str(j.get('id', '')),
        'ref':          str(j.get('reference') or j.get('jobNumber') or j.get('id', '')),
        'desc':         j.get('description') or j.get('jobType') or 'Job',
        'client':       j.get('customerName') or j.get('contactName') or j.get('client') or '—',
        'area':         j.get('postcode') or (j.get('address') or {}).get('postcode') or j.get('location') or '—',
        'type':         j.get('jobTypeName') or j.get('jobType') or j.get('type') or 'Reactive',
        'status':       j.get('status') or '',
        'sla':          parse_sla(j),
        'priority':     get_priority(j),
        'resourceId':   str(j.get('resourceId') or ''),
        'plannedStart': planned_start,
        'actualStart':  actual_start,
        'plannedEnd':   planned_end,
        'actualEnd':    actual_end,
        'startTime':    actual_start or planned_start,
        'endTime':      actual_end or planned_end,
        'durationMins': get_duration_minutes(j),
    }

COMPLETED_STATUSES = {'completedOk', 'completedWithIssues', 'cancelled'}

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

@app.route('/api/engineers')
def get_engineers():
    try:
        data = bc_get('/resources', {'pageSize': 200})
        raw = data if isinstance(data, list) else (
            data.get('items') or data.get('data') or data.get('resources') or []
        )
        print(f"[ENG] Got {len(raw)} raw resources")

        engineers = []
        for i, e in enumerate(raw):
            name = e.get('name') or f"{e.get('firstName','')} {e.get('lastName','')}".strip() or 'Engineer'
            if is_group_entry(name):
                continue
            # Also skip entries that are clearly vehicles (have reg plates etc)
            if e.get('type', '').lower() in ['vehicle', 'asset']:
                continue

            status_raw = (e.get('status') or e.get('currentStatus') or '').lower()
            if 'travel' in status_raw or 'driving' in status_raw:
                status = 'travelling'
            elif 'busy' in status_raw or 'on site' in status_raw or 'working' in status_raw:
                status = 'busy'
            else:
                status = 'available'

            # Clean up trainee marker
            is_trainee = '(T)' in name or '(TS)' in name
            clean_name = name.replace('(T)', '').replace('(TS)', '').strip()

            engineers.append({
                'id':        str(e.get('id') or e.get('resourceId', i)),
                'name':      clean_name,
                'isTrainee': is_trainee,
                'region':    e.get('region') or e.get('area') or e.get('homePostcode') or '—',
                'status':    status,
                'skills':    e.get('skills') or e.get('certifications') or [],
                'lat':       e.get('latitude') or (e.get('currentLocation') or {}).get('latitude'),
                'lng':       e.get('longitude') or (e.get('currentLocation') or {}).get('longitude'),
            })

        print(f"[ENG] Returning {len(engineers)} real engineers")
        return jsonify({'engineers': engineers, 'total': len(engineers)})

    except Exception as e:
        print(f"[ENG] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/jobs/unassigned')
def get_unassigned_jobs():
    """Today's and tomorrow's unassigned/unscheduled jobs"""
    try:
        today_start = datetime.now().strftime('%Y-%m-%dT00:00:00')
        tomorrow_end = (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%dT23:59:59')

        data = bc_get('/jobs', {
            'StartAtFrom': today_start,
            'StartAtTo': tomorrow_end,
            'status': ['new', 'unscheduled'],
            'pageSize': 200
        })

        raw = data if isinstance(data, list) else (
            data.get('items') or data.get('data') or data.get('jobs') or []
        )
        print(f"[UNASSIGNED] Got {len(raw)} raw jobs")

        jobs = []
        for j in raw:
            status = (j.get('status') or '').lower()
            # Skip completed jobs
            if status in [s.lower() for s in COMPLETED_STATUSES]:
                continue
            # Skip already assigned jobs
            if j.get('resourceId'):
                continue
            jobs.append(format_job(j))

        order = {'breach': 0, 'urgent': 1, 'ok': 2}
        jobs.sort(key=lambda j: (order.get(j['priority'], 2), j['sla'] or '9999'))
        print(f"[UNASSIGNED] Returning {len(jobs)} unassigned jobs")
        return jsonify({'jobs': jobs, 'total': len(jobs)})

    except Exception as e:
        print(f"[UNASSIGNED] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/schedule/tomorrow')
def get_tomorrow_schedule():
    """Get all scheduled jobs for tomorrow, grouped by engineer"""
    try:
        tomorrow = datetime.now() + timedelta(days=1)
        tomorrow_start = tomorrow.strftime('%Y-%m-%dT00:00:00')
        tomorrow_end   = tomorrow.strftime('%Y-%m-%dT23:59:59')

        data = bc_get('/jobs', {
            'plannedAtFrom': tomorrow_start,
            'plannedAtTo':   tomorrow_end,
            'pageSize': 1000
        })

        raw = data if isinstance(data, list) else (
            data.get('items') or data.get('data') or data.get('jobs') or []
        )
        print(f"[TOMORROW] Got {len(raw)} raw jobs")

        # Group by resource/engineer
        by_engineer = {}
        unassigned_tomorrow = []

        for j in raw:
            status = (j.get('status') or '').lower()
            if status in [s.lower() for s in COMPLETED_STATUSES]:
                continue

            job = format_job(j)
            rid = job['resourceId']

            if rid:
                if rid not in by_engineer:
                    by_engineer[rid] = []
                by_engineer[rid].append(job)
            else:
                unassigned_tomorrow.append(job)

        # Sort each engineer's jobs by start time
        for rid in by_engineer:
            by_engineer[rid].sort(key=lambda j: j['startTime'] or '99:99')

        print(f"[TOMORROW] {len(by_engineer)} engineers with jobs, {len(unassigned_tomorrow)} unassigned")
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
        resource_id = body.get('resourceId')
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
