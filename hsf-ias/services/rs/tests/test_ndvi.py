"""NDVI 计算与分级/预警判定单测（docs/08 §2.3 冻结阈值口径，不依赖外部服务）。"""
import sys

sys.path.insert(0, "/srv")

from app import ndvi


def test_ndvi_matrix_basic():
    m = ndvi.ndvi_matrix([[0.1, 0.1]], [[0.3, 0.5]])
    assert abs(m[0][0] - 0.5) < 1e-3            # (0.3-0.1)/(0.3+0.1)=0.5
    assert abs(m[0][1] - 0.6667) < 1e-3
    assert abs(ndvi.ndvi_matrix([[0.0]], [[0.0]])[0][0]) < 1e-3   # 防零除 1e-6


def test_vigor_levels_frozen_thresholds():
    assert ndvi.vigor_level(0.72) == "旺长"
    assert ndvi.vigor_level(0.60) == "旺长"       # 左闭
    assert ndvi.vigor_level(0.50) == "正常"
    assert ndvi.vigor_level(0.40) == "正常"
    assert ndvi.vigor_level(0.30) == "偏弱"
    assert ndvi.vigor_level(0.20) == "偏弱"
    assert ndvi.vigor_level(0.12) == "长势差"
    assert ndvi.vigor_level(-0.05) == "长势差"


def test_aggregate_and_masks():
    red = [[0.1] * 6] * 6
    nir = [[0.5] * 6] * 6                         # NDVI≈0.667 → 旺长
    ndvi_m = ndvi.ndvi_matrix(red, nir)
    # 环坐标为真实经纬度量级：cell=10m → deg≈9e-5 度/像元；2×2 像元环 → 包围盒 3×3=9 像元
    d = 9e-5
    parcels = [{"code": "L5306000001",
                "rings": [[[103.0, 25.0], [103.0 + 2 * d, 25.0],
                           [103.0 + 2 * d, 25.0 - 2 * d], [103.0, 25.0 - 2 * d]]]}]
    masks = ndvi.parcel_masks(parcels, 6, 6, 10.0)
    out = ndvi.aggregate(ndvi_m, masks)
    assert len(out) == 1
    assert out[0]["parcel_code"] == "L5306000001"
    assert out[0]["vigor_level"] == "旺长"
    assert out[0]["pixel_count"] == 9


def test_fall_alerts_frozen_rule():
    assert ndvi.fall_alerts(0.55, 0.25) is True    # 降幅 0.30>0.15 且 0.25<0.30 → 预警
    assert ndvi.fall_alerts(0.55, 0.35) is False   # 降幅 0.20 但后景 ≥0.30
    assert ndvi.fall_alerts(0.45, 0.32) is False   # 降幅 0.13 ≤0.15
    assert ndvi.fall_alerts(None, 0.10) is False   # 无上一景不判定
