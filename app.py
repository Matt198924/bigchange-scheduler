import os
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
COMPLETED_STATUSES = {'completedok', 'completedwithissues', 'cancelled'}

# How many days ahead of today the schedule window covers.
# 5 => today + the next 5 days (6 days total).
SCHEDULE_DAYS = int(os.environ.get('SCHEDULE_DAYS', '5'))

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

def cache_clear_schedules():
    for k in [k for k in _cache if k.startswith('schedule_')]:
        _cache.pop(k, None)

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
        'desc':            (j.get('description') or 'Job')[:400],
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
        'date':            job_date(j),
    }

def job_date(j):
    """Which day a job belongs to. Planned date wins so a late start
    doesn't shunt a job into the wrong day."""
    ts = j.get('plannedStartAt') or j.get('actualStartAt') or ''
    return ts[:10] if ts else None

def window_dates(days):
    base = datetime.now()
    return [(base + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days + 1)]

def fetch_window(days=SCHEDULE_DAYS):
    """One paged call covering today 00:00 -> today+days 23:59, cached.
    Every schedule endpoint slices this rather than hitting the API itself."""
    key = f'schedule_window_{days}'
    cached = cache_get(key, max_age=120)
    if cached is not None:
        print(f"[WINDOW] Cache hit: {len(cached)} jobs ({days}d)")
        return cached
    start = datetime.now()
    end   = start + timedelta(days=days)
    raw = fetch_paged({
        'plannedAtFrom': start.strftime('%Y-%m-%dT00:00:00'),
        'plannedAtTo':   end.strftime('%Y-%m-%dT23:59:59'),
    })
    print(f"[WINDOW] Fetched {len(raw)} jobs over {days + 1} days")
    cache_set(key, raw)
    return raw

def group_by_engineer(raw_jobs, drop_completed=True):
    by_engineer = {}
    eng_names = {}
    for j in raw_jobs:
        if drop_completed and (j.get('status') or '').lower() in COMPLETED_STATUSES:
            continue
        rid = str(j.get('resourceId') or '')
        if not rid:
            continue
        job = format_job(j)
        by_engineer.setdefault(rid, []).append(job)
        if job['resourceName']:
            eng_names[rid] = job['resourceName']
    for rid in by_engineer:
        by_engineer[rid].sort(key=lambda x: x['startTime'] or '99:99')
    return by_engineer, eng_names

def day_payload(raw_jobs, date_str, is_today):
    # Today keeps completed jobs visible (as before); future days drop them.
    by_engineer, eng_names = group_by_engineer(raw_jobs, drop_completed=not is_today)
    return {
        'date':          date_str,
        'byEngineer':    by_engineer,
        'engNames':      eng_names,
        'jobCount':      sum(len(v) for v in by_engineer.values()),
        'engineerCount': len(by_engineer),
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

@app.route('/api/jobs/unassigned')
def get_unassigned_jobs():
    try:
        cached = cache_get('unassigned_jobs', max_age=120)
        if cached is not None:
            print(f"[UNASSIGNED] Cache hit: {len(cached)} jobs")
            return jsonify({'jobs': cached, 'total': len(cached)})

        all_raw = []
        seen_ids = set()

        # Use a wide date window to catch all unassigned jobs
        # Jobs can sit unassigned for months so look back 180 days
        from_date = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%dT00:00:00')
        to_date   = (datetime.now() + timedelta(days=90)).strftime('%Y-%m-%dT23:59:59')

        for status_val in ['new', 'unscheduled']:
            page = 1
            while page <= 5:  # max 5000 jobs per status
                try:
                    data = bc_get('/jobs', {
                        'StatusModifiedAtFrom': from_date,
                        'StatusModifiedAtTo':   to_date,
                        'status':               status_val,
                        'pageNumber':           page,
                        'pageSize':             1000,
                    })
                    items = data if isinstance(data, list) else (data.get('items') or [])
                    new_items = [j for j in items if j.get('id') not in seen_ids]
                    seen_ids.update(j.get('id') for j in new_items)
                    all_raw.extend(new_items)
                    print(f"[UNASSIGNED] status={status_val} page={page}: {len(items)} jobs")
                    if len(items) < 1000:
                        break
                    page += 1
                except Exception as e:
                    print(f"[UNASSIGNED] status={status_val} page={page} failed: {e}")
                    break

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

@app.route('/api/schedule/upcoming')
def get_upcoming_schedule():
    """Today plus the next SCHEDULE_DAYS days, bucketed by date.
    Override with ?days=N (0-30)."""
    try:
        days = request.args.get('days', type=int)
        days = SCHEDULE_DAYS if days is None else max(0, min(days, 30))

        raw = fetch_window(days)
        dates = window_dates(days)
        today_str = dates[0]

        buckets = {d: [] for d in dates}
        for j in raw:
            d = job_date(j)
            if d in buckets:
                buckets[d].append(j)

        by_date = {}
        all_names = {}
        for d in dates:
            payload = day_payload(buckets[d], d, is_today=(d == today_str))
            all_names.update(payload['engNames'])
            by_date[d] = payload

        return jsonify({
            'days':      days,
            'dates':     dates,
            'today':     today_str,
            'byDate':    by_date,
            'engNames':  all_names,
            'totalJobs': sum(by_date[d]['jobCount'] for d in dates),
        })

    except Exception as e:
        print(f"[UPCOMING] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/schedule/tomorrow')
def get_tomorrow_schedule():
    try:
        raw = fetch_window()
        date_str = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        day_jobs = [j for j in raw if job_date(j) == date_str]
        print(f"[TOMORROW] {len(day_jobs)} jobs for {date_str}")
        return jsonify(day_payload(day_jobs, date_str, is_today=False))
    except Exception as e:
        print(f"[TOMORROW] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/schedule/today')
def get_today_schedule():
    try:
        raw = fetch_window()
        date_str = datetime.now().strftime('%Y-%m-%d')
        day_jobs = [j for j in raw if job_date(j) == date_str]
        print(f"[TODAY] {len(day_jobs)} jobs for {date_str}")
        return jsonify(day_payload(day_jobs, date_str, is_today=True))
    except Exception as e:
        print(f"[TODAY] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/schedule/<date_str>')
def get_schedule_for_date(date_str):
    """Any single day inside the window, YYYY-MM-DD."""
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'date must be YYYY-MM-DD'}), 400
    try:
        raw = fetch_window()
        today_str = datetime.now().strftime('%Y-%m-%d')
        day_jobs = [j for j in raw if job_date(j) == date_str]
        return jsonify(day_payload(day_jobs, date_str, is_today=(date_str == today_str)))
    except Exception as e:
        print(f"[DATE {date_str}] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/jobs/<job_id>/flag')
def get_job_flag(job_id):
    try:
        data = bc_get(f'/jobs/{job_id}/flags')
        return jsonify({'flag': data})
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return jsonify({'flag': None})
        print(f"[FLAG] ERROR for job {job_id}: {e}")
        return jsonify({'flag': None, 'error': str(e)})
    except Exception as e:
        print(f"[FLAG] ERROR for job {job_id}: {e}")
        return jsonify({'flag': None, 'error': str(e)})

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
        cache_clear_schedules()
        result = bc_put(f'/jobs/{job_id}/schedule', payload)
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        print(f"[ASSIGN] ERROR: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
