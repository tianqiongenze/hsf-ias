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

# R-07（2026-11-14）：JWT_SECRET 外置 fail-closed——env 缺失/空值导入即抛异常，禁止源码内置回退密钥
JWT_SECRET = os.environ.get("JWT_SECRET", "")
if not JWT_SECRET:
    raise RuntimeError("env JWT_SECRET 未设置或为空（R-07 fail-closed：值由 hsf-secrets 注入，"
                       "部署期经 scripts/gen-secrets.sh 生成，禁止源码内置回退密钥）")

# R-31（2026-11-14，ADR-HSF-027）：JWT 签名算法演进——HS256 冻结兼容开关 / RS256 非对称签名。
# JWT_ALG=HS256（默认，docs/05 §1.4 冻结路径逐字节不变）；JWT_ALG=RS256 时私钥签发（iam）/
# 公钥验签（gw 与 REST 服务），kid 头支持多公钥轮换。密钥经 hsf-secrets 注入（gen-secrets.sh
# RSA-2048 生成；Secret 值=PEM 的 base64 单行编码——kubectl --from-env-file 不接受多行），
# prod/dev 分值（R-07 口径延续：签名密钥按命名空间独立）。HS256 兼容开关=JWT_ALG 键值，
# 回退即改 Secret 该键滚动（ADR-HSF-027 影响列）。
JWT_ALG = os.environ.get("JWT_ALG", "HS256").upper()
JWT_KID = os.environ.get("JWT_KID", "hsf-jwt-1")


def _pem(env_name: str) -> str:
    """Secret 键值（PEM 的 base64 单行）→ PEM 文本；空值 → 空串。"""
    import base64 as _b64
    raw = os.environ.get(env_name, "")
    if not raw:
        return ""
    try:
        return _b64.b64decode(raw).decode("utf-8")
    except Exception as e:
        raise RuntimeError(f"env {env_name} 非 base64 编码的 PEM（R-31 口径：值=PEM 的 base64 单行）: {e}")


JWT_PRIVATE_KEY = _pem("JWT_PRIVATE_KEY")
JWT_PUBLIC_KEY = _pem("JWT_PUBLIC_KEY")
JWT_PUBLIC_KEY_PREV = _pem("JWT_PUBLIC_KEY_PREV")   # 轮换期旧公钥（可空：kid 匹配才启用）
JWT_KID_PREV = os.environ.get("JWT_KID_PREV", "")
if JWT_ALG == "RS256" and not JWT_PRIVATE_KEY:
    raise RuntimeError("JWT_ALG=RS256 但 JWT_PRIVATE_KEY 未注入（R-31 fail-closed：签发方必须持私钥）")


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


def issue_token(sub, name: str, roles: list, ttl_seconds: int = 7200) -> str:
    """签发 JWT：PyJWT≥2.10 强制 sub 必须为字符串（CONVENTIONS §2.1）。
    TTL=7200s（2h）：R-08 决策 A（2026-11-14，改实现对齐声明）——原实现 86400×15（15 天）与
    docs/02"15min"声明矛盾 1440 倍；取 2h 兼顾等保鉴别时效（相对 15 天缩短 180 倍）与
    E2E/k6/演示窗口免重登（刷新令牌流 b 方案改动大，不做——显式声明）。ADR-HSF-019 补登。
    R-31（ADR-HSF-027）：JWT_ALG=HS256（默认）→ 冻结路径逐字节不变；RS256 → 私钥签发 +
    kid 头（gw 按 kid 选公钥，支持轮换）。"""
    claims = {"sub": str(sub), "name": name, "roles": roles,
              "exp": int(time.time()) + ttl_seconds}
    if JWT_ALG == "RS256":
        return jwt.encode(claims, JWT_PRIVATE_KEY, algorithm="RS256", headers={"kid": JWT_KID})
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict:
    """REST 通道本地验签（R-31 统一入口：iot/sim/rs/uplink 的 require_login 共用）。

    JWT_ALG=HS256（默认）→ JWT_SECRET 对称验签（冻结路径不变）；RS256 → 公钥验签
    （REST 服务只持公钥——私钥不出 iam，泄露面收敛为"单服务泄露即可伪造令牌"到
    "私钥仅存于签发方"）。kid 轮换：RS256 模式 PyJWT 默认不支持按 kid 选钥——本服务侧
    仅信任当前公钥（轮换窗口内旧令牌过期时间 2h 远短于轮换窗，双公钥仅 gw 需要）。
    验签失败抛 PyJWT 异常（调用方按 401 UNAUTHENTICATED 处理，语义不变）。"""
    if JWT_ALG == "RS256":
        if not JWT_PUBLIC_KEY:
            raise RuntimeError("JWT_ALG=RS256 但 JWT_PUBLIC_KEY 未注入（R-31 fail-closed：验签方必须持公钥）")
        return jwt.decode(token, JWT_PUBLIC_KEY, algorithms=["RS256"])
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])


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
