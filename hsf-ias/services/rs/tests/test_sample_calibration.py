"""R-15（docs/24 §4，docs/23 §7.6/复核附录#13）单测：seed 样本地块漂移重标定。

锁三项契约（不依赖外部服务）：
  1. ZONES 与 land_parcel 实际地块集（0001/0002/0003/0005/0006/0007，集群实测漂移口径）的
     像元 bbox 逐一对齐——用 land seed 几何公式复算地块 rings，经 ndvi.parcel_masks 换算比对；
  2. 两景解译端到端：各地块 ndvi_mean 落在目标标定值邻域（±0.01，噪声均值≈0），scene1 覆盖
     §2.3 四级长势；
  3. 第二景 RULE_NDVIFALL 自触发地块恰为 {0003, 0005}（≥2 地块降幅，R-13 落库粒度修复的
     自触发场景）；SCENE_PLANS 单一实现点（make_sample 导入同源）。
"""
import json
import sys

sys.path.insert(0, "/srv")

from app import ndvi, sample


def _rect_ring(lng0: float, lat0: float, w: float, h: float) -> list:
    """land/app/domain.py rect_geojson 环公式（逐字同源，测试自含防跨包名冲突）。"""
    return [[round(lng0, 6), round(lat0, 6)], [round(lng0 + w, 6), round(lat0, 6)],
            [round(lng0 + w, 6), round(lat0 + h, 6)], [round(lng0, 6), round(lat0 + h, 6)],
            [round(lng0, 6), round(lat0, 6)]]


def _seed_parcels(codes: list[str]) -> list[dict]:
    """land/app/repo.py seed_if_empty 同款几何：i=SEED 序，rect 0.0015×0.0012 北东向。"""
    seed_all = ["L5306000001", "L5306000002", "L5306000003", "L5306000004",
                "L5306000005", "L5306000006", "L5306000007", "L5306000008"]
    out = []
    for code in codes:
        i = seed_all.index(code) + 1
        lng0 = 102.747 + ((i - 1) % 4) * 0.002
        lat0 = 25.0485 + ((i - 1) // 4) * 0.002
        out.append({"code": code, "rings": [_rect_ring(lng0, lat0, 0.0015, 0.0012)]})
    return out


ACTUAL_PARCELS = ["L5306000001", "L5306000002", "L5306000003",
                  "L5306000005", "L5306000006", "L5306000007"]


def test_zones_aligned_with_actual_parcel_bboxes():
    """ZONES 矩形与实际地块集像元 bbox 逐一相等（漂移重标定的核心锚点）。"""
    masks = ndvi.parcel_masks(_seed_parcels(ACTUAL_PARCELS), sample.W, sample.H, sample.CELL)
    assert set(masks) == set(ACTUAL_PARCELS), "实际地块集掩膜缺失（几何换算漂移）"
    for (r0, r1, c0, c1, _), code in zip(sample.ZONES, sample.PARCEL_ORDER):
        m = masks[code]
        rows = [r for r in range(sample.H) if m[r].any()]
        cols = [c for c in range(sample.W) if m[:, c].any()]
        assert (min(rows), max(rows) + 1, min(cols), max(cols) + 1) == (r0, r1, c0, c1), (
            f"{code} ZONES {(r0, r1, c0, c1)} 与实际掩膜 bbox 失配（R-15 标定回归）")


def _decode(scene_ndvi: dict[str, float]) -> dict[str, float]:
    red, nir, _, _ = sample._bands(scene_ndvi)
    scene = {"bands": {"red": red, "nir": nir}, "width": sample.W, "height": sample.H,
             "cell_size_m": sample.CELL}
    return {v["parcel_code"]: v["ndvi_mean"]
            for v in ndvi.process(scene, _seed_parcels(ACTUAL_PARCELS))["vigors"]}


def test_scene1_covers_four_vigor_levels_on_actual_parcels():
    """scene1 解译：各地块 NDVI 落目标邻域（±0.01），四级长势全覆盖（docs/08 §2.5 口径）。"""
    code, targets = sample.SCENE_PLANS[0]
    assert code == "RS-SAMPLE-R15-20261009"
    means = _decode(targets)
    assert set(means) == set(ACTUAL_PARCELS)
    for parcel, target in targets.items():
        assert abs(means[parcel] - target) <= 0.01, (
            f"{parcel} ndvi_mean={means[parcel]} 偏离标定值 {target}（ZONES 失配或噪声回归）")
    levels = {ndvi.vigor_level(v) for v in means.values()}
    assert {"旺长", "正常", "偏弱", "长势差"} <= levels, f"四级长势未全覆盖：{levels}"


def test_scene2_falls_on_exactly_two_parcels():
    """scene2：RULE_NDVIFALL 自触发地块恰为 {L5306000003, L5306000005}（≥2 地块，R-13 场景），
    其余地地块不误触发（降幅 ≤0.02 或后景不满足 <0.30）。"""
    code, targets = sample.SCENE_PLANS[1]
    assert code == "RS-SAMPLE-R15-20261108"
    prev, curr = sample.SCENE_PLANS[0][1], targets
    means2 = _decode(curr)
    falls = {p for p in ACTUAL_PARCELS
             if ndvi.fall_alerts(prev.get(p), means2[p])}
    assert falls == set(sample.FALL_TARGET_PARCELS), (
        f"降幅自触发地块={falls}，预期 {set(sample.FALL_TARGET_PARCELS)}（R-15 标定回归）")
    for p in sample.FALL_TARGET_PARCELS:
        assert prev[p] - means2[p] > ndvi.FALL_DROP and means2[p] < ndvi.FALL_CEIL


def test_scene_plans_single_source_and_generation_stamp():
    """SCENE_PLANS 唯一实现点=app.sample（make_sample 仅导入再导出，同对象）；场景编码带
    R15 代际戳（旧代际景 uk_scene 幂等不重登，重标定样本须新编码方可重新登记生效）。"""
    import os
    _rs_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # services/rs
    sys.path.insert(0, os.path.join(_rs_root, "sample"))                     # services/rs/sample
    import make_sample
    assert make_sample.SCENE_PLANS is sample.SCENE_PLANS, (
        "SCENE_PLANS 必须同源（make_sample 从 app.sample 导入再导出，禁止双实现漂移）")
    codes = [c for c, _ in sample.SCENE_PLANS]
    assert codes == ["RS-SAMPLE-R15-20261009", "RS-SAMPLE-R15-20261108"]
    assert all("R15" in c for c in codes), "场景编码缺 R15 代际戳（重标定代际留痕）"
