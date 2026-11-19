"""M8 更正净结算的 HTTP 端到端：受理、重试幂等、结清、界面、结案下载。"""
import csv
import io
import pytest
from app.errors import DomainError


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'http.db'))
    from app.main import create_app
    app = create_app()
    app.config['TESTING'] = True
    return app.test_client()


def _payload(cid='C-HTTP-1', refund='1000.00', recovery='300.00'):
    return {'correction_id': cid, 'policy_id': 'P002', 'claim_id': 'CLM-77',
            'refund_premium': refund, 'recover_claim': recovery}


def test_full_lifecycle_pay_direction(client):
    r = client.post('/api/m8_settlement', json=_payload())
    assert r.status_code == 200
    body = r.get_json()
    assert body['status'] == '待结清'
    assert body['direction'] == '退款' and body['net_amount'] == '700.00'
    assert body['offset_balance'] == '300.00'

    # 重试幂等：同一更正号不重复建账
    again = client.post('/api/m8_settlement', json=_payload())
    assert again.get_json()['version'] == 1
    assert len(client.get('/api/m8_settlement').get_json()) == 1

    # 结清恰好一次
    s = client.post('/api/m8_settlement/C-HTTP-1/settle')
    assert s.get_json()['status'] == '已结清'
    dup = client.post('/api/m8_settlement/C-HTTP-1/settle')
    assert dup.status_code == 409 and dup.get_json()['code'] == 'ALREADY_SETTLED'


def test_collect_and_flat_directions(client):
    client.post('/api/m8_settlement', json=_payload('C-COL', refund='100.00', recovery='400.00'))
    assert client.get('/api/m8_settlement/C-COL').get_json()['direction'] == '追偿'
    client.post('/api/m8_settlement', json=_payload('C-FLAT', refund='250.00', recovery='250.00'))
    flat = client.get('/api/m8_settlement/C-FLAT').get_json()
    assert flat['direction'] == '两清' and flat['net_amount'] == '0.00'


def test_invalid_input_rejected(client):
    bad = client.post('/api/m8_settlement',
                      json={'correction_id': 'C', 'policy_id': 'P', 'claim_id': '',
                            'refund_premium': '100', 'recover_claim': '10'})
    assert bad.status_code == 422


def test_review_screen_renders(client):
    client.post('/api/m8_settlement', json=_payload())
    page = client.get('/m8_settlement')
    assert page.status_code == 200
    html = page.data.decode()
    for token in ('应退保费', '应追赔款', '抵扣余额', '支付方向', 'C-HTTP-1'):
        assert token in html


def test_export_csv_includes_settlement(client):
    client.post('/api/m8_settlement', json=_payload())
    client.post('/api/m8_settlement/C-HTTP-1/settle')
    run = client.post('/api/runs', json={'sample': 'clean.csv'}).get_json()
    dl = client.get('/api/archive/%s/export.csv' % run['run_id'])
    assert dl.status_code == 200
    text = dl.data.decode('utf-8-sig')
    rows = list(csv.reader(io.StringIO(text)))
    kinds = {row[0] for row in rows}
    assert '更正结算' in kinds and '结算义务腿' in kinds
    # 三条义务腿齐全，净额可被解释
    leg_rows = [row for row in rows if row[0] == '结算义务腿']
    assert len(leg_rows) == 3


def test_settled_correction_cannot_be_replaced(client):
    client.post('/api/m8_settlement', json=_payload())
    client.post('/api/m8_settlement/C-HTTP-1/settle')
    locked = client.post('/api/m8_settlement', json=_payload(refund='9999.00'))
    assert locked.status_code == 409 and locked.get_json()['code'] == 'SETTLED_LOCKED'
