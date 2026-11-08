# HSF 二期工程约定增补（在 CONVENTIONS.md 基础上追加，所有二期子代理必读）

## T-1 二期范围（对齐 docs/18 §四二期路线）

| 项 | 一期状态 | 二期目标 |
|---|---|---|
| MQTT 接入 | 无 | 部署 Mosquitto broker（hsf-prod，ClusterIP 1883），sim 设备模拟器经 MQTT 发布，**iot 新服务**订阅接入 |
| 时序库 | TiDB hsf_ts（未用） | **TimescaleDB**（timescale/timescaledb:2.17.2-pg16，1 副本 StatefulSet + 10Gi local-path PVC），库名 hsf_ts，sensor_reading 建 hypertable；TiDB hsf_ts 保留为归档口径（不再写入） |
| 智慧生产 | 规则引擎告警（sim 内） | **iot 服务**承接：规则引擎（墒情/渍害/大风/虫害）+ **灌溉决策**（墒情低于阈值→建议灌溉+用水量估算，写 hsf_biz.irrigation_advice）+ **通知**（alert 增加 notify 表，mobile 端拉取） |
| 全局限流 | 进程内桶（一期 R-5 回退） | **ratelimit-svc**（新服务 8096，Python redis.cluster.RedisCluster——已实证对 hostname 广播集群正常）+ 固定窗口；gw express 中间件改 HTTP 调用 ratelimit-svc（fail-open），删除 ioredis |
| 移动端 | 基础页 | 设备/传感器卡片、灌溉建议卡、通知列表 |
| 一期遗留 | — | 同源反代专项 E2E 用例（page 同源 fetch /graphql 200）；E2E 扩充至 ≥17；k6 增 iot-write 时序吞吐场景 |

## T-2 新增组件约定

- **Mosquitto**：镜像拉取 `eclipse-mosquitto:2.1` → tag 注册 `10.167.2.175:30500/base/mosquitto:2.1`；StatefulSet 1 副本 hsf-prod；密码认证（user `hsf`，密码入 hsf-secrets MQTT_PASS）；Service hsf-mqtt:1883（ClusterIP，不暴露 NodePort）
- **TimescaleDB**：镜像 `timescale/timescaledb:2.17.2-pg16` → tag `10.167.2.175:30500/base/timescaledb:2.17.2-pg16`；StatefulSet hsf-timescale 1 副本 + PVC 10Gi（storageClassName local-path）；Service hsf-timescale:5432；库 hsf_ts，用户 hsf/密码入 Secret；`CREATE EXTENSION IF NOT EXISTS timescaledb;`
- **iot 服务**（新）：8097 端口；FastAPI；MQTT 订阅线程（paho-mqtt，topic `hsf/sensor/+/+`）+ 批量写 TimescaleDB（COPY 或 executemany 500/批）+ 规则引擎 + REST API（/devices /sensor/latest /alerts /irrigation-advice /notify）；NodePort 31213
- **ratelimit-svc**（新）：8096；GET /limit?key=&cost=1 → RedisCluster 固定窗口（key `hsf:rl:{key}:{分钟epoch}`，INCR+EXPIRE 120）；响应 {allowed, remain}；gw env RATE_SVC=http://ratelimit-svc.hsf-prod.svc.cluster.local:8096
- **sim 改造**：保留 HTTP /sim/tick；新增 MQTT 发布者（paho-mqtt 连 hsf-mqtt，定时发布模拟数据），MQTT_PASS 入 env
- **服务镜像 tag**：二期统一 **2.0**（sim/iot/ratelimit/gw/web 全部重建）；gw 删除 ioredis 依赖与 Redis 客户端代码
- **E2E 扩充至 ≥17 用例**（新增：MQTT 链路=发布后 5s 内时序可查、灌溉建议生成、通知列表、同源反代 page fetch /graphql 200、TimescaleDB 连通探针）
- **k6 增 iot-write 场景**（HTTP 触发 sim tick 批量写，单独报告不进 thresholds）
- **端口分配新增**：iot 8097、ratelimit 8096、timescale 5432、mqtt 1883（均 ClusterIP；iot NodePort 31213）

## T-3 已知坑延续（一期 R-1~R-6 全部继续有效）

- ioredis Cluster 对 hostname 广播 Redis 集群无限刷新循环——二期一律不用 ioredis；Python redis.cluster.RedisCluster 已实证可用
- express 预中间件限流 429 语义保留，但限流判定改为 HTTP 调用 ratelimit-svc（fail-open：网络错误=放行并计数告警日志）
- 30090 幽灵端口已清；新增页面禁止再引入
- NodePort 端口：31210(web) 31211(sim) 31213(iot) 已用；新 NodePort 前必须 ss -tln 核验
- master requests 被租户占满：新有状态服务（timescale/mosquitto）只允许调度 worker1（nodeSelector kubernetes.io/hostname: k8s-worker1）
- gw 滚动更新时禁用 `kubectl set image` 后再 apply 旧清单（一期教训：manifest 必须是唯一事实源，镜像 tag 只改 manifest）
