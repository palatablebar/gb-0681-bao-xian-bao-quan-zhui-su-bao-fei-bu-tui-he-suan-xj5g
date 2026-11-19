# 深度登记 保险保全追溯与保费补退核算

规模：中型。核心规则：按保全有效日拆分责任区间，撤销回滚指向原变更；实收补退按日精确分配尾差。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 保单采信 | 查看并复算保单采信 | `/api/m1_ingest` | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py` |
| M2 责任映射 | 查看并复算责任映射 | `/api/m2_mapping` | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py` |
| M3 保全选版 | 查看并复算保全选版 | `/api/m3_versions` | `app/modules/m3_versions/rules.py` | `tests/test_versions.py` |
| M4 承保切片 | 查看并复算承保切片 | `/api/m4_segments` | `app/modules/m4_segments/rules.py` | `tests/test_segments.py` |
| M5 保费重算 | 查看并复算保费重算 | `/api/m5_calculate` | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py` |
| M6 补退对账 | 查看并复算补退对账 | `/api/m6_reconcile` | `app/modules/m6_reconcile/compare.py` | `tests/test_reconcile.py` |
| M7 结案归档 | 查看并复算结案归档 | `/api/m7_archive` | `app/modules/m7_archive/export_out.py` | `tests/test_archive.py` |
| M8 更正净结算 | 追溯减保更正的退款/追偿净额裁决 | `/api/m8_settlement` | `app/modules/m8_settlement/rules.py` | `tests/test_settlement_rules.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030中文 | 固定UTF-8失败 | 按编码链回退 | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_legacy_encoding` |
| TRAP-02 | 表头BOM | 首列键失配 | 去除BOM | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_bom` |
| TRAP-03 | 引号内换行 | 分行误计 | csv逻辑记录 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_preserves_multiline_note` |
| TRAP-05 | 列数错位 | 静默补列 | WIDTH隔离 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_width_isolation` |
| TRAP-06 | 全角千分位 | float失败 | NFKC与Decimal | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_thousands` |
| TRAP-07 | 空值哨兵 | 空值当零 | MISSING隔离 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_missing_numeric` |
| TRAP-08 | 前导零ID | 编码失真 | 字符串保形 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_identifier_zeroes` |
| TRAP-10 | 浮点误差 | 累计失准 | Decimal求和 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_decimal_precision` |
| TRAP-14 | 同版冲突 | 最后覆盖 | 整组隔离 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_real_conflict_quarantine` |
| TRAP-18 | 三种空态 | 除零或伪成功 | 三个错误码 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_empty_three_states` |
| TRAP-26 | 旧版本复核 | 覆盖新结论 | 事务条件更新 | `app/store.py` | `tests/test_archive.py::test_stale_review` |
| TRAP-23 | 生效日端点 | 双计生效日 | 半开区间 | `app/modules/m4_segments/rules.py` | `tests/test_segments.py::test_halfopen_conservation` |
| TRAP-24 | 分币尾差 | 段合计不等 | 最大余额法 | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py::test_largest_remainder_conserves` |
| TRAP-30 | 退款追偿直接冲抵 | 改写原始义务，污染后续加保缴费态 | 义务独立建账，抵扣仅在结算层 | `app/modules/m8_settlement/rules.py` | `tests/test_settlement_rules.py::test_original_obligation_amounts_never_mutated_by_netting` |
| TRAP-31 | 同号更正重试 | 重复退款/重复追偿 | correction_id 唯一键 + 条件更新恰好一次 | `app/modules/m8_settlement/repository.py` | `tests/test_settlement_repo.py::test_settle_is_once_only` |
| TRAP-32 | 替换失败 | 半新半旧、旧版丢失 | 单事务显式 BEGIN/ROLLBACK，失败保留旧版 | `app/modules/m8_settlement/repository.py` | `tests/test_settlement_repo.py::test_replace_failure_restores_and_keeps_old_version` |
| TRAP-33 | 义务腿丢失 | 净额无法解释仍结案 | 读出即守恒校验，损坏拒绝导出并标注 BROKEN | `app/modules/m8_settlement/repository.py` | `tests/test_settlement_repo.py::test_lost_leg_record_is_detected_not_silently_explained` |
| TRAP-34 | sqlite3 静默 BEGIN | 回滚误并已提交事务 | autocommit 连接 + 显式事务边界 | `app/store.py::autocommit_connection` | `tests/test_settlement_repo.py` |

## 对账规则

输入行数=采纳+隔离+空行+重复表头+完全重复；业务总数与计算证据独立求和。隔离行保留来源行、ID、原因码与原值。

## 更正净结算规则（M8）

追溯减保生效后，已付赔案对应的一段保障不再成立：公司应退多收保费（REFUND_PREMIUM），同时向赔案追回赔款（RECOVER_CLAIM）。两笔义务**独立建账、各自挂来源**，追偿腿保留原赔案号，理赔侧原赔案与追偿记录始终可追。抵扣（NETTING）只在结算层以 `min(应退,应追)` 体现，不回写任何原始义务金额，因此同一保单随后加保的缴费状态不会被错误冲抵。

唯一净结算 = 应退保费 − 应追赔款：为正→方向「退款」（净付），为负→「追偿」（净收），为零→「两清」无划款。守恒式：`净付 − 净收 = 应退 − 应追`，且同一结算不得同时存在应付与应收。状态由「待结清」（赔案已付、更正未结清）经条件更新迁移到「已结清」，重试返回 409，不重复退款或追偿；已结清记录锁定不可替换。义务腿缺失或被篡改时守恒校验失败，结案 CSV 拒绝导出。

## 已知不覆盖

不做多租户权限和外部服务联调。行业规则以项目数据字典为准。文件上限2MB，逐行解析后在上限内完成分组计算。
