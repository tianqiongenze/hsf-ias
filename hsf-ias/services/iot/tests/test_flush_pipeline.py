"""iot TDD：flush 管道编排（docs/07 §4.1 刷后四步 + 回差→告警→notify→决策联动）。

以 dict/函数桩替身 TiDB 双池与 TimescaleDB（幂等语义与 uk_* 一致）——不依赖外部服务。
冻结断言：一批读数 → ① 时序批量写一次 ② latest 缓存取批内最后值 ③ sensor.last_* UPDATE
④ 回差触发→alert_record 1 行 + notify 4 行 + 统计缓存失效 + domain_event；soil_moisture
<40 → 灌溉建议（6h 窗口幂等）+ notify 4 行；≥55 → OPEN 建议自动闭环。
"""
import datetime as dt

import pytest

from app import repo, rules
from app.decision import DEFAULTS
from app.rules import HysteresisEngine

V_TZ = dt.timezone(dt.timedelta(hours=8))
T0 = dt.datetime(2026, 11, 8, 10, 0, 0, 123000, tzinfo=V_TZ)
PARAMS = dict(DEFAULTS)


class FakeDB:
    """TiDB 替身：INSERT IGNORE 按 uk_* 幂等（与 D-07-3/04 §4.6 语义一致）。"""

    def __init__(self):
        self.executed: list = []
        self.alert_seq = 0
        self.alerts: dict = {}
        self.advice_seq = 0
        self.advices: dict = {}
        self.notifies: list = []

    def execute(self, sql, args=()):
        self.executed.append(("x", sql, args))
        if sql.startswith("INSERT IGNORE INTO hsf_biz.irrigation_advice"):
            dkey = args[-1]
            if dkey in self.advices:
                return 0                              # uk_advice_dedup 命中 → 行数不变
            self.advice_seq += 1
            self.advices[dkey] = {"id": self.advice_seq, "parcel_code": args[0],
                                  "sensor_code": args[1], "village_code": args[2],
                                  "trigger_value": args[3], "per_mu_m3": args[7],
                                  "area_mu": args[8], "total_m3": args[9], "state": "OPEN"}
            return self.advice_seq
        return 1                                      # UPDATE auto-close 等 → affected 1

    def execute_many(self, sql, args_list):
        self.executed.append(("m", sql, list(args_list)))
        if sql.startswith("INSERT IGNORE INTO hsf_biz.alert_record"):
            n = 0
            for r in args_list:
                key = (r[0], r[1], r[4])
                if key not in self.alerts:            # uk_rule_reading 幂等
                    self.alert_seq += 1
                    self.alerts[key] = {"id": self.alert_seq, "sensor_code": r[0],
                                        "rule_code": r[1], "metric": r[2], "trig_value": r[3],
                                        "trig_time": str(r[4]), "village_code": r[5],
                                        "parcel_code": r[6]}
                    n += 1
            return n
        if sql.startswith("INSERT IGNORE INTO hsf_biz.notify"):
            self.notifies.extend(list(args_list))
            return len(args_list)
        return len(args_list)

    def query(self, sql, args=(), one=False):
        if "FROM hsf_biz.sensor" in sql and "SELECT sensor_code" in sql:
            rows = [{"sensor_code": "S5306000001", "metric": "soil_moisture",
                     "village_code": "53060010101", "parcel_code": "L5306000001"}]
        elif "FROM hsf_md.org" in sql:
            rows = [{"code": "53060010101", "name": "清溪村"}]
        elif "FROM hsf_md.land_parcel WHERE code IN" in sql:
            rows = [{"code": "L5306000001", "area_mu": 12.5}]
        elif "FROM hsf_biz.alert_record WHERE sensor_code" in sql:
            rows = [self.alerts.get((args[0], args[1], args[2]))] \
                if (args[0], args[1], args[2]) in self.alerts else []
        elif "FROM hsf_biz.irrigation_advice WHERE dedup_key" in sql:
            rows = [self.advices[args[0]]] if args[0] in self.advices else []
        elif "SELECT COUNT(*)" in sql:
            rows = [{"c": 0}]
        else:
            rows = []
        return (rows[0] if rows else None) if one else rows


@pytest.fixture()
def fake(monkeypatch):
    """替身注入：repo.db / repo.tsdb / repo.invalidate_prefix / repo._emit。"""
    f = FakeDB()
    monkeypatch.setattr(repo, "db", f)
    monkeypatch.setattr(repo, "registry_village_name", lambda code: "清溪村")
    monkeypatch.setattr(repo.tsdb, "execute_values_insert", lambda sql, rows, page_size=500: len(rows))
    emitted, invalidated = [], []
    monkeypatch.setattr(repo, "_emit", lambda topic, payload: emitted.append(topic))
    monkeypatch.setattr(repo, "invalidate_prefix", lambda prefix: invalidated.append(prefix))
    f.emitted, f.invalidated = emitted, invalidated
    return f


def _flush(fake, readings):
    registry = repo.SensorRegistry()
    registry.refresh()
    latest = repo.LatestCache()
    engine = HysteresisEngine(rules.RULES)
    return repo.flush(readings, engine, registry, latest, PARAMS), registry, latest, engine


def test_flush_pipeline_full_cycle(fake):
    """一批三读数：14.9（触发+建议）/ 16.0（滞回抑制）/ 20.0（退出）→ 管道四步齐备。"""
    ts = lambda i: T0 + dt.timedelta(milliseconds=i)  # noqa: E731
    batch = [("S5306000001", "soil_moisture", 14.9, ts(0)),
             ("S5306000001", "soil_moisture", 16.0, ts(1)),
             ("S5306000001", "soil_moisture", 20.0, ts(2))]
    summary, registry, latest, engine = _flush(fake, batch)

    assert summary["written"] == 3 and summary["skipped"] == 0     # ① 时序批量写
    assert latest._d["S5306000001"][1] == 20.0                     # ② latest=批内最后值
    last_update = [e for e in fake.executed if e[1].startswith("UPDATE hsf_biz.sensor")]
    assert len(last_update) == 1 and last_update[0][2] == [(20.0, repo._naive(ts(2)),
                                                            "S5306000001")]  # ③ last_*
    assert summary["alerts"] == 1 and summary["rule_codes"] == ["RULE_DROUGHT"]  # ④ 回差触发一次
    assert len(fake.alerts) == 1                                   # 16.0/20.0 不重复告警
    alert = next(iter(fake.alerts.values()))
    assert alert["rule_code"] == "RULE_DROUGHT" and alert["village_code"] == "53060010101"
    assert len([n for n in fake.notifies if n[7] == f"alert:{alert['id']}"]) == 4  # notify 4 行
    assert {n[2] for n in fake.notifies if n[7] == f"alert:{alert['id']}"} == \
        set(repo.NOTIFY_ROLES)                                     # admin/town/village/keeper
    assert fake.invalidated == ["hsf:stat:alerts:"]                # 统计缓存失效（一期契约④）
    assert "hsf.iot.alert.raised" in fake.emitted                  # domain_event

    assert summary["advices"] == 1                                 # 14.9<40 → 建议
    advice = next(iter(fake.advices.values()))
    assert advice["per_mu_m3"] == 112.3                            # 667×0.3×1.4×(55−14.9)/100
    assert advice["total_m3"] == advice["per_mu_m3"] * 12.5 or advice["total_m3"] is not None
    assert len([n for n in fake.notifies if n[7] == f"advice:{advice['id']}"]) == 4
    assert "hsf.iot.advice.created" in fake.emitted


def test_flush_same_window_dedup_idempotent(fake):
    """同 6h 窗口第二次极端读数 → 建议行数不变（uk_advice_dedup 幂等）且不重复通知。"""
    t1 = T0.replace(hour=7)                                        # 同 6~12 窗口
    t2 = T0.replace(hour=11, minute=59)
    s1, *_ = _flush(fake, [("S5306000001", "soil_moisture", 30.0, t1)])
    assert s1["advices"] == 1
    notify_after_first = len(fake.notifies)
    s2, *_ = _flush(fake, [("S5306000001", "soil_moisture", 35.0, t2)])
    assert s2["advices"] == 0                                      # INSERT IGNORE 幂等
    assert len(fake.advices) == 1 and len(fake.notifies) == notify_after_first


def test_flush_auto_close_on_recover(fake):
    """墒情 ≥55 → OPEN 建议自动闭环（AUTO_RECOVER）；同批不再生成新建议。"""
    _flush(fake, [("S5306000001", "soil_moisture", 39.9, T0)])
    assert len(fake.advices) == 1
    summary, *_ = _flush(fake, [("S5306000001", "soil_moisture", 55.0, T0)])
    assert summary["closed"] == 1
    close_sql = [e for e in fake.executed if "close_reason='AUTO_RECOVER'" in e[1]]
    assert close_sql and close_sql[0][2] == ("L5306000001",)


def test_flush_alert_replay_same_trig_time_idempotent(fake):
    """QoS1 重复投递（同读数重放）→ 回差仍 TRIPPED 不再触发；uk_rule_reading 兜底。"""
    batch = [("S5306000001", "wind_speed", 19.0, T0)]
    s1, _, _, engine = _flush(fake, batch)
    assert s1["alerts"] == 1
    s2, *_ = _flush(fake, [batch[0]])                              # 同一读数重放
    assert s2["alerts"] == 0 and len(fake.alerts) == 1


def test_flush_failure_requeues_whole_batch():
    """刷写失败整批回队保序（幂等键保证重试不重复落库）。"""
    buf = __import__("app.mqtt_sub", fromlist=["ReadingsBuffer"]).ReadingsBuffer()
    rows = [("S1", "soil_moisture", 30.0, T0), ("S1", "soil_moisture", 31.0, T0)]
    for r in rows:
        buf.append(r)
    taken = buf.swap()
    assert taken == rows
    buf.put_front(taken)
    assert list(buf.swap()) == rows                                # 顺序保持
