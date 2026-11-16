"""R-30 演练模块单测：scene 校验（行列/规模/NDVI 统计）与分位数重标定——不依赖 rasterio。"""
import pytest

from app import rehearsal


def _scene(w=8, h=8):
    red = [[0.3] * w for _ in range(h)]
    nir = [[0.6] * w for _ in range(h)]
    return {"sceneCode": "R30-TEST", "source": "SENTINEL", "acquiredAt": "2026-11-16 10:00:00",
            "bands": {"red": red, "nir": nir}, "width": w, "height": h, "cellSizeM": 10.0}


def test_validate_scene_basic_stats():
    r = rehearsal.validate_scene(_scene())
    assert r["pixels"] == 64 and r["ndvi_valid"] == 64
    # NDVI=(nir-red)/(nir+red)=0.3/0.9≈0.3333
    assert abs(r["ndvi_p50"] - 0.3333) < 0.001
    assert r["out_of_range"] == 0


def test_validate_scene_rejects_mismatched_bands():
    s = _scene()
    s["bands"]["nir"] = [row[:-1] for row in s["bands"]["nir"]]
    with pytest.raises(ValueError, match="行列不一致"):
        rehearsal.validate_scene(s)


def test_validate_scene_rejects_oversize():
    s = _scene(600, 500)                              # 300k 像元 > 262144
    s["width"], s["height"] = 600, 500
    with pytest.raises(ValueError, match="规模超界"):
        rehearsal.validate_scene(s)


def test_recalibrate_quantile_boundaries():
    series = [i / 100 for i in range(100)]            # 0.00~0.99 均匀
    c = rehearsal.recalibrate(series)
    assert 0.24 <= c["proposed"]["长势差<"] <= 0.26   # P25
    assert 0.74 <= c["proposed"]["正常<"] <= 0.76     # P75（三边界：长势差<25%/偏弱<50%/正常<75%）
    assert abs(c["delta"]["正常边界"] - (c["proposed"]["偏弱<"] - 0.40)) < 0.001
    assert "评审" in c["note"]


def test_recalibrate_empty_raises():
    with pytest.raises(ValueError, match="序列为空"):
        rehearsal.recalibrate([])


def test_write_report(tmp_path):
    p = rehearsal.write_report(rehearsal.validate_scene(_scene()),
                               rehearsal.rehearsal_recalibrate_helper()
                               if hasattr(rehearsal, "rehearsal_recalibrate_helper")
                               else rehearsal.recalibrate([0.1, 0.3, 0.5, 0.7, 0.9]),
                               str(tmp_path / "r30.md"))
    assert "R-30" in open(p, encoding="utf-8").read()
