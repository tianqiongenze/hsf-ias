# ADR-HSF-002：时序方案——TiDB `hsf_ts`（一期不引入 TimescaleDB/TDengine）

> 迁移登记（R-17，2026-11-14）：正文自 `hsf/docs/02` §3.2 原样迁移。**影响列更新（R-17 同批）**：
> 二期（2026-11-08）已引入 TimescaleDB 承接 `hsf_ts`——本 ADR"不引入 TimescaleDB"结论**已被推翻**，
> 推翻事实与细节见 README §3 注记、`CONVENTIONS-P2.md` T-1/T-2、docs/07 §1.2、docs/24 §4 R-17 现状行；
> 本文件保留一期决策原文以存决策轨迹，状态列不改写（历史决策不可追改）。

| 项 | 内容 |
|---|---|
| 状态 | 已采纳（一期冻结；二期演进见影响列注记）｜ 决策日期 2026-11-07 ｜ 决策人：架构设计师（工作流） |
| 背景 | ① 原文章要求"时序库（墒情/气象实时）"；② 集群事实：无 TimescaleDB/TDengine（理由 R1 同 ADR-001）；③ 一期数据量：48 台模拟传感器×5min tick ≈ **1.4 万条/日**，年内 <600 万条（`hsf/docs/01` NFR-07）；④ 写入目标 ≥1 万条/s（NFR-04）为**压测能力口径**而非日常负载 |
| 决策 | 1. 独立库 `hsf_ts`，核心表 `sensor_reading`（列结构承 `CONVENTIONS.md` §2.13：`sensor_code CHAR(20)`、`metric VARCHAR(16)`、`value DECIMAL(10,3)`、`recorded_at DATETIME(3)`、`KEY idx_code_time(sensor_code, recorded_at)`；主键实现冻结为 `id BIGINT AUTO_RANDOM(5) PRIMARY KEY + PRE_SPLIT_REGIONS=4`——对 §2.13"无显式主键"口径的**显式偏离**，偏离声明见 04 号 §5.1，更符合 §2.10"自增主键"建表自检项）；2. 写入：sim 服务批量 INSERT **1000 条/批**（`hsf_common.db.execute_many`）；3. `SHARD_ROW_ID_BITS=4 PRE_SPLIT_REGIONS=8` 规约（§2.10）**仅适用于无显式主键大表**——`sensor_reading` 已有显式主键，不适用该选项；4. 保留策略：在线 ≥180 天，超期按 04 号文档归档脚本转储 |
| 理由（编号） | R1 集群无该组件，新增=有状态服务+运维（同 ADR-001 R1）；R2 一期日常写入 1.4 万条/日，TiDB 容量与吞吐余量两个数量级以上；R3 万条/s 峰值能力：DV-IAS 同集群已完成 1.058 亿行时序批量写入融合验证（commit e149e3c、`docs/15`），批量写模式可平移，HSF 以 NFR-04 基准压测复验；R4 `idx_code_time(sensor_code, recorded_at)` 覆盖"按设备取最新值/时间窗"两类高频查询（FR-502 `/sim/latest`、告警规则扫描）；R5 与业务库同引擎，运维技能/备份/监控一套 |
| 备选与不选理由 | ① **TimescaleDB**：PostgreSQL 系，需新建库+压缩/连续聚合特性在 2 节点共享集群的收益不成比；② **TDengine**：独立时序引擎，新增有状态服务与备份链路；③ **Pulsar+TiDB 管道**：一期无流式消费诉求，先落库再查询即可（与 ADR-HSF-004 一致）；④ 以上均登记为二期真实 IoT 接入时的评估项 |
| 影响与限制（诚实声明） | ① 无降采样/连续查询/数据压缩等时序库专有能力——降采样在查询层 GROUP BY 时间桶实现，超长窗口查询性能依赖索引与缓存；② 单表容量护栏：>5 亿行/年时必须先做归档再谈扩容（04 号文档设计归档任务）；③ 写入吞吐依赖批大小，禁止单条 INSERT 高频写（sim 实现约定）。**二期演进注记（R-17 建库时补记）**：二期新增 hsf-timescale（TimescaleDB 2.17.2-pg16 StatefulSet）+ hsf-mosquitto（eclipse-mosquitto 2.1）两个 StatefulSet，`hsf_ts` 时序写入职责移交 iot 服务——本 ADR 两个备选项之一在二期真实 IoT 接入触发器下落地，推翻"不引"结论；新增有状态服务触发器（docs/02 §9）在目录建立前已发生，按存量豁免以差异声明+manifest 头注留痕，不倒序补立编号（README §3 注记） |
| 演进触发器 | 真实设备接入且峰值 >5 万点/s；需要连续聚合/降采样物化；hsf_ts 单表 >5 亿行——满足其一启动 TimescaleDB/TDengine 二期 ADR（**已触发并执行**，见上注记） |
| 验证方式 | NFR-04 基准压测（≥1 万条/s×60s，0 失败、无死锁/锁冲突）；FR-502（tick 后 latest 单调递增）；FR-503（4 条规则边界值单测 14.9/15.1、84.9/85.1、16.9/17.1、79.9/80.1） |
