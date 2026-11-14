"""HSF rs 合成样本栅格（V-2：与真实 GeoTIFF 处理链路同构，内网无外网影像源的确定性数据源）。

seed 两景（acquired_at 间隔 30 天）：第一景覆盖四级长势；第二景对 **L5306000003 与 L5306000005**
构造 NDVI 降幅（0.55→0.25：降幅 0.30>0.15 且后景 0.25<0.30 → 双地块触发 RULE_NDVIFALL）——
≥2 地块降幅为 R-13 落库粒度修复（trig_time 地块序号偏移）提供自触发多地块场景（docs/24 §4 R-15）。

**R-15 重标定（2026-11-14，docs/24 §4；标定记录落 docs/22）**：种子地块集已漂移——land seed
代码 8 地块（0001~0008），集群实测 land_parcel 实际地块集为 0001/0002/0003/0005/0006/0007
（`GET /vigor` 实测 total=42 六地块，与 docs/23 §5.1 漂移记录一致）。ZONES 按实际地块集重标定：
像元矩形由 land seed 几何公式（land/app/repo.py seed_if_empty：102.747+((i-1)%4)*0.002 /
25.0485+((i-1)//4)*0.002，rect 0.0015×0.0012 北东向）经 `ndvi.parcel_masks` 同法换算——
六地块 bbox（[r0:r1,c0:c1] 半开区间）：
  L5306000005 rows 0..14 cols 0..17 ｜ L5306000006 rows 0..14 cols 22..39 ｜ L5306000007 rows 0..14 cols 44..62
  L5306000001 rows 22..36 cols 0..17 ｜ L5306000002 rows 22..36 cols 22..39 ｜ L5306000003 rows 22..36 cols 44..62
（上下两行各 3 地块，行列间隙为背景；ndvi.py 线性映射 ADR-001 口径不变）。
场景编码加 R15 代际戳（RS-SAMPLE-R15-*）：旧代际景已按旧 ZONES 落库且 uk_scene 幂等不重登，
新代际编码保证重标定样本在存量环境可重新登记生效。
"""
import datetime as dt

from . import repo

W = H = 64
CELL = 10.0  # 米/像元

# 6 个样本地块的像元矩形区（R-15 重标定：对应集群实际地块集 0001/0002/0003/0005/0006/0007，
# bbox 换算依据见模块 docstring；此处 ZONES 与 PARCEL_ORDER 按序 zip——先命中先生效，区间互不重叠）
ZONES = [  # (row0, row1, col0, col1, target_ndvi)——半开区间，与 ndvi.parcel_masks 切片一致
    (22, 36, 0, 17, 0.72),    # L5306000001 旺长（scene1 基线 0.72）
    (22, 36, 22, 39, 0.50),   # L5306000002 正常（scene1 基线 0.50）
    (22, 36, 44, 62, 0.55),   # L5306000003 正常（scene2 降至 0.25 → 预警）
    (0, 14, 0, 17, 0.55),     # L5306000005 正常（scene2 降至 0.25 → 预警，R-13 双地块场景）
    (0, 14, 22, 39, 0.30),    # L5306000006 偏弱
    (0, 14, 44, 62, 0.12),    # L5306000007 长势差
]
# 实际地块集（排序序=R-13 trig_time 地块序号偏移的景内地块序，ndvi.aggregate 输出同序）
PARCEL_ORDER = ["L5306000001", "L5306000002", "L5306000003",
                "L5306000005", "L5306000006", "L5306000007"]

# SCENE_PLANS 同源唯一实现点（docs/24 §4 R-15：app/sample.py 为唯一实现，make_sample/seed_rs
# 导入本定义）：(scene_code, {parcel_code: 目标 NDVI})——第二景对 0003/0005 双地块降幅
# （0.55→0.25），其余地块微调不触发（降幅 ≤0.02 或后景不满足 <0.30）
SCENE_PLANS = [
    ("RS-SAMPLE-R15-20261009", {"L5306000001": 0.72, "L5306000002": 0.50, "L5306000003": 0.55,
                                "L5306000005": 0.55, "L5306000006": 0.30, "L5306000007": 0.12}),
    ("RS-SAMPLE-R15-20261108", {"L5306000001": 0.71, "L5306000002": 0.49, "L5306000003": 0.25,
                                "L5306000005": 0.25, "L5306000006": 0.28, "L5306000007": 0.13}),
]
# 预期降幅触发地块（R-15 验收/单测锚点）：0003 与 0005 各自降幅 0.30>0.15 且后景 0.25<0.30
FALL_TARGET_PARCELS = ("L5306000003", "L5306000005")


def _bands(scene_ndvi: dict[str, float], base_red: float = 0.08) -> tuple[list, list, float, float]:
    """按目标 NDVI 生成 red/nir 矩阵：nir = (ndvi*(nir+red)+red ...) 反解——
    工程简化：固定 red=base_red+噪声，nir = red*(1+ndvi)/(1-ndvi)（NDVI 定义反解）。"""
    red_m = [[0.0] * W for _ in range(H)]
    nir_m = [[0.0] * W for _ in range(H)]
    import random
    rng = random.Random(20261108)                       # 确定性（验收可复现）
    for r in range(H):
        for c in range(W):
            t = 0.0
            for (r0, r1, c0, c1, v), code in zip(ZONES, PARCEL_ORDER):
                if r0 <= r < r1 and c0 <= c < c1:
                    t = scene_ndvi.get(code, v)
                    break
            else:
                t = 0.25 + rng.random() * 0.1           # 非地块背景（裸土/杂地）
            noise = rng.uniform(-0.015, 0.015)
            ndvi = max(-0.2, min(0.95, t + noise))
            red = base_red + rng.uniform(0, 0.02)
            nir = red * (1 + ndvi) / max(1 - ndvi, 1e-3)
            red_m[r][c] = round(red, 4)
            nir_m[r][c] = round(nir, 4)
    return red_m, nir_m, base_red, 0.0


def seed(admin_user_id: int) -> dict:
    """登记两景并解译（SCENE_PLANS 唯一实现点）：R15 代际基线景 + 双地块降幅景（自触发）。"""
    results = []
    day1 = (dt.datetime.now() - dt.timedelta(days=30)).strftime("%Y-%m-%d 09:30:00")
    day2 = dt.datetime.now().strftime("%Y-%m-%d 09:30:00")
    for (code, ndvi_map), acquired in zip(SCENE_PLANS, (day1, day2)):
        red, nir, _, _ = _bands(ndvi_map)
        repo.insert_scene(code, "SAMPLE", acquired, red, nir, W, H, CELL)
        scene = repo.get_scene(code)
        parcels = repo.parcels_geojson()
        from . import ndvi as ndvi_mod
        result = ndvi_mod.process(scene, parcels)
        repo.save_vigor(code, result["vigors"])
        repo.mark_scene(code, "PROCESSED")
        alerts = repo.ndvi_fall_alerts(result["vigors"], code, acquired)
        results.append({"sceneCode": code, "parcels": len(result["vigors"]),
                        "alerts": alerts,
                        "levels": {v["parcel_code"]: v["vigor_level"] for v in result["vigors"]}})
    return {"seeded": len(results), "scenes": results}
