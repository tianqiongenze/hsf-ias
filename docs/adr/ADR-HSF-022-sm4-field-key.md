# ADR-HSF-022：SM4 字段加密密钥外置与轮换（R-06）

| 项 | 内容 |
|---|---|
| 编号 | ADR-HSF-022（R-17 补登——docs/24 §4 R-06 实施路径⑥"冻结清单增补按 docs/02 §9 出 ADR（联动 R-17）"） |
| 状态 | 已采纳（Accepted，2026-11-14 实施并验收） |
| 关联 | docs/24 §4 R-06（审核缺口#5：SM4 密钥源码内置）、R-07（运行时密钥治理同族，ADR-HSF-023）、R-17（补登）、docs/07 §1.4（Secret 增键清单）、docs/04 §7.3（凭据分发键名清单）、CONVENTIONS §2.17、`services/common/hsf_common/crypto.py`（fail-closed 实现） |
| 决策日 | 2026-11-14 |

## 背景

- 一期实现 `crypto.py:8` 源码内置缺省 SM4 密钥常量（注释自认"生产必须外置轮换"），全仓 `grep SM4_FIELD_KEY`
  零命中——包括 hsf-prod 在内全部环境实际使用公开常量密钥，`phone_enc`（iam）/`contact_enc`（facility）
  密文可用公开密钥解密（审核缺口#5，high）。

## 决策

1. Secret `hsf-secrets` 两命名空间增键 **`SM4_FIELD_KEY`**（32 字节=64 hex 字符，CSPRNG 生成，
   `scripts/gen-secrets.sh` 部署期注入，真值不入 git——R-07 口径）；
2. `20-services.yaml` 为消费方（iam/facility）**显式 secretKeyRef 注入**（envFrom 全量注入之外的显式
   依赖声明，防漏配静默回退）；
3. `crypto.py` **fail-closed**：env 缺失/非 32 字节 hex → 启动/首次加解密即失败，**源码禁止内置回退密钥**；
4. 冻结清单回写：docs/07 §1.4 Secret 增键清单、docs/04 §7.3 键名清单增补 `SM4_FIELD_KEY` 行（本 ADR 为
   增补留痕）；
5. 轮换流程入 CONVENTIONS §2.17：gen-secrets 生成新值 → 存量密文迁移（旧键解密→新键重加密，行级校验，
   迁移前建 `*_bak_r06` 备份表）→ Secret 更新 → 滚动 iam/facility → E2E 登录/脱敏用例复测；
   **迁移完成前不删除旧键值备份**。

## 影响

- 消费面：iam `phone_enc`、facility `contact_enc` 两处字段加密；其它服务不消费（不注入）。
- 兼容性：源码常量密钥路径删除——旧镜像内常量在新 Secret 注入后不再生效（env 显式覆盖）。
- 回退：Secret 恢复旧值 + env 重新注入即可解密存量（迁移完成前旧键保留在 Secret 历史版本）。
- 验证方式：`grep -rn "0123456789abcdef" hsf-ias/` → 0 命中；`kubectl -n hsf-prod get secret hsf-secrets
  -o jsonpath='{.data.SM4_FIELD_KEY}' | base64 -d | wc -c` → 64；`env -u SM4_FIELD_KEY` 加解密 → 抛异常；
  E2E 登录/脱敏展示用例 PASS。
