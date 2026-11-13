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
    from app import get_token, CUSTOMER_ID
    q = {'action': action, **(params or {})}
    r = requests.get(WS_URL, params=q, headers={'Authorization': f'Bearer {get_token()}', 'customer-id': str(CUSTOMER_ID)}, timeout=40)
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
            sid = int(_pick(r, 'stockid', 'stockdetailsid', 'stockitemid', 'stock_id'))
            etype = int(_pick(r, 'entitytype', 'entitytypeid', 'locationtype') or 0)
            eid = int(_pick(r, 'entityid', 'locationid', 'assetid', 'contactid'))
            qty = float(_pick(r, 'quantity', 'reorderlevel', 'level', 'reorderquantity') or 0)
        except (TypeError, ValueError): continue
        make = _pick(r, 'make', 'stockmake') or ''
        model = _pick(r, 'model', 'stockmodel', 'stockname', 'description') or ''
        num = _pick(r, 'number', 'modelnumber', 'stocknumber') or ''
        out.append({'reorderId': _pick(r, 'stockreorderid', 'reorderid', 'id'), 'stockId': sid, 'entityType': etype, 'entityId': eid, 'level': qty,
                    'name': ' '.join(str(x) for x in (make, model, num) if x).strip(),
                    'entityName': _pick(r, 'entityname', 'registration', 'assetname', 'contactname', 'locationname') or ''})
    _cache['levels'] = (time.time(), (out, body))
    return out, body


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
    out = []
    for name, p in variants:
        try:
            body = _ws('ListStockReorderLevel', p)
            out.append({'try': name, 'count': len(_rows(body)), 'raw': _j.dumps(body)[:700]})
        except Exception as ex:
            out.append({'try': name, 'error': str(ex)[:400]})
    return jsonify({'results': out})


@reorder_bp.route('/api/reorder/levels')
def levels():
    try: rows, _ = _levels()
    except Exception as ex: return jsonify({'error': str(ex)[:500]}), 502
    try: veh = _vehicles()
    except Exception: veh = {}
    locs = {}
    for r in rows:
        k = (r['entityType'], r['entityId'])
        L = locs.setdefault(k, {'entityType': r['entityType'], 'entityId': r['entityId'], 'kind': ETYPE.get(r['entityType'], 'Location'), 'items': [], 'label': ''})
        L['label'] = L['label'] or r['entityName'] or (veh.get(r['entityId']) if r['entityType'] == 2 else '') or f"{ETYPE.get(r['entityType'], 'Location')} {r['entityId']}"
        L['items'].append({'stockId': r['stockId'], 'name': r['name'] or f"Stock {r['stockId']}", 'level': r['level'], 'reorderId': r['reorderId']})
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


def _stock_catalog():
    if _cache.get('cat') and time.time() - _cache['cat'][0] < 900: return _cache['cat'][1]
    out, page = [], 1
    while page <= 100:
        items = _items('/stockDetails', {'pageSize': 200, 'pageNumber': page})
        out += [{'id': s['id'], 'text': ' '.join(str(x) for x in (s.get('make'), s.get('model'), s.get('modelNumber')) if x).strip()} for s in items if s.get('id')]
        if len(items) < 200: break
        page += 1
    _cache['cat'] = (time.time(), out)
    return out


@reorder_bp.route('/api/reorder/search')
def search():
    q = (request.args.get('q') or '').strip().lower()
    if len(q) < 2: return jsonify({'results': []})
    try: cat = _stock_catalog()
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    words = q.split()
    return jsonify({'results': [s for s in cat if all(w in s['text'].lower() for w in words)][:25]})


def _set(stock_id, et, eid, qty):
    return _ws('AddStockReorderLevel', {'StockId': stock_id, 'EntityType': et, 'EntityId': eid, 'Quantity': qty})


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
    try: v = _vehicles()
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    skip = (request.args.get('skip') or 'stock').strip().lower()
    return jsonify({'vans': [{'id': i, 'name': n} for i, n in sorted(v.items(), key=lambda x: x[1]) if not (skip and skip in n.lower())]})


@reorder_bp.route('/api/reorder/add-all', methods=['POST'])
def add_all():
    """Add one part and quantity as the reorder level on every van given (one call each)."""
    b = request.get_json(silent=True) or {}
    try: sid, qty, ids = int(b['stockId']), float(b['quantity']), [int(x) for x in b['vanIds']]
    except (KeyError, TypeError, ValueError): return jsonify({'error': 'stockId, quantity and vanIds are needed'}), 400
    if qty <= 0 or not ids or len(ids) > 300: return jsonify({'error': 'Check the quantity and the van list'}), 400
    done, failed = [], []
    for vid in ids:
        try: _set(sid, 2, vid, qty); done.append(vid)
        except Exception as ex: failed.append({'id': vid, 'error': str(ex)[:200]})
        time.sleep(0.15)
    _cache.pop('levels', None)
    return jsonify({'done': len(done), 'failed': failed})
