"""HSF Web 基建：应用工厂（健康/就绪/指标）、GraphQL 上下文与鉴权、JWT 签发。"""
import os
import time

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from . import db

REQ_TOTAL = Counter("hsf_http_requests_total", "HTTP请求总数", ["svc", "path", "code"])
REQ_LATENCY = Histogram("hsf_http_request_seconds", "HTTP延迟", ["svc", "path"])

JWT_SECRET = os.environ.get("JWT_SECRET", "HsfJwt-2026-bN8wQ4vXzA6sD1fG")  # 与 gw 同源（hsf-secrets）


def create_app(svc_name: str, startup=None, readyz=None) -> FastAPI:
    """readyz：可选自定义就绪校验（返回 None=就绪 / str=不就绪原因），缺省走 TiDB
    SELECT 1（既有服务零行为变化）；iot 用于三连接校验（TiDB+Timescale+MQTT，07 号 §4.1）。"""
    app = FastAPI(title=f"HSF {svc_name}", docs_url=None, redoc_url=None)
    # 同步 resolver 跑在 starlette 线程池：默认 anyio 限 40 tokens，200VU 压测即排队成秒级尾
    # （docs/20 §3.4 复测定位）。提额到 200×4副本=800 并发容量，DB 侧由 TiDB 连接数兜底。
    try:
        from anyio import to_thread
        to_thread.current_default_thread_limiter().total_tokens = 200
    except Exception:
        pass
    state = {"ready": False, "started": time.time()}

    @app.on_event("startup")
    async def _s():
        if startup:
            await startup() if hasattr(startup, "__await__") else startup()
        state["ready"] = True

    @app.get("/healthz")
    def healthz():
        return {"svc": svc_name, "status": "up", "uptime": int(time.time() - state["started"])}

    @app.get("/readyz")
    def ready_endpoint():
        if not state["ready"]:
            return PlainTextResponse("not ready", status_code=503)
        if readyz is not None:
            err = readyz()
            if err:
                return PlainTextResponse(str(err), status_code=503)
            return {"ready": True}
        try:
            db.query("SELECT 1")
        except Exception as e:
            return PlainTextResponse(f"db: {e}", status_code=503)
        return {"ready": True}

    @app.get("/metrics")
    def metrics():
        # §2.8：必须 Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)——JSONResponse 会转义引号致 Prometheus 解析失败
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def instrument(request: Request, call_next):
        start = time.time()
        resp = await call_next(request)
        path = request.url.path
        REQ_TOTAL.labels(svc_name, path, resp.status_code).inc()
        REQ_LATENCY.labels(svc_name, path).observe(time.time() - start)
        return resp

    return app


def gql_context(request: Request) -> dict:
    """GraphQL 上下文：信任网关注入的身份头（内网，05号文档 §1.4 安全模型）。
    x-user-name 由网关 Base64 编码（HTTP 头仅允许 ByteString），此处解码；兼容明文回退。"""
    import base64 as _b64
    _un = request.headers.get("x-user-name", "")
    try:
        _un = _b64.b64decode(_un, validate=True).decode("utf-8")
    except Exception:
        pass  # 明文直传（直连子图调试场景）
    return {
        "request": request,
        "user_id": int(request.headers.get("x-user-id") or 0),
        "user_name": _un,
        "user_roles": (request.headers.get("x-user-roles") or "").split(","),
    }


def issue_token(sub, name: str, roles: list, ttl_seconds: int = 86400 * 15) -> str:
    """签发 JWT：PyJWT≥2.10 强制 sub 必须为字符串（CONVENTIONS §2.1）。"""
    return jwt.encode({"sub": str(sub), "name": name, "roles": roles,
                       "exp": int(time.time()) + ttl_seconds}, JWT_SECRET, algorithm="HS256")


def require_user(info, roles: list = None):
    """鉴权断言：未登录/无角色 → GraphQL 业务错误（05号文档 §2.1 错误码）。"""
    ctx = info.context
    if not ctx.get("user_id"):
        raise Exception429("UNAUTHENTICATED", "请先登录")
    if roles and not set(roles) & set(ctx.get("user_roles") or []):
        raise Exception429("FORBIDDEN", "无权限执行该操作")
    return ctx


class Exception429(Exception):
    """业务错误载体（05号文档 §2.1）：graphql-core 会透传 .extensions，
    网关据此拿到 extensions.code（D-05-2：GraphQL 通道 HTTP 200 + errors[0].extensions.code）。"""

    def __init__(self, code: str, msg: str):
        self.code, self.msg = code, msg
        self.extensions = {"code": code}
        super().__init__(msg)
