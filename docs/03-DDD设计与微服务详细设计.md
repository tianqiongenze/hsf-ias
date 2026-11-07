# 03 HSF 高标准农田信息平台 — DDD 设计与微服务详细设计

> 版本：v1.0 ｜ 编制日期：2026-11-07 ｜ 文档状态：**评审稿（随 00 号确认后冻结；状态机转移表与枚举名以本文 §4/§6 = 05 号 §5.2/§6.2 冻结表为准，DDL 列名/枚举存储值以 04 号为准，SDL 以 05 号为准——本文为其 DDD 视图，三者已对齐）**
> 方法：事件风暴（Event Storming）→ 限界上下文映射 → 聚合/实体/值对象/领域事件 → 状态机转移表 → 服务详细设计（Schema/DDL/缓存/权限）→ 测试映射（承继 DV-IAS `docs/04` 方法，按 HSF 五上下文重做）
> 编制依据：`hsf/CONVENTIONS.md`、`hsf/docs/00/01/02`、DV-IAS `docs/04-DDD领域设计与微服务详细设计`（工单四态引擎写法实证于 `dv-ias/services/governance/app/repo.py` `transition()` 模式）

---

## 1. 领域划分：核心域/支撑域/通用域

| 上下文 | 域类型 | 判定理由 | 一期对应服务 |
|---|---|---|---|
| **project 项目全周期** | 核心域 | 农田建设监管的业务主线（立项到绩效），规则最复杂（7 态状态机+关联地块+监理），是甲方核心价值 | project :8093（GraphQL 子图） |
| **land 地块主数据** | 核心域 | "家底"所在：统一编码体系+一张图数据源，被 project/facility 引用 | land :8092（GraphQL 子图） |
| **facility 设施管护** | 支撑域 | 有独立闭环（台账/二维码/工单 4 态）但规则复用 DV-IAS 已验证工单引擎 | facility :8094（GraphQL 子图） |
| **sim 时序与告警** | 支撑域 | 一期为模拟数据源+规则引擎；二期换真实数据源时**仅换数据入口，规则引擎与告警域不变**（`hsf/docs/00` §7 IoT 行） | sim :8095（REST，ADR-HSF-003） |
| **iam 身份与组织** | 通用域 | 认证/组织/RBAC/审计，全平台共性能力 | iam :8091（GraphQL 子图） |

## 2. 事件风暴摘要（域事件时间线）

```
[通用域 iam]      org.changed ──► party.created                             （组织/主体建档，为各域提供主体）
[核心域 land]     parcel.registered ──► parcel.updated                      （地块建档→属性维护，8 个种子地块）
[核心域 project]  project.created ──► submitted ──► approved ──► construction.started
                                        ──► supervision.started ──► accepted ──► performed   （7 态主线）
[支撑域 facility] facility.registered ──► workorder.created ──► accepted ──► dispatched ──► resolved ──► approved  （工单闭环）
[支撑域 sim]      device.registered ──► reading.batched ──► alert.raised ──► alert.handled ──► alert.closed  （感知→告警→处置）
```

事件登记规约（ADR-HSF-004）：全部事件**事务性写入** `hsf_biz.domain_event`（Outbox），topic 命名 `hsf.<ctx>.<entity>.<verb-past>`（对齐 DV-IAS `docs/04 §6` 的 `szxc.*` 规范）；一期无实时订阅方，事件表承担**留痕+二期投递器数据源**双重职责。完整事件字典见 §4.7。

## 3. 限界上下文映射图（Context Map）

```
                          ┌────────────────────────┐
                          │ iam（通用域·开放主机OHS）│  Party(@key id)  Account/Org
                          └───────△────────△───────┘
                     holder 联邦引用│        │supervisor 联邦引用 / 上报·处置人 user_id
                          ┌────────┴──┐  ┌──┴──────────┐
        parcels 编码引用──►│ land      │◄──│ project     │    （U=上游 D=下游：land 是 U，project/facility 是 D）
        parcel 编码引用──► │ (核心域)   │   │ (核心域)     │
                          └───────────┘   └─────────────┘
                                ▲ parcel 编码引用（facility 台账外联地块）
                          ┌─────┴─────────┐
                          │ facility(支撑域)│ Facility(@key code)  WorkOrder
                          └───────────────┘
                          ┌───────────────┐
                          │ sim(支撑域)     │  告警按 village_code/sensor_code 关联，无跨上下文实体引用
                          └───────────────┘
外部：无（一期零外部系统，无 ACL 防腐层；与 DV-IAS 的跨平台互查为二期联邦预留，`hsf/docs/00` §2）
```

| # | 上游 → 下游 | 关系模式 | 协作机制 | 契约物 |
|---|---|---|---|---|
| 1 | iam → land/project/facility | **OHS（开放主机服务）+ Published Language** | 联邦实体 `Party`(@key id)，下游只存 `party_id`，姓名等经 `_resolve_reference` 跨子图解析 | SDL（05 号） |
| 2 | land → project / facility | **Customer–Supplier（land 为 U）** | 下游仅存 `parcel_code`（CHAR(20) 编码引用，无物理外键）；创建时跨库 SELECT 校验存在性（§7） | 编码规则 ^L\d{10}$ |
| 3 | sim → 各域 | **事件（发布者）** | `hsf.sim.alert.*` 写 Outbox；告警记录自带 village_code/sensor_code，下游一期直接读 alert_record（同库 hsf_biz） | alert_record DDL |
| 4 | 各域 → iam（审计/主体校验） | **同库共享（hsf_md）** | 跨库 SELECT 校验账号/组织存在性；审计同事务 INSERT | audit_log DDL |
| 5 | HSF ↔ DV-IAS | **联邦互查（预留）** | 一期仅保留 `@key` 模式一致性，不建立运行时连接 | 二期 ADR |

**关键设计纪律**：① 跨上下文**不建物理外键**（TiDB 外键为实验特性 + 联邦引用已承担对象图关联），引用完整性=应用层跨库校验+`uk_*` 唯一键；② 跨上下文**不做跨服务 HTTP 同步调用**（`hsf_app` 对三库均有授权，`CONVENTIONS.md` §3.2 冻结，同实例跨库 SELECT 即可）；③ 下游永远只存**编码/ID 标量**，不冗余上游属性（杜绝主数据漂移）。

---

## 4. 各上下文战术设计

### 4.1 iam 身份与组织（:8091）

**聚合与实体**

| 类型 | 名称 | 关键属性 | 说明 |
|---|---|---|---|
| 聚合根 | `Organization` | code(主键, CHAR(16))、name、level(COUNTY/TOWNSHIP/VILLAGE/KEEP_UNIT/COOP)、parent_code | 县-乡-村三级 + 管护主体/合作社两类主体组织（04 号 §3.1 冻结：code 自然主键，列宽预留——评审 R6）；种子 ≥9 节点（FR-701） |
| 聚合根 | `Party` | id、name、phone_hash(SM3)+phone_enc(SM4-GCM)、org_code、role | **联邦实体** `Party`(@key id)；JWT `sub=str(party.id)`（04 号 §3.2"JWT sub 即本表 id"；PyJWT≥2.10 要求字符串，`CONVENTIONS.md` §2.1）；登录标识=phone_hash 等值检索 |
| 聚合根 | `Account` | id、party_id(uk, 1:1)、password_salt、password_hash(SM3-PBKDF2)、status | 登录凭据（04 号 §3.3 冻结）；status=active/locked 仅作**手动停用标志**（一期无锁定/解锁/停用接口，见下与 DDD-A3） |
| 值对象 | `Role`、`PermissionScope` | role∈{admin,town,village,keeper,coop}；scope∈{county,township,village,facilities,parcels} | 角色↔数据范围默认映射见 §5.7 |
| 值对象 | `Phone`、`MaskedPhone` | 校验+SM4 加密+展示脱敏 mask_phone | 复用 `hsf_common.crypto` |
| 实体 | `AuditLog`（只追加） | user_id、user_name、action、object_type/object_id、ip、result、created_at | FR-704；仅插入（04 号 §4.8 冻结；应用层纪律：代码无 UPDATE/DELETE 路径，static_gate 检查） |

**领域事件**：`hsf.iam.org.changed`、`hsf.iam.party.created`、`hsf.iam.party.updated`

**账号状态（一期口径，对齐 05 号 §3.1 冻结契约）**：一期 iam Mutation 仅 `login`（05 号 §3.1；§3.2 注明组织/账号维护走种子脚本），**不实现自动锁定/解锁/停用接口**——`account.status`（active/locked）仅作为运维经种子/DB 手动置位的停用标志；连续登录失败自动锁定（原 3 态状态机 NORMAL/LOCKED/DISABLED）与账号管理 mutation 列二期（假设登记见 §10 DDD-A3）。错密/停用账号登录一律返回 `UNAUTHENTICATED`（HTTP 200 + extensions.code，05 号 D-05-2）并记审计 login_fail。

**不变量**：① party.phone_hash 全局唯一（uk_phone，登录标识）；② 一个 Account 对应一个 Party（1:1，uk_account_party）；③ 口令永不明文落库/落日志（SM3-PBKDF2 派生）；④ party.role 单值且受五角色枚举约束。

**数据表**（hsf_md）：`org`、`party`、`account`；hsf_biz：`audit_log`。DDL 要点见 §5.1（与 04 号 §3.1~3.3/§4.8 一致）。

**GraphQL 契约要点**（SDL 全文 05 号，冻结方；§4.1 契约表已与 05 号 §3.1/§3.2 同步——评审 R6/R16）：

| 类别 | 成员 | 权限 |
|---|---|---|
| Query | `orgTree`、`party(id)`、`parties(orgCode,page,size)`、`me`、`auditLogs(userId,action,timeFrom,timeTo,page,size)`（仅 admin，FR-704 过滤查询） | 数据范围过滤（05 号 §2.4） |
| Mutation | `login` | 公开（账号维护走种子脚本，05 号 §3.2） |

### 4.2 land 地块主数据（:8092）

**聚合与实体**

| 类型 | 名称 | 关键属性 | 说明 |
|---|---|---|---|
| 聚合根 | `LandParcel` | code(uk, ^L\d{10}$)、name、village_code、holder_id、area_mu、soil_type、crop_type、geo_json(JSON, WGS84 Polygon)、status | **联邦实体** `LandParcel`(@key code)；列名对齐 04 号 §3.4 冻结；`holder` 为联邦引用字段（解析到 Party.name，FR-104） |
| 值对象 | `ParcelCode` | 正则 `^L\d{10}$`（11 位：L+6 位区划+4 位序号） | 列宽 CHAR(20) 预留（`CONVENTIONS.md` §2.3） |
| 值对象 | `Geometry` | GeoJSON Polygon 结构校验（type/coordinates 非空、环闭合抽查） | 应用层校验，无拓扑校验（ADR-HSF-001 限制 ③） |

**领域事件**：`hsf.land.parcel.registered`、`hsf.land.parcel.updated`

**状态机（地块生命周期，存储值对齐 04 号 §3.4/05 号 §4.1 冻结枚举 ACTIVE/RETIRED）**

```
ACTIVE(在册，登记即在册,默认) ──retire(admin)──► RETIRED(退役,终态,预留)
```
- 种子地块直接 ACTIVE；RETIRED 地块不出现在一张图与按村默认列表（显式传参才可查）；退役不可逆，仅 admin。
- **一期无流转接口**：05 号 §4.1 冻结 land Mutation 仅 `registerParcel`，属性修订（update）与退役（retire）列二期；原「REGISTERED→ACTIVE→INACTIVE」3 态口径废止（评审 R2 同类枚举漂移修正）。

**不变量**：① code 全局唯一不可变（uk_code）；② village_code 必须存在于 org（跨库校验）；③ holder_party_id 可空（未确权），非空时必须存在于 party；④ geojson 必须为合法 Polygon。

**数据表**（hsf_md）：`land_parcel`（uk_code、idx_village、geojson JSON 列）。DDL 见 §5.2。

**GraphQL 契约要点**（SDL 全文 05 号，冻结方）：

| 类别 | 成员 | 权限 |
|---|---|---|
| Query | `landParcel(code)`（联邦入口之一）、`landParcels(villageCode,page)`（数据范围强制）、`landGeojson(villageCode)`（FeatureCollection）、`landStats(villageCode)`（地块总数/总面积聚合，FR-203 落点） | 登录读（范围过滤按 05 号 §2.4：keeper 为空集） |
| Mutation | `registerParcel` | admin / village(本村) |

> `updateParcel`/`deactivateParcel` 不在一期 SDL（05 号 §4.1 冻结仅 registerParcel），属性修订与退役列二期。

**缓存**：`hsf:land:parcel:{code}`(300s)、`hsf:land:geojson:{villageCode}`(600s)（04 号 §8.2 键 3/2）；写后 `invalidate_prefix("hsf:land:geojson:")` + 地块详情单键（失效映射 04 号 §8.5）。

### 4.3 project 项目全周期（:8093）

**聚合与实体**

| 类型 | 名称 | 关键属性 | 说明 |
|---|---|---|---|
| 聚合根 | `FarmProject` | code(uk, ^P\d{10}$)、name、build_year、content、applicant_org_code、supervisor_id、state、acceptance_note(验收归档结论)、created_at/updated_at | **联邦实体** `FarmProject`(@key code)；列名对齐 04 号 §4.1 冻结；`supervisor` 联邦引用 Party（FR-304）；`parcels` 联邦引用 [LandParcel]（FR-301） |
| 实体 | `ProjectParcel` | project_code、parcel_code | 多对多关联表，uk_proj_parcel |
| 实体 | `ProjectTransition`（只追加） | project_id、action(存储值=mutation 名，冻结清单见 §6/05 号 §5.2)、from_state、to_state、actor_id、note、created_at | 每次流转留痕（FR-302"记录操作人与时间"；列名对齐 04 号 §4.3） |
| 值对象 | `AcceptanceArchive` | 验收结论文本(note→acceptance_note)+归档时间+操作人 | 验收归档（FR-305）内容：`acceptanceProject`(SUPERVISED→ACCEPTED) 可携带结论，`acceptanceArchive`(ACCEPTED→PERFORMED) 必填 note 落 acceptance_note（05 号 §5.1/§5.2 冻结） |

**领域事件**：`hsf.project.created`、`hsf.project.submitted`、`hsf.project.approved`、`hsf.project.construction.started`、`hsf.project.supervision.started`、`hsf.project.accepted`、`hsf.project.performed`

**状态机（项目全周期 7 态，冻结口径——枚举名与 `hsf/docs/01` §4.3 一致）**

```
RESERVED(储备) ──submitProject──► SUBMITTED(申报) ──approveProject──► APPROVED(审批) ──startConstruction──► CONSTRUCTING(施工)
    ──startSupervision──► SUPERVISED(监理) ──acceptanceProject──► ACCEPTED(验收) ──acceptanceArchive──► PERFORMED(绩效,终态)
```

| # | 动作（mutation，05 号 §5.2 冻结命名） | from → to | 允许角色 | 留痕 |
|---|---|---|---|---|
| 1 | createProject | — → RESERVED | admin/town | transition 不适用，写 domain_event+audit |
| 2 | submitProject | RESERVED → SUBMITTED | town(本乡镇申报)/admin | ✓ |
| 3 | approveProject | SUBMITTED → APPROVED | admin | ✓ |
| 4 | startConstruction | APPROVED → CONSTRUCTING | admin | ✓ |
| 5 | startSupervision | CONSTRUCTING → SUPERVISED | admin | ✓ |
| 6 | acceptanceProject | SUPERVISED → ACCEPTED | admin | ✓（note 可选，随留痕落库） |
| 7 | acceptanceArchive | ACCEPTED → PERFORMED | admin | ✓（note 必填，入 acceptance_note，FR-305） |

- **合法转移共 6 条流转 mutation（创建行为不进转移矩阵），7 态×6 动作矩阵中其余组合全部非法**（FR-303：跨态或回退如 储备→施工、验收→申报 一律拒绝；动作命名与 05 号 §5.2 冻结表一致——评审 R5：原「acceptanceArchive=SUPERVISED→ACCEPTED / performProject=ACCEPTED→PERFORMED」为命名相反的笔误，已对齐 05 号）。非法流转返回业务错误码 `PROJECT_ILLEGAL_TRANSITION`，HTTP 200 + GraphQL error（错误码表 05 号），**不落库、不留痕、只记审计**。
- PERFORMED 为终态，无出边。

**不变量**：① 流转仅沿转移表，且操作人角色匹配；② 关联地块必须为 ACTIVE 且存在于 hsf_md.land_parcel（创建/变更时跨库校验）；③ supervisor_id 可空，非空必须存在于 party；④ PERFORMED 后所有 mutation 拒绝。

**数据表**（hsf_biz）：`farm_project`、`project_parcel`、`project_transition`。DDL 见 §5.3。

**缓存**：`hsf:proj:{code}`(300s，04 号 §8.2 键 4)；每次流转 mutation 后失效（失效映射 04 号 §8.5）。

### 4.4 facility 设施台账与巡检工单（:8094）

**聚合与实体**

| 类型 | 名称 | 关键属性 | 说明 |
|---|---|---|---|
| 聚合根 | `Facility` | code(uk, ^F\d{10}$)、type∈{WELL 机井/PUMP_STATION 泵站/CANAL 渠道/ROAD 道路}、name、parcel_code(可空外联地块)、village_code、build_year、status(NORMAL/BROKEN/REPAIRING/RETIRED)、qr_token | **联邦实体** `Facility`(@key code)；类型/状态枚举列名对齐 04 号 §3.5 冻结；`parcel` 联邦引用 LandParcel（FR-401） |
| 聚合根 | `WorkOrder` | id(自增)、sn(uk_wo_sn, WO+yyyyMMdd+6 位流水)、facility_code、village_code、description、contact_enc(SM4-GCM 密文)、reporter_id、assignee_id、state、resolve_note、perf_score、created_at/updated_at | 列名对齐 04 号 §4.4 `facility_workorder` 冻结（评审 R7：原 work_order 缺 sn、contact 明文均废止）；`reporter`/`assignee` 联邦引用 Party |
| 实体 | `WorkOrderTransition`（只追加） | wo_id、action(存储值=mutation 名，冻结清单见 §6)、from_state、to_state、actor_id、note、created_at | 4 态全留痕（FR-404；对齐 04 号 §4.5 `workorder_transition`） |
| 值对象 | `QrCode` | token=32 位随机 hex（secrets），URL=`<web>/m/facility?token=<t>` | FR-402：token 唯一（uk_qr_token）、校验通过直达设施详情；**二维码图片渲染一期可选/二期**——一期交付 token+URL（管理端复制张贴、移动端直达），图片生成方式（前端 vendored qrcode 库或服务端 SVG）未在一期冻结，落地时另立 ADR（FR-402 验收不测图片） |

**领域事件**：`hsf.facility.facility.registered`、`hsf.facility.workorder.created`、`hsf.facility.workorder.accepted`、`hsf.facility.workorder.dispatched`、`hsf.facility.workorder.resolved`、`hsf.facility.workorder.approved`

**状态机（巡检工单 4 态，冻结口径）**

> 口径说明（对齐 `hsf/docs/00` §3"工单 4 态"与 `hsf/docs/01` FR-403"accept→dispatch→resolve→approve"）：**4 态 = 受理/派单/办结/销项四个业务状态**；创建动作产出初始态 `OPEN(待受理)`，不计入 4 态。FR-602 断言"移动端提交后工单以受理态出现在列表"即 OPEN 态可见。转移动作名与 DV-IAS 工单引擎一致（`CONVENTIONS.md` §1"复用 accept→dispatch→resolve→approve 状态机"）。

```
OPEN(待受理,初始) ──accept──► ACCEPTED(受理) ──dispatch──► DISPATCHED(派单) ──resolve──► RESOLVED(办结) ──approve──► APPROVED(销项/闭环,终态)
```

| # | 动作（mutation，05 号 §6.1/§6.2 冻结命名） | from → to | 允许角色 | 必填 | 留痕 |
|---|---|---|---|---|---|
| 1 | reportWorkorder（扫码/台账/移动端；FR-602 移动上报复用本 mutation） | — → OPEN | keeper/village/town/admin | facilityCode+description（contact 可选，SM4-GCM 密文落库） | domain_event+audit |
| 2 | acceptWorkorder | OPEN → ACCEPTED | village/town/admin | — | ✓ |
| 3 | dispatchWorkorder | ACCEPTED → DISPATCHED | town/admin | assigneeId（keeper） | ✓ |
| 4 | resolveWorkorder | DISPATCHED → RESOLVED | **assignee 本人或 admin** | note 必填（办结反馈，FR-403） | ✓ |
| 5 | approveWorkorder | RESOLVED → APPROVED | village/town/admin | —（绩效积分字段可空，FR-404） | ✓ |

- 非法流转（跳步/回退/终态再动）返回 `WORKORDER_ILLEGAL_TRANSITION`，不落库只记审计；单测对 **5 态×4 动作**转移矩阵全覆盖（评审 R21⑨ 统一口径；受理/办结角色按 05 号 §6.2 冻结表对齐——评审 R8①②）。
- keeper 数据范围=所辖设施（FR-702）：仅能 resolve 自己被派的单、查看本人工单。

**不变量**：① facility_code 必须存在于 hsf_md.facility；② dispatch 目标 assignee 必须是 keeper 角色账号；③ resolve 必须携带反馈内容（≥1 字符）；④ 终态 APPROVED 后无出边；⑤ 每态变更记录操作人与时间（FR-404）。

**数据表**（hsf_md）：`facility`；hsf_biz：`facility_workorder`、`workorder_transition`。DDL 见 §5.4。

**缓存**：`hsf:fac:{code}`(300s)、`hsf:fac:qr:{token}`(600s)（04 号 §8.2 键 5/6）；设施登记/状态变更失效；工单列表不缓存（写多读少+留痕即时性），村工单统计块 `hsf:stat:workorder:{villageCode}`(300s，键 9) 随流转失效。

### 4.5 sim 时序与告警（:8095，REST 服务）

**聚合与实体**

| 类型 | 名称 | 关键属性 | 说明 |
|---|---|---|---|
| 聚合根 | `SensorDevice` | sensor_code(uk, ^S\d{10}$，A-04-2 冻结)、metric∈6 类（04 号 §4.7 冻结：soil_moisture/air_temp/wind_speed/rainfall/**pest_index**/water_level）、village_code、parcel_code(可空)、status(ACTIVE/OFFLINE)、last_value/last_recorded_at(tick 直写) | 种子 6 类×2 台/村×4 村=48 台（FR-501）；落 `hsf_biz.sensor`（04 号 §1.2/§4.7 冻结，含 last_* 读优化受控冗余列，仅 sim 写——评审 R3：原 `hsf_ts.sensor_device` 口径废止） |
| 聚合根 | `AlertRecord` | id、rule_code、sensor_code、metric、trig_value、trig_time、village_code、parcel_code、state(OPEN/PROCESSING/CLOSED)、handled_by、handled_at、closed_at、note | 落 `hsf_biz.alert_record`（04 号 §4.6 冻结：列名/枚举值/uk_rule_reading 幂等键均以 04 为准——评审 R2）；**告警 3 态状态机**见下 |
| 实体 | `AlertRule` | rule_code(uk, RULE_DROUGHT/RULE_WATERLOGGING/RULE_GALE/RULE_PEST)、metric、op(<,>)、threshold、enabled | 一期 4 条（FR-503）：旱情(墒情<15)、渍害(墒情>85)、大风(风速>17)、虫害(虫情>80)；告警类型语义由 rule_code 承载（04/05 冻结口径，无独立 alert_type 列） |
| 实体 | `SensorReading`（无聚合语义，只追加） | id BIGINT AUTO_RANDOM(5) PRIMARY KEY、sensor_code CHAR(20)、metric VARCHAR(16)、value DECIMAL(10,3)、recorded_at DATETIME(3) | `hsf_ts.sensor_reading`，列结构承 `CONVENTIONS.md` §2.13；主键为 04 号 §5.1 冻结的 AUTO_RANDOM(5)+PRE_SPLIT_REGIONS=4（对 §2.13 的显式偏离，声明见 04——评审 R4）；批量 1000/批 INSERT |
| 领域服务 | `RuleEngine` | 纯函数：`(rule, reading) → AlertRecord?`，命中即同事务写 alert_record+Outbox | 边界值单测（14.9/15.1、84.9/85.1、16.9/17.1、79.9/80.1） |

**领域事件**：`hsf.sim.device.registered`、`hsf.sim.reading.batched`、`hsf.sim.alert.raised`、`hsf.sim.alert.handled`、`hsf.sim.alert.closed`

**状态机（告警 3 态，冻结口径——存储值对齐 04 号 §4.6/05 号 §7.1 冻结枚举 OPEN/PROCESSING/CLOSED，评审 R2；对齐 `hsf/docs/01` FR-504"待处置→处置中→已闭环"）**

```
OPEN(待处置,初始=alert.raised) ──handle──► PROCESSING(处置中) ──close──► CLOSED(已闭环,终态)
```

| # | 动作 | from → to | 允许角色 | 必填 | 留痕 |
|---|---|---|---|---|---|
| 1 | （规则引擎自动生成） | — → OPEN | 系统 | rule_code+sensor_code+trig_value+trig_time（uk_rule_reading 幂等） | Outbox+record 本身 |
| 2 | handle | OPEN → PROCESSING | village/town/admin | handled_by（自动取当前用户） | handled_at |
| 3 | close | PROCESSING → CLOSED | village/town/admin | note（处置反馈） | closed_at |

- 非法流转返回 `ALERT_ILLEGAL_TRANSITION`；CLOSED 终态。数据范围：按 village_code 过滤（FR-603：keeper/coop 仅见所辖范围）。
- **一期最低验收口径为"可查询、可统计"（FR-504）；告警状态推进接口 `POST /sim/alerts/{id}/transition` 冻结为二期 P2（05 号 A-05-2/§7.1 端点 6），一期不实现**——FR-504"状态推进留痕"验收项随 P2 豁免并在 20 号报告如实记录；本节状态机与存储枚举仍按 04/05 冻结值落地，为二期接口预留（评审 R11：原"handle/close 一期即提供"口径废止）。

**REST 契约要点**（OpenAPI 自动生成，接口表冻结于 05 号）：

| 方法与路径（05 号 §7.1 冻结，评审 R10 统一 `/sim` 前缀） | 作用 | 验收 |
|---|---|---|
| `POST /sim/tick` | 生成一轮观测值（48 台×1 条），批量 1000/批写 hsf_ts；规则引擎评估并写告警 | FR-502：tick 后 latest 单调递增 |
| `GET /sim/sensors` | 设备清单（48 台） | FR-501 |
| `GET /sim/latest` | 各设备最新值（快路径直读 `sensor.last_*`，idx_code_time 兜底） | FR-502 |
| `GET /sim/alerts?state=&villageCode=&page=&size=` | 告警分页查询（state∈OPEN/PROCESSING/CLOSED；CORS 允许 web 源） | FR-504/603 |
| `GET /sim/alerts/statistics?dim=` | 按类型/村/状态统计 | FR-505（与明细聚合一致） |
| `POST /sim/alerts/{id}/transition`（body：action=handle\|close） | 3 态推进——**二期 P2**（05 号 A-05-2） | FR-504 留痕（一期豁免） |

**数据表**：hsf_biz：`sensor`（含 last_* 冗余列）、`alert_rule`、`alert_record`；hsf_ts：`sensor_reading`（AUTO_RANDOM(5) PRIMARY KEY + PRE_SPLIT_REGIONS=4，04 号 §5.1）。DDL 见 §5.5。

### 4.6 跨上下文协作矩阵（事件 + 联邦引用，冻结）

**读侧：联邦引用（6 条，全部经网关组合，FR-104/304/401 验收）**

| # | 持有方(子图) | 字段 | 目标实体(@key) | 解析方 | 场景 |
|---|---|---|---|---|---|
| 1 | land | `LandParcel.holder` | Party(@key id) | iam | 地块权属人姓名（FR-104） |
| 2 | project | `FarmProject.supervisor` | Party(@key id) | iam | 项目监理人（FR-304） |
| 3 | project | `FarmProject.parcels` | [LandParcel](@key code) | land | 项目-地块关联（FR-301） |
| 4 | facility | `Facility.parcel` | LandParcel(@key code) | land | 设施所属地块（FR-401） |
| 5 | facility | `WorkOrder.reporter/assignee` | Party(@key id) | iam | 工单上报/派单人（留痕展示） |
| 6 | web（客户端聚合） | 页面统计角标 | — | gw 并发查询 land+sim | FR-203（landParcels.total + landStats.totalAreaMu + `/sim/alerts/statistics`） |

**写侧：领域事件（Outbox 一期语义，ADR-HSF-004）**

| 事件 topic | 生产者 | 一期"消费"落点 | 二期消费者（预留） |
|---|---|---|---|
| hsf.iam.party.created / hsf.iam.org.changed | iam | Outbox 留痕 | DV-IAS 联邦互查同步 |
| hsf.land.parcel.registered/updated | land | Outbox 留痕 | 一张图增量刷新、上级平台上报 |
| hsf.project.*（7 个流转事件） | project | Outbox 留痕+audit | 绩效统计、监管平台报送 |
| hsf.facility.workorder.*（5 个） | facility | Outbox 留痕+audit | 消息推送、绩效积分 |
| hsf.sim.alert.raised/handled/closed | sim | Outbox 留痕 | 告警自动生成工单（联动 facility）、推送 |

**跨库校验（写侧同步一致性的唯一手段）**：project/facility/sim 写路径中对上游编码做 `SELECT ... FROM hsf_md.land_parcel / hsf_md.party / hsf_md.org WHERE code/id IN (...)` 存在性校验，校验失败返回业务错误码（`PARCEL_NOT_FOUND` 等），不落库。此为**同 TiDB 实例跨库查询**，非跨服务调用（§3 纪律 ②）。

### 4.7 领域事件字典（Outbox 登记表）

| Topic | 生产者 | 载荷要点 | 一期消费方 |
|---|---|---|---|
| hsf.iam.party.created | iam | party_id,org_code | 留痕 |
| hsf.land.parcel.registered | land | parcel_code,village_code | 留痕 |
| hsf.land.parcel.updated | land | parcel_code,fields | 留痕 |
| hsf.project.created | project | project_code,parcels[] | 留痕 |
| hsf.project.submitted/approved/construction.started/supervision.started/accepted/performed | project | project_code,from,to,actor_id | 留痕+audit |
| hsf.facility.facility.registered | facility | facility_code,type,parcel_code | 留痕 |
| hsf.facility.workorder.created/accepted/dispatched/resolved/approved | facility | wo_id,facility_code,from,to,actor_id | 留痕+audit |
| hsf.sim.device.registered | sim | sensor_code,metric,village_code | 留痕 |
| hsf.sim.reading.batched | sim | batch_size,window | 留痕 |
| hsf.sim.alert.raised/handled/closed | sim | alert_id,rule_code,sensor_code,village_code | 留痕+audit |

（载荷 JSON Schema 与 AsyncAPI 登记归 05 号文档。）

---

## 5. 微服务详细设计

### 5.0 服务通用规约（全部子图一致）

| 项 | 规约 | 来源 |
|---|---|---|
| 端口 | iam 8091 / land 8092 / project 8093 / facility 8094 / sim 8095 | `CONVENTIONS.md` §1 |
| 端点 | GraphQL 子图：`/graphql`（业务）+ `/healthz`（不依赖 DB）+ `/readyz`（校验 DB）+ `/metrics`；sim：上述 REST + 同款三端点 | §2.14 |
| /metrics | 必须 `Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)` | §2.8 |
| 连接池 | write_pool(env DB_USER/DB_PASS) + read_pool(env DB_USER_RO/DB_PASS_RO)，host 均 `tidb.infra.svc.cluster.local:4000` | §2.9 |
| 建表自检 | 自增主键、`created_at DATETIME DEFAULT CURRENT_TIMESTAMP`、幂等 `uk_*`、查询列有索引、大表 `SHARD_ROW_ID_BITS=4 PRE_SPLIT_REGIONS=8`（仅无显式主键者） | §2.10 |
| 保留字 | 禁用 column/order/desc/group/condition 等做列名 | §2.2 |
| 种子 | seed_if_empty 双守卫（先查 org 再查 party 防双副本重种 1062） | §2.11 |
| 身份 | 子图 `gql_context` 解 `x-user-*` 头，`x-user-name` b64decode(validate=True) 失败按明文回退 | §2.7 |

### 5.1 iam 数据表（hsf_md / hsf_biz）

```sql
-- hsf_md.org（组织树；与 04 号 §3.1 冻结 DDL 一致，code 自然主键；评审 R6：code CHAR(16) 预留）
CREATE TABLE org (
  code CHAR(16) NOT NULL,                    -- 行政区划 6/9/11 位或主体码 12 位(530600900001)；列宽预留（CONVENTIONS §2.3 宁可宽不可窄，规避 DV-IAS org.code 过短坑#4）
  name VARCHAR(64) NOT NULL,
  level VARCHAR(16) NOT NULL,                -- COUNTY/TOWNSHIP/VILLAGE/KEEP_UNIT/COOP（04 号冻结枚举）
  parent_code CHAR(16) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (code),
  KEY idx_parent (parent_code)
);

-- hsf_md.party（人员主体；联邦实体 Party；04 号 §3.2 冻结）
CREATE TABLE party (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(64) NOT NULL,
  phone_hash CHAR(64) NOT NULL,              -- SM3(phone)：登录标识与等值检索（uk_phone）
  phone_enc TEXT NOT NULL,                   -- SM4-GCM 密文（展示层 mask_phone，NFR-06）
  org_code CHAR(16) NOT NULL,                -- 所属组织 org.code（含 12 位主体码，列宽预留）
  role VARCHAR(16) NOT NULL,                 -- admin/town/village/keeper/coop（04 号冻结，单角色）
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_phone (phone_hash),
  KEY idx_org (org_code)
);

-- hsf_md.account（登录凭据，1:1 party；04 号 §3.3 冻结）
CREATE TABLE account (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  party_id BIGINT NOT NULL,
  password_salt CHAR(32) NOT NULL,
  password_hash CHAR(64) NOT NULL,           -- SM3-PBKDF2 派生
  status VARCHAR(16) NOT NULL DEFAULT 'active',  -- active/locked（手动停用标志；一期无锁定接口，DDD-A3）
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_account_party (party_id)
);

-- hsf_biz.audit_log（审计，仅插入；与 04 号 §4.8 冻结 DDL 一致；idx 支撑按用户/时间/动作过滤 FR-704）
CREATE TABLE audit_log (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id BIGINT NULL,                       -- 未认证请求为 NULL
  user_name VARCHAR(64) NULL,                -- 操作人姓名快照（防后续改名不可追溯）
  action VARCHAR(64) NOT NULL,
  object_type VARCHAR(32) NULL,
  object_id VARCHAR(64) NULL,
  ip VARCHAR(45) NULL,
  result VARCHAR(16) NOT NULL,               -- OK/DENIED/ERROR（越权 403 亦记录）
  detail VARCHAR(512) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_user_time (user_id, created_at),
  KEY idx_action_time (action, created_at)
);
```

### 5.2 land 数据表（hsf_md）

```sql
-- 与 04 号 §3.4 冻结 DDL 一致
CREATE TABLE land_parcel (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  code CHAR(20) NOT NULL,                    -- ^L\d{10}$，列宽预留（§2.3）
  name VARCHAR(64) NOT NULL,
  village_code CHAR(12) NOT NULL,            -- 村码 11 位（org.code，GB/T 2260）
  holder_id BIGINT NULL,                     -- 联邦引用 Party(@key id)
  area_mu DECIMAL(10,2) NULL,
  soil_type VARCHAR(32) NULL,
  crop_type VARCHAR(32) NULL,
  geo_json JSON NULL,                         -- GeoJSON geometry(Polygon), WGS84（ADR-HSF-001）
  status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE/RETIRED（04 号冻结；RETIRED 一期无流转接口，§4.2）
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NULL,
  UNIQUE KEY uk_parcel_code (code),
  KEY idx_village (village_code, status),
  KEY idx_holder (holder_id)
);
```

### 5.3 project 数据表（hsf_biz）

```sql
-- 与 04 号 §4.1~4.3 冻结 DDL 一致
CREATE TABLE farm_project (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  code CHAR(20) NOT NULL,                    -- ^P\d{10}$
  name VARCHAR(128) NOT NULL,
  build_year SMALLINT NOT NULL,
  content TEXT NULL,
  applicant_org_code CHAR(12) NOT NULL,      -- 申报单位 org.code（04 号冻结）
  supervisor_id BIGINT NULL,                 -- 联邦引用 Party
  state VARCHAR(16) NOT NULL DEFAULT 'RESERVED',
  acceptance_note VARCHAR(512) NULL,         -- 验收归档结论（acceptanceArchive 回填，FR-305）
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NULL,
  UNIQUE KEY uk_proj_code (code),
  KEY idx_state_year (state, build_year),
  KEY idx_applicant (applicant_org_code),
  KEY idx_supervisor (supervisor_id)
);

CREATE TABLE project_parcel (
  project_code CHAR(20) NOT NULL,
  parcel_code CHAR(20) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (project_code, parcel_code),
  KEY idx_parcel (parcel_code)
);

CREATE TABLE project_transition (            -- 状态留痕（只追加）
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  project_id BIGINT NOT NULL,
  action VARCHAR(24) NOT NULL,               -- 冻结值=05 号 §5.2 mutation 名：submitProject/approveProject/startConstruction/startSupervision/acceptanceProject/acceptanceArchive（评审 R13）
  from_state VARCHAR(16) NOT NULL,
  to_state VARCHAR(16) NOT NULL,
  actor_id BIGINT NOT NULL,                  -- 操作人 party.id
  note VARCHAR(255) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_project (project_id, created_at)
);
```

### 5.4 facility 数据表（hsf_md / hsf_biz）

```sql
-- hsf_md.facility（台账；联邦实体 Facility；与 04 号 §3.5 冻结 DDL 一致）
CREATE TABLE facility (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  code CHAR(20) NOT NULL,                    -- ^F\d{10}$
  type VARCHAR(16) NOT NULL,                 -- WELL(机井)/PUMP_STATION(泵站)/CANAL(渠道)/ROAD(道路)（04 号冻结枚举，评审 R7）
  name VARCHAR(64) NOT NULL,
  parcel_code CHAR(20) NULL,                 -- 联邦引用 LandParcel（FR-401）
  village_code CHAR(12) NOT NULL,
  build_year SMALLINT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'NORMAL',  -- NORMAL/BROKEN/REPAIRING/RETIRED（04 号冻结）
  qr_token CHAR(32) NULL,                    -- FR-402：secrets.token_hex(16)，注册时生成；uk_qr_token
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NULL,
  UNIQUE KEY uk_fac_code (code),
  UNIQUE KEY uk_qr_token (qr_token),
  KEY idx_village_type (village_code, type, status),
  KEY idx_parcel (parcel_code)
);

-- hsf_biz.facility_workorder（巡检工单；与 04 号 §4.4 冻结 DDL 一致，评审 R7）
CREATE TABLE facility_workorder (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  sn VARCHAR(32) NOT NULL,                    -- WO+yyyyMMdd+6位流水（uk_wo_sn；取号 Redis INCR hsf:seq:workorder，04 号 §2.2；DDD-D1）
  facility_code CHAR(20) NOT NULL,
  village_code CHAR(12) NOT NULL,             -- 冗余设施所属村（范围过滤免联 hsf_md）
  state VARCHAR(16) NOT NULL DEFAULT 'OPEN',  -- OPEN/ACCEPTED/DISPATCHED/RESOLVED/APPROVED
  description TEXT NOT NULL,
  contact_enc TEXT NULL,                      -- 联系方式 SM4-GCM 密文（NFR-06/V-15：库内密文，接口仅出 contactMasked，评审 R7）
  reporter_id BIGINT NOT NULL,
  assignee_id BIGINT NULL,                    -- dispatch 必填，keeper
  resolve_note TEXT NULL,                     -- resolve 必填（FR-403）
  perf_score INT NULL,                        -- 绩效积分，可空（FR-404）
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NULL,
  UNIQUE KEY uk_wo_sn (sn),
  KEY idx_facility (facility_code, created_at),
  KEY idx_state_village (state, village_code, created_at),
  KEY idx_assignee (assignee_id, state),
  KEY idx_reporter (reporter_id)
);

CREATE TABLE workorder_transition (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  wo_id BIGINT NOT NULL,
  action VARCHAR(16) NOT NULL,               -- 冻结值=05 号 §6.2 mutation 名：acceptWorkorder/dispatchWorkorder/resolveWorkorder/approveWorkorder（评审 R13 同类）
  from_state VARCHAR(16) NOT NULL,
  to_state VARCHAR(16) NOT NULL,
  actor_id BIGINT NOT NULL,
  note VARCHAR(255) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_wo (wo_id, created_at)
);
```

### 5.5 sim 数据表（hsf_ts / hsf_biz）

```sql
-- hsf_biz.sensor（模拟设备注册；与 04 号 §4.7 冻结 DDL 一致，评审 R3：原 hsf_ts.sensor_device 口径废止）
CREATE TABLE sensor (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  sensor_code CHAR(20) NOT NULL,             -- S+6位区划+4位序号（A-04-2 冻结，种子 48 台）
  metric VARCHAR(16) NOT NULL,               -- 6 类之一（04 号 §4.7 冻结：soil_moisture/air_temp/wind_speed/rainfall/pest_index/water_level）
  village_code CHAR(12) NOT NULL,
  parcel_code CHAR(20) NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE/OFFLINE（04 号冻结）
  last_value DECIMAL(10,3) NULL,             -- 最新观测值（tick 直写，/sim/latest 快路径）
  last_recorded_at DATETIME(3) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NULL,
  UNIQUE KEY uk_sensor_code (sensor_code),
  KEY idx_village_metric (village_code, metric)
);

-- hsf_ts.sensor_reading（列结构承 CONVENTIONS.md §2.13；主键=04 号 §5.1 冻结的 AUTO_RANDOM(5)+PRE_SPLIT_REGIONS=4，
-- 对 §2.13"无显式主键"口径的显式偏离，偏离声明见 04 号 §5.1——评审 R4）
CREATE TABLE sensor_reading (
  id BIGINT NOT NULL AUTO_RANDOM(5) PRIMARY KEY,
  sensor_code CHAR(20) NOT NULL,
  metric VARCHAR(16) NOT NULL,
  value DECIMAL(10,3) NOT NULL,
  recorded_at DATETIME(3) NOT NULL,
  KEY idx_code_time (sensor_code, recorded_at)
) PRE_SPLIT_REGIONS=4;

-- hsf_biz.alert_rule（一期 4 条，FR-503；与 04 号 §4.10 冻结 DDL 一致）
CREATE TABLE alert_rule (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  rule_code VARCHAR(32) NOT NULL,            -- RULE_DROUGHT/RULE_WATERLOGGING/RULE_GALE/RULE_PEST（05 号 §7.2 冻结）
  metric VARCHAR(16) NOT NULL,
  op VARCHAR(4) NOT NULL,                    -- '<' 或 '>'
  threshold DECIMAL(10,3) NOT NULL,
  enabled TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_rule_code (rule_code)
);

-- hsf_biz.alert_record（告警 3 态；与 04 号 §4.6 冻结 DDL 一致——评审 R2：枚举值/列名均以 04 为准）
CREATE TABLE alert_record (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  rule_code VARCHAR(16) NOT NULL,            -- RULE_DROUGHT/RULE_WATERLOGGING/RULE_GALE/RULE_PEST
  sensor_code CHAR(20) NOT NULL,
  metric VARCHAR(16) NOT NULL,
  trig_value DECIMAL(10,3) NOT NULL,         -- 触发时观测值
  trig_time DATETIME(3) NOT NULL,            -- 触发读数的 recorded_at（幂等键组成部分）
  village_code CHAR(12) NOT NULL,
  parcel_code CHAR(20) NULL,
  state VARCHAR(16) NOT NULL DEFAULT 'OPEN', -- OPEN/PROCESSING/CLOSED（FR-504 三态，04 号冻结）
  handled_by BIGINT NULL,
  handled_at DATETIME NULL,
  closed_at DATETIME NULL,
  note VARCHAR(255) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_rule_reading (sensor_code, rule_code, trig_time),
  KEY idx_sensor_time (sensor_code, trig_time),
  KEY idx_village_time (village_code, trig_time),
  KEY idx_state (state, trig_time)
);
```

（`hsf_biz.domain_event` Outbox 表与 `hsf_biz.audit_log`：完整 DDL 冻结于 04 号 §4.9/§4.8——id 自增 PK、topic VARCHAR(64)、payload JSON、created_at、published_at DATETIME NULL、KEY idx_topic_time，见 ADR-HSF-004/02 号文档。）

### 5.6 GraphQL 类型与操作清单（跨子图同名实体强制）

| 子图 | 联邦实体（keys） | Query | Mutation | `_resolve_reference` 返回 |
|---|---|---|---|---|
| iam | `Party`(["id"]) | orgTree / party / parties / me / auditLogs（admin） | login | 按 id 查 party |
| land | `LandParcel`(["code"]) | landParcel / landParcels / landGeojson / landStats | registerParcel | 按 code 查地块（含 holder 联邦字段） |
| project | `FarmProject`(["code"]) | farmProject / farmProjects / projectTransitions | createProject / submitProject / approveProject / startConstruction / startSupervision / acceptanceProject / acceptanceArchive | 按 code 查项目（含 supervisor、parcels 联邦字段） |
| facility | `Facility`(["code"]) | facility / facilities / workorder / workorders / facilityByToken | registerFacility / reportWorkorder / accept·dispatch·resolve·approveWorkorder | 按 code 查设施（含 parcel 联邦字段） |
| sim | （无，REST） | — | — | — |

- Schema 构造必须显式：`strawberry.federation.Schema(query=..., mutation=..., types=[Party, LandParcel, FarmProject, Facility], enable_federation_2=True)`——未注册实体不进 SDL，网关组合 400（DV-IAS 生产事故，`CONVENTIONS.md` §2.4）。
- 各子图只声明**自己拥有**的实体类型+被引用实体的最小投影字段（如 land 子图内 `Party` 仅需 id/name 用于解析），避免组合冲突。

### 5.7 权限矩阵（RBAC+数据范围，FR-702 验收口径）

| 操作 \ 角色 | admin(全县) | town(本乡镇) | village(本村) | keeper(所辖设施) | coop(关联地块) |
|---|---|---|---|---|---|
| 组织/账号管理 | RW（一期接口仅 login，账号维护走种子，05 号 §3.2） | R(本乡镇) | R | R | — |
| 地块登记 | RW | — | RW(本村) | — | — |
| 一张图/地块查询（landParcels/landGeojson/landStats） | R | R(本乡镇) | R(本村) | **空集**（无地块范围，05 号 §2.4） | R(关联地块 holder=me) |
| 项目创建/流转 | RW | submit(申报)/R(本乡) | **空集**（05 号 §2.4） | — | — |
| 设施台账维护 | RW | R | RW(本村) | R | — |
| 工单 创建/受理/派单/办结/销项 | 全部 | 创建+受理+派单+销项 | 创建+受理+销项（**无办结**，05 号 §2.4/§6.2） | 创建+办结(被派单人，resolve 仅限 assignee 本人) | 创建(经营问题上报) |
| 告警 查看（处置接口二期 P2） | 全县 | 乡内村集 | 本村 | 所辖设施所在村集 | 关联地块所在村集 |
| 审计查询（auditLogs） | R | — | — | — | — |
| tick/设备查询（REST） | 登录（任意角色，05 号 §7.1——评审 R8⑤） | 登录 | 登录 | 登录 | 登录 |

- 越权读→返回空集或 403；越权写→403 且**记审计**（FR-702/704）。
- 数据范围实现：子图在 repo 查询层强制追加范围谓词（如 village 查询地块强制 `village_code=<用户 org 对应村码>`），不依赖前端。

## 6. 状态机实现规约（全部三个状态机统一）

```python
# app/domain.py —— 转移表驱动（以 project 为例；工单/告警同构）
# action 键=mutation 名（与 05 号 §5.2 冻结表一致，亦为 project_transition.action 的冻结存储值——评审 R5/R13）
PROJECT_TRANSITIONS = {
    ("RESERVED", "submitProject"): "SUBMITTED",
    ("SUBMITTED", "approveProject"): "APPROVED",
    ("APPROVED", "startConstruction"): "CONSTRUCTING",
    ("CONSTRUCTING", "startSupervision"): "SUPERVISED",
    ("SUPERVISED", "acceptanceProject"): "ACCEPTED",
    ("ACCEPTED", "acceptanceArchive"): "PERFORMED",
}

def transition(current: str, action: str) -> str:
    nxt = PROJECT_TRANSITIONS.get((current, action))
    if nxt is None:
        raise BusinessError("PROJECT_ILLEGAL_TRANSITION")   # 不落库、不留痕、记审计
    return nxt
```

统一约束：① 转移表为**纯函数常量**，单测对全状态×全动作矩阵穷举（FR-303"状态机转移表全覆盖"）；② 合法流转=同事务 [业务 UPDATE + 留痕 INSERT + audit INSERT + domain_event INSERT]；③ 终态（PERFORMED/APPROVED/CLOSED）无出边由转移表天然保证；④ 错误码表（PROJECT_/WORKORDER_/ALERT_/ACCOUNT_ILLEGAL_TRANSITION、*_NOT_FOUND 等）冻结于 05 号。

## 7. 一致性策略

| 场景 | 策略 | 依据 |
|---|---|---|
| 单聚合内 | 本地事务 ACID（写池） | `CONVENTIONS.md` §2.9 |
| 跨上下文读 | 联邦引用，秒级最终一致（上游变更后下游缓存 TTL 内可见旧值，可接受并在 05 号标注） | §4.6 |
| 跨上下文写校验 | 同 TiDB 实例跨库 SELECT（hsf_app 三库授权），失败即拒 | `CONVENTIONS.md` §3.2；ADR-HSF-004 |
| 跨上下文异步 | Outbox 表（无 broker），同事务落库；二期投递器→Pulsar | ADR-HSF-004 |
| 计数/统计 | 一期直接聚合查询+缓存（无预聚合表），statistics 与明细一致性由同库同源保证 | FR-505 |
| 缓存一致 | cache-aside + 写后 `invalidate_prefix(域前缀)`；TTL ±20% 抖动防雪崩；singleflight 防击穿；空值缓存防穿透 | `cache.py:3/75-102` |

## 8. 六边形代码结构（Python 服务模板，镜像 dv-ias 实布局）

```
hsf-ias/services/<svc>/
 ├─ app/main.py        # FastAPI 装配：/graphql + /healthz + /readyz + /metrics；seed_if_empty 启动钩子
 ├─ app/schema.py      # Strawberry 联邦类型 + Query/Mutation + _resolve_reference（依赖：domain、repo）
 ├─ app/domain.py      # 聚合/值对象/状态机转移表/业务错误码/不变量（纯函数，零 IO）
 ├─ app/repo.py        # 仓储：read_pool 查询 + write_pool 事务（业务+留痕+审计+Outbox）+ 缓存读写
 ├─ app/cache_keys.py  # key 常量与 TTL（04 号冻结）
 ├─ tests/test_domain.py   # 状态机矩阵/不变量/规则边界值单测（pytest）
 └─ Dockerfile         # FROM 10.167.2.175:30500/szxc/base:1.2
```

- 依赖规则：`schema → domain → repo`；domain 不 import repo/db（可测试性）；repo 实现 domain 定义的错误与常量。**务实取舍声明**：一期不设显式仓储接口层（Python 动态类型下接口层收益低于成本），DV-IAS 实布局（main/schema/repo/tests 四件）即此口径，本文增补 domain.py 拆分纯域逻辑以支撑状态机矩阵单测。
- Monorepo 根：`hsf-ias/`（gw/ services/ k8s/ scripts/ perf/ test/），结构完全镜像 `dv-ias/`（`CONVENTIONS.md` §1）。

## 9. 测试映射（金字塔，执行口径归 20 号报告）

| 层 | 对象 | 用例（映射验收条款） |
|---|---|---|
| 单元（pytest） | 三个状态机全矩阵 | FR-303（7 态非法流转全拒）、FR-403（工单 5 态×4 动作矩阵）、FR-504（告警 3 态） |
| 单元 | RuleEngine 边界值 | FR-503（14.9/15.1、84.9/85.1、16.9/17.1、79.9/80.1） |
| 单元 | 编码正则/不变量/SM4+SM3/脱敏 | FR-101、NFR-06 |
| 契约 | SDL 组合 | gw 组合失败即 CI 失败；联邦查询 FR-104/304/401 |
| E2E（Playwright ≥10 用例） | 三端渲染/登录/错密/一张图/项目全周期/工单闭环/告警查询/联邦跨域/限流 429/越权 403 | `hsf/docs/01` §8.1 |
| 性能 | k6 browse 200VU+report 20VU；Locust 写路径；sim 批量写基准 ≥1 万条/s | NFR-01~04 |
| 静态门禁 | static_gate.py 16 条 | `CONVENTIONS.md` §2 全量 |

## 10. 假设与冻结项登记（本文新增，编号接 `hsf/docs/01` §7）

| # | 项 | 内容 | 影响 |
|---|---|---|---|
| DDD-A2 | 设备编码规则 | sensor_code `^S\d{10}$`（S+6 位区划+4 位序号，04 号 A-04-2 冻结），S 类为编码体系扩展类，不与 L/P/F 主三类冲突 | 列宽 CHAR(20) 不变 |
| DDD-A3 | 账号锁定口径（评审 R6 收敛） | **一期不实现自动锁定/解锁/停用接口**（05 号 §3.1 冻结 Mutation 仅 login，账号维护走种子脚本）：`account.status`（active/locked，04 号 §3.3）仅作运维手动停用标志；连续失败 5 次自动锁定（env LOCK_THRESHOLD 可配）随二期账号管理接口一并设计 | 错密/停用账号一律 `UNAUTHENTICATED`；等保"登录失败处理"列二期增强并如实声明 |
| DDD-D1 | 工单流水号（评审 R7 修订） | `sn`=WO+yyyyMMdd+6 位当日序号（uk_wo_sn；Redis INCR `hsf:seq:workorder`，04 号 §2.2 冻结）；不占用 ^[LPF]\d{10}$ 编码体系（FR-101 仅冻结三类主编码）——原「展示号 WO-<id>」口径废止（05 号 SDL `Workorder.sn: String!` 必有 sn） | 如需工单编码进体系，须新 ADR |
| DDD-D2 | 工单"4 态"口径 | 4 态=受理/派单/办结/销项；OPEN(待受理) 为创建初始态不计入（§4.4 口径说明） | E2E 断言按本口径 |
| DDD-D3 | roles 存储格式 | VARCHAR(128) 逗号分隔（一期 ≤1 角色/账号） | 04 号冻结时可改 JSON 列，迁移成本低 |
| DDD-D4 | sim CORS | web 源白名单（告警 REST 直连不走网关） | 二期若网关加 REST 路由则收敛 |
| DDD-D5 | phone 可检索密文 | SM4-GCM 密文列 + SM3 散列列（uk/检索走散列列） | 若需改为确定性加密另立 ADR |
