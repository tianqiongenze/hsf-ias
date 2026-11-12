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


def build_payload(county: str) -> dict:
    """组装完整上报批次（items 按四类分组，summary 计数）。"""
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

    for r in db.query("SELECT s.sensor_code, s.metric, s.village_code, s.status AS state "
                      "FROM hsf_biz.sensor s ORDER BY s.sensor_code"):
        items.append({"type": map_type(r["sensor_code"]), "code": r["sensor_code"],
                      "metric": r["metric"], "villageCode": r["village_code"],
                      "state": r["state"]})

    summary: dict[str, int] = {}
    for it in items:
        summary[it["type"]] = summary.get(it["type"], 0) + 1
    return {"reportBatch": "", "countyCode": county, "items": items,
            "summary": summary, "schemaVersion": "1.0"}


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
