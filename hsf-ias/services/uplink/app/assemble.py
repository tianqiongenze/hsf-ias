"""上报 payload 组装（docs/08 §3.1 冻结映射：统一编码前缀→监管平台上报粒度）。

L/P/F/S → PARCEL/PROJECT/FACILITY/SENSOR；state 按枚举直传；面积/金额以登记值为准
（ADR-001"面积重算以登记值为准"口径延续）。从 hsf_md/hsf_biz 自主组装，不跨服务 RPC。
"""
import json

from hsf_common import db

_TYPE_MAP = {"L": "PARCEL", "P": "PROJECT", "F": "FACILITY", "S": "SENSOR"}


def map_type(code: str) -> str:
    """统一编码首字母 → 上报 type（docs/08 §3.1 D-08-2）。"""
    return _TYPE_MAP.get(code[:1], "OTHER")


def build_payload(county: str, batch: str = "") -> dict:
    """组装完整上报批次（items 仅含四类，summary 计数）。

    batch：批次编码回填 reportBatch（docs/08 §3.1 payload 契约首字段）——stub 校验
    "缺少 reportBatch" 即 REJECTED，创建任务时必须回填（docs/08 §3.2 状态闭环前提）。

    P3-3 收口（2026-11-14）：payload 契约（D-08-2）items 只允许 PARCEL/PROJECT/FACILITY/
    SENSOR 四类——历史数据中非统一编码设备（如 DEV-*）映射为 OTHER 后**排除出 items**，
    以 summary.EXCLUDED 与顶层 excludedOther 如实计数（不静默丢弃）；stub 校验规则不变
    （防御纵深：即使未来数据绕过排除也回执 REJECTED 而非带病上报）。"""
    items: list[dict] = []

    for r in db.query("SELECT code, name, state, build_year, content, applicant_org_code "
                      "FROM hsf_biz.farm_project ORDER BY code"):
        items.append({"type": map_type(r["code"]), "code": r["code"], "name": r["name"],
                      "state": r["state"], "buildYear": r["build_year"] or "",
                      "content": (r["content"] or "")[:80],
                      "applicantOrg": r["applicant_org_code"] or ""})

    for r in db.query("SELECT p.code, p.name, p.area_mu, p.holder_id, p.village_code, "
                      "pt.name AS holder_name, p.geo_json FROM hsf_md.land_parcel p "
                      "LEFT JOIN hsf_md.party pt ON pt.id=p.holder_id ORDER BY p.code"):
        geo_hint = _wkt_hint(r["geo_json"])
        items.append({"type": map_type(r["code"]), "code": r["code"], "name": r["name"],
                      "areaMu": float(r["area_mu"] or 0), "holderName": r["holder_name"] or "",
                      "villageCode": r["village_code"], "geoWktHint": geo_hint})

    for r in db.query("SELECT f.code, f.type, f.parcel_code, f.status, f.village_code "
                      "FROM hsf_md.facility f ORDER BY f.code"):
        items.append({"type": map_type(r["code"]), "code": r["code"], "kind": r["type"],
                      "parcelCode": r["parcel_code"], "state": r["status"],
                      "villageCode": r["village_code"]})

    excluded = 0
    for r in db.query("SELECT s.sensor_code, s.metric, s.village_code, s.parcel_code, s.status AS state "
                      "FROM hsf_biz.sensor s ORDER BY s.sensor_code"):
        if map_type(r["sensor_code"]) == "OTHER":
            excluded += 1   # P3-3：非统一编码设备不进 items（summary.EXCLUDED 计数留痕）
            continue
        items.append({"type": map_type(r["sensor_code"]), "code": r["sensor_code"],
                      "metric": r["metric"], "villageCode": r["village_code"],
                      # parcelCode=设备注册登记的地块关联（hsf_biz.sensor.parcel_code，docs/08 §3.1/§4）：
                      # 上级平台按地块聚合传感器，与 FACILITY 项同口径；可为空=未挂地块
                      "parcelCode": r["parcel_code"],
                      "state": r["state"]})

    summary: dict[str, int] = {}
    for it in items:
        summary[it["type"]] = summary.get(it["type"], 0) + 1
    if excluded:
        summary["EXCLUDED"] = excluded
    return {"reportBatch": batch, "countyCode": county, "items": items,
            "summary": summary,
            "excludedOther": excluded,   # P3-3 留痕：非统一编码条数（docs/08 §3.1 契约外数据）
            "schemaVersion": "1.1"}   # 1.0→1.1（2026-11-13 评审修订）：SENSOR 增 parcelCode


def _wkt_hint(geo_json: str) -> str:
    """geoWktHint：GeoJSON Polygon 前 3 点的 WKT 片段（上报粒度提示，非全量几何）。"""
    if not geo_json:
        return ""
    try:
        g = json.loads(geo_json)
        coords = (g.get("geometry") or {}).get("coordinates") or g.get("coordinates") or []
        ring = coords[0][:3] if coords else []
        pts = ",".join(f"{lon} {lat}" for lon, lat in ring)
        return f"POLYGON({pts},...)" if pts else ""
    except Exception:
        return ""


def validate_payload(payload: dict) -> list[str]:
    """stub 回执前的完整性校验（transport stub 使用；真实平台同规则联调）。"""
    errors: list[str] = []
    if not payload.get("reportBatch"):
        errors.append("缺少 reportBatch")
    if not payload.get("countyCode"):
        errors.append("缺少 countyCode")
    if not payload.get("items"):
        errors.append("items 为空")
    for it in payload.get("items") or []:
        if not it.get("code") or it.get("type") not in ("PARCEL", "PROJECT", "FACILITY", "SENSOR"):
            errors.append(f"item 编码或类型非法：{it.get('code')}/{it.get('type')}")
    return errors
