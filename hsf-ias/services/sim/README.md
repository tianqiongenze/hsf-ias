# sim — IoT 模拟设备与读数发布服务（:8095，REST 非 GraphQL，二期 2.0）

对齐：`hsf/docs/07`（§2 MQTT 接入、§2.5 职责边界冻结）、`hsf/docs/05 §7`（一期端点契约
保留）、`hsf/docs/04 §4.6/4.7`（设备台账与量程冻结）。基础镜像
`10.167.2.175:30500/szxc/base:1.2` + 二期增量 `paho-mqtt==2.1.0`（requirements.txt），
NodePort **31211**（不变）。

**二期职责边界（07 号 §2.5 冻结）**：sim 负责"造数与发数"，iot 负责"收数、判数、决策
与落库"，broker 只管可靠转发——**一期 sim 的规则引擎（`rules.py` RULES）、TiDB hsf_ts
时序写入、`sensor.last_*` 维护、alert_rule/种子告警职责全部移交 iot 服务（:8097）**，
sim 不再持有任何规则与阈值常量（static_gate 检查项）。

## 模拟逻辑

### 1. 设备注册（FR-501）

启动时 `seed_if_empty()` 幂等注册模拟设备台账 `hsf_biz.sensor`：

- **6 类 metric × 每村 2 台 × 4 村 = 48 台**，编码 `S+6位区划+4位序号`（S5306000001~48，
  `uk_sensor_code` 幂等 + 计数双守卫防双副本重种）；iot 以本台账为准入镜像（只读）；
- metric 冻结命名与量程（04 号 §4.7）：`soil_moisture` 5~95%、`air_temp` -5~40℃、
  `wind_speed` 0~25m/s、`rainfall` 0~60mm、`pest_index` 0~100、`water_level` 0~5m；
- 每台绑定村码与种子地块 `parcel_code`（软引用，L5306000001~08 交替）。

### 2. 观测值生成（`app/rules.py::next_value`，仅保留"造数"）

- **正常带**均匀采样：soil_moisture 25~75、air_temp 12~34、wind_speed 1~9、
  rainfall 0~25、pest_index 20~65、water_level 0.5~3.0；
- **极端注入**：带规则的 metric 每条读数以 2% 概率落入极端带——
  soil_moisture → 旱情段 6~14.5 或 渍害段 86~93（两段各半）、wind_speed → 18~24、
  pest_index → 82~96，保证 iot 回差状态机必然产生真实告警（E2E 前提）；
- 阈值判定在 iot（进入条件沿用一期冻结值 + 二期回差退出，见 iot README）；
- 传入固定种子的 `random.Random` 即可复现（单测 `test_deterministic_with_seeded_rng`）。

### 3. MQTT 发布（二期新增，07 号 §2.2/§2.3 冻结）

topic `hsf/sensor/{deviceCode}/{metric}`，QoS1，retain=false；payload 冻结形态：

```json
{"device_code": "S5306000001", "metric": "soil_moisture", "value": 23.456,
 "recorded_at": "2026-11-08T10:00:00.123+08:00", "village_code": "53060010101"}
```

`recorded_at` = sim 生成时刻（ISO8601+08:00 毫秒，iot 不改写）；批量 tick 步进 1ms
保证 iot 侧幂等键可区分。两个入口共用同一发布通道（`app/mqtt_pub.py`）：

| 入口 | 行为 |
|---|---|
| 心跳线程 | 连接 broker 后按 `MQTT_INTERVAL`（env，缺省 2s）间隔发布全部 ACTIVE 设备读数（48 条/轮）；未连接时读数即弃，量级由下一轮补齐 |
| `POST /sim/tick?steps=N` | 批量触发同步发布（N 默认 1、上限 5000）；返回 `{"published": <int>, "steps": <int>, "mqtt_connected": <bool>}`——broker 未连接时 published=0（HTTP 仍 200，k6 iot-write 以 published 换算吞吐，07 号 §6.3） |

D-07-6 量级核算（≈2.1 万告警/日）以单发布方为前提：副本固定 1、uvicorn workers=1
（多 worker 会双倍心跳发布）。

### 4. 保留的查询端点（05 号 §7.1 一期冻结契约，读路径不变）

| 方法/路径 | 说明 | 鉴权 |
|---|---|---|
| `GET /sim/sensors?villageCode=&metric=&page=&size=` | 设备清单（total/page/size/items） | Bearer JWT |
| `GET /sim/latest?villageCode=` | 各设备最新值（快路径读 `sensor.last_*`——二期起由 iot flush 维护） | Bearer JWT |
| `GET /sim/alerts?state=&villageCode=&page=&size=` | 告警分页（只读；写入方=iot 回差状态机） | Bearer JWT |
| `GET /sim/alerts/statistics?dim=state\|rule\|village&villageCode=` | 告警统计（缓存键 7；iot 写告警后失效同前缀） | Bearer JWT |
| `GET /alerts`、`GET /alerts/statistics`、`GET /sensor/latest` | 任务文本口径兼容别名 | Bearer JWT |
| `GET /healthz` `GET /readyz` `GET /metrics` | 探针与指标（hsf_common.create_app 提供） | 公开 |

鉴权：`Authorization: Bearer <JWT>`（与网关同一 JWT_SECRET，HS256；缺失/无效 → 401）。
CORS 允许 web 源（env `CORS_ORIGINS`，默认 `*`）。

## 缓存键（04 号 §8.2）

| 键 | TTL | 失效时机 |
|---|---|---|
| `hsf:sim:latest:{villageCode}` | 60s（L1 15s） | 不主动失效（最新值允许秒级陈旧） |
| `hsf:stat:alerts:{dim}:{scope}` | 300s（L1 60s） | 新告警由 **iot** 写入后 `invalidate_prefix("hsf:stat:alerts:")` |

## env

| 键 | 缺省 | 说明 |
|---|---|---|
| `MQTT_HOST/PORT/USER/PASS` | hsf-mqtt/1883/hsf/— | broker 连接（MQTT_PASS 入 hsf-secrets，T-2） |
| `MQTT_INTERVAL` | 2 | 心跳发布间隔（s）；告警/通知噪声过大时按比例下调发布频次（A-07-2 同旋钮） |
| `CORS_ORIGINS` | * | 允许源 |

## 领域事件（ADR-HSF-004 Outbox）

`hsf.sim.device.registered`（种子）保留；`hsf.sim.reading.batched`/`hsf.sim.alert.raised`
随职责移交终止，对应 iot 侧 `hsf.iot.alert.raised` / `hsf.iot.advice.created`。
TiDB `hsf_ts.sensor_reading` 二期零写入（归档快照，V2-06 校验不再增长）。

## 本地测试

```bash
PYTHONPATH=services/sim python -m pytest services/sim/tests -q   # 生成器 + 发布契约单测（纯函数，无需 DB/broker）
```
