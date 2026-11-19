"""更正结算持久层：幂等、替换回滚、明细丢失、恰好一次结清、结案导出。"""
import sqlite3
import pytest
from app.errors import DomainError
from app.store import autocommit_connection
from app.modules.m8_settlement import repository, service
from app.modules.m8_settlement.rules import STATUS_OPEN, STATUS_SETTLED
from app.modules.m7_archive.export_out import bundle
from app.workflow import load, run


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'settle.db'))
    db = autocommit_connection()
    repository.init_settlement_tables(db)
    db.close()
    return tmp_path


def _req(cid='C001', refund='1000.00', recovery='300.00'):
    return dict(correction_id=cid, policy_id='P002', claim_id='CLM-9',
                refund_premium=refund, recover_claim=recovery)


def test_create_starts_open_with_paid_claim_unsettled(db_env):
    r = service.create(_req())
    assert r['status'] == STATUS_OPEN and r['claim_id'] == 'CLM-9'
    assert r['net_payable'] == '700.00'


def test_retry_same_correction_is_idempotent_no_double_refund(db_env):
    first = service.create(_req())
    second = service.create(_req())
    assert first['correction_id'] == second['correction_id']
    assert len(repository.list_settlements()) == 1
    db = autocommit_connection()
    # 义务腿仍只有三条，没有重复建账
    legs = db.execute('SELECT COUNT(*) c FROM settlement_legs').fetchone()['c']
    db.close()
    assert legs == 3


def test_settle_is_once_only(db_env):
    service.create(_req())
    done = service.close('C001')
    assert done['status'] == STATUS_SETTLED
    with pytest.raises(DomainError) as e:
        service.close('C001')
    assert e.value.code == 'ALREADY_SETTLED' and e.value.status == 409
    # 已结清后再提交同号更正也被锁定，不能改写义务去向
    with pytest.raises(DomainError) as lock:
        service.create(_req(refund='1200.00'))
    assert lock.value.code == 'SETTLED_LOCKED'


def test_replace_open_correction_keeps_version_and_history(db_env):
    service.create(_req(refund='1000.00', recovery='300.00'))
    updated = service.create(_req(refund='1000.00', recovery='500.00'))
    assert updated['version'] == 2
    assert updated['net_payable'] == '500.00'
    assert len(updated['legs']) == 3   # 旧腿被整组替换，无残留


def test_replace_failure_restores_and_keeps_old_version(db_env, monkeypatch):
    service.create(_req())
    # 令替换过程中义务腿写入失败，断言旧版完整保留
    def boom(*a, **k):
        raise sqlite3.IntegrityError('injected leg failure')
    monkeypatch.setattr(repository, '_replace_legs', boom)
    with pytest.raises(DomainError) as e:
        service.create(_req(refund='1000.00', recovery='500.00'))
    assert e.value.code == 'REPLACE_FAILED'
    # 只撤销函数 patch，保留 db_env 注入的 DB_PATH（monkeypatch.undo 会连环境变量一起撤销）
    monkeypatch.delattr(repository, '_replace_legs')
    db = autocommit_connection()
    kept = repository.get_settlement(db, 'C001')
    db.close()
    assert kept['version'] == 1 and kept['recover_claim'] == '300.00'
    assert len(kept['legs']) == 3      # 旧版义务腿完好


def test_lost_leg_record_is_detected_not_silently_explained(db_env):
    service.create(_req())
    db = autocommit_connection()
    db.execute("DELETE FROM settlement_legs WHERE correction_id='C001' AND leg='RECOVER_CLAIM'")
    with pytest.raises(DomainError) as e:
        repository.get_settlement(db, 'C001')
    db.close()
    assert e.value.code == 'LEDGER'
    # 列表不静默隐藏损坏记录
    listed = repository.list_settlements()
    assert listed[0].get('integrity') == 'BROKEN'


def test_bundle_export_contains_settlement_and_legs(db_env):
    state = run(load('clean.csv'), {})
    service.create(_req())
    service.close('C001')
    csv_text = bundle(state)
    assert '更正结算' in csv_text and '结算义务腿' in csv_text
    assert 'C001' in csv_text and 'CLM-9' in csv_text
    assert 'REFUND_PREMIUM' in csv_text and 'RECOVER_CLAIM' in csv_text


def test_bundle_refuses_when_ledger_broken(db_env):
    state = run(load('clean.csv'), {})
    service.create(_req())
    db = autocommit_connection()
    db.execute("DELETE FROM settlement_legs WHERE correction_id='C001'")
    db.close()
    with pytest.raises(DomainError):
        bundle(state)


def test_concurrent_settle_only_one_wins(db_env):
    service.create(_req())
    # 两个独立连接竞争结清，条件更新 WHERE status='待结清' 保证恰好一行变更
    db1 = autocommit_connection()
    db1.execute('BEGIN')
    cur1 = db1.execute("UPDATE settlements SET status=? WHERE correction_id=? AND status=?",
                       (STATUS_SETTLED, 'C001', STATUS_OPEN))
    db1.commit()
    assert cur1.rowcount == 1
    db1.close()

    db2 = autocommit_connection()
    cur2 = db2.execute("UPDATE settlements SET status=? WHERE correction_id=? AND status=?",
                       (STATUS_SETTLED, 'C001', STATUS_OPEN))
    db2.commit()
    assert cur2.rowcount == 0      # 重试不再命中待结清行，杜绝重复退款/追偿
    db2.close()

    db = autocommit_connection()
    assert repository.get_settlement(db, 'C001')['status'] == STATUS_SETTLED
    db.close()


def test_all_obligations_have_destination_after_settle(db_env):
    done = service.close(service.create(_req(refund='1000.00', recovery='300.00'))['correction_id'])
    # 守恒：应退 = 抵扣 + 净付；应追 = 抵扣 + 净收；义务全部有去向
    from decimal import Decimal
    assert Decimal(done['refund_premium']) == Decimal(done['offset_balance']) + Decimal(done['net_payable'])
    assert Decimal(done['recover_claim']) == Decimal(done['offset_balance']) + Decimal(done['net_receivable'])
    assert done['status'] == STATUS_SETTLED
