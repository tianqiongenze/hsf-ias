"""land TDD 单元测试：统一编码、GeoJSON 校验、FeatureCollection（FR-101/103，纯函数不依赖 DB）。"""
import json
import sys

sys.path.insert(0, "/srv")  # 容器内 WORKDIR=/srv；本地以 PYTHONPATH=. 运行

import pytest

from app.domain import (BusinessError, CODE_RE, MAX_GEOJSON_FEATURES, feature_collection,
                        make_code, rect_geojson, to_feature, validate_geojson)


# ── FR-101 统一编码 ──
def test_code_format_and_regex():
    assert make_code(1) == "L5306000001"
    assert make_code(8) == "L5306000008"
    assert make_code(9999) == "L5306009999"
    for seq in (1, 8, 1234, 9999):
        assert CODE_RE.match(make_code(seq))
    assert len(make_code(1)) == 11  # 11 位实长，CHAR(20) 列宽预留


# ── FR-103 GeoJSON Polygon 校验 ──
def test_geojson_none_passes_through():
    assert validate_geojson(None) is None
    assert validate_geojson("  ") is None


def test_geojson_valid_polygon_normalized():
    raw = rect_geojson(102.75, 25.05, 0.001, 0.0008)
    out = validate_geojson(raw)
    assert json.loads(out)["type"] == "Polygon"
    ring = json.loads(out)["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) >= 4  # 环闭合


def test_geojson_invalid_rejected():
    for bad in ('{"type":"Point","coordinates":[1,2]}',          # 非 Polygon
                "not-json",                                       # 非 JSON
                '{"type":"Polygon","coordinates":[]}',            # 无环
                '{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1]]]}',   # <4 点
                '{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,1]]]}',  # 未闭合
                '{"type":"Polygon","coordinates":[[[200,0],[1,0],[1,1],[0,1],[0,0]]]}'):  # 越界
        with pytest.raises(BusinessError) as ei:
            validate_geojson(bad)
        assert ei.value.code == "VALIDATION"


# ── 种子基线：8 地块矩形围绕 102.75E,25.05N（04 号 §2.3 / FR-103②）──
def test_seed_rectangles_cluster_around_center():
    for i in range(1, 9):
        lng0 = 102.747 + ((i - 1) % 4) * 0.002
        lat0 = 25.0485 + ((i - 1) // 4) * 0.002
        g = json.loads(rect_geojson(lng0, lat0, 0.0015, 0.0012))
        ring = g["coordinates"][0]
        assert g["type"] == "Polygon" and ring[0] == ring[-1]
        for lng, lat in ring:
            assert 102.74 <= lng <= 102.76 and 25.04 <= lat <= 25.06  # 中心附近


# ── FeatureCollection 组装（FR-103①）──
def _row(i):
    return {"code": f"L530600000{i}", "name": f"地块{i}", "village_code": "53060010101",
            "area_mu": 12.5, "soil_type": "水稻土", "crop_type": "水稻",
            "geo_json": rect_geojson(102.75, 25.05, 0.001, 0.0008)}


def test_feature_collection_shape():
    fc = feature_collection([_row(i) for i in range(1, 4)])
    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 3
    f = fc["features"][0]
    assert f["type"] == "Feature" and f["geometry"]["type"] == "Polygon"
    assert f["properties"]["code"] == "L5306000001"
    assert f["properties"]["villageCode"] == "53060010101"  # camelCase 对齐 SDL 映射
    assert f["properties"]["areaMu"] == 12.5


def test_feature_collection_caps_features():
    rows = [dict(_row(1), code=f"L530600{i:04d}") for i in range(MAX_GEOJSON_FEATURES + 10)]
    assert len(feature_collection(rows)["features"]) == MAX_GEOJSON_FEATURES


def test_to_feature_tolerates_broken_geometry():
    row = dict(_row(1), geo_json="{broken")
    assert to_feature(row)["geometry"] is None  # 出图健壮性：坏几何置空不抛
