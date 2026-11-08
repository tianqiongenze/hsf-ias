"""sim 观测值生成（纯函数层，无外部依赖）——二期仅保留"造数"职责。

对齐：hsf/docs/04 §4.7（metric 命名与量程冻结）、docs/07 §2.5（职责边界冻结：
规则引擎/告警职责移交 iot 服务——本模块不再持有任何规则与阈值常量，static_gate 检查项；
sim 只做值域生成与发布，iot 做判数与落库）。
"""
import random

# ── 观测值生成（量程 = 04 号 §4.7 冻结；正常带 + 极端带）────────────────────────
# (量程低, 量程高, 正常带低, 正常带高, 极端带 or None)
METRIC_RANGES = {
    "soil_moisture": (5.0, 95.0, 25.0, 75.0, (6.0, 14.5, 86.0, 93.0)),   # 旱情/渍害两段极端
    "air_temp":      (-5.0, 40.0, 12.0, 34.0, None),                     # 无规则 metric
    "wind_speed":    (0.0, 25.0, 1.0, 9.0, (18.0, 24.0, None, None)),    # 大风单段极端
    "rainfall":      (0.0, 60.0, 0.0, 25.0, None),                       # 无规则 metric
    "pest_index":    (0.0, 100.0, 20.0, 65.0, (82.0, 96.0, None, None)), # 虫害单段极端
    "water_level":   (0.0, 5.0, 0.5, 3.0, None),                         # 无规则 metric
}

EXTREME_PROBABILITY = 0.02  # 每条读数 2% 概率落入极端带（保证持续心跳产生真实告警，iot 侧判）


def next_value(metric: str, rng: random.Random | None = None) -> float:
    """生成一条观测值：正常带均匀采样；带规则的 metric 以小概率注入极端带。

    阈值判定已移交 iot（docs/07 §2.5）——极端带取值略越过一期阈值（如旱情段 6~14.5
    越过 15、大风段 18~24 越过 17），保证 iot 回差状态机必然产生真实告警（E2E 前提）。"""
    rng = rng or random
    lo, hi, n_lo, n_hi, extreme = METRIC_RANGES[metric]
    if extreme and rng.random() < EXTREME_PROBABILITY:
        e1lo, e1hi, e2lo, e2hi = extreme
        bands = [(e1lo, e1hi)] + ([(e2lo, e2hi)] if e2lo is not None else [])
        lo, hi = bands[rng.randrange(len(bands))]
    else:
        lo, hi = n_lo, n_hi
    return round(rng.uniform(lo, hi), 3)


def metrics() -> tuple[str, ...]:
    """6 类 metric（04 号 §4.7 冻结命名）。"""
    return tuple(METRIC_RANGES)
