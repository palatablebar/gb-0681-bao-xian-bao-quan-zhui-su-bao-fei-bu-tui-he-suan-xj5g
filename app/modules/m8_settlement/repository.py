"""更正结算的持久层。

关键保证：
- correction_id 是唯一幂等键：同一更正重试只返回同一记录，不会产生第二笔
  退款或追偿（DB 唯一约束 + 条件更新双保险）。
- 替换(save 新版本)在单事务内完成：显式 BEGIN，任一步失败 ROLLBACK 并保留旧版。
- 结清用条件更新 ... WHERE status='待结清'，并发/重试下恰好一次。
- 义务腿独立建表，结算记录与明细同生共死，丢失明细则守恒校验拒绝导出。

事务控制：统一使用 autocommit 连接(isolation_level=None)并显式
BEGIN/COMMIT/ROLLBACK，杜绝 sqlite3 默认模式的静默 BEGIN 把独立写操作
并入后续回滚。
"""
import json
from app.errors import DomainError
from app.store import autocommit_connection as _connect
from app.modules.m8_settlement.rules import verify, mark_settled, STATUS_OPEN, STATUS_SETTLED


def init_settlement_tables(db):
    db.execute('''CREATE TABLE IF NOT EXISTS settlements(
        correction_id TEXT PRIMARY KEY,
        body TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT '待结清')''')
    db.execute('''CREATE TABLE IF NOT EXISTS settlement_legs(
        correction_id TEXT NOT NULL,
        leg TEXT NOT NULL,
        claim_id TEXT NOT NULL,
        debit TEXT NOT NULL,
        credit TEXT NOT NULL,
        memo TEXT NOT NULL,
        PRIMARY KEY(correction_id, leg))''')


def _row_to_dict(r):
    body = json.loads(r['body'])
    body.update(version=r['version'], status=r['status'])
    return body


def get_settlement(db, correction_id):
    r = db.execute('SELECT * FROM settlements WHERE correction_id=?', (correction_id,)).fetchone()
    if not r:
        raise DomainError('NOT_FOUND', '更正记录不存在', 404)
    record = _row_to_dict(r)
    legs = db.execute('SELECT leg,claim_id,debit,credit,memo FROM settlement_legs '
                      'WHERE correction_id=? ORDER BY rowid', (correction_id,)).fetchall()
    record['legs'] = [dict(x) for x in legs]
    verify(record)          # 读出即校验：明细丢失/被篡改立即暴露，净额不可凭空解释
    return record


def save_settlement(record):
    """幂等保存。返回 (记录, 是否新建)。

    已存在同号更正：
    - 仅当仍是「待结清」且内容确有变化时，在同一事务内用新版替换，
      失败回滚、旧版原样保留；
    - 内容相同则原样返回（重试不重复建账）；
    - 已结清记录不可再被替换（义务已有去向，禁止改写历史）。
    """
    verify(record)
    cid = record['correction_id']
    db = _connect()
    try:
        init_settlement_tables(db)
        existing = db.execute('SELECT * FROM settlements WHERE correction_id=?', (cid,)).fetchone()
        if not existing:
            db.execute('BEGIN')
            db.execute('INSERT INTO settlements(correction_id,body,version,status) VALUES(?,?,?,?)',
                       (cid, json.dumps(record, ensure_ascii=False), 1, record['status']))
            _replace_legs(db, cid, record['legs'])
            db.commit()
            created = True
        else:
            old = _row_to_dict(existing)
            if old['status'] == STATUS_SETTLED:
                raise DomainError('SETTLED_LOCKED', '更正已结清，不得替换或重复结算', 409)
            if _same_business(old, record):
                return get_settlement(db, cid), False
            try:
                # 单事务替换：body、版本号、义务腿一起换，异常整体回滚，旧版保留
                db.execute('BEGIN')
                db.execute('UPDATE settlements SET body=?, version=version+1 WHERE correction_id=?',
                           (json.dumps(record, ensure_ascii=False), cid))
                _replace_legs(db, cid, record['legs'])
                db.commit()
            except DomainError:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                raise DomainError('REPLACE_FAILED', '替换失败，已恢复并保留旧版', 409)
            created = False
        return get_settlement(db, cid), created
    finally:
        db.close()


def _replace_legs(db, cid, legs):
    db.execute('DELETE FROM settlement_legs WHERE correction_id=?', (cid,))
    db.executemany('INSERT INTO settlement_legs(correction_id,leg,claim_id,debit,credit,memo) '
                   'VALUES(?,?,?,?,?,?)',
                   [(cid, l['leg'], l.get('claim_id', ''), l['debit'], l['credit'], l['memo'])
                    for l in legs])


def _same_business(old, new):
    keys = ('policy_id', 'claim_id', 'refund_premium', 'recover_claim',
            'offset_balance', 'net_payable', 'net_receivable', 'net_amount', 'direction')
    return all(str(old.get(k)) == str(new.get(k)) for k in keys)


def settle(correction_id):
    """执行唯一净结算。条件更新保证恰好一次结清。"""
    db = _connect()
    try:
        init_settlement_tables(db)
        record = get_settlement(db, correction_id)
        if record['status'] == STATUS_SETTLED:
            raise DomainError('ALREADY_SETTLED', '更正已结清，重试不得重复退款或追偿', 409)
        db.execute('BEGIN')
        cur = db.execute("UPDATE settlements SET status=? WHERE correction_id=? AND status=?",
                         (STATUS_SETTLED, correction_id, STATUS_OPEN))
        if cur.rowcount != 1:
            db.rollback()
            raise DomainError('CONFLICT', '结清条件已变化，请刷新后重试', 409)
        mark_settled(record)      # 内存态迁移并再次守恒校验
        db.execute('UPDATE settlements SET body=? WHERE correction_id=?',
                   (json.dumps(record, ensure_ascii=False), correction_id))
        db.commit()
        return get_settlement(db, correction_id)
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


def list_settlements():
    db = _connect()
    try:
        init_settlement_tables(db)
        rows = db.execute('SELECT correction_id FROM settlements ORDER BY rowid DESC').fetchall()
        records = []
        for r in rows:
            try:
                records.append(get_settlement(db, r['correction_id']))
            except DomainError:
                # 明细损坏的记录不静默丢弃：列入清单并标注，使净额无法解释这一事实可见
                broken = db.execute('SELECT * FROM settlements WHERE correction_id=?',
                                    (r['correction_id'],)).fetchone()
                item = _row_to_dict(broken)
                item['legs'] = []
                item['integrity'] = 'BROKEN'
                records.append(item)
        return records
    finally:
        db.close()
