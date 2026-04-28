import os
import re
import time
import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from datetime import datetime, timezone, timedelta

app = Flask(__name__, static_folder='static')
CORS(app)

CLIENT_ID     = os.environ.get('BIGCHANGE_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('BIGCHANGE_CLIENT_SECRET', '')
CUSTOMER_ID   = os.environ.get('BIGCHANGE_CUSTOMER_ID', '1564')
API_BASE      = 'https://api.bigchange.com/v1'
TOKEN_URL     = 'https://api.bigchange.com/auth/tokens'

VALID_CATEGORY_IDS = {77961, 82685, 82693, 82694, 82695, 82696, 82697}

JOBWATCH_URL      = 'https://webservice.bigchange.com/v01/services.ashx'
JOBWATCH_KEY      = os.environ.get('JOBWATCH_KEY', '')
JOBWATCH_USERNAME = os.environ.get('JOBWATCH_USERNAME', '')
JOBWATCH_PASSWORD = os.environ.get('JOBWATCH_PASSWORD', '')
COMPLETED_STATUSES = {'completedok', 'completedwithissues', 'cancelled'}

_token_cache = {'token': None, 'expires_at': 0}
_cache = {}

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
    _token_cache['expires_at'] = time.time() + data.get('expires_in', 3600)
    return _token_cache['token']

def cache_get(key, max_age=90):
    entry = _cache.get(key)
    if entry and time.time() - entry['ts'] < max_age:
        return entry['data']
    return None

def cache_set(key, data):
    _cache[key] = {'data': data, 'ts': time.time()}

def bc_get(path, params=None):
    token = get_token()
    url = f'{API_BASE}{path}'
    print(f"[API] GET {url} params={params}")
    resp = requests.get(url, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'customer-id': CUSTOMER_ID,
    }, params=params, timeout=30)
    print(f"[API] {resp.status_code}: {resp.text[:200]}")
    resp.raise_for_status()
    return resp.json()

def bc_put(path, body):
    token = get_token()
    resp = requests.put(f'{API_BASE}{path}', headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'customer-id': CUSTOMER_ID,
    }, json=body, timeout=15)
    print(f"[PUT] {resp.status_code}: {resp.text[:200]}")
    resp.raise_for_status()
    try: return resp.json()
    except: return {}

def fetch_paged(params):
    all_items = []
    page = 1
    while page <= 10:
        data = bc_get('/jobs', {**params, 'pageNumber': page, 'pageSize': 1000})
        items = data if isinstance(data, list) else (data.get('items') or [])
        all_items.extend(items)
        print(f"[PAGED] Page {page}: {len(items)} items")
        if len(items) < 1000:
            break
        page += 1
    return all_items

def is_group_entry(name):
    return bool(re.match(r'^\(\d+\)', name.strip()))

def is_valid_category(job):
    cat_id = job.get('categoryId')
    if cat_id is None:
        return False
    return int(cat_id) in VALID_CATEGORY_IDS

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
        'id':              str(j.get('id', '')),
        'ref':             str(j.get('reference') or j.get('id', '')),
        'desc':            (j.get('description') or 'Job')[:100],
        'client':          j.get('contactName') or j.get('customerName') or '—',
        'area':            j.get('contactAddress') or '—',
        'type':            j.get('typeName') or 'Reactive',
        'status':          j.get('status') or '',
        'category':        j.get('categoryName') or '',
        'resourceId':      str(j.get('resourceId') or ''),
        'resourceName':    j.get('resourceName') or '',
        'startTime':       j.get('actualStartAt') or j.get('plannedStartAt'),
        'endTime':         j.get('actualEndAt') or j.get('plannedEndAt'),
        'actualStart':     j.get('actualStartAt'),
        'statusModifiedAt': j.get('statusModifiedAt'),
        'durationMins':    get_duration_minutes(j),
        'lat':             loc.get('latitude'),
        'lng':             loc.get('longitude'),
    }

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/status')
def api_status():
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
        cached = cache_get('unassigned_jobs', max_age=180)
        if cached is not None:
            print(f"[UNASSIGNED] Cache hit: {len(cached)} jobs")
            return jsonify({'jobs': cached, 'total': len(cached)})

        from_date = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%dT00:00:00')
        to_date   = (datetime.now() + timedelta(days=90)).strftime('%Y-%m-%dT23:59:59')

        all_raw = []
        seen_ids = set()

        for status_val in ['new', 'unscheduled']:
            try:
                data = bc_get('/jobs', {
                    'StatusModifiedAtFrom': from_date,
                    'StatusModifiedAtTo':   to_date,
                    'status':               status_val,
                    'pageSize':             1000,
                })
                # Note: BigChange API doesn't support categoryId filter directly
                # so we filter after fetching
                items = data if isinstance(data, list) else (data.get('items') or [])
                new_items = [j for j in items if j.get('id') not in seen_ids]
                seen_ids.update(j.get('id') for j in new_items)
                all_raw.extend(new_items)
                print(f"[UNASSIGNED] status={status_val}: {len(items)} jobs")
            except Exception as e:
                print(f"[UNASSIGNED] status={status_val} failed: {e}")

        print(f"[UNASSIGNED] Total fetched: {len(all_raw)}")

        jobs = []
        for j in all_raw:
            if j.get('resourceId'): continue
            if not is_valid_category(j): continue
            jobs.append(format_job(j))

        jobs.sort(key=lambda j: -j['durationMins'])
        print(f"[UNASSIGNED] Returning {len(jobs)} jobs")
        cache_set('unassigned_jobs', jobs)
        return jsonify({'jobs': jobs, 'total': len(jobs)})

    except Exception as e:
        print(f"[UNASSIGNED] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/schedule/tomorrow')
def get_tomorrow_schedule():
    try:
        cached = cache_get('schedule_tomorrow', max_age=120)
        if cached:
            return jsonify(cached)

        tomorrow = datetime.now() + timedelta(days=1)
        raw = fetch_paged({
            'plannedAtFrom': tomorrow.strftime('%Y-%m-%dT00:00:00'),
            'plannedAtTo':   tomorrow.strftime('%Y-%m-%dT23:59:59'),
        })
        print(f"[TOMORROW] Got {len(raw)} jobs")

        by_engineer = {}
        eng_names = {}
        for j in raw:
            if (j.get('status') or '').lower() in COMPLETED_STATUSES: continue
            rid = str(j.get('resourceId') or '')
            if not rid: continue
            job = format_job(j)
            by_engineer.setdefault(rid, []).append(job)
            if job['resourceName']:
                eng_names[rid] = job['resourceName']

        for rid in by_engineer:
            by_engineer[rid].sort(key=lambda j: j['startTime'] or '99:99')

        result = {'byEngineer': by_engineer, 'engNames': eng_names, 'date': tomorrow.strftime('%Y-%m-%d')}
        cache_set('schedule_tomorrow', result)
        return jsonify(result)

    except Exception as e:
        print(f"[TOMORROW] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/schedule/today')
def get_today_schedule():
    try:
        cached = cache_get('schedule_today', max_age=30)
        if cached:
            return jsonify(cached)

        today = datetime.now()
        raw = fetch_paged({
            'plannedAtFrom': today.strftime('%Y-%m-%dT00:00:00'),
            'plannedAtTo':   today.strftime('%Y-%m-%dT23:59:59'),
        })
        print(f"[TODAY] Got {len(raw)} jobs")

        by_engineer = {}
        eng_names = {}
        for j in raw:
            rid = str(j.get('resourceId') or '')
            if not rid: continue
            job = format_job(j)
            by_engineer.setdefault(rid, []).append(job)
            if job['resourceName']:
                eng_names[rid] = job['resourceName']

        for rid in by_engineer:
            by_engineer[rid].sort(key=lambda j: j['startTime'] or '99:99')

        result = {'byEngineer': by_engineer, 'engNames': eng_names, 'date': today.strftime('%Y-%m-%d')}
        cache_set('schedule_today', result)
        return jsonify(result)

    except Exception as e:
        print(f"[TODAY] ERROR: {e}")
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
        _cache.pop('unassigned_jobs', None)
        _cache.pop('schedule_today', None)
        _cache.pop('schedule_tomorrow', None)
        result = bc_put(f'/jobs/{job_id}/schedule', payload)
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        print(f"[ASSIGN] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

def jobwatch_get(action, params=None):
    import base64
    creds = base64.b64encode(f'{JOBWATCH_USERNAME}:{JOBWATCH_PASSWORD}'.encode()).decode()
    p = {'action': action, 'Format': 'JSON'}
    if params:
        p.update(params)
    resp = requests.get(JOBWATCH_URL, params=p, headers={
        'Authorization': f'Basic {creds}',
        'key': JOBWATCH_KEY,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()

@app.route('/api/jobs/<job_id>/flags', methods=['GET'])
def get_job_flags(job_id):
    try:
        # Only fetch flags we care about
        TARGET_FLAGS = {'parts received', 'parts with engineer'}
        data = jobwatch_get('JobFlags', {'JobId': job_id})
        flags = []
        for f in (data if isinstance(data, list) else []):
            name = (f.get('name') or f.get('Name') or '').lower().strip()
            if name in TARGET_FLAGS:
                flags.append({
                    'name':    f.get('name') or f.get('Name') or '',
                    'colour':  f.get('colour') or f.get('Colour') or '#888',
                    'comment': f.get('comment') or f.get('Comment') or '',
                })
        return jsonify({'flags': flags})
    except Exception as e:
        print(f"[FLAGS] ERROR for job {job_id}: {e}")
        return jsonify({'flags': [], 'error': str(e)})

@app.route('/api/jobs/<job_id>/constraints', methods=['GET'])
def get_job_constraints(job_id):
    try:
        data  = bc_get(f'/jobs/{job_id}/constraints', {'pageSize': 100})
        items = data if isinstance(data, list) else (data.get('items') or [])
        constraints = [{'type': c.get('type'), 'constraintAt': c.get('constraintAt'), 'entityId': c.get('entityId')} for c in items]
        return jsonify({'constraints': constraints})
    except Exception as e:
        return jsonify({'constraints': [], 'error': str(e)})

@app.route('/api/debug/category-ids')
def get_category_ids():
    try:
        from_date = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%dT00:00:00')
        to_date   = (datetime.now() + timedelta(days=90)).strftime('%Y-%m-%dT23:59:59')
        all_raw = []
        for status_val in ['new', 'unscheduled']:
            try:
                data = bc_get('/jobs', {
                    'StatusModifiedAtFrom': from_date,
                    'StatusModifiedAtTo':   to_date,
                    'status':               status_val,
                    'pageSize':             1000,
                })
                # Note: BigChange API doesn't support categoryId filter directly
                # so we filter after fetching
                items = data if isinstance(data, list) else (data.get('items') or [])
                all_raw.extend(items)
            except: pass
        cats = {}
        for j in all_raw:
            name = j.get('categoryName') or 'none'
            cid  = j.get('categoryId')
            if name not in cats:
                cats[name] = cid
        return jsonify({'categories': cats})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
