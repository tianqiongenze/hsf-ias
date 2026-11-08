# iot — IoT 接入与时序/智慧生产服务（:8097，REST 非 GraphQL，二期新增）

对齐：`hsf/docs/07`（§2 MQTT 接入、§3 TimescaleDB、§4 iot 服务与智慧生产，全部冻结）、
`hsf/docs/05 §2.4`（数据范围矩阵）、`hsf/docs/04 §4.6/4.7/§5.1`（DDL/量程冻结）、
`CONVENTIONS-P2 T-2`。基础镜像 `10.167.2.175:30500/szxc/base:1.2` + 二期增量
`psycopg2-binary==2.9.10` / `paho-mqtt==2.1.0`（requirements.txt）。NodePort **31213**。

**职责边界（07 号 §2.5）**：sim 负责"造数与发数"（MQTT 发布），iot 负责"收数、判数、
决策与落库"，broker 只管可靠转发——iot 是 **TimescaleDB 唯一写入方**、**sensor.last_*
唯一维护方**、**alert_rule 种子唯一来源**；TiDB hsf_ts 二期零写入（归档快照）。

## 数据管道（07 号 §4.1 冻结）

```
hsf-mqtt:1883 ──订阅 hsf/sensor/+/+（QoS1，clean_session=false，client_id=hsf-iot-0，
                重连 1s→30s 退避）──► MQTT 回调线程（只解析校验+入缓冲，不碰 DB）
                                      │ 进程内缓冲：500 条或 2s 先到者刷
                                      ▼
                     flush（单线程串行，副本固定 1、uvicorn workers=1）：
                       ① execute_values 500/页 INSERT … ON CONFLICT DO NOTHING
                          → hsf_ts.sensor_reading（uk_reading 幂等承接 QoS1 重复，D-07-3）
                       ② 更新进程内 latest 缓存（A-07-3）
                       ③ UPDATE hsf_biz.sensor.last_*（读优化冗余，仅 iot 写）
                       ④ 规则引擎（回差）→ alert_record + notify + 失效统计缓存
                       ⑤ 灌溉决策 → irrigation_advice（6h 窗口幂等）+ notify
```

消息四重校验（07 号 §2.2）：topic↔payload 一致性 → metric 冻结命名（04 §4.7 六类）→
值域复核（越界丢弃）→ 台账准入（未知 device 丢弃 + `maybe_refresh` 节流刷新镜像，不做
动态注册）。无效消息按原因计数 `hsf_iot_mqtt_invalid_total{reason}`。

## 规则引擎（07 号 §4.2：一期 4 条阈值 + 二期滞后回差 HYST）

| rule_code | metric | 进入（=一期冻结） | 退出（回差） | env |
|---|---|---|---|---|
| RULE_DROUGHT | soil_moisture | `< 15`（旱情） | `≥ 20` | HYST_PCT 缺省 5 |
| RULE_WATERLOGGING | soil_moisture | `> 85`（渍害） | `≤ 80` | 同上 |
| RULE_GALE | wind_speed | `> 17`（大风） | `≤ 14` | 表值 14=17−3 |
| RULE_PEST | pest_index | `> 80`（虫害） | `≤ 75` | 同上 |

- 状态机：每 `(device_code, rule_code)` 一个槽位；NORMAL→TRIPPED 仅此刻写
  `alert_record`（`uk_rule_reading` 幂等兜底）；TRIPPED→NORMAL 不改告警 state
  （闭环仍人工，A-05-2 三期评估）——滞回带内读数抖动不产生重复告警。
- **重启重建**：启动时对每 `(device, rule)` 查 alert_record 最近 1 条 state≠CLOSED
  → 置 TRIPPED，防重启后同一持续异常立刻重复告警。
- 写告警后动作（承一期 05 §7.2④）：失效 `hsf:stat:alerts:*` → notify 4 行 → domain_event。

## 灌溉决策（07 号 §4.3/4.4 冻结）

- 触发：墒情 `< IRR_START_PCT(40)` 且该地块当前 6h 窗口无建议 → 生成 OPEN 建议；
  恢复：`≥ IRR_STOP_PCT(55)` → OPEN 建议自动闭环（`close_reason=AUTO_RECOVER`）。
- 用水量公式：`M（m³/亩）= 667 × h × γ × (θ_fc − θ_now)/100`（缺省 h=0.30m、γ=1.40、
  θ_fc=55%），示例 θ_now=38 → ≈47.6 m³/亩；`Total = M × area_mu`（面积缺失 NULL）。
- 幂等：`dedup_key = {parcel_code}:{窗口起点}`（本地 0/6/12/18 点对齐），
  `uk_advice_dedup` + INSERT IGNORE；同窗口重复极端读数行数不变。
- 参数 env：`IRR_START_PCT/IRR_STOP_PCT/IRR_FC_PCT/IRR_DEPTH_M/IRR_SOIL_GAMMA/IRR_DEDUP_HOURS`。
- 输出为"决策参考值"，mobile 卡片标注"建议值，请结合实际修正"。

## 通知 notify（07 号 §4.5 冻结）

每条新告警/新建议 → 4 行（admin/town/village/keeper，`uk_notify_dedup(dedup_key,user_role)`
INSERT IGNORE 幂等），全部行 `village_code`=事件村快照；标题/正文 iot 中文模板生成；
coop 不写行（推送面 4 角色，查询面不变）。读侧角色恒取 JWT（不接受入参 role），
行级角色过滤与村范围过滤叠加：admin 全县 / town 乡内村集 / village 本村 /
keeper 所辖设施所在村集 / coop 空集（05 §2.4）。

## 端点（07 号 §4.6 冻结）

| 方法/路径 | 说明 | 鉴权 |
|---|---|---|
| `GET /devices?villageCode=&metric=&page=&size=` | 设备台账（数据源 hsf_biz.sensor，字段口径同 05 §7.1 端点 2） | Bearer JWT（村/乡/县范围过滤） |
| `GET /sensor/latest?villageCode=` | 各设备最新值（进程内 latest 缓存；冷启动回源 TimescaleDB DISTINCT ON） | Bearer JWT |
| `GET /alerts?state=&villageCode=&page=&size=` | 告警分页（契约同 05 §7.1 端点 4） | Bearer JWT（范围过滤） |
| `GET /irrigation-advice?state=&parcelCode=&villageCode=&page=&size=` | 灌溉建议分页 | Bearer JWT（范围过滤） |
| `GET /notify?unreadOnly=&page=&size=` | 通知列表（role 取自 JWT，防越权拉取） | Bearer JWT |
| `POST /notify/{id}/read` | 置 read_at=NOW()；重复调用幂等 no-op；行.user_role ≠ token 角色 → 403 | Bearer JWT |
| `GET /healthz` `GET /readyz` `GET /metrics` | 探针与指标（hsf_common 提供；**readyz 校验 TiDB+Timescale+MQTT 三连接**） | 公开 |

鉴权：`Authorization: Bearer <JWT>`（与网关同一 JWT_SECRET，HS256；缺失/无效 → 401
`{"detail":{"code":"UNAUTHENTICATED",…}}`）；错误码 HTTP 映射同 05 §2.1
（FORBIDDEN 403 / NOT_FOUND 404）。

## env

| 键 | 缺省 | 说明 |
|---|---|---|
| `MQTT_HOST/PORT/USER/PASS` | hsf-mqtt/1883/hsf/— | broker 连接（MQTT_PASS 入 hsf-secrets） |
| `TIMESCALE_DSN` | postgresql://hsf:hsf@localhost:5432/hsf_ts | manifest 注入 `postgresql://hsf:{TIMESCALE_PASS}@hsf-timescale:5432/hsf_ts` |
| `DB_HOST_WRITE/READ、DB_USER(_RO)/DB_PASS(_RO)` | tidb:4000 | TiDB 双池（hsf_common.db） |
| `HYST_PCT` | 5 | 回差统一覆盖（缺省=冻结表值 20/80/14/75） |
| `IRR_*` | 见上 | 灌溉决策参数（缺省冻结值） |
| `JWT_SECRET` | hsf-secrets 注入 | 与 gw 同源 |

## 指标（07 号 §4.1 冻结命名）

`hsf_iot_mqtt_received_total` / `hsf_iot_mqtt_invalid_total{reason}` /
`hsf_iot_readings_written_total` / `hsf_iot_onconflict_skipped_total`（QoS1 幂等跳过）/
`hsf_iot_flush_seconds`（Histogram）/ `hsf_iot_rule_tripped_total{rule_code}` /
`hsf_iot_advice_created_total`；HTTP 指标 `hsf_http_*` 由 hsf_common.web 提供。
/metrics 输出 `Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)`（CONVENTIONS §2.8）。

## 本地测试

```bash
PYTHONPATH=services/iot python -m pytest services/iot/tests -q
# 34 例：回差状态机边界值/重启重建/HYST 覆盖、决策公式/阈值/6h 去重键/自动闭环、
#        MQTT 四重校验——全部纯函数+dict 桩，不依赖 DB/broker
```

## 部署事实（对接 manifest/init-ts，D-07-8）

- TimescaleDB 表/连续聚合/策略由 `scripts/init-ts.sh` 初始化；iot 只写数据不做 DDL。
- TiDB 侧 iot 启动幂等建 `irrigation_advice`/`notify`（存量表零 DDL 变更、零迁移），
  并幂等建 `alert_record`/`alert_rule`/`domain_event`（IF NOT EXISTS，承接一期 DDL——
  一期由 sim 建；二期落库方移交 iot，`alert_record`/`alert_rule` 种子与写入同步移交）。
- 副本固定 1、uvicorn workers=1、禁 HPA（latest 缓存/回差状态机/缓冲为进程内状态）。
- 资源口径（07 号 §1.3）：requests cpu 250m / mem 384Mi，limits cpu 2 / mem 1Gi，
  PriorityClass hsf-normal，podAntiAffinity preferred，Service 带 `metrics:"true"` label
  （CONVENTIONS §2.16）；NodePort 31213（上线前 `ss -tln` 核验，T-3）。
- 建议容器 env `TZ=Asia/Shanghai`（TiDB DATETIME 列以本地 naive 写入，A-04-3 全链路口径）。
