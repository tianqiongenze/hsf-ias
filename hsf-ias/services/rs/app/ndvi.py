"""HSF rs NDVI 解译核心（numpy 纯 CPU，V-1 红线：无 torch/tf/任何模型推理依赖）。

docs/08 §2.2 冻结管道：波段矩阵 → NDVI=(NIR−Red)/(NIR+Red+1e-6) → 地块掩膜线性映射
（WGS84 经纬度→像元索引，ADR-001 口径无投影转换）→ 按地块聚合 → §2.3 四级分级落库。
"""
import numpy as np

# docs/08 §2.3 冻结阈值（ndvi_mean 区间，左闭右开，最低档无下界）
VIGOR_LEVELS = (("旺长", 0.60, float("inf")),
                ("正常", 0.40, 0.60),
                ("偏弱", 0.20, 0.40),
                ("长势差", float("-inf"), 0.20))
# 非农化预警冻结参数（docs/08 §2.3）：相邻两景均值降幅 >0.15 且后景 <0.30
FALL_DROP = 0.15
FALL_CEIL = 0.30


def ndvi_matrix(red: list[list[float]], nir: list[list[float]]) -> np.ndarray:
    """波段矩阵 → NDVI 矩阵（float32；1e-6 防零除，docs/08 §2.2 ③）。"""
    r = np.asarray(red, dtype=np.float32)
    n = np.asarray(nir, dtype=np.float32)
    return (n - r) / (n + r + np.float32(1e-6))


def vigor_level(mean: float) -> str:
    """NDVI 均值 → 四级长势（§2.3 冻结阈值，先命中先返回）。"""
    for level, lo, hi in VIGOR_LEVELS:
        if lo <= mean < hi:
            return level
    return "长势差"


def parcel_masks(parcels: list[dict], width: int, height: int,
                 cell_size_m: float) -> dict[str, np.ndarray]:
    """地块 rings → 像元掩膜字典。

    输入 parcels 元素：{"code", "rings": [[ring0 点对...], [ring1 ...]]}（Polygon coordinates
    原样，外环在前——repo.parcels_geojson 契约）。线性映射（ADR-001）：栅格锚点取全部地块
    包围盒左上角（minLon/minLat），像元索引 col=floor((lon−minLon)/deg)、row=floor((minLat−lat)/deg)；
    deg_per_cell = cell_size_m / 111320.0（赤道 1°≈111.32km 的工程简化，同 ADR-001 线性口径）。
    """
    pts: list[tuple[float, float]] = []
    for p in parcels:
        for ring in (p.get("rings") or []):
            for lon, lat in ring:
                pts.append((lon, lat))
    if not pts:
        return {}
    min_lon, max_lat = min(x for x, _ in pts), max(y for _, y in pts)
    deg = cell_size_m / 111320.0

    def to_cell(lon: float, lat: float) -> tuple[int, int]:
        col = int((lon - min_lon) / deg)
        row = int((max_lat - lat) / deg)
        return col, row

    masks: dict[str, np.ndarray] = {}
    for p in parcels:
        mask = np.zeros((height, width), dtype=bool)
        for ring in (p.get("rings") or []):
            cells = [to_cell(lon, lat) for lon, lat in ring]
            if len(cells) < 3:
                continue
            cols = [c for c, _ in cells]
            rows = [r for _, r in cells]
            c0, c1 = max(min(cols), 0), min(max(cols) + 1, width)
            r0, r1 = max(min(rows), 0), min(max(rows) + 1, height)
            # 包围盒近似掩膜（ADR-001 工程口径：不引入多边形栅格化依赖，边界像元以环包围盒为准）
            if c1 > c0 and r1 > r0:
                mask[r0:r1, c0:c1] = True
        if mask.any():
            masks[p["code"]] = mask
    return masks


def aggregate(ndvi: np.ndarray, masks: dict[str, np.ndarray]) -> list[dict]:
    """按地块聚合 NDVI 统计 + 分级（docs/08 §2.2 ④）。"""
    out: list[dict] = []
    for code, mask in masks.items():
        vals = ndvi[mask]
        if vals.size == 0:
            continue
        mean = float(np.mean(vals))
        out.append({"parcel_code": code, "ndvi_mean": round(mean, 4),
                    "ndvi_min": round(float(np.min(vals)), 4),
                    "ndvi_max": round(float(np.max(vals)), 4),
                    "vigor_level": vigor_level(mean),
                    "pixel_count": int(vals.size)})
    out.sort(key=lambda x: x["parcel_code"])
    return out


def process(scene: dict, parcels: list[dict]) -> dict:
    """完整解译：波段→NDVI→掩膜→聚合。scene 含 bands(red/nir)/width/height/cell_size_m。"""
    ndvi = ndvi_matrix(scene["bands"]["red"], scene["bands"]["nir"])
    masks = parcel_masks(parcels, scene["width"], scene["height"],
                         float(scene["cell_size_m"]))
    return {"vigors": aggregate(ndvi, masks)}


def fall_alerts(prev_mean: float | None, curr_mean: float) -> bool:
    """非农化预警判定（§2.3）：相邻两景降幅 >0.15 且后景 <0.30。"""
    if prev_mean is None:
        return False
    return (prev_mean - curr_mean) > FALL_DROP and curr_mean < FALL_CEIL
