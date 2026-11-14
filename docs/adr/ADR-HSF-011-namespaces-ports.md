# ADR-HSF-011：命名空间/端口/NodePort 冻结（迁移登记）

> 迁移登记（R-17，2026-11-14）：自 `hsf/docs/02` §3.5 迁移为独立 ADR 文件。**影响列更新（R-17 同批）**：
> gw NodePort 30090→30099 顺延事实（2026-11-07 部署实测，`hsf-ias/k8s/20-services.yaml:6-8` 头注）
> 并入本 ADR 影响列；docs/24 §4 R-17 原拟以新编号登记该顺延，按存量豁免口径（决策在目录建立前发生、
> 以 manifest 头注留痕在先）并入本件，不倒序占用新编号。

| 项 | 内容 |
|---|---|
| 状态 | 已采纳（一期冻结）｜ 决策日期 2026-11-07（§3.5 编号固化）｜ 决策人：架构设计师（工作流） |
| 背景 | 共享多租户集群（k8s-master/k8s-worker1），命名空间隔离租户；端口分配需防跨服务冲突与跨租户占用（NodePort 为集群全局资源） |
| 决策 | 命名空间 hsf-prod/hsf-dev；子图端口 iam 8091/land 8092/project 8093/facility 8094/sim 8095；gw 业务 8090+探针 9090；web 80；NodePort gw **30099**/web 31210/sim 31211（上线前 `ss -tln` 核验，冲突顺延并记录）。二期/三期增补（CONVENTIONS-P2/P3）：iot 8097（NodePort 31213）、ratelimit 8096、timescale 5432、mqtt 1883、rs 8098（NodePort 31214）、uplink 8099（ClusterIP 不出集群网） |
| 理由 | 端口面一次冻结杜绝"部署期随手改端口"漂移；gw 8091 与 iam 冲突的真实事故以独立探针端口 9090 收口（CONVENTIONS §2.5） |
| 影响与限制 | **NodePort 顺延事实（R-17 影响列更新）**：gw 原定 30090 被跨租户 dify-plus/redis8-node-0 占用（ss 不可见、仅集群分配表可见），2026-11-07 顺延为 30099——触发器③（NodePort 变更）在目录建立前已发生，以 manifest 头注+CONVENTIONS.md:40 留痕，不倒序补立新编号（README §3 注记）；此后任何 NodePort 变更必须新开 ADR。新 NodePort 前置核验义务（`ss -tln`）为部署纪律（CONVENTIONS §1） |
| 验证方式 | `kubectl get svc -A \| grep -E "30099\|31210\|31211\|31213\|31214"` 与 manifest 一致；docs/16 R-16 已将活跃契约文档 26 处 30090 收口为 30099 |
