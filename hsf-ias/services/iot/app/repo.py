"""iot 持久化与写入管道：TiDB（hsf_common.db 双池）+ TimescaleDB（app.tsdb）。

对齐：hsf/docs/07 §2.2（消息校验）、§2.5（职责边界：iot=收数/判数/决策/落库）、§4.1（缓冲
500/2s 刷 + 刷后四步）、§4.2（告警回差）、§4.3/4.4（灌溉决策与 DDL）、§4.5（notify 写入/
读取规则）、§4.6（REST 契约）；docs/04 §4.6/§5.1（alert_* DDL 冻结）、05 §2.4（数据范围矩阵）。

边界（docs/07 §2.5）：sim 只造数发数；iot 是 TimescaleDB 唯一写入方、sensor.last_* 唯一
维护方、alert_rule 种子唯一来源；TiDB hsf_ts 二期零写入（归档快照）。
"""
import datetime as dt
import json
import threading
import time

from hsf_common import db
from hsf_common.cache import invalidate_prefix

from . import decision, metrics, rules, tsdb

BATCH_MAX = 500          # 缓冲条数阈值（500 条或 2s 先到者刷，CONVENTIONS-P2 T-2）
FLUSH_INTERVAL = 2.0     # 缓冲时间阈值（s）
REGISTRY_REFRESH = 60.0  # 台账刷新周期（s）；未知设备触发即时刷新（10s 节流）
BUFFER_PUT_FRONT_MAX = 50000  # 刷写失败回队上限（防 TimescaleDB 长时间不可用时内存无界）

NOTIFY_ROLES = ("admin", "town", "village", "keeper")  # 推送面 4 角色，coop 不写行（D-07-6）

# ── DDL（冻结；CREATE TABLE IF NOT EXISTS 对存量表零变更——D-07-8）────────────────
# alert_record/alert_rule/domain_event：一期冻结 DDL 原样承接（一期 sim 建，二期 iot 落库方）
DDL = [
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
) COMMENT='阈值规则（二期 iot 种子 4 条，FR-503；回差退出阈值代码化于 app/rules.py）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.domain_event(
  id           BIGINT      NOT NULL AUTO_INCREMENT,
  topic        VARCHAR(64) NOT NULL COMMENT '事件主题 hsf.<ctx>.<entity>.<verb-past>（ADR-HSF-004）',
  payload      JSON        NOT NULL COMMENT '事件载荷 JSON',
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  published_at DATETIME    NULL COMMENT '二期投递器仍未引入：恒 NULL（MQTT 仅承载设备数据面，docs/07 §9-⑧）',
  PRIMARY KEY (id),
  KEY idx_topic_time (topic, created_at)
) COMMENT='领域事件 Outbox（写路径追加 1 行，ADR-HSF-004）'""",
    # irrigation_advice：docs/07 §4.4 冻结 DDL 原文
    """CREATE TABLE IF NOT EXISTS hsf_biz.irrigation_advice(
  id            BIGINT        NOT NULL AUTO_INCREMENT,
  parcel_code   CHAR(20)      NOT NULL COMMENT '触发地块（跨库逻辑引用 hsf_md.land_parcel，无外键 D-04-3）',
  sensor_code   CHAR(20)      NOT NULL COMMENT '触发墒情传感器',
  village_code  CHAR(16)      NOT NULL COMMENT '所在村（冗余，范围过滤免联）',
  trigger_value DECIMAL(10,3) NOT NULL COMMENT '触发时墒情（百分数，pymysql 占位符冲突故不写百分号字面量）',
  theta_fc      DECIMAL(5,1)  NOT NULL DEFAULT 55.0 COMMENT '决策参数快照（复盘可重算）',
  depth_m       DECIMAL(4,2)  NOT NULL DEFAULT 0.30,
  soil_gamma    DECIMAL(3,2)  NOT NULL DEFAULT 1.40,
  per_mu_m3     DECIMAL(10,1) NOT NULL COMMENT 'M=667×h×γ×(θfc−θnow)/100（07 号 §4.3 冻结公式）',
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
) COMMENT='灌溉建议（阈值 40（百分数）+回差 15+6h 去重；用水量公式冻结于 07 号 4.3 节）'""",
    # notify：docs/07 §4.5 冻结 DDL 原文
    """CREATE TABLE IF NOT EXISTS hsf_biz.notify(
  id           BIGINT       NOT NULL AUTO_INCREMENT,
  alert_id     BIGINT       NULL COMMENT '关联告警 alert_record.id（kind=ALERT 时非空）',
  advice_id    BIGINT       NULL COMMENT '关联灌溉建议 irrigation_advice.id（kind=ADVICE 时非空）',
  user_role    VARCHAR(16)  NOT NULL COMMENT '通知目标角色：admin/town/village/keeper（D-07-6）',
  village_code CHAR(16)     NOT NULL COMMENT '事件村快照（4 行同值写入；读侧按 05 §2.4 角色范围过滤）',
  kind         VARCHAR(16)  NOT NULL DEFAULT 'ALERT' COMMENT 'ALERT告警/ADVICE灌溉建议',
  title        VARCHAR(128) NOT NULL COMMENT '标题（中文）',
  content      VARCHAR(512) NOT NULL COMMENT '正文（含触发值/建议量等摘要）',
  read_at      DATETIME     NULL COMMENT '已读时间（NULL=未读；mobile 拉取后显式置位）',
  dedup_key    VARCHAR(64)  NOT NULL COMMENT 'alert:{id} 或 advice:{id}（事件幂等键）',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_notify_dedup (dedup_key, user_role),
  KEY idx_role_time (user_role, created_at),
  KEY idx_role_unread (user_role, village_code, read_at)
) COMMENT='站内通知（写侧=iot，读侧=mobile 拉取 API；已读=read_at 置位）'""",
]


def init_db():
    """TiDB 侧幂等建表（D-07-8：存量表零变更、零迁移；TimescaleDB 由 init-ts.sh 初始化）。"""
    for ddl in DDL:
        db.execute(ddl)


def seed_if_empty():
    """alert_rule 种子 4 条（uk_rule_code 幂等；规则引擎代码化形态同步于 app/rules.py）。"""
    db.execute_many(
        "INSERT IGNORE INTO hsf_biz.alert_rule(rule_code,metric,op,threshold) VALUES(%s,%s,%s,%s)",
        [(r["rule_code"], r["metric"], r["op"], r["threshold"]) for r in rules.RULES])


def _emit(topic: str, payload: dict):
    """Outbox 追加（ADR-HSF-004 延续：二期 MQTT 只承载设备数据面，事件仍走表不投递）。"""
    db.execute("INSERT INTO hsf_biz.domain_event(topic,payload) VALUES(%s,%s)",
               (topic, json.dumps(payload, ensure_ascii=False, default=str)))


def _naive(ts: dt.datetime) -> dt.datetime:
    """TiDB DATETIME 无时区：aware → 本地 naive（容器 TZ=Asia/Shanghai，A-04-3 全链路口径）。"""
    if ts.tzinfo is not None:
        return ts.astimezone().replace(tzinfo=None)
    return ts


def _f(x):
    """DECIMAL → float（JSON 可序列化）。"""
    return float(x) if x is not None else None


# ── 设备台账只读镜像（docs/07 §2.2：设备准入走台账，不做动态注册）────────────────

class SensorRegistry:
    """hsf_biz.sensor 只读镜像 + 冗余维度（村名/地块面积）。读多写少，定时全量刷新。"""

    def __init__(self, refresh_seconds: float = REGISTRY_REFRESH):
        self._refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._devices: dict[str, dict] = {}      # sensor_code → {metric, village_code, parcel_code}
        self._village_names: dict[str, str] = {} # village_code → org.name（通知文案）
        self._areas: dict[str, float | None] = {}# parcel_code → land_parcel.area_mu
        self._last_refresh = 0.0
        self._unknown_hit = 0.0                  # 未知设备触发即时刷新的节流戳

    def refresh(self) -> int:
        devices = {r["sensor_code"]: {"metric": r["metric"], "village_code": r["village_code"],
                                      "parcel_code": r["parcel_code"]}
                   for r in db.query("SELECT sensor_code, metric, village_code, parcel_code "
                                     "FROM hsf_biz.sensor")}
        names = {r["code"]: r["name"] for r in
                 db.query("SELECT code, name FROM hsf_md.org")}
        parcels = {d["parcel_code"] for d in devices.values() if d["parcel_code"]}
        areas: dict[str, float | None] = {}
        if parcels:
            marks = ",".join(["%s"] * len(parcels))
            for r in db.query(f"SELECT code, area_mu FROM hsf_md.land_parcel WHERE code IN ({marks})",
                              tuple(parcels)):
                areas[r["code"]] = _f(r["area_mu"])
        with self._lock:
            self._devices = devices
            self._village_names = names
            self._areas = areas
            self._last_refresh = time.monotonic()
        return len(devices)

    def maybe_refresh(self, min_gap: float = 10.0) -> None:
        """未知设备出现时的节流即时刷新（新设备注册后 ≤10s 内接入）。"""
        now = time.monotonic()
        if now - self._unknown_hit >= min_gap and now - self._last_refresh >= min_gap:
            self._unknown_hit = now
            try:
                self.refresh()
            except Exception:
                pass  # 刷新失败下轮定时重试；准入判定仍用当前镜像

    def get(self, sensor_code: str) -> dict | None:
        return self._devices.get(sensor_code)

    def all(self) -> dict[str, dict]:
        return self._devices

    def village_name(self, village_code: str) -> str:
        return self._village_names.get(village_code, "")

    def area_of(self, parcel_code: str) -> float | None:
        return self._areas.get((parcel_code or "").strip())


# ── 进程内 latest 缓存（A-07-3：每批 flush 后更新；冷启动回源 TimescaleDB DISTINCT ON）

class LatestCache:
    def __init__(self):
        self._d: dict[str, tuple[str, float, dt.datetime]] = {}  # device → (metric, value, ts)

    def put(self, device_code: str, metric: str, value: float, ts: dt.datetime):
        self._d[device_code] = (metric, float(value), ts)

    def items(self, registry: SensorRegistry, village_code: str | None = None) -> list[dict]:
        out = []
        for code in sorted(registry.all()):
            dev = registry.get(code)
            if village_code and dev and dev["village_code"] != village_code:
                continue
            hit = self._d.get(code)
            out.append({"sensorCode": code, "metric": dev["metric"] if dev else (hit[0] if hit else None),
                        "value": hit[1] if hit else None,
                        "recordedAt": hit[2].isoformat(timespec="milliseconds") if hit else None})
        return out

    def backfill(self) -> int:
        """冷启动回源：每设备取 TimescaleDB 最新 1 条（DISTINCT ON，docs/07 §3.2/A-07-3）。"""
        rows = tsdb.query(
            "SELECT DISTINCT ON (device_code) device_code, metric, value, recorded_at "
            "FROM hsf_ts.sensor_reading ORDER BY device_code, recorded_at DESC")
        for r in rows:
            self.put(r["device_code"], r["metric"], float(r["value"]), r["recorded_at"])
        return len(rows)


# ── 通知（docs/07 §4.5 写入规则：每事件 4 行，INSERT IGNORE uk_notify_dedup 幂等）────

def _notify_rows(alert_id, advice_id, village_code, kind, title, content, dedup_key) -> int:
    return max(db.execute_many(
        "INSERT IGNORE INTO hsf_biz.notify(alert_id,advice_id,user_role,village_code,kind,"
        "title,content,dedup_key) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        [(alert_id, advice_id, role, village_code, kind, title, content, dedup_key)
         for role in NOTIFY_ROLES]), 0)


def notify_for_alert(alert: dict, rule: dict, village_name: str) -> int:
    """新告警 → 4 行 notify（kind=ALERT，dedup_key=alert:{id}，中文模板）。"""
    where = "低于" if rule["op"] == "<" else "高于"
    vn = village_name or alert["village_code"]
    title = f"{vn}{rule['label']}告警"[:128]
    content = (f"传感器 {alert['sensor_code']} {rule['label']}指标 {alert['metric']}="
               f"{_f(alert['trig_value'])}，{where}阈值 {rule['threshold']}（{alert['rule_code']}），"
               f"请及时处置")[:512]
    return _notify_rows(alert["id"], None, alert["village_code"], "ALERT", title, content,
                        f"alert:{alert['id']}")


def notify_for_advice(advice: dict, params: dict, village_name: str) -> int:
    """新建议 → 4 行 notify（kind=ADVICE，dedup_key=advice:{id}，含建议量摘要）。"""
    vn = village_name or advice["village_code"]
    title = f"{vn}灌溉建议"[:128]
    total = advice.get("total_m3")
    content = (f"地块 {advice['parcel_code']} 墒情 {_f(advice['trigger_value'])}% 低于 "
               f"{params['start']}%，建议灌溉 {advice['per_mu_m3']} m³/亩"
               + (f"，合计约 {total} m³" if total is not None else "")
               + "（建议值，请结合实际修正）")[:512]
    return _notify_rows(None, advice["id"], advice["village_code"], "ADVICE", title, content,
                        f"advice:{advice['id']}")


# ── 告警写入（uk_rule_reading 幂等；承一期 05 §7.2④ 写后动作）────────────────────

RULE_BY_CODE = {r["rule_code"]: r for r in rules.RULES}


def insert_alerts(rows: list[tuple], registry: SensorRegistry) -> int:
    """批量 INSERT IGNORE（同读数+同规则幂等）；对每个事件补 4 行 notify + 失效统计缓存
    + domain_event（dedup_key 幂等，重复评估不重复通知）。返回实际新增行数。"""
    if not rows:
        return 0
    inserted = max(db.execute_many(
        "INSERT IGNORE INTO hsf_biz.alert_record(sensor_code,rule_code,metric,trig_value,"
        "trig_time,village_code,parcel_code,state) VALUES(%s,%s,%s,%s,%s,%s,%s,'OPEN')", rows), 0)
    for r in rows:
        hit = db.query("SELECT id, sensor_code, rule_code, metric, trig_value, trig_time, "
                       "village_code, parcel_code FROM hsf_biz.alert_record "
                       "WHERE sensor_code=%s AND rule_code=%s AND trig_time=%s",
                       (r[0], r[1], r[4]), one=True)
        if hit:
            rule = RULE_BY_CODE.get(r[1])
            notify_for_alert(hit, rule or {}, registry.village_name(hit["village_code"]))
    if inserted:
        invalidate_prefix("hsf:stat:alerts:")  # 一期契约④：统计缓存键 7/8 失效
        _emit("hsf.iot.alert.raised", {"count": inserted})
    return inserted


# ── 灌溉决策落库（docs/07 §4.3/4.4；uk_advice_dedup 幂等）────────────────────────

def create_advice(sensor_code: str, village_code: str, parcel_code: str, trigger_value: float,
                  trig_time: dt.datetime, params: dict, area_mu: float | None) -> dict | None:
    """墒情 <40 → 生成建议；同 6h 窗口重复触发 → uk_advice_dedup 幂等（行数不变，返回
    None 不重复通知——hsf_iot_advice_created_total 仅计新插入）。新插入时补 4 行 notify
    + domain_event。"""
    dkey = decision.window_key(parcel_code, trig_time, params["dedup_hours"])
    per_mu = decision.per_mu_m3(trigger_value, params)
    total = decision.total_m3(per_mu, area_mu)
    rid = db.execute(
        "INSERT IGNORE INTO hsf_biz.irrigation_advice(parcel_code,sensor_code,village_code,"
        "trigger_value,theta_fc,depth_m,soil_gamma,per_mu_m3,area_mu,total_m3,dedup_key) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (parcel_code.strip(), sensor_code, village_code, trigger_value, params["fc"],
         params["depth"], params["gamma"], per_mu, area_mu, total, dkey))
    row = db.query("SELECT id, parcel_code, sensor_code, village_code, trigger_value, per_mu_m3, "
                   "area_mu, total_m3, state, created_at FROM hsf_biz.irrigation_advice "
                   "WHERE dedup_key=%s", (dkey,), one=True)
    if row and rid:
        notify_for_advice(row, params, registry_village_name(village_code))
        _emit("hsf.iot.advice.created", {"adviceId": row["id"], "parcelCode": parcel_code.strip(),
                                         "perMuM3": per_mu, "totalM3": total})
    return row if rid else None


def registry_village_name(village_code: str) -> str:
    """轻量村名查询（org 表 9 行量级；create_advice 路径不持有 registry 引用时兜底）。"""
    r = db.query("SELECT name FROM hsf_md.org WHERE code=%s", (village_code,), one=True)
    return r["name"] if r else ""


def auto_close_advices(parcel_code: str) -> int:
    """墒情 ≥55 → 该地块 OPEN 建议自动闭环（AUTO_RECOVER，docs/07 §4.3 状态机）。"""
    return max(db.execute(
        "UPDATE hsf_biz.irrigation_advice SET closed_at=NOW(), close_reason='AUTO_RECOVER' "
        "WHERE parcel_code=%s AND state='OPEN'", (parcel_code.strip(),)), 0)


def rebuild_tripped(engine) -> int:
    """重启重建（docs/07 §4.2）：每 (device, rule) 最近 1 条 state≠CLOSED 告警 → 置 TRIPPED。"""
    rows = db.query(
        "SELECT a.sensor_code, a.rule_code FROM hsf_biz.alert_record a "
        "JOIN (SELECT sensor_code, rule_code, MAX(id) mid FROM hsf_biz.alert_record "
        "      GROUP BY sensor_code, rule_code) t ON a.id = t.mid "
        "WHERE a.state <> 'CLOSED'")
    return engine.seed_tripped([(r["sensor_code"], r["rule_code"]) for r in rows])


# ── flush 管道（docs/07 §4.1：写时序 → latest 缓存 → last_* → 规则/决策）───────────

def flush(batch: list[tuple], engine, registry: SensorRegistry, latest: LatestCache,
          params: dict) -> dict:
    """一批读数落库与评估。batch 元素 = (device_code, metric, value, recorded_at aware)。

    单批 execute_values + ON CONFLICT DO NOTHING（docs/07 §3.2，QoS1 幂等 D-07-3）；
    刷后四步按 §4.1 顺序执行。"""
    written = skipped = 0
    if batch:
        rows = [(d, m, float(v), ts) for (d, m, v, ts) in batch]
        written = tsdb.execute_values_insert(
            "INSERT INTO hsf_ts.sensor_reading(device_code, metric, value, recorded_at) "
            "VALUES %s ON CONFLICT (device_code, metric, recorded_at) DO NOTHING", rows)
        skipped = len(rows) - written
        for d, m, v, ts in batch:                      # (a) latest 进程内缓存
            latest.put(d, m, v, ts)
        last: dict[str, tuple] = {}                    # (b) sensor.last_* 读优化冗余（仅 iot 写）
        for d, m, v, ts in batch:
            last[d] = (v, ts)
        db.execute_many(
            "UPDATE hsf_biz.sensor SET `last_value`=%s, `last_recorded_at`=%s, updated_at=NOW() "
            "WHERE sensor_code=%s",
            [(v, _naive(ts), d) for d, (v, ts) in last.items()])

    alerts_new = 0                                     # (c) 规则引擎（回差状态机）
    alert_rows: list[tuple] = []
    rule_codes: list[str] = []
    for d, m, v, ts in batch:
        for r in engine.feed(d, m, v):
            rule_codes.append(r["rule_code"])
            dev = registry.get(d)
            alert_rows.append((d, r["rule_code"], m, v, _naive(ts),
                               dev["village_code"], dev["parcel_code"]) if dev
                              else (d, r["rule_code"], m, v, _naive(ts), "", None))
    alerts_new = insert_alerts(alert_rows, registry)   # 幂等键兜底：刷写失败重试不重复告警

    advices_new = closed = 0                           # (d) 灌溉决策（仅 soil_moisture）
    for d, m, v, ts in batch:
        if m != "soil_moisture":
            continue
        dev = registry.get(d)
        if not dev or not dev["parcel_code"]:
            continue  # 未绑定地块的墒情读数不参与决策（跨库引用以地块为键）
        parcel = dev["parcel_code"].strip()
        if decision.needs_advice(v, params):
            row = create_advice(d, dev["village_code"], parcel, v, ts, params,
                                registry.area_of(parcel))
            if row:
                advices_new += 1
        elif decision.should_auto_close(v, params):
            closed += auto_close_advices(parcel)
    return {"received": len(batch), "written": written, "skipped": skipped,
            "alerts": alerts_new, "advices": advices_new, "closed": closed,
            "rule_codes": rule_codes}


def flusher_loop(buffer, engine, registry: SensorRegistry, latest: LatestCache, params: dict,
                 stop_event: threading.Event, on_flush_error=None) -> None:
    """刷写线程体：500 条或 2s 先到者刷（轮询 0.2s 粒度）；失败整批回队重试（幂等兜底）；
    台账/面积镜像随周期刷新（60s）。"""
    last_flush = time.monotonic()
    last_registry = time.monotonic()
    while not stop_event.is_set():
        stop_event.wait(0.2)
        now = time.monotonic()
        if now - last_registry >= REGISTRY_REFRESH:
            last_registry = now
            try:
                registry.refresh()         # 新设备/面积变更 ≤60s 内接入
            except Exception:
                pass                       # 刷新失败沿用当前镜像，下轮重试
        batch: list[tuple] = []
        if len(buffer) >= BATCH_MAX or (len(buffer) > 0 and now - last_flush >= FLUSH_INTERVAL):
            batch = buffer.swap()
        if not batch:
            continue
        try:
            with metrics.FLUSH_SECONDS.time():
                summary = flush(batch, engine, registry, latest, params)
            metrics.READINGS_WRITTEN.inc(summary["written"])
            metrics.ONCONFLICT_SKIPPED.inc(summary["skipped"])
            for rc in summary["rule_codes"]:
                metrics.RULE_TRIPPED.labels(rc).inc()
            if summary["advices"]:
                metrics.ADVICE_CREATED.inc(summary["advices"])
        except Exception as e:
            if on_flush_error:
                on_flush_error(e, len(batch))
            buffer.put_front(batch[:BUFFER_PUT_FRONT_MAX])
        last_flush = time.monotonic()


# ── 数据范围（05 §2.4 矩阵 + docs/07 §4.5 notify 读取模型；role 取自 JWT）──────────

_org_cache: dict[int, tuple[float, dict]] = {}
_villages_cache: dict[str, tuple[float, list]] = {}
SCOPE_TTL = 60.0


def jwt_role(user: dict) -> str | None:
    """JWT roles 列表 → 单角色（iam 登录 roles=[party.role]，五角色取命中首个）。"""
    for r in ("admin", "town", "village", "keeper", "coop"):
        if r in (user.get("roles") or []):
            return r
    return None


def party_of(user_id: int) -> dict:
    """x-user-id(JWT sub) → party {org_code, role}（read pool 直查，进程内缓存 60s）。"""
    hit = _org_cache.get(user_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    row = db.query("SELECT org_code, role FROM hsf_md.party WHERE id=%s", (user_id,), one=True) \
        or {"org_code": "", "role": ""}
    _org_cache[user_id] = (time.monotonic() + SCOPE_TTL, row)
    return row


def _villages_for(role: str, user_id: int, org_code: str) -> list[str]:
    """keeper=所辖设施所在村集 / coop=关联地块所在村集（05 §2.4；docs/07 §4.5 同口径）。"""
    key = f"{role}:{user_id}"
    hit = _villages_cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    if role == "keeper":
        rows = db.query(
            "SELECT DISTINCT f.village_code FROM hsf_biz.facility_workorder w "
            "JOIN hsf_md.facility f ON w.facility_code = f.code WHERE w.assignee_id=%s",
            (user_id,))
    elif role == "coop":
        rows = db.query("SELECT DISTINCT village_code FROM hsf_md.land_parcel WHERE holder_id=%s",
                        (user_id,))
    else:
        rows = []
    villages = [r["village_code"] for r in rows if r.get("village_code")]
    _villages_cache[key] = (time.monotonic() + SCOPE_TTL, villages)
    return villages


def scope_where(role: str | None, user_id: int, col: str = "village_code") -> tuple[str, list]:
    """按角色生成村过滤 SQL 片段（admin=不过滤；返回 (fragment, args)，空片段=无过滤）。"""
    if role == "admin":
        return "", []
    org = party_of(user_id)
    org_code = org.get("org_code") or ""
    if role == "town":
        return f"{col} LIKE %s", [org_code[:9] + "%"] if org_code else ("1=0", [])
    if role == "village":
        return f"{col} = %s", [org_code] if org_code else ("1=0", [])
    if role in ("keeper", "coop"):
        villages = _villages_for(role, user_id, org_code)
        if not villages:
            return "1=0", []
        marks = ",".join(["%s"] * len(villages))
        return f"{col} IN ({marks})", list(villages)
    return "1=0", []  # 未知角色：查询面空集（越权返回空集，05 §2.4）


# ── 查询（docs/07 §4.6 端点 1/3/4/5 契约；字段口径同 05 §7.1）─────────────────────

def _page(page: int, size: int) -> tuple[int, int]:
    """分页规约（05 §2.2）：page≥1，顶层 size≤100。"""
    return max(page, 1), min(max(size, 1), 100)


def devices_list(village_code: str | None, metric: str | None, page: int, size: int,
                 role: str | None, user_id: int) -> dict:
    """GET /devices：数据源 hsf_biz.sensor，字段口径同 05 §7.1 端点 2（hsf_biz.sensor 含
    last_* 读优化冗余——二期由 iot flush 维护，docs/07 §2.5）。"""
    page, size = _page(page, size)
    where, args = "WHERE 1=1", []
    if village_code:
        where += " AND village_code=%s"; args.append(village_code)
    if metric:
        where += " AND metric=%s"; args.append(metric)
    frag, fargs = scope_where(role, user_id)
    if frag:
        where += f" AND {frag}"; args.extend(fargs)
    args = tuple(args)
    total = int(db.query(f"SELECT COUNT(*) c FROM hsf_biz.sensor {where}", args, one=True)["c"])
    rows = db.query(f"SELECT * FROM hsf_biz.sensor {where} "
                    f"ORDER BY sensor_code LIMIT %s OFFSET %s", args + (size, (page - 1) * size))
    items = [{"sensorCode": r["sensor_code"], "metric": r["metric"],
              "villageCode": r["village_code"], "parcelCode": r["parcel_code"],
              "status": r["status"], "lastValue": _f(r["last_value"]),
              "lastRecordedAt": str(r["last_recorded_at"]) if r["last_recorded_at"] else None}
             for r in rows]
    return {"total": total, "page": page, "size": size, "items": items}


def alerts_list(state: str | None, village_code: str | None, page: int, size: int,
                role: str | None, user_id: int) -> dict:
    """GET /alerts：契约同 05 §7.1 端点 4（docs/07 §4.6 端点 3）。"""
    page, size = _page(page, size)
    where, args = "WHERE 1=1", []
    if state:
        where += " AND state=%s"; args.append(state)
    if village_code:
        where += " AND village_code=%s"; args.append(village_code)
    frag, fargs = scope_where(role, user_id)
    if frag:
        where += f" AND {frag}"; args.extend(fargs)
    args = tuple(args)
    total = int(db.query(f"SELECT COUNT(*) c FROM hsf_biz.alert_record {where}", args, one=True)["c"])
    rows = db.query(f"SELECT * FROM hsf_biz.alert_record {where} "
                    f"ORDER BY id DESC LIMIT %s OFFSET %s", args + (size, (page - 1) * size))
    items = [{"id": r["id"], "sensorCode": r["sensor_code"], "ruleCode": r["rule_code"],
              "metric": r["metric"], "trigValue": _f(r["trig_value"]),
              "trigTime": str(r["trig_time"]), "villageCode": r["village_code"],
              "parcelCode": r["parcel_code"], "state": r["state"],
              "handledBy": r["handled_by"],
              "handledAt": str(r["handled_at"]) if r["handled_at"] else None,
              "closedAt": str(r["closed_at"]) if r["closed_at"] else None, "note": r["note"]}
             for r in rows]
    return {"total": total, "page": page, "size": size, "items": items}


def advice_list(state: str | None, parcel_code: str | None, village_code: str | None,
                page: int, size: int, role: str | None, user_id: int) -> dict:
    """GET /irrigation-advice（docs/07 §4.6 端点 4）。"""
    page, size = _page(page, size)
    where, args = "WHERE 1=1", []
    if state:
        where += " AND state=%s"; args.append(state)
    if parcel_code:
        where += " AND parcel_code=%s"; args.append(parcel_code)
    if village_code:
        where += " AND village_code=%s"; args.append(village_code)
    frag, fargs = scope_where(role, user_id)
    if frag:
        where += f" AND {frag}"; args.extend(fargs)
    args = tuple(args)
    total = int(db.query(f"SELECT COUNT(*) c FROM hsf_biz.irrigation_advice {where}",
                         args, one=True)["c"])
    rows = db.query(f"SELECT * FROM hsf_biz.irrigation_advice {where} "
                    f"ORDER BY id DESC LIMIT %s OFFSET %s", args + (size, (page - 1) * size))
    items = [{"id": r["id"], "parcelCode": r["parcel_code"], "sensorCode": r["sensor_code"],
              "villageCode": r["village_code"], "triggerValue": _f(r["trigger_value"]),
              "perMuM3": _f(r["per_mu_m3"]), "areaMu": _f(r["area_mu"]),
              "totalM3": _f(r["total_m3"]), "state": r["state"],
              "closedAt": str(r["closed_at"]) if r["closed_at"] else None,
              "closeReason": r["close_reason"],
              "createdAt": str(r["created_at"])} for r in rows]
    return {"total": total, "page": page, "size": size, "items": items}


def notify_list(role: str | None, user_id: int, unread_only: bool, page: int, size: int) -> dict:
    """GET /notify：role 恒取自 JWT（不接受入参，防越权）；行级角色过滤与村范围过滤叠加
    执行（docs/07 §4.5 读取模型）；coop 推送面空集。"""
    page, size = _page(page, size)
    base = {"total": 0, "unread": 0, "page": page, "size": size, "items": []}
    if role not in NOTIFY_ROLES:
        return base
    where, args = "WHERE user_role=%s", [role]
    frag, fargs = scope_where(role, user_id, col="village_code")
    if frag:
        where += f" AND {frag}"; args.extend(fargs)
    if unread_only:
        where += " AND read_at IS NULL"
    args = tuple(args)
    total = int(db.query(f"SELECT COUNT(*) c FROM hsf_biz.notify {where}", args, one=True)["c"])
    unread_where, unread_args = "WHERE user_role=%s AND read_at IS NULL", [role]
    if frag:
        unread_where += f" AND {frag}"; unread_args.extend(fargs)
    unread = int(db.query(f"SELECT COUNT(*) c FROM hsf_biz.notify {unread_where}",
                          tuple(unread_args), one=True)["c"])
    rows = db.query(f"SELECT * FROM hsf_biz.notify {where} "
                    f"ORDER BY id DESC LIMIT %s OFFSET %s", args + (size, (page - 1) * size))
    items = [{"id": r["id"], "alertId": r["alert_id"], "adviceId": r["advice_id"],
              "kind": r["kind"], "title": r["title"], "content": r["content"],
              "villageCode": r["village_code"],
              "readAt": str(r["read_at"]) if r["read_at"] else None,
              "createdAt": str(r["created_at"])} for r in rows]
    return {"total": total, "unread": unread, "page": page, "size": size, "items": items}


def notify_read(role: str | None, notify_id: int) -> dict:
    """POST /notify/{id}/read：置 read_at=NOW()，重复调用幂等 no-op；
    行.user_role ≠ token 角色 → FORBIDDEN（docs/07 §4.6 端点 6）。返回 (readAt, error)。"""
    row = db.query("SELECT id, user_role, read_at FROM hsf_biz.notify WHERE id=%s",
                   (notify_id,), one=True)
    if not row:
        return None, "NOT_FOUND"
    if role not in NOTIFY_ROLES or row["user_role"] != role:
        return None, "FORBIDDEN"
    if row["read_at"] is None:
        db.execute("UPDATE hsf_biz.notify SET read_at=NOW() WHERE id=%s AND read_at IS NULL",
                   (notify_id,))
        row = db.query("SELECT id, read_at FROM hsf_biz.notify WHERE id=%s", (notify_id,), one=True)
    return {"id": row["id"], "readAt": str(row["read_at"])}, None
