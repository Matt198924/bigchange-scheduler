import os
import time
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
           'unscheduled': sum(1 for j in counted if j.get('status') == 'unscheduled'), 'otherJobs': len(jobs) - len(counted)}
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
           'months': [{'month': k, 'jobs': v} for k, v in sorted(months.items())], 'engineers': len({j['resourceId'] for j in live})}
    _cache['booked'] = (time.time(), res)
    return jsonify(res)
