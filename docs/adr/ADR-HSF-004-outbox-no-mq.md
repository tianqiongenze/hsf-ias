# ADR-HSF-004：一期无消息队列——领域事件=Outbox 表 + 联邦引用

> 迁移登记（R-17，2026-11-14）：正文自 `hsf/docs/02` §3.4 原样迁移（唯一事实源仍为 docs/02）。

| 项 | 内容 |
|---|---|
| 状态 | 已采纳（一期冻结；与 `hsf/docs/00` §4.2"一期无 RAG/图谱/事件总线诉求"一致）｜ 决策日期 2026-11-07 ｜ 决策人：架构设计师（工作流） |
| 背景 | DV-IAS 跨上下文协作靠 Pulsar（`docs/04 §5`：Outbox+Pulsar 至少一次投递）。HSF 一期盘点跨上下文交互后：**读侧** 6 条协作全部可用联邦 `@key` 引用解决（`03` §4.6 矩阵），**写侧**仅两条弱异步诉求（审计、事件留痕），均可同事务落库解决——不存在需要 broker 的消费者 |
| 决策 | 1. 领域事件统一**事务性写入** `hsf_biz.domain_event`（Outbox 表：id/topic/payload JSON/created_at/published_at）；2. 跨上下文**读**协作一律 GraphQL 联邦引用（编码存本方表、对象经 `_resolve_reference` 跨子图解析）；3. 审计由各子图在**同一写事务**内 INSERT `hsf_biz.audit_log`（不依赖事件消费）；4. 事件 topic 命名预留 `hsf.<ctx>.<entity>.<verb-past>`（对齐 DV-IAS `szxc.*` 规范，`docs/04 §6`），AsyncAPI 登记于 05 号文档 |
| 理由 | R1 一期事件消费者集合为空，引入 Pulsar=有 broker 无订阅者的"半套 MQ"（运维成本无收益）；R2 Outbox 表已保留事件语义与完整审计轨迹，二期上 Pulsar 时仅需增加**投递器**（轮询 published_at IS NULL → 发 Pulsar → 置位），业务代码零改动；R3 同事务落审计保证 FR-704"写操作必留痕"的强一致（消息队列做不到 at-least-once 之外的即时可见） |
| 备选与不选理由 | ① Pulsar（集群已有）：无消费者，纯成本；② 进程内事件总线：跨副本不可见，语义弱于 Outbox 表；③ 同步 REST 调用链（如 project 创建时调 land 校验）：**不选跨服务 HTTP 同步调用**，改用同 TiDB 实例跨库 SELECT 校验（见 `03` §4.6/§7——`hsf_app` 对三库均有权限，`CONVENTIONS.md` §3.2 冻结），消除分布式调用失败面 |
| 影响与限制 | 事件无实时订阅方 → "事件驱动"在退化为"事件记录"；任何需要实时联动的新需求（如告警自动生成工单）必须先立项升级投递器，不允许绕过 Outbox 私拉 HTTP。**三期沿用注记**：rs 解译失败事件仍走 domain_event 留痕（`services/rs/app/main.py:93` 注释、docs/08 §2.2 ⑤），未引入 Pulsar |
| 演进触发器 | 二期真实 IoT 接入（设备上行→规则→多消费者）即引入 Pulsar+投递器，topic 命名已预留（二期实际以 MQTT broker 承接设备上行，Pulsar 未启用——MQTT 语义见 CONVENTIONS-P2 T-2/ADR-HSF-018） |
| 验证方式 | 每个写 mutation 后 `domain_event` 新增 1 行（单测断言）；审计同事务断言（FR-704） |
