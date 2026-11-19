"""追溯减保更正净结算的领域规则测试。"""
from decimal import Decimal
import pytest
from app.errors import DomainError
from app.modules.m8_settlement.rules import (
    settle_correction, verify, mark_settled,
    STATUS_OPEN, STATUS_SETTLED, DIR_PAY, DIR_COLLECT, DIR_FLAT)


def _req(**over):
    base = dict(correction_id='C001', policy_id='P002', claim_id='CLM-9',
                refund_premium='1000.00', recover_claim='300.00')
    base.update(over)
    return base


def test_net_settlement_unique_and_conserved():
    r = settle_correction(_req())
    # 应退 1000、应追 300 → 抵扣 300、净付 700、方向退款
    assert r['refund_premium'] == '1000.00'
    assert r['recover_claim'] == '300.00'
    assert r['offset_balance'] == '300.00'
    assert r['net_payable'] == '700.00'
    assert r['net_receivable'] == '0.00'
    assert r['net_amount'] == '700.00'
    assert r['direction'] == DIR_PAY
    assert r['status'] == STATUS_OPEN
    assert verify(r) is True


def test_direction_collect_when_recovery_exceeds_refund():
    r = settle_correction(_req(refund_premium='200.00', recover_claim='750.50'))
    assert r['direction'] == DIR_COLLECT
    assert r['net_receivable'] == '550.50'
    assert r['net_payable'] == '0.00'
    assert r['offset_balance'] == '200.00'
    assert r['net_amount'] == '550.50'


def test_direction_flat_when_equal():
    r = settle_correction(_req(refund_premium='400.00', recover_claim='400.00'))
    assert r['direction'] == DIR_FLAT
    assert r['net_amount'] == '0.00'
    assert r['offset_balance'] == '400.00'
    assert r['net_payable'] == '0.00' and r['net_receivable'] == '0.00'


def test_obligations_are_independent_legs():
    r = settle_correction(_req())
    legs = {l['leg']: l for l in r['legs']}
    # 退款腿与追偿腿各自独立、挂不同来源；追偿腿保留原赔案号
    assert legs['REFUND_PREMIUM']['debit'] == '1000.00'
    assert legs['RECOVER_CLAIM']['credit'] == '300.00'
    assert legs['RECOVER_CLAIM']['claim_id'] == 'CLM-9'
    assert legs['NETTING']['debit'] == legs['NETTING']['credit'] == '300.00'


def test_original_obligation_amounts_never_mutated_by_netting():
    # 抵扣只在 NETTING 腿体现，退款/追偿原始金额保持不变
    r = settle_correction(_req(refund_premium='1000.00', recover_claim='999.99'))
    assert r['refund_premium'] == '1000.00' and r['recover_claim'] == '999.99'
    assert r['offset_balance'] == '999.99'
    # 后续加保缴费判断依据的是义务原值，不被净额改写
    assert Decimal(r['legs'][0]['debit']) == Decimal('1000.00')


def test_decimal_precision_no_float_error():
    r = settle_correction(_req(refund_premium='0.30', recover_claim='0.10'))
    assert r['net_amount'] == '0.20' and r['direction'] == DIR_PAY
    verify(r)


def test_negative_amounts_rejected():
    with pytest.raises(DomainError) as e:
        settle_correction(_req(refund_premium='-1.00'))
    assert e.value.code == 'AMOUNT'
    with pytest.raises(DomainError):
        settle_correction(_req(recover_claim='-1.00'))


def test_missing_required_field_rejected():
    with pytest.raises(DomainError):
        settle_correction(_req(claim_id=''))
    with pytest.raises(DomainError):
        settle_correction(_req(correction_id='  '))


def test_tampered_net_amount_breaks_verification():
    r = settle_correction(_req())
    r['net_amount'] = '999.99'
    with pytest.raises(DomainError) as e:
        verify(r)
    assert e.value.code == 'LEDGER'


def test_lost_leg_makes_net_unexplainable():
    r = settle_correction(_req())
    r['legs'] = [l for l in r['legs'] if l['leg'] != 'RECOVER_CLAIM']
    with pytest.raises(DomainError) as e:
        verify(r)
    assert e.value.code == 'LEDGER'


def test_tampered_leg_breaks_verification():
    r = settle_correction(_req())
    r['legs'][0]['debit'] = '0.01'
    with pytest.raises(DomainError):
        verify(r)


def test_settle_status_transition_and_retry_blocked():
    r = settle_correction(_req())
    mark_settled(r)
    assert r['status'] == STATUS_SETTLED
    with pytest.raises(DomainError) as e:
        mark_settled(r)
    assert e.value.status == 409 and e.value.code == 'ALREADY_SETTLED'


def test_direction_mismatch_rejected():
    r = settle_correction(_req())
    r['direction'] = DIR_COLLECT
    with pytest.raises(DomainError):
        verify(r)
