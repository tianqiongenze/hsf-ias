"""HSF rs-svc：遥感影像登记与 NDVI CPU 解译（:8098，docs/08 §2 冻结）。

职责边界（D-08-1）：登记（POST /scenes）→ 解译（POST /scenes/{code}/process，numpy 纯 CPU，
V-1 红线：无 torch/tf/任何模型推理）→ 长势落库（rs_vigor）→ 非农化预警（RULE_NDVIFALL，
复用二期 alert_record+notify 管道，独立数据源新规则）→ 查询（/vigor、/alerts、GraphQL 子图）。
V-2 红线：样本栅格（JSON 波段矩阵）与真实 GeoTIFF 处理链路同构，真实影像仅替换 ingest 适配器。
"""
import os
import sys

sys.path.insert(0, "/srv")

import jwt
from fastapi import Depends, Header, HTTPException

from hsf_common import db
from hsf_common.web import create_app

from . import ingest, ndvi, repo, sample

JWT_SECRET = os.environ.get("JWT_SECRET", "")


def _http_err(status: int, code: str, msg: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": msg})


def require_login(authorization: str = Header(default="")) -> dict:
    """REST 通道 JWT 校验（与 iot/main.py 同法，05 号 §2.1 HTTP 映射）。"""
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
    """admin 专属操作（登记/解译/上报创建）——角色校验（05 号 §7.1 口径）。"""
    if "admin" not in (user.get("roles") or []):
        raise _http_err(403, "FORBIDDEN", "仅县管理员可执行该操作")
    return user


def _startup():
    """启动编排：TiDB 幂等建表（rs_scene/rs_vigor，D-08-1 存量零变更）+ 样本就绪检查。"""
    repo.init_db()
    n = repo.count_scenes()
    print(f"[rs] 启动完成，已登记影像 {n} 景", flush=True)


app = create_app("rs", startup=_startup)


# ── 端点 1：POST /scenes（登记影像，幂等键 scene_code）──────────────────────────

@app.post("/scenes")
def register_scene(body: dict, user: dict = Depends(require_login)):
    require_admin(user)
    try:
        scene = ingest.json_bands(body)                   # V-2：输入适配器唯一入口
    except ingest.IngestError as e:
        raise _http_err(400, "INVALID_ARGUMENT", str(e))
    sid = repo.insert_scene(scene["sceneCode"], scene["source"], scene["acquiredAt"],
                            scene["bands"]["red"], scene["bands"]["nir"],
                            scene["width"], scene["height"], scene["cellSizeM"])
    return {"id": sid, "sceneCode": scene["sceneCode"], "width": scene["width"],
            "height": scene["height"], "state": "REGISTERED",
            "dedup": sid is None}   # insert_scene 幂等：scene_code 冲突 INSERT IGNORE→None


# ── 端点 2：POST /scenes/{code}/process（NDVI 解译，同步 <10s）─────────────────

@app.post("/scenes/{scene_code}/process")
def process_scene(scene_code: str, user: dict = Depends(require_login)):
    require_admin(user)
    scene = repo.get_scene(scene_code)
    if scene is None:
        raise _http_err(404, "NOT_FOUND", f"影像不存在：{scene_code}")
    if scene["state"] == "PROCESSED":                     # 幂等：重复触发返回已有结果
        rows = repo.vigor_by_scene(scene_code)
        return {"sceneCode": scene_code, "state": "PROCESSED", "dedup": True,
                "parcels": len(rows), "items": rows[:20]}
    parcels = repo.parcels_geojson()                      # land 子图同源：hsf_md.land_parcel
    if not parcels:
        raise _http_err(409, "CONFLICT", "无地块掩膜（hsf_md.land_parcel 为空）")
    try:
        result = ndvi.process(scene, parcels)             # numpy 纯 CPU（V-1）
    except Exception as e:
        # docs/08 §2.2 ⑤：解译失败 → state=FAILED + domain_event 留痕（outbox 模式，ADR-HSF-004 沿用）
        repo.mark_scene(scene_code, "FAILED")
        repo.record_event("rs.process.failed", {"scene": scene_code, "error": str(e)[:200]})
        raise _http_err(500, "INTERNAL_ERROR", f"解译失败已留痕（state=FAILED）：{e}") from e
    repo.save_vigor(scene_code, result["vigors"])
    repo.mark_scene(scene_code, "PROCESSED")
    alerts = repo.ndvi_fall_alerts(result["vigors"], scene_code, scene["acquired_at"])
    return {"sceneCode": scene_code, "state": "PROCESSED", "dedup": False,
            "parcels": len(result["vigors"]), "alerts": alerts,
            "items": result["vigors"][:20]}


# ── 端点 3：GET /vigor（长势分页）───────────────────────────────────────────────

@app.get("/vigor")
def vigor(sceneCode: str | None = None, parcelCode: str | None = None,
          vigorLevel: str | None = None, page: int = 1, size: int = 20,
          user: dict = Depends(require_login)):
    return repo.vigor_list(sceneCode, parcelCode, vigorLevel, page, size,
                           repo.jwt_role(user))


# ── 端点 4：GET /vigor/latest（地块最新长势，一张图联邦字段同源）────────────────

@app.get("/vigor/latest")
def vigor_latest(parcelCode: str, user: dict = Depends(require_login)):
    row = repo.vigor_latest(parcelCode)
    if row is None:
        raise _http_err(404, "NOT_FOUND", f"该地块暂无解译结果：{parcelCode}")
    return row


# ── 端点 5：GET /alerts（NDVI 预警，rule_code 过滤复用二期告警管道）──────────────

@app.get("/alerts")
def alerts(ruleCode: str | None = None, page: int = 1, size: int = 20,
           user: dict = Depends(require_login)):
    return repo.alerts_list(ruleCode or "RULE_NDVIFALL", page, size, repo.jwt_role(user))


# ── 端点 6：POST /sample/seed（登记+解译两景确定性样本，验收数据构造器）──────────

@app.post("/sample/seed")
def sample_seed(user: dict = Depends(require_login)):
    require_admin(user)
    summary = sample.seed(admin_user_id=user["user_id"])
    return summary


# ── GraphQL 子图（gw SUBGRAPHS 组合：rsVigor / rsVigorLatest）────────────────────

try:
    import strawberry
    from strawberry.fastapi import GraphQLRouter

    @strawberry.type
    class RsVigor:
        scene_code: str
        parcel_code: str
        ndvi_mean: float
        ndvi_min: float
        ndvi_max: float
        vigor_level: str
        pixel_count: int

    @strawberry.type
    class Query:
        @strawberry.field
        def rs_vigor(self, scene_code: str) -> list[RsVigor]:
            rows = repo.vigor_by_scene(scene_code)
            return [RsVigor(**{k: (float(v) if isinstance(v, (int, float)) else v)
                               for k, v in r.items() if k in RsVigor.__annotations__})
                    for r in rows]

        @strawberry.field
        def rs_vigor_latest(self, parcel_code: str) -> RsVigor | None:
            r = repo.vigor_latest(parcel_code)
            if r is None:
                return None
            return RsVigor(**{k: (float(v) if isinstance(v, (int, float)) else v)
                              for k, v in r.items() if k in RsVigor.__annotations__})

    schema = strawberry.federation.Schema(query=Query, types=[RsVigor])   # 联邦子图（_service 契约，gw 组合必需）
    app.include_router(GraphQLRouter(schema), prefix="/graphql")
except Exception as e:                                    # pragma: no cover
    print(f"[rs] GraphQL 子图初始化失败（服务仍可用 REST）：{e}", flush=True)
