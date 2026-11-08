"""HSF ratelimit-svc：全局限流服务（:8096，CONVENTIONS-P2 T-1/T-2 冻结）。

gw 限流中间件改经 HTTP 调用本服务（fail-open：本服务不可用=网关放行+节流告警日志），
替代一期进程内令牌桶——桶按副本各自计数，全局容量=cap×副本数不可控（docs/20 §5.3 复测定位）。
判定：Redis Cluster 固定窗口 INCRBY(cost)+EXPIRE(120)，键 hsf:rl:{key}:{分钟epoch}；
count 超过窗口容量 → {allowed:false, remain:0}，gw 据此回一期冻结形态的 429 JSON。
Redis 故障时本服务回 503（不谎报 allowed），gw 收到非 200 即 fail-open 放行（T-3 口径）。
内网服务（00-base NetworkPolicy default-deny 仅同命名空间放行），无端点级鉴权。
"""
import logging
import os
import sys
import threading
import time

sys.path.insert(0, "/srv")

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from app.limiter import FixedWindowLimiter

REQ_TOTAL = Counter("hsf_http_requests_total", "HTTP请求总数", ["svc", "path", "code"])
REQ_LATENCY = Histogram("hsf_http_request_seconds", "HTTP延迟", ["svc", "path"])

# 窗口容量（req/min/键，docs/07 §5.1/§9-③ 二期冻结 1200；env 可调，manifest 注入）：
# 固定窗口下 429 确定性可复现（1500 突发 vs 1200 必触发），根除一期令牌桶"永打不穿"验收缺陷
RATE_CAP = int(os.environ.get("RATE_CAP") or "1200") or 1200

_logger = logging.getLogger("uvicorn.error")
_client = None
_client_lock = threading.Lock()


def get_client():
    """Redis Cluster 客户端懒初始化（同 hsf_common.cache._r 口径：Python 客户端对 hostname
    广播集群已实证可用，CONVENTIONS-P2 T-3 禁 ioredis）。探针先于 Redis 可用时不阻塞启动。"""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import redis
                startup = [
                    tuple(n.split(":")) for n in
                    os.environ.get("REDIS_URL", "hsf-redis-0.hsf-redis-h.infra.svc.cluster.local:6379").split(",")
                ]
                _client = redis.cluster.RedisCluster(
                    host=startup[0][0], port=int(startup[0][1]),
                    password=os.environ.get("REDIS_PASS", ""),
                    socket_timeout=2, socket_connect_timeout=2,
                    skip_full_coverage_check=True,
                )
    return _client


limiter = FixedWindowLimiter(RATE_CAP)
state = {"started": time.time()}

app = FastAPI(title="HSF ratelimit", docs_url=None, redoc_url=None)
# 同步端点跑在 starlette 线程池：/limit 为 gw 每请求热路径，提额对齐 hsf_common.web（docs/20 §3.4）
try:
    from anyio import to_thread
    to_thread.current_default_thread_limiter().total_tokens = 200
except Exception:
    pass


# ── 端点 1：GET /limit?key=&cost=（CONVENTIONS-P2 T-2 冻结契约）──────────────

@app.get("/limit")
def limit(key: str = Query(min_length=1), cost: int = Query(default=1, ge=1)):
    """扣减一次配额并判定，响应冻结形态 {allowed, remain}。"""
    try:
        return limiter.hit(get_client(), key, cost)
    except Exception as e:  # Redis 不可达/超时等 → 503：gw 收到非 200 即 fail-open 放行（T-3）
        _logger.warning("ratelimit backend unavailable: %r", e)
        raise HTTPException(status_code=503,
                            detail={"code": "RATELIMIT_UNAVAILABLE", "message": "限流后端暂不可用"})


# ── 探针与指标（口径同 hsf_common.web：/healthz 不依赖后端，/readyz 校验 Redis）──

@app.get("/healthz")
def healthz():
    return {"svc": "ratelimit", "status": "up", "uptime": int(time.time() - state["started"])}


@app.get("/readyz")
def readyz():
    try:
        get_client().ping()
    except Exception as e:
        return PlainTextResponse(f"redis: {e}", status_code=503)
    return {"ready": True, "backend": "redis-cluster"}


@app.get("/metrics")
def metrics():
    # CONVENTIONS §2.8：必须 PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
    # ——JSONResponse 会转义引号致 Prometheus 解析失败
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.middleware("http")
async def instrument(request, call_next):
    start = time.time()
    resp = await call_next(request)
    path = request.url.path
    REQ_TOTAL.labels("ratelimit", path, resp.status_code).inc()
    REQ_LATENCY.labels("ratelimit", path).observe(time.time() - start)
    return resp
