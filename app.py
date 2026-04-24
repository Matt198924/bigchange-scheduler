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

VALID_CATEGORY_KEYWORDS = ['new jobs', 'allocations']
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
    resp = requests.post(f'{API_BASE}{path}', headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'customer-id': CUSTOMER_ID,
    }, json=body, timeout=15)
    resp.raise_for_status()
    try: return resp.json()
    except: return {}

def is_group_entry(name):
    return bool(re.match(r'^\(\d+\)', name.strip()))

def is_valid_category(job):
    cat = (job.get('categoryName') or '').lower().strip()
    if not cat:
        return True
    return any(kw in cat for kw in VALID_CATEGORY_KEYWORDS)

def get_duration_minutes(job):
    for field in ['plannedDuration', 'actualDuration', 'duration']:
        val = job.get(field)
        if val:
            try:
                v = float(val)
                return int(v) if v > 24 else int(v * 60)
            except: pass
    return 60

def format_job(j):
    loc = j.get('contactLocation') or {}
    return {
        'id':           str(j.get('id', '')),
        'ref':          str(j.get('reference') or j.get('id', '')),
        'desc':         (j.get('description') or 'Job')[:100],
        'client':       j.get('contactName') or j.get('customerName') or '—',
        'area':         j.get('contactAddress') or '—',
        'type':         j.get('typeName') or 'Reactive',
        'status':       j.get('status') or '',
        'category':     j.get('categoryName') or '',
        'resourceId':   str(j.get('resourceId') or ''),
        'resourceName': j.get('resourceName') or '',
        'startTime':    j.get('actualStartAt') or j.get('plannedStartAt'),
        'endTime':      j.get('actualEndAt') or j.get('plannedEndAt'),
        'durationMins': get_duration_minutes(j),
        'lat':          loc.get('latitude'),
        'lng':          loc.get('longitude'),
    }

def fetch_paged(params):
    all_items = []
    page = 1
    while page <= 10:
        data = bc_get('/jobs', {**params, 'pageNumber': page, 'pageSize': 1000})
        raw = data if isinstance(data, list) else (data.get('items') or [])
        all_items.extend(raw)
        print(f"[PAGED] Page {page}: {len(raw)} items")
        if len(raw) < 1000:
            break
        page += 1
    return all_items

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
            if is_group_entry(name): continue
            if e.get('type', '').lower() in ['vehicle', 'asset']: continue
            is_trainee = '(T)' in name or '(TS)' in name
            engineers.append({
                'id':        str(e.get('id') or i),
                'name':      name.replace('(T)', '').replace('(TS)', '').strip(),
                'isTrainee': is_trainee,
                'region':    e.get('region') or e.get('homePostcode') or '—',
            })
        return jsonify({'engineers': engineers, 'total': len(engineers)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/jobs/unassigned')
def get_unassigned_jobs():
    try:
        # Fetch only jobs with no resource assigned, modified in last 30 days
        # Use a rolling window to keep response small
        from_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%dT00:00:00')
        to_date   = (datetime.now() + timedelta(days=60)).strftime('%Y-%m-%dT23:59:59')

        raw = fetch_paged({
            'StatusModifiedAtFrom': from_date,
            'StatusModifiedAtTo':   to_date,
        })

        print(f"[UNASSIGNED] Fetched {len(raw)} jobs")

        jobs = []
        for j in raw:
            status = (j.get('status') or '').lower()
            if status in COMPLETED_STATUSES: continue
            if status == 'cancelled': continue
            if j.get('resourceId'): continue  # skip assigned jobs
            if not is_valid_category(j): continue
            jobs.append(format_job(j))

        jobs.sort(key=lambda j: -j['durationMins'])
        print(f"[UNASSIGNED] Returning {len(jobs)} unassigned jobs in valid categories")
        return jsonify({'jobs': jobs, 'total': len(jobs)})

    except Exception as e:
        print(f"[UNASSIGNED] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/schedule/tomorrow')
def get_tomorrow_schedule():
    try:
        tomorrow = datetime.now() + timedelta(days=1)
        raw = fetch_paged({
            'plannedAtFrom': tomorrow.strftime('%Y-%m-%dT00:00:00'),
            'plannedAtTo':   tomorrow.strftime('%Y-%m-%dT23:59:59'),
        })
        print(f"[TOMORROW] Got {len(raw)} jobs")

        by_engineer = {}
        for j in raw:
            if (j.get('status') or '').lower() in COMPLETED_STATUSES: continue
            rid = str(j.get('resourceId') or '')
            if not rid: continue
            by_engineer.setdefault(rid, []).append(format_job(j))

        for rid in by_engineer:
            by_engineer[rid].sort(key=lambda j: j['startTime'] or '99:99')

        # Build engineer name map from job data (resourceName is on each job)
        eng_names = {}
        for rid, eng_jobs in by_engineer.items():
            for j in eng_jobs:
                if j.get('resourceName'):
                    eng_names[rid] = j['resourceName']
                    break

        return jsonify({'byEngineer': by_engineer, 'engNames': eng_names, 'date': tomorrow.strftime('%Y-%m-%d')})
    except Exception as e:
        print(f"[TOMORROW] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/schedule/today')
def get_today_schedule():
    try:
        today = datetime.now()
        raw = fetch_paged({
            'plannedAtFrom': today.strftime('%Y-%m-%dT00:00:00'),
            'plannedAtTo':   today.strftime('%Y-%m-%dT23:59:59'),
        })
        print(f"[TODAY] Got {len(raw)} jobs")

        by_engineer = {}
        for j in raw:
            rid = str(j.get('resourceId') or '')
            if not rid: continue
            by_engineer.setdefault(rid, []).append(format_job(j))

        for rid in by_engineer:
            by_engineer[rid].sort(key=lambda j: j['startTime'] or '99:99')

        eng_names = {}
        for rid, eng_jobs in by_engineer.items():
            for j in eng_jobs:
                if j.get('resourceName'):
                    eng_names[rid] = j['resourceName']
                    break

        return jsonify({'byEngineer': by_engineer, 'engNames': eng_names, 'date': today.strftime('%Y-%m-%d')})
    except Exception as e:
        print(f"[TODAY] ERROR: {e}")
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
        # Correct endpoint is PUT /jobs/:jobId/schedule
        token = get_token()
        url = f'{API_BASE}/jobs/{job_id}/schedule'
        resp = requests.put(url, headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'customer-id': CUSTOMER_ID,
        }, json=payload, timeout=15)
        print(f"[ASSIGN] {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
        return jsonify({'success': True})
    except Exception as e:
        print(f"[ASSIGN] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/jobs/<job_id>/constraints', methods=['GET'])
def get_job_constraints(job_id):
    try:
        data = bc_get(f'/jobs/{job_id}/constraints', {'pageSize': 100})
        items = data if isinstance(data, list) else (data.get('items') or [])
        constraints = []
        for c in items:
            constraints.append({
                'type':         c.get('type') or '',
                'constraintAt': c.get('constraintAt'),
                'entityId':     c.get('entityId'),
            })
        return jsonify({'constraints': constraints})
    except Exception as e:
        print(f"[CONSTRAINTS] ERROR for job {job_id}: {e}")
        return jsonify({'constraints': [], 'error': str(e)})

@app.route('/api/debug/categories')
def debug_categories():
    try:
        ninety_days_ago = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%dT00:00:00')
        now_str = datetime.now().strftime('%Y-%m-%dT23:59:59')
        data = bc_get('/jobs', {'StatusModifiedAtFrom': ninety_days_ago, 'StatusModifiedAtTo': now_str, 'pageSize': 1000})
        raw = data if isinstance(data, list) else (data.get('items') or [])
        cats = {}
        statuses = {}
        for j in raw:
            cats[j.get('categoryName','none')] = cats.get(j.get('categoryName','none'), 0) + 1
            statuses[j.get('status','none')] = statuses.get(j.get('status','none'), 0) + 1
        unassigned = [j for j in raw if not j.get('resourceId') and (j.get('status') or '').lower() not in COMPLETED_STATUSES]
        return jsonify({'total': len(raw), 'categories': cats, 'statuses': statuses, 'unassigned_count': len(unassigned)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
