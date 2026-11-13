import os
from flask import Blueprint, send_from_directory
stockmoves_bp = Blueprint('stockmoves', __name__)
PAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'stockmoves')


@stockmoves_bp.route('/stockmoves')
@stockmoves_bp.route('/stockmoves/')
def page(): return send_from_directory(PAGE_DIR, 'index.html')
