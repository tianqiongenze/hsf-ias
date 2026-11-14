# ADR-HSF-013：缓存独立集群 hsf-redis（迁移登记）

> 迁移登记（R-17，2026-11-14）：自 `hsf/docs/02` §3.5 迁移为独立 ADR 文件（四要素齐备）。

| 项 | 内容 |
|---|---|
| 状态 | 已采纳（一期冻结）｜ 决策日期 2026-11-07（§3.5 编号固化）｜ 决策人：架构设计师（工作流） |
| 背景 | 会话态/验证码/热点缓存需要跨服务共享且可水平扩展；单实例 Redis 为单点，Sentinel 运维面复杂；集群 infra 命名空间已有先例 |
| 决策 | 缓存独立集群 `hsf-redis` 3 节点（infra 命名空间，镜像 `base/redis:8-alpine`，headless svc `hsf-redis-h`；`--cluster-announce-hostname "${HOSTNAME}.hsf-redis-h.infra.svc.cluster.local"` 必须用 `${}` 展开——CONVENTIONS §3.5 部署口径） |
| 理由 | 3 节点 Cluster 兼顾高可用与最小资源；hostname 广播使 pod 重建后地址稳定 |
| 影响与限制 | **客户端红线（T-3，一期真实事故）**：Node ioredis 对 hostname 广播集群无限刷新循环——二期起一律 Python `redis.cluster.RedisCluster`（已实证可用），全仓禁用 ioredis（static_gate 检查项 8/V-4）；gw 限流判定二期移至 ratelimit-svc（gw 无 Redis 客户端，docs/07 §5.2）；密码入 hsf-secrets `REDIS_PASS`（R-07 真值出库） |
| 验证方式 | `redis-cli --cluster check` 3 节点全槽覆盖；ratelimit/iot 跨副本共享计数（docs/07 §5.4 六步口径） |
