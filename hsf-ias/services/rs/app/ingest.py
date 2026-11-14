"""HSF rs 输入适配器（CONVENTIONS-P3 V-2 红线：真实影像只需替换本单文件，契约不变）。

适配器契约（登记→解译管道不变）：任何影像源最终产出统一的 scene 字典——
    {"sceneCode", "source", "acquiredAt"(YYYY-MM-DD HH:MM:SS naive 本地，A-04-3),
     "bands": {"red": [[...]], "nir": [[...]]}, "width", "height", "cellSizeM"}
本期实现 json_bands：登记请求内联 JSON 波段矩阵（SAMPLE 合成样本栅格，64×64 级别规模）。
真实 GeoTIFF 接入时（三期不做，docs/08 §1 显式边界）：仅在本文件新增 geotiff_bands
（rasterio/GDAL 读波段 → 同一 scene 字典），main.py /scenes 增加文件上传分支即可——
NDVI 计算/掩膜聚合/分级落库/非农化预警全部零改动。
"""
from __future__ import annotations

MAX_PIXELS = 262144      # 栅格规模上界（64×64 样本期口径的 64 倍余量，防超大 payload）
MIN_PIXELS = 2           # 至少 2 像元（1×2 起可算 NDVI）


class IngestError(ValueError):
    """输入适配失败（登记请求 400 INVALID_ARGUMENT 的唯一来源）。"""


def json_bands(body: dict) -> dict:
    """SAMPLE 适配器：登记请求体 → 统一 scene 字典（矩阵行列一致性与规模校验）。"""
    try:
        scene_code = str(body["sceneCode"])
        acquired_at = str(body["acquiredAt"])
        bands = body["bands"]
        red = bands["red"]
        nir = bands["nir"]
    except KeyError as e:
        raise IngestError(f"缺少必填字段：{e}") from None
    if not (isinstance(red, list) and isinstance(nir, list) and red and nir):
        raise IngestError("波段矩阵为空")
    h = len(red)
    w = len(red[0])
    if not (len(nir) == h and all(len(r) == w for r in red)
            and all(len(n) == w for n in nir)):
        raise IngestError("波段矩阵行列不一致")
    if not MIN_PIXELS <= w * h <= MAX_PIXELS:
        raise IngestError(f"栅格规模超界（{MIN_PIXELS}~{MAX_PIXELS} 像元）")
    try:
        cell = float(body.get("cellSizeM") or 10.0)
    except (TypeError, ValueError):
        raise IngestError("cellSizeM 非法（须为数字，米/像元）") from None
    return {"sceneCode": scene_code, "source": str(body.get("source") or "SAMPLE"),
            "acquiredAt": acquired_at, "bands": {"red": red, "nir": nir},
            "width": w, "height": h, "cellSizeM": cell}


def geotiff_bands(path: str) -> dict:                     # pragma: no cover（三期不做，V-2 占位）
    """真实影像适配占位（SENTINEL 等）：GeoTIFF → 统一 scene 字典。

    三期显式不做（docs/08 §1：内网无外网影像源）；真实接入时仅替换本文件，
    管道契约（登记→波段提取→NDVI→按地块聚合→分级落库）零变更。"""
    raise NotImplementedError("真实 GeoTIFF 输入适配器三期不做（docs/08 §1/V-2 契约占位）")
