"""ratelimit TDD：固定窗口语义（allowed/remain/窗口滚动/键格式/TTL）+ 端点契约。

全部用内存 fake（任务口径：fakeredis 或 mock；docs/07 §6.2 冻结用例面：cost=1/1200、
窗口翻分钟重置、cap 边界 =cap 放行/=cap+1 拒绝）——不依赖任何真实 Redis/网络，CI 可离线跑。
FakeRedis.eval 模拟冻结 Lua 脚本语义（INCRBY + 本窗口首次写入时 EXPIRE，docs/07 §5.1）。
"""
import pytest
from fastapi.testclient import TestClient

from app import main as rl_main
from app.limiter import COUNT_SCRIPT, TTL_SECONDS, FixedWindowLimiter


class FakeRedis:
    """内存版最小 Redis：仅 limiter/readyz 依赖的最小面。

    eval 按冻结 Lua 脚本语义模拟：cur=INCRBY(key,cost)；cur==cost（首写）时 EXPIRE(key,ttl)。
    eval_calls 计数用于断言"每命中一次 = 单次 RTT"（脚本原子性，docs/07 §9-⑥）。
    """

    def __init__(self):
        self.store, self.ttls, self.eval_calls = {}, {}, 0

    def eval(self, script, numkeys, key, *args):
        assert script == COUNT_SCRIPT  # 端到端走冻结脚本常量，防实现与语义漂移
        cost, ttl = int(args[0]), int(args[1])
        self.store[key] = self.store.get(key, 0) + cost
        if self.store[key] == cost:
            self.ttls[key] = ttl
        self.eval_calls += 1
        return self.store[key]

    def ping(self):
        return True


BASE_MIN = 1_700_000_000 // 60  # 固定基准分钟 epoch（可复现）
NOW = BASE_MIN * 60 + 30.0      # 固定基准取窗口中段，避免交界歧义


# ── 核心：FixedWindowLimiter 固定窗口语义 ────────────────────────────────────

def test_first_hit_allowed_and_remain_minus_cost():
    lim = FixedWindowLimiter(10)
    r = lim.hit(FakeRedis(), "u:1", 1, now=NOW)
    assert r == {"allowed": True, "remain": 9}


def test_cap_boundary_frozen():
    """cap 边界（§6.2 冻结）：第 cap 次放行、cap+1 次拒绝且 remain=0（拒绝计数仍累计）。"""
    lim, fake = FixedWindowLimiter(3), FakeRedis()
    assert lim.hit(fake, "u:1", 1, now=NOW) == {"allowed": True, "remain": 2}
    assert lim.hit(fake, "u:1", 1, now=NOW) == {"allowed": True, "remain": 1}
    assert lim.hit(fake, "u:1", 1, now=NOW) == {"allowed": True, "remain": 0}
    assert lim.hit(fake, "u:1", 1, now=NOW) == {"allowed": False, "remain": 0}
    assert fake.store[lim.redis_key("u:1", BASE_MIN)] == 4


def test_cost_1200_fills_window_in_one_rtt():
    """cost>1 验证口径（§5.4 步骤 2/3）：cost=1200 单次打满窗口，再 cost=1 即拒绝。"""
    lim, fake = FixedWindowLimiter(1200), FakeRedis()
    assert lim.hit(fake, "u:9", 1200, now=NOW) == {"allowed": True, "remain": 0}
    assert lim.hit(fake, "u:9", 1, now=NOW) == {"allowed": False, "remain": 0}
    assert fake.eval_calls == 2  # 每命中一次恰一次脚本调用（单 RTT）


def test_cost_sums_across_hits():
    """cost=N 一次扣减 N（等价 N 请求占用）：remain=cap-cost。"""
    lim, fake = FixedWindowLimiter(10), FakeRedis()
    assert lim.hit(fake, "u:2", 5, now=NOW) == {"allowed": True, "remain": 5}
    assert lim.hit(fake, "u:2", 6, now=NOW) == {"allowed": False, "remain": 0}


def test_key_format_frozen():
    """键格式冻结（T-2/§5.1）：hsf:rl:{key}:{分钟epoch}，epoch=int(unix秒//60)。"""
    lim = FixedWindowLimiter(10)
    assert lim.redis_key("u:42", lim.minute_epoch(NOW)) == f"hsf:rl:u:42:{BASE_MIN}"
    assert lim.redis_key("ip:10.167.2.175", lim.minute_epoch(NOW)) == f"hsf:rl:ip:10.167.2.175:{BASE_MIN}"


def test_ttl_set_once_on_first_write():
    """TTL 120（§5.1 冻结）：仅本窗口首次写入（cur==cost）时 EXPIRE——窗口键翻分钟后不复用，
    一次置位 + 120s>60s 窗长足以自清；窗口内后续命中不再刷新。"""
    lim, fake = FixedWindowLimiter(10), FakeRedis()
    lim.hit(fake, "u:3", 1, now=NOW)
    lim.hit(fake, "u:3", 1, now=NOW)  # 非首写：不再 EXPIRE
    rk = lim.redis_key("u:3", BASE_MIN)
    assert fake.ttls[rk] == TTL_SECONDS == 120
    lim.hit(fake, "u:3", 1, now=NOW)
    assert fake.ttls[rk] == 120  # 保持首次置位值


def test_window_rollover_resets_count():
    """分钟滚动即新键：count 归零重新允许，旧键保持原值（由 TTL 自清）。"""
    lim, fake = FixedWindowLimiter(2), FakeRedis()
    assert lim.hit(fake, "u:4", 1, now=NOW) == {"allowed": True, "remain": 1}
    assert lim.hit(fake, "u:4", 1, now=NOW) == {"allowed": True, "remain": 0}
    assert lim.hit(fake, "u:4", 1, now=NOW) == {"allowed": False, "remain": 0}
    nxt = lim.hit(fake, "u:4", 1, now=NOW + 60)  # 下一分钟 epoch
    assert nxt == {"allowed": True, "remain": 1}
    assert lim.redis_key("u:4", BASE_MIN) in fake.store
    assert lim.redis_key("u:4", BASE_MIN + 1) in fake.store


def test_invalid_cost_rejected():
    """cost<1 属调用方编程错误：核心层直接 ValueError（端点层另有 422 拦截）。"""
    lim = FixedWindowLimiter(10)
    with pytest.raises(ValueError):
        lim.hit(FakeRedis(), "u:1", 0, now=NOW)
    with pytest.raises(ValueError):
        lim.hit(FakeRedis(), "u:1", -3, now=NOW)


def test_keys_isolated_between_callers():
    """不同 key 各自独立窗口（gw 按 user.sub / ip 分键的语义前提）。"""
    lim, fake = FixedWindowLimiter(1), FakeRedis()
    assert lim.hit(fake, "u:1", 1, now=NOW) == {"allowed": True, "remain": 0}
    assert lim.hit(fake, "u:2", 1, now=NOW) == {"allowed": True, "remain": 0}
    assert lim.hit(fake, "u:1", 1, now=NOW)["allowed"] is False


# ── 端点契约：/limit /healthz /readyz /metrics ──────────────────────────────

@pytest.fixture()
def api(monkeypatch):
    """注入内存 fake：core 限流器 cap=2 便于复现 429 语义；单一 FakeRedis 贯穿三次请求。"""
    fake = FakeRedis()
    monkeypatch.setattr(rl_main, "limiter", FixedWindowLimiter(2))
    monkeypatch.setattr(rl_main, "get_client", lambda: fake)
    return TestClient(rl_main.app)


def test_endpoint_limit_response_shape(api):
    r = api.get("/limit", params={"key": "u:9"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"allowed", "remain"}  # 冻结响应形态（T-2/§5.1）
    assert body == {"allowed": True, "remain": 1}


def test_endpoint_limit_reject_after_cap(api):
    for _ in range(2):
        api.get("/limit", params={"key": "u:9"})
    r = api.get("/limit", params={"key": "u:9"})
    assert r.status_code == 200
    assert r.json() == {"allowed": False, "remain": 0}  # 拒绝判定由 gw 转 429


def test_endpoint_limit_validation(api):
    """缺 key / 空 key / cost<1 → 422（gw 视非 200 一律 fail-open，不会误伤业务）。"""
    assert api.get("/limit").status_code == 422
    assert api.get("/limit", params={"key": ""}).status_code == 422
    assert api.get("/limit", params={"key": "u:9", "cost": 0}).status_code == 422


def test_endpoint_limit_503_when_backend_down(monkeypatch):
    """Redis 挂 → 503 且不谎报 allowed：gw 依 fail-open 放行（T-3）。"""
    def boom():
        raise RuntimeError("redis cluster down")
    monkeypatch.setattr(rl_main, "get_client", boom)
    with TestClient(rl_main.app) as c:
        r = c.get("/limit", params={"key": "u:1"})
    assert r.status_code == 503
    assert "allowed" not in r.json()


def test_healthz_no_backend_dependency(monkeypatch):
    """/healthz 不依赖 Redis（§5.1/CONVENTIONS §2.14）：Redis 挂也必须 200。"""
    def boom():
        raise RuntimeError("redis cluster down")
    monkeypatch.setattr(rl_main, "get_client", boom)
    with TestClient(rl_main.app) as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["svc"] == "ratelimit" and r.json()["status"] == "up"


def test_readyz_checks_redis(monkeypatch):
    """/readyz 校验后端：Redis 正常 → ready:true；挂 → 503（§5.1 探针口径）。"""
    monkeypatch.setattr(rl_main, "get_client", lambda: FakeRedis())
    with TestClient(rl_main.app) as c:
        assert c.get("/readyz").status_code == 200

    def boom():
        raise RuntimeError("redis cluster down")
    monkeypatch.setattr(rl_main, "get_client", boom)
    with TestClient(rl_main.app) as c:
        assert c.get("/readyz").status_code == 503


def test_metrics_prometheus_content_type(api):
    """/metrics 必须 PlainText+CONTENT_TYPE_LATEST（CONVENTIONS §2.8 真实事故）。"""
    r = api.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert b"hsf_http_requests_total" in r.content  # 指标命名与全平台统一（web.py 同名）
