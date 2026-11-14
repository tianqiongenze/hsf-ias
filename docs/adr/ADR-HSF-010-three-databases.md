# ADR-HSF-010：三库分离与双账号（迁移登记）

> 迁移登记（R-17，2026-11-14）：自 `hsf/docs/02` §3.5"既定决策登记表"迁移为独立 ADR 文件（四要素齐备）。
> 冻结来源即决策依据，不再论证。

| 项 | 内容 |
|---|---|
| 状态 | 已采纳（一期冻结）｜ 决策日期 2026-11-07（§3.5 编号固化）｜ 决策人：架构设计师（工作流） |
| 背景 | 主数据（地块/设施/组织）、业务数据（项目/工单/告警/审计）、时序读数三类数据访问模式与保留策略不同；集群不存在 `tidb-read` 只读别名主机名（CONVENTIONS §2.9 一期真实事故）；读写分离需在应用层双连接池实现 |
| 决策 | 三库分离：`hsf_md`(主数据) / `hsf_biz`(业务) / `hsf_ts`(时序)；账号 `hsf_app`(三库读写=write_pool) / `hsf_ro`(三库只读=read_pool)；双连接池 host 均为 `tidb.infra.svc.cluster.local:4000`（**不存在任何只读别名主机名**） |
| 理由 | 库级隔离对齐数据保留/备份/权限差异；同实例跨库 SELECT 消除分布式调用失败面（ADR-HSF-004 R3）；`hsf_ro` 供只读链路限流兜底 |
| 影响与限制 | Secret `hsf-secrets` 键名冻结（docs/04 §7.3：DB_HOST_WRITE/DB_HOST_READ/DB_USER/DB_USER_RO/DB_PASS/DB_PASS_RO）；`hsf_ts` 二期写职责移交 TimescaleDB（hsf_ts TiDB 库保留归档口径，CONVENTIONS-P2 T-1），本 ADR 账号/双池口径不变 |
| 验证方式 | static_gate 检查项 1（tidb-read 主机名 0 命中）；CONVENTIONS §2.9 |
