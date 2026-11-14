"""HSF uplink-svc：全国农田建设综合监测监管平台上报通道（:8099，ClusterIP，docs/08 §3 冻结）。

职责边界（D-08-2/D-08-3）：统一编码（L/P/F/S）→ 上报粒度映射（PARCEL/PROJECT/FACILITY/SENSOR）
→ 上报任务状态机（PENDING→SENT→ACKED/REJECTED，uk_batch 幂等）→ payload 完整导出 JSON 归档。
V-3 红线：真实平台在内网不可达——transport.py 为唯一替换点（stub 回执），状态机/幂等/重试不变。
"""
import os
import sys

sys.path.insert(0, "/srv")

import jwt
from fastapi import Depends, Header, HTTPException

from hsf_common import db
from hsf_common.web import create_app

from . import assemble, metrics, repo, transport   # metrics 修复（2026-11-14 部署实测 NameError：main 引用 TASKS/SENDS 未导入）

JWT_SECRET = os.environ.get("JWT_SECRET", "")


def _http_err(status: int, code: str, msg: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": msg})


def require_login(authorization: str = Header(default="")) -> dict:
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token:
        raise _http_err(401, "UNAUTHENTICATED", "请先登录")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise _http_err(401, "UNAUTHENTICATED", "凭据无效或已过期")
    return {"user_id": int(payload.get("sub") or 0),
            "name": payload.get("name") or "", "roles": payload.get("roles") or []}


def require_admin(user: dict) -> dict:
    if "admin" not in (user.get("roles") or []):
        raise _http_err(403, "FORBIDDEN", "仅县管理员可执行该操作")
    return user


def _startup():
    repo.init_db()
    n = repo.count_tasks()
    print(f"[uplink] 启动完成，历史上报任务 {n} 批", flush=True)


app = create_app("uplink", startup=_startup)


# ── 端点 1：POST /tasks（创建上报任务，幂等 batch_code；payload 自主组装）─────────

@app.post("/tasks")
def create_task(body: dict, user: dict = Depends(require_login)):
    require_admin(user)
    batch_code = str(body.get("batchCode") or "").strip()
    if not batch_code:
        raise _http_err(400, "INVALID_ARGUMENT", "缺少 batchCode（批次编码，幂等键）")
    county = str(body.get("countyCode") or "530600")
    existing = repo.get_task(batch_code)
    if existing is not None:                              # 幂等：重复创建返回已有任务
        return {"batchCode": batch_code, "state": existing["state"],
                "itemCount": existing["item_count"], "dedup": True}
    payload = assemble.build_payload(county, batch=batch_code)   # reportBatch 回填（§3.1 契约）
    if not payload["items"]:
        raise _http_err(409, "CONFLICT", "无可上报数据（种子库为空）")
    task_id = repo.insert_task(batch_code, payload, len(payload["items"]))
    metrics.TASKS.inc(1)
    return {"batchCode": batch_code, "taskId": task_id, "state": "PENDING",
            "itemCount": len(payload["items"]),
            "byType": payload["summary"], "dedup": False}


# ── 端点 2：POST /tasks/{batch}/send（发送：transport 隔离层 + 状态机）────────────

@app.post("/tasks/{batch_code}/send")
def send_task(batch_code: str, user: dict = Depends(require_login)):
    require_admin(user)
    task = repo.get_task(batch_code)
    if task is None:
        raise _http_err(404, "NOT_FOUND", f"上报任务不存在：{batch_code}")
    if task["state"] in ("SENT", "ACKED"):                # 幂等：已发送不重发（回执留档）
        return {"batchCode": batch_code, "state": task["state"], "dedup": True,
                "receipt": task["receipt"]}
    payload = task["payload"]
    receipt, ok = transport.send(payload)                 # V-3：stub/真实平台唯一替换点
    repo.mark_sent(batch_code, receipt, ok)
    if ok:
        repo.mark_acked(batch_code, receipt)
        state = "ACKED"
    else:
        state = "REJECTED"
    metrics.SENDS.labels(state).inc()
    return {"batchCode": batch_code, "state": state, "dedup": False, "receipt": receipt}


# ── 端点 3：GET /tasks（分页）与 4：GET /tasks/{batch}/payload（导出归档）──────────

@app.get("/tasks")
def tasks(state: str | None = None, page: int = 1, size: int = 20,
          user: dict = Depends(require_login)):
    return repo.tasks_list(state, page, size)


@app.get("/tasks/{batch_code}/payload")
def task_payload(batch_code: str, user: dict = Depends(require_login)):
    task = repo.get_task(batch_code)
    if task is None:
        raise _http_err(404, "NOT_FOUND", f"上报任务不存在：{batch_code}")
    return task["payload"]
