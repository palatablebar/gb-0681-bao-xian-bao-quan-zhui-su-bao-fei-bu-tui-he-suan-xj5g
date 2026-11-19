from app.modules.m8_settlement.rules import settle_correction
from app.modules.m8_settlement import repository


def create(correction):
    """受理一笔更正并形成唯一净结算（初始为待结清）。重试幂等。"""
    record = settle_correction(correction)
    saved, _ = repository.save_settlement(record)
    return saved


def close(correction_id):
    """执行结清：各项义务自此有去向，重复调用被拒绝。"""
    return repository.settle(correction_id)
