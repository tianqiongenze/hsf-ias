"""iot R-01 TDD：刷写/评估分层、worker 池、有界缓冲背压与冻结契约不回归。

冻结契约断言（docs/07 §4.1 / CONVENTIONS-P2 T-2，docs/24 §4 R-01 红线）：
  ① BATCH_MAX=500 / FLUSH_INTERVAL=2.0（"500 条或 2s 先到者刷"）不变；
  ② /sensor/latest 口径=A-07-3（每批写后更新，且 latest=最新读数语义多线程不回退）；
  ③ 回差状态机串行语义不变（唯一消费者），重放幂等（uk_rule_reading 兜底不变）。
新增逻辑：flush_write/evaluate 分层、BatchQueue（满丢最旧+计数）、ReadingsBuffer 有界背压、
swap(on_take) 保序移交、evaluator_loop 失败重投、多 worker 排空无丢失。
"""
import datetime as dt
import threading
import time

import pytest
from prometheus_client import REGISTRY

from app import mqtt_sub, repo, rules
from app.decision import DEFAULTS
from app.rules import HysteresisEngine
from test_flush_pipeline import PARAMS, T0, FakeDB  # noqa: F401  （复用替身与冻结参数）

V_TZ = dt.timezone(dt.timedelta(hours=8))


@pytest.fixture()
def fake(monkeypatch):
    f = FakeDB()
    monkeypatch.setattr(repo, "db", f)
    monkeypatch.setattr(repo, "registry_village_name", lambda code: "清溪村")
    written_log = []
    monkeypatch.setattr(repo.tsdb, "execute_values_insert",
                        lambda sql, rows, page_size=500: (written_log.append(len(rows)),
                                                          len(rows))[1])
    f.written_log = written_log
    monkeypatch.setattr(repo, "_emit", lambda topic, payload: None)
    monkeypatch.setattr(repo, "invalidate_prefix", lambda prefix: None)
    return f


def wait_until(cond, timeout=6.0, step=0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


def _registry():
    r = repo.SensorRegistry()
    r.refresh()
    return r


# ── 冻结契约不回归 ───────────────────────────────────────────────────────────────────

def test_frozen_cadence_constants_unchanged():
    """① 刷写节奏冻结：500 条或 2s 先到者刷（R-01 不得动这两个常量）。"""
    assert repo.BATCH_MAX == 500
    assert repo.FLUSH_INTERVAL == 2.0


# ── 分层正确性：flush_write / evaluate / flush 组合 ─────────────────────────────────

def test_flush_write_only_no_eval_side_effects(fake):
    """flush_write 只做写路径三步（时序/latest/last_*），不触发规则与决策。"""
    registry = _registry()
    latest = repo.LatestCache()
    batch = [("S5306000001", "soil_moisture", 14.9, T0)]   # 14.9 必触发 RULE_DROUGHT
    summary = repo.flush_write(batch, latest)
    assert summary == {"received": 1, "written": 1, "skipped": 0}
    assert latest._d["S5306000001"][1] == 14.9             # (a) latest 每批写后更新
    assert [e for e in fake.executed if e[1].startswith("UPDATE hsf_biz.sensor")]  # (b) last_*
    assert len(fake.alerts) == 0                           # (c) 无规则副作用
    assert len(fake.advices) == 0                          # (d) 无决策副作用


def test_evaluate_produces_alert_and_advice(fake):
    """evaluate 旁路独立产出告警+建议（与原 flush ④⑤ 等价——分家不减功能）。"""
    registry = _registry()
    engine = HysteresisEngine(rules.RULES)
    batch = [("S5306000001", "soil_moisture", 14.9, T0)]
    summary = repo.evaluate(batch, engine, registry, PARAMS)
    assert summary["alerts"] == 1 and summary["rule_codes"] == ["RULE_DROUGHT"]
    assert summary["advices"] == 1                         # 14.9<40 → 灌溉建议
    assert len(fake.notifies) == 8                         # alert 4 行 + advice 4 行


def test_composed_flush_equals_write_plus_eval(fake):
    """flush() 组合 = flush_write + evaluate（EVAL_INLINE=1 回退路径行为等价、键集不变）。"""
    registry = _registry()
    latest = repo.LatestCache()
    engine = HysteresisEngine(rules.RULES)
    batch = [("S5306000001", "soil_moisture", 14.9, T0),
             ("S5306000001", "soil_moisture", 16.0, T0 + dt.timedelta(milliseconds=1))]
    summary = repo.flush(batch, engine, registry, latest, PARAMS)
    assert set(summary) == {"received", "written", "skipped", "alerts", "advices",
                            "closed", "rule_codes"}
    assert summary["written"] == 2 and summary["alerts"] == 1 and summary["advices"] == 1


def test_engine_replay_idempotent_and_serial(fake):
    """③ 回差状态机串行语义：同读数重放不重复告警（TRIPPED 持续），时序回退值不误触发。"""
    registry = _registry()
    engine = HysteresisEngine(rules.RULES)
    batch = [("S5306000001", "wind_speed", 19.0, T0)]      # >17 触发 RULE_GALE
    assert repo.evaluate(batch, engine, registry, PARAMS)["alerts"] == 1
    assert repo.evaluate(batch, engine, registry, PARAMS)["alerts"] == 0   # 重放：不再触发
    assert engine.is_tripped("S5306000001", "RULE_GALE")


# ── 有界缓冲背压（R-01 approach ③）─────────────────────────────────────────────────

def test_bounded_buffer_drops_newest_when_full():
    drops = []
    buf = mqtt_sub.ReadingsBuffer(maxlen=3, on_drop=lambda: drops.append(1))
    for i in range(5):
        buf.append(("S1", "soil_moisture", 30.0 + i, T0))
    assert len(buf) == 3                                   # 上限生效
    assert [r[2] for r in buf.swap()] == [30.0, 31.0, 32.0]  # 保序：旧数据不动、丢最新
    assert len(drops) == 2


def test_bounded_buffer_zero_is_unbounded_legacy():
    buf = mqtt_sub.ReadingsBuffer(maxlen=0)                # 0=无界=旧行为回退
    for i in range(10):
        buf.append(("S1", "soil_moisture", 30.0, T0))
    assert len(buf) == 10


def test_swap_on_take_preserves_order_and_drains():
    seen = []
    buf = mqtt_sub.ReadingsBuffer()
    for i in range(4):
        buf.append(("S1", "soil_moisture", 30.0 + i, T0))
    got = buf.swap(on_take=seen.append)
    assert [r[2] for r in got] == [30.0, 31.0, 32.0, 33.0]
    assert seen == [got]                                   # on_take 收到同一批（锁内移交）
    assert len(buf) == 0


def test_latest_cache_timestamp_guard(fake):
    """② /sensor/latest 口径：多 worker 并发下旧批后到不覆盖新值（latest=最新读数）。"""
    latest = repo.LatestCache()
    t_old, t_new = T0, T0 + dt.timedelta(seconds=2)
    latest.put("S1", "soil_moisture", 31.0, t_new)
    latest.put("S1", "soil_moisture", 30.0, t_old)         # 旧值晚到（并发乱序）
    assert latest._d["S1"][1] == 31.0                      # 新值不被回退
    latest.put("S1", "soil_moisture", 32.0, t_new)         # 同刻覆盖仍允许（QoS1 重复）
    assert latest._d["S1"][1] == 32.0


# ── BatchQueue（评估旁路移交通道）──────────────────────────────────────────────────

def test_batchqueue_fifo_and_depth():
    q = repo.BatchQueue(maxsize=8)
    b1, b2 = [("S1", "soil_moisture", 30.0, T0)], [("S1", "wind_speed", 19.0, T0)]
    assert q.put(b1) == 0 and q.put(b2) == 0
    assert q.depth() == 2
    assert q.get(1.0) is b1 and q.get(1.0) is b2           # FIFO
    assert q.get(0.05) is None                             # 空超时


def test_batchqueue_drop_oldest_when_full_and_metric():
    before = REGISTRY.get_sample_value("hsf_iot_eval_queue_dropped_total") or 0.0
    q = repo.BatchQueue(maxsize=2)
    b1 = [("S1", "soil_moisture", 30.0, T0)] * 3           # 3 条
    b2 = [("S1", "wind_speed", 19.0, T0)]
    b3 = [("S1", "air_temp", 30.0, T0)]
    assert q.put(b1) == 0
    assert q.put(b2) == 0
    assert q.put(b3) == 3                                  # 挤掉最旧 b1（按读数条数计）
    assert q.depth() == 2
    after = REGISTRY.get_sample_value("hsf_iot_eval_queue_dropped_total") or 0.0
    assert after - before == 3.0
    assert q.get(1.0) is b2 and q.get(1.0) is b3           # 旧批被丢、次序保持


def test_batchqueue_put_front_retry_priority():
    q = repo.BatchQueue(maxsize=4)
    b1, b2 = [("S1", "soil_moisture", 30.0, T0)], [("S1", "air_temp", 30.0, T0)]
    q.put(b1)
    q.put(b2)
    assert q.get(1.0) is b1
    q.put_front(b1)                                        # 评估失败回队首
    assert q.get(1.0) is b1 and q.get(1.0) is b2


def test_evaluator_loop_processes_and_retries_on_error(fake):
    """评估线程：正常消费出告警；engine 异常一次 → EVAL_FAILED 计数并回队重试成功。"""
    registry = _registry()
    engine = HysteresisEngine(rules.RULES)
    q = repo.BatchQueue(maxsize=4)
    stop = threading.Event()
    calls = {"n": 0}
    real_feed = engine.feed

    def flaky_feed(d, m, v):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated eval failure")
        return real_feed(d, m, v)

    engine.feed = flaky_feed
    threading.Thread(target=repo.evaluator_loop,
                     args=(q, engine, registry, PARAMS, stop), daemon=True).start()
    q.put([("S5306000001", "wind_speed", 19.0, T0)])
    assert wait_until(lambda: len(fake.alerts) == 1), "评估失败重试后应产出告警"
    before = REGISTRY.get_sample_value("hsf_iot_eval_failed_total") or 0.0
    assert before >= 1.0
    stop.set()


# ── worker 池集成（多线程排空无丢失 + 旁路全链路）──────────────────────────────────

def test_multi_worker_drains_buffer_no_loss(fake):
    """3 个 worker 消费同一缓冲：两轮共 2000 条全部落库（无丢失），缓冲排空。"""
    buf = mqtt_sub.ReadingsBuffer()
    registry = _registry()
    latest = repo.LatestCache()
    engine = HysteresisEngine(rules.RULES)
    stop = threading.Event()
    errors = []
    for i in range(3):
        threading.Thread(target=repo.flusher_loop,
                         args=(buf, engine, registry, latest, PARAMS, stop,
                               lambda e, n, _errors=errors: _errors.append((e, n))),
                         daemon=True).start()
    for i in range(1200):
        buf.append(("S5306000001", "air_temp", 20.0 + (i % 10), T0))
    assert wait_until(lambda: len(buf) == 0 and sum(fake.written_log) == 1200), \
        f"第一轮未排空：buf={len(buf)} written={sum(fake.written_log)}"
    for i in range(800):
        buf.append(("S5306000001", "air_temp", 20.0 + (i % 10),
                    T0 + dt.timedelta(milliseconds=i)))
    assert wait_until(lambda: len(buf) == 0 and sum(fake.written_log) == 2000), \
        f"第二轮未排空：buf={len(buf)} written={sum(fake.written_log)}"
    assert errors == []                                    # 无刷写失败
    stop.set()


def test_bypass_pipeline_end_to_end_alerts_arrive(fake):
    """旁路全链路：buffer→worker（swap 内移交）→评估线程→告警落库；数据全量写入。"""
    buf = mqtt_sub.ReadingsBuffer()
    registry = _registry()
    latest = repo.LatestCache()
    engine = HysteresisEngine(rules.RULES)
    q = repo.BatchQueue(maxsize=8)
    stop = threading.Event()
    for i in range(2):
        threading.Thread(target=repo.flusher_loop,
                         args=(buf, engine, registry, latest, PARAMS, stop, None, q),
                         daemon=True).start()
    threading.Thread(target=repo.evaluator_loop,
                     args=(q, engine, registry, PARAMS, stop), daemon=True).start()
    for i in range(501):                                   # >500 触发节奏；极端风触发 RULE_GALE
        buf.append(("S5306000001", "wind_speed", 19.0 + (i % 3), T0))
    assert wait_until(lambda: len(buf) == 0 and sum(fake.written_log) == 501
                      and len(fake.alerts) >= 1 and q.depth() == 0), \
        f"旁路链路未收敛：buf={len(buf)} written={sum(fake.written_log)} " \
        f"alerts={len(fake.alerts)} q={q.depth()}"
    assert any(k[1] == "RULE_GALE" for k in fake.alerts)
    stop.set()


def test_inline_pipeline_still_works(fake):
    """EVAL_INLINE 回退路径（eval_queue=None）：单 worker 内联评估，告警照常产出。"""
    buf = mqtt_sub.ReadingsBuffer()
    registry = _registry()
    latest = repo.LatestCache()
    engine = HysteresisEngine(rules.RULES)
    stop = threading.Event()
    threading.Thread(target=repo.flusher_loop,
                     args=(buf, engine, registry, latest, PARAMS, stop, None, None),
                     daemon=True).start()
    for i in range(501):
        buf.append(("S5306000001", "wind_speed", 19.0 + (i % 3), T0))
    assert wait_until(lambda: len(buf) == 0 and sum(fake.written_log) == 501
                      and len(fake.alerts) >= 1)
    stop.set()


def test_flush_failure_requeues_and_no_data_loss(fake, monkeypatch):
    """写库失败整批回队 → 恢复后重试成功：总量不丢（幂等键防重复由 TS 侧 ON CONFLICT 承担）。"""
    buf = mqtt_sub.ReadingsBuffer()
    registry = _registry()
    latest = repo.LatestCache()
    engine = HysteresisEngine(rules.RULES)
    stop = threading.Event()
    state = {"fail": True}

    def flaky_insert(sql, rows, page_size=500):
        if state["fail"]:
            raise RuntimeError("timescale down")
        fake.written_log.append(len(rows))
        return len(rows)

    monkeypatch.setattr(repo.tsdb, "execute_values_insert", flaky_insert)
    errors = []
    threading.Thread(target=repo.flusher_loop,
                     args=(buf, engine, registry, latest, PARAMS, stop,
                           lambda e, n: errors.append(str(e)), None),
                     daemon=True).start()
    for i in range(501):
        buf.append(("S5306000001", "air_temp", 20.0, T0))
    assert wait_until(lambda: len(errors) >= 1), "失败批次应回队并记录错误"
    assert len(fake.written_log) == 0                      # 失败期无写入
    state["fail"] = False
    assert wait_until(lambda: len(buf) == 0 and sum(fake.written_log) == 501), \
        f"恢复后应重试成功：buf={len(buf)} written={sum(fake.written_log)}"
    stop.set()


def test_broker_sysmon_parses_deltas():
    """R-03 $SYS 解析：dropped Δ 增量计入、broker 重启清零不产生负增量、其余 bucket 落 gauge。"""
    from app import sysmon
    mon = sysmon.BrokerSysMonitor("h", 1883, "u", "p")

    def feed(topic, payload):
        mon._on_message(None, None, type("M", (), {"topic": topic,
                                                   "payload": payload})())
    before_dropped = REGISTRY.get_sample_value("hsf_broker_messages_dropped_total") or 0.0
    feed("$SYS/broker/publish/messages/dropped", b"100")   # 基线：不产生增量
    assert mon._dropped_last == 100
    feed("$SYS/broker/publish/messages/dropped", b"150")   # +50
    after = REGISTRY.get_sample_value("hsf_broker_messages_dropped_total") or 0.0
    assert after - before_dropped == 50.0
    feed("$SYS/broker/mqtt/publish/dropped", b"170")       # 新别名同源：+20
    assert (REGISTRY.get_sample_value("hsf_broker_messages_dropped_total") or 0.0) - after == 20.0
    feed("$SYS/broker/publish/messages/dropped", b"5")     # broker 重启清零：重置基线
    assert mon._dropped_last == 5 and mon.drop_resets() == 1
    assert (REGISTRY.get_sample_value("hsf_broker_messages_dropped_total") or 0.0) - after == 20.0
    feed("$SYS/broker/store/messages/count", b"42")
    assert REGISTRY.get_sample_value("hsf_broker_messages_stored") == 42.0
    feed("$SYS/broker/clients/connected", b"3")
    assert REGISTRY.get_sample_value("hsf_broker_clients_connected") == 3.0
    feed("$SYS/broker/heap/current", b"1048576")
    assert REGISTRY.get_sample_value("hsf_broker_heap_bytes") == 1048576.0
    feed("$SYS/broker/version", b"2.1.2")                  # 非数值 bucket：不落指标不报错
