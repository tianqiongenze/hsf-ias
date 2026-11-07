"""HSF sim-svc：IoT 模拟与时序告警（:8095，REST 非 GraphQL——05 号 §7/D-05-3）。

鉴权复用同一 JWT（Authorization: Bearer，HS256）；错误码 HTTP 映射见 05 号 §2.1。
机器/脚本流量直连（NodePort 31211），一期不做端点级限流（评审 R10）。
"""
import os
import sys

sys.path.insert(0, "/srv")

import jwt
from fastapi import Depends, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from hsf_common.web import create_app

from app import repo

app = create_app("sim")
# CORS 允许 web 源（03 号 §4.5 端点表；一期局域网内网，env 可收紧）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o],
    allow_methods=["*"], allow_headers=["*"],
)

# 与签发侧同源：hsf_common.web.JWT_SECRET（集群由 hsf-secrets 注入 JWT_SECRET env）
try:
    from hsf_common.web import JWT_SECRET as _ISSUER_SECRET
except Exception:
    _ISSUER_SECRET = ""
JWT_SECRET = os.environ.get("JWT_SECRET") or _ISSUER_SECRET


def _http_err(status: int, code: str, msg: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": msg})


def require_login(authorization: str = Header(default="")) -> dict:
    """REST 通道 JWT 校验：无 token/无效 → 401 UNAUTHENTICATED（05 号 §7.1）。"""
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token:
        raise _http_err(401, "UNAUTHENTICATED", "请先登录")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise _http_err(401, "UNAUTHENTICATED", "凭据无效或已过期")
    return {"user_id": int(payload.get("sub") or 0),
            "name": payload.get("name") or "", "roles": payload.get("roles") or []}


def _page(page: int, size: int) -> tuple[int, int]:
    """分页规约（05 号 §2.2）：page≥1，顶层 size≤100。"""
    return max(page, 1), min(max(size, 1), 100)


@app.on_event("startup")
def _init():
    repo.init_db()
    repo.seed_if_empty()  # 48 台设备 + 4 条规则 + 4 规则各 1 条种子告警（幂等双守卫）


# ── 端点 1：POST /sim/tick（FR-502）────────────────────────────────────────

@app.post("/sim/tick")
def sim_tick(steps: int = 1, user: dict = Depends(require_login)):
    return repo.tick(steps)


# ── 端点 2：GET /sim/sensors（FR-501）──────────────────────────────────────

@app.get("/sim/sensors")
def sim_sensors(villageCode: str | None = None, metric: str | None = None,
                page: int = 1, size: int = 20, user: dict = Depends(require_login)):
    page, size = _page(page, size)
    return repo.sensors_list(villageCode, metric, page, size)


# ── 端点 3：GET /sim/latest（FR-502③，快路径读 sensor.last_*）────────────────

@app.get("/sim/latest")
def sim_latest(villageCode: str | None = None, user: dict = Depends(require_login)):
    return repo.latest(villageCode)


# ── 端点 4：GET /sim/alerts（FR-504）───────────────────────────────────────

@app.get("/sim/alerts")
def sim_alerts(state: str | None = None, villageCode: str | None = None,
               page: int = 1, size: int = 20, user: dict = Depends(require_login)):
    page, size = _page(page, size)
    return repo.alerts_list(state, villageCode, page, size)


# ── 端点 5：GET /sim/alerts/statistics（FR-505）────────────────────────────

@app.get("/sim/alerts/statistics")
def sim_alerts_statistics(dim: str = "state", villageCode: str | None = None,
                          user: dict = Depends(require_login)):
    try:
        return repo.statistics(dim, villageCode)
    except ValueError as e:
        raise _http_err(400, "VALIDATION", str(e))


# ── 兼容别名（workflow 任务口径 GET /alerts、/alerts/statistics、/sensor/latest；
#    正式契约 = 05 号 §7.1 冻结端点，本组别名仅为任务文本对齐）───────────────────

@app.get("/alerts")
def alerts_alias(state: str | None = None, villageCode: str | None = None,
                 page: int = 1, size: int = 20, user: dict = Depends(require_login)):
    page, size = _page(page, size)
    return repo.alerts_list(state, villageCode, page, size)


@app.get("/alerts/statistics")
def alerts_statistics_alias(dim: str = "state", villageCode: str | None = None,
                            user: dict = Depends(require_login)):
    try:
        return repo.statistics(dim, villageCode)
    except ValueError as e:
        raise _http_err(400, "VALIDATION", str(e))


@app.get("/sensor/latest")
def sensor_latest_alias(villageCode: str | None = None, user: dict = Depends(require_login)):
    return repo.latest(villageCode)
