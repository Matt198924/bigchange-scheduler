import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Blueprint, jsonify, request, send_from_directory
import leave

dashboard_bp = Blueprint('dashboard', __name__)
PAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'dashboard')
LONDON = ZoneInfo('Europe/London')
# Only jobs in these categories count towards "jobs completed" (completed OK or completed with issues).
COUNT_CATEGORIES = {142643, 82693, 82694, 142645, 82695, 142646}
_cache = {}


def _by_category(jobs, limit=400):
    """Group jobs by category name with a short job list each (for the expandable breakdowns)."""
    cats = {}
    for j in jobs:
        c = cats.setdefault(j.get('categoryId'), {'id': j.get('categoryId'), 'name': j.get('categoryName') or f"Category {j.get('categoryId')}", 'count': 0, 'jobs': []})
        c['count'] += 1
        if len(c['jobs']) < limit:
            c['jobs'].append({'ref': str(j.get('reference') or j.get('id') or ''), 'client': j.get('contactName') or '', 'desc': (j.get('description') or '')[:90],
                              'engineer': j.get('resourceName') or '', 'date': (j.get('plannedStartAt') or '')[:10], 'status': j.get('status') or ''})
    for c in cats.values(): c['jobs'].sort(key=lambda x: x['date'] or '9')
    return sorted(cats.values(), key=lambda c: -c['count'])


def _day(date_str=None):
    d = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else datetime.now(LONDON).date()
    a = datetime(d.year, d.month, d.day, tzinfo=LONDON)
    return d, a.astimezone(timezone.utc), (a + timedelta(days=1)).astimezone(timezone.utc)


def _jobs(start, end, statuses=None):
    """Jobs planned within [start, end), all pages (cancelled jobs are left out by BigChange unless asked for)."""
    out, page = [], 1
    while page <= 20:
        p = {'plannedAtFrom': start.strftime('%Y-%m-%dT%H:%M:%SZ'), 'plannedAtTo': end.strftime('%Y-%m-%dT%H:%M:%SZ'), 'pageSize': 1000, 'pageNumber': page}
        if statuses: p['status'] = statuses
        data = leave.bc_hooks['get']('/jobs', p)
        items = data if isinstance(data, list) else (data.get('items') or [])
        out += items
        if len(items) < 1000: break
        page += 1
    return out


def _total_engineers():
    try: return int(float(leave.get_setting('dash_engineers') or 44))
    except ValueError: return 44


@dashboard_bp.route('/dashboard')
@dashboard_bp.route('/dashboard/')
def page(): return send_from_directory(PAGE_DIR, 'index.html')


def _stats(date_str):
    d, a, b = _day(date_str)
    key = f'day-{d}'
    if _cache.get(key) and time.time() - _cache[key][0] < 60: return {**_cache[key][1], 'totalEngineers': _total_engineers()}
    jobs = _jobs(a, b)
    counted = [j for j in jobs if j.get('categoryId') in COUNT_CATEGORIES]
    done = [j for j in counted if j.get('status') in ('completedOk', 'completedWithIssues')]
    engineers = {j.get('resourceId') for j in counted if j.get('resourceId')}
    is_past = d <= datetime.now(LONDON).date()
    jobs_n = len(done) if is_past else len(counted)
    res = {'date': str(d), 'planned': len(counted), 'completed': len(done), 'withIssues': sum(1 for j in done if j.get('status') == 'completedWithIssues'),
           'engineers': len(engineers), 'basis': 'completed' if is_past else 'planned', 'jobs': jobs_n,
           'average': round(jobs_n / len(engineers), 1) if engineers else 0, 'otherJobs': len(jobs) - len(counted)}
    _cache[key] = (time.time(), res)
    return {**res, 'totalEngineers': _total_engineers()}


@dashboard_bp.route('/api/dashboard/day')
def day():
    """Jobs, engineers with work and the average per engineer for one day (counted categories only)."""
    try: return jsonify(_stats(request.args.get('date')))
    except ValueError: return jsonify({'error': 'Bad date'}), 400
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502


@dashboard_bp.route('/api/dashboard/next')
def next_days():
    """The next working day(s) to plan for: tomorrow, or on a Friday the weekend days then Monday (Saturday and Sunday ask for Sunday/Monday and Monday)."""
    today = datetime.now(LONDON).date()
    n = {4: 3, 5: 2, 6: 1}.get(today.weekday(), 1)       # Fri: Sat, Sun, Mon. Sat: Sun, Mon. Sun: Mon. Otherwise tomorrow.
    try: return jsonify({'days': [_stats(str(today + timedelta(days=i))) for i in range(1, n + 1)]})
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502


@dashboard_bp.route('/api/dashboard/settings', methods=['PUT'])
def settings():
    b = request.get_json(silent=True) or {}
    try: n = int(b['totalEngineers'])
    except (KeyError, TypeError, ValueError): return jsonify({'error': 'totalEngineers needed'}), 400
    if not 0 < n < 500: return jsonify({'error': 'Check the number'}), 400
    leave.init_db(); leave.set_setting('dash_engineers', str(n))
    return jsonify({'totalEngineers': n})


@dashboard_bp.route('/api/dashboard/unallocated')
def unallocated():
    """Jobs with no engineer booked yet (status new or unscheduled), counted in the chosen categories."""
    if _cache.get('unalloc') and time.time() - _cache['unalloc'][0] < 120: return jsonify(_cache['unalloc'][1])
    now = datetime.now()
    # BigChange rejects very wide date ranges, so look back about 6 months in two slices (plus the next 90 days)
    slices, end = [], now + timedelta(days=90)
    for _ in range(2):
        start = end - timedelta(days=135)
        slices.append((start.strftime('%Y-%m-%dT00:00:00'), end.strftime('%Y-%m-%dT23:59:59'))); end = start
    seen, jobs = set(), []
    try:
        for f, t in slices:
            for st in ('new', 'unscheduled'):
                page = 1
                while page <= 10:
                    data = leave.bc_hooks['get']('/jobs', {'statusModifiedAtFrom': f, 'statusModifiedAtTo': t, 'status': st, 'pageSize': 1000, 'pageNumber': page})
                    items = data if isinstance(data, list) else (data.get('items') or [])
                    for j in items:
                        if j.get('id') not in seen: seen.add(j.get('id')); jobs.append(j)
                    if len(items) < 1000: break
                    page += 1
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    counted = [j for j in jobs if j.get('categoryId') in COUNT_CATEGORIES]
    res = {'unallocated': len(counted), 'new': sum(1 for j in counted if j.get('status') == 'new'),
           'unscheduled': sum(1 for j in counted if j.get('status') == 'unscheduled'), 'otherJobs': len(jobs) - len(counted), 'categories': _by_category(counted)}
    _cache['unalloc'] = (time.time(), res)
    return jsonify(res)


@dashboard_bp.route('/api/dashboard/booked')
def booked():
    """Allocated work still to be done over the next 3 months: planned, with an engineer, not completed or cancelled, in the counted categories."""
    if _cache.get('booked') and time.time() - _cache['booked'][0] < 300: return jsonify(_cache['booked'][1])
    today = datetime.now(LONDON).date()
    jobs, seen = [], set()
    try:
        for i in range(3):       # three slices of 31 days (BigChange rejects very wide ranges)
            _, a, _b = _day(str(today + timedelta(days=31 * i)))
            _, _a, b = _day(str(today + timedelta(days=31 * i + 30)))
            for j in _jobs(a, b):
                if j.get('id') not in seen: seen.add(j.get('id')); jobs.append(j)
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    live = [j for j in jobs if j.get('categoryId') in COUNT_CATEGORIES and j.get('resourceId') and j.get('status') not in ('completedOk', 'completedWithIssues', 'cancelled')]
    months = {}
    for j in live:
        try: m = datetime.fromisoformat((j.get('plannedStartAt') or '')[:19]).strftime('%Y-%m')
        except ValueError: continue
        months[m] = months.get(m, 0) + 1
    res = {'total': len(live), 'from': str(today), 'to': str(today + timedelta(days=92)),
           'months': [{'month': k, 'jobs': v} for k, v in sorted(months.items())], 'engineers': len({j['resourceId'] for j in live}), 'categories': _by_category(live)}
    _cache['booked'] = (time.time(), res)
    return jsonify(res)


# ── Flags (pink / black / yellow) on jobs that are not financially complete ─────────────────────
_flag_lock = threading.Lock()
COLOURS = ('pink', 'black', 'yellow')
DEFAULT_FLAGS = {'pink': [10121], 'black': [210481], 'yellow': [10122]}     # R&S flag ids, used until changed under Flag setup


def _jget(key, default):
    try: return json.loads(leave.get_setting(key) or '') or default
    except ValueError: return default


def _job_flags():
    if _cache.get('flaglist') and time.time() - _cache['flaglist'][0] < 600: return _cache['flaglist'][1]
    out, page = [], 1
    while page <= 10:
        data = leave.bc_hooks['get']('/flags', {'entityType': 'job', 'pageSize': 200, 'pageNumber': page})
        items = data if isinstance(data, list) else (data.get('items') or [])
        out += items
        if len(items) < 200: break
        page += 1
    _cache['flaglist'] = (time.time(), out)
    return out


def _flag_map(flags):
    """Which flag ids count as pink / black / yellow: saved choice if there is one, else matched on the flag's colour or name."""
    saved = _jget('dash_flagmap', {})
    m = {}
    for c in COLOURS:
        if saved.get(c) is not None: m[c] = [int(x) for x in saved[c]]
        else: m[c] = DEFAULT_FLAGS[c]
    return m


def _get_retry(path, params=None, tries=5):
    for n in range(tries):
        try: return leave.bc_hooks['get'](path, params or {})
        except Exception as ex:
            if '429' not in str(ex) or n == tries - 1: raise
            time.sleep(2 + n * 3)


def _flag_count():
    st = lambda **kw: leave.set_setting('dash_flagcount', json.dumps({**_jget('dash_flagcount', {}), **kw}))
    try:
        today = datetime.now(timezone.utc)
        jobs, seen = [], set()
        for i in range(12):        # open-ended search by creation date, 30-day slices over the last year
            end = today - timedelta(days=30 * i); start = end - timedelta(days=30)
            page = 1
            while page <= 10:
                data = _get_retry('/jobs', {'createdAtFrom': start.strftime('%Y-%m-%dT%H:%M:%SZ'), 'createdAtTo': end.strftime('%Y-%m-%dT%H:%M:%SZ'), 'pageSize': 1000, 'pageNumber': page})
                items = data if isinstance(data, list) else (data.get('items') or [])
                for j in items:
                    if j.get('id') not in seen and not j.get('isFinanciallyComplete'): seen.add(j.get('id')); jobs.append(j)
                if len(items) < 1000: break
                page += 1
            st(beat=time.time(), running=True, msg=f'Finding jobs ({i + 1} of 12)')
        counts, done = {}, 0

        def one(j):
            data = _get_retry(f"/jobs/{j['id']}/flags")
            return data if isinstance(data, list) else (data.get('items') or [])
        with ThreadPoolExecutor(max_workers=4) as pool:
            for flags in pool.map(one, jobs):
                for f in flags: counts[str(f.get('flagId'))] = counts.get(str(f.get('flagId')), 0) + 1
                done += 1
                if done % 50 == 0: st(beat=time.time(), running=True, done=done, total=len(jobs), msg=f'Checking flags on {done} of {len(jobs)} jobs')
        day = datetime.now(LONDON).strftime('%Y-%m-%d')
        snaps = _jget('dash_flagsnaps', {})
        snaps.setdefault(day, counts)
        for k in sorted(snaps)[:-70]: snaps.pop(k, None)
        leave.set_setting('dash_flagsnaps', json.dumps(snaps))
        st(running=False, counts=counts, jobs=len(jobs), done=len(jobs), total=len(jobs), error='', msg='Finished', finishedTs=time.time())
    except Exception as ex:
        st(running=False, error=str(ex)[:300], msg='Stopped - will retry')


def _by_colour(counts, fmap):
    return {c: sum(counts.get(str(i), 0) for i in fmap[c]) for c in COLOURS}


@dashboard_bp.route('/api/dashboard/flags')
def flags():
    leave.init_db()
    with _flag_lock:
        s = _jget('dash_flagcount', {})
        stale = s.get('running') and time.time() - float(s.get('beat') or 0) > 600
        due = not s.get('running') and (not s.get('finishedTs') or time.time() - float(s['finishedTs']) > 3600) and time.time() - float(s.get('lastTry') or 0) > 300
        if stale or due:
            leave.set_setting('dash_flagcount', json.dumps({**s, 'running': True, 'beat': time.time(), 'lastTry': time.time(), 'msg': 'Starting...', 'error': ''}))
            threading.Thread(target=_flag_count, daemon=True).start()
            s = _jget('dash_flagcount', {})
    try: fl = _job_flags()
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    fmap = _flag_map(fl)
    snaps = _jget('dash_flagsnaps', {})
    today = datetime.now(LONDON).date()
    monday = str(today - timedelta(days=today.weekday()))
    day_s = snaps.get(str(today))
    week_s = next((snaps[k] for k in sorted(snaps) if k >= monday), None)
    return jsonify({'state': {k: s.get(k) for k in ('running', 'msg', 'error', 'done', 'total', 'jobs', 'finishedTs')},
                    'now': _by_colour(s.get('counts') or {}, fmap) if s.get('counts') else None,
                    'startDay': _by_colour(day_s, fmap) if day_s else None, 'startWeek': _by_colour(week_s, fmap) if week_s else None,
                    'map': fmap, 'names': {c: [(next((f.get('name') for f in fl if f['id'] == i), None) or f'Flag {i}') for i in fmap[c]] for c in COLOURS}, 'flags': [{'id': f['id'], 'name': f.get('name'), 'color': f.get('color'), 'count': (s.get('counts') or {}).get(str(f['id']), 0)} for f in fl]})


@dashboard_bp.route('/api/dashboard/flags/config', methods=['PUT'])
def flags_config():
    b = request.get_json(silent=True) or {}
    try: m = {c: [int(x) for x in b.get(c, [])] for c in COLOURS}
    except (TypeError, ValueError): return jsonify({'error': 'Flag ids must be numbers'}), 400
    leave.init_db(); leave.set_setting('dash_flagmap', json.dumps(m))
    return jsonify({'map': m})


@dashboard_bp.route('/api/dashboard/flags/recount', methods=['POST'])
def flags_recount():
    with _flag_lock:
        s = _jget('dash_flagcount', {})
        if s.get('running') and time.time() - float(s.get('beat') or 0) < 600: return jsonify({'ok': True})
        leave.set_setting('dash_flagcount', json.dumps({**s, 'running': True, 'beat': time.time(), 'lastTry': time.time(), 'msg': 'Starting...', 'error': ''}))
        threading.Thread(target=_flag_count, daemon=True).start()
    return jsonify({'ok': True})
