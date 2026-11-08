# 07 HSF 二期设计：IoT 中台与时序与智慧生产

> 版本：v1.1（评审修订稿 2026-11-08：按评审意见修订 DDL 语法与限定名/引导口径/通知读写模型/压测负载模型/资源核算/验收命令等，正文与 §9 声明同步）｜ 编制日期：2026-11-08 ｜ 编制：二期架构师
> 上游依据：`hsf/CONVENTIONS-P2.md`（二期强制约定 T-1~T-3）→ `hsf/CONVENTIONS.md`（一期 16 条强制约定与坑）→ `hsf/docs/04`（一期 DDL/编码/缓存冻结）、`hsf/docs/05`（一期 SDL/REST 契约冻结）→ `docs/18` §四（二期路线）
> 参考实现（已读源码）：`hsf-ias/services/sim/app/{main.py,repo.py,rules.py}`、`hsf-ias/gw/gateway.js`、`hsf-ias/k8s/{00-base,10-redis,20-services,40-k6}.yaml`、`hsf-ias/test/e2e_browser.py`、`hsf-ias/perf/{baseline.js,stress.js}`、`hsf-ias/services/common/hsf_common/{cache.py,db.py}`
> 冻结声明：**本文冻结二期全部新增 DDL、MQTT 契约（topic/payload/QoS/认证）、iot REST 契约、限流 API 与参数、决策模型公式、E2E 用例集、升级与回滚步骤**。与一期冻结契约的每一处偏离在 §9 显式声明；无声明处一律沿用一期（04/05 号）口径。读者对象=二期四路实现工程师（IoT/限流与网关/数据/前端与测试）与部署工程师。

---

## 0. 决议与假设清单（先读）

| # | 类型 | 内容 | 理由与回退 |
|---|---|---|---|
| D-07-1 | 决议 | **iot 服务是二期时序唯一写入方**（TimescaleDB）与规则引擎/灌溉决策/通知唯一持有者；sim 职责收缩为"数据生成 + MQTT 发布 + 设备台账种子"，不再写时序库、不再做规则告警 | 职责单一（CONVENTIONS-P2 T-1"iot 服务承接"）；回退：sim 1.0.1 镜像可整体回滚恢复旧链路（§8） |
| D-07-2 | 决议 | TimescaleDB 库名沿用 `hsf_ts`；**TiDB `hsf_ts` 二期零写入**，保留为归档口径（一期残留数据 ≤259 万条作为历史快照只读） | CONVENTIONS-P2 T-1 原文；一期数据不迁移（D-07-8） |
| D-07-3 | 决议 | hypertable 上建唯一索引 `(device_code, metric, recorded_at)`（含时间分区列，hypertable 允许），iot 写入 `ON CONFLICT DO NOTHING`——以**数据库幂等**承接 MQTT QoS1 重复与重连补投 | QoS1 至少一次语义必然产生重复；应用层去重不可靠（多缓冲窗口）；时间列在索引内满足 hypertable 唯一约束规则 |
| D-07-4 | 决议 | 告警规则引擎加**滞后回差**：进入阈值沿用一期 4 条（<15 / >85 / >17 / >80），退出阈值=进入阈值±5 个百分点；状态机内存态 + 重启时从 `alert_record` 最近 OPEN 记录重建 | 防墒情/风速在阈值附近抖动致告警风暴（workflow-p2 iot 工程师任务原文"带滞后回差防告警抖动"）；一期边界值语义（14.9 触发/15.1 不触发）不变 |
| D-07-5 | 决议 | 灌溉决策：墒情 <40（IRR_START）生成建议、≥55（IRR_STOP，回差 15）自动闭环；用水量公式 **M = 667 × h × γ × (θ_fc − θ_now) / 100**（m³/亩）；同地块 6h 窗口去重（`uk_advice_dedup`） | workflow-p2 决策触发口径（<40%、6h 去重）；公式为工程简化模型，参数全 env 可调；幂等模式对齐一期 `uk_rule_reading`（04 §4.6） |
| D-07-6 | 决议 | notify 通知模型：每条新告警/新建议按 4 角色（admin/town/village/keeper）各写 1 行，**全部行 `village_code`=事件村快照**；读侧 role 取自 JWT + 按 05 §2.4 范围矩阵过滤（§4.5 读取模型）；`read_at` 由 mobile 拉取后显式置位；`uk_notify_dedup(dedup_key,user_role)` 幂等 | 范围对齐 05 §2.4：admin=全县（不过滤）、town=乡内村集、village=本村、keeper=所辖设施所在村集、coop 不入推送面；行数=告警×4+建议×4：建议 ≤32 条/日（8 地块×6h 去重窗口，§4.3——常态 30% 墒情读数 <40 即触发，A-07-4），告警按回差状态机每异常事件 1 行、演示生成器常态注入下 ≈2.1 万条/日（48 台×0.5Hz×极端带占比 1~2%）→ notify ≈8.3 万行/日，TiDB 亿行级承载已由 DV-IAS 同集群 1.058 亿行实测验证（`docs/02` 选型表），量级无压力；通知噪声过大时调 sim `MQTT_INTERVAL`（A-07-2 同旋钮）按比例下降 |
| D-07-7 | 决议 | 限流二期值 **RATE_CAP=1200/min 固定窗口**（对一期 6000 令牌桶的显式偏离，§9-③）；gw `RATE_MODE` 开关：`svc`（默认）/`local`（一期进程内桶回退）；fail-open 超时 500ms | 固定窗口下 429 可在验收窗口内**确定性**复现（E2E 突发量 30000→1500）；固定窗口无补充速率，压测与 E2E 不再受一期"100 tokens/s 永打不穿"缺陷（docs/20 §5.1）困扰 |
| D-07-8 | 决议 | **数据迁移=空库新建不迁移**：TimescaleDB 全新初始化（init-ts.sh 幂等）；TiDB 侧仅 iot 启动时幂等建 `irrigation_advice`/`notify` 两张新表，存量表结构零变更，存量数据零迁移 | 一期数据为种子/演示量级，迁移收益为零、风险为正；对齐 CONVENTIONS-P2 T-1"空库新建"验收口径 |
| A-07-1 | 假设 | Mosquitto 明文 1883（ClusterIP 不出网）+ 密码认证 + ACL，不做 TLS | 与一期威胁模型一致（内网受控 + NetworkPolicy 默认拒绝入站、同命名空间放行，`00-base.yaml` §NetworkPolicy 注释）；TLS 列三期外联场景 |
| A-07-2 | 假设 | sim MQTT 心跳 **2s/轮常开**（48 台×0.5Hz=24 条/s 稳态 ≈ 207 万条/日），容量按"压缩策略 1 天后生效"核算（§3.6） | workflow-p2 sim 改造任务原文"2s 间隔"；若评审认为日增量过大，仅需调 env `MQTT_INTERVAL`（sim，单位 s，默认 2），清单单行变更 |
| A-07-3 | 假设 | iot 进程内 `latest` 缓存（dict，每批 flush 后更新），冷启动回源 TimescaleDB `DISTINCT ON`；iot 单副本为缓存与回差状态机一致性的前提 | 48 台×6 metric 序列内存可忽略；单副本故障即重启（Deployment 探针），补偿窗口 ≤ 补投队列 |
| A-07-4 | 假设 | E2E 灌溉建议用例的 <40 墒情读数由 sim **常态分布**保证（不依赖极端注入）：正常带 25~75 均匀采样本身即 30% 概率 <40（`rules.py:28`），叠加 2% 极端带注入（`rules.py:36`，旱情段 6.0~14.5 全段 <40）→ P(单条 <40) = 0.98×0.30 + 0.02×0.5 ≈ **0.304**；`steps=200` 产出 8 墒情传感器 × 200 = 1,600 条 → P(≥1 条) = 1−(1−0.304)^1600 ≈ 1（确定性满足） | 不为 E2E 新增 sim 注入 API（05 §7.1 端点契约不动）；结论方向与一期论证一致、用例更稳，但前提修正（原"仅极端注入产生 <40"与生成器不符）；副作用知悉：常态下每 6h 窗口每地块均产出灌溉建议（≤8 地块×4 窗口=32 条/日），通知量级口径已随 D-07-6 修订 |

---

## 1. 二期总体架构

### 1.1 拓扑（增量）

```
sim(2.0) ──MQTT QoS1──► hsf-mosquitto(2.1) ◄──订阅 QoS1(clean_session=false)── iot(2.0)
  │                        │ StatefulSet×worker1                                     │
  │ /sim/tick(HTTP,k6/E2E) │ 1883 ClusterIP                                          ├─ 批量 500/2s → TimescaleDB hsf_ts(sensor_reading hypertable)
  ▼                        ▼                                                         ├─ 规则引擎(回差) → hsf_biz.alert_record + notify
gw(2.0) ──限流检查──► ratelimit-svc(2.0) ──► hsf-redis 集群(infra,既有)             ├─ 灌溉决策 → hsf_biz.irrigation_advice
  ▲                    8096 ClusterIP                                               └─ REST :8097/NodePort 31213
  │                                                                                      ▲
web(2.0) nginx ──/graphql→ gw、/iot/→ iot-svc（同源反代）────────────── mobile H5 / admin 新页签
TiDB(既有) :4000 ── hsf_md/hsf_biz 读写照旧；hsf_ts 二期零写入（归档快照）
```

### 1.2 组件清单（端口/镜像/工作负载）

| 组件 | 类型 | 端口 | 镜像（10.167.2.175:30500） | 副本 | NodePort | 一期 tag → 二期 tag |
|---|---|---|---|---|---|---|
| hsf-timescale | StatefulSet（新） | 5432 ClusterIP | base/timescaledb:2.17.2-pg16 | 1 | — | 新增 |
| hsf-mosquitto | StatefulSet（新） | 1883 ClusterIP | base/mosquitto:2.1 | 1 | — | 新增 |
| iot-svc | Deployment（新） | 8097 + NodePort | hsf/iot:2.0 | **1（固定，禁 HPA）** | 31213 | 新增 |
| ratelimit-svc | Deployment（新） | 8096 ClusterIP | hsf/ratelimit:2.0 | 2 | — | 新增 |
| sim | Deployment | 8095 | hsf/sim:2.0 | 1 | 31211（不变） | 1.0.1 → 2.0 |
| gw | Deployment | 8090/9090 | hsf/gw:2.0 | 2（HPA 4-6 不变） | 30099（不变） | 1.1.2 → 2.0 |
| web | Deployment | 80 | hsf/web:2.0 | 2 | 31210（不变） | 1.0 → 2.0 |
| iam/land/project/facility | Deployment | 8091-8094 | hsf/<svc>:1.0.1 | 2 | — | **沿用 1.x（无代码变更）** |

> 端口分配对齐 CONVENTIONS-P2 T-2（iot 8097/ratelimit 8096/timescale 5432/mqtt 1883，均 ClusterIP；iot NodePort 31213）。NodePort 31213 上线前 `ss -tln` 核验（T-3）。

> **hsf-dev 同构口径**（承接 `20-services.yaml:6` 一期约定，二期新/改清单全部适用）：`25-timescale.yaml`、`30-mqtt.yaml`（新）、`20-services.yaml` 增量（iot/ratelimit）与 sim/gw/web 2.0 镜像同样以 `sed 's/hsf-prod/hsf-dev/g'` 派生 dev 清单另行 apply——副本数仅 ratelimit 减半（2→1，一期"同构、副本减半"惯例 `00-base.yaml:75`），iot 固定 1、StatefulSet×2 固定 1 不变；清单/env 中含 `hsf-prod` 的值（RATE_SVC、IOT_UPSTREAM）随 sed 一并替换即生效，PVC/local-path/nodeSelector 原样沿用。本文 §7/§8 升级与回滚步骤均以 **hsf-prod 为对象冻结**（dev 按需另行 apply，二期验收仅在 hsf-prod 执行）；Secret 新键两命名空间同步（§1.4）与 dev 同构兼容。

### 1.3 资源与配额核算

| 组件 | requests | limits | PriorityClass | 调度 |
|---|---|---|---|---|
| hsf-timescale | cpu 500m / mem 1Gi | cpu 2 / mem 2Gi | hsf-normal | nodeSelector `kubernetes.io/hostname: k8s-worker1`（master requests 满，T-3） |
| hsf-mosquitto | cpu 100m / mem 128Mi | cpu 500m / mem 256Mi | hsf-normal | 同上 |
| iot-svc | cpu 250m / mem 384Mi | cpu 2 / mem 1Gi | hsf-normal | podAntiAffinity preferred；单副本 |
| ratelimit-svc | cpu 150m / mem 256Mi | cpu 1 / mem 512Mi | hsf-normal | podAntiAffinity preferred |

- 表内四组件合计 requests ≈ **1.0 CPU / 1.75Gi**、limits ≈ 5.5 CPU / **3.75Gi**；叠加压测/E2E 临时 Job（一期 `40-k6.yaml:73` 同款 requests 200m/256Mi、limits 1 CPU/512Mi，PriorityClass hsf-batch 用后即删）峰值 requests ≈ **1.2 CPU / 2.0Gi**、limits.mem ≈ **4.25Gi**：现配额（`00-base.yaml:43/46` ResourceQuota requests.cpu=12 / limits.memory=48Gi）与一期实测占用（≈2.2 CPU / ≈11Gi）之下余量充足，**无需扩配额**。
- NetworkPolicy：新组件均处 hsf-prod，同命名空间互访已放行；Prometheus 跨 ns 抓取规则已存在（60-monitors 同模式）——**零策略变更**。
- iot/ratelimit 的 Service 带 `metrics:"true"` label + 端口名 `http-metrics`（CONVENTIONS §2.16）；sim/iot 暴露 Prometheus /metrics（`Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)`，CONVENTIONS §2.8）。

### 1.4 Secret 增键（00-base.yaml 两个命名空间同步）

| 新键 | 值 | 使用方 |
|---|---|---|
| `MQTT_PASS` | `<gen>`（mosquitto_passwd 派生） | sim、iot（MQTT_USER=hsf 固定）、mosquitto initContainer |
| `TIMESCALE_PASS` | `<gen>` | iot（TIMESCALE_DSN=postgresql://hsf:{pass}@hsf-timescale:5432/hsf_ts）、init-ts.sh、hsf-timescale StatefulSet（`POSTGRES_PASSWORD`，引导口径见 §3.1） |

其余键（DB_*/REDIS_*/JWT_SECRET）沿用；iot/ratelimit 继续 `envFrom: hsf-secrets`（sim 模式一致，`20-services.yaml:209`）。

---

## 2. MQTT 接入设计

### 2.1 Topic 规划（冻结）

| Topic | 方向 | QoS | retain | 用途 | 二期/三期 |
|---|---|---|---|---|---|
| `hsf/sensor/{deviceCode}/{metric}` | sim → broker → iot | 1 | false | 传感器读数（唯一数据主题） | 二期实现 |
| `hsf/cmd/{deviceCode}/{action}` | 平台 → 设备 | 1 | false | 下行控制（泵站启停等） | **仅预留命名，不实现** |
| `hsf/alert/{villageCode}` | iot → 外部 | 1 | false | 告警外发（移动推送通道接入时用） | **仅预留**；二期通知走 notify 表拉取（对 D-05-7 的显式修订：WebSocket 仍不引入，§9-⑨） |
| `$SYS/#` | broker 内部 | — | — | broker 指标观测（排障用） | 只读，不接监控面板 |

- `{deviceCode}` = `hsf_biz.sensor.sensor_code`（11 位 S 码，04 §2.2 冻结）；`{metric}` ∈ 04 §4.7 冻结 6 类。iot 对未知 deviceCode/metric 的消息：**丢弃 + 计数** `hsf_iot_mqtt_invalid_total`（不做动态注册，设备准入仍走台账）。
- `{deviceCode}/{metric}` 二级通配订阅 `hsf/sensor/+/+`（CONVENTIONS-P2 T-2 原文）。

### 2.2 消息 payload（冻结）

```json
{"device_code": "S5306000001",
 "metric": "soil_moisture",
 "value": 23.456,
 "recorded_at": "2026-11-08T10:00:00.123+08:00",
 "village_code": "53060010101"}
```

| 规则 | 内容 |
|---|---|
| `recorded_at` | **sim 生成时刻**（ISO8601+08:00，毫秒），iot 不改写；观测时刻端到端口径（对齐 A-04-3 全链路 Asia/Shanghai） |
| 一致性校验 | iot 以 payload 为写入依据；topic 解析出的 `{deviceCode}/{metric}` 与 payload 不一致 → 丢弃 + 计数（防发布端 bug 污染时序） |
| 值域复核 | iot 按 04 §4.7 量程复核 value（越界丢弃 + 计数），防极端脏数据进入聚合 |
| 步进约束 | sim 批量 tick 时 `recorded_at` 步进 1ms（一期 `repo.py:183` 同法），保证幂等键可区分 |

### 2.3 QoS 与会话

| 项 | 冻结值 | 理由 |
|---|---|---|
| sim 发布 QoS | 1（at-least-once），`retain=false` | QoS0 网络抖动丢点不可接受；QoS2 在单 broker 内增益不抵开销；重复由 D-07-3 数据库幂等兜底 |
| iot 订阅 QoS | 1，`clean_session=false`，`client_id=hsf-iot-0` | iot 短暂重启/断连期间 broker 保留会话并补投（离线队列） |
| broker 队列 | `max_queued_messages 10000`、`max_inflight_messages 100` | 覆盖 iot 重启窗口（稳态 24 条/s × 2s 缓冲，1 万条 ≈ 7 分钟兜底） |
| iot 重连 | paho `reconnect_delay_set(1, 30)`（1s→30s 指数退避） | broker 滚动重启（§7）时读数不丢只延后 |
| broker 持久化 | `persistence true` + PVC（§1.3） | 会话与离线队列重启保留 |

### 2.4 认证与 ACL（冻结）

| 项 | 冻结值 |
|---|---|
| 监听 | `listener 1883 0.0.0.0`，`allow_anonymous false`（明文，A-07-1） |
| 账号 | 单账号 `hsf`；密码 = Secret `MQTT_PASS` |
| 密码文件 | initContainer 执行 `mosquitto_passwd -b -c /mosquitto/data/passwd hsf "$MQTT_PASS"`（PVC 内，幂等覆盖，密码不落 ConfigMap） |
| ACL（ConfigMap `acl` 键） | `user hsf` / `topic readwrite hsf/sensor/#`——sim 只需 pub、iot 只需 sub，单一 readwrite 账号即可满足且避免两账号密钥分发 |
| mosquitto.conf | `listener 1883 0.0.0.0` / `allow_anonymous false` / `password_file /mosquitto/data/passwd` / `acl_file /mosquitto/config/acl` / `persistence true` / `persistence_location /mosquitto/data/` / `max_queued_messages 10000` / `max_inflight_messages 100` |
| 镜像/部署 | `eclipse-mosquitto:2.1` → `10.167.2.175:30500/base/mosquitto:2.1`；StatefulSet `hsf-mosquitto` 1 副本 + PVC 1Gi（local-path）+ Service `hsf-mqtt:1883`（T-2） |

### 2.5 sim 发布器与 iot 订阅者职责边界（冻结）

| 关注点 | sim（发布器，2.0） | broker | iot（订阅者，2.0） |
|---|---|---|---|
| 数据生成 | ✔ `rules.next_value()` 值域生成（`rules.py:27-34` 冻结量程不变） | ✘ | ✘（不做任何模拟） |
| MQTT 发布 | ✔ 心跳 2s/轮（48 条）+ `POST /sim/tick` 批量触发同步发布，响应含 `published` 条数 | ✘ | ✘ |
| 路由/会话/补投 | ✘ | ✔ QoS1 会话保持、离线补投、持久化 | ✘ |
| 一致性/值域校验 | 值域（生成侧保证） | ✘ | ✔ topic↔payload 校验 + 值域复核（§2.2） |
| 时序写入 | ✘（**一期写 TiDB hsf_ts 职责删除**） | ✘ | ✔ TimescaleDB 唯一写入方（缓冲 500 条或 2s 刷一批） |
| 规则引擎/告警 | ✘（**一期 `rules.py` RULES 职责移交 iot**） | ✘ | ✔ 回差状态机 → alert_record（uk_rule_reading 幂等键不变） |
| 灌溉决策/通知 | ✘ | ✘ | ✔ irrigation_advice + notify |
| 设备台账 | ✔ 启动幂等 seed `hsf_biz.sensor`（48 台，一期 `repo.py:124-138` 逻辑保留） | ✘ | 只读（/devices 数据源） |
| `sensor.last_*` 冗余列 | ✘（**二期移交**，§9-④） | ✘ | ✔ 每批 flush 后 UPDATE（48 行/批） |
| alert_rule 种子 | ✘（移交） | ✘ | ✔ 启动幂等 seed 4 条（INSERT IGNORE uk_rule_code） |
| Outbox 事件 | ✘ | ✘ | ✔ `hsf.iot.alert.raised` / `hsf.iot.advice.created` 写 domain_event（ADR-HSF-004 延续） |

> 边界一句话：**sim 负责"造数与发数"，iot 负责"收数、判数、决策与落库"，broker 只管可靠转发**。二期后 sim 不再持有任何规则与阈值常量（static_gate 检查项，§6.4）。

---

## 3. TimescaleDB 时序设计

### 3.1 部署事实（直接采用，T-2）

| 项 | 冻结值 |
|---|---|
| 镜像 | `timescale/timescaledb:2.17.2-pg16` → `10.167.2.175:30500/base/timescaledb:2.17.2-pg16` |
| 工作负载 | StatefulSet `hsf-timescale` 1 副本（hsf-prod）+ PVC **10Gi**（storageClassName `local-path`）+ Service `hsf-timescale:5432`；nodeSelector worker1 |
| 库/账号 | 库 `hsf_ts`（沿用一期命名，D-07-2）；用户 `hsf`，密码 Secret `TIMESCALE_PASS`；**引导口径**：StatefulSet env `POSTGRES_USER=hsf` / `POSTGRES_DB=hsf_ts` / `POSTGRES_PASSWORD=<Secret TIMESCALE_PASS>`——官方镜像首次启动 initdb 按 POSTGRES_USER/DB 建库建用户，`hsf` 即超级用户兼业务用户（单实例单租户、内网受控，威胁模型同 A-07-1），`CREATE EXTENSION timescaledb` 的超级用户要求随之满足；无独立超级用户凭据、无新增 Secret 键（StatefulSet/iot/init-ts.sh 三方共用同一键） |
| 探针 | `pg_isready -U hsf -d hsf_ts`（readiness/liveness）；Pod env `TZ=Asia/Shanghai` + PG 参数 `timezone='Asia/Shanghai'`（A-04-3 对齐） |
| 初始化 | `scripts/init-ts.sh`：等 pg_isready → 以 `hsf/TIMESCALE_PASS` 连 `hsf_ts`（同 iot DSN）→ `CREATE SCHEMA IF NOT EXISTS hsf_ts` → `CREATE EXTENSION IF NOT EXISTS timescaledb` → 建 hypertable/索引/连续聚合/策略；**全幂等可重复执行**（Deployment Job 或临时 Pod 方式，参照 CONVENTIONS §3.2 临时 pod 模式） |

### 3.2 Hypertable DDL（冻结）

```sql
CREATE SCHEMA IF NOT EXISTS hsf_ts;          -- 库名与 schema 同名：initdb 默认仅 public，先建 schema 否则 CREATE TABLE 报 schema 不存在（init-ts.sh 步骤同 §3.1）
CREATE EXTENSION IF NOT EXISTS timescaledb;  -- 引导权限口径见 §3.1（POSTGRES_USER=hsf 即超级用户）

-- PostgreSQL 无行内 COMMENT 子句（那是 MySQL/TiDB 语法）——列注释一律后置 COMMENT ON 语句表达，幂等可重复执行：
CREATE TABLE IF NOT EXISTS hsf_ts.sensor_reading(
  device_code CHAR(20)         NOT NULL,
  metric      VARCHAR(16)      NOT NULL,
  value       DOUBLE PRECISION NOT NULL,
  recorded_at TIMESTAMPTZ      NOT NULL
);
COMMENT ON COLUMN hsf_ts.sensor_reading.device_code IS '设备编码=sensor.sensor_code（S+6位区划+4位序号，列宽预留对齐 CONVENTIONS §2.3 宁可宽不可窄；PG bpchar 等值比较忽略尾部空格）';
COMMENT ON COLUMN hsf_ts.sensor_reading.metric      IS '04 §4.7 冻结 6 类';
COMMENT ON COLUMN hsf_ts.sensor_reading.value       IS '观测值（DECIMAL→DOUBLE：时序聚合允许浮点，量纲与一期一致）';
COMMENT ON COLUMN hsf_ts.sensor_reading.recorded_at IS '观测时刻（sim 生成，ISO8601+08:00）';

SELECT create_hypertable('hsf_ts.sensor_reading', 'recorded_at',
       chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);

-- 幂等唯一索引（含时间分区列，hypertable 合法）：承接 QoS1 重复/补投（D-07-3）
CREATE UNIQUE INDEX IF NOT EXISTS uk_reading
  ON hsf_ts.sensor_reading(device_code, metric, recorded_at);

-- iot 写入形态（psycopg2 execute_values，页大小 500）：
INSERT INTO hsf_ts.sensor_reading(device_code, metric, value, recorded_at)
VALUES %s ON CONFLICT (device_code, metric, recorded_at) DO NOTHING;
```

### 3.3 chunk 间隔 = 1 天（论证）

| 候选 | chunk 数量（90 天保留） | 单 chunk 行数（稳态 24 条/s） | 判定 |
|---|---|---|---|
| 1 hour | 2160 | ~8.6 万 | chunk 管理开销过大（TimescaleDB 建议单库 chunk 总量 <1000） |
| **1 day** | **90** | **~207 万** | ✔ chunk 数受控；压缩/保留/聚合均按天粒度自然对齐；压测突发（60 万条/分钟级）单 chunk 内可承受 |
| 7 day | 13 | ~1450 万 | 删除/压缩粒度过粗，压缩批处理耗时长 |

### 3.4 连续聚合（5min/1h 均值，冻结）

```sql
-- 5 分钟聚合（报表/趋势图/大屏）
CREATE MATERIALIZED VIEW IF NOT EXISTS hsf_ts.sensor_reading_5min
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '5 minutes', recorded_at) AS bucket,
       device_code, metric,
       avg(value) AS avg_v, min(value) AS min_v, max(value) AS max_v, count(*) AS cnt
FROM hsf_ts.sensor_reading GROUP BY 1, 2, 3 WITH NO DATA;

-- 策略参数的关系名一律 hsf_ts. 限定（psql 默认 search_path=public，不限定则 relation does not exist，与 §3.2/§3.5 同口径）
SELECT add_continuous_aggregate_policy('hsf_ts.sensor_reading_5min',
       start_offset => INTERVAL '3 hours', end_offset => INTERVAL '5 minutes',
       schedule_interval => INTERVAL '10 minutes', if_not_exists => TRUE);
SELECT add_retention_policy('hsf_ts.sensor_reading_5min', INTERVAL '90 days', if_not_exists => TRUE);

-- 1 小时聚合（归档口径主载体，3 年在线，NFR-07）
CREATE MATERIALIZED VIEW IF NOT EXISTS hsf_ts.sensor_reading_1h
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', recorded_at) AS bucket,
       device_code, metric,
       avg(value) AS avg_v, min(value) AS min_v, max(value) AS max_v, count(*) AS cnt
FROM hsf_ts.sensor_reading GROUP BY 1, 2, 3 WITH NO DATA;

SELECT add_continuous_aggregate_policy('hsf_ts.sensor_reading_1h',
       start_offset => INTERVAL '1 day', end_offset => INTERVAL '1 hour',
       schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE);
SELECT add_retention_policy('hsf_ts.sensor_reading_1h', INTERVAL '3 years', if_not_exists => TRUE);
```

- 两视图均按原始表聚合（不采用层级聚合，降低策略耦合）；查询侧 `materialized_only=false` 兜实时尾窗（5min 视图 end_offset=5min 的未聚合区间）。
- 1h 视图行数 = 48 序列×24 = 1152 行/日 ≈ 3 年 126 万行，体积可忽略——**3 年在线由聚合承载**。

### 3.5 压缩与保留策略（冻结）

```sql
ALTER TABLE hsf_ts.sensor_reading SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'device_code, metric',
  timescaledb.compress_orderby   = 'recorded_at DESC');
SELECT add_compression_policy('hsf_ts.sensor_reading', INTERVAL '1 day', if_not_exists => TRUE);
SELECT add_retention_policy('hsf_ts.sensor_reading',   INTERVAL '90 days', if_not_exists => TRUE);
```

### 3.6 容量核算（10Gi PVC 内闭合）

| 项 | 估算 | 依据 |
|---|---|---|
| 稳态写入 | 48 台 × 0.5Hz = 24 条/s = **207 万条/日** | A-07-2（MQTT_INTERVAL=2s） |
| 原始行宽（含索引） | ≈ 180B/行（heap 52B+行头 24B + 唯一索引 + 分区索引项） | PG 16 元组布局 |
| 未压缩日增量 | ≈ 370MB/日 | 仅"当天 chunk"存在 |
| 压缩后稳态 | 压缩比按保守 **10×**（segmentby device_code,metric + orderby recorded_at）→ ≈ 37MB/日 × 89 天 ≈ **3.3GB** | TimescaleDB 列存压缩在同构时序上实测常见 10~20× |
| 5min/1h 聚合 | <100MB | §3.4 行数推算 |
| 压测余量 | k6 iot-write 单场 ≈ 480 万条 ≈ 未压缩 900MB（当日 chunk 内，次日压缩） | §6.3 |
| **合计** | **≈ 4.5~5.5GB，10Gi 内余量 ≈ 45%** | 结论：90 天保留可行；若评审调大 MQTT_INTERVAL 则日增量按比例下降 |

### 3.7 与 TiDB hsf_ts 归档口径分工（对 04 §5.2 的承接与修订，§9-①②）

| 维度 | TimescaleDB hsf_ts（二期热时序） | TiDB hsf_ts（一期遗留，归档快照） |
|---|---|---|
| 写 | iot 唯一实时写入（MQTT 管道） | **二期零写入**（sim 2.0 已摘除写路径） |
| 读 | /sensor/latest、时序拉取、5min/1h 聚合查询 | 一期残留数据（≤259 万条）只读留档，二期无读方 |
| 保留 | 原始 90 天 + 5min 聚合 90 天 + 1h 聚合 3 年 | 原样保留（历史快照），清理列三期 |
| `sensor_reading_1h` 归档表 | **实体=连续聚合视图（本库）** | 04 §5.2 预留的 TiDB 同名表**二期不建**（避免双写双口径，§9-②） |
| 在线期口径 | 见 §9-①（NFR-07 修订：原始 90 天 + 聚合 180 天/3 年） | — |
| 三期出口 | 1h 聚合导出 Parquet/TiDB 由三期实施（04 §5.2 原口径顺延） | — |

---

## 4. iot 服务（:8097）与智慧生产

### 4.1 进程结构与写入管道

| 项 | 冻结值 |
|---|---|
| 框架/端口 | FastAPI + uvicorn，业务 8097；`/healthz`（不依赖外部）/`/readyz`（校验 TiDB+Timescale+MQTT 三连接）/`/metrics`（CONVENTIONS §2.14） |
| 依赖 | `paho-mqtt`、`psycopg2-binary` + 既有 base 镜像（pydantic/fastapi/prometheus-client/httpx）；TiDB 池复用 `hsf_common.db` 双池模式 |
| MQTT 订阅线程 | 单线程回调：解析/校验 → 进程内批量缓冲 |
| 缓冲刷新 | **500 条或 2s 先到者刷**（CONVENTIONS-P2 T-2）；单批 `execute_values` + `ON CONFLICT DO NOTHING`；刷后（a）更新进程内 latest 缓存（b）UPDATE `hsf_biz.sensor.last_*`（48 行）（c）评估规则/决策 |
| 批量指标 | `hsf_iot_mqtt_received_total` / `hsf_iot_readings_written_total` / `hsf_iot_onconflict_skipped_total` / `hsf_iot_flush_seconds`（Histogram）/ `hsf_iot_mqtt_invalid_total` / `hsf_iot_rule_tripped_total` / `hsf_iot_advice_created_total` |
| 副本 | **固定 1**（latest 缓存与回差状态机为进程内状态；D-07-1/A-07-3），manifest 禁 HPA |

### 4.2 告警规则引擎（阈值 + 滞后回差）

一期 4 条规则的**进入阈值与幂等键不变**（05 §7.2 / 04 §4.10），二期新增退出回差 HYST=5（env `HYST_PCT` 可调）：

| rule_code | metric | 进入（触发，=一期冻结） | 退出（回差 +5） | 单测断言 |
|---|---|---|---|---|
| RULE_DROUGHT | soil_moisture | `< 15` | `≥ 20` | 14.9 触发；随后 16 **不**重复触发（未退出）；20 → NORMAL；再 14.9 → 新告警（新 trig_time） |
| RULE_WATERLOGGING | soil_moisture | `> 85` | `≤ 80` | 85.1 触发；84.9→80 区间抑制；80 → NORMAL |
| RULE_GALE | wind_speed | `> 17` | `≤ 14` | 17.1 触发；14~17 抑制；14 → NORMAL |
| RULE_PEST | pest_index | `> 80` | `≤ 75` | 80.1 触发；75~80 抑制；75 → NORMAL |

状态机（每 `(device_code, rule_code)` 一个槽位，进程内 dict）：

```
NORMAL ──满足进入条件──► TRIPPED（仅此刻写 alert_record，uk_rule_reading 兜底幂等）
TRIPPED ──满足退出条件──► NORMAL（不自动改 alert_record.state——CLOSED 仍由人工闭环，
                          与 05 §7.1 端点 6 的二期语义一致）
```

- **重启重建**：iot 启动时对每 `(device_code, rule_code)` 查 `alert_record` 最近 1 条 state≠CLOSED 记录，存在则置 TRIPPED（防止重启后同一持续异常立刻重复告警）。
- 写告警后动作（承一期契约 05 §7.2④）：失效缓存键 7/8（`hsf:stat:alerts:*` 前缀）→ 写 notify（§4.5）→ domain_event。
- `POST /sim/alerts/{id}/transition`（一期冻结为 P2 的 A-05-2）二期**不实现**（iot 无该端点，避免双入口；人工闭环列三期评估）。

### 4.3 灌溉决策模型（阈值 + 回差 + 用水量公式）

**参数（env 可调，缺省冻结）**：

| 参数 | env | 缺省 | 含义 |
|---|---|---|---|
| 触发阈值 | `IRR_START_PCT` | 40.0 | 墒情低于此值生成建议（workflow-p2 口径） |
| 恢复阈值 | `IRR_STOP_PCT` | 55.0 | 墒情回升至此值自动闭环（回差 15 个百分点，防抖动） |
| 田间持水率 | `IRR_FC_PCT` | 55.0 | θ_fc，与 sensor 量纲一致（相对含水量口径，04 §4.7 量程 5~95） |
| 计划湿润层深度 | `IRR_DEPTH_M` | 0.30 | h（m），粮油作物典型根系层 |
| 土壤干容重 | `IRR_SOIL_GAMMA` | 1.40 | γ（t/m³），红壤/水稻土典型值 |
| 去重窗口 | `IRR_DEDUP_HOURS` | 6 | 同地块窗口内仅 1 条建议 |

**用水量估算公式（冻结）**：

```
M（m³/亩） = 667 × h × γ × (θ_fc − θ_now) / 100
总灌溉量 Total（m³） = M × area_mu（hsf_md.land_parcel.area_mu；面积缺失则 total_m3=NULL）
示例：θ_now=38 → M = 667 × 0.3 × 1.4 × (55−38)/100 ≈ 47.6 m³/亩（典型灌水定额 20~60 m³/亩区间，模型合理）
说明：公式为工程简化模型（重量含水率口径），输出为"决策参考值"，实际执行由村管护员按经验修正——文案在 mobile 卡片标注"建议值"。
```

**决策状态机与幂等**：

```
墒情 < IRR_START(40) 且 该地块当前 6h 窗口无建议记录 → 生成 irrigation_advice(state=OPEN)
墒情 ≥ IRR_STOP(55) 且 存在 OPEN 建议 → closed_at=NOW()，close_reason='AUTO_RECOVER'
6h 去重：dedup_key = f"{parcel_code}:{窗口起点}"，窗口起点=recorded_at 按本地时间 0/6/12/18 点对齐
         → UNIQUE KEY uk_advice_dedup(dedup_key)，INSERT IGNORE（对齐 04 §4.6 幂等模式）
单测断言（边界值）：39.9 生成 / 40.1 不生成；54.9 保持 OPEN / 55.0 自动闭环；
                  同窗口第二次极端读数 → 行数不变（uk_advice_dedup 生效）
```

### 4.4 hsf_biz.irrigation_advice DDL（冻结）

```sql
CREATE TABLE IF NOT EXISTS hsf_biz.irrigation_advice(
  id            BIGINT        NOT NULL AUTO_INCREMENT,
  parcel_code   CHAR(20)      NOT NULL COMMENT '触发地块（跨库逻辑引用 hsf_md.land_parcel，无外键 D-04-3）',
  sensor_code   CHAR(20)      NOT NULL COMMENT '触发墒情传感器',
  village_code  CHAR(16)      NOT NULL COMMENT '所在村（冗余，范围过滤免联）',
  trigger_value DECIMAL(10,3) NOT NULL COMMENT '触发时墒情（%）',
  theta_fc      DECIMAL(5,1)  NOT NULL DEFAULT 55.0 COMMENT '决策参数快照（复盘可重算）',
  depth_m       DECIMAL(4,2)  NOT NULL DEFAULT 0.30,
  soil_gamma    DECIMAL(3,2)  NOT NULL DEFAULT 1.40,
  per_mu_m3     DECIMAL(10,1) NOT NULL COMMENT 'M=667×h×γ×(θfc−θnow)/100（本文 §4.3 冻结公式）',
  area_mu       DECIMAL(10,2) NULL COMMENT '地块面积快照（land_parcel.area_mu）',
  total_m3      DECIMAL(12,1) NULL COMMENT 'M×area_mu；面积缺失为 NULL',
  state         VARCHAR(16)   NOT NULL DEFAULT 'OPEN' COMMENT 'OPEN待处置/CLOSED已闭环',
  closed_at     DATETIME      NULL,
  close_reason  VARCHAR(64)   NULL COMMENT 'AUTO_RECOVER墒情回升自动闭环',
  dedup_key     VARCHAR(64)   NOT NULL COMMENT 'parcel_code:6h窗口起点（幂等键）',
  created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_advice_dedup (dedup_key),
  KEY idx_parcel_time (parcel_code, created_at),
  KEY idx_state_village (state, village_code, created_at)
) COMMENT='灌溉建议（阈值 40%+回差 15+6h 去重；用水量公式冻结于 07 号 §4.3）';
```

### 4.5 通知 notify（表 + 写入/读取规则）

```sql
CREATE TABLE IF NOT EXISTS hsf_biz.notify(
  id           BIGINT       NOT NULL AUTO_INCREMENT,
  alert_id     BIGINT       NULL COMMENT '关联告警 alert_record.id（kind=ALERT 时非空）',
  advice_id    BIGINT       NULL COMMENT '关联灌溉建议 irrigation_advice.id（kind=ADVICE 时非空）',
  user_role    VARCHAR(16)  NOT NULL COMMENT '通知目标角色：admin/town/village/keeper（D-07-6）',
  village_code CHAR(16)     NOT NULL COMMENT '事件村快照（4 行同值写入；读侧按 05 §2.4 角色范围过滤与展示，见下方读取模型）',
  kind         VARCHAR(16)  NOT NULL DEFAULT 'ALERT' COMMENT 'ALERT告警/ADVICE灌溉建议',
  title        VARCHAR(128) NOT NULL COMMENT '标题（中文，如"清溪村旱情告警"）',
  content      VARCHAR(512) NOT NULL COMMENT '正文（含触发值/建议量等摘要）',
  read_at      DATETIME     NULL COMMENT '已读时间（NULL=未读；mobile 拉取后显式置位）',
  dedup_key    VARCHAR(64)  NOT NULL COMMENT 'alert:{id} 或 advice:{id}（事件幂等键）',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_notify_dedup (dedup_key, user_role),
  KEY idx_role_time (user_role, created_at),
  KEY idx_role_unread (user_role, village_code, read_at)
) COMMENT='站内通知（写侧=iot，读侧=mobile 拉取 API；已读=read_at 置位）';
```

写入规则：每条新告警/新建议 → 4 行（admin/town/village/keeper，INSERT IGNORE，幂等键 `uk_notify_dedup`），**全部行 `village_code`=事件村**（快照冗余，供读侧范围过滤与展示）；title/content 由 iot 模板生成（中文）。coop **不写行**：通知推送面二期仅 4 角色，coop 用户 `/notify` 返回空集；告警**查询面**不变（§4.6 端点 3 仍按 05 §2.4 coop=关联地块所在村集过滤）——推送面与查询面分离，二者不矛盾。

**读取模型（§4.6 端点 5 消费，冻结；对齐 05 §2.4 数据范围矩阵）**——`user_role` 恒取自 JWT（不接受入参 role，防越权拉取他角色通知），行级角色过滤与数据范围过滤**叠加执行**，同一事件对每个用户至多可见 1 行：

| JWT 角色 | 范围过滤（与 `user_role=<JWT 角色>` 同时生效） | 依据 |
|---|---|---|
| admin | 无村过滤（全县可见） | 05 §2.4 |
| town | `LEFT(village_code,9) = 本乡 org_code`（事件村前 9 位=乡码） | 05 §2.4 乡内村集；乡/村编码规则 04 §9.1 |
| village | `village_code = 本村 org_code`（11 位村码=org_code） | 05 §2.4 本村 |
| keeper | `village_code ∈ 所辖设施所在村集`（`SELECT DISTINCT f.village_code FROM hsf_biz.workorder w JOIN hsf_biz.facility f ON w.facility_code=f.code WHERE w.assignee_id=me`） | 05 §2.4 alerts 行同口径（keeper 属管护队、无村归属，不写"本村"） |
| coop | 空集（无行写入，见写入规则） | 05 §2.4 coop 仅查询面 |

- 范围解析由 iot 完成：`x-user-id` → `hsf_md.party.org_code`（read pool 直查，结果进程内缓存）；keeper 村集按上式查询并缓存。

### 4.6 iot REST 契约（冻结；鉴权同 05 §7 require_login 模式，错误码 HTTP 映射同 05 §2.1）

| # | 方法/路径 | 入参 | 出参（200） | 权限/数据范围（§2.4 矩阵） |
|---|---|---|---|---|
| 1 | `GET /devices` | villageCode、metric、page、size | `{total,page,size,items:[{sensorCode,metric,villageCode,parcelCode,status,lastValue,lastRecordedAt}]}`（数据源 `hsf_biz.sensor`，字段口径同 05 §7.1 端点 2） | 登录（村/乡/县范围过滤） |
| 2 | `GET /sensor/latest` | villageCode | `{items:[{sensorCode,metric,value,recordedAt}]}`（进程内 latest 缓存，A-07-3） | 登录 |
| 3 | `GET /alerts` | state、villageCode、page、size | 同 05 §7.1 端点 4 契约（读 alert_record） | 登录（范围过滤） |
| 4 | `GET /irrigation-advice` | state、parcelCode、villageCode、page、size | `{total,page,size,items:[{id,parcelCode,sensorCode,villageCode,triggerValue,perMuM3,areaMu,totalM3,state,closedAt,createdAt}]}` | 登录（范围过滤；keeper=所辖设施所在村） |
| 5 | `GET /notify` | unreadOnly、page、size | `{total,unread,page,size,items:[{id,alertId,adviceId,kind,title,content,villageCode,readAt,createdAt}]}`；**role 取自 JWT**（不接受入参 role，防越权拉取他角色通知） | 登录；范围过滤按 **§4.5 读取模型**（admin 全县 / town 乡内村集 / village 本村 / keeper 所辖村集 / coop 空集） |
| 6 | `POST /notify/{id}/read` | — | `{id,readAt}`；置 `read_at=NOW()`，已读重复调用幂等 no-op；行.user_role ≠ token 角色 → FORBIDDEN | 登录 |
| 7 | `GET /healthz` `/readyz` `/metrics` | — | 探针/ Prom text（Response+CONTENT_TYPE_LATEST，CONVENTIONS §2.8） | 公开/公开/内网 |

### 4.7 web 同源反代（web:2.0 nginx.conf.template 增量，冻结）

```nginx
# 一期模板已有（保持不变；一期遗留专项=E2E 用例 19 补验证，CONVENTIONS-P2 T-1）：
location = /graphql {
  proxy_pass http://${GW_UPSTREAM};
}
# 二期增量——与既有 ${GW_UPSTREAM}/${SIM_UPSTREAM} 同一 env 注入模式，模板禁止硬编码 namespace：
location /iot/ {             # mobile/admin 调 iot（同源免跨域）
  proxy_pass http://${IOT_UPSTREAM};
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
}
```

> 上游变量注入：一期 `services/web/nginx.conf.template` 既有 `${GW_UPSTREAM}/${SIM_UPSTREAM}` 模式（默认值在 `services/web/Dockerfile:14-15`）。二期新增 `IOT_UPSTREAM=iot-svc.hsf-prod.svc.cluster.local:8097`——web:2.0 Dockerfile `ENV` 默认值同法 + manifest env 显式声明，hsf-dev 同构 `sed 's/hsf-prod/hsf-dev/g'` 时 env 值随清单一并替换，不破坏 dev 同构（§1.2）。

> 前端增量：admin 增"设备与墒情"页签（设备列表+最新值+告警）与"灌溉建议"卡片；mobile 增通知列表 + 灌溉建议卡（均经 `/iot/` 反代）。全部文案中文，禁 CDN（CONVENTIONS §2.12）。

---

## 5. 全局限流 ratelimit-svc（:8096）

### 5.1 API 契约（冻结）

| # | 方法/路径 | 入参 | 出参（200） | 说明 |
|---|---|---|---|---|
| 1 | `GET /limit` | `key`（必填，调用方已归一化的限流键）、`cost`（默认 1） | `{"allowed": true|false, "remain": <int>}` | 固定窗口判定；`remain`=本窗口余量（耗尽后为 0） |
| 2 | `GET /healthz` / `GET /metrics` | — | ok / Prom text | 探针不依赖 Redis（进程存活即 200）；/readyz 校验 Redis |

固定窗口实现（workflow-p2 口径的原子化实现，§9-⑥）：

```
key_db   = f"hsf:rl:{key}:{分钟epoch}"        # 分钟epoch = int(now/60)
cur      = Lua 原子脚本（key_db/cost 入参，单次 RTT）：
           c = INCRBY key_db cost；c == cost（=本窗口首次写入）时 EXPIRE key_db 120
           ——INCRBY 与 TTL 置位同脚本原子完成，消除"INCR 后 EXPIRE 前"进程崩溃
             留下无 TTL 永生键的窗口（T-2"INCR+EXPIRE 120"的原子化等价实现，
             键格式/TTL=120s（窗口 60s+60s 余量）/响应语义均不变，§9-⑥）
allowed  = cur <= RATE_CAP（env，二期冻结 1200）
remain   = max(RATE_CAP − cur, 0)
cost>1   （E2E/验证用）：同脚本 INCRBY cost 一次计入——等价 N 请求占用，确定性复现 429 而无需真打 N 次
```

| 项 | 冻结值 |
|---|---|
| Redis 客户端 | `redis.cluster.RedisCluster`（启动节点取 `REDIS_URL` 首个 + `socket_timeout=2`——一期 `cache.py:73-76` 已实证 hostname 广播集群可用；**禁止 ioredis**，T-3） |
| RATE_CAP | env，二期冻结 **1200**（每 key 每分钟；偏离声明 §9-③） |
| 固有语义声明 | 固定窗口存在窗口边界突刺（相邻窗口各打满 = 瞬时 2×cap）；一期令牌桶的"补充速率"语义不延续——两种口径差异在验收与容量模型中按固定窗口理解 |
| 键空间 | 前缀 `hsf:rl:`（`hsf:` 统一前缀惯例，04 §8.2）；TTL 120s 自清理，不占长期内存 |
| 部署 | Deployment 2 副本（无状态、Redis 侧天然共享）；ClusterIP；requests cpu 150m/mem 256Mi，limits cpu 1/mem 512Mi |

### 5.2 gw 集成（gw:2.0，冻结）

```js
// pre-middleware 内（一期 429 中间件位置不变，gateway.js:88-104 骨架保留）：
const key = user ? `u:${user.sub}` : `ip:${ip}`;            // 键归一化不变（05 §1.4）
async function checkLimit(key) {                             // RATE_MODE=svc（默认）
  try {
    const r = await fetch(`${RATE_SVC}/limit?key=${encodeURIComponent(key)}&cost=1`,
                          { signal: AbortSignal.timeout(500) });   // 冻结 500ms（见下）
    if (!r.ok) throw new Error(`rate-svc ${r.status}`);
    return { allowed: (await r.json()).allowed !== false, mode: 'svc' };
  } catch (e) {
    warnThrottled(key, e);                    // fail-open：放行 + 节流告警日志（同 key 60s 一条）
    return { allowed: true, mode: 'svc-failopen' };
  }
}
// allowed=false → 与一期完全相同的 429 响应体（HTTP 429 + errors[0].extensions.code=RATE_LIMITED）
```

| 项 | 冻结值 | 理由 |
|---|---|---|
| 超时 | **500ms**（AbortSignal） | 同步限流检查在请求关键路径上：p95 预算 500ms（一期阈值）之下，检查预算应 ≤10%；同命名空间 RTT <5ms，500ms 已含 100× 裕度。workflow 任务文本的"5s"按此收紧（§9-⑥） |
| fail-open | 非 200 / 网络错误 / 超时 → 放行 + 节流告警日志（60s/key） | CONVENTIONS-P2 T-3"网络错误=放行并计数告警日志"；可用性优先于限流精度（限流失效不阻断业务） |
| env | `RATE_MODE=svc`（默认）、`RATE_SVC=http://ratelimit-svc.hsf-prod.svc.cluster.local:8096`、`RATE_CAP=6000`（**仅 local 回退模式读取**） | manifest 唯一事实源（T-3） |
| ioredis | gw 删除 `ioredis` require 与全部 Redis 客户端代码（T-2/package.json 同步删除依赖） | T-3 红线；static_gate 检查 |
| 429 语义 | 与一期逐字节一致（express 层直接回 429，不经 Apollo） | 一期 R 修复的回归保护（docs/20 §5.1） |

### 5.3 与一期进程内桶的切换开关（冻结）

| RATE_MODE | 行为 | 容量语义 | 用途 |
|---|---|---|---|
| `svc`（默认） | HTTP 调 ratelimit-svc，固定窗口，**跨副本全局共享** | 每 key 1200/min 集群级唯一口径 | 二期常态 |
| `local` | 一期进程内令牌桶（保留约 15 行原实现，cap=RATE_CAP env） | 每 key 6000/min × 副本数（局部口径） | **回退开关**：ratelimit-svc/Redis 故障且 fail-open 不可接受时，manifest 单行 `RATE_MODE=local` → apply 即回退，不改镜像 |

> 说明：workflow 任务文本"删除进程内 buckets"按 ask 要求修订为"保留为回退路径"（§9-⑥）——切换开关必须以代码存在为前提；回退路径不使用 ioredis，与 T-3 红线无冲突。

### 5.4 全局语义验证口径（部署工程师执行，确定性复现）

| 步骤 | 命令/动作 | 期望 |
|---|---|---|
| 1 | admin 登录取 token（key=`u:{sub}`） | — |
| 2 | `GET ratelimit-svc:8096/limit?key=u:{sub}&cost=1200` | `{"allowed":true,"remain":0}`（本窗口打满） |
| 3 | 同 key 再 `cost=1` | `{"allowed":false,"remain":0}` |
| 4 | 换一来源（另一 Pod/E2E 容器）重复步骤 3 | 仍 `allowed=false` ⇒ **共享态=全局限流生效**（对一期"每副本独立 6000"缺陷（docs/20 §5.1）的闭环） |
| 5 | 经 gw 真实突发：登录态并发 1500 请求（E2E 用例 8 改造，§6.1） | 出现 ≥1 个 HTTP 429 + RATE_LIMITED |
| 6 | 停 ratelimit-svc（scale 0）经 gw 打 50 请求 | 全部 200/400（fail-open 放行）+ gw 日志节流告警；恢复 scale 2 |

---

## 6. 测试设计

### 6.1 E2E 新增用例（一期 13 条全部保留 + 新增 6 条 = 19 条 ≥17，冻结）

| # | 用例 | 步骤与断言要点 | 优先级 | 对应 |
|---|---|---|---|---|
| 14 | TimescaleDB 就绪探针 | `GET http://iot-svc...:8097/readyz` → 200（校验 TiDB+Timescale+MQTT 三连接） | P0 | §3.1/§4.1 |
| 15 | 设备列表 48 台 | page 同源 `fetch('/iot/devices?size=100')`（带 token）→ `total=48` 且含 soil_moisture 序列 | P1 | §4.6 端点 1 / FR-501 |
| 16 | MQTT 链路端到端 | 记录基线 → `POST /sim/tick?steps=1`（响应 `published=48`）→ **5s 内** iot `/sensor/latest` 出现 > 基线的 `recordedAt` | P0 | §2/§4.1（T-2"5s 内时序可查"） |
| 17 | 灌溉建议生成 + 6h 去重 | `POST /sim/tick?steps=200`（≥1 条 <40 墒情概率≈1，A-07-4）→ `/irrigation-advice` 新增 ≥1 条且 `perMuM3>0`；再 tick 200 条 → total **不增**（同窗口去重） | P0 | §4.3/§4.4 |
| 18 | 通知列表与已读 | village 登录 → `/notify`（role 自 JWT）→ `total≥1` 且含 ALERT/ADVICE；`POST /notify/{id}/read` → `readAt` 非空且 `unreadOnly=true` 过滤生效 | P1 | §4.5/§4.6 端点 5/6 |
| 19 | 同源反代 /graphql | `page.goto(WEB)` 后 `page.evaluate(fetch('/graphql',{query:'{__typename}'}))` → 200 | P1 | §4.7 / T-1 一期遗留专项 |

一期用例修订 1 处（断言不变、触发方式改造）：**用例 8（限流触发）**——原"5×cap 无登录并发突发（30000 次）"改为"登录态 1500 并发突发 + 步骤 3/4 的 ratelimit-svc 跨视角断言"（§5.4；RATE_CAP=1200 下 1500 突发必然触发，成本降 20 倍）。其余 12 条（三端渲染/登录/错密/一张图/项目 7 态/工单 4 态/告警查询/联邦跨域）零改动回归。脚本仍为 JSON 行输出 + `SUMMARY fail= total=`（`e2e_browser.py:265` 模式），限流用例保持最后执行。

### 6.2 单测清单（iot/tests、ratelimit/tests，不依赖外部服务）

| 模块 | 用例 | 断言 |
|---|---|---|
| iot 规则引擎 | 回差状态机 ×4 规则 | §4.2 单测列全过；重启重建（TRIPPED 恢复） |
| iot 决策 | 阈值/回差/去重/公式 | 39.9 生成、40.1 不生成；54.9 保持、55.0 闭环；同窗口幂等；θ_now=38 → per_mu_m3≈47.6 |
| iot 接入 | topic/payload 校验 | 不一致丢弃计数；越界值丢弃；500 条或 2s 触发 flush（mock 时间） |
| iot 通知 | 幂等与置位 | 重复事件不重复插（uk_notify_dedup）；read 幂等 no-op |
| ratelimit | 固定窗口 | cost=1/1500；窗口翻分钟重置；cap 边界（=cap 放行、=cap+1 拒绝）；fakeredis/mock |

### 6.3 k6 iot-write 场景设计（冻结；单独报告，**不进 thresholds**——T-2）

| 项 | 冻结值 |
|---|---|
| 脚本 | `perf/stress.js` 增 `iot-write` 场景（40-k6.yaml ConfigMap 同源同步） |
| 压测路径 | `POST {SIM}/sim/tick?steps=500`（登录态，直打 sim 31211——机器流量直连口径，05 §7 评审 R10；**不顶格** TICK_MAX_STEPS=5000 上限，留 10× 余量） |
| 负载模型 | constant-vus 4 VU × 每 VU 50 次迭代，迭代间 sleep 5s（缓冲排水）；单次迭代发布 500×48=**2.4 万条**；总量 4×50×2.4 万 = **480 万条**（与 §3.6"压测余量"口径一致） |
| 超时/背压防护 | 单迭代 2.4 万条 QoS1 同步发布必须在 k6 HTTP 超时内闭合：paho 默认 inflight=20 下 24,000 条 ≈ 1,200 轮 PUBACK×2~5ms ≈ 2.4~6s（k6 默认 60s 内）；显式配置三件——k6 请求 `timeout: '120s'` 兜底、sim 发布器 `max_inflight_messages_set(100)`（对齐 broker `max_inflight_messages 100`，§2.3）、迭代间 sleep 5s 排缓冲。E2E 用例 17（steps=200=9,600 条）无此问题 |
| 度量 | k6 侧 Trend `iot_tick_duration` + Counter `iot_published`；结束后读 `iot /metrics` 的 `hsf_iot_readings_written_total` 差值 → **条/s = Δ/elapsed**（全链路：sim 生成→MQTT→iot 缓冲→TimescaleDB 落库） |
| 判读记录 | ① 稳态吞吐条/s；② `hsf_iot_flush_seconds` p95；③ `hsf_iot_onconflict_skipped_total`（应≈0）；④ 无积压（tick 响应时间无单调上涨） |
| 参考目标 | ≥5000 条/s（观测记录，无硬阈值）；对照一期 NFR-04 直写 TiDB 1 万条/s 时如实注明口径差异（二期含 broker 转发 + 幂等索引维护） |
| 报告 | 数值与命令输出写入 docs/21（二期融合测试报告），不参与 k6 thresholds 判定 |

### 6.4 k6 基线适配与静态门禁增量

- **baseline.js 改造**：browse 场景增每 VU 登录——登录号循环取自 **04 §9.1 冻结的 15 个种子账号**（`13800000601~610`、`13900000901~905`，口令 `hsf@2026`），并同步修复 `stress.js:17` 号码模板缺陷（`'13800000'+2位` 生成 10 位错号、仅 `13800000604` 合法——改为按冻结号码表取号，废除 `%600` 虚拟池）。RATE_CAP=1200 固定窗口下，无登录流量全部聚合在单个 `ip:` 键（≈4800/min）必触发 429；改造后 200 browse VU 共享 **15 个 `u:` 键**（每键 ≈13 VU × 17~24 req/min ≈ **230~320/min < 1200**）不受影响。**不新增种子账号**（对 04 §9.1 零偏离，仅修脚本缺陷，§9-③）。读查询与负载画像不变。
- **static_gate.py 二期检查项**（workflow-p2 数据工程师任务原文 + 本文增补）：① gw 代码/`package.json` 无 ioredis；② iot 含 `TIMESCALE_DSN` 与 `paho-mqtt`；③ sim 源码不含规则引擎符号（`RULE_DROUGHT` 等，D-07-1）；④ 新增服务（iot/ratelimit）必含 `/healthz`+`/readyz`+`/metrics` 与 resources.limits；⑤ gw 含 `RATE_MODE`/`RATE_SVC`；⑥ E2E 脚本用例数 ≥17。

---

## 7. 滚动升级预案（二期上线顺序，冻结）

**镜像策略**：二期统一 tag **2.0**（sim/iot/ratelimit/gw/web 全部重建推仓，T-2）；iam/land/project/facility 沿用 1.0.1 不重建。**镜像 tag 只改 manifest，禁止 `kubectl set image` 后再 apply 旧清单**（T-3 / 一期教训）。以下步骤 1~7 均以 **hsf-prod** 为对象冻结；hsf-dev 同构口径见 §1.2（sed 派生清单按需另行 apply，不在步骤范围）。

| 步骤 | 动作 | 就绪判据（再进下一步） |
|---|---|---|
| 1 | 同步代码 `/opt/hsf-ias`（CONVENTIONS §3.1 冻结路径）；构建并推送 2.0 五个镜像 | registry tags list 可见 2.0 |
| 2 | 更新 `00-base.yaml` Secret（MQTT_PASS/TIMESCALE_PASS）并 apply | 两命名空间 Secret 含新键 |
| 3 | apply `25-timescale.yaml` + `30-mqtt.yaml`（StatefulSet×2，worker1）→ 跑 `scripts/init-ts.sh` | 两 pod Running；`CREATE EXTENSION`/hypertable/聚合/策略幂等执行通过 |
| 4 | apply `20-services.yaml` 增量（ratelimit-svc、iot-svc） | ratelimit 2/2、iot 1/1 Running；`/limit?..cost=1` 冒烟 200 |
| 5 | apply sim:2.0（Deployment 滚动，maxSurge=1/maxUnavailable=0） | sim pod 新版 Ready；日志可见 MQTT 连接成功 |
| 6 | apply gw:2.0（env 增 RATE_MODE/RATE_SVC） | gw 滚动完成；`{__typename}` 200；§5.4 步骤 1~4 通过 |
| 7 | apply web:2.0（nginx 增 `/iot/` 反代，env 增 `IOT_UPSTREAM`；`/graphql` 一期已有，仅补 E2E 用例 19） | 三页 + 新页签 HTTP 200；E2E 19 条全跑 |

- **滚动安全性**：sim/gw 均为 Deployment `RollingUpdate(maxSurge=1, maxUnavailable=0)`，升级期零中断；iot 单副本重启窗口（秒级）由 broker 离线补投兜底（§2.3）。
- **StatefulSet×2 升级语义**：单副本 RollingUpdate 实际为"停旧起新"，窗口 <2min（镜像本地仓库）；PVC（volumeClaimTemplates）随 pod 重建保留，**数据不丢**；升级窗口内 MQTT 读数由 `max_queued_messages 10000` 缓冲，iot 回连后自动补投。
- **数据迁移=空库新建不迁移**（D-07-8）：TimescaleDB 由 init-ts.sh 全新初始化；TiDB 侧仅 iot 启动幂等建 `irrigation_advice`/`notify`（CREATE TABLE IF NOT EXISTS），**存量表零 DDL 变更、存量行零迁移**；一期 TiDB hsf_ts 数据原样留档。
- **资源窗口**：k6/E2E Job 用后即删（PriorityClass hsf-batch，一期同模式）；上线全程新组件 +1.2 CPU requests，配额余量充足（§1.3）。

---

## 8. 回滚预案（冻结）

**触发条件**（任一）：新链路验收（§7 步骤 6/7 判据）失败且 30 分钟内无法修复；k6 基线失败率 >5% 或 p95 较一期终版恶化 >50%；时序/决策数据出现错误口径（重复/丢失超 1%）。

| 组件 | 回滚动作 | 数据处置 | 验证 |
|---|---|---|---|
| gw | manifest：镜像 2.0→1.1.2 且删除 RATE_MODE/RATE_SVC env → apply | 无状态 | `{__typename}` 200；一期 E2E 13 条中 1~13 回归 |
| sim | manifest：2.0→1.0.1 → apply | 无状态；一期规则引擎恢复后按 `uk_rule_reading` 幂等接管告警评估（与 iot 已写记录无冲突：同幂等键空间） | `/sim/tick` 恢复写 TiDB hsf_ts（一期行为） |
| web | manifest：2.0→1.0 → apply | 无状态 | 三页 200 |
| iot / ratelimit | `kubectl scale deploy iot-svc ratelimit-svc --replicas=0`（或整体不回滚保留观测） | 无状态 | gw 已切 local 模式即无依赖 |
| hsf-timescale / hsf-mosquitto | **不回滚**（新组件无"旧版"语义）：scale 0 下线即可；PVC 数据保留 | 二期新增表 `irrigation_advice`/`notify` 可 DROP（无消费方后）；TimescaleDB 数据保留 | — |

- **整体回滚=回归一期终版清单**（git 还原 4 个 manifest 文件 + apply），一期镜像全部留存本地仓库（一期下线记录 dc985be 结论），回滚 RTO ≤15 分钟（不含 E2E 回归）。
- **回滚演练口径**：验收前执行一次"gw 切 local 模式"演练（§5.3），确认切换生效与恢复；完整回滚演练随二期验收执行一次并记录于 docs/21。
- **不可回滚项声明**：TimescaleDB 已写入的二期时序数据在回滚后无人读取（sim 1.0.1 回写 TiDB hsf_ts）——数据保留不清理，三期若建归档管道可一并纳管；`alert_record` 中 iot 写入的告警行与 sim 1.0.1 规则引擎幂等兼容（§8 表内说明）。

---

## 9. 与一期冻结契约的差异声明（全部显式，评审对照用）

| # | 一期冻结 | 二期修订 | 理由 |
|---|---|---|---|
| ① | 01 号 NFR-07"时序在线 ≥180 天（原始）" | 原始 **90 天** + 5min 聚合 90 天 + **1h 聚合 3 年**；"≥180 天在线"由聚合层满足 | 10Gi PVC 容量约束（§3.6 核算：原始 180 天未压缩不可行）；追溯诉求由 1h 聚合承载 |
| ② | 04 §5.2"sensor_reading_1h（预留，二期建）"（TiDB） | 该表**不在 TiDB 建**，实体=TimescaleDB 连续聚合 `sensor_reading_1h`（§3.4） | 避免双写双口径；TiDB hsf_ts 零写入（T-1） |
| ③ | CONVENTIONS §2.6 / 05 §1.4"RATE_CAP=6000/min 令牌桶" | **固定窗口 1200/min/key**（env 可调）；k6 browse 增每 VU 登录（15 个 `u:` 键复用 04 §9.1 冻结种子账号，同步修复 stress.js 号码模板 10 位错号缺陷；单键 ≈230~320/min，不新增种子账号）；E2E 限流用例触发量 30000→1500 | 固定窗口下 429 确定性可复现，根除一期"100 tokens/s 永打不穿"验收缺陷（docs/20 §5.1）；无登录共享出口 IP 的机器流量对 per-user 限流无意义 |
| ④ | 04 §4.7"`sensor.last_*` 仅 sim 写" | **仅 iot 写**（每批 flush 后 UPDATE）；sim 2.0 不再写 | 写职责随规则引擎一并移交 iot（D-07-1）；列语义/消费方（/sim/latest、/sensor/latest 快路径）不变 |
| ⑤ | 05 §7.1 端点 1 响应 `{inserted, batches, alerts_created}` | sim 2.0 tick 响应 **`{published, steps}`**（发布条数；时序落库与告警改为异步结果） | sim 不再写库（D-07-1）；k6 iot-write 与 E2E 用例 16 同步改用 `published`；`inserted/batches` 字段废弃 |
| ⑥ | workflow-p2 任务文本三处：gw"删除进程内 buckets"、限流超时"5s"、固定窗口"INCR+EXPIRE 120" | 保留进程内桶为 `RATE_MODE=local` 回退路径（§5.3）；超时冻结 **500ms**（§5.2）；计数改为**单 Lua 脚本原子 INCRBY+EXPIRE**（§5.1，键格式/TTL/响应语义不变） | ask 要求"与一期进程内桶的切换开关"必须以代码存在为前提；同步限流检查不得占用请求预算 10% 以上；INCR 与 EXPIRE 分离存在崩溃窗口（无 TTL 永生键） |
| ⑦ | 05 §7.1 端点 6（告警 transition，冻结为 P2） | 二期仍不实现；iot 不提供告警处置写接口 | 避免与人工闭环流程双入口；列三期评估 |
| ⑧ | 04 §10 / 05 §8.2"无消息队列" | MQTT 引入为**设备数据面**通道（非业务事件总线）：domain_event/Outbox 语义不变，仍不投递（ADR-HSF-004） | MQTT 只承载 sensor_reading 数据流，业务事件仍走 Outbox 表——两通道边界见表 |
| ⑨ | 05 号 D-05-7"一期无 Subscription；WebSocket 列二期" | 二期**不引入 WebSocket**：通知实时性 = mobile 拉取 `/notify`（§4.5/§4.6 端点 5），大屏轮询口径延续 | 通知面为低频拉取场景（量级核算见 D-07-6），mobile H5 轮询即可满足；引入 WS 需 gw 与部署面双变更且平台仍无推送通道（无 MQ/WS 基础设施），收益不抵——WebSocket 列三期（§2.1 原注"口径延续"实为变更，本行显式声明） |

---

## 10. 验收矩阵（二期，与 docs/21 融合测试报告对照执行）

| # | 验收项 | 验证方法（验收环境执行） | 对应 |
|---|---|---|---|
| V2-01 | 新组件上线 | `kubectl -n hsf-prod get pod,sts,pvc` → timescale/mosquitto/iot/ratelimit 全 Running，PVC Bound 且 worker1 调度 | §1.2/§1.3 |
| V2-02 | MQTT 认证生效 | 匿名 `mosquitto_pub` 被拒（Connection Refused: not authorised）；hsf 账号发布成功 | §2.4 |
| V2-03 | QoS1 幂等 | 同一 payload 重复发布 2 次 → `hsf_iot_onconflict_skipped_total` +1，表行数不增 | §3.2/D-07-3 |
| V2-04 | hypertable 与策略 | `\dx+ timescaledb`；`SELECT show_chunks('hsf_ts.sensor_reading');`（或查 `timescaledb_information.chunks`）确认按 1 天分块；`SELECT job_id,proc_name FROM timescaledb_information.jobs;` 共 **6** 条策略 job（2×policy_refresh_continuous_aggregate + 3×policy_retention + 1×policy_compression，对应 §3.4/§3.5） | §3.3~3.5 |
| V2-05 | 连续聚合出数 | `SELECT * FROM hsf_ts.sensor_reading_5min ORDER BY bucket DESC LIMIT 5` 有数；1h 视图 bucket 对齐整点 | §3.4 |
| V2-06 | TiDB hsf_ts 零写入 | sim:2.0 上线后 `SELECT MAX(recorded_at) FROM hsf_ts.sensor_reading`（TiDB）不再增长 | §3.7/D-07-2 |
| V2-07 | 灌溉建议 | 边界单测全过；E2E 用例 17 通过；`per_mu_m3` 与公式手工复算一致（θ=38→47.6） | §4.3/§4.4 |
| V2-08 | 通知闭环 | E2E 用例 18 通过；`uk_notify_dedup` 重复事件不重复插 | §4.5 |
| V2-09 | 全局限流 | §5.4 六步全过：共享态 429、fail-open 放行 + 告警日志、切 `local` 回退生效并恢复 | §5 |
| V2-10 | E2E 全量 | 19 条 0 FAIL（JSON 行输出 SUMMARY fail=0 total=19） | §6.1 |
| V2-11 | k6 基线 | browse+report 0 失败、p95 记录在案（阈值口径与一期一致） | §6.4 |
| V2-12 | iot-write 吞吐 | 场景执行完成，条/s 与 flush p95 写入 docs/21（不设 thresholds） | §6.3 |
| V2-13 | 静态门禁 | `python hsf-ias/scripts/static_gate.py` 全绿（含 §6.4 六项二期检查） | §6.4 |
| V2-14 | 同源反代 | E2E 用例 19 通过；web 三页 + 设备页签 + 移动端通知/建议卡 200 | §4.7 |
| V2-15 | 回滚演练 | gw 切 `RATE_MODE=local` 演练通过并还原；整体回滚步骤（§8）文档化于 docs/21 | §8 |
| V2-16 | 观测接入 | Prometheus targets 含 iot/ratelimit 全 UP；Grafana 可查 hsf_iot_* 指标 | §1.3/§4.1 |
