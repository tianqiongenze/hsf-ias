"""sim 持久化：模拟设备台账（hsf_biz.sensor）、时序批量写（hsf_ts.sensor_reading）、
阈值告警（hsf_biz.alert_record/alert_rule，uk_rule_reading 幂等）。

对齐：hsf/docs/04 §4.6/4.7/4.10/§5.1（DDL 冻结）、docs/05 §7.1~7.3（端点与写入契约）。
"""
import datetime as dt

from hsf_common import db
from hsf_common.cache import cached, invalidate_prefix

from . import rules

BATCH_SIZE = 1000        # 时序批量写 1000 条/批（CONVENTIONS §2.13 / NFR-04）
TICK_MAX_STEPS = 5000    # 单请求步数上限（防误用；压测经并发达成 ≥1万条/s）

SEED_VILLAGES = (
    ("53060010101", ("L5306000001", "L5306000002")),
    ("53060010102", ("L5306000003", "L5306000004")),
    ("53060010103", ("L5306000005", "L5306000006")),
    ("53060010201", ("L5306000007", "L5306000008")),
)
SEED_SENSORS_PER_METRIC = 2   # 6 类 × 2 台/村 × 4 村 = 48 台（FR-501）
SEED_ALERT_TRIG_TIME = dt.datetime(2026, 1, 1, 9, 0, 0)  # 种子告警固定触发时刻（幂等键一部分）

# ── DDL（04 号冻结）─────────────────────────────────────────────────────────
DDL = [
    # 时序表（04 号 §5.1：显式偏离 §2.13 已声明——AUTO_RANDOM 打散写热点 + 预分裂）
    """CREATE TABLE IF NOT EXISTS hsf_ts.sensor_reading(
  id          BIGINT        NOT NULL AUTO_RANDOM(5),
  sensor_code CHAR(20)      NOT NULL,
  metric      VARCHAR(16)   NOT NULL,
  value       DECIMAL(10,3) NOT NULL COMMENT '观测值（value 非 SQL 保留字，CONVENTIONS §2.2 已复核）',
  recorded_at DATETIME(3)   NOT NULL COMMENT '毫秒精度观测时间',
  PRIMARY KEY (id),
  KEY idx_code_time (sensor_code, recorded_at)
) PRE_SPLIT_REGIONS=4
  COMMENT='传感器时序（只插入；批量 INSERT 1000 条/批，FR-502/NFR-04）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.sensor(
  id               BIGINT        NOT NULL AUTO_INCREMENT,
  sensor_code      CHAR(20)      NOT NULL COMMENT 'S+6位区划+4位序号（种子 48 台）',
  metric           VARCHAR(16)   NOT NULL COMMENT 'soil_moisture/air_temp/wind_speed/rainfall/pest_index/water_level',
  village_code     CHAR(16)      NOT NULL COMMENT '所属村（列宽依 CONVENTIONS §2.3 预留，实长 11 位）',
  parcel_code      CHAR(20)      NULL COMMENT '绑定地块',
  status           VARCHAR(16)   NOT NULL DEFAULT 'ACTIVE' COMMENT 'ACTIVE/OFFLINE',
  `last_value`       DECIMAL(10,3) NULL COMMENT '最新观测值（tick 直写，latest 快路径；TiDB 保留字必反引号 CONVENTIONS §2.2）',
  `last_recorded_at` DATETIME(3)   NULL COMMENT '最新观测时间',
  created_at       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME      NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_sensor_code (sensor_code),
  KEY idx_village_metric (village_code, metric)
) COMMENT='模拟设备注册（FR-501/502；last_* 为读优化的受控冗余，仅 sim 写）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.alert_record(
  id           BIGINT        NOT NULL AUTO_INCREMENT,
  sensor_code  CHAR(20)      NOT NULL COMMENT '触发传感器',
  rule_code    VARCHAR(16)   NOT NULL COMMENT 'RULE_DROUGHT/RULE_WATERLOGGING/RULE_GALE/RULE_PEST',
  metric       VARCHAR(16)   NOT NULL COMMENT '触发指标（冗余存储，免联 sensor 表）',
  trig_value   DECIMAL(10,3) NOT NULL COMMENT '触发时观测值',
  trig_time    DATETIME(3)   NOT NULL COMMENT '触发读数的 recorded_at（幂等键组成部分）',
  village_code CHAR(16)      NOT NULL COMMENT '传感器所在村（冗余，范围过滤/统计免联）',
  parcel_code  CHAR(20)      NULL COMMENT '传感器绑定地块',
  state        VARCHAR(16)   NOT NULL DEFAULT 'OPEN'
    COMMENT 'OPEN待处置/PROCESSING处置中/CLOSED已闭环（FR-504 三态）',
  handled_by   BIGINT        NULL COMMENT '处置人 party.id',
  handled_at   DATETIME      NULL COMMENT '进入处置中时间',
  closed_at    DATETIME      NULL COMMENT '闭环时间',
  note         VARCHAR(255)  NULL COMMENT '处置说明',
  created_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_rule_reading (sensor_code, rule_code, trig_time),
  KEY idx_sensor_time (sensor_code, trig_time),
  KEY idx_village_time (village_code, trig_time),
  KEY idx_state (state, trig_time)
) COMMENT='阈值告警：同一读数+同一规则唯一（uk_rule_reading 幂等，FR-503~505）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.alert_rule(
  id         BIGINT        NOT NULL AUTO_INCREMENT,
  rule_code  VARCHAR(32)   NOT NULL COMMENT 'RULE_DROUGHT/RULE_WATERLOGGING/RULE_GALE/RULE_PEST',
  metric     VARCHAR(16)   NOT NULL COMMENT 'soil_moisture/wind_speed/pest_index',
  op         VARCHAR(4)    NOT NULL COMMENT '< 或 >',
  threshold  DECIMAL(10,3) NOT NULL COMMENT '15/85/17/80（FR-503 边界值单测对象）',
  enabled    TINYINT(1)    NOT NULL DEFAULT 1,
  created_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_rule_code (rule_code)
) COMMENT='阈值规则（种子 4 条，FR-503）；告警类型语义由 rule_code 承载'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.domain_event(
  id           BIGINT      NOT NULL AUTO_INCREMENT,
  topic        VARCHAR(64) NOT NULL COMMENT '事件主题 hsf.<ctx>.<entity>.<verb-past>（ADR-HSF-004）',
  payload      JSON        NOT NULL COMMENT '事件载荷 JSON',
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  published_at DATETIME    NULL COMMENT '二期投递器置位；一期恒 NULL（一期无 broker）',
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


# ── 种子（04 号 §9.1：传感器 48 台；告警 ≥4 条且 4 规则各至少 1）────────────────

def seed_if_empty():
    _seed_rules()
    _seed_sensors()
    _seed_alerts()


def _seed_rules():
    """4 条阈值规则（uk_rule_code 幂等；规则引擎代码化形态同步于 app/rules.py）。"""
    db.execute_many(
        "INSERT IGNORE INTO hsf_biz.alert_rule(rule_code,metric,op,threshold) VALUES(%s,%s,%s,%s)",
        [(r["rule_code"], r["metric"], r["op"], r["threshold"]) for r in rules.RULES])


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
    db.execute_many(
        "INSERT IGNORE INTO hsf_biz.sensor(sensor_code,metric,village_code,parcel_code,status) "
        "VALUES(%s,%s,%s,%s,%s)", rows)


def _seed_alerts():
    """每条规则各 1 条种子告警（INSERT IGNORE，uk_rule_reading 幂等）。"""
    probes = {"RULE_DROUGHT": 12.4, "RULE_WATERLOGGING": 91.2,
              "RULE_GALE": 19.6, "RULE_PEST": 86.3}
    rows = []
    for r in rules.RULES:
        s = db.query("SELECT sensor_code, village_code, parcel_code FROM hsf_biz.sensor "
                     "WHERE metric=%s ORDER BY sensor_code LIMIT 1", (r["metric"],), one=True)
        if not s:
            continue  # 设备未就绪（极端并发场景），下次启动重试
        rows.append((s["sensor_code"], r["rule_code"], r["metric"], probes[r["rule_code"]],
                     SEED_ALERT_TRIG_TIME, s["village_code"], s["parcel_code"]))
    if rows:
        _insert_alerts(rows)


def _insert_alerts(rows: list[tuple]) -> int:
    """INSERT IGNORE：同读数+同规则重复评估不产生重复告警（04 号 §4.6 幂等说明）。"""
    if not rows:
        return 0
    n = db.execute_many(
        "INSERT IGNORE INTO hsf_biz.alert_record(sensor_code,rule_code,metric,trig_value,"
        "trig_time,village_code,parcel_code,state) VALUES(%s,%s,%s,%s,%s,%s,%s,'OPEN')", rows)
    if n:
        invalidate_prefix("hsf:stat:alerts:")  # 键 7/8 失效（05 号 §7.2 契约④）
        _emit("hsf.sim.alert.raised", {"count": int(n)})
    return int(n or 0)


# ── tick（05 号 §7.3 写入契约）──────────────────────────────────────────────

def tick(steps: int = 1) -> dict:
    """steps 轮 × 48 台 → 时序批量写 1000/批 → 更新 last_* → 规则引擎评估新读数。"""
    steps = max(1, min(int(steps or 1), TICK_MAX_STEPS))
    sensors = db.query("SELECT sensor_code, metric, village_code, parcel_code "
                       "FROM hsf_biz.sensor WHERE status='ACTIVE'")
    if not sensors:
        return {"inserted": 0, "batches": 0, "alerts_created": 0}
    base = dt.datetime.now()
    readings: list[tuple] = []              # (sensor_code, metric, value, recorded_at)
    last_per_sensor: dict[str, tuple] = {}  # sensor_code -> (value, recorded_at)
    for i in range(steps):
        ts = base + dt.timedelta(milliseconds=i)  # 步进 1ms，保证 trig_time 可区分
        for s in sensors:
            v = rules.next_value(s["metric"])
            readings.append((s["sensor_code"], s["metric"], v, ts))
            last_per_sensor[s["sensor_code"]] = (v, ts)
    inserted = batches = 0
    for i in range(0, len(readings), BATCH_SIZE):
        chunk = readings[i:i + BATCH_SIZE]
        inserted += max(db.execute_many(
            "INSERT INTO hsf_ts.sensor_reading(sensor_code,metric,value,recorded_at) "
            "VALUES(%s,%s,%s,%s)", chunk), 0)
        batches += 1
    # last_* 读优化冗余（仅 sim 写；每台取最后一步的值）
    db.execute_many(
        "UPDATE hsf_biz.sensor SET `last_value`=%s, `last_recorded_at`=%s, updated_at=NOW() "
        "WHERE sensor_code=%s",
        [(v, ts, code) for code, (v, ts) in last_per_sensor.items()])
    # 规则引擎仅评估本次新读数（05 号 §7.2 契约①）；uk_rule_reading 幂等去重
    meta = {s["sensor_code"]: s for s in sensors}
    alert_rows = []
    for code, metric, v, ts in readings:
        for hit in rules.evaluate(metric, v):
            s = meta[code]
            alert_rows.append((code, hit["rule_code"], metric, v, ts,
                               s["village_code"], s["parcel_code"]))
    alerts_created = _insert_alerts(alert_rows)
    _emit("hsf.sim.reading.batched", {"inserted": inserted, "batches": batches})
    return {"inserted": int(inserted), "batches": int(batches), "alerts_created": alerts_created}


# ── 查询（05 号 §7.1 端点 2~5）─────────────────────────────────────────────

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
    """快路径直读 sensor.last_*（键 10：hsf:sim:latest:{villageCode}，60s 自然过期不主动失效）。"""
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
