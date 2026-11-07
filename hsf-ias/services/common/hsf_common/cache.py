"""HSF 多级缓存：L1 进程内 TTL + L2 Redis Cluster（04号文档§8）。

模式：cache-aside；提供 singleflight 防击穿（进程内锁）、TTL ±20% 抖动防雪崩、
空值缓存（仅 L2 存 60s、不进 L1）防穿透；Redis 故障降级直查 DB 不阻塞业务（P8 原则）。
"""
import os
import random
import threading
import time
import typing as t

_K = t.TypeVar("_K")
_V = t.TypeVar("_V")

EMPTY_L2_TTL = 60  # 空值缓存仅 L2 存 60s（04 号 §8.2 键 12）


def _is_empty(val) -> bool:
    return val is None or (isinstance(val, (list, dict)) and not val)


class L1Cache:
    def __init__(self, maxsize: int = 10000):
        self._d: dict = {}
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def get(self, key: str):
        with self._lock:
            item = self._d.get(key)
            if not item:
                return None, False
            exp, val = item
            if exp < time.monotonic():
                self._d.pop(key, None)
                return None, False
            return val, True

    def set(self, key: str, val, ttl: float):
        with self._lock:
            if len(self._d) >= self._maxsize:
                self._d.clear()  # MVP 简化；生产用 LRU
            self._d[key] = (time.monotonic() + ttl, val)

    def delete(self, key: str):
        with self._lock:
            self._d.pop(key, None)

    def clear_prefix(self, prefix: str):
        with self._lock:
            for k in [k for k in self._d if k.startswith(prefix)]:
                self._d.pop(k, None)


l1 = L1Cache()

_redis = None
_redis_init_lock = threading.Lock()
_flight_locks: dict = {}
_flight_map_lock = threading.Lock()


def _r():
    global _redis
    if _redis is None:
        with _redis_init_lock:
            if _redis is None:
                import redis
                startup = [
                    tuple(n.split(":")) for n in
                    os.environ.get("REDIS_URL", "hsf-redis-0.hsf-redis-h.infra.svc.cluster.local:6379").split(",")
                ]
                _redis = redis.cluster.RedisCluster(
                    host=startup[0][0], port=int(startup[0][1]),
                    password=os.environ.get("REDIS_PASS", ""),
                    socket_timeout=2, socket_connect_timeout=2,
                    skip_full_coverage_check=True,
                )
    return _redis


def cached(key: str, ttl: float, loader: t.Callable[[], _V],
           l1_ttl: float = None, allow_empty: bool = True) -> _V:
    """cache-aside：L1 → L2 → loader（singleflight）→ 回填。TTL 自动 ±20% 抖动。"""
    jitter = lambda base: base * (0.8 + random.random() * 0.4)
    val, hit = l1.get(key)
    if hit:
        return val
    try:
        raw = _r().get(key)
        if raw is not None:
            import json
            val = json.loads(raw)
            if _is_empty(val):
                return val  # 空值命中直接返回，不进 L1（键 12 防穿透）
            l1.set(key, val, l1_ttl or ttl / 3)
            return val
    except Exception:
        pass  # Redis 故障降级为穿透（P8 原则：缓存不可用不阻塞业务）

    with _flight_map_lock:
        fl = _flight_locks.setdefault(key, threading.Lock())
    with fl:
        val, hit = l1.get(key)  # double-check
        if hit:
            return val
        val = loader()
        import json
        try:
            _r().setex(key, EMPTY_L2_TTL if _is_empty(val) else int(jitter(ttl)),
                       json.dumps(val, default=str, ensure_ascii=False))
        except Exception:
            pass
        if _is_empty(val):
            l1.delete(key)  # 空值只在 L2 存 60s 防穿透，不进 L1（04 号 §8.4）
        else:
            l1.set(key, val, l1_ttl or ttl / 3)
        return val


def invalidate(key: str):
    l1.delete(key)
    try:
        _r().delete(key)
    except Exception:
        pass


def invalidate_prefix(prefix: str):
    l1.clear_prefix(prefix)
    try:
        for k in _r().scan_iter(f"{prefix}*"):
            _r().delete(k)
    except Exception:
        pass


def counter(key: str, delta: int = 1) -> int:
    try:
        return int(_r().incrby(key, delta))
    except Exception:
        return -1
