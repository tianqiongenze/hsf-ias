# ADR-HSF-024：上报 payload 空间矢量增强——geoWkt 完整 WKT 与 schemaVersion 1.2（R-24）

| 项 | 内容 |
|---|---|
| 编号 | ADR-HSF-024（R-17 建库同批立卷；决策日 2026-11-14） |
| 状态 | 已采纳（Accepted，2026-11-14 实施并单测验收） |
| 关联 | docs/24 §4 R-24（对标①差距④：上图入库按计划/实施/验收三阶段组织并逐级审核、与国土空间一张图互联互通）、ADR-HSF-017（uplink stub 契约，本件为其 payload 契约 v1.1→v1.2 演进）、ADR-HSF-001（面积以登记值为准/应用层几何校验深度口径）、docs/08 §3.1（D-08-2 契约增补回写）、D-08-2/D-08-3 冻结语义 |
| 决策日 | 2026-11-14 |

## 背景

- 对标①差距④：数字乡村最优实践要求上报数据"按计划/实施/验收三阶段数据表组织并逐级审核；与国土空间
  一张图互联互通"；当前 payload（v1.1）PARCEL 项仅 `geoWktHint`（前 3 点截断提示，非全量几何）——
  上级平台无法据提示上图。
- V-3 红线不变：真实监管平台内网不可达（触发条件项），本决策只对齐 payload 契约本身。
- D-08-2 契约演进纪律：新增字段属冻结清单变更，按 docs/02 §9 立卷。

## 决策

1. **PARCEL 项增 `geoWkt` 完整闭合环 WKT**：`land_parcel.geo_json` 全量顶点序列化（点数=源数据+闭合补点，
   首点≠尾点时补首点闭合——RFC 7946 允许未闭合环，WKT 环必须显式闭合）；`geoWktHint` **保留兼容**
   （v1.0/1.1 消费方零影响）；
2. **schemaVersion 1.1→1.2**，顶层增 `phases` 三阶段批次元数据**字段预留**：planPhase/buildPhase/
   acceptPhase 各含 `status`（审核状态位，预留 PENDING）+ `summary`（阶段摘要，预留 null）——**状态机
   不动**（D-08-3 PENDING→SENT→ACKED/REJECTED 语义不变），真实平台接入时由 transport 替换方按平台
   协议填充；
3. **stub 完整性校验同步扩展**（`validate_payload`）：schemaVersion 白名单（1.0/1.1 历史归档重发放行）、
   1.2 必含 phases 结构、PARCEL 须含 geoWkt/geoWktHint 至少一项（全缺=数据缺陷 → REJECTED 留痕，与
   P3-3 防御纵深同原则）、geoWkt 须闭合环（≥4 位置且首尾相同）；
4. **体积限值 8MiB**（`MAX_PAYLOAD_BYTES`）：超限 REJECTED 促成分批上报而非带病超载；分批/压缩机制列为
   真实平台联调触发项（地块数千级场景），本期仅限值+留痕（种子样本实测 579 bytes）；
5. 回退开关=装配层开关关闭新字段（docs/24 R-24 风险行）——实现上以 `geoWkt` 为纯新增字段，回退即
   build_payload 移除该键（单文件单行，无库变更）。

## 影响

- 兼容性：纯新增字段+版本号演进，D-08-2 既有字段语义零变更；历史归档 payload（1.0/1.1）重发经白名单
  放行。
- 数据面：无 geo_json 的 PARCEL（两项全空）将被 stub REJECTED——按"上图入库"语义这是数据质量门而非
  兼容性破坏（REJECTED 附明确原因，可修复后重报）。
- 验收：E2E 用例 23 payload 导出 `geoWkt` 含闭合环且点数=源数据；stub 回执 ACKED 回归；payload 体积
  记录在案（<8MiB，单测打印留痕）；pytest `services/uplink/tests/` 全 PASS（17 条，其中 R-24 新增 10 条）。
- 回退：装配层移除 geoWkt/phases 两键 + schemaVersion 回 1.1（需推翻本 ADR，新编号登记）。
