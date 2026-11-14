# ADR-HSF-027：JWT RS256 非对称签名演进（HS256 冻结兼容开关保留）（R-31）

| 项 | 内容 |
|---|---|
| 编号 | ADR-HSF-027（R-17 建库同批立卷；决策日 2026-11-14） |
| 状态 | 已采纳（Accepted，2026-11-14 实施并本地功能验收——python/node 双侧验签回归全 PASS；集群 cutover 与 E2E 部署窗口执行，步骤见影响列 runbook） |
| 关联 | docs/24 §4 R-31（对标③：RS256 防子图伪造令牌）、docs/05 §1.4（JWT 冻结行增补）、docs/04 §7.3 / docs/07 §1.4（Secret 键清单增补）、ADR-HSF-023（密钥治理口径延续）、R-08/ADR-HSF-019（TTL 语义不变）、`services/common/hsf_common/web.py`（issue_token/decode_token）、`gw/gateway.js`（verifyJwt）、`scripts/gen-secrets.sh`（RSA 密钥族生成） |
| 决策日 | 2026-11-14 |

## 背景

- 对标③："推荐 RS256 非对称签名防子图伪造令牌"——现状 HS256 共享密钥下 gw 与全部子图同值
  （R-07 治理后仍为对称密钥）：任何持有 JWT_SECRET 的服务/副本泄露即可伪造网关可信令牌。
- 威胁面收敛目标：签名私钥只存于签发方（iam）；验证方（gw + 四 REST 服务）只持公钥。
- 冻结契约不破坏纪律：docs/05 §1.4 "JWT HS256 共享密钥"为冻结口径，演进必须以开关形态落地并
  按 docs/02 §9 出 ADR（本件）。

## 决策

1. **密钥族入 hsf-secrets**（两命名空间，prod/dev 分值——签名密钥同 JWT_SECRET 分值原则）：
   `JWT_ALG`（**HS256 默认=冻结兼容开关**）、`JWT_KID`、`JWT_PRIVATE_KEY`（签发方 iam，base64(PEM)
   单行——`kubectl --from-env-file` 不接受多行）、`JWT_PUBLIC_KEY`（gw/REST 验签）、
   `JWT_PUBLIC_KEY_PREV`/`JWT_KID_PREV`（轮换窗口旧公钥，可空）；RSA-2048 由 gen-secrets.sh 生成
   （含形态校验与 prod/dev 分值断言）；**私钥不注入 gw**（最小授权）；
2. **签发**（`hsf_common.web.issue_token`）：JWT_ALG=HS256 → 冻结路径逐字节不变；RS256 → 私钥签发 +
   kid 头；claims 形态/exp 语义不变（TTL 2h 延续 ADR-HSF-019）；
3. **验签**（gw `verifyJwt` + 四 REST 服务统一走 `hsf_common.web.decode_token`）：HS256 → JWT_SECRET
   对称验签（冻结路径）；RS256 → kid 选公钥（kid=JWT_KID_PREV 且旧公钥在位 → 旧钥；缺/未知 kid →
   当前公钥兜底）后 algorithms=['RS256'] 白名单验签——**伪造 HS256 共享密钥令牌被拒绝**；
4. **fail-closed**：JWT_ALG=RS256 且公钥/私钥缺失 → 启动即退出（gw process.exit(1)/web.py 导入期
   RuntimeError——与 R-07 哨兵行为同纪律）；
5. **kid 轮换语义**：gw 支持双公钥（当前+PREV）；REST 服务单公钥（旧令牌 TTL 2h 远短于轮换窗，
   双公钥仅 gw 需要——诚实边界留档）。

## 影响

- **RS256 cutover runbook**（单窗口，顺序红线）：① gen-secrets.sh 增量补键（默认 JWT_ALG=HS256，
  存量键不轮换）→ ② 重建镜像 gw:3.0.4/iam:1.0.3（含 R-26/R-31 代码）+ 四 REST 服务 cutover 版
  （验签代码已入工作区，HS256 默认下旧镜像行为不变）→ ③ 00-base 增键 apply + gen-secrets 注入
  Secret → ④ 滚动 iam/gw（HS256 模式，行为零变化）→ ⑤ E2E 登录/错密回归 PASS → ⑥ 切换 Secret
  `JWT_ALG=RS256` + 全量滚动（iam/gw + 四 REST）→ ⑦ E2E 25/25 + 伪造 HS256 令牌拒绝实测；
  ⑥/⑦ 之前四 REST 服务必须已重建（否则 RS256 令牌在其本地验签 401）。
- 契约面：docs/05 §1.4 增补行、docs/04 §7.3/docs/07 §1.4 键清单增补（本 ADR 留痕）。
- 验收（本会话已执行）：python 单测 7/7（HS256 冻结路径不变/RS256 往返+kid 头/伪造 HS256 拒绝/
  他钥拒绝/公钥缺失 fail-closed/PEM base64 编解码）；node 功能测试 8/8（RS256 验签/kid=PREV 轮换/
  伪造 HS256 拒绝/无 kid 兜底/未知 kid 兜底/HS256 默认路径不变/HS256 拒 RS256 令牌/缺公钥启动即退出）；
  gen-secrets.sh 双分支功能测试（全新生成/增量补键：PROD/DEV 密钥对配对+分值+RSA 形态校验全过）。
- 回退：Secret `JWT_ALG` 键值回 `HS256` + 滚动（单键回退，密钥无需更换）；代码回退=还原
  verifyJwt/issue_token（需推翻本 ADR，新编号登记）。
