# HSF 架构决策记录（ADR）索引

> 依据：`hsf/docs/02-技术选型与总体架构.md` §9"架构治理"（目录承诺：编号 HSF-001 起步、§3 即 ADR 正文、
> 正式目录在实施期建库——本目录即该承诺的落地，2026-11-14 由 R-17 建库）。
> 体例：背景 / 决策 / 影响 三要素为最低要求（迁移登记件补"状态"共四要素），格式对齐 docs/02 §3
> （状态/背景/决策/理由/备选与不选理由/影响与限制/演进触发器/验证方式）与 DV-IAS `docs/adr/` 惯例。

## 1. 治理规则（激活条款，R-17 收口写入）

| 规则 | 内容 |
|---|---|
| 触发器（docs/02 §9 冻结，三项任一发生 → **先出 ADR 再实施**） | ① 新增有状态服务；② 跨子图新实体；③ NodePort 变更 |
| 契约冻结 | SDL（docs/05）+ DDL（docs/04）+ 状态机转移表（docs/03 §6）为唯一事实源，改动必须走 ADR |
| 编号 | 顺序递增不复用；**编号空缺如实保留**（见 §3 缺号说明），禁止倒序补位 |
| 审查对象 | 冻结清单增补（Secret 键/env/端口/DDL）、推翻既有 ADR、跨服务语义重载（如 alert_record 字段重载） |
| 存量豁免 | 差异声明 / manifest 头注 / 复核附录记载不替代 ADR——自本目录建立起一律以 ADR 留痕 |

## 2. 目录文件索引

| 编号 | 标题 | 状态 | 文件 |
|---|---|---|---|
| ADR-HSF-001 | GIS：TiDB 存 GeoJSON + 前端纯 SVG 渲染 | 已采纳 | `ADR-HSF-001-gis-tidb-geojson-svg.md` |
| ADR-HSF-002 | 时序：TiDB hsf_ts（不引 TimescaleDB/TDengine） | 已采纳（部分被 016 系列 Rejected→见 018 注记） | `ADR-HSF-002-timeseries-tidb.md` |
| ADR-HSF-003 | sim 独立 REST 服务（非 GraphQL 子图） | 已采纳 | `ADR-HSF-003-sim-rest.md` |
| ADR-HSF-004 | 一期无消息队列：Outbox 表 + 联邦引用 | 已采纳 | `ADR-HSF-004-outbox-no-mq.md` |
| ADR-HSF-010 | 三库分离 + hsf_app/hsf_ro 双账号 | 已采纳 | `ADR-HSF-010-three-databases.md` |
| ADR-HSF-011 | 命名空间/端口/NodePort 冻结 | 已采纳 | `ADR-HSF-011-namespaces-ports.md` |
| ADR-HSF-012 | 镜像仓库与 BASE 镜像 | 已采纳 | `ADR-HSF-012-image-registry.md` |
| ADR-HSF-013 | 缓存独立集群 hsf-redis 3 节点 | 已采纳 | `ADR-HSF-013-redis-cluster.md` |
| ADR-HSF-014 | 联邦实体四件套跨子图同名 | 已采纳 | `ADR-HSF-014-federation-entities.md` |
| ADR-HSF-015 | 依赖锁定版本 | 已采纳 | `ADR-HSF-015-dependency-pins.md` |
| ADR-HSF-016 | rs 遥感合成样本源（V-2 红线） | 已采纳（存量补立，决策日 2026-11-08） | `ADR-HSF-016-rs-synthetic-source.md` |
| ADR-HSF-017 | uplink 契约化 stub 上报（V-3 红线） | 已采纳（存量补立，决策日 2026-11-08） | `ADR-HSF-017-uplink-stub.md` |
| ADR-HSF-018 | mosquitto 最小授权 sim/iot 读写分离（R-20） | 已接受 | `ADR-HSF-018-mosquitto-minimal-acl.md` |
| ADR-HSF-019 | JWT 寿命 2h（R-08 决策 A） | 已采纳 | `ADR-HSF-019-jwt-ttl-2h.md` |
| ADR-HSF-020 | 设备接入框架正式编号 D-08-4 + deviceCode S 前缀契约回写（R-16） | 已采纳 | `ADR-HSF-020-device-code-s-prefix.md` |
| ADR-HSF-021 | NDVI 预警 trig_time 地块序号微秒偏移（R-13 方案 b） | 已采纳 | `ADR-HSF-021-ndvi-trig-time-offset.md` |
| ADR-HSF-022 | SM4 字段加密密钥外置与轮换（R-06） | 已采纳 | `ADR-HSF-022-sm4-field-key.md` |
| ADR-HSF-023 | 运行时密钥治理：真值出库 + fail-closed + prod/dev 分值（R-07） | 已采纳 | `ADR-HSF-023-secret-governance.md` |
| ADR-HSF-024 | 上报 payload 空间矢量增强 geoWkt + schemaVersion 1.2（R-24） | 已采纳 | `ADR-HSF-024-payload-geowkt-v1.2.md` |
| ADR-HSF-025 | 限流滑动窗口可选演进 RATE_MODE2（R-25） | 已采纳（默认仍固定窗口） | `ADR-HSF-025-ratelimit-sliding-window.md` |
| ADR-HSF-026 | GraphQL 深度/复杂度护栏（推翻 D-05-8，R-26） | 已采纳 | `ADR-HSF-026-query-depth-guard.md` |
| ADR-HSF-027 | JWT RS256 非对称签名演进（R-31） | 已采纳（HS256 兼容开关保留） | `ADR-HSF-027-jwt-rs256.md` |

## 3. 编号空缺说明（如实留档，禁止补位）

- **HSF-005 ~ HSF-009 无决策内容**：docs/02 §3 仅成文 001~004，§3.5"既定决策登记表"编号固化自
  010 起——005~009 为登记表建立时的跳号空缺，从未承载决策。按"编号顺序递增不复用"规则保留空缺，
  不回填、不复用（避免两份文档对同一编号给出不同语义）。
- **HSF-002 与 ADR-HSF-016 系列的关系注记**：docs/24 §4 R-17 原计划由"TimescaleDB+Mosquitto 引入"
  决策占用 HSF-016（推翻 ADR-HSF-002"不引 TimescaleDB"）。该推翻事实由本目录登记为
  **018 注记 + 002 影响列更新**（"TimescaleDB 已于二期引入，推翻'不引'结论，细节见
  docs/07 §1.2 / CONVENTIONS-P2 T-1/T-2 / docs/24 R-17 现状行"），不再单开编号——二期引入决策
  以差异声明+manifest 头注留痕在先（2026-11-08），按存量豁免口径不倒序补立新编号；本目录建立后
  同类变更必须新开编号（见 §1 审查对象）。
- gw NodePort 30090→30099 顺延（docs/24 R-17 原拟 HSF-017 事项）：实际记录以
  `hsf-ias/k8s/20-services.yaml:6-8` 头注 + docs/16 处 R-16 修复为准（2026-11-07 决策），ADR-011
  影响列同步更新，不倒序补立。

## 4. 迁移登记表（HSF-001~015，从 docs/02 §3/§3.5 迁移，正文见对应文件）

| 编号 | 决策一句话 | 冻结来源（不再论证） |
|---|---|---|
| ADR-HSF-001 | GIS：TiDB 存 GeoJSON + 前端纯 SVG 渲染（vendored 轻量库增强） | `CONVENTIONS.md` §2.12、docs/02 §3.1 |
| ADR-HSF-002 | 时序：TiDB `hsf_ts`（不引 TimescaleDB/TDengine）**[二期已推翻"不引"，见 §3 注记]** | `CONVENTIONS-P2.md` T-1/T-2、docs/02 §3.2 |
| ADR-HSF-003 | sim 为独立 REST 服务（FastAPI，非 GraphQL 子图） | docs/05 §0 D-05-3、docs/02 §3.3 |
| ADR-HSF-004 | 一期无消息队列：领域事件=Outbox 表 + 联邦引用 | docs/02 §3.4、docs/03 §4.6 |
| ADR-HSF-010 | 三库分离 `hsf_md`/`hsf_biz`/`hsf_ts`；账号 `hsf_app`(rw)/`hsf_ro`(ro)；双池 host 同为 `tidb.infra.svc.cluster.local:4000` | `CONVENTIONS.md` §1/§2.9、docs/00 C2 |
| ADR-HSF-011 | 命名空间 hsf-prod/hsf-dev；子图端口 8091~8095；gw 8090+9090；web 80；NodePort gw 30099/web 31210/sim 31211（`ss -tln` 核验，冲突顺延并记录） | `CONVENTIONS.md` §1、docs/00 C6 |
| ADR-HSF-012 | 镜像 `10.167.2.175:30500/hsf/<svc>:<tag>`，BASE=`szxc/base:1.2` | `CONVENTIONS.md` §0/§1、docs/00 C5 |
| ADR-HSF-013 | 缓存独立集群 `hsf-redis` 3 节点（infra，`base/redis:8-alpine`，headless `hsf-redis-h`） | `CONVENTIONS.md` §1、docs/00 C3 |
| ADR-HSF-014 | 联邦实体四件套跨子图同名：`Party`(@key id)/`LandParcel`(@key code)/`FarmProject`(@key code)/`Facility`(@key code)；Schema 显式 types 列表 | `CONVENTIONS.md` §1/§2.4 |
| ADR-HSF-015 | 依赖锁定 `pydantic==2.10.6`、`strawberry-graphql==0.254.1`、`graphql-core==3.2.5` 等（完整清单见 `CONVENTIONS.md` §2.1） | `CONVENTIONS.md` §2.1 |

> 二期/三期增补决策（TimescaleDB/Mosquitto/ratelimit-svc/iot/rs/uplink 引入与镜像 tag 口径）冻结于
> `CONVENTIONS-P2.md` T-1/T-2、`CONVENTIONS-P3.md` U/V/X/Y——以约定文件为冻结源，不逐一拆分 ADR 编号；
> 其后触犯触发器的**行为级变更**自 018 起逐件立卷（018 mosquitto ACL、024 geoWkt、025 滑窗、
> 026 深度护栏、027 RS256）。

## 5. 待登记跟踪

| 决策 | 状态 |
|---|---|
| R-01（flusher 多线程化/FLUSH_WORKERS） | 属阶段 1 性能专项执行方职责——env 增补不触冻结清单变更时以 docs/24 实施记录留痕；若实施中改写 docs/07 §1.2 冻结语义须补立 ADR（本目录届时顺延编号） |
| R-03（broker $SYS 采集） | $SYS 采集侧（BrokerSysMonitor 复用 iot 进程）已按"新增组件走 ADR"评估：未新增独立组件，以 docs/08 §2.3 + iot 源码注释留痕；若后续引入独立采集 sidecar 须补立 ADR |
| R-23（k6 判据调整） | 冻结判据未变更（CONVENTIONS.md:83 / CONVENTIONS-P3.md:46 X-2 维持）；如确认方批准调整，按 docs/02 §9 出 ADR 并回写两处约定 |
