# ADR-HSF-015：依赖锁定版本（迁移登记）

> 迁移登记（R-17，2026-11-14）：自 `hsf/docs/02` §3.5 迁移为独立 ADR 文件（四要素齐备）。

| 项 | 内容 |
|---|---|
| 状态 | 已采纳（一期冻结）｜ 决策日期 2026-11-07（§3.5 编号固化）｜ 决策人：架构设计师（工作流） |
| 背景 | Python 依赖次版本漂移曾多次引发行为变更（strawberry 联邦解析器行为随版本变化——CONVENTIONS §2.4 事故）；构建可重复性要求锁死全量版本 |
| 决策 | `requirements-base.txt` 锁定：`pydantic==2.10.6`、`graphql-core==3.2.5`、`strawberry-graphql==0.254.1`、`PyJWT`、`pymysql`、`redis`、`cryptography`、`uvicorn`、`fastapi`、`prometheus-client`、`httpx`（完整清单以 CONVENTIONS §2.1/`hsf-ias/requirements-base.txt` 为准）；Node 侧 gw 依赖以 `package.json` + `package-lock.json` 锁定（失配由 npm ci 拦截） |
| 理由 | 版本漂移不可控=契约行为漂移；BASE 镜像+锁定清单使构建可重复 |
| 影响与限制 | PyJWT≥2.10 强制 `sub` 必须字符串（`issue_token` 内 `str(sub)`，CONVENTIONS §2.1）；升级任何锁定依赖（如 strawberry）属契约面变更，须评审+门禁+回归三件套 |
| 验证方式 | `pip freeze`（BASE 内）与 requirements-base.txt 逐包对账；smoke-import 全绿（scripts/smoke-import.sh，IMPORT-OK 才可部署） |
