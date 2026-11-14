# ADR-HSF-017：uplink 上级上报采用契约化 stub（V-3 红线）——存量补立

| 项 | 内容 |
|---|---|
| 编号 | ADR-HSF-017（R-17 存量补立，2026-11-14；决策实际发生于 2026-11-08 三期设计冻结，此前以 `CONVENTIONS-P3.md` V-3 / docs/08 §3 显式边界声明留痕，未出 ADR——本件补立） |
| 状态 | 已采纳（Accepted，2026-11-08 三期冻结；补立 2026-11-14） |
| 关联 | docs/24 §4 R-24（payload 空间矢量增强，本 ADR 契约的 v1.1→v1.2 演进，见 ADR-HSF-024）、R-17（补立）、`CONVENTIONS-P3.md` V-3、docs/08 §3（D-08-2 payload 契约/D-08-3 状态机）、ADR-HSF-001（"面积重算以登记值为准"口径延续） |
| 决策日 | 2026-11-08（三期设计冻结）；补立 2026-11-14 |

## 背景

- 对标①差距④：数字乡村最优实践为"上图入库按计划/实施/验收三阶段数据表组织并逐级审核；与国土空间
  一张图互联互通"——真实监管平台联调是最终闭环形态。
- 集群事实（V-3）：**监管平台在内网不可达**——无联调对象、无回执协议样例、无鉴权对接材料。
- 上报链路需完整交付"统一编码→上报粒度映射（L/P/F/S→PARCEL/PROJECT/FACILITY/SENSOR）→上报任务状态机
  （PENDING→SENT→ACKED/REJECTED，uk_batch 幂等）→payload 完整导出 JSON 归档"以证明通道可用。

## 决策

1. uplink 采用**契约化 stub**：上报 payload 按统一编码完整组装并可导出 JSON 归档（`GET
   /tasks/{batch}/payload`）；回执由 stub 按**确定性规则**生成（`validate_payload` 完整性校验通过→ACKED，
   缺字段→REJECTED，同 payload 同 checksum 回执幂等——`services/uplink/app/transport.py`
   StubTransport）；
2. `transport.py` 冻结为**真实平台唯一替换点**（HTTP 客户端单文件），状态机/幂等/重试逻辑不因替换改动
   （V-3 原文冻结）；
3. P3-3 收口（2026-11-14）：payload items 只允许四类——非统一编码设备映射 OTHER 后**排除出 items**，
   以 `summary.EXCLUDED` 与顶层 `excludedOther` 如实计数（不静默丢弃），stub 校验规则不变（防御纵深）；
4. 真实平台联调为**触发条件项**（docs/24 §6 授权依赖专节：平台侧联调窗口），联调时仅替换 transport 单文件；
   payload 契约演进（R-24 geoWkt/schemaVersion 1.2）见 ADR-HSF-024——geoWktHint 字段保留兼容。

## 影响

- 诚实边界：上报通道以 stub 闭环，回执语义=stub 规则而非真实平台协议；对接协议（鉴权/加密/重试码表）
  在真实联调前为**未定义项**，不预先虚构。
- 交付形态：E2E 用例 23（payload 导出+stub 回执+状态闭环）验收；payload JSON 可归档审计。
- 状态机冻结（D-08-3）：PENDING→SENT→ACKED/REJECTED，uk_batch 幂等；stub 不绕过状态机直接写终态。
- 回退：不适用（transport 单文件替换即切换真实平台；stub 保留为测试回退路径）。
- 验证方式：E2E 用例 23；pytest `services/uplink/tests/` 全 PASS（回执确定性/校验规则/状态机迁移）；
  payload 导出体积与完整性记录（R-24 验收口径）。
