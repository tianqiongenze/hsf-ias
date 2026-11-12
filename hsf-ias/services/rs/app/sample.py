"""HSF rs 合成样本栅格（V-2：与真实 GeoTIFF 处理链路同构，内网无外网影像源的确定性数据源）。

seed 两景（acquired_at 间隔 30 天）：第一景覆盖四级长势；第二景对 L5306000003 构造
NDVI 降幅 0.2（0.55→0.35 → 不触发；按 docs/08 §2.3 需 >0.15 降幅且后景 <0.30——
构造 0.55→0.25：降幅 0.30>0.15 且 0.25<0.30 ✓ 触发 RULE_NDVIFALL）。
"""
import datetime as dt

from . import repo

W = H = 64
CELL = 10.0  # 米/像元

# 4 个样本地块的像元矩形区（覆盖种子 L5306000001~0004 的质心邻域——经纬度由 seed 时
# 从 parcels_geojson 包围盒锚定，见 seed()；此处只描述相对布局与目标 NDVI）
ZONES = [  # (row0, row1, col0, col1, target_ndvi)
    (4, 28, 4, 28, 0.72),    # 旺长
    (4, 28, 36, 60, 0.50),   # 正常
    (36, 60, 4, 28, 0.30),   # 偏弱
    (36, 60, 36, 60, 0.12),  # 长势差
]
PARCEL_ORDER = ["L5306000001", "L5306000002", "L5306000003", "L5306000004"]


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
    """登记两景并解译：scene-20261009（基线）+ scene-20261108（第二景对 0003 降幅触发）。"""
    results = []
    day1 = (dt.datetime.now() - dt.timedelta(days=30)).strftime("%Y-%m-%d 09:30:00")
    day2 = dt.datetime.now().strftime("%Y-%m-%d 09:30:00")
    plans = [("RS-SAMPLE-20261009", day1,
              {"L5306000001": 0.72, "L5306000002": 0.50, "L5306000003": 0.55, "L5306000004": 0.12}),
             ("RS-SAMPLE-20261108", day2,
              {"L5306000001": 0.71, "L5306000002": 0.49, "L5306000003": 0.25, "L5306000004": 0.13})]
    for code, acquired, ndvi_map in plans:
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
    return {"seeded": 2, "scenes": results}
