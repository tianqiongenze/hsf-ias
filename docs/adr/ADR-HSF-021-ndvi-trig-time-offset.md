# ADR-HSF-021：NDVI 预警 trig_time 地块序号微秒偏移（R-13 方案 b）

| 项 | 内容 |
|---|---|
| 编号 | ADR-HSF-021（R-17 补登——docs/24 §4 R-13"方案决策以 ADR 留痕（联动 R-17）"） |
| 状态 | 已采纳（Accepted，2026-11-14 实施并验收） |
| 关联 | docs/24 §4 R-13（复核附录#12：同景多地块降幅预警静默丢失）、R-14（字段语义重载声明）、R-15（重标定样本联动）、R-17（补登）、docs/08 §2.3（alert_record 字段语义重载声明）、`services/iot/app/repo.py:32-38`（冻结 DDL 注释）、`services/rs/app/repo.py:189-196`（INSERT IGNORE+NOT EXISTS 实现） |
| 决策日 | 2026-11-14 |

## 背景

- 库级唯一键 `uk_rule_reading (sensor_code, rule_code, trig_time)`（`services/iot/app/repo.py:49` 冻结 DDL，
  **存量零变更红线**不许改）覆盖 rs 侧 parcel 粒度的 NOT EXISTS 判重——同景多地块降幅时三元组全同，
  仅落首条，多地块预警静默丢失。
- 方案候选：a) sensor_code 内嵌地块标识（CHAR(20) 容不下 scene(32)+parcel，弃）；b) trig_time 按地块序号
  偏移微秒（幂等键内嵌 parcel，不改库）；c) 冻结口径声明"每景每规则单条预警"（丢失多地块明细语义）。

## 决策（三选一选 b）

**方案 b**：`trig_time = 触发读数 recorded_at + 地块序号微秒偏移`（景内 parcel_code 排序 ×1000μs/地块，
`("RS-"+scene_code)[:20]` 合成源不变）——幂等键内嵌地块标识，**不改 DDL**（D-08-1"存量零变更只新增"
红线不触碰），多地块降幅各落一条预警。

## 影响

- **显式偏离声明**：trig_time 偏离 `services/iot/app/repo.py:37` 冻结注释"触发读数的 recorded_at
  （幂等键组成部分）"原语义——重载口径（"recorded_at + 地块序号微秒偏移"）以 docs/08 §2.3 字段语义
  重载声明覆盖（R-14①②），`iot/app/repo.py` 冻结注释行同步追加（注释级，不改 DDL）。
- 语义边界：偏移上限=地块数×1000μs（数千地块 <5ms），不影响任何时间语义消费方（按天/按景聚合口径）。
- 回退：还原 `services/rs/app/repo.py` 偏移逻辑即回单条语义（无库变更，无需数据迁移）。
- 验证方式：构造双地块降幅样本 → `GET /alerts?rule_code=RULE_NDVIFALL` 两条且 parcel_code 各异；
  pytest `services/rs/tests/test_alert_granularity.py` PASS；E2E 用例 21 断言加固回归。
