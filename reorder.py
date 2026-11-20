"""Stock reorder levels (BigChange web service), as its own hub tool at /reorder/.

Reads every reorder level, grouped by location (van or contact), shows current quantities, lets the office amend a level
and add one part to every van. Talks to BigChange's older web service (services.ashx); current quantities come from the REST API.
"""
import os
import re
import time
import requests
from flask import Blueprint, jsonify, request, send_from_directory

import leave

reorder_bp = Blueprint('reorder', __name__)
WS_URL = os.environ.get('BIGCHANGE_WS_URL', 'https://webservice.bigchange.com/v01/services.ashx')
PAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'reorder')
ETYPE = {1: 'Contact', 2: 'Asset'}
_cache = {}


def _ws(action, params=None):
    user, pw, key = os.environ.get('BIGCHANGE_WS_USER', ''), os.environ.get('BIGCHANGE_WS_PASSWORD', ''), os.environ.get('BIGCHANGE_WS_KEY', '')
    if not (user and pw and key):
        raise RuntimeError('The older BigChange web service needs BIGCHANGE_WS_USER, BIGCHANGE_WS_PASSWORD and BIGCHANGE_WS_KEY set on Render')
    q = {'action': action, **(params or {})}
    r = requests.get(WS_URL, params=q, auth=(user, pw), headers={'key': key}, timeout=40)
    print(f'[REORDER] {action} {r.status_code}: {r.text[:200]}')
    try: body = r.json()
    except ValueError: body = {'text': r.text[:500]}
    if not r.ok: raise RuntimeError(f'BigChange {r.status_code}: {r.text[:300]}')
    return body


def _rest(path, params=None):
    return leave.bc_hooks['get'](path, params or {})


def _items(path, params):
    data = _rest(path, params)
    return data if isinstance(data, list) else (data.get('items') or [])


def _rows(body):
    """Result rows from a web service response, whatever wrapper it uses."""
    if isinstance(body, list): return body
    if isinstance(body, dict):
        for k in ('Result', 'result', 'Results', 'Items', 'items'):
            if isinstance(body.get(k), list): return body[k]
    return []


def _pick(row, *names):
    low = {str(k).lower(): v for k, v in row.items()}
    for n in names:
        if n in low and low[n] not in (None, ''): return low[n]
    return None


def _find(row, *needles, exclude=()):
    """First value whose key (lower-cased) contains one of the needles."""
    for k, v in row.items():
        kl = str(k).lower()
        if any(n in kl for n in needles) and not any(x in kl for x in exclude) and v not in (None, ''): return k, v
    return None, None


def _levels():
    if _cache.get('levels') and time.time() - _cache['levels'][0] < 45: return _cache['levels'][1]
    body = _ws('ListStockReorderLevel')
    rows_in = _rows(body)
    if not rows_in:      # some accounts only answer when asked about one van at a time
        from concurrent.futures import ThreadPoolExecutor
        def one(vid):
            try: return _rows(_ws('ListStockReorderLevel', {'EntityType': 2, 'EntityId': vid}))
            except Exception: return []
        try: ids = list(_vehicles().keys())
        except Exception: ids = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            for res in pool.map(one, ids): rows_in += res
    out = []
    for r in rows_in:
        if not isinstance(r, dict): continue
        try:
            _, sid = _find(r, 'stockid')
            if sid is None: _, sid = _find(r, 'stockdetailsid', 'stockitemid')
            _, qty = _find(r, 'quantity', 'level')
            _, eid = _find(r, 'entityid')
            _, et = _find(r, 'entitytype')
            if eid is None:      # older shape: separate vehicle / contact id fields
                _, vid = _find(r, 'vehicle', 'asset', exclude=('name', 'registration'))
                _, cid = _find(r, 'contact', exclude=('name',))
                if vid not in (None, '', 0) and int(vid): et, eid = 'asset', vid
                elif cid not in (None, '', 0) and int(cid): et, eid = 'contact', cid
            t = str(et).strip().lower()
            etype = 1 if (t == '1' or t.startswith('contact')) else 2 if (t == '2' or t.startswith(('asset', 'vehicle'))) else 0
            if not etype or eid is None: continue
            eid = int(eid)
            sid, qty = int(sid), float(qty or 0)
        except (TypeError, ValueError): continue
        _, make = _find(r, 'make')
        _, model = _find(r, 'model')
        _, num = _find(r, 'number')
        _, ename = _find(r, 'registration', 'vehiclename', 'assetname', 'contactname', 'locationname')
        _, rid = _find(r, 'reorderid', 'stockreorderid')
        out.append({'reorderId': rid, 'stockId': sid, 'entityType': etype, 'entityId': eid, 'level': qty, 'rawType': str(et),
                    'name': ' '.join(str(x) for x in (make, model, num) if x).strip(), 'entityName': ename or ''})
    _cache['levels'] = (time.time(), (out, body))
    return out, body


def _contact_names(ids):
    from concurrent.futures import ThreadPoolExecutor
    cache = _cache.setdefault('cn', {})
    todo = [i for i in ids if i not in cache]
    def one(i):
        try:
            c = _rest(f'/contacts/{int(i)}', {}) or {}
            return i, c.get('name') or c.get('contactName') or ''
        except Exception: return i, ''
    if todo:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for i, n in pool.map(one, todo[:300]): cache[i] = n
    return cache


def _asset_names(ids):
    """Asset id -> registration, from the web service (asset groups, then one by one for any left)."""
    from concurrent.futures import ThreadPoolExecutor
    cache = _cache.setdefault('an', {})
    everything = ids is None
    miss = [i for i in (ids or []) if i not in cache]
    if not miss and not everything: return cache
    def reg_of(row):
        _, reg = _find(row, 'registration', 'reg')
        if not reg: _, reg = _find(row, 'assetname', 'name', 'reference')
        return str(reg).strip() if reg else ''
    try:
        for g in _rows(_ws('AssetGroups')):
            if not isinstance(g, dict): continue
            _, gid = _find(g, 'assetgroupid', 'groupid', 'id')
            if gid is None: continue
            for a in _rows(_ws('Assets', {'AssetGroupId': gid})):
                if not isinstance(a, dict): continue
                _, aid = _find(a, 'assetid', 'id')
                try: aid = int(aid)
                except (TypeError, ValueError): continue
                if reg_of(a): cache[aid] = reg_of(a)
    except Exception as ex: print(f'[REORDER] asset groups unavailable: {ex}')
    left = [i for i in (miss or []) if i not in cache][:150]
    def one(i):
        try:
            rows = _rows(_ws('Asset', {'AssetId': i}))
            return i, reg_of(rows[0]) if rows and isinstance(rows[0], dict) else ''
        except Exception: return i, ''
    if left:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for i, n in pool.map(one, left):
                if n: cache[i] = n
    return cache


def _vans():
    """Every van: BigChange assets (the ids reorder levels are held against) plus any REST vehicles."""
    if _cache.get('vans') and time.time() - _cache['vans'][0] < 600: return _cache['vans'][1]
    out = {}
    try: out.update(_vehicles())
    except Exception as ex: print(f'[REORDER] vehicles unavailable: {ex}')
    try: out.update(_asset_names(None))
    except Exception as ex: print(f'[REORDER] assets unavailable: {ex}')
    _cache['vans'] = (time.time(), out)
    return out


def _vehicles():
    if _cache.get('veh') and time.time() - _cache['veh'][0] < 600: return _cache['veh'][1]
    out, page = {}, 1
    while page <= 20:
        items = _items('/vehicles', {'pageSize': 200, 'pageNumber': page})
        for v in items:
            if not v.get('isArchived'): out[int(v['id'])] = (v.get('registration') or v.get('reference') or str(v['id']))
        if len(items) < 200: break
        page += 1
    _cache['veh'] = (time.time(), out)
    return out


@reorder_bp.route('/reorder')
@reorder_bp.route('/reorder/')
def page(): return send_from_directory(PAGE_DIR, 'index.html')


@reorder_bp.route('/api/reorder/lookup', methods=['POST'])
def lookup_ids():
    """Look up pasted stock IDs one at a time and show exactly what BigChange says, so naming problems can be diagnosed."""
    import json as _j
    ids = [int(x) for x in re.findall(r'\d+', (request.get_json(silent=True) or {}).get('ids', ''))][:15]
    cat = {}
    try: cat = {s['id']: s['text'] for s in _stock_catalog()}
    except Exception as ex: cat_err = str(ex)[:200]
    out = []
    for sid in ids:
        r = {'id': sid, 'inCatalog': cat.get(sid) or None}
        for label, path, params in (('/stockDetails/{id}', f'/stockDetails/{sid}', {}), ('/stockDetails?id=', '/stockDetails', {'id': sid})):
            try:
                d = _rest(path, params)
                r[label] = _j.dumps(d)[:400]
            except Exception as ex: r[label] = 'ERROR: ' + str(ex)[:250]
        out.append(r)
    return jsonify({'results': out})


@reorder_bp.route('/api/reorder/check')
def check():
    """Raw answers from BigChange. With ?entityId= also tries asking for one van in several ways."""
    import json as _j
    variants = [('no filter', {})]
    eid = (request.args.get('entityId') or '').strip()
    if eid.isdigit():
        variants += [('EntityType=2 + EntityId', {'EntityType': 2, 'EntityId': eid}), ('EntityId only', {'EntityId': eid}),
                     ('AssetId', {'AssetId': eid}), ('EntityType=2 + EntityId + StockId=0', {'EntityType': 2, 'EntityId': eid, 'StockId': 0})]
    sid = (request.args.get('stockId') or '').strip()
    if sid.isdigit():
        variants += [('StockId only', {'StockId': sid})]
        if eid.isdigit(): variants += [('EntityType=2 + EntityId + StockId', {'EntityType': 2, 'EntityId': eid, 'StockId': sid})]
    raw_q = (request.args.get('q') or '').strip()
    if raw_q:      # free-form test, e.g. StockId=348050&EntityType=2&EntityId=233377
        from urllib.parse import parse_qsl
        variants = [('your own: ' + raw_q, dict(parse_qsl(raw_q)))] + variants
    out = []
    for name, p in variants:
        try:
            body = _ws(request.args.get('action') or 'ListStockReorderLevel', p)
            rr = _rows(body)
            out.append({'try': name, 'count': len(rr), 'firstRow': rr[0] if rr else None, 'raw': _j.dumps(body)[:300]})
        except Exception as ex:
            out.append({'try': name, 'error': str(ex)[:400]})
    try:      # control: a simple, unrelated call, to tell 'sign-in not accepted' from 'this call wants other parameters'
        body = _ws('ping')
        out.append({'try': 'CONTROL: ping (sign-in test)', 'count': len(_rows(body)), 'raw': _j.dumps(body)[:300]})
    except Exception as ex:
        out.append({'try': 'CONTROL: ping (sign-in test)', 'error': str(ex)[:300]})
    return jsonify({'results': out})


def _lookup_names(ids, limit=40):
    """Look up individual stock IDs the catalog doesn't name. Found names are saved so each ID is only asked once."""
    import json
    try: known = json.loads(leave.get_setting('stock_names_v1') or '{}')
    except Exception: known = {}
    got, changed = {}, False
    for sid in ids:
        if str(sid) in known: got[sid] = known[str(sid)]; continue
        if limit <= 0: continue
        limit -= 1
        try:
            d = leave.bc_hooks['get'](f'/stockDetails/{sid}', {})
            d = d[0] if isinstance(d, list) and d else d
            t = ' '.join(str(x) for x in (d.get('make'), d.get('model'), d.get('modelNumber')) if x).strip() if isinstance(d, dict) else ''
        except Exception as ex:
            print('[LOOKUP] stock', sid, 'failed:', str(ex)[:150]); break    # stop on the first failure (404 / 429): don't burn quota
        if t: known[str(sid)] = t; got[sid] = t; changed = True
    if changed:
        try: leave.set_setting('stock_names_v1', json.dumps(known, separators=(',', ':')))
        except Exception: pass
    return got

@reorder_bp.route('/api/reorder/levels')
def levels():
    try: rows, _ = _levels()
    except Exception as ex: return jsonify({'error': str(ex)[:500]}), 502
    try: veh = _vans()
    except Exception: veh = {}
    cn = {}
    try: cn = _contact_names(sorted({r['entityId'] for r in rows if r['entityType'] == 1}))
    except Exception: pass
    an = {}
    try: an = _asset_names(sorted({r['entityId'] for r in rows if r['entityType'] == 2}))
    except Exception: pass
    locs = {}
    for r in rows:
        k = (r['entityType'], r['entityId'])
        L = locs.setdefault(k, {'entityType': r['entityType'], 'entityId': r['entityId'], 'kind': ETYPE.get(r['entityType'], 'Location'), 'items': [], 'label': ''})
        L['label'] = L['label'] or r['entityName'] or (an.get(r['entityId']) or veh.get(r['entityId']) if r['entityType'] == 2 else cn.get(r['entityId'])) or f"{ETYPE.get(r['entityType'], 'Location')} {r['entityId']}"
        L['items'].append({'stockId': r['stockId'], 'name': r['name'] or f"Stock {r['stockId']}", 'level': r['level'], 'reorderId': r['reorderId']})
    # Name only the parts that are in reorder levels: use any catalog copy we already hold (never fetch the whole stock list here),
    # then look up the rest one by one and remember them.
    cat = (_cache.get('cat') or (0, None))[1] or _saved_catalog()
    names = {s['id']: s['text'] for s in cat}
    for L in locs.values():
        for i in L['items']:
            if names.get(i['stockId']): i['name'] = names[i['stockId']]
    missing = sorted({i['stockId'] for L in locs.values() for i in L['items'] if i['name'].startswith('Stock ')})
    if missing:
        found = _lookup_names(missing, limit=500)
        for L in locs.values():
            for i in L['items']:
                if found.get(i['stockId']): i['name'] = found[i['stockId']]
    for vid, vname in veh.items():   # vans with no reorder levels yet still get listed so parts can be added
        if (2, vid) not in locs and 'stock' not in vname.lower():
            locs[(2, vid)] = {'entityType': 2, 'entityId': vid, 'kind': 'Asset', 'items': [], 'label': vname}
    out = sorted(locs.values(), key=lambda x: (x['entityType'], x['label'].lower()))
    for L in out: L['items'].sort(key=lambda i: i['name'].lower())
    return jsonify({'locations': out, 'total': len(rows)})


@reorder_bp.route('/api/reorder/quantities')
def quantities():
    """Current quantity per stock id at one location (REST stock items)."""
    try: et, eid = int(request.args.get('entityType')), int(request.args.get('entityId'))
    except (TypeError, ValueError): return jsonify({'error': 'entityType and entityId needed'}), 400
    key = 'locationVehicleId' if et == 2 else 'locationContactId'
    qty, page = {}, 1
    try:
        while page <= 50:
            items = _items('/stockItems', {key: eid, 'isSold': False, 'pageSize': 200, 'pageNumber': page})
            for s in items:
                try: qty[str(s.get('stockDetailsId'))] = qty.get(str(s.get('stockDetailsId')), 0) + float(s.get('quantity') or 0)
                except (TypeError, ValueError): pass
            if len(items) < 200: break
            page += 1
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    return jsonify({'quantities': qty})


def _split_model(model):
    """'** vs ** YELLOW PUSH ON FEMALES 6.3MM 613013 (534-553)' -> ('613013 (534-553)', 'YELLOW PUSH ON FEMALES 6.3MM')."""
    m = re.search(r'(\S*\d\S*(?:\s*\([^)]*\))?)\s*$', model or '')
    if not m: return (model or '').strip(), ''
    desc = re.sub(r'^\W*vs\W*', '', (model[:m.start()]).strip(), flags=re.I).strip(' *-')
    return m.group(1).strip(), desc


def _items_retry(path, params, tries=6):
    """Like _items, but waits and retries when BigChange says 429 (too many requests)."""
    for n in range(tries):
        try: return _items(path, params)
        except Exception as ex:
            if '429' not in str(ex) or n == tries - 1: raise
            time.sleep(2 + n * 3)


def _saved_catalog():
    """Last full catalog kept in the database, so part names still show when BigChange's API is unavailable or over quota."""
    try:
        import json
        raw = leave.get_setting('stock_catalog_v1')
        return json.loads(raw) if raw else []
    except Exception: return []

def _stock_catalog():
    """All stock records, cached for an hour. Pages are fetched gently and kept, so a rate-limit stop resumes where it left off."""
    if _cache.get('cat') and time.time() - _cache['cat'][0] < 3600: return _cache['cat'][1]
    if not _cache.get('cat'):
        saved = _saved_catalog()
        if saved: _cache['cat'] = (time.time() - 3000, saved)   # use the saved copy now; refresh again in ~10 min
        if saved: return saved
    st = _cache.setdefault('catpart', {'page': 1, 'out': []})
    out = st['out']
    while st["page"] <= 600:
        try: items = _items_retry('/stockDetails', {'pageSize': 200, 'pageNumber': st['page']})
        except Exception:
            if _cache.get('cat'): return _cache['cat'][1]     # use the older copy rather than fail
            saved = _saved_catalog()
            if saved: return saved
            raise
        for s in items:
            if not s.get('id'): continue
            part = _split_model((s.get('model') or '').strip())[0] or (s.get('modelNumber') or s.get('stockCode') or '').strip()
            out.append({'id': s['id'], 'text': ' '.join(str(x) for x in (s.get('make'), s.get('model'), s.get('modelNumber')) if x).strip(),
                        'part': part, 'num': (s.get('modelNumber') or '').strip()})
        if len(items) < 200: break
        st['page'] += 1
        time.sleep(0.4)
    _cache['cat'] = (time.time(), out)
    _cache.pop('catpart', None)
    try:
        import json
        if out: leave.set_setting('stock_catalog_v1', json.dumps(out, separators=(',', ':')))
    except Exception as ex: print('[CATALOG] could not save copy:', ex)
    return out


@reorder_bp.route('/api/reorder/search')
def search():
    q = (request.args.get('q') or '').strip().lower()
    if len(q) < 2: return jsonify({'results': []})
    try: cat = _stock_catalog()
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    words = q.split()
    return jsonify({'results': [s for s in cat if all(w in s['text'].lower() for w in words)][:25]})


def _fresh_levels():
    _cache.pop('levels', None)
    return _levels()[0]


def _set(stock_id, et, eid, qty, rows=None):
    """Make the reorder level for this part at this location exactly qty: delete every existing row for it, then add one.
    BigChange's add call creates a new row rather than replacing, so existing rows must go first. rows = fresh levels (pass once for bulk jobs)."""
    if rows is None: rows = _fresh_levels()
    old = [r for r in rows if r['stockId'] == stock_id and r['entityType'] == et and r['entityId'] == eid and r['reorderId']]
    for r in old: _del(int(r['reorderId']))
    if qty <= 0: return {'removed': len(old)}
    return _ws('AddStockReorderLevel', {'StockId': stock_id, 'EntityType': et, 'EntityId': eid, 'Quantity': qty})


def _del(rid):
    return _ws('DeleteStockReorderLevel', {'StockReorderId': rid})


@reorder_bp.route('/api/reorder/delete-all', methods=['POST'])
def delete_all():
    """Remove every reorder level held at one location (van or contact)."""
    b = request.get_json(silent=True) or {}
    try: et, eid = int(b['entityType']), int(b['entityId'])
    except (KeyError, TypeError, ValueError): return jsonify({'error': 'entityType and entityId needed'}), 400
    _cache.pop('levels', None)
    try: rows, _ = _levels()
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    mine = [r for r in rows if r['entityType'] == et and r['entityId'] == eid and r['reorderId']]
    if b.get('expect') is not None and int(b['expect']) != len(mine):
        return jsonify({'error': f'Expected {b["expect"]} levels but BigChange now has {len(mine)}. Reload and try again.'}), 409
    failed = []
    for r in mine:
        try: _del(int(r['reorderId']))
        except Exception as ex: failed.append({'stockId': r['stockId'], 'error': str(ex)[:150]})
        time.sleep(0.1)
    _cache.pop('levels', None)
    left = None
    try: left = sum(1 for r in _levels()[0] if r['entityType'] == et and r['entityId'] == eid)
    except Exception: pass
    return jsonify({'deleted': len(mine) - len(failed), 'failed': failed, 'left': left})


@reorder_bp.route('/api/reorder/remove-part', methods=['POST'])
def remove_part():
    """Remove one part's reorder level (every row for it) from each of the vans given."""
    b = request.get_json(silent=True) or {}
    try: sid, ids = int(b['stockId']), {int(x) for x in b['vanIds']}
    except (KeyError, TypeError, ValueError): return jsonify({'error': 'stockId and vanIds needed'}), 400
    if not ids or len(ids) > 300: return jsonify({'error': 'Check the van list'}), 400
    try: rows = _fresh_levels()
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    mine = [r for r in rows if r['stockId'] == sid and r['entityType'] == 2 and r['entityId'] in ids and r['reorderId']]
    failed = []
    for r in mine:
        try: _del(int(r['reorderId']))
        except Exception as ex: failed.append({'id': r['entityId'], 'error': str(ex)[:150]})
        time.sleep(0.1)
    _cache.pop('levels', None)
    return jsonify({'deleted': len(mine) - len(failed), 'failed': failed, 'vansChecked': len(ids), 'vansWith': len({r['entityId'] for r in mine})})


@reorder_bp.route('/api/reorder/set', methods=['POST'])
def set_level():
    b = request.get_json(silent=True) or {}
    try: sid, et, eid, qty = int(b['stockId']), int(b['entityType']), int(b['entityId']), float(b['quantity'])
    except (KeyError, TypeError, ValueError): return jsonify({'error': 'stockId, entityType, entityId and quantity are needed'}), 400
    if qty < 0: return jsonify({'error': 'Quantity cannot be negative'}), 400
    try: res = _set(sid, et, eid, qty)
    except Exception as ex: return jsonify({'error': str(ex)[:400]}), 502
    _cache.pop('levels', None)
    after = None
    try:
        rows, _ = _levels()
        after = next((r['level'] for r in rows if r['stockId'] == sid and r['entityType'] == et and r['entityId'] == eid), None)
    except Exception: pass
    return jsonify({'ok': True, 'asked': qty, 'now': after, 'result': res})


@reorder_bp.route('/api/reorder/vans')
def vans():
    try: v = _vans()
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    if not v: return jsonify({'error': 'BigChange returned no vans'}), 502
    skip = request.args.get('skip', 'stock').strip().lower()
    return jsonify({'vans': [{'id': i, 'name': n} for i, n in sorted(v.items(), key=lambda x: x[1]) if not (skip and skip in n.lower())]})


@reorder_bp.route('/api/reorder/add-all', methods=['POST'])
def add_all():
    """Add one part and quantity as the reorder level on every van given (one call each)."""
    b = request.get_json(silent=True) or {}
    try: sid, qty, ids = int(b['stockId']), float(b['quantity']), [int(x) for x in b['vanIds']]
    except (KeyError, TypeError, ValueError): return jsonify({'error': 'stockId, quantity and vanIds are needed'}), 400
    if qty <= 0 or not ids or len(ids) > 300: return jsonify({'error': 'Check the quantity and the van list'}), 400
    done, failed = [], []
    try: cur = _fresh_levels()
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    for vid in ids:
        try: _set(sid, 2, vid, qty, cur); done.append(vid)
        except Exception as ex: failed.append({'id': vid, 'error': str(ex)[:200]})
        time.sleep(0.15)
    _cache.pop('levels', None)
    return jsonify({'done': len(done), 'failed': failed})


def _norm(t):
    return re.sub(r'\s+', ' ', str(t or '').strip().lower())


def _read_upload(data):
    """Rows of (part text, quantity) from a CSV or Excel file: first two columns, header row optional."""
    rows = []
    if data[:2] == b'PK':
        import io
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
        raw = [list(r) for r in ws.iter_rows(values_only=True)]
    else:
        import csv, io
        txt = data.decode('utf-8-sig', errors='replace')
        raw = list(csv.reader(io.StringIO(txt)))
    # Spreadsheet with headed columns (Make, Model, ... Level): the part is "make model", the level comes from the level column.
    for i, r in enumerate(raw[:10]):   # file exported from a van: Stock ID, Part, Re-Order Level
        low = [str(c or '').strip().lower().replace(' ', '') for c in r]
        if 'stockid' in low and any('level' in c for c in low):
            si, li = low.index('stockid'), next(j for j, c in enumerate(low) if 'level' in c)
            ni = next((j for j, c in enumerate(low) if c in ('part', 'name', 'make')), None)
            for rr in raw[i + 1:]:
                try: sid, q = int(float(str(rr[si]))), float(str(rr[li]).replace(',', ''))
                except (ValueError, IndexError, TypeError): continue
                rows.append((str(rr[ni]).strip() if ni is not None and len(rr) > ni and rr[ni] is not None else str(sid), q, None, sid))
            return rows
    hdr = None
    for i, r in enumerate(raw[:10]):
        low = [str(c or '').strip().lower() for c in r]
        if 'make' in low and 'model' in low and any('level' in c or 'qty' in c or 'quantity' in c for c in low):
            hdr = (i, low.index('make'), low.index('model'), next(j for j, c in enumerate(low) if 'level' in c or 'qty' in c or 'quantity' in c)); break
    if hdr:
        _, mk, md, lv = hdr
        for r in raw[hdr[0] + 1:]:
            if len(r) <= max(mk, md, lv): continue
            try: q = float(str(r[lv]).replace(',', '').strip())
            except ValueError: continue
            text = ' '.join(str(x).strip() for x in (r[mk], r[md]) if x not in (None, ''))
            if text: rows.append((text, q, str(r[md]).strip() if r[md] not in (None, '') else None, None))
        return rows
    for r in raw:
        if len(r) < 2 or r[0] in (None, ''): continue
        try: q = float(str(r[1]).replace(',', '').strip())
        except ValueError: continue       # header row or junk
        rows.append((str(r[0]).strip(), q, None, None))
    return rows


@reorder_bp.route('/api/reorder/upload-preview', methods=['POST'])
def upload_preview():
    """Match an uploaded list (part, quantity) to stock records, and show what would change at one location."""
    try: et, eid = int(request.args.get('entityType')), int(request.args.get('entityId'))
    except (TypeError, ValueError): return jsonify({'error': 'entityType and entityId needed'}), 400
    data = request.get_data()
    if not data: return jsonify({'error': 'No file received'}), 400
    try: rows = _read_upload(data)
    except Exception as ex: return jsonify({'error': f'Could not read that file: {ex}'}), 400
    if not rows: return jsonify({'error': 'No rows found - the first column needs the part, the second the quantity'}), 400
    if len(rows) > 2000: return jsonify({'error': 'Too many rows (limit 2000)'}), 400
    try:
        cur, _ = _levels()
        cat = [] if all(r[3] for r in rows) else _stock_catalog()   # a file exported from a van has Stock IDs: no need to read the whole stock list
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    by_part = {}
    for s in cat:
        for k in (_norm(s['part']), _norm(s['num'])):
            if k: by_part.setdefault(k, []).append(s)
    have = {r['stockId']: r['level'] for r in cur if r['entityType'] == et and r['entityId'] == eid}
    ok, bad = [], []
    asked_bc = 0
    for text, q, model, sid in rows:
        if sid:   # exported file: the stock ID is already known, no matching needed
            ok.append({'stockId': sid, 'name': text, 'asked': text, 'quantity': q, 'now': have.get(sid)}); continue
        k = _norm(text); hits = by_part.get(k) or []
        if not hits:   # "make model" from a spreadsheet: the stock record's name is "make model [model number]"
            hits = [s for s in cat if k and (_norm(s['text']) == k or _norm(s['text']).startswith(k + ' '))]
            hits = hits if len(hits) == 1 else []
        if not hits:
            hits = [s for s in cat if k and k in _norm(s['text'])]
            hits = hits if len(hits) == 1 else []
        if not hits and model and len(_norm(model)) >= 4:   # the model alone (e.g. an HP part number)
            km = _norm(model)
            hits = [s for s in cat if km == _norm(s['part']) or km == _norm(s['num']) or (' ' + km + ' ') in (' ' + _norm(s['text']) + ' ')]
            hits = hits if len(hits) == 1 else []
        if not hits and model and asked_bc < 300:   # not in our copy of the stock list: ask BigChange for that model directly
            asked_bc += 1
            try:
                found = _items('/stockDetails', {'model': model, 'pageSize': 20})
                found = [{'id': f['id'], 'text': ' '.join(str(x) for x in (f.get('make'), f.get('model'), f.get('modelNumber')) if x).strip()} for f in found if f.get('id') and _norm(f.get('model')) == _norm(model)]
                exact = [f for f in found if _norm(f['text']).startswith(k)] or found
                hits = exact if len(exact) == 1 else []
            except Exception: asked_bc = 10**6      # stop asking BigChange after the first failure
        if len(hits) == 1: ok.append({'stockId': hits[0]['id'], 'name': hits[0]['text'], 'asked': text, 'quantity': q, 'now': have.get(hits[0]['id'])})
        else: bad.append({'asked': text, 'quantity': q, 'why': 'More than one match' if len(by_part.get(k) or []) > 1 else 'Not found (stock list has %d parts)' % len(cat)})
    return jsonify({'matched': ok, 'unmatched': bad})


@reorder_bp.route('/api/reorder/upload-apply', methods=['POST'])
def upload_apply():
    b = request.get_json(silent=True) or {}
    try: et, eid, rows = int(b['entityType']), int(b['entityId']), b['rows']
    except (KeyError, TypeError, ValueError): return jsonify({'error': 'entityType, entityId and rows needed'}), 400
    if not isinstance(rows, list) or not rows or len(rows) > 2000: return jsonify({'error': 'Check the rows'}), 400
    failed = []
    try: cur = _fresh_levels()
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    for r in rows:
        try: _set(int(r['stockId']), et, eid, float(r['quantity']), cur)
        except Exception as ex: failed.append({'stockId': r.get('stockId'), 'error': str(ex)[:150]})
        time.sleep(0.15)
    _cache.pop('levels', None)
    mismatch = []
    try:
        cur, _ = _levels(); have = {r['stockId']: r['level'] for r in cur if r['entityType'] == et and r['entityId'] == eid}
        mismatch = [{'stockId': r['stockId'], 'asked': r['quantity'], 'now': have.get(int(r['stockId']))} for r in rows if have.get(int(r['stockId'])) != float(r['quantity'])][:50]
    except Exception: pass
    return jsonify({'done': len(rows) - len(failed), 'failed': failed, 'mismatch': mismatch})
