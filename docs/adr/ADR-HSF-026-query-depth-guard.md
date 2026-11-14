# ADR-HSF-026：GraphQL 查询深度/复杂度护栏（推翻 D-05-8"一期不做"）+ CI 组合校验（R-26）

| 项 | 内容 |
|---|---|
| 编号 | ADR-HSF-026（R-17 建库同批立卷；决策日 2026-11-14） |
| 状态 | 已采纳（Accepted，2026-11-14 实施并本地功能验收；集群侧 E2E 回归待 gw:3.0.4 部署窗口） |
| 关联 | docs/24 §4 R-26（对标③：GraphQL 应叠加深度/复杂度操作限制或受信文档；子图改动组合校验进 CI）、docs/05 §0 D-05-8（一期"不做"决议——**部分推翻**，本文档已同步注记）、docs/05 §1.4（gw env 表增补查询护栏行）、docs/05 §2 D-05-2（错误形态：HTTP 4xx + errors[0].extensions.code）、`gw/gateway.js`（queryGuardViolation 实现）、`scripts/static_gate.py`（检查项 14 + CI 组合校验提示）、R-12（enable_federation_2 门禁项并入 CI 清单） |
| 决策日 | 2026-11-14 |

## 背景

- D-05-8 冻结"网关不做查询深度限制/APQ"，理由=内网受控+JWT+限流+鉴权覆盖一期威胁模型；对标③
  （GraphQL 治理最优实践）明确要求叠加深度/复杂度操作限制，恶意客户端（即使持有效 JWT）可构造
  20 层嵌套查询放大子图解析与组合开销。
- 一期 gateway.js:95 注释自认"查询深度限制/APQ 一期不做"——缺口真实存在。
- 组合面：gw 启动 serviceList 手动组合、无 CI 校验——子图 SDL 变更靠部署期发现（R-12 曾漏）。

## 决策

1. **gw express 层查询护栏**（GraphQL 解析前、限流中间件后）：`queryGuardViolation` 基于 graphql-js
   parse AST 计算深度（字段嵌套层数，片段按 spread 内联展开+环防护）与总可选字段数（片段重复展开按
   处计数的保守口径）——违规 → **HTTP 400 + `QUERY_TOO_DEEP`**（D-05-2 错误形态一致）；
2. **阈值（docs/24 §4 R-26 口径）**：`QUERY_DEPTH_MAX` 默认 **10**（现有业务查询实测最大 6 层，裕度
   充分；20 层恶意深查询必拒）、`QUERY_FIELD_COUNT_MAX` 默认 **300**（按现有查询形态调参）；
3. **回退开关**：`QUERY_GUARD=off`（docs/24 R-26 风险行"阈值过严误伤报表查询——灰度 env 开关；
   回退=开关关闭"）——代码常驻（static_gate 检查项 14 钉住存在性），开关仅运行时；
4. **CI 组合校验提示**（static_gate 常驻 INFO 输出）：子图 SDL 变更 → gw 本地组合 → supergraph SDL
   快照 diff 留档——内网无 rover，**以 gw 组合输出为等价物**（docs/24 R-26 实施路径②原口径）；
   R-12 的 enable_federation_2 门禁项（检查项 7）并入本清单；
5. **APQ 维持不做**（D-05-8 该半项不被推翻——内网场景无自动持久化诉求）。

## 影响

- 契约面：docs/05 §0 D-05-8 注记部分推翻、§1.4 增查询护栏行；新错误码 `QUERY_TOO_DEEP` 进入
  错误码面（400，非业务码通道）。
- 兼容性：语法错误在护栏内放行（交 Apollo 原生 GRAPHQL_PARSE_FAILED，不双重处理）；GET 请求
  （query 参数）不在护栏面（POST body 判定——现有客户端全部 POST，GET 面留待 Ingress 收口时统一）。
- 验收（本会话已执行）：gw/gateway.js node --check 语法通过；功能测试 17/17 PASS（真实 graphql-js
  AST：业务查询 6 形态放行/20 层深查询拒绝（depth=21>10）/阈值边界（depth=10 放行、11 拒绝）/
  300 字段放行 301 拒绝/片段环不悬挂/片段重复展开保守计数/语法错误放行/QUERY_GUARD=off 关闭）；
  static_gate violations=0（含新检查项 14 + 负向自测 4 违规命中）。
- 回退：`QUERY_GUARD=off`（manifest 单行）；代码回退=移除护栏中间件与函数（需推翻本 ADR，新编号）。
