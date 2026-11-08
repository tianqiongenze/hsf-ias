"""sim 持久化（二期：仅"造数与发数"——docs/07 §2.5 职责边界冻结）：
模拟设备台账（hsf_biz.sensor，启动幂等 seed 48 台）+ 读数生成。

二期移交说明（07 号 §2.5）：
- 时序写入（TiDB hsf_ts → 二期 TimescaleDB）→ iot 服务（唯一写入方）；
- sensor.last_* 冗余列维护 → iot（flush 后 UPDATE）；
- 规则引擎/告警（alert_record 写入 + alert_rule 种子 + 种子告警）→ iot（回差状态机）；
- 本模块只保留设备台账 seed、读数生成与既有查询端点（05 §7.1 一期冻结契约）。
"""
import datetime as dt

from hsf_common import db
from hsf_common.cache import cached

from . import mqtt_pub, rules

TICK_MAX_STEPS = 5000    # 单请求步数上限（防误用；压测经并发达成批量发布）

SEED_VILLAGES = (
    ("53060010101", ("L5306000001", "L5306000002")),
    ("53060010102", ("L5306000003", "L5306000004")),
    ("53060010103", ("L5306000005", "L5306000006")),
    ("53060010201", ("L5306000007", "L5306000008")),
)
SEED_SENSORS_PER_METRIC = 2   # 6 类 × 2 台/村 × 4 村 = 48 台（FR-501）

# ── DDL（04 号冻结；仅保留 sim 仍职责内的表 + TiDB 归档口径表）─────────────────────
# sensor_reading（TiDB hsf_ts）：二期 sim 零写入（归档快照，07 号 §3.7），IF NOT EXISTS
# 仅为全新环境保留归档口径表体（V2-06 校验 TiDB MAX(recorded_at) 不再增长），时序主载体
# = TimescaleDB hsf_ts.sensor_reading hypertable（scripts/init-ts.sh 初始化）。
DDL = [
    """CREATE TABLE IF NOT EXISTS hsf_ts.sensor_reading(
  id          BIGINT        NOT NULL AUTO_RANDOM(5),
  sensor_code CHAR(20)      NOT NULL,
  metric      VARCHAR(16)   NOT NULL,
  value       DECIMAL(10,3) NOT NULL COMMENT '观测值（value 非 SQL 保留字，CONVENTIONS §2.2 已复核）',
  recorded_at DATETIME(3)   NOT NULL COMMENT '毫秒精度观测时间（二期起本表零写入，归档快照）',
  PRIMARY KEY (id),
  KEY idx_code_time (sensor_code, recorded_at)
) PRE_SPLIT_REGIONS=4
  COMMENT='传感器时序（一期归档快照；二期写入路径=TimescaleDB hypertable，07 号 §3.2）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.sensor(
  id               BIGINT        NOT NULL AUTO_INCREMENT,
  sensor_code      CHAR(20)      NOT NULL COMMENT 'S+6位区划+4位序号（种子 48 台）',
  metric           VARCHAR(16)   NOT NULL COMMENT 'soil_moisture/air_temp/wind_speed/rainfall/pest_index/water_level',
  village_code     CHAR(16)      NOT NULL COMMENT '所属村（列宽依 CONVENTIONS §2.3 预留，实长 11 位）',
  parcel_code      CHAR(20)      NULL COMMENT '绑定地块',
  status           VARCHAR(16)   NOT NULL DEFAULT 'ACTIVE' COMMENT 'ACTIVE/OFFLINE',
  `last_value`       DECIMAL(10,3) NULL COMMENT '最新观测值（读优化受控冗余；二期起由 iot 维护，07 号 §2.5）',
  `last_recorded_at` DATETIME(3)   NULL COMMENT '最新观测时间（二期起由 iot 维护）',
  created_at       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME      NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_sensor_code (sensor_code),
  KEY idx_village_metric (village_code, metric)
) COMMENT='设备台账（FR-501/502；二期 sim 启动 seed，iot 只读为准入台账/告警维度源）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.domain_event(
  id           BIGINT      NOT NULL AUTO_INCREMENT,
  topic        VARCHAR(64) NOT NULL COMMENT '事件主题 hsf.<ctx>.<entity>.<verb-past>（ADR-HSF-004）',
  payload      JSON        NOT NULL COMMENT '事件载荷 JSON',
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  published_at DATETIME    NULL COMMENT '二期投递器仍未引入：恒 NULL（MQTT 仅承载设备数据面）',
  PRIMARY KEY (id),
  KEY idx_topic_time (topic, created_at)
) COMMENT='领域事件 Outbox（写路径追加 1 行，ADR-HSF-004）'""",
]


def init_db():
    for ddl in DDL:
        db.execute(ddl)


def _emit(topic: str, payload: dict):
    import json
    db.execute("INSERT INTO hsf_biz.domain_event(topic,payload) VALUES(%s,%s)",
               (topic, json.dumps(payload, ensure_ascii=False, default=str)))


# ── 种子（04 号 §9.1：传感器 48 台；二期仅设备台账——alert_rule/种子告警移交 iot）────

def seed_if_empty():
    _seed_sensors()


def _seed_sensors():
    """48 台 = 6 类 metric × 2 台/村 × 4 村；INSERT IGNORE（uk_sensor_code）双守卫。"""
    if db.query("SELECT COUNT(*) c FROM hsf_biz.sensor", one=True)["c"] >= \
            len(SEED_VILLAGES) * len(rules.METRIC_RANGES) * SEED_SENSORS_PER_METRIC:
        return
    rows, seq = [], 1
    for village_code, parcels in SEED_VILLAGES:
        for metric in rules.metrics():
            for i in range(SEED_SENSORS_PER_METRIC):
                rows.append((f"S530600{seq:04d}", metric, village_code,
                             parcels[i % len(parcels)], "ACTIVE"))
                seq += 1
    n = db.execute_many(
        "INSERT IGNORE INTO hsf_biz.sensor(sensor_code,metric,village_code,parcel_code,status) "
        "VALUES(%s,%s,%s,%s,%s)", rows)
    if n:
        _emit("hsf.sim.device.registered", {"count": int(n)})


# ── 读数生成（05 号 §7.3 写入契约的二期形态：只生成，写入经 MQTT 由 iot 落库）────────

def active_sensors() -> list[dict]:
    return db.query("SELECT sensor_code, metric, village_code, parcel_code "
                    "FROM hsf_biz.sensor WHERE status='ACTIVE'")


def generate_readings(steps: int = 1, sensors: list[dict] | None = None) -> list[tuple]:
    """steps 轮 × ACTIVE 设备 → 读数元组列表 (device_code, metric, value, recorded_at,
    village_code)。recorded_at 步进 1ms（一期同法），保证 iot 侧幂等键可区分。"""
    steps = max(1, min(int(steps or 1), TICK_MAX_STEPS))
    sensors = sensors if sensors is not None else active_sensors()
    base = mqtt_pub.now_local()
    readings: list[tuple] = []
    for i in range(steps):
        ts = base + dt.timedelta(milliseconds=i)
        for s in sensors:
            readings.append((s["sensor_code"], s["metric"], rules.next_value(s["metric"]),
                             ts, s["village_code"]))
    return readings


def heartbeat_readings() -> list[tuple]:
    """心跳一轮（MQTT_INTERVAL 间隔由发布器控制）：每 ACTIVE 设备 1 条，48 条/轮。"""
    sensors = active_sensors()
    if not sensors:
        return []
    ts = mqtt_pub.now_local()
    return [(s["sensor_code"], s["metric"], rules.next_value(s["metric"]), ts, s["village_code"])
            for s in sensors]


# ── 查询（05 号 §7.1 一期冻结端点；告警数据源二期起为 iot 写入的 alert_record）───────

def _f(x):
    """DECIMAL → float（JSON 可序列化）。"""
    return float(x) if x is not None else None


def sensors_list(village_code: str | None, metric: str | None, page: int, size: int) -> dict:
    where, args = "WHERE 1=1", []
    if village_code:
        where += " AND village_code=%s"; args.append(village_code)
    if metric:
        where += " AND metric=%s"; args.append(metric)
    args = tuple(args)
    total = int(db.query(f"SELECT COUNT(*) c FROM hsf_biz.sensor {where}", args, one=True)["c"])
    rows = db.query(f"SELECT * FROM hsf_biz.sensor {where} ORDER BY sensor_code LIMIT %s OFFSET %s",
                    args + (size, (max(page, 1) - 1) * size))
    items = [{"sensorCode": r["sensor_code"], "metric": r["metric"],
              "villageCode": r["village_code"], "parcelCode": r["parcel_code"],
              "status": r["status"], "lastValue": _f(r["last_value"]),
              "lastRecordedAt": str(r["last_recorded_at"]) if r["last_recorded_at"] else None}
             for r in rows]
    return {"total": total, "page": max(page, 1), "size": size, "items": items}


def latest(village_code: str | None) -> dict:
    """快路径直读 sensor.last_*（键 10：hsf:sim:latest:{villageCode}，60s 自然过期不主动失效；
    last_* 二期起由 iot flush 维护，本端点读法不变）。"""
    key = f"hsf:sim:latest:{village_code or 'all'}"
    items = cached(key, 60, lambda: _load_latest(village_code), l1_ttl=15)
    return {"items": items}


def _load_latest(village_code: str | None) -> list[dict]:
    where, args = ("WHERE village_code=%s", [village_code]) if village_code else ("", [])
    rows = db.query(f"SELECT sensor_code, metric, `last_value`, `last_recorded_at` "
                    f"FROM hsf_biz.sensor {where} ORDER BY sensor_code", tuple(args))
    return [{"sensorCode": r["sensor_code"], "metric": r["metric"], "value": _f(r["last_value"]),
             "recordedAt": str(r["last_recorded_at"]) if r["last_recorded_at"] else None}
            for r in rows]


def alerts_list(state: str | None, village_code: str | None, page: int, size: int) -> dict:
    """告警分页（一期冻结契约保留为只读查询；写入方二期为 iot 回差状态机）。"""
    where, args = "WHERE 1=1", []
    if state:
        where += " AND state=%s"; args.append(state)
    if village_code:
        where += " AND village_code=%s"; args.append(village_code)
    args = tuple(args)
    total = int(db.query(f"SELECT COUNT(*) c FROM hsf_biz.alert_record {where}", args, one=True)["c"])
    rows = db.query(f"SELECT * FROM hsf_biz.alert_record {where} ORDER BY id DESC LIMIT %s OFFSET %s",
                    args + (size, (max(page, 1) - 1) * size))
    items = [{"id": r["id"], "sensorCode": r["sensor_code"], "ruleCode": r["rule_code"],
              "metric": r["metric"], "trigValue": _f(r["trig_value"]),
              "trigTime": str(r["trig_time"]), "villageCode": r["village_code"],
              "parcelCode": r["parcel_code"], "state": r["state"],
              "handledBy": r["handled_by"], "handledAt": str(r["handled_at"]) if r["handled_at"] else None,
              "closedAt": str(r["closed_at"]) if r["closed_at"] else None, "note": r["note"]}
             for r in rows]
    return {"total": total, "page": max(page, 1), "size": size, "items": items}


STAT_DIMS = {"state": "state", "rule": "rule_code", "village": "village_code"}


def statistics(dim: str, village_code: str | None) -> dict:
    """按状态/规则/村聚合（键 7：hsf:stat:alerts:{dim}:{scope}，300s）。"""
    if dim not in STAT_DIMS:
        raise ValueError(f"dim 仅支持 state|rule|village：{dim}")
    col = STAT_DIMS[dim]
    scope = village_code or "all"
    items = cached(f"hsf:stat:alerts:{dim}:{scope}", 300,
                   lambda: _load_statistics(dim, col, village_code), l1_ttl=60)
    return {"dim": dim, "items": items}


def _load_statistics(dim: str, col: str, village_code: str | None) -> list[dict]:
    where, args = ("WHERE village_code=%s", [village_code]) if village_code else ("", [])
    rows = db.query(f"SELECT {col} k, COUNT(*) c FROM hsf_biz.alert_record {where} "
                    f"GROUP BY {col} ORDER BY c DESC", tuple(args))
    return [{"key": r["k"], "count": int(r["c"])} for r in rows]
