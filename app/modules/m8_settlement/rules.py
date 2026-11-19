"""追溯减保更正的净额结算规则（纯领域层，不接触数据库）。

裁决原则：
1. 应退保费(refund)与应追赔款(recovery)是两笔相互独立的义务，各自建账、
   保留来源赔案，禁止互相改写原始金额——理赔侧的原赔案与追偿记录始终可追。
2. 唯一净结算 = 应退保费 - 应追赔款，只生成一条带方向的结算指令；
   抵扣(netting)只体现在结算层，不回写到任何一笔义务上，避免污染同一保单
   随后加保的缴费状态。
3. 全部金额用 Decimal 计算并量化到分；守恒式可由记录独立重算，
   任一明细丢失都会使守恒失败、净额无法被解释。
"""
from decimal import Decimal, ROUND_HALF_UP
from app.errors import DomainError

CENT = Decimal('0.01')
# 结算状态：待结清（赔案已付、更正未结清）→ 已结清（各项义务均有去向）
STATUS_OPEN = '待结清'
STATUS_SETTLED = '已结清'
# 结算方向：公司对外付款 / 向收款方追回 / 两清无划款
DIR_PAY = '退款'
DIR_COLLECT = '追偿'
DIR_FLAT = '两清'


def _money(value, field):
    try:
        d = Decimal(str(value))
    except Exception:
        raise DomainError('AMOUNT', '%s 必须是数值' % field)
    if d.is_nan() or d.is_infinite():
        raise DomainError('AMOUNT', '%s 数值非法' % field)
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def _require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise DomainError('PARAMETERS', '%s 不能为空' % field)
    return value.strip()


def settle_correction(correction):
    """根据一笔更正请求，生成义务账、抵扣与唯一净结算指令。

    入参 correction 必含：
      correction_id 更正号(幂等键)、policy_id 保单号、
      claim_id 已付赔案号、
      refund_premium 应退保费(>=0)、recover_claim 应追赔款(>=0)。
    返回不可再被改写的结算快照（金额为两位小数字符串）。
    """
    cid = _require_text(correction.get('correction_id'), 'correction_id')
    pid = _require_text(correction.get('policy_id'), 'policy_id')
    claim_id = _require_text(correction.get('claim_id'), 'claim_id')
    refund = _money(correction.get('refund_premium'), 'refund_premium')
    recovery = _money(correction.get('recover_claim'), 'recover_claim')
    if refund < 0:
        raise DomainError('AMOUNT', '应退保费不能为负')
    if recovery < 0:
        raise DomainError('AMOUNT', '应追赔款不能为负')

    # 净额：正=公司净付，负=公司净收，零=两清。抵扣余额即被抵销的部分。
    net = refund - recovery
    offset = min(refund, recovery)              # 实际抵扣余额
    if net > 0:
        direction, net_payable, net_receivable = DIR_PAY, net, Decimal('0.00')
    elif net < 0:
        direction, net_payable, net_receivable = DIR_COLLECT, Decimal('0.00'), -net
    else:
        direction, net_payable, net_receivable = DIR_FLAT, Decimal('0.00'), Decimal('0.00')

    # 独立义务账：两条腿各自挂来源，原赔案与追偿互不覆盖。
    legs = [
        {'leg': 'REFUND_PREMIUM', 'source': cid, 'claim_id': '',
         'debit': str(refund), 'credit': '0.00', 'memo': '追溯减保应退保费'},
        {'leg': 'RECOVER_CLAIM', 'source': cid, 'claim_id': claim_id,
         'debit': '0.00', 'credit': str(recovery), 'memo': '已付赔案追偿(%s)' % claim_id},
        {'leg': 'NETTING', 'source': cid, 'claim_id': claim_id,
         'debit': str(offset), 'credit': str(offset), 'memo': '退款与追偿抵扣，不回写原始义务'},
    ]
    record = {
        'correction_id': cid,
        'policy_id': pid,
        'claim_id': claim_id,
        'status': STATUS_OPEN,
        'refund_premium': str(refund),
        'recover_claim': str(recovery),
        'offset_balance': str(offset),
        'net_payable': str(net_payable),
        'net_receivable': str(net_receivable),
        'net_amount': str(net.copy_abs()),
        'direction': direction,
        'legs': legs,
        'version': 1,
    }
    verify(record)          # 落账前自证守恒，防止生成无法解释的净额
    return record


def verify(record):
    """守恒校验：用义务腿独立重算净额。任一明细缺失/篡改即拒绝。

    refund = recovery + 净付 - 净收（按方向带符号），
    且 应退、应追、抵扣、净付、净收、方向之间必须自洽。
    """
    try:
        refund = Decimal(record['refund_premium'])
        recovery = Decimal(record['recover_claim'])
        offset = Decimal(record['offset_balance'])
        payable = Decimal(record['net_payable'])
        receivable = Decimal(record['net_receivable'])
        amount = Decimal(record['net_amount'])
    except (KeyError, TypeError, ValueError):
        raise DomainError('LEDGER', '结算记录缺失，净额无法解释')

    legs = record.get('legs')
    if not legs or len(legs) != 3:
        raise DomainError('LEDGER', '义务腿缺失，净额无法解释')
    leg_refund = next((l for l in legs if l['leg'] == 'REFUND_PREMIUM'), None)
    leg_recover = next((l for l in legs if l['leg'] == 'RECOVER_CLAIM'), None)
    leg_netting = next((l for l in legs if l['leg'] == 'NETTING'), None)
    if not (leg_refund and leg_recover and leg_netting):
        raise DomainError('LEDGER', '义务腿缺失，净额无法解释')
    if Decimal(leg_refund['debit']) != refund or Decimal(leg_recover['credit']) != recovery:
        raise DomainError('LEDGER', '义务腿与汇总不符，净额无法解释')

    net = refund - recovery
    # 守恒式：净额必须能由两笔独立义务推出
    if payable - receivable != net:
        raise DomainError('LEDGER', '净额与义务账不平衡')
    if amount != net.copy_abs():
        raise DomainError('LEDGER', '结算金额与净额不符')
    if offset != min(refund, recovery):
        raise DomainError('LEDGER', '抵扣余额计算错误')
    expect_dir = DIR_PAY if net > 0 else DIR_COLLECT if net < 0 else DIR_FLAT
    if record['direction'] != expect_dir:
        raise DomainError('LEDGER', '支付方向与净额不符')
    if payable > 0 and receivable > 0:
        raise DomainError('LEDGER', '同一结算不得同时存在应付与应收')
    return True


def can_settle(record):
    """仅待结清更正可执行结清；已结清重试必须被识别为重复。"""
    return record.get('status') == STATUS_OPEN


def mark_settled(record):
    """状态迁移：待结清 → 已结清。重复结清被拒绝，杜绝重复退款/追偿。"""
    if record.get('status') == STATUS_SETTLED:
        raise DomainError('ALREADY_SETTLED', '更正已结清，重试不得重复退款或追偿', 409)
    if record.get('status') != STATUS_OPEN:
        raise DomainError('STATUS', '结算状态非法')
    verify(record)
    record['status'] = STATUS_SETTLED
    return record
