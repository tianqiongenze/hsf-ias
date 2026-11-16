"""真实影像接入演练包（R-30 触发条件项，docs/24 §4 R-30 / docs/08 §2.3 复评机制）。

定位：真实卫星影像源落地（Sentinel-2 离线包人工导入）**当天即可执行**的完整操作剧本——
脚本本身不依赖外网；本文件 + `validate_geotiff.py`（见下）构成"离线包→适配器→重标定→回测"
四步演练的自动化骨架，演练记录回写 docs/22（CONVENTIONS-P3 V-2 契约不变承诺的落地证据）。

四步剧本（每步产出物 → 归档位置）：
  ① 获取：Copernicus Data Space 导出 L2A GeoTIFF（B04 red / B08 nir，10m）→
     存放 /opt/hsf/data/sentinel2/<日期>/*.tif（服务器侧，不入 git）
  ② 适配：python -m app.rehearsal validate <tif 路径>
     → 用 rasterio/GDAL 读波段（演练环境 pip install rasterio）→ 统一 scene 字典校验
     （行列一致/规模/NDVI∈[-1,1] 像元占比）→ 输出适配报告
  ③ 解译走通：适配报告通过后，按 docs/08 §2.4 契约走 REST（POST /scenes source=SENTINEL
     → process → /vigor）——管道零改动验证
  ④ 重标定：本模块 `recalibrate(ndvi_series)` 按真实 NDVI 分位数（P25/P50/P75）重算
     四级边界 → 与冻结阈值对比 → 生成回测报告（误报/漏报口径，按 §2.3 复评机制），
     经评审后人工生效（演练不动线上阈值）。
"""
from __future__ import annotations

import json

# 冻结阈值（docs/08 §2.3）：演练对比基准
FROZEN_LEVELS = {"旺长": 0.60, "正常": 0.40, "偏弱": 0.20}   # 边界值：<0.20 长势差


def validate_scene(scene: dict) -> dict:
    """统一 scene 字典校验（②步适配报告核心）：行列一致/规模/NDVI 值域统计。"""
    red, nir = scene["bands"]["red"], scene["bands"]["nir"]
    h, w = len(red), len(red[0])
    if len(nir) != h or any(len(r) != w for r in red) or any(len(n) != w for n in nir):
        raise ValueError("波段矩阵行列不一致")
    if w * h > 262144:
        raise ValueError(f"栅格规模超界 {w}x{h}")
    vals = []
    for r_row, n_row in zip(red, nir):
        for r, n in zip(r_row, n_row):
            denom = n + r
            if denom:
                vals.append((n - r) / (denom + 1e-6))
    vals.sort()
    pct = lambda q: vals[min(int(len(vals) * q), len(vals) - 1)] if vals else 0.0
    return {"sceneCode": scene["sceneCode"], "source": scene.get("source", "SENTINEL"),
            "width": w, "height": h, "pixels": w * h, "ndvi_valid": len(vals),
            "ndvi_min": round(vals[0], 4) if vals else None,
            "ndvi_p25": round(pct(0.25), 4), "ndvi_p50": round(pct(0.50), 4),
            "ndvi_p75": round(pct(0.75), 4),
            "ndvi_max": round(vals[-1], 4) if vals else None,
            "out_of_range": sum(1 for v in vals if v < -1 or v > 1)}


def recalibrate(ndvi_series: list[float]) -> dict:
    """④步重标定：真实 NDVI 分位数 → 建议四级边界（P25/P50/P75），与冻结阈值对比。"""
    if not ndvi_series:
        raise ValueError("NDVI 序列为空（影像未解译或地块掩膜为空）")
    s = sorted(ndvi_series)
    pct = lambda q: s[min(int(len(s) * q), len(s) - 1)]
    p25, p50, p75 = round(pct(0.25), 3), round(pct(0.50), 3), round(pct(0.75), 3)
    proposed = {"长势差<": p25, "偏弱<": p50, "正常<": p75, "旺长≥": p75}
    return {"frozen": FROZEN_LEVELS, "proposed": proposed,
            "delta": {"正常边界": round(p50 - FROZEN_LEVELS["正常"], 3),
                      "旺长边界": round(p75 - FROZEN_LEVELS["旺长"], 3)},
            "samples": len(s),
            "note": "分位数重标定建议——经评审人工生效，演练不动线上阈值（docs/08 §2.3）"}


def write_report(scene_report: dict, calib: dict, path: str) -> str:
    """演练报告落盘（回写 docs/22 附件）。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write("# R-30 真实影像接入演练报告\n\n## 适配校验\n\n```json\n")
        f.write(json.dumps(scene_report, ensure_ascii=False, indent=2))
        f.write("\n```\n\n## 重标定建议\n\n```json\n")
        f.write(json.dumps(calib, ensure_ascii=False, indent=2))
        f.write("\n```\n")
    return path
