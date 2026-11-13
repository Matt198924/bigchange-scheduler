"""Parts demand planner (office only, inside Staff admin).

Syncs the last ~6 months of parts sold (BigChange invoice lines coded to nominal 5000) into our own tables (resumable background job),
then turns that usage into a suggested stock level per van and for the stock room.
"""
import json
import os
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify, request, send_from_directory

import leave

demand_bp = Blueprint('demand', __name__)
DONE = ('completedOk', 'completedWithIssues')
DEFAULT_CFG = {'vans': 0, 'van_cycle_weeks': 2, 'lead_weeks': 2, 'review_weeks': 1, 'service': 95, 'min_jobs': 3, 'room_match': 'stock'}
Z = {90: 1.28, 95: 1.65, 98: 2.05, 99: 2.33}
_lock = threading.Lock()
_sd = {}


def _tables():
    leave.init_db()
    pk = 'SERIAL PRIMARY KEY' if leave.IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    leave.execute('''CREATE TABLE IF NOT EXISTS demand_jobs (job_id TEXT PRIMARY KEY, resource_id TEXT, resource_name TEXT,
        done_at TEXT, lines INTEGER DEFAULT 0)''')
    leave.execute(f'''CREATE TABLE IF NOT EXISTS demand_lines (id {pk}, job_id TEXT, part TEXT, name TEXT, qty REAL,
        done_at TEXT, resource_id TEXT, resource_name TEXT)''')
    for col in ('cost', 'sell'):
        try: leave.execute(f'ALTER TABLE demand_lines ADD COLUMN {col} REAL DEFAULT 0')
        except Exception: pass
    leave.execute('CREATE TABLE IF NOT EXISTS demand_costs (sd_id TEXT PRIMARY KEY, part TEXT, cost REAL, checked TEXT)')
    leave.execute('CREATE TABLE IF NOT EXISTS demand_stock (part TEXT, vehicle_id TEXT, qty REAL)')
    leave.execute('CREATE TABLE IF NOT EXISTS demand_vehicles (vehicle_id TEXT PRIMARY KEY, label TEXT, active INTEGER)')
    leave.execute('CREATE INDEX IF NOT EXISTS demand_lines_part ON demand_lines (part)')


def _items(path, params):
    data = leave.bc_hooks['get'](path, params)
    return data if isinstance(data, list) else (data.get('items') or [])


def _split_model(model):
    """'** vs ** YELLOW PUSH ON FEMALES 6.3MM 613013 (534-553)' -> ('613013 (534-553)', 'YELLOW PUSH ON FEMALES 6.3MM')."""
    m = re.search(r'(\S*\d\S*(?:\s*\([^)]*\))?)\s*$', model or '')
    if not m: return (model or '').strip(), ''
    desc = re.sub(r'^\W*vs\W*', '', (model[:m.start()]).strip(), flags=re.I).strip(' *-')
    return m.group(1).strip(), desc


def _details(sid):
    if sid not in _sd:
        try: _sd[sid] = leave.bc_hooks['get'](f'/stockDetails/{int(sid)}', {}) or {}
        except Exception: _sd[sid] = {}       # deleted or hidden stock record: fall back to what the job line carries
    return _sd[sid]


def _status():
    try: return json.loads(leave.get_setting('demand_sync') or '{}')
    except ValueError: return {}


def _set_status(**kw):
    s = _status(); s.update(kw); leave.set_setting('demand_sync', json.dumps(s))


_nominal = {}


def _nominal_id(code='5000'):
    if code not in _nominal:
        page = 1
        while page <= 20 and code not in _nominal:
            items = _items('/nominalCodes', {'pageSize': 200, 'pageNumber': page})
            for n in items:
                _nominal[str(n.get('code'))] = n.get('id')
            if len(items) < 200: break
            page += 1
    return _nominal.get(code)


_jobres = {}


def _job_resource(jid):
    if jid not in _jobres:
        try:
            j = leave.bc_hooks['get'](f'/jobs/{int(jid)}', {}) or {}
            _jobres[jid] = (str(j.get('resourceId') or ''), j.get('resourceName') or '')
        except Exception:
            _jobres[jid] = ('', '')
    return _jobres[jid]


def _sync_costs():
    """Cost price per part from BigChange stock details (make/model) -> supplier cost. Only parts we have sold are looked up."""
    sold = {r['part'] for r in leave.query('SELECT DISTINCT part FROM demand_lines')}
    page, todo = 1, 0
    while page <= 100:
        items = _items('/stockDetails', {'pageSize': 200, 'pageNumber': page})
        for sd in items:
            sid = str(sd.get('id'))
            part, _ = _split_model((sd.get('model') or '').strip())
            for cand in (part, (sd.get('modelNumber') or '').strip(), (sd.get('stockCode') or '').strip()):
                if cand in sold: part = cand; break
            else: continue
            if leave.query('SELECT 1 FROM demand_costs WHERE sd_id = ?', (sid,)): continue
            try: sup = _items(f'/stockDetails/{int(sid)}/suppliers', {'pageSize': 50})
            except Exception: sup = []
            costs = [float(s['cost']) for s in sup if s.get('cost')]
            leave.execute('INSERT INTO demand_costs (sd_id, part, cost, checked) VALUES (?,?,?,?)', (sid, part, sum(costs) / len(costs) if costs else 0, datetime.now().strftime('%Y-%m-%d')))
            todo += 1
            _set_status(beat=time.time(), msg=f'Reading cost prices ({todo} parts so far)')
            time.sleep(0.1)
        if len(items) < 200: break
        page += 1


def _invoice_lines(inv):
    lines, page = [], 1
    while page <= 10:
        got = []
        for attempt in range(3):
            try: got = _items(f"/finance/invoices/{int(inv['id'])}/lineItems", {'pageSize': 200, 'pageNumber': page}); break
            except Exception: time.sleep(1 + attempt)
        lines += got
        if len(got) < 200: break
        page += 1
    return inv, lines[0:0] + lines


def _sync_stock():
    """Vehicles, then current stock on hand per vehicle (items with no vehicle are held at a contact/site)."""
    leave.execute('DELETE FROM demand_vehicles')
    page = 1
    while page <= 20:
        items = _items('/vehicles', {'pageSize': 200, 'pageNumber': page})
        for v in items:
            label = ' '.join(str(v.get(k) or '') for k in ('reference', 'registration', 'groupName')).strip()
            leave.execute('INSERT INTO demand_vehicles (vehicle_id, label, active) VALUES (?,?,?)',
                          (str(v.get('id')), label, 0 if v.get('isArchived') else 1))
        if len(items) < 200: break
        page += 1
    sdmap = {r['sd_id']: r['part'] for r in leave.query('SELECT sd_id, part FROM demand_costs')}
    rows, page = {}, 1
    while page <= 200:
        _set_status(beat=time.time(), msg=f'Reading current stock (page {page})')
        items = _items('/stockItems', {'isSold': False, 'pageSize': 200, 'pageNumber': page})
        for s in items:
            part = sdmap.get(str(s.get('stockDetailsId')))
            if not part: continue
            try: q = float(s.get('quantity') or 0)
            except (TypeError, ValueError): q = 0
            k = (part, str(s.get('locationVehicleId') or ''))
            rows[k] = rows.get(k, 0) + q
        if len(items) < 200: break
        page += 1
    leave.execute('DELETE FROM demand_stock')
    for (part, vid), q in rows.items():
        leave.execute('INSERT INTO demand_stock (part, vehicle_id, qty) VALUES (?,?,?)', (part, vid, q))


def _sync(days):
    """Parts sold = invoice lines coded to nominal 5000, over the last `days` days."""
    try:
        _tables()
        if leave.get_setting('demand_source') != 'invoice2':      # earlier job-stock data is replaced by invoice data
            leave.execute('DELETE FROM demand_lines'); leave.execute('DELETE FROM demand_jobs'); leave.set_setting('demand_source', 'invoice2')
        nid = _nominal_id('5000')
        if not nid: raise RuntimeError('Nominal code 5000 was not found in BigChange')
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        windows, a = [], start
        while a < end:
            b = min(a + timedelta(days=3), end); windows.append((a, b)); a = b
        _set_status(beat=time.time(), running=True, windowsTotal=len(windows), windowsDone=0, jobsNew=0, error='', msg='Starting...',
                    started=datetime.now().strftime('%d %b %Y %H:%M'))
        new = 0
        for i, (a, b) in enumerate(windows):
            f, t = a.strftime('%Y-%m-%dT%H:%M:%SZ'), b.strftime('%Y-%m-%dT%H:%M:%SZ')
            page, invs = 1, []
            while page <= 30:
                items = _items('/finance/invoices', {'createdAtFrom': f, 'createdAtTo': t, 'pageSize': 200, 'pageNumber': page})
                invs += items
                if len(items) < 200: break
                page += 1
            todo = [inv for inv in invs if not inv.get('cancelledAt') and not leave.query('SELECT 1 FROM demand_jobs WHERE job_id = ?', (str(inv.get('id')),))]
            with ThreadPoolExecutor(max_workers=6) as pool:           # line items are read in parallel, saved one at a time
                fetched = list(pool.map(_invoice_lines, todo))
            for inv, lines in fetched:
                iid = str(inv.get('id'))
                when = (inv.get('createdAt') or f)[:10]
                n, rid, rname = 0, '', ''
                for ln in lines:
                    if ln.get('nominalCodeId') != nid: continue
                    try: qty = float(ln.get('quantity') or 0)
                    except (TypeError, ValueError): qty = 0
                    desc = (ln.get('description') or '').strip()
                    if not qty or not desc: continue
                    part, name = _split_model(desc)
                    if not part: part, name = desc[:60], ''
                    if not rid and inv.get('jobId'): rid, rname = _job_resource(inv['jobId'])
                    try: cost, sell = float(ln.get('unitCost') or 0), float(ln.get('unitSellingPrice') or 0)
                    except (TypeError, ValueError): cost = sell = 0
                    leave.execute('INSERT INTO demand_lines (job_id, part, name, qty, done_at, resource_id, resource_name, cost, sell) VALUES (?,?,?,?,?,?,?,?,?)',
                                  (iid, part, name[:120], qty, when, rid, rname, cost, sell))
                    n += 1
                leave.execute('INSERT INTO demand_jobs (job_id, resource_id, resource_name, done_at, lines) VALUES (?,?,?,?,?)', (iid, rid, rname, when, n))
                new += 1
            _set_status(beat=time.time(), windowsDone=i + 1, jobsNew=new, msg=f'Reading invoices up to {b.strftime("%d %b %Y")}')
        try: _sync_costs()
        except Exception as ex: _set_status(costError=str(ex)[:200])
        try: _sync_stock()
        except Exception as ex: _set_status(stockError=str(ex)[:200])
        _set_status(running=False, msg='Finished', finished=datetime.now().strftime('%d %b %Y %H:%M'), finishedTs=time.time())
    except Exception as ex:
        resp = getattr(ex, 'response', None)
        _set_status(running=False, error=f"{ex}{' - ' + resp.text[:200] if resp is not None else ''}"[:400], msg='Stopped - press Sync to carry on')


def _cfg():
    try: c = json.loads(leave.get_setting('demand_cfg') or '{}')
    except ValueError: c = {}
    return {**DEFAULT_CFG, **{k: c[k] for k in DEFAULT_CFG if k in c}}


def analyse(cfg, days=182):
    """Per part: usage over the window and suggested holdings."""
    _tables()
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    rows = leave.query('SELECT job_id, part, name, qty, done_at, resource_id, cost, sell FROM demand_lines WHERE done_at >= ?', (since,))
    weeks = max(1, days // 7)
    t0 = datetime.strptime(since, '%Y-%m-%d')
    vans_seen = {r['resource_id'] for r in rows if r['resource_id']}
    vrows = leave.query('SELECT vehicle_id, label, active FROM demand_vehicles')
    key = (cfg.get('room_match') or '').strip().lower()
    room_ids = {v['vehicle_id'] for v in vrows if key and key in (v['label'] or '').lower()}
    api_vans = sum(1 for v in vrows if v['active'] and v['vehicle_id'] not in room_ids)
    vans = int(cfg['vans']) or api_vans or max(1, len(vans_seen))
    onvan, onroom = {}, {}
    for s in leave.query('SELECT part, vehicle_id, qty FROM demand_stock'):
        (onvan if s['vehicle_id'] and s['vehicle_id'] not in room_ids else onroom)[s['part']] = (onvan if s['vehicle_id'] and s['vehicle_id'] not in room_ids else onroom).get(s['part'], 0) + float(s['qty'])
    have_stock = bool(onvan or onroom)
    z = Z.get(int(cfg['service']), 1.65)
    parts = {}
    for r in rows:
        p = parts.setdefault(r['part'], {'part': r['part'], 'name': r['name'] or '', 'qty': 0.0, 'jobs': set(), 'eng': set(),
                                         'wk': [0.0] * (weeks + 1), 'mo': [0.0] * 7, 'last': '', 'cq': 0.0, 'cv': 0.0, 'sv': 0.0})
        q = float(r['qty'])
        d = datetime.strptime(r['done_at'], '%Y-%m-%d')
        p['qty'] += q; p['jobs'].add(r['job_id'])
        if q > 0 and (r['cost'] or 0) > 0: p['cq'] += q; p['cv'] += q * r['cost']; p['sv'] += q * (r['sell'] or 0)
        if r['resource_id']: p['eng'].add(r['resource_id'])
        p['wk'][min(weeks, (d - t0).days // 7)] += q
        p['mo'][min(6, (d - t0).days // 30)] += q
        p['last'] = max(p['last'], r['done_at'])
        if not p['name'] and r['name']: p['name'] = r['name']
    cmap = {}
    for r in leave.query('SELECT part, cost FROM demand_costs WHERE cost > 0'): cmap[r['part']] = float(r['cost'])
    out = []
    cyc, lead, rev = float(cfg['van_cycle_weeks']), float(cfg['lead_weeks']), float(cfg['review_weeks'])
    for p in parts.values():
        wk = p['wk'][:weeks]
        m = sum(wk) / weeks
        s = math.sqrt(sum((x - m) ** 2 for x in wk) / weeks)
        jobs = len(p['jobs'])
        if jobs >= int(cfg['min_jobs']):
            mv, sv = m / vans, s / math.sqrt(vans)
            van = max(1, math.ceil(mv * cyc + z * sv * math.sqrt(cyc) - 1e-9))
            kind = 'Van + stock room'
        else:
            van, kind = 0, ('Stock room only' if jobs >= 2 else 'Order on demand')
        horizon = lead + rev
        room = math.ceil(m * horizon + z * s * math.sqrt(horizon) - 1e-9) if jobs >= 2 else 0
        rop = math.ceil(m * lead + z * s * math.sqrt(lead) - 1e-9) if jobs >= 2 else 0
        if jobs >= 2: room, rop = max(room, 1), max(rop, 1)
        if p['qty'] <= 0: continue
        cost = round(cmap[p['part']], 2) if p['part'] in cmap else (round(p['cv'] / p['cq'], 2) if p['cq'] else 0)
        sell = round(p['sv'] / p['cq'], 2) if p['cq'] else 0
        ov, orr = round(onvan.get(p['part'], 0), 2), round(onroom.get(p['part'], 0), 2)
        target = room + van * vans
        order = max(0, math.ceil(target - ov - orr)) if have_stock else 0
        out.append({'onVans': ov, 'onRoom': orr, 'vanTarget': van * vans, 'order': order, 'orderValue': round(order * cost, 2), 'cost': cost, 'sell': sell, 'vanValue': round(van * cost, 2), 'roomValue': round(room * cost, 2), 'totalValue': round((room + van * vans) * cost, 2), 'part': p['part'], 'name': p['name'], 'qty': round(p['qty'], 2), 'jobs': jobs, 'engineers': len(p['eng']),
                    'perWeek': round(m, 2), 'perMonth': round(m * 52 / 12, 1), 'months': [round(x, 1) for x in p['mo'][:6]], 'last': p['last'],
                    'van': van, 'room': room, 'reorder': rop, 'totalHeld': room + van * vans, 'kind': kind})
    out.sort(key=lambda x: -x['qty'])
    tot = {'room': round(sum(x['roomValue'] for x in out), 2), 'perVan': round(sum(x['vanValue'] for x in out), 2), 'allVans': round(sum(x['vanValue'] for x in out) * vans, 2), 'noCost': sum(1 for x in out if not x['cost']), 'order': round(sum(x['orderValue'] for x in out), 2), 'orderLines': sum(1 for x in out if x['order'] > 0), 'haveStock': have_stock}
    return {'totals': tot, 'parts': out, 'vans': vans, 'vansSeen': api_vans or len(vans_seen), 'since': since, 'jobs': len({r['job_id'] for r in rows}), 'cfg': cfg}


@demand_bp.route('/demand')
@demand_bp.route('/demand/')
def demand_page():
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'demand'), 'index.html')


@demand_bp.route('/api/demand')
def demand():
    st = _status()      # keep it fresh: if the last full sync is over 12 hours old, top up the last 14 days in the background
    if st.get('finishedTs') and not st.get('running') and time.time() - st['finishedTs'] > 12 * 3600:
        with _lock:
            _set_status(beat=time.time(), running=True, error='', msg='Topping up...')
            threading.Thread(target=_sync, args=(14,), daemon=True).start()
    return jsonify({**analyse(_cfg()), 'sync': _status()})


@demand_bp.route('/api/demand/config', methods=['PUT'])
def demand_config():
    body = request.get_json(silent=True) or {}
    c = _cfg()
    for k in DEFAULT_CFG:
        if k in body:
            try: c[k] = str(body[k])[:60] if k == 'room_match' else (max(0, float(body[k])) if k != 'service' else int(body[k]))
            except (TypeError, ValueError): pass
    leave.init_db(); leave.set_setting('demand_cfg', json.dumps(c))
    return jsonify({'cfg': c})


@demand_bp.route('/api/demand/sync', methods=['POST'])
def demand_sync():
    with _lock:
        st = _status()
        if st.get('running') and time.time() - float(st.get('beat', 0) or 0) < 600:
            return jsonify({'sync': st})
        _tables()
        _set_status(running=True, error='', msg='Starting...')
        threading.Thread(target=_sync, args=(183,), daemon=True).start()
    return jsonify({'sync': _status()})


@demand_bp.route('/api/demand/status')
def demand_status():
    return jsonify({'sync': _status()})
