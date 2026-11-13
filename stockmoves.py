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


def _parse(b):
    """mode 'out': stock room (fromId) -> ticked vans. mode 'back': ticked vans -> stock room (toId)."""
    mode = b.get('mode') or 'out'
    vans = [int(x) for x in b['vanIds']]
    room = int(b['roomId'] if 'roomId' in b else b['fromId'])
    rk = b.get('roomKind') or b.get('fromKind') or 'contact'
    return mode, rk, room, vans


@stockmoves_bp.route('/api/stockmoves/preview', methods=['POST'])
def preview():
    """Check enough stock is held at the source(s) for what is being moved."""
    b = request.get_json(silent=True) or {}
    try: mode, rk, room, vans = _parse(b)
    except (KeyError, TypeError, ValueError): return jsonify({'error': 'roomId and vanIds needed'}), 400
    rows = _plan_rows(b.get('rows') or [])
    if not rows or not vans or len(rows) > 300 or len(vans) > 100: return jsonify({'error': 'Check the parts and vans (up to 300 parts, 100 vans)'}), 400
    try: cat = {s['id']: s['text'] for s in reorder._stock_catalog()}
    except Exception: cat = {}
    res = []
    try:
        if mode == 'out':
            for sid, q in rows:
                have = sum(float(i.get('quantity') or 0) for i in _at(rk, room, sid))
                res.append({'stockId': sid, 'name': cat.get(sid) or f'Stock {sid}', 'each': q, 'need': q * len(vans), 'have': have, 'ok': have >= q * len(vans), 'canDo': int(have // q) if q else 0})
                time.sleep(0.1)
        else:   # back: every van must hold the quantity being returned
            held = {}
            for vid in vans:
                for i in _at('van', vid):
                    if i.get('quantity') is not None: held[(vid, i.get('stockDetailsId'))] = held.get((vid, i.get('stockDetailsId')), 0) + float(i['quantity'])
                time.sleep(0.1)
            for sid, q in rows:
                ok_vans = sum(1 for v in vans if held.get((v, sid), 0) >= q)
                res.append({'stockId': sid, 'name': cat.get(sid) or f'Stock {sid}', 'each': q, 'need': q * len(vans), 'have': q * ok_vans, 'ok': ok_vans == len(vans), 'canDo': ok_vans,
                            'note': f'{len(vans) - ok_vans} van(s) do not hold {q}' if ok_vans < len(vans) else ''})
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    return jsonify({'rows': res, 'vans': len(vans), 'mode': mode})


@stockmoves_bp.route('/api/stockmoves/apply', methods=['POST'])
def apply():
    """Move stock. The destination is added to first, then the source reduced, so a failure never loses stock."""
    b = request.get_json(silent=True) or {}
    try: mode, rk, room, vans = _parse(b)
    except (KeyError, TypeError, ValueError): return jsonify({'error': 'roomId and vanIds needed'}), 400
    rows = _plan_rows(b.get('rows') or [])
    if not rows or not vans or len(rows) > 300 or len(vans) > 100: return jsonify({'error': 'Check the parts and vans'}), 400
    _init()
    van_names = reorder._vans()
    room_label = b.get('roomLabel') or f'{rk} {room}'
    who, now = _who(), datetime.now().strftime('%Y-%m-%d %H:%M')
    try: cat = {s['id']: s['text'] for s in reorder._stock_catalog()}
    except Exception: cat = {}
    patch, post = leave.bc_hooks['patch'], leave.bc_hooks['post']
    done, failed, skipped = 0, [], []

    def log(vid, sid, q, ok, note=''):
        vn = van_names.get(vid, str(vid))
        leave.execute('INSERT INTO stock_moves_log (at, who, from_label, to_label, stock_id, part, qty, ok, note) VALUES (?,?,?,?,?,?,?,?,?)',
                      (now, who, room_label if mode == 'out' else vn, vn if mode == 'out' else room_label, str(sid), cat.get(sid, ''), q, 1 if ok else 0, note[:200]))

    def take(pool, q):
        left = q
        for i in sorted(pool, key=lambda i: -float(i.get('quantity') or 0)):
            if left <= 0: break
            t = min(left, float(i['quantity']))
            if t <= 0: continue
            patch(f"/stockItems/{i['id']}", {'quantity': float(i['quantity']) - t}); i['quantity'] = float(i['quantity']) - t; left -= t

    def give(items, kind, loc, sid, q):
        e = items.get(sid)
        if e:
            patch(f"/stockItems/{e['id']}", {'quantity': float(e.get('quantity') or 0) + q}); e['quantity'] = float(e.get('quantity') or 0) + q
        else:
            r = post('/stockItems', {'stockDetailsId': sid, 'quantity': q, 'locationVehicleId' if kind == 'van' else 'locationContactId': loc})
            items[sid] = {'id': r.get('id') if isinstance(r, dict) else r, 'stockDetailsId': sid, 'quantity': q}

    def by_part(items):
        m = {}
        for i in items: m.setdefault(i.get('stockDetailsId'), i)
        return m

    try:
        room_items = [i for i in _items({('locationVehicleId' if rk == 'van' else 'locationContactId'): room}) if i.get('quantity') is not None]
    except Exception as ex: return jsonify({'error': str(ex)[:300]}), 502
    room_pool = {}
    for i in room_items: room_pool.setdefault(i.get('stockDetailsId'), []).append(i)
    room_first = by_part(room_items)

    for vid in vans:
        try: vit = [i for i in _items({'locationVehicleId': vid}) if i.get('quantity') is not None]
        except Exception as ex:
            failed.append({'van': van_names.get(vid, vid), 'part': 'all', 'error': 'could not read van stock: ' + str(ex)[:120]}); continue
        van_pool = {}
        for i in vit: van_pool.setdefault(i.get('stockDetailsId'), []).append(i)
        van_first = by_part(vit)
        for sid, q in rows:
            src = room_pool.get(sid, []) if mode == 'out' else van_pool.get(sid, [])
            have = sum(float(i['quantity']) for i in src)
            if have < q:
                skipped.append({'van': van_names.get(vid, vid), 'part': cat.get(sid, sid), 'wanted': q, 'have': have, 'from': 'stock room' if mode == 'out' else 'van'})
                log(vid, sid, q, False, 'skipped: not enough in source'); continue
            try:
                if mode == 'out':
                    give(van_first, 'van', vid, sid, q); take(src, q)
                else:
                    give(room_first, rk, room, sid, q); take(src, q)
                done += 1; log(vid, sid, q, True)
            except Exception as ex:
                failed.append({'van': van_names.get(vid, vid), 'part': cat.get(sid, sid), 'error': str(ex)[:160]}); log(vid, sid, q, False, str(ex))
            time.sleep(0.15)
    return jsonify({'moved': done, 'failed': failed, 'skipped': skipped})


@stockmoves_bp.route('/api/stockmoves/log')
def history():
    _init()
    rows = leave.query('SELECT at, who, from_label, to_label, part, qty, ok, note FROM stock_moves_log ORDER BY id DESC LIMIT 200')
    return jsonify({'log': [dict(r) for r in rows]})


@stockmoves_bp.route('/api/stockmoves/search')
def search(): return reorder.search()


@stockmoves_bp.route('/api/stockmoves/upload-preview', methods=['POST'])
def upload_preview(): return reorder.upload_preview()
