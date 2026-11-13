import os
from flask import Blueprint, send_from_directory
dashboard_bp = Blueprint('dashboard', __name__)
PAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'dashboard')


@dashboard_bp.route('/dashboard')
@dashboard_bp.route('/dashboard/')
def page(): return send_from_directory(PAGE_DIR, 'index.html')
