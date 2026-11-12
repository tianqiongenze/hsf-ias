"""HSF iot-svc：IoT 接入与时序/智慧生产（:8097，REST 非 GraphQL——07 号 §4）。

职责边界（07 号 §2.5）：收数（MQTT 订阅）→ 判数（规则引擎回差）→ 决策（灌溉建议）→
落库（TimescaleDB 时序 + TiDB 告警/建议/通知）。sim 负责"造数与发数"，broker 只管转发。
REST 鉴权复用同一 JWT（Authorization: Bearer，HS256）；错误码 HTTP 映射见 05 号 §2.1。
单副本固定（latest 缓存与回差状态机为进程内状态，D-07-1/A-07-3），uvicorn workers=1。
"""
import os
import sys
import threading

sys.path.insert(0, "/srv")

import jwt
from fastapi import Depends, Header, HTTPException

from hsf_common import db
from hsf_common.web import create_app

from . import decision, mqtt_sub, repo, tsdb
from .rules import HysteresisEngine

MQTT_HOST = os.environ.get("MQTT_HOST", "hsf-mqtt")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "hsf")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

IRR_PARAMS = decision.load_params()


def _http_err(status: int, code: str, msg: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": msg})


def require_login(authorization: str = Header(default="")) -> dict:
    """REST 通道 JWT 校验：无 token/无效 → 401 UNAUTHENTICATED（05 号 §7.1 同法）。"""
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token:
        raise _http_err(401, "UNAUTHENTICATED", "请先登录")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise _http_err(401, "UNAUTHENTICATED", "凭据无效或已过期")
    return {"user_id": int(payload.get("sub") or 0),
            "name": payload.get("name") or "", "roles": payload.get("roles") or []}


JWT_SECRET = os.environ.get("JWT_SECRET", "")

# ── 进程内组件（单副本固定；flusher 串行消费，规则状态机/latest 缓存仅其线程可见）────
engine = HysteresisEngine()
registry = repo.SensorRegistry()
latest = repo.LatestCache()
buffer = mqtt_sub.ReadingsBuffer()
subscriber: mqtt_sub.MqttSubscriber | None = None
_flusher_stop = threading.Event()


def _on_flush_error(e: Exception, batch_size: int) -> None:
    print(f"[iot] flush 失败（batch={batch_size}）整批回队重试：{e}", flush=True)


def _readyz() -> str | None:
    """三连接就绪校验（docs/07 §4.1/E2E 用例 14）：TiDB + TimescaleDB + MQTT。"""
    try:
        db.query("SELECT 1")
    except Exception as e:
        return f"tidb: {e}"
    try:
        tsdb.ping()
    except Exception as e:
        return f"timescale: {e}"
    if subscriber is None or not subscriber.is_connected():
        return "mqtt: not connected"
    return None


def _startup():
    """启动编排：建表/种子 → 台账镜像 → latest 冷启动回源 → 回差状态重建 → 双线程起跑。"""
    repo.init_db()                     # TiDB 幂等建表（D-07-8：存量零变更）
    repo.seed_if_empty()               # alert_rule 4 条（uk_rule_code 幂等）
    registry.refresh()                 # 设备台账只读镜像（60s 定时 + 未知设备节流触发）
    try:
        latest.backfill()              # latest 冷启动回源 TimescaleDB DISTINCT ON（A-07-3）
    except Exception as e:
        print(f"[iot] latest 回源失败（TimescaleDB 未就绪时允许，flush 后自愈）：{e}", flush=True)
    try:
        repo.rebuild_tripped(engine)   # 重启重建：state≠CLOSED → TRIPPED（防重复告警）
    except Exception as e:
        print(f"[iot] 回差状态重建失败（alert_record 未就绪时允许）：{e}", flush=True)
    global subscriber
    subscriber = mqtt_sub.MqttSubscriber(buffer, registry, MQTT_HOST, MQTT_PORT,
                                         MQTT_USER, MQTT_PASS)
    subscriber.start()
    threading.Thread(target=repo.flusher_loop,
                     args=(buffer, engine, registry, latest, IRR_PARAMS,
                           _flusher_stop, _on_flush_error),
                     name="iot-flusher", daemon=True).start()


app = create_app("iot", startup=_startup, readyz=_readyz)


# ── 端点 1：GET /devices（docs/07 §4.6 契约 1）──────────────────────────────────

@app.get("/devices")
def devices(villageCode: str | None = None, metric: str | None = None,
            page: int = 1, size: int = 20, user: dict = Depends(require_login)):
    return repo.devices_list(villageCode, metric, page, size,
                             repo.jwt_role(user), user["user_id"])


# ── 端点 2：GET /sensor/latest（进程内 latest 缓存快路径，A-07-3）────────────────

@app.get("/sensor/latest")
def sensor_latest(villageCode: str | None = None, user: dict = Depends(require_login)):
    return {"items": latest.items(registry, villageCode)}


# ── 端点 3：GET /alerts（契约同 05 §7.1 端点 4）──────────────────────────────────

@app.get("/alerts")
def alerts(state: str | None = None, villageCode: str | None = None,
           page: int = 1, size: int = 20, user: dict = Depends(require_login)):
    return repo.alerts_list(state, villageCode, page, size,
                            repo.jwt_role(user), user["user_id"])


# ── 端点 4：GET /irrigation-advice（docs/07 §4.6 契约 4）─────────────────────────

@app.get("/irrigation-advice")
def irrigation_advice(state: str | None = None, parcelCode: str | None = None,
                      villageCode: str | None = None, page: int = 1, size: int = 20,
                      user: dict = Depends(require_login)):
    return repo.advice_list(state, parcelCode, villageCode, page, size,
                            repo.jwt_role(user), user["user_id"])


# ── 端点 5/6：GET /notify、POST /notify/{id}/read（docs/07 §4.6 契约 5/6）────────

@app.get("/notify")
def notify(unreadOnly: bool = False, page: int = 1, size: int = 20,
           user: dict = Depends(require_login)):
    return repo.notify_list(repo.jwt_role(user), user["user_id"], unreadOnly, page, size)


@app.post("/notify/{notify_id}/read")
def notify_read(notify_id: int, user: dict = Depends(require_login)):
    result, err = repo.notify_read(repo.jwt_role(user), notify_id)
    if err == "NOT_FOUND":
        raise _http_err(404, "NOT_FOUND", "通知不存在")
    if err:
        raise _http_err(403, "FORBIDDEN", "无权限操作该通知")
    return result


# ── 端点 7：POST /devices/register（三期 docs/08 §4 设备接入框架，D-08-4）────────

@app.post("/devices/register")
def devices_register(body: dict, user: dict = Depends(require_login)):
    if "admin" not in (user.get("roles") or []):
        raise _http_err(403, "FORBIDDEN", "仅县管理员可注册设备")
    device_code = str(body.get("deviceCode") or "").strip()
    metric = str(body.get("metric") or "").strip()
    village_code = str(body.get("villageCode") or "").strip()
    parcel_code = (str(body.get("parcelCode") or "").strip() or None)
    if not device_code or not metric or not village_code:
        raise _http_err(400, "INVALID_ARGUMENT", "缺少 deviceCode/metric/villageCode")
    if metric not in ("soil_moisture", "air_temp", "wind_speed", "rainfall",
                      "pest_index", "water_level"):
        raise _http_err(400, "INVALID_ARGUMENT", "metric 不在 04 §4.7 冻结 6 类之内")
    result = repo.register_device(device_code, metric, village_code, parcel_code)
    if result is None:
        raise _http_err(409, "CONFLICT", f"设备已注册（台账或凭证存在）：{device_code}")
    try:
        registry.maybe_refresh(force=True)   # 新设备立即进台账镜像（免等 60s 周期）
    except TypeError:
        registry.refresh()                   # 兼容无 force 参数签名
    return result
