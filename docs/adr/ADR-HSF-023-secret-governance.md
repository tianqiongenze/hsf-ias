# ADR-HSF-023：运行时密钥治理——真值出库、fail-closed、prod/dev 分值、全量轮换（R-07）

| 项 | 内容 |
|---|---|
| 编号 | ADR-HSF-023（R-17 补登——R-07 阶段 0 实施，决策与契约变更按治理规则登记） |
| 状态 | 已采纳（Accepted，2026-11-14 实施并验收） |
| 关联 | docs/24 §4 R-07（审核缺口#6：运行时密钥明文入库 dev=prod 同值+代码回退密钥）、R-17（补登）、ADR-HSF-022（SM4 同族）、R-20（MQTT 分账号增键协同）、docs/04 §7.3 / docs/07 §1.4（键名冻结清单）、CONVENTIONS §2.18/§2.19、`scripts/gen-secrets.sh`（部署期注入实现） |
| 决策日 | 2026-11-14 |

## 背景

- 一期 `k8s/00-base.yaml:108-141` 两命名空间 hsf-secrets 六键（DB_PASS/DB_PASS_RO/REDIS_PASS/MQTT_PASS/
  TIMESCALE_PASS/JWT_SECRET）**明文实值且完全同值**（dev 区注释"与 hsf-prod 同值"）——dev 泄露即可伪造
  prod 令牌（审核缺口#6，high）。
- 代码级回退密钥两处：`gw/gateway.js:25` 与 `hsf_common/web.py:15`（`'HsfJwt-2026-…'`）——env 缺失时静默
  回退内置常量，密钥治理被架空。

## 决策

1. **真值出库**：Secret manifest 改空占位符，部署期由 `scripts/gen-secrets.sh`（部署机 `openssl rand`
   CSPRNG 生成；`k8s/gen/` 真值文件 chmod 600 且 .gitignore 排除，不入 git）注入三处 Secret
   （hsf-prod/hsf-dev/infra）；幂等语义=真值文件已存在则复用（重复执行不轮换），全量轮换须显式
   `ROTATE=1 ACK_ROTATE=1`；
2. **分值口径**：JWT_SECRET prod/dev **强制分值**（脚本内断言冲突即退出）；DB/Redis/MQTT/Timescale 为
   共享单实例（各一套账号）两 ns 同值——组件级分值需独立实例，显式声明不强制；
3. **代码侧 fail-closed**：`gw/gateway.js` JWT_SECRET 非空断言（缺失 `process.exit(1)`）、
   `hsf_common/web.py` JWT_SECRET 非空断言（导入期抛异常）、`crypto.py` SM4 长度断言——**两处内置回退
   密钥删除，禁止回补**；
4. **部署顺序冻结（CONVENTIONS §2.19）**：00-base（占位符 Secret）→ 10-redis → gen-secrets.sh（覆盖
   三处 Secret 真值）→ 20-services → rollout 全绿；空键 Secret 被 apply 后服务 fail-closed 拒启动=
   预期哨兵行为，勿绕过；
5. **全量轮换顺序**：组件改密（TiDB ALTER USER→TimescaleDB ALTER USER→Redis Secret+滚动+cluster
   create→mosquitto passwd 重建）→ gen-secrets.sh 覆盖 Secret → 全量服务滚动 → rollout 全绿 →
   E2E 25/25 回归（滚动前核对 ResourceQuota 余量——QUOTA-1 教训）；
6. **git 历史风险**：内网私有仓库，轮换前旧值存在于历史提交——默认记录风险不重写历史；彻底清除须
   `git filter-repo`（决策项，须评审）。

## 影响

- 冻结清单增补留痕：00-base.yaml Secret 键形态改占位符（键名清单不变）；gen-secrets.sh 键集=
  键名冻结清单的部署期镜像（增键须同步两处+本目录新 ADR，如 R-20 的 MQTT_PASS_SIM/IOT 由
  ADR-HSF-018 登记）。
- 回退：Secret 回滚旧值+rollout（旧值窗口内保留至验收通过）；fail-closed 断言回退=恢复内置常量
  （推翻本 ADR，须新编号）。
- 验证方式：`grep -rn "HsfJwt-2026|HsfApp-2026|HsfRo-2026|HsfRedis-2026|HsfMqtt-2026|HsfTs-2026" hsf-ias/`
  → 0 命中；prod/dev JWT_SECRET 分值（kubectl 取值对比）；全负载 rollout 全绿；E2E 25/25 回归 PASS。
