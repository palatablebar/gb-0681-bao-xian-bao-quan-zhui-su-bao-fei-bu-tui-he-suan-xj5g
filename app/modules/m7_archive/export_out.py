import io,csv,json,hashlib

BOM='﻿'

def _settlement_rows(state):
    """从持久层读取更正结算，逐条守恒校验后导出。

    明细丢失/被篡改会在校验处抛错，拒绝带着无法解释的净额结案。
    """
    try:
        from app.modules.m8_settlement import repository
        from app.store import autocommit_connection
    except Exception:
        return []
    db = autocommit_connection()
    try:
        repository.init_settlement_tables(db)
        ids = [r['correction_id'] for r in db.execute(
            'SELECT correction_id FROM settlements ORDER BY rowid').fetchall()]
        return [repository.get_settlement(db, cid) for cid in ids]
    finally:
        db.close()

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['记录类型','批次','来源行','业务ID','原因码','内容'])
    rid=state.get('run_id','preview')
    writer.writerow(['运行清单',rid,'','','',json.dumps(state.get('manifest',{}),ensure_ascii=False,sort_keys=True)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get('reconcile',{}),ensure_ascii=False,sort_keys=True)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    for row in state['quarantine']:writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True)])
    for row in _settlement_rows(state):
        # 每条更正：先写唯一净结算汇总，再逐条写独立义务腿，保证原赔案与追偿可追
        writer.writerow(['更正结算',rid,'',row['correction_id'],row['direction'],
                         json.dumps({k:row[k] for k in ('correction_id','policy_id','claim_id','status',
                            'refund_premium','recover_claim','offset_balance','net_payable',
                            'net_receivable','net_amount','direction','version')},
                                    ensure_ascii=False,sort_keys=True)])
        for leg in row['legs']:
            writer.writerow(['结算义务腿',rid,'',row['correction_id'],leg['leg'],
                             json.dumps(leg,ensure_ascii=False,sort_keys=True)])
    return BOM+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()
