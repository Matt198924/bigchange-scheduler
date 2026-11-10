"""Van parts finder, built into the staff hub at /parts.

The page itself is static (static/parts). The stock list lives in the leave database (so it survives deploys) and is
replaced by uploading the stock Excel file from the Staff tab. Until one is uploaded, the bundled stock.json is used.
"""
import io
import json
import os
from datetime import datetime
from flask import Blueprint, jsonify, redirect, request, send_from_directory

import leave

parts_bp = Blueprint('parts', __name__)
PARTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'parts')


@parts_bp.route('/parts')
def parts_redirect():
    return redirect('/parts/', code=301)


@parts_bp.route('/parts/')
def parts_index():
    return send_from_directory(PARTS_DIR, 'index.html')


@parts_bp.route('/parts/<path:name>')
def parts_file(name):
    return send_from_directory(PARTS_DIR, name)


def _bundled():
    with open(os.path.join(PARTS_DIR, 'stock.json'), encoding='utf-8') as f:
        return json.load(f)


def parse_stock_xlsx(data):
    """Rows of (part, qty, location) from the stock workbook: first three columns, header row first."""
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
    out = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0: continue
        part = str(row[0] if len(row) > 0 and row[0] is not None else '').strip()
        if not part: continue
        loc = str(row[2] if len(row) > 2 and row[2] is not None else '').strip()
        try: qty = float(row[1]); qty = int(qty) if qty == int(qty) else qty
        except (TypeError, ValueError, IndexError): qty = 0
        out.append({'part': part, 'qty': qty, 'location': loc})
    return out


@parts_bp.route('/api/parts/stock')
def stock():
    try:
        leave.init_db()
        raw = leave.get_setting('parts_stock')
        if raw:
            return jsonify({'parts': json.loads(raw), 'updated': leave.get_setting('parts_stock_at') or ''})
    except Exception as ex:
        print(f'[PARTS] stored stock unavailable, using bundled list: {ex}')
    return jsonify({'parts': _bundled(), 'updated': ''})


@parts_bp.route('/api/parts/admin/stock', methods=['POST'])
def upload_stock():
    """Admin only: body is the raw .xlsx file."""
    if not leave.is_admin(): return leave._auth_error()
    data = request.get_data()
    if not data: return jsonify({'error': 'No file received'}), 400
    try: rows = parse_stock_xlsx(data)
    except Exception as ex: return jsonify({'error': f'Could not read that Excel file: {ex}'}), 400
    if not rows: return jsonify({'error': 'No parts found - the first sheet needs a header row, then part, qty, location'}), 400
    leave.init_db()
    stamp = datetime.now().strftime('%d %b %Y %H:%M')
    leave.set_setting('parts_stock', json.dumps(rows, ensure_ascii=False))
    leave.set_setting('parts_stock_at', stamp)
    return jsonify({'success': True, 'count': len(rows), 'updated': stamp})
