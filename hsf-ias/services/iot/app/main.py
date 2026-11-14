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
from hsf_common.web import create_app, decode_token

from . import decision, metrics, mqtt_sub, repo, sysmon, tsdb
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
        payload = decode_token(token)   # R-31：统一验签入口（HS256 冻结路径 / JWT_ALG=RS256 公钥验签）
    except Exception:
        raise _http_err(401, "UNAUTHENTICATED", "凭据无效或已过期")
    return {"user_id": int(payload.get("sub") or 0),
            "name": payload.get("name") or "", "roles": payload.get("roles") or []}


JWT_SECRET = os.environ.get("JWT_SECRET", "")

# ── 进程内组件（单副本固定；latest 缓存与回差状态机为进程内状态，D-07-1/A-07-3）──────
# R-01 分层后线程可见性：latest 缓存=刷写 worker 线程写（时间戳守卫）、REST 线程读；
# 回差状态机 engine=评估线程独占（EVAL_INLINE=1 回退时=唯一 worker），单线程串行不变。
engine = HysteresisEngine()
registry = repo.SensorRegistry()
latest = repo.LatestCache()
# R-01 有界缓冲背压（docs/24 §4 R-01 approach ③）：IOT_BUFFER_MAX 缺省 10 万条，
# 溢出丢弃+计数（防 TimescaleDB 长时间不可用叠加高速流的 OOM 风险）；0=无界=旧行为回退
buffer = mqtt_sub.ReadingsBuffer(maxlen=mqtt_sub.buffer_max(),
                                 on_drop=metrics.BUFFER_DROPPED.inc)
subscriber: mqtt_sub.MqttSubscriber | None = None
sysmon_monitor: sysmon.BrokerSysMonitor | None = None     # R-03 broker $SYS 监视（观测面）
_flusher_stop = threading.Event()


def _on_flush_error(e: Exception, batch_size: int) -> None:
    print(f"[iot] flush 失败（batch={batch_size}）整批回队重试：{e}", flush=True)


def _on_eval_error(e: Exception, batch_size: int) -> None:
    print(f"[iot] 评估失败（batch={batch_size}）整批回队重试：{e}", flush=True)


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
    """启动编排：建表/种子 → 台账镜像 → latest 冷启动回源 → 回差状态重建 → 订阅 + 刷写
    worker 池/评估线程（R-01）+ broker $SYS 监视（R-03）。"""
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
    global subscriber, sysmon_monitor
    subscriber = mqtt_sub.MqttSubscriber(buffer, registry, MQTT_HOST, MQTT_PORT,
                                         MQTT_USER, MQTT_PASS)
    subscriber.start()
    # R-01 刷写侧多线程化与评估旁路（docs/24 §4 R-01）：
    #   FLUSH_WORKERS：N 个刷写 worker 消费同一缓冲（缺省 1=等价旧行为保回退）；
    #   EVAL_INLINE=1：评估回内联（旧路径整体回退——此时强制单 worker，保回差状态机串行）；
    #   EVAL_QUEUE_MAX：旁路批次队列上限（缺省 64 批，满丢最旧+计数）。
    workers = max(1, int(os.environ.get("FLUSH_WORKERS", "1")))
    eval_inline = os.environ.get("EVAL_INLINE", "0") == "1"
    if eval_inline:
        workers = 1
    metrics.FLUSH_WORKERS_G.set(workers)
    eval_queue: repo.BatchQueue | None = None
    if not eval_inline:
        eval_queue = repo.BatchQueue(max(1, int(os.environ.get("EVAL_QUEUE_MAX", "64"))))
        threading.Thread(target=repo.evaluator_loop,
                         args=(eval_queue, engine, registry, IRR_PARAMS,
                               _flusher_stop, _on_eval_error),
                         name="iot-evaluator", daemon=True).start()
    for i in range(workers):
        threading.Thread(target=repo.flusher_loop,
                         args=(buffer, engine, registry, latest, IRR_PARAMS,
                               _flusher_stop, _on_flush_error, eval_queue),
                         name=f"iot-flusher-{i}", daemon=True).start()
    print(f"[iot] 刷写 worker={workers}（FLUSH_WORKERS） 评估旁路={'关(内联回退)' if eval_inline else '开'}"
          f" 缓冲上限={mqtt_sub.buffer_max()}（IOT_BUFFER_MAX）", flush=True)
    # R-03 broker 丢弃监控（观测面：失败不进 readyz、不影响收数链路；前置=sys_interval 与
    # $SYS 只读 ACL，k8s/30-mqtt.yaml R-03 增量，随 broker 滚动生效）
    try:
        sysmon_monitor = sysmon.BrokerSysMonitor(MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS)
        sysmon_monitor.start()
    except Exception as e:
        print(f"[iot] broker $SYS 监视启动失败（观测面降级）：{e}", flush=True)


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
    # 统一编码校验（docs/04 统一编码 + P3-3 收口）：注册端点仅受理传感器，设备码必须 S 前缀——
    # 非 S 前缀设备（如早期手工登记 DEV-*）会破坏上报契约（uplink 按 L/P/F/S 映射，docs/08 §3.1）
    if not device_code.startswith("S"):
        raise _http_err(400, "INVALID_ARGUMENT",
                        "deviceCode 必须为统一编码 S 前缀（传感器），收到：" + device_code[:32])
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
