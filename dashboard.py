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


@dashboard_bp.route('/api/dashboard/day')
def day():
    """Jobs, engineers with work and the average per engineer for one day (counted categories only)."""
    try: d, a, b = _day(request.args.get('date'))
    except ValueError: return jsonify({'error': 'Bad date'}), 400
    key = f'day-{d}'
    if _cache.get(key) and time.time() - _cache[key][0] < 60: return jsonify({**_cache[key][1], 'totalEngineers': _total_engineers()})
    try: jobs = _jobs(a, b)
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    counted = [j for j in jobs if j.get('categoryId') in COUNT_CATEGORIES]
    done = [j for j in counted if j.get('status') in ('completedOk', 'completedWithIssues')]
    engineers = {j.get('resourceId') for j in counted if j.get('resourceId')}
    is_past = d <= datetime.now(LONDON).date()
    jobs_n = len(done) if is_past else len(counted)
    res = {'date': str(d), 'planned': len(counted), 'completed': len(done), 'withIssues': sum(1 for j in done if j.get('status') == 'completedWithIssues'),
           'engineers': len(engineers), 'basis': 'completed' if is_past else 'planned', 'jobs': jobs_n,
           'average': round(jobs_n / len(engineers), 1) if engineers else 0, 'otherJobs': len(jobs) - len(counted)}
    _cache[key] = (time.time(), res)
    return jsonify({**res, 'totalEngineers': _total_engineers()})


@dashboard_bp.route('/api/dashboard/settings', methods=['PUT'])
def settings():
    b = request.get_json(silent=True) or {}
    try: n = int(b['totalEngineers'])
    except (KeyError, TypeError, ValueError): return jsonify({'error': 'totalEngineers needed'}), 400
    if not 0 < n < 500: return jsonify({'error': 'Check the number'}), 400
    leave.init_db(); leave.set_setting('dash_engineers', str(n))
    return jsonify({'totalEngineers': n})
