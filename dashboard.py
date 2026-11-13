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


def _jobs(start, end, statuses):
    """Jobs planned within [start, end) with the given statuses, all pages."""
    out, page = [], 1
    while page <= 20:
        data = leave.bc_hooks['get']('/jobs', {'plannedAtFrom': start.strftime('%Y-%m-%dT%H:%M:%SZ'), 'plannedAtTo': end.strftime('%Y-%m-%dT%H:%M:%SZ'),
                                              'status': statuses, 'pageSize': 1000, 'pageNumber': page})
        items = data if isinstance(data, list) else (data.get('items') or [])
        out += items
        if len(items) < 1000: break
        page += 1
    return out


@dashboard_bp.route('/dashboard')
@dashboard_bp.route('/dashboard/')
def page(): return send_from_directory(PAGE_DIR, 'index.html')


@dashboard_bp.route('/api/dashboard/completed')
def completed():
    """Jobs completed (OK or with issues) on a day, counting only the chosen categories."""
    try: d, a, b = _day(request.args.get('date'))
    except ValueError: return jsonify({'error': 'Bad date'}), 400
    key = f'completed-{d}'
    if _cache.get(key) and time.time() - _cache[key][0] < 60: return jsonify(_cache[key][1])
    try: jobs = _jobs(a, b, ['completedOk', 'completedWithIssues'])
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    counted = [j for j in jobs if j.get('categoryId') in COUNT_CATEGORIES]
    res = {'date': str(d), 'completed': len(counted), 'allCompleted': len(jobs), 'excluded': len(jobs) - len(counted),
           'withIssues': sum(1 for j in counted if j.get('status') == 'completedWithIssues')}
    _cache[key] = (time.time(), res)
    return jsonify(res)
