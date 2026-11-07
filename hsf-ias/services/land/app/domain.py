"""land 领域层（纯函数零 IO）：统一编码、GeoJSON 校验、一张图 FeatureCollection 组装。

依据：03 号 §4.2（land 战术设计）、04 号 §2.1/§2.2（编码规则与取号）、05 号 §2.1（错误码）。
"""
import json
import re

# 统一编码：L+6 位行政区划（县级 530600）+4 位县内序号，^L\d{10}$（04 号 §2.1 冻结）
CODE_RE = re.compile(r"^L\d{10}$")
COUNTY_PREFIX = "L530600"

# 一张图大键防护：单村要素上限（04 号 §8.3）
MAX_GEOJSON_FEATURES = 500


class BusinessError(Exception):
    """领域业务错误：code 对齐 05 号 §2.1 错误码注册表（VALIDATION/NOT_FOUND/CODE_DUPLICATED…）。"""

    def __init__(self, code: str, msg: str):
        self.code, self.msg = code, msg
        super().__init__(msg)


def make_code(seq: int) -> str:
    """序号 → 地块编码（L5306000001 起，04 号 §2.3 种子基线同区间）。"""
    return f"{COUNTY_PREFIX}{seq:04d}"


def validate_geojson(raw: str | None) -> str | None:
    """GeoJSON Polygon 结构校验：合法 JSON、type=Polygon、环≥4 点且闭合、经纬度范围（WGS84）。

    应用层校验无拓扑校验（ADR-HSF-001 限制③）；返回规范化 JSON 字符串供 JSON 列存储。
    """
    if raw is None or not raw.strip():
        return None
    try:
        g = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise BusinessError("VALIDATION", "GeoJSON 不是合法 JSON")
    if not isinstance(g, dict) or g.get("type") != "Polygon":
        raise BusinessError("VALIDATION", "GeoJSON 必须为 Polygon 类型")
    rings = g.get("coordinates")
    if not isinstance(rings, list) or not rings:
        raise BusinessError("VALIDATION", "GeoJSON 缺少 coordinates 环")
    for ring in rings:
        if not isinstance(ring, list) or len(ring) < 4:
            raise BusinessError("VALIDATION", "多边形环至少 4 个点")
        if ring[0] != ring[-1]:
            raise BusinessError("VALIDATION", "多边形环未闭合")
        for pt in ring:
            if (not isinstance(pt, list) or len(pt) != 2
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pt)):
                raise BusinessError("VALIDATION", "坐标点必须为 [lng, lat] 数值对")
            lng, lat = pt
            if not (-180 <= lng <= 180 and -90 <= lat <= 90):
                raise BusinessError("VALIDATION", "经纬度超出 WGS84 范围")
    return json.dumps(g, ensure_ascii=False)


def rect_geojson(lng0: float, lat0: float, width: float, height: float) -> str:
    """WGS84 矩形 Polygon（[lng,lat] 顺序，环闭合）——种子地块生成用。"""
    ring = [
        [round(lng0, 6), round(lat0, 6)],
        [round(lng0 + width, 6), round(lat0, 6)],
        [round(lng0 + width, 6), round(lat0 + height, 6)],
        [round(lng0, 6), round(lat0 + height, 6)],
        [round(lng0, 6), round(lat0, 6)],
    ]
    return json.dumps({"type": "Polygon", "coordinates": [ring]}, ensure_ascii=False)


def empty_feature_collection() -> dict:
    return {"type": "FeatureCollection", "features": []}


def _geometry_of(geo) -> dict | None:
    """JSON 列取回（str/dict 兼容，A-04-1 回退 TEXT 同为 str）；解析失败置空不影响出图。"""
    if isinstance(geo, str):
        try:
            geo = json.loads(geo)
        except (json.JSONDecodeError, ValueError):
            return None
    return geo if isinstance(geo, dict) else None


def to_feature(row: dict) -> dict:
    """地块行 → GeoJSON Feature（properties 承载台账属性，FR-202 点击面板数据源）。"""
    return {
        "type": "Feature",
        "geometry": _geometry_of(row.get("geo_json")),
        "properties": {
            "code": row["code"],
            "name": row["name"],
            "villageCode": row["village_code"],
            "areaMu": float(row["area_mu"]) if row.get("area_mu") is not None else None,
            "soilType": row.get("soil_type"),
            "cropType": row.get("crop_type"),
        },
    }


def feature_collection(rows: list) -> dict:
    """行集 → FeatureCollection；超出单村上限截断（04 号 §8.3，一期 8 块远低于限）。"""
    return {"type": "FeatureCollection",
            "features": [to_feature(r) for r in rows[:MAX_GEOJSON_FEATURES]]}
