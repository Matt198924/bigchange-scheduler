import os
import re
import time
from datetime import datetime
from flask import Blueprint, jsonify, request, send_from_directory
import leave
import reorder

stockmoves_bp = Blueprint('stockmoves', __name__)
PAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'stockmoves')
_ready = False


def _init():
    global _ready
    if _ready: return
    leave.init_db()
    pk = 'SERIAL PRIMARY KEY' if leave.IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    leave.execute(f'''CREATE TABLE IF NOT EXISTS stock_moves_log (id {pk}, at TEXT, who TEXT, from_label TEXT, to_label TEXT,
                    stock_id TEXT, part TEXT, qty REAL, ok INTEGER, note TEXT)''')
    _ready = True


def _items(params):
    """All unsold stock items matching params (paged, retrying on 429)."""
    out, page = [], 1
    while page <= 30:
        got = reorder._items_retry('/stockItems', {**params, 'isSold': False, 'pageSize': 200, 'pageNumber': page})
        out += got
        if len(got) < 200: break
        page += 1
    return out


def _at(kind, loc_id, stock_id=None):
    p = {('locationVehicleId' if kind == 'van' else 'locationContactId'): loc_id}
    if stock_id: p['stockDetailsId'] = stock_id
    return _items(p)


def _who():
    try: return (leave.current_employee() or {}).get('name') or ''
    except Exception: return ''


@stockmoves_bp.route('/stockmoves')
@stockmoves_bp.route('/stockmoves/')
def page(): return send_from_directory(PAGE_DIR, 'index.html')


@stockmoves_bp.route('/api/stockmoves/locations')
def locations():
    try: vans = reorder._vans()
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    rooms = []
    try:
        ids = sorted({r['entityId'] for r in reorder._levels()[0] if r['entityType'] == 1})
        names = reorder._contact_names(ids)
        rooms = [{'kind': 'contact', 'id': i, 'name': n} for i, n in names.items() if n and 'stock' in n.lower()]
    except Exception as ex: print(f'[MOVES] stock rooms unavailable: {ex}')
    return jsonify({'rooms': sorted(rooms, key=lambda x: x['name']),
                    'vans': [{'kind': 'van', 'id': i, 'name': n} for i, n in sorted(vans.items(), key=lambda x: x[1])]})


def _plan_rows(rows):
    out = []
    for r in rows:
        try: out.append((int(r['stockId']), float(r['quantity'])))
        except (KeyError, TypeError, ValueError): pass
    merged = {}
    for sid, q in out: merged[sid] = merged.get(sid, 0) + q
    return [(s, q) for s, q in merged.items() if q > 0]


@stockmoves_bp.route('/api/stockmoves/preview', methods=['POST'])
def preview():
    """Check the source location holds enough of each part for every destination van."""
    b = request.get_json(silent=True) or {}
    try: fk, fid, vans = b['fromKind'], int(b['fromId']), [int(x) for x in b['toVanIds']]
    except (KeyError, TypeError, ValueError): return jsonify({'error': 'fromKind, fromId and toVanIds needed'}), 400
    rows = _plan_rows(b.get('rows') or [])
    if not rows or not vans or len(rows) > 300 or len(vans) > 100: return jsonify({'error': 'Check the parts and vans (up to 300 parts, 100 vans)'}), 400
    try: cat = {s['id']: s['text'] for s in reorder._stock_catalog()}
    except Exception: cat = {}
    res = []
    try:
        for sid, q in rows:
            have = sum(float(i.get('quantity') or 0) for i in _at(fk, fid, sid) if i.get('quantity') is not None)
            need = q * len(vans)
            res.append({'stockId': sid, 'name': cat.get(sid) or f'Stock {sid}', 'each': q, 'need': need, 'have': have, 'ok': have >= need})
            time.sleep(0.1)
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    return jsonify({'rows': res, 'vans': len(vans)})


@stockmoves_bp.route('/api/stockmoves/apply', methods=['POST'])
def apply():
    """Move stock: add to each van first, then take the same quantity off the source (so a failure never loses stock)."""
    b = request.get_json(silent=True) or {}
    try: fk, fid, vans = b['fromKind'], int(b['fromId']), [int(x) for x in b['toVanIds']]
    except (KeyError, TypeError, ValueError): return jsonify({'error': 'fromKind, fromId and toVanIds needed'}), 400
    rows = _plan_rows(b.get('rows') or [])
    if not rows or not vans or len(rows) > 300 or len(vans) > 100: return jsonify({'error': 'Check the parts and vans'}), 400
    _init()
    van_names = reorder._vans()
    flabel = b.get('fromLabel') or f'{fk} {fid}'
    who, now = _who(), datetime.now().strftime('%Y-%m-%d %H:%M')
    try: cat = {s['id']: s['text'] for s in reorder._stock_catalog()}
    except Exception: cat = {}
    patch, post = leave.bc_hooks['patch'], leave.bc_hooks['post']
    done, failed = 0, []

    def log(vid, sid, q, ok, note=''):
        leave.execute('INSERT INTO stock_moves_log (at, who, from_label, to_label, stock_id, part, qty, ok, note) VALUES (?,?,?,?,?,?,?,?,?)',
                      (now, who, flabel, van_names.get(vid, str(vid)), str(sid), cat.get(sid, ''), q, 1 if ok else 0, note[:200]))

    try: src = {sid: [i for i in _at(fk, fid, sid) if i.get('quantity') is not None] for sid, _ in rows}
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    for vid in vans:
        try: vitems = {}; [vitems.setdefault(i.get('stockDetailsId'), i) for i in _at('van', vid)]
        except Exception as ex:
            failed.append({'van': van_names.get(vid, vid), 'part': 'all', 'error': 'could not read van stock: ' + str(ex)[:120]}); continue
        for sid, q in rows:
            pool = sorted(src[sid], key=lambda i: -float(i.get('quantity') or 0))
            if sum(float(i['quantity']) for i in pool) < q:
                failed.append({'van': van_names.get(vid, vid), 'part': cat.get(sid, sid), 'error': 'not enough in source'}); log(vid, sid, q, False, 'not enough in source'); continue
            try:
                ex_item = vitems.get(sid)
                if ex_item:
                    patch(f"/stockItems/{ex_item['id']}", {'quantity': float(ex_item.get('quantity') or 0) + q})
                    ex_item['quantity'] = float(ex_item.get('quantity') or 0) + q
                else:
                    r = post('/stockItems', {'stockDetailsId': sid, 'quantity': q, 'locationVehicleId': vid})
                    vitems[sid] = {'id': r.get('id') if isinstance(r, dict) else r, 'stockDetailsId': sid, 'quantity': q}
                left = q
                for i in pool:
                    if left <= 0: break
                    take = min(left, float(i['quantity']))
                    patch(f"/stockItems/{i['id']}", {'quantity': float(i['quantity']) - take})
                    i['quantity'] = float(i['quantity']) - take; left -= take
                done += 1; log(vid, sid, q, True)
            except Exception as ex:
                failed.append({'van': van_names.get(vid, vid), 'part': cat.get(sid, sid), 'error': str(ex)[:160]}); log(vid, sid, q, False, str(ex))
            time.sleep(0.15)
    return jsonify({'moved': done, 'failed': failed})


@stockmoves_bp.route('/api/stockmoves/log')
def history():
    _init()
    rows = leave.query('SELECT at, who, from_label, to_label, part, qty, ok, note FROM stock_moves_log ORDER BY id DESC LIMIT 200')
    return jsonify({'log': [dict(r) for r in rows]})


@stockmoves_bp.route('/api/stockmoves/search')
def search(): return reorder.search()


@stockmoves_bp.route('/api/stockmoves/upload-preview', methods=['POST'])
def upload_preview(): return reorder.upload_preview()
