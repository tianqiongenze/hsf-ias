"""上报 payload 组装（docs/08 §3.1 冻结映射：统一编码前缀→监管平台上报粒度）。

L/P/F/S → PARCEL/PROJECT/FACILITY/SENSOR；state 按枚举直传；面积/金额以登记值为准
（ADR-001"面积重算以登记值为准"口径延续）。从 hsf_md/hsf_biz 自主组装，不跨服务 RPC。

R-24（2026-11-14，schemaVersion 1.1→1.2，ADR-HSF-024）：PARCEL 项增 `geoWkt` 完整闭合环 WKT
（上图入库口径——对标①"与国土空间一张图互联互通"的矢量对齐），`geoWktHint` 保留兼容（v1.0/1.1
消费方不受影响）；顶层增 `phases` 三阶段批次元数据**字段预留**（planPhase/buildPhase/acceptPhase
摘要+逐级审核状态位——状态机不动，真实平台接入时由 transport 替换方按平台协议填充）。
"""
import json

from hsf_common import db

_TYPE_MAP = {"L": "PARCEL", "P": "PROJECT", "F": "FACILITY", "S": "SENSOR"}

# R-24：schemaVersion 演进（stub 校验允许历史归档 payload 重发——1.0/1.1 契约不破坏）
SCHEMA_VERSION = "1.2"
SCHEMA_VERSIONS_ALLOWED = ("1.0", "1.1", "1.2")
# R-24：payload 体积限值（验收口径"体积记录在案（<设定限值）"；单批 >限值 → stub REJECTED，
# 促成分批上报而非带病超载——地块数千级需分批/压缩，docs/24 §4 R-24 风险行）
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
# R-24：三阶段批次元数据（字段预留，docs/08 §3.1 增补）——status 位与 summary 均为占位常量，
# 不接任何审核状态机（D-08-3 状态机 PENDING→SENT→ACKED/REJECTED 语义不变）
_PHASES_RESERVED = {
    "planPhase": {"status": "PENDING", "summary": None},     # 计划阶段摘要+审核状态位（预留）
    "buildPhase": {"status": "PENDING", "summary": None},    # 实施阶段摘要+审核状态位（预留）
    "acceptPhase": {"status": "PENDING", "summary": None},   # 验收阶段摘要+审核状态位（预留）
}


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
                      "villageCode": r["village_code"], "geoWktHint": geo_hint,
                      # R-24（schemaVersion 1.2）：完整闭合环 WKT（上图入库口径）；
                      # geo_json 缺失/非法时为空串（geoWktHint 仍在，validate 按"至少一项"放行）
                      "geoWkt": _wkt_full(r["geo_json"])})

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
            # R-24（schemaVersion 1.2，ADR-HSF-024）：三阶段批次元数据字段预留（状态机不动）
            "phases": {k: dict(v) for k, v in _PHASES_RESERVED.items()},
            "schemaVersion": SCHEMA_VERSION}   # 1.0→1.1（2026-11-13：SENSOR 增 parcelCode）→
                                               # 1.2（2026-11-14，R-24）：PARCEL 增 geoWkt+phases 预留


def _wkt_hint(geo_json: str) -> str:
    """geoWktHint：GeoJSON Polygon 前 3 点的 WKT 片段（上报粒度提示，非全量几何；v1.2 保留兼容）。"""
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


def _wkt_full(geo_json: str) -> str:
    """geoWkt：GeoJSON Polygon 全量闭合环 WKT（R-24 上图入库口径，ADR-HSF-024）。

    序列化 land_parcel.geo_json 全部顶点；GeoJSON 规范（RFC 7946）允许未闭合环——首点不等于尾点时
    补首点闭合（WKT POLYGON 环必须显式闭合）。顶点 <3（不成环）或 geo_json 缺失/非法 → 空串
    （geoWktHint 兜底，validate 按"geoWkt/geoWktHint 至少一项"放行——与 hint 同源的降级语义）。"""
    if not geo_json:
        return ""
    try:
        g = json.loads(geo_json)
        coords = (g.get("geometry") or {}).get("coordinates") or g.get("coordinates") or []
        ring = coords[0] if coords else []
        pts = [(float(lon), float(lat)) for lon, lat in ring]
        if len(pts) >= 3 and pts[0] != pts[-1]:
            pts.append(pts[0])              # 闭合：补首点作尾点
        if len(pts) < 4:
            return ""                       # 闭合环最少 4 位置（3 顶点+闭合）
        return "POLYGON((" + ",".join(f"{lon} {lat}" for lon, lat in pts) + "))"
    except Exception:
        return ""


def check_wkt_closed(wkt: str) -> str:
    """stub 校验规则扩展（R-24）：geoWkt 必须为闭合 POLYGON 环（≥4 位置且首尾相同）。

    返回错误描述（""=通过）。只验环闭合与最少位置数，不做拓扑校验（ADR-HSF-001 影响列：
    应用层格式校验口径，与 land 侧抽查同深度）。"""
    if not isinstance(wkt, str) or not wkt.startswith("POLYGON((") or not wkt.endswith("))"):
        return "非 POLYGON((...)) 形态"
    body = wkt[len("POLYGON(("):-len("))")]
    try:
        pts = [tuple(float(x) for x in p.strip().split()) for p in body.split(",")]
    except (ValueError, AttributeError):
        return "坐标点解析失败"
    if len(pts) < 4:
        return f"环位置数 {len(pts)} < 4（不成闭合环）"
    if pts[0] != pts[-1]:
        return f"环未闭合（首点 {pts[0]} ≠ 尾点 {pts[-1]}）"
    return ""


def payload_size(payload: dict) -> int:
    """payload 序列化体积（字节）——R-24 验收口径"体积记录在案（<设定限值）"的度量口径。"""
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def validate_payload(payload: dict) -> list[str]:
    """stub 回执前的完整性校验（transport stub 使用；真实平台同规则联调）。

    R-24 扩展：① schemaVersion 白名单（1.0/1.1 历史归档 payload 重发放行——契约不破坏）；
    ② 1.2 必含 phases 三阶段预留结构（planPhase/buildPhase/acceptPhase，各含 status/summary）；
    ③ PARCEL 项须含空间矢量（geoWkt 或 geoWktHint 至少一项；geoWkt 须闭合环）；
    ④ 序列化体积 ≤ MAX_PAYLOAD_BYTES（超限 REJECTED 促成分批，不留档案外超大 payload）。"""
    errors: list[str] = []
    if not payload.get("reportBatch"):
        errors.append("缺少 reportBatch")
    if not payload.get("countyCode"):
        errors.append("缺少 countyCode")
    if not payload.get("items"):
        errors.append("items 为空")
    sv = payload.get("schemaVersion")
    if sv is not None and sv not in SCHEMA_VERSIONS_ALLOWED:
        errors.append(f"schemaVersion 非法：{sv}（允许 {', '.join(SCHEMA_VERSIONS_ALLOWED)}）")
    if sv == SCHEMA_VERSION:
        phases = payload.get("phases")
        if not isinstance(phases, dict) or set(phases) != set(_PHASES_RESERVED):
            errors.append("schemaVersion 1.2 缺 phases 三阶段结构（planPhase/buildPhase/acceptPhase）")
        else:
            for name, ph in phases.items():
                if not isinstance(ph, dict) or "status" not in ph or "summary" not in ph:
                    errors.append(f"phases.{name} 缺 status/summary 字段（预留结构）")
    for it in payload.get("items") or []:
        if not it.get("code") or it.get("type") not in ("PARCEL", "PROJECT", "FACILITY", "SENSOR"):
            errors.append(f"item 编码或类型非法：{it.get('code')}/{it.get('type')}")
            continue
        if it.get("type") == "PARCEL":     # R-24③：PARCEL 空间矢量完整性
            if not it.get("geoWkt") and not it.get("geoWktHint"):
                errors.append(f"PARCEL 项缺空间矢量（geoWkt/geoWktHint 至少一项）：{it.get('code')}")
            elif it.get("geoWkt"):
                err = check_wkt_closed(it["geoWkt"])
                if err:
                    errors.append(f"{it.get('code')} geoWkt {err}")
    size = payload_size(payload)
    if size > MAX_PAYLOAD_BYTES:
        errors.append(f"payload 体积 {size} 字节超过限值 {MAX_PAYLOAD_BYTES}（需分批上报，R-24）")
    return errors
