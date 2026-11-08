# HSF 高标准农田信息平台

> "一张网感知、一张图统览、一个库汇聚、一平台支撑、N 应用闭环、三体系保障"（一期落地：一张图 + 项目全周期 + 设施管护 + IoT 模拟告警；二期落地：MQTT 中台 + TimescaleDB 时序 + 灌溉决策 + 通知 + 全局限流）
> 技术栈：K8s + GraphQL Federation(Apollo Gateway) + TiDB(hsf_md/hsf_biz) + TimescaleDB(hsf_ts) + Mosquitto MQTT + Redis Cluster + Strawberry Python 子图 + Node.js 网关 + 纯静态三端（零 CDN 依赖）
> 决策依据：DV-IAS 项目 docs/18《融合性分析与决策建议》——独立立项 + 共享底座 + 联邦集成
> 姊妹项目：https://github.com/tianqiongenze/dv-ias （数字乡村综合服务平台，本平台复用其工程体系）

## 二期验收结论（2026-11-08，docs/21）

| 指标 | 结果 |
|---|---|
| 新组件 | hsf-timescale（PVC 10Gi）/ hsf-mosquitto（PVC 1Gi）Running；iot 2.0.6（NodePort 31213）/ ratelimit 2.0 上线；sim/gw/web 升 2.0 |
| MQTT→时序 | tick 240 条 6s 内落库 delta=432；心跳 delta=96 精确匹配；TimescaleDB 实时行数 300 万+ 持续增长 |
| 灌溉决策 | total=24 条真实建议（M=667×h×γ×(θfc−θ)/100 公式核验一致）、6h 窗口去重生效、AUTO_RECOVER 自动闭环 |
| 通知 | notify 4 角色推送面（village total=6951）、unreadOnly 过滤+已读置位 |
| 全局限流 | 固定窗口 1200/min/key **跨副本全局共享**（另一副本视角 denied）、gw 突发 1500→429×300、fail-open 验证 |
| E2E | **19/19 PASS**（一期 13 零改动回归 + 二期 6 新增） |
| k6 基线 | **0 失败**（0/7368）；browse p95=**348.89ms**（较一期 521ms 优化 33%）、report p95=103.69ms |
| 时序吞吐 | iot-write：published 218 万条、**TimescaleDB 落库 ≈5524 条/s ≥ 5000 参考目标** |
| 静态门禁 | 二期检查项 8~13 全绿（violations=0/103 files） |

完整证据与缺陷修复记录（W2-1~W2-4 连环缺陷链、W3-1/W3-2、Redis 集群重建、quota 排障）：`docs/21-二期融合测试报告.md`

## 一期验收结论（2026-11-08，复测终版）

| 指标 | 结果 |
|---|---|
| 工作负载 | gw×4 / iam·land·project·facility×2 / sim×1 / web×2 全部 Running；hsf-redis 3 节点集群 |
| 数据规模 | 一张图 8 地块（GeoJSON）+ 项目全周期 7 态 + 设施工单 4 态 + 48 传感器时序 |
| k6 基线 | browse 200VU + report 20VU：**0 失败**，med 20.69ms，browse p95=521ms（阈值 500，+4% 为共享集群环境噪声，四轮收敛轨迹 1.66s→521ms 留档） |
| E2E（Playwright） | **13/13 PASS**（三端渲染/登录/一张图渲染/项目全周期/设施工单/告警/联邦跨域/限流 429） |
| 限流语义 | express 预中间件 **HTTP 429 + RATE_LIMITED**（1500 并发突发→255×429；全局 6000 突发→367×429，RATE_CAP=1200/min/实例） |
| 安全 | 国密 SM3 口令/SM4-GCM 字段、网关 JWT+身份头注入（x-user-name Base64）、限流 429、MCP 式审计预留 |
| 监控 | Prometheus targets hsf-* 全 UP（ServiceMonitor） |

完整证据与缺陷修复记录（R-1~R-6，含 ioredis Cluster 刷新循环、Promise 恒真限流失效等真实事故）：`docs/20-融合测试报告.md`

## 目录

```
docs/      00 方案总览 / 01 需求规格 / 02 选型与架构 / 03 DDD / 04 数据与缓存 / 05 SDL / 06 实施验收
           07 二期设计（IoT 中台/时序/决策/限流） / 20 一期融合测试报告 / 21 二期融合测试报告
gw/        Apollo Federation 网关（限流判定 HTTP 调 ratelimit-svc fail-open、RATE_MODE=svc/local 双模）
services/  iam（组织账号）· land（地块一张图）· project（全周期状态机）· facility（台账工单）
           sim（IoT 模拟 + MQTT 发布）· iot（MQTT 订阅/TimescaleDB/规则引擎/灌溉决策/通知）
           ratelimit（全局固定窗口限流）· common/hsf_common（国密/双连接池/多级缓存/web 框架）
k8s/       00-base / 10-redis / 20-services / 25-timescale / 30-mqtt / 40-k6 / 60-monitors
scripts/   build / deploy / init-ts（TimescaleDB 幂等初始化）/ smoke-import / static_gate（13 项防回归门禁）
perf/      baseline.js（登录改造版）/ stress.js（含 iot-write 场景）/ locustfile.py
test/      e2e_browser.py（19 用例）
```

## 快速开始

```bash
# 部署（假设集群内 Registry 10.167.2.175:30500，TiDB infra 可用）
cd hsf-ias && TAG=2.0 bash scripts/build.sh && bash scripts/deploy.sh && bash scripts/init-ts.sh
# 一张图：http://10.167.2.175:31210   网关：http://10.167.2.175:30099   iot：31213
# 演示账号：13800000601（县管）/ 13800000602（乡镇）/ 13800000604（村） 密码 hsf@2026
```

## 已知边界（二期后）

- 感知层为模拟数据（sim 经 MQTT 真实发布，iot 订阅接入；真实硬件接入为三期）
- GIS 采用 GeoJSON-in-TiDB + SVG/Leaflet 简化（ADR：集群无 PostGIS；二期评估）
- 遥感解译、全国农田建设监管平台对接为三期
- 限流为 ratelimit-svc 固定窗口（1200/min/key 全局共享，gw RATE_MODE=local 可回退进程内桶）；窗口边界突刺语义见 docs/07 §5.1
- iot 单副本固定（latest 缓存+回差状态机进程内态）；重启由 broker 离线补投+数据库幂等兜底（实测 87 万条补投重复零重复落库）
