# 05 HSF GraphQL 接口与集成设计

> 版本：v1.0（设计冻结稿）｜ 编制日期：2026-11-07 ｜ 编制：数据架构师
> 上游依据：`hsf/CONVENTIONS.md`（端口/实体/网关强制约定）、`hsf/docs/01`（SRS 全部 FR 验收标准）、`hsf/docs/04`（DDL/编码/缓存/枚举存储值冻结点）
> 参考实现（已验证）：DV-IAS `docs/06-GraphQL接口与集成设计.md`；代码 `dv-ias/gw/gateway.js`（网关三件套）、`dv-ias/services/iam/app/schema.py`、`dv-ias/services/governance/app/schema.py`（Strawberry Federation 子图与实体桩模式）
> 冻结声明：**本文 SDL 是接口唯一事实源（SDD）**；代码中 Python 字段用 snake_case，经 Strawberry 默认命名转换上线为 camelCase——**本文 SDL 即线上契约**（A-05-3）。状态机枚举与 04 号 DDL 存储值一一对应。

---

## 0. 决议与假设清单（先读）

| # | 类型 | 内容 | 理由 |
|---|---|---|---|
| D-05-1 | 决议 | 分页采用 **offset 分页**（`page/size` + `XxxPage{total,page,size,items}`），不采用 DV-IAS docs/06 §3 承诺的 Relay Connection（`first/after`） | 与 DV-IAS **已验证实现**一致（`iam/app/schema.py:94-104`）；一期量级小（地块 ≤1 万），COUNT 代价可忽略；cursor 分页列二期增强。这是对参考**设计文档**的显式裁剪，以参考**代码实现**为准 |
| D-05-2 | 决议 | 401 口径澄清：GraphQL 通道未登录/错密返回 **HTTP 200 + `errors[0].extensions.code=UNAUTHENTICATED`**；REST 通道（sim）返回 **HTTP 401**。SRS FR-701/FR-703 中"返回 401"以 `extensions.code` 断言等价执行（E2E 断言 code 而非 HTTP 状态码） | GraphQL 惯例（业务错误不映射 HTTP 5xx/4xx）；网关仅限流路径用 HTTP 429。DV-IAS `web.py require_user` 同语义 |
| D-05-3 | 决议 | sim 为 **REST 服务而非 GraphQL 子图**（FastAPI，NodePort 31211 直连），其 REST 契约在本文 **§7.1** 统一冻结（评审 R21③） | `CONVENTIONS.md` §1 明确"sim IoT 模拟与告警（FastAPI 非 GraphQL）"；k6 时序压测直打 REST 更简单 |
| D-05-4 | 决议 | 业务错误码扩展 `INVALID_TRANSITION` / `CODE_DUPLICATED`（在 DV-IAS 七个基础码之外新增） | FR-303/FR-403 要求"非法流转返回业务错误码"、FR-101 要求编码冲突可区分；网关 formatError 仅脱敏 `INTERNAL*` 前缀，业务码不受影响 |
| D-05-5 | 决议 | `@key` 实体仅 4 个：`Party(id)`/`LandParcel(code)`/`FarmProject(code)`/`Facility(code)`；Workorder/Alert 等无跨子图引用诉求，不做实体；**`Organization` 为普通类型（非 @key 实体）**——无任何跨子图引用方，§1.2/§3.1 已同步（评审 R12） | `CONVENTIONS.md` §1 强制同名实体清单；无引用不做实体，减少组合复杂度 |
| D-05-6 | 决议 | 时间字段 SDL 类型为 `String`，格式 **ISO8601 +08:00**（如 `2026-11-07T15:04:05+08:00`） | DV-IAS 已验证实现即 String（`governance/app/schema.py:29`）；自定义 Scalar 一期无增益 |
| D-05-7 | 决议 | 一期**无 Subscription**；实时性靠轮询（大屏 5s，`docs/06` §8 模式） | 一期无推送通道需求（无 MQ）；WebSocket 列二期 |
| D-05-8 | 决议 | 网关**不做查询深度限制/APQ**（DV-IAS docs/06 §1 承诺项的裁剪）：内网受控 + JWT + 限流 + 鉴权已覆盖一期威胁模型 | DV-IAS 实际网关未实现深度限制（`gateway.js` 注释），避免承诺未实现能力；列为二期加固 |
| A-05-1 | 假设 | 分页 `total` 一律返回真实 COUNT（不沿用 DV-IAS `total=-1` 简化） | 一期量级下 COUNT 可忽略；前端直接渲染总数 |
| A-05-2 | 假设 | 告警状态推进接口 `POST /sim/alerts/{id}/transition` 冻结为二期（P2）；一期验收最低口径=查询+统计（FR-504 原文允许） | `hsf/docs/01` FR-504"最低验收口径：可查询、可统计" |

---

## 1. 联邦架构

### 1.1 拓扑

```
管理端(Web:31210)  一张图大屏(Web)  移动端H5(Web)  k6/Locust/E2E
        │ HTTPS/HTTP（NodePort 30090）
        ▼
Apollo Gateway（gw：8090 业务 /graphql + 9090 /healthz 探针）
  JWT(HS256) 校验 · 令牌桶限流 RATE_CAP=6000/min · 身份头注入
  · formatError 脱敏(INTERNAL* → UNAVAILABLE) · CORS · INTROSPECTION 开关
  ├── iam       子图  http://iam-svc.hsf-prod.svc.cluster.local:8091/graphql
  ├── land      子图  http://land-svc.hsf-prod.svc.cluster.local:8092/graphql
  ├── project   子图  http://project-svc.hsf-prod.svc.cluster.local:8093/graphql
  ├── facility  子图  http://facility-svc.hsf-prod.svc.cluster.local:8094/graphql
  └── （sim 为 REST :8095，NodePort 31211 直连，不进联邦，见 §8）
```

### 1.2 子图注册表

| 子图 | 端口 | Service（hsf-prod） | 职责（限界上下文） | 本图拥有的 @key 实体 | 引用的外部实体（桩） | Strawberry 显式 types 注册（`CONVENTIONS.md` §2.4） |
|---|---|---|---|---|---|---|
| iam | 8091 | iam-svc | 组织/账号/角色/登录/审计 | Party(id)（`Organization` 为普通类型，非 @key 实体——D-05-5，评审 R12） | — | `[Party, Organization]`（types 列表可含普通类型） |
| land | 8092 | land-svc | 地块主数据/统一编码/GeoJSON | LandParcel(code) | Party | `[LandParcel, Party]` |
| project | 8093 | project-svc | 项目 7 态全周期 | FarmProject(code) | Party、LandParcel | `[FarmProject, Party, LandParcel]` |
| facility | 8094 | facility-svc | 设施台账/二维码/工单 4 态 | Facility(code) | Party、LandParcel | `[Facility, Party, LandParcel]` |
| sim | 8095 | sim-svc | IoT 模拟/时序/规则告警（REST） | —（非联邦） | —（直读库） | —（FastAPI 无 schema） |

> **强制**：每个子图 `strawberry.federation.Schema(query=…, mutation=…, types=[上表清单], enable_federation_2=True)` 必须显式传 `types`——未注册实体不进 SDL，网关组合直接 400（DV-IAS 真实生产事故，`CONVENTIONS.md` §2.4）。冒烟 `IMPORT-OK` 后另做 SDL 断言（§11 V-16）。

### 1.3 联邦实体总表（跨子图同名强制）

| 实体 | @key | 拥有者 | 被引用（字段路径） | 解析模式 |
|---|---|---|---|---|
| `Party` | `id` | iam | land: `LandParcel.holder`；project: `FarmProject.supervisor`；facility: `Workorder.reporter/.assignee` | 本地桩：引用子图仅持有 key 字段，`_resolve_reference` 返回 `cls(id=…)` 桩，其余字段由网关路由回 iam 解析 |
| `LandParcel` | `code` | land | project: `FarmProject.parcels[]`；facility: `Facility.parcel` | 同上（code 桩） |
| `FarmProject` | `code` | project | —（一期无引用方；预留 DV-IAS 互查） | — |
| `Facility` | `code` | facility | —（一期无引用方；预留） | — |

> 桩模式代码证据：`dv-ias/services/governance/app/schema.py:8-15`（Party 桩 `_resolve_reference` 返回 `cls(id=id)`，name 等由 iam 子图提供）。验收路径：`{ landParcel(code:"L5306000001"){ code holder { name } } }` 一次组合返回真实种子数据（`CONVENTIONS.md` §3.8）。

### 1.4 网关（gw）设计

| 项 | 冻结值 | 依据 |
|---|---|---|
| 端口 | 业务 8090（/graphql）、探针 9090（/healthz，进程存活即 200）；容器同时声明 8090+9090 | `CONVENTIONS.md` §2.5 |
| NodePort | 30090（上线前 `ss -tln` 核验） | `hsf/docs/00` C6 |
| JWT | HS256 共享密钥（Secret `hsf-secrets` 的 `JWT_SECRET`，与子图 `issue_token` 同源）；`sub` 强制字符串（PyJWT≥2.10） | `CONVENTIONS.md` §2.1/§2.7 |
| 身份头 | 校验通过后注入 `x-user-id` / `x-user-roles` / `x-user-name`（**Base64 编码**，HTTP 头仅允许 ByteString）；未登录清空头；子图 b64decode 失败按明文回退 | `CONVENTIONS.md` §2.7 |
| 限流 | 令牌桶 `RATE_CAP=6000/min`（env 可调），按 `user.sub` 或 `ip` 分桶；超限 **HTTP 429** + `RATE_LIMITED` | `CONVENTIONS.md` §2.6 |
| 错误脱敏 | `extensions.code` 以 `INTERNAL` 开头 → 替换为 `{"message":"服务内部错误，请稍后重试","extensions":{"code":"UNAVAILABLE"}}` | `CONVENTIONS.md` §2.6 |
| 内省 | `INTROSPECTION` env 控制（dev 开/生产关） | DV-IAS gateway.js 同参 |
| SUBGRAPHS env | `iam=http://iam-svc.hsf-prod.svc.cluster.local:8091/graphql,land=http://land-svc.hsf-prod.svc.cluster.local:8092/graphql,project=http://project-svc.hsf-prod.svc.cluster.local:8093/graphql,facility=http://facility-svc.hsf-prod.svc.cluster.local:8094/graphql` | §1.1 |
| 伪造头 | 客户端伪造的 `x-user-*` 在网关 `willSendRequest` 中被覆盖/剥离，子图只信任网关注入 | DV-IAS gateway.js 同法（FR-703 验收） |

---

## 2. 全局接口规约

### 2.1 错误码注册表（`extensions.code`）

| code | 语义 | 典型触发 | REST(sim) HTTP 映射 | 对应验收 |
|---|---|---|---|---|
| `UNAUTHENTICATED` | 未登录 / 凭据无效 | 无 token、token 过期、手机号或口令错误 | 401 | FR-701③、FR-703 |
| `FORBIDDEN` | 已登录但越权 | 角色不含该操作、数据范围外对象 | 403 | FR-702 |
| `VALIDATION` | 参数/业务校验失败 | 村码不存在、GeoJSON 非法、面积负数、办结未填反馈 | 400 | FR-102 等 |
| `NOT_FOUND` | 对象不存在 | 编码/ID 查无、token 失效 | 404 | FR-102/401 |
| `CONFLICT` | 并发/重复提交 | 同一对象并发写冲突（一期乐观兜底） | 409 | 通用 |
| `RATE_LIMITED` | 网关限流 | 超 6000/min | 429 | FR-705 |
| `INVALID_TRANSITION` | **状态机非法流转**（跨态/回退/未终态操作） | `RESERVED` 直接 `startConstruction`；`ACCEPTED` 回退申报 | 400 | FR-303/FR-403 |
| `CODE_DUPLICATED` | 统一编码冲突 | 并发取号撞 `uk_*` 且重试 3 次失败 | 409 | FR-101 |
| `UNAVAILABLE` | 下游不可用 / **内部错误脱敏后** | 子图异常（INTERNAL* 被网关替换为本码） | 503 | `CONVENTIONS.md` §2.6 |

错误 JSON 形状（GraphQL 通道，业务错误 HTTP 仍为 200——D-05-2）：

```json
{
  "errors": [{
    "message": "项目处于 RESERVED 状态，不允许 startConstruction",
    "path": ["startConstruction"],
    "extensions": { "code": "INVALID_TRANSITION", "fromState": "RESERVED", "action": "startConstruction" }
  }],
  "data": { "startConstruction": null }
}
```

规约：① `message` 为面向用户的中文文案（不暴露 SQL/堆栈）；② `extensions.code` 必填、其余键可选；③ 内部异常一律以 `UNAVAILABLE` 出网关（脱敏）；④ 子图实现载体为 `hsf_common.web.Exception429(code, msg)` 模式（DV-IAS 已验证）。

### 2.2 分页规约（D-05-1）

| 项 | 规约 |
|---|---|
| 入参 | `page: Int = 1`（≥1）、`size: Int = 20`；上限冻结：顶层列表 `size ≤ 100`，嵌套列表（`Facility.workorders`）`size ≤ 50`，超限静默取上限 |
| 返回 | `type XxxPage { total: Int! page: Int! size: Int! items: [Xxx!]! }`（`total` 为真实 COUNT，A-05-1） |
| 排序 | 业务表默认 `ORDER BY id DESC`（新→旧）；主数据点查与树不受影响；时序按 `recorded_at DESC` |
| 深分页 | 一期不做 cursor；`page*size ≤ 10000` 软约束（越界返回空页） |
| 二期演进 | `Connection(first/after)` 仅当单表 >10 万行且深翻页成为常态时引入，届时旧分页字段 `@deprecated` 保留两个版本周期 |

### 2.3 命名 / 时间 / 敏感字段规约

| 项 | 规约 |
|---|---|
| 命名 | SDL/线上 camelCase；Python snake_case（Strawberry 默认转换，A-05-3） |
| 时间 | `String`，ISO8601 +08:00（D-05-6）；空值为 `null` 不用 `"-"` |
| 手机号/联系方式 | 仅 `phoneMasked` / `contactMasked` 输出（`mask_phone`：11 位手机号 3+4+4，如 `138****0601`——评审 R21④）；**SDL 不含** `phoneHash/phoneEnc/contactEnc` 字段——SDL 即契约，敏感字段不进契约（NFR-06） |
| 全局 ID | 不做全局 base64 ID；实体 key 直接用业务编码（`code`）或库内 `id` 字符串（@key 语义自足） |
| 幂等 | mutation 不做请求级幂等键（一期）；业务幂等由 `uk_*`（编码/流水号/告警键）承载 |

### 2.4 鉴权与数据范围矩阵（FR-702 冻结）

角色：`admin`(县)/`town`(乡)/`village`(村)/`keeper`(管护)/`coop`(合作社)；网关注入 `x-user-id/x-user-roles`，子图据此过滤。

| 查询/操作 | admin | town | village | keeper | coop |
|---|---|---|---|---|---|
| `orgTree` / `parties` | 全县 | 本乡（org_code 前缀 9 位） | 本村 | 本人组织 | 本人组织 |
| `landParcels` / `landGeojson` / `landStats` | 全县 | 乡内村集 | `village_code=本村` | **空集**（无地块范围） | 强制追加 `holder_id=me`（传入村码取交集） |
| `farmProjects` | 全县 | `applicant_org_code` 前缀 9 位 | 空集 | 空集 | 空集 |
| `facilities` | 全县 | 乡内村集 | 本村 | 所辖设施（其 assignee 工单关联的 facility 集合） | 关联地块上的设施 |
| `workorders` | 全县 | 乡内村集 | 本村 | `assignee_id=me` | `reporter_id=me` |
| alerts（sim REST） | 全县 | 乡内村集 | 本村 | 所辖设施所在村集 | 关联地块所在村集 |
| 写操作归属 | 全部 | 乡内申报/流转/派单 | 本村登记/受理/销项 | 接单办结反馈 | 仅上报 |
| 越权行为 | 返回空集或 `FORBIDDEN`（查询→空集，写→FORBIDDEN），均记审计 `result=DENIED` | | | | |

---

## 3. iam 子图（:8091）

职责：组织树、五角色账号、JWT 登录、审计落地。DDL：04 号 §3.1~3.3；缓存：`hsf:md:org:tree:v{n}`（04 号 §8.2 键 1）。

### 3.1 SDL（冻结）

```graphql
# ═══ iam 子图 schemas/iam.graphql（线上 camelCase）═══

"平台账号主体（联邦实体，拥有者=iam）"
type Party @key(fields: "id") {
  id: ID!
  name: String!
  "admin|town|village|keeper|coop"
  role: String!
  "脱敏手机号 138****0601；明文/密文不出图"
  phoneMasked: String!
  orgCode: String!
  org: Organization
}

"组织节点（普通类型，非联邦实体：无跨子图引用方，@key 无必要——D-05-5，评审 R12）"
type Organization {
  code: String!
  name: String!
  "COUNTY|TOWNSHIP|VILLAGE|KEEP_UNIT|COOP"
  level: String!
  parent: Organization
  children: [Organization!]!
}

"登录成功载荷；失败 → UNAUTHENTICATED"
type AuthPayload {
  token: String!
  name: String!
  roles: [String!]!
  partyId: ID!
}

"审计日志（只读投影；仅插入由各子图写路径落库，04 号 §4.8）"
type AuditLog {
  id: ID!
  userId: ID
  userName: String
  action: String!
  objectType: String
  objectId: String
  ip: String
  "OK|DENIED|ERROR"
  result: String!
  createdAt: String
}

type PartyPage { total: Int! page: Int! size: Int! items: [Party!]! }
type AuditLogPage { total: Int! page: Int! size: Int! items: [AuditLog!]! }

type Query {
  "组织树（缓存 hsf:md:org:tree:v{n}）"
  orgTree: [Organization!]!
  party(id: ID!): Party
  "按组织前缀分页（数据范围矩阵 §2.4）"
  parties(orgCode: String, page: Int = 1, size: Int = 20): PartyPage!
  "当前登录人（未登录 → null）"
  me: Party
  "审计过滤查询（FR-704：按用户/动作/时间窗过滤；仅 admin，越权 FORBIDDEN——评审 R16 补落点）"
  auditLogs(userId: ID, action: String, timeFrom: String, timeTo: String,
            page: Int = 1, size: Int = 20): AuditLogPage!
}

type Mutation {
  "登录：SM3 摘要索引 + SM3-PBKDF2 校验；签发 JWT(sub=partyId 字符串)"
  login(phone: String!, password: String!): AuthPayload!
}
```

### 3.2 目录与验收

| 操作 | 签名要点 | 权限 | 错误码 | 缓存动作 | 对应 FR |
|---|---|---|---|---|---|
| `orgTree` | 返回 ≥9 种子节点 | 任意登录 | — | 读键 1 | FR-701① |
| `login` | 成功签发 token、`sub` 为字符串；失败 `UNAUTHENTICATED`（审计 login_fail） | 公开 | UNAUTHENTICATED | 无 | FR-701②③ |
| `me` / `party` | 联邦实体解析入口 | 登录 | — | 无 | FR-703 |
| `parties` | orgCode 前缀过滤 | 登录（范围 §2.4） | — | 无 | FR-702 |
| `auditLogs` | userId/action/timeFrom/timeTo 过滤（FR-704） | **仅 admin** | FORBIDDEN | 无（读池直查） | FR-704 |

> 一期组织/账号维护走种子脚本（幂等双守卫，04 号 §9.1）；`createOrg/createParty` 管理端 CRUD 不在一期 SDL（00 号 §6.1 一期范围表未列）。账号停用/锁定无接口：`account.status`（active/locked，04 号 §3.3）为运维手动置位标志，连续失败自动锁定列二期（03 号 DDD-A3 口径，评审 R6 收敛）。

---

## 4. land 子图（:8092）

职责：地块登记（统一编码）、台账分页、一张图 GeoJSON、权属联邦引用。DDL：04 号 §3.4；缓存：键 2/3。

### 4.1 SDL（冻结）

```graphql
# ═══ land 子图 schemas/land.graphql ═══

"地块（联邦实体，拥有者=land；@key=统一编码）"
type LandParcel @key(fields: "code") {
  code: String!          # L+6位区划+4位序号
  name: String!
  villageCode: String!
  areaMu: Float          "亩"
  soilType: String
  cropType: String
  "ACTIVE|RETIRED"
  status: String!
  "权属人（联邦桩 → iam.Party，FR-104）"
  holder: Party
  "GeoJSON geometry（Polygon）字符串，前端直接 JSON.parse"
  geojson: String
  createdAt: String      "ISO8601+08:00"
}

"联邦桩（iam 拥有）"
type Party @key(fields: "id") { id: ID! }

type LandParcelPage { total: Int! page: Int! size: Int! items: [LandParcel!]! }

"一张图统计（FR-203 角标数据源；评审 R15 补面积聚合落点）"
type LandStats {
  villageCode: String!
  "地块总数（=landParcels.total 同源 COUNT）"
  total: Int!
  "总面积（亩，SUM(area_mu)，未登记面积为 null 的地块不计入）"
  totalAreaMu: Float
}

input RegisterParcelInput {
  name: String!
  villageCode: String!
  holderId: ID
  areaMu: Float
  soilType: String
  cropType: String
  "GeoJSON Polygon 字符串（应用层校验合法性与坐标范围）"
  geojson: String
}

type Query {
  "按编码精确查询（uk_parcel_code 点查）"
  landParcel(code: String!): LandParcel
  "按村分页（数据范围 §2.4：village 强制本村 / coop 强制 holder_id=me / keeper 空集）"
  landParcels(villageCode: String, page: Int = 1, size: Int = 20): LandParcelPage!
  "一张图数据源：整村 FeatureCollection JSON（缓存 hsf:land:geojson:{villageCode}）"
  landGeojson(villageCode: String!): String!
  "一张图统计：地块总数/总面积（FR-203；数据范围 §2.4，keeper 空集返回 0/null）"
  landStats(villageCode: String!): LandStats!
}

type Mutation {
  "登记地块（admin/village）：服务端生成编码 L+区划+序号，返回含 code"
  registerParcel(input: RegisterParcelInput!): LandParcel!
}
```

### 4.2 目录与验收

| 操作 | 权限 | 错误码 | 缓存失效 | 对应 FR |
|---|---|---|---|---|
| `landParcel` | 公开查询（存在即返回） | — | 读键 3 | FR-102③ |
| `landParcels` | 登录（范围 §2.4） | — | 读键 3 | FR-102② |
| `landGeojson` | 登录 | VALIDATION（村码非法） | 读键 2 | FR-103 |
| `landStats` | 登录（范围 §2.4） | VALIDATION（村码非法） | 无（COUNT/SUM 读池直查） | FR-203 |
| `registerParcel` | admin / village | VALIDATION、NOT_FOUND、CODE_DUPLICATED、FORBIDDEN | 删键 2（前缀）+键 3 | FR-102①、FR-101 |

---

## 5. project 子图（:8093）

职责：项目全周期 7 态状态机、项目-地块关联、监理联邦引用。DDL：04 号 §4.1~4.3；缓存：键 4。

### 5.1 SDL（冻结）

```graphql
# ═══ project 子图 schemas/project.graphql ═══

"高标准农田项目（联邦实体，拥有者=project）"
type FarmProject @key(fields: "code") {
  code: String!          # P+6位区划+4位序号
  name: String!
  buildYear: Int!
  content: String
  applicantOrgCode: String!
  "7 态：RESERVED|SUBMITTED|APPROVED|CONSTRUCTING|SUPERVISED|ACCEPTED|PERFORMED（存储值=04号§4.1）"
  state: ProjectState!
  "监理人（联邦桩 → iam.Party，FR-304）"
  supervisor: Party
  "关联地块（联邦桩列表 → land.LandParcel，FR-301）"
  parcels: [LandParcel!]!
  "验收归档结论（PERFORMED 态回填，FR-305）"
  acceptanceNote: String
  createdAt: String
  updatedAt: String
}

enum ProjectState {
  RESERVED SUBMITTED APPROVED CONSTRUCTING SUPERVISED ACCEPTED PERFORMED
}

"联邦桩（iam 拥有）"
type Party @key(fields: "id") { id: ID! }
"联邦桩（land 拥有）"
type LandParcel @key(fields: "code") { code: String! }

type FarmProjectPage { total: Int! page: Int! size: Int! items: [FarmProject!]! }

type ProjectTransition {
  id: ID!
  action: String!        # 冻结存储值=§5.2 mutation 名：submitProject/approveProject/startConstruction/startSupervision/acceptanceProject/acceptanceArchive（FR-302 断言对象——评审 R13）
  fromState: ProjectState!
  toState: ProjectState!
  actorId: ID!
  note: String
  createdAt: String
}

input CreateProjectInput {
  name: String!
  buildYear: Int!
  content: String
  applicantOrgCode: String!
  supervisorId: ID
  "关联地块编码集（校验全部存在于 hsf_md.land_parcel）"
  parcelCodes: [String!]!
}

type Query {
  farmProject(code: String!): FarmProject
  farmProjects(state: ProjectState, buildYear: Int, page: Int = 1, size: Int = 20): FarmProjectPage!
  "流转留痕时间线（FR-302）"
  projectTransitions(code: String!): [ProjectTransition!]!
}

type Mutation {
  "立项（admin/town）：生成 P 编码，初始 RESERVED"
  createProject(input: CreateProjectInput!): FarmProject!
  "申报 RESERVED→SUBMITTED（town/admin）"
  submitProject(code: String!): FarmProject!
  "审批 SUBMITTED→APPROVED（admin）"
  approveProject(code: String!): FarmProject!
  "开工 APPROVED→CONSTRUCTING（admin/town）"
  startConstruction(code: String!): FarmProject!
  "转监理 CONSTRUCTING→SUPERVISED（admin/town）"
  startSupervision(code: String!): FarmProject!
  "验收 SUPERVISED→ACCEPTED（admin）"
  acceptanceProject(code: String!, note: String): FarmProject!
  "验收归档 ACCEPTED→PERFORMED，note 入 acceptance_note（FR-305）"
  acceptanceArchive(code: String!, note: String!): FarmProject!
}
```

### 5.2 状态机转移表（FR-303 全覆盖单测的冻结依据）

| # | from | action（mutation） | to | 允许角色 | 非法路径示例（必须被拒） |
|---|---|---|---|---|---|
| 1 | RESERVED | submitProject | SUBMITTED | town/admin | RESERVED→APPROVED（跳态）、RESERVED→CONSTRUCTING |
| 2 | SUBMITTED | approveProject | APPROVED | admin | SUBMITTED→CONSTRUCTING（跳态） |
| 3 | APPROVED | startConstruction | CONSTRUCTING | admin/town | APPROVED→SUPERVISED |
| 4 | CONSTRUCTING | startSupervision | SUPERVISED | admin/town | CONSTRUCTING→ACCEPTED |
| 5 | SUPERVISED | acceptanceProject | ACCEPTED | admin | SUPERVISED→PERFORMED |
| 6 | ACCEPTED | acceptanceArchive | PERFORMED | admin | **ACCEPTED→SUBMITTED（回退）** |
| — | 其余一切组合 | 任何流转 mutation | — | — | 一律 `INVALID_TRANSITION` 且不落库（**7 态×6 动作**转移矩阵单测全覆盖） |

### 5.3 目录与验收

| 操作 | 权限 | 错误码 | 缓存失效 | 对应 FR |
|---|---|---|---|---|
| `farmProject` / `farmProjects` | admin/town（§2.4） | — | 读键 4 | FR-301 |
| `projectTransitions` | admin/town | NOT_FOUND | 无 | FR-302 |
| `createProject` | admin/town | VALIDATION/NOT_FOUND/CODE_DUPLICATED | 删键 4 | FR-301 |
| 6 个流转 mutation | 见 §5.2 | INVALID_TRANSITION、FORBIDDEN、NOT_FOUND | 删键 4 | FR-302/303/304/305 |

---

## 6. facility 子图（:8094）

职责：设施台账、二维码、工单 4 态闭环。DDL：04 号 §3.5、§4.4~4.5；缓存：键 5/6/9。

### 6.1 SDL（冻结）

```graphql
# ═══ facility 子图 schemas/facility.graphql ═══

"设施（联邦实体，拥有者=facility）"
type Facility @key(fields: "code") {
  code: String!          # F+6位区划+4位序号
  "WELL机井|PUMP_STATION泵站|CANAL渠道|ROAD道路"
  type: FacilityType!
  name: String!
  villageCode: String!
  buildYear: Int
  "NORMAL|BROKEN|REPAIRING|RETIRED"
  status: FacilityStatus!
  "所属地块（联邦桩 → land.LandParcel，FR-401②）"
  parcel: LandParcel
  "扫码 token（仅 facilityByToken 路径使用；不随台账列表下发）"
  qrToken: String
  "本设施工单（嵌套分页，size≤50）"
  workorders(page: Int = 1, size: Int = 20): WorkorderPage!
  createdAt: String
}

enum FacilityType { WELL PUMP_STATION CANAL ROAD }
enum FacilityStatus { NORMAL BROKEN REPAIRING RETIRED }

"巡检/报修工单（非实体，D-05-5）"
type Workorder {
  id: ID!
  sn: String!            # WO+yyyyMMdd+6位序号
  facility: Facility
  "OPEN|ACCEPTED|DISPATCHED|RESOLVED|APPROVED（存储值=04号§4.4）"
  state: WorkorderState!
  description: String
  "联系方式脱敏（SM4 解密后 mask；密文不出图）"
  contactMasked: String
  "上报人（联邦桩 → iam.Party）"
  reporter: Party
  "派单对象 keeper（联邦桩）"
  assignee: Party
  resolveNote: String
  "绩效积分（一期恒 null，FR-404）"
  perfScore: Int
  transitions: [WorkorderTransition!]!
  createdAt: String
  updatedAt: String
}

enum WorkorderState { OPEN ACCEPTED DISPATCHED RESOLVED APPROVED }

"联邦桩（iam 拥有）"
type Party @key(fields: "id") { id: ID! }
"联邦桩（land 拥有）"
type LandParcel @key(fields: "code") { code: String! }

type WorkorderTransition {
  id: ID!
  action: String!        # 冻结存储值=§6.2 mutation 名：acceptWorkorder/dispatchWorkorder/resolveWorkorder/approveWorkorder（与评审 R13 同类统一）
  fromState: WorkorderState!
  toState: WorkorderState!
  actorId: ID!
  note: String
  createdAt: String
}

type FacilityPage { total: Int! page: Int! size: Int! items: [Facility!]! }
type WorkorderPage { total: Int! page: Int! size: Int! items: [Workorder!]! }

input RegisterFacilityInput {
  type: FacilityType!
  name: String!
  villageCode: String!
  parcelCode: String
  buildYear: Int
}

type Query {
  facility(code: String!): Facility
  "台账组合过滤（FR-405：类型/村/状态三条件可任选）"
  facilities(type: FacilityType, villageCode: String, status: FacilityStatus,
             page: Int = 1, size: Int = 20): FacilityPage!
  "工单列表（数据范围 §2.4：keeper 仅 assignee_id=me）"
  workorders(state: WorkorderState, villageCode: String, facilityCode: String,
             page: Int = 1, size: Int = 20): WorkorderPage!
  workorder(id: ID!): Workorder
  "移动端扫码直达（FR-601）：token=uk_qr_token 点查，需登录"
  facilityByToken(token: String!): Facility
}

type Mutation {
  "登记设施（admin/village）：生成 F 编码并签发 qr_token"
  registerFacility(input: RegisterFacilityInput!): Facility!
  "上报工单（任意登录角色；FR-602 移动上报复用本 mutation）"
  reportWorkorder(facilityCode: String!, description: String!, contact: String): Workorder!
  "受理 OPEN→ACCEPTED（village/town/admin）"
  acceptWorkorder(id: ID!): Workorder!
  "派单 ACCEPTED→DISPATCHED，assigneeId 必填且须为 keeper（town/admin）"
  dispatchWorkorder(id: ID!, assigneeId: ID!): Workorder!
  "办结 DISPATCHED→RESOLVED，note 必填（被派单 keeper 或 admin）"
  resolveWorkorder(id: ID!, note: String!): Workorder!
  "销项 RESOLVED→APPROVED（village/town/admin）"
  approveWorkorder(id: ID!): Workorder!
}
```

### 6.2 工单状态机转移表（FR-403 冻结依据）

| # | from | action | to | 允许角色 | 关键校验 |
|---|---|---|---|---|---|
| 1 | OPEN | acceptWorkorder | ACCEPTED | village/town/admin | — |
| 2 | ACCEPTED | dispatchWorkorder | DISPATCHED | town/admin | `assigneeId` 对应 party.role=keeper，否则 VALIDATION |
| 3 | DISPATCHED | resolveWorkorder | RESOLVED | assignee 本人或 admin | `note` 必填（办结反馈，FR-403） |
| 4 | RESOLVED | approveWorkorder | APPROVED | village/town/admin | — |
| — | 其余一切组合 | — | — | — | `INVALID_TRANSITION` 且不落库（**5 态×4 动作**矩阵单测全覆盖——评审 R21⑨） |

### 6.3 目录与验收

| 操作 | 权限 | 错误码 | 缓存失效 | 对应 FR |
|---|---|---|---|---|
| `facility` / `facilities` | 登录（§2.4） | — | 读键 5 | FR-401/405 |
| `facilityByToken` | 登录 | NOT_FOUND（token 失效） | 读键 6 | FR-402/601 |
| `workorders` / `workorder` | 登录（§2.4） | — | 无（走库，工单流转频繁） | FR-403/702 |
| `registerFacility` | admin/village | VALIDATION/NOT_FOUND/CODE_DUPLICATED | 删键 5 | FR-401① |
| `reportWorkorder` | 任意登录 | NOT_FOUND、VALIDATION | 删键 9 | FR-403/602 |
| 4 个流转 mutation | 见 §6.2 | INVALID_TRANSITION、FORBIDDEN、NOT_FOUND、VALIDATION | 删键 9 + 键 5 | FR-403/404 |

---

## 7. sim 服务（REST，:8095，NodePort 31211）

> **非 GraphQL**（D-05-3）：IoT 时序写入与告警为机器/脚本流量（tick 定时、k6 压测直打），REST 更直接；不进联邦组合，避免大 payload 拖累网关。鉴权复用同一 JWT（`Authorization: Bearer`），错误码复用 §2.1（HTTP 映射列生效）。**限流仅网关通道**（RATE_CAP=6000/min，FR-705/E2E 用例 12 走 30090）：sim 直连端点（31211）一期不做端点级限流——机器流量直打为压测前提（ADR-HSF-003 R3），如需限流列二期（评审 R10：删除端点 1 的 429 承诺）。

### 7.1 端点契约（冻结）

| # | 方法/路径 | 入参 | 出参（200） | 权限 | 错误 | 对应 FR |
|---|---|---|---|---|---|---|
| 1 | `POST /sim/tick` | query：`steps=1`（生成步数）；body 无 | `{"inserted": <int>, "batches": <int>, "alerts_created": <int>}` | 登录（任意角色） | 401（限流仅网关通道，见 §7 说明——评审 R10） | FR-502 |
| 2 | `GET /sim/sensors` | query：`villageCode`、`metric`、`page`、`size` | `{"total":48,"page":1,"size":20,"items":[{sensorCode,metric,villageCode,parcelCode,status,lastValue,lastRecordedAt}]}` | 登录 | — | FR-501 |
| 3 | `GET /sim/latest` | query：`villageCode` | `{"items":[{sensorCode,metric,value,recordedAt}]}`（快路径读 `sensor.last_*`，缓存键 10） | 登录 | — | FR-502③ |
| 4 | `GET /sim/alerts` | query：`state`(OPEN/PROCESSING/CLOSED)、`villageCode`、`page`、`size` | `{"total":…,"page":…,"size":…,"items":[{id,sensorCode,ruleCode,metric,trigValue,trigTime,villageCode,state,handledBy,handledAt,closedAt,note}]}` | 登录（范围 §2.4 告警行） | — | FR-504 |
| 5 | `GET /sim/alerts/statistics` | query：`dim=state|rule|village`、`villageCode` | `{"dim":"village","items":[{"key":"53060010101","count":12},…]}`（缓存键 7/8） | 登录 | — | FR-505 |
| 6 | `POST /sim/alerts/{id}/transition` | body：`{"action":"handle"|"close","note":"…"}` | 告警对象（更新后） | admin/town/village | **二期实现（A-05-2）**；接口冻结于本期契约为 P2 | FR-504（最低口径=端点 4/5） |
| 7 | `GET /healthz` `GET /readyz` `GET /metrics` | — | 探针/Prometheus text | 公开/公开/内网 | — | NFR-08 |

### 7.2 告警规则引擎（一期 4 条，阈值在 04 号 §4.7/本文双冻结）

| rule_code | metric | 条件 | 告警 state 初值 | 边界值单测（FR-503） |
|---|---|---|---|---|
| RULE_DROUGHT | soil_moisture | `value < 15` | OPEN | 14.9 触发 / 15.1 不触发 |
| RULE_WATERLOGGING | soil_moisture | `value > 85` | OPEN | 85.1 触发 / 84.9 不触发 |
| RULE_GALE | wind_speed | `value > 17` | OPEN | 17.1 触发 / 16.9 不触发 |
| RULE_PEST | pest_index | `value > 80` | OPEN | 80.1 触发 / 79.9 不触发 |

规则执行契约：① 仅评估**本次 tick 新生成读数**；② 命中写 `hsf_biz.alert_record`，`(sensor_code, rule_code, trig_time)` 幂等（04 号 §4.6）；③ 一条读数命中多条规则 → 多条告警；④ 告警写入后失效缓存键 7/8。

### 7.3 tick 写入契约（NFR-04）

```
POST /sim/tick?steps=1
  → 每步对 48 台 sensor 各生成 1 条读数（值域见 04 号 §4.7）
  → INSERT 1000 条/批 execute_many（写池 hsf_app）
  → 更新 sensor.last_value/last_recorded_at
  → 规则引擎评估新读数 → alert_record
  → 响应 inserted/batches/alerts_created
压测口径：≥10 批/s × 1000 条 持续 60s ≥ 1 万条/s，0 失败（k6 直打 31211）
```

---

## 8. 集成与预留

### 8.1 与 DV-IAS 的联邦互查预留（`hsf/docs/00` §2）

| 项 | 一期口径 |
|---|---|
| 实体同名对齐 | HSF 四实体命名与 DV-IAS `Party`(@key id) 模式一致，语法层可被对方网关组合 |
| 语义边界 | **不打通**：HSF Party=平台账号主体（admin/town/village/keeper/coop），DV-IAS Party=村户居民——同名异义，互查前须做 ID 映射 ACL（三期评估） |
| 上级平台导出 | 统一编码（L/P/F/S）即可映射"全国农田建设综合监测监管平台"上报粒度；导出通道三期（`hsf/docs/00` §3 集成对接行） |

### 8.2 明确不做（一期边界，引用 `hsf/docs/00` §4.2）

MCP 工具层、A2A、消息推送、WebSocket/SSE、APQ、外部系统 ACL（短信/支付/政务共享/气象拉取）——均不在一期；二期按真实 IoT 与用户量评估接入顺序。

---

## 9. 假设汇总

见 §0 决议与假设清单（D-05-1~8、A-05-1~2）。传导自 01 号 SRS 的 A4（传感器 metric/编码业务基线）已在 04 号 §4.7 冻结闭环。

---

## 10. 验收矩阵（接口层，与 04 号 V-01~15 并行执行）

| # | 验收项 | 验证方法（验收环境执行） | 对应 |
|---|---|---|---|
| V-16 | 四子图 SDL 组合成功 | `curl http://10.167.2.175:30090/ -X POST -H 'Content-Type: application/json' -d '{"query":"{ __typename }"}'` → 200 且网关日志无组合错误；各子图 `_entities` 含 §1.3 全部 @key | §1 |
| V-17 | 联邦跨域（holder） | `{"query":"{ landParcel(code:\"L5306000001\"){ code holder { name } } }"}` → holder.name=种子权属人 | §1.3/FR-104 |
| V-18 | 联邦跨域（supervisor+parcels） | `{ farmProject(code:"P5306000001"){ supervisor { name } parcels { code } } }` → 真实种子数据 | FR-304/301 |
| V-19 | 联邦跨域（parcel） | `{ facility(code:"F5306000001"){ parcel { code } } }` | FR-401② |
| V-20 | 登录与 401 口径 | 错密 `login` → errors[0].extensions.code=UNAUTHENTICATED（HTTP 200，D-05-2）；无 token 查 `me` → me=null | FR-701③ |
| V-21 | 限流 429 | 循环 >RATE_CAP 次请求 → HTTP 429 + RATE_LIMITED | FR-705 |
| V-22 | 非法流转拒绝 | RESERVED 项目直接 `startConstruction` → INVALID_TRANSITION；DB 状态仍 RESERVED（不落库）；转移矩阵单测 7 态×6 动作 / 5 态×4 动作全过 | FR-303/403 |
| V-23 | 编码冲突 | 并发 registerParcel 同村 → 至多一个成功，冲突者重试或 CODE_DUPLICATED，无重复码 | FR-101 |
| V-24 | 数据范围 | village 登录 `landParcels` 仅本村；keeper `workorders` 仅 assignee=me；coop `landParcels` 仅 holder=me | FR-702①② |
| V-25 | 伪造身份头 | 携带伪造 `x-user-id: 1` 无 token 访问写操作 → UNAUTHENTICATED（网关剥离伪造头） | FR-703 |
| V-26 | 工单全闭环 E2E | report→accept→dispatch(keeper)→resolve(note)→approve 依序成功；非法穿插步骤被拒 | FR-403 |
| V-27 | tick 与最新值 | 连续两次 `/sim/tick` → inserted 单调递增；`/sim/latest` 返回 48 台最新值 | FR-502 |
| V-28 | 告警边界与统计 | 4 规则边界值单测过；`/sim/alerts?state=OPEN` 过滤正确；statistics 与明细聚合一致 | FR-503/505 |
| V-29 | 分页上限 | `size=500` 请求 → 实际返回 ≤100 条且 size=100（静默钳制） | §2.2 |
| V-30 | 错误脱敏 | 构造子图内部异常 → 网关返回 code=UNAVAILABLE + 固定中文文案，无堆栈/SQL 泄漏 | §2.1 |

---

## 11. 附：SDL ↔ Python 字段映射示例（开发对照，A-05-3）

| SDL（线上） | Python（strawberry） | 说明 |
|---|---|---|
| `landParcel` | `def land_parcel(...)` | Query 字段默认转换 |
| `villageCode` | `village_code: str` | 类型字段默认转换 |
| `registerParcel(input:)` | `def register_parcel(self, info, input: RegisterParcelInput)` | mutation 默认转换 |
| `Party._resolve_reference` | `@classmethod def _resolve_reference(cls, id: strawberry.ID)` | 桩模式，`dv-ias/services/governance/app/schema.py:13-15` |
