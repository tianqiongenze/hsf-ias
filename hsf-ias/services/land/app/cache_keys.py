"""land 缓存键常量与 TTL（04 号 §8.2 冻结：键 2 一张图要素 / 键 3 地块详情）。"""

GEOJSON_PREFIX = "hsf:land:geojson:"  # 键2 失效用前缀（写路径 invalidate_prefix）


def geojson_key(village_code: str) -> str:
    """键2：一张图村级 FeatureCollection，hsf:land:geojson:{villageCode}。"""
    return f"hsf:land:geojson:{village_code}"


def parcel_key(code: str) -> str:
    """键3：地块详情，hsf:land:parcel:{code}。"""
    return f"hsf:land:parcel:{code}"


GEOJSON_TTL = 600  # L2 TTL（s）
PARCEL_TTL = 300
L1_TTL = 60  # L1 TTL ≤ L2 TTL/3（04 号 §8.4 跨副本脏窗口约束）
