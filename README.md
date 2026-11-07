# HSF 高标准农田信息平台

> "一张网感知、一张图统览、一个库汇聚、一平台支撑、N 应用闭环、三体系保障"（一期落地：一张图 + 项目全周期 + 设施管护 + IoT 模拟告警）
> 技术栈：K8s + GraphQL Federation(Apollo Gateway) + TiDB(hsf_md/hsf_biz/hsf_ts) + Redis Cluster + Strawberry Python 子图 + Node.js 网关 + 纯静态三端（零 CDN 依赖）
> 决策依据：DV-IAS 项目 docs/18《融合性分析与决策建议》——独立立项 + 共享底座 + 联邦集成
> 姊妹项目：https://github.com/tianqiongenze/dv-ias （数字乡村综合服务平台，本平台复用其工程体系）

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
docs/      00 方案总览 / 01 需求规格 / 02 选型与架构 / 03 DDD / 04 数据与缓存 / 05 SDL / 06 实施验收 / 20 融合测试报告
gw/        Apollo Federation 网关（express 预中间件限流 429、独立 9090 探针、Base64 身份头）
services/  iam（组织账号）· land（地块一张图）· project（全周期状态机）· facility（台账工单）· sim（IoT 模拟告警）
           common/hsf_common（国密/双连接池/多级缓存/web 框架，移植自 DV-IAS）
k8s/       00-base / 10-redis / 20-services / 40-k6 / 60-monitors
scripts/   build / deploy / smoke-import / static_gate（12 项防回归静态门禁）
perf/      baseline.js / stress.js / locustfile.py
test/      e2e_browser.py（13 用例）
```

## 快速开始

```bash
# 部署（假设集群内 Registry 10.167.2.175:30500，TiDB infra 可用）
cd hsf-ias && bash scripts/build.sh && bash scripts/deploy.sh
# 一张图：http://10.167.2.175:31210   网关：http://10.167.2.175:30099
# 演示账号：13800000601（县管）/ 13800000602（乡镇）/ 13800000604（村） 密码 hsf@2026
```

## 已知边界（一期）

- 感知层为模拟数据（sim 服务生成墒情/气象/虫情，规则引擎告警）；真实硬件接入为二期
- GIS 采用 GeoJSON-in-TiDB + SVG/Leaflet 简化（ADR：集群无 PostGIS；二期评估）
- 遥感解译、全国农田建设监管平台对接为三期
- 限流为进程内令牌桶（RATE_CAP=1200/min/实例）；Redis 全局桶待二期（ioredis Cluster 对 hostname 广播集群刷新循环问题已留档）
