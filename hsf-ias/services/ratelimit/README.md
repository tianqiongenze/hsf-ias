# ratelimit — 全局限流服务（:8096，二期新增）

对齐：`hsf/docs/07 §5`（全局限流设计冻结）、`CONVENTIONS-P2 T-1/T-2/T-3`。
基础镜像 `10.167.2.175:30500/szxc/base:1.2`，镜像 tag 二期 **2.0**，ClusterIP（仅集群内网）。

## 一期回退的根因与本服务定位

一期 gw 进程内令牌桶（R-5 回退：ioredis Cluster 对 hostname 广播集群无限刷新循环）按
**副本各自计数**，全局容量 = cap × gw 副本数（HPA 4~6 副本时 24000~36000/min），限流形同虚设。
二期判定移入本服务：gw 中间件 HTTP 调用 `/limit`，**所有 gw 副本落在同一 Redis 窗口键上**——
跨副本全局一致（docs/07 §5.4 验证口径：单 pod 1500 突发 vs cap=1200 出现 429，另一 pod 视角同样 429）。

## 契约（docs/07 §5.1 冻结）

### GET /limit?key=&cost=

| 项 | 约定 |
|---|---|
| 计数键 | `hsf:rl:{key}:{分钟epoch}`，分钟 epoch = `int(unix秒 // 60)` |
| 算法 | 固定窗口，**Lua 原子脚本单次 RTT**：`INCRBY(key,cost)`，`cur==cost`（本窗口首次写入）时 `EXPIRE(key,120)`——INCRBY 与 TTL 置位同脚本原子完成，消除"INCR 后 EXPIRE 前"崩溃留下无 TTL 永生键的窗口（§9-⑥：T-2"INCR+EXPIRE 120"的原子化等价实现，键格式/TTL/响应语义不变） |
| TTL | **120s**（>60s 窗长覆盖时钟偏斜；窗口键翻分钟后不复用，首写置位一次即自清） |
| 容量 | env `RATE_CAP`，二期冻结 **1200**/key/min（§9-③：固定窗口下 429 确定性可复现，根除一期令牌桶"永打不穿"验收缺陷） |
| 判定 | `cur ≤ cap` → `{"allowed": true, "remain": cap-cur}`；超限 → `{"allowed": false, "remain": 0}`（拒绝计数仍累计，防重试绕过） |
| cost | 默认 1；cost>1（E2E/验证用）一次计入等价 N 请求占用（§5.4 步骤 2 的 `cost=1200` 打满窗口） |
| key 来源 | gw：登录 `u:{jwt.sub}`，匿名 `ip:{remoteAddress}`（CONVENTIONS §2.6 口径不变） |
| Redis 故障 | **503**，绝不谎报 `allowed`——gw 收到非 200 即 fail-open |

### fail-open 链路（docs/07 §5.2 / CONVENTIONS-P2 T-3）

本服务 503 / 网络错误 / gw 侧 **500ms** 超时（§9-⑥：同步限流检查在请求关键路径上，p95 预算 500ms
之下检查预算 ≤10%，同命名空间 RTT <5ms 已含 100× 裕度）→ gw 放行 + `console.warn` 节流日志
（**同 key 60s 一条**）。限流是保护措施，其自身故障不允许放大为业务不可用；可用性优先级：gw > 本服务。
gw 侧另设 `RATE_MODE=local` 一期进程内桶回退开关（§5.3，ratelimit-svc/Redis 故障且 fail-open
不可接受时 manifest 单行切回，不改镜像）。

## 探针与指标

- `/healthz`：不依赖 Redis（§5.1/CONVENTIONS §2.14），进程存活即 200
- `/readyz`：Redis Cluster `PING`，失败 503（pod 摘出后 gw 全量 fail-open，等价"限流降级"）
- `/metrics`：`hsf_http_requests_total{svc="ratelimit",path,code}` + `hsf_http_request_seconds`
  （命名与 hsf_common.web 全平台统一；§2.8 PlainTextResponse 口径）

## 单测

`tests/test_limit.py`：内存 fake（不依赖真实 Redis；FakeRedis.eval 模拟冻结 Lua 脚本语义），
覆盖 docs/07 §6.2 冻结用例面——cost=1/1200（单 RTT 打满窗口）、cap 边界（=cap 放行/=cap+1 拒绝）、
键格式冻结、TTL 首写置位、分钟滚动重置、键隔离、`/limit` 422/503、`/healthz` 无依赖、`/metrics`
content-type。本地：`PYTHONPATH=services python -m pytest services/ratelimit/tests -v`。

## 部署

`k8s/20-services.yaml`：Deployment 2 副本（无状态，反亲和跨主机）+ Service 8096 ClusterIP
（`metrics: "true"` 进 ServiceMonitor）。依赖：hsf-redis 集群（infra，`REDIS_URL`/`REDIS_PASS`
经 hsf-secrets envFrom 注入）。构建：`docker build -f services/ratelimit/Dockerfile -t
10.167.2.175:30500/hsf/ratelimit:2.0 .`（上下文=hsf-ias/ 根）。

## 已知边界（如实记录）

- 固定窗口交界突刺：相邻两窗合计最多 2×cap 瞬时通过——一期令牌桶"补充速率"语义不延续
  （§5.1 固有语义声明），验收与容量模型按固定窗口理解；
- Lua 脚本经 `EVAL` 按 numkeys 路由到键所在分片（Redis Cluster 单键单槽，无需 hash tag）；
- gw 侧 500ms 超时兜底 ratelimit-svc 卡死：readyz 摘 pod + k8s 重启为根治路径。
