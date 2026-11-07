# sim — IoT 模拟与时序告警服务（:8095，REST 非 GraphQL）

对齐：`hsf/docs/05 §7`（端点契约冻结，D-05-3）、`hsf/docs/04 §4.6/4.7/4.10/§5.1`（DDL 冻结）、
`hsf/docs/03 §4.5`（聚合与告警 3 态）。基础镜像 `10.167.2.175:30500/szxc/base:1.2`，
NodePort **31211** 直连（不进联邦组合；机器/脚本流量直打，一期不做端点级限流——评审 R10）。

## 模拟逻辑

### 1. 设备注册（FR-501）

启动时 `seed_if_empty()` 幂等注册模拟设备台账 `hsf_biz.sensor`：

- **6 类 metric × 每村 2 台 × 4 村 = 48 台**，编码 `S+6位区划+4位序号`（S5306000001~48，
  顺次按 村→metric→台 分配，`uk_sensor_code` 幂等 + 计数双守卫防双副本重种）；
- metric 冻结命名与量程（04 号 §4.7）：`soil_moisture` 5~95%、`air_temp` -5~40℃、
  `wind_speed` 0~25m/s、`rainfall` 0~60mm、`pest_index` 0~100、`water_level` 0~5m；
- 每台绑定村码与种子地块 `parcel_code`（软引用，L5306000001~08 交替）。

### 2. 观测值生成（`app/rules.py::next_value`）

每步对 48 台各生成 1 条读数：

- **正常带**均匀采样：soil_moisture 25~75、air_temp 12~34、wind_speed 1~9、
  rainfall 0~25、pest_index 20~65、water_level 0.5~3.0；
- **极端注入**：带规则的 metric 每条读数以 2% 概率落入极端带——
  soil_moisture → 旱情段 6~14.5 或 渍害段 86~93（两段各半）、wind_speed → 18~24、
  pest_index → 82~96，保证持续 tick 必然产生真实告警（E2E/验收前提）；
- 传入固定种子的 `random.Random` 即可复现（单测 `test_deterministic_with_seeded_rng`）。

### 3. tick 写入契约（FR-502/NFR-04）

`POST /sim/tick?steps=N`（N 默认 1，上限 5000）：

```
每步 48 台 × 1 条 → 攒批 → execute_many 1000 条/批写 hsf_ts.sensor_reading
（AUTO_RANDOM(5) 主键 + PRE_SPLIT_REGIONS=4，idx_code_time 覆盖读路径）
→ UPDATE hsf_biz.sensor.last_value/last_recorded_at（每台取最后一步值，读优化冗余仅 sim 写）
→ 规则引擎仅评估本次新读数 → 命中写 hsf_biz.alert_record
→ 返回 {"inserted": <int>, "batches": <int>, "alerts_created": <int>}
```

压测口径：`steps=1000` 单请求即 48 批；k6 并发直打 31211 达 ≥1 万条/s、0 失败。

### 4. 规则引擎（FR-503，05 号 §7.2 冻结）

纯函数 `(rule, reading) → 命中规则列表`（`app/rules.py::evaluate`），规则同时落
`hsf_biz.alert_rule`（种子 4 条，uk_rule_code）：

| rule_code | metric | 条件 | 语义 | 边界值单测 |
|---|---|---|---|---|
| RULE_DROUGHT | soil_moisture | `< 15` | 旱情 | 14.9 触发 / 15.1 不触发 |
| RULE_WATERLOGGING | soil_moisture | `> 85` | 渍害 | 85.1 触发 / 84.9 不触发 |
| RULE_GALE | wind_speed | `> 17` | 大风 | 17.1 触发 / 16.9 不触发 |
| RULE_PEST | pest_index | `> 80` | 虫害 | 80.1 触发 / 79.9 不触发 |

执行契约：① 仅评估本次 tick 新读数；② 命中写 `alert_record`，`(sensor_code, rule_code,
trig_time)` 唯一键幂等（重复 tick/重放不产生重复告警，INSERT IGNORE 计入实际新增）；
③ 一条读数命中多条规则 → 多条告警；④ 写入后失效统计缓存前缀 `hsf:stat:alerts:`。
种子另含每条规则各 1 条固定触发时刻的告警（`uk_rule_reading` 幂等），满足 04 号 §9.1
"告警 ≥4 条且 4 规则各至少 1"。

### 5. 告警生命周期（FR-504/FR-505）

存储三态 `OPEN → PROCESSING → CLOSED`（处置人/时间/说明留痕列齐备）；一期最低验收口径为
**可查询、可统计**，状态推进接口 `POST /sim/alerts/{id}/transition` 冻结为二期 P2（A-05-2）。

## 端点（05 号 §7.1 冻结；另有任务口径兼容别名）

| 方法/路径 | 说明 | 鉴权 |
|---|---|---|
| `POST /sim/tick?steps=N` | 生成 N 步读数并评估告警 | Bearer JWT（任意角色） |
| `GET /sim/sensors?villageCode=&metric=&page=&size=` | 设备清单（total/page/size/items） | Bearer JWT |
| `GET /sim/latest?villageCode=` | 各设备最新值（快路径读 `sensor.last_*`，缓存键 10，60s 自然过期） | Bearer JWT |
| `GET /sim/alerts?state=&villageCode=&page=&size=` | 告警分页（state∈OPEN/PROCESSING/CLOSED） | Bearer JWT |
| `GET /sim/alerts/statistics?dim=state\|rule\|village&villageCode=` | 告警统计（缓存键 7，300s） | Bearer JWT |
| `GET /alerts`、`GET /alerts/statistics`、`GET /sensor/latest` | 任务文本口径兼容别名，行为同上 | Bearer JWT |
| `GET /healthz` `GET /readyz` `GET /metrics` | 探针与指标（hsf_common.create_app 提供） | 公开 |

鉴权：`Authorization: Bearer <JWT>`（与网关同一 JWT_SECRET，HS256；缺失/无效 → 401
`{"detail":{"code":"UNAUTHENTICATED","message":…}}`）。CORS 允许 web 源（env `CORS_ORIGINS`，默认 `*`）。

## 缓存键（04 号 §8.2）

| 键 | TTL | 失效时机 |
|---|---|---|
| `hsf:sim:latest:{villageCode}` | 60s（L1 15s） | tick 后**不**主动失效（最新值允许秒级陈旧） |
| `hsf:stat:alerts:{dim}:{scope}` | 300s（L1 60s） | 新告警写入后 `invalidate_prefix("hsf:stat:alerts:")` |

## 领域事件（ADR-HSF-004 Outbox）

`hsf.sim.device.registered`（种子）、`hsf.sim.reading.batched`（每次 tick 1 行）、
`hsf.sim.alert.raised`（有新增告警时）；`hsf_biz.domain_event.published_at` 一期恒 NULL。

## 与真实 IoT 的替换路径（二期）

仅换"数据入口"：把 `POST /sim/tick` 的生成器（`rules.next_value`）替换为设备网关接入，
`hsf_biz.sensor` 改由注册流程维护；规则引擎、`hsf_ts` 写入、告警与统计路径全部不变。

## 本地测试

```bash
PYTHONPATH=services/sim python -m pytest services/sim/tests -q   # 规则引擎/生成器单测（纯函数，无需 DB）
```
