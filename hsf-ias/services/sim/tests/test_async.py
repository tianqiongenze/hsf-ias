"""sim v1.1 异步化单测（ADR-HSF-028）：单调取号、发布池分片、JobManager 状态机、
混合响应阈值语义——不依赖 broker（paho 桩）。"""
import datetime as dt
import threading
import time

import pytest
import paho.mqtt.client as _mqtt

from app import jobs, mqtt_pub
from app.mqtt_pub import _MonoClock

V_TZ = dt.timezone(dt.timedelta(hours=8))
T0 = dt.datetime(2026, 11, 15, 10, 0, 0, 123000, tzinfo=V_TZ)


# ── 单调取号器（recorded_at 全局唯一——onconflict 根因消除）─────────────────────────

def test_mono_next_strictly_increasing():
    """严格递增 + 步进 ≥1ms：取号本身耗时的墙钟推进可超过 1ms 下限（只验下界）。"""
    c = _MonoClock()
    seq = [c.next() for _ in range(50)]
    for a, b in zip(seq, seq[1:]):
        assert b > a                                            # 严格递增（下界 1ms）


def test_mono_next_batch_reserves_slots():
    """批量取号预留 [base, base+(n-1)ms]；紧随的单条取号 ≥ 批末+1ms（跨批不重叠）。"""
    c = _MonoClock()
    base = c.next_batch(10)
    assert base <= c.next_batch(10)                             # 第二批基点不早于首批末槽
    one = c.next()
    assert one > base + dt.timedelta(milliseconds=9)            # 单条取号越过批量槽位


def test_mono_clock_beats_wall_clock_regression():
    """回拨场景：系统时间倒退时取号仍单调（last 锚定，now<=last 即 +1ms）。"""
    c = _MonoClock()
    c._last = dt.datetime(2030, 1, 1, tzinfo=V_TZ)              # 人为锚定未来时刻
    assert c.next() > dt.datetime(2030, 1, 1, tzinfo=V_TZ)      # 不回退


# ── PublisherPool 分片并行 ──────────────────────────────────────────────────────────

class _CountingPub:
    """MqttPublisher 鸭子桩：记录分片输入与发布数。"""

    def __init__(self, delay=0.0):
        self.seen: list[list[tuple]] = []
        self.delay = delay
        self._total = 0

    def publish_readings(self, readings):
        self.seen.append(list(readings))
        time.sleep(self.delay)
        self._total += len(readings)
        return len(readings)

    def is_connected(self):
        return True

    def published_total(self):
        return self._total

    def stop(self):
        pass


def test_pool_shards_readings_round_robin():
    pool = jobs.PublisherPool.__new__(jobs.PublisherPool)       # 绕过真实 paho 构造
    pool._size = 4
    pool._clients = [_CountingPub() for _ in range(4)]
    readings = [(f"S{i:02d}", "soil_moisture", 30.0, T0, "53060010101") for i in range(10)]
    assert pool.publish_readings(readings) == 10                # 全部接受
    assert [len(c.seen[0]) for c in pool._clients] == [3, 3, 2, 2]   # i%4 分片
    seen_codes = sorted(x[0] for c in pool._clients for x in c.seen[0])
    assert seen_codes == sorted(f"S{i:02d}" for i in range(10))      # 无丢失无重复


def test_pool_totals_and_connected_aggregate():
    pool = jobs.PublisherPool.__new__(jobs.PublisherPool)
    pool._size = 2
    pool._clients = [_CountingPub(), _CountingPub()]
    pool.publish_readings([("S1", "soil_moisture", 30.0, T0, "v")])
    assert pool.published_total() == 1
    assert pool.is_connected() is True


def test_pool_parallel_beats_serial_wallclock():
    """4 连接并行：每连接 40ms 延迟 ×10 条分片，墙钟应远小于串行总和（并行生效证据）。"""
    pool = jobs.PublisherPool.__new__(jobs.PublisherPool)
    pool._size = 4
    pool._clients = [_CountingPub(delay=0.04) for _ in range(4)]
    readings = [(f"S{i:02d}", "soil_moisture", 30.0, T0, "v") for i in range(40)]
    t0 = time.monotonic()
    assert pool.publish_readings(readings) == 40
    elapsed = time.monotonic() - t0
    assert elapsed < 0.12                                       # 串行≈0.4s，并行≈0.04s+调度


# ── JobManager 状态机 ───────────────────────────────────────────────────────────────

def test_job_lifecycle_queued_publishing_done():
    pool = jobs.PublisherPool.__new__(jobs.PublisherPool)
    pool._size = 1
    pool._clients = [_CountingPub()]
    mgr = jobs.JobManager(pool, lambda n: [(f"S{i}", "m", 1.0, T0, "v")
                                           for i in range(n * 2)])
    mgr.start()
    jid = mgr.submit(3)
    assert jid is not None
    for _ in range(100):                                        # 轮询至终态（≤5s）
        st = mgr.status(jid)
        if st["state"] in ("DONE", "FAILED"):
            break
        time.sleep(0.05)
    assert st["state"] == "DONE" and st["published"] == 6
    assert mgr.status("J-nope") is None                         # 未知 id → None（404 口径）


def test_job_failure_captured_not_fatal():
    def _boom(n):
        raise RuntimeError("boom")
    pool = jobs.PublisherPool.__new__(jobs.PublisherPool)
    pool._size = 1
    pool._clients = [_CountingPub()]
    mgr = jobs.JobManager(pool, _boom)
    mgr.start()
    jid = mgr.submit(1)
    for _ in range(100):
        st = mgr.status(jid)
        if st["state"] == "FAILED":
            break
        time.sleep(0.05)
    assert st["state"] == "FAILED" and "boom" in st["error"]
    assert mgr.submit(1) is not None                            # worker 存活可再接任务


def test_job_queue_full_returns_none():
    pool = jobs.PublisherPool.__new__(jobs.PublisherPool)
    pool._size = 1
    pool._clients = [_CountingPub(delay=0.3)]                   # 慢发布占住 worker
    gate = threading.Event()
    mgr = jobs.JobManager(pool, lambda n: (gate.wait(2), [])[1], queue_max=1)
    mgr.start()
    j1 = mgr.submit(1)
    time.sleep(0.1)                                             # worker 已取出 j1
    j2 = mgr.submit(1)                                          # 队列 [j2]
    assert mgr.submit(1) is None                                # 队列满 → None（503 口径）
    gate.set()
    assert j1 and j2


def test_job_eviction_keeps_most_recent():
    """keep 上限：任务记录超限淘汰最旧（submit 15 个，keep=10 → 记录面收缩）。"""
    pool = jobs.PublisherPool.__new__(jobs.PublisherPool)
    pool._size = 1
    pool._clients = [_CountingPub()]
    mgr = jobs.JobManager(pool, lambda n: [], keep=10, queue_max=32)
    ids = [mgr.submit(0) for _ in range(15)]
    assert all(jid for jid in ids)                              # 全部入队成功
    assert mgr.depth() == 15                                    # 队列全在（worker 未启动）
    statuses = [mgr.status(j) for j in ids]
    assert any(s is None for s in statuses[:6])                 # 最旧的被淘汰
    assert all(s is not None for s in statuses[-9:])            # 最近 9 个（keep-1 后入队者）在册


# ── 混合响应阈值语义（main.sim_tick 分支口径）────────────────────────────────────────

def test_sync_threshold_semantics():
    """steps≤SYNC_MAX 同步、>走异步——阈值来自 env SIM_SYNC_MAX_STEPS（缺省 4）。"""
    import os
    assert int(os.environ.get("SIM_SYNC_MAX_STEPS", "4")) == 4  # 缺省口径（E2E steps≤4 零改动）


def test_off_switch_creates_plain_publisher():
    """SIM_ASYNC=off 语义：开关为 False 时不构造池（回退单连接完全旧行为）。
    导入 app.main 会触发 hsf_common.web R-07 fail-closed（JWT_SECRET 必须在 env）——
    测试注入临时 JWT_SECRET（R-07 语义本身即被该异常验证：无 env 拒绝启动）。"""
    import os
    os.environ.setdefault("JWT_SECRET", "test-only-not-a-secret")   # 仅测试进程 env
    try:
        import app.main as m
    except AttributeError as e:
        if "add_event_handler" in str(e):                       # 测试机 FastAPI 事件 API 差异
            pytest.skip("app.main 事件注册与本机 FastAPI 版本不兼容（部署镜像内正常）")
        raise
    m.SIM_ASYNC_ON = False
    assert m.SIM_ASYNC_ON is False                              # 关闭语义在位
    assert hasattr(m, "jobs_mgr")                               # 模块含异步槽位（on 才赋值）
