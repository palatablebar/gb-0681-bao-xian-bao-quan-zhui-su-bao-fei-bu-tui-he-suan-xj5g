from flask import Blueprint, request, jsonify, render_template
from app.config import TITLE, SHORT, MODULES
from app.errors import DomainError
from app import store
from app.workflow import load, run
from app.modules.m8_settlement import service, repository

bp = Blueprint('m8_settlement', __name__)


@bp.get('/m8_settlement')
def screen():
    settlements = repository.list_settlements()
    hist = store.history()
    state = store.get(hist[0]['id']) if hist else run(load('dirty.csv'), {})
    return render_template('m8_settlement.html', title=TITLE, short=SHORT,
                           modules=MODULES, current=8, data=state, run=state,
                           history=hist, settlements=settlements)


@bp.post('/api/m8_settlement')
def create_correction():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise DomainError('PAYLOAD', '请求必须是对象')
    return jsonify(service.create(data))


@bp.get('/api/m8_settlement')
def list_corrections():
    return jsonify(repository.list_settlements())


@bp.get('/api/m8_settlement/<correction_id>')
def get_correction(correction_id):
    from app.store import autocommit_connection
    db = autocommit_connection()
    try:
        repository.init_settlement_tables(db)
        return jsonify(repository.get_settlement(db, correction_id))
    finally:
        db.close()


@bp.post('/api/m8_settlement/<correction_id>/settle')
def settle_correction(correction_id):
    return jsonify(service.close(correction_id))
