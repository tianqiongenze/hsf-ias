"""sim 领域服务（纯函数层，无外部依赖）：告警规则引擎 + 观测值生成。

对齐：hsf/docs/05 §7.2（规则/边界值冻结）、docs/04 §4.7（metric 命名与量程冻结）。
单测对象：4 条规则边界值（14.9/15.1、84.9/85.1、16.9/17.1、79.9/80.1，FR-503）。
"""
import random

# ── 告警规则（一期 4 条，05 号 §7.2 / 04 号 §4.10 冻结；op 仅 < 或 >）──────────
RULES = (
    {"rule_code": "RULE_DROUGHT", "metric": "soil_moisture", "op": "<", "threshold": 15.0,
     "label": "旱情"},
    {"rule_code": "RULE_WATERLOGGING", "metric": "soil_moisture", "op": ">", "threshold": 85.0,
     "label": "渍害"},
    {"rule_code": "RULE_GALE", "metric": "wind_speed", "op": ">", "threshold": 17.0,
     "label": "大风"},
    {"rule_code": "RULE_PEST", "metric": "pest_index", "op": ">", "threshold": 80.0,
     "label": "虫害"},
)

RULES_BY_METRIC: dict[str, tuple[dict, ...]] = {}
for _r in RULES:
    RULES_BY_METRIC.setdefault(_r["metric"], []).append(_r)
RULES_BY_METRIC = {k: tuple(v) for k, v in RULES_BY_METRIC.items()}

# ── 观测值生成（量程 = 04 号 §4.7 冻结；正常带 + 极端注入触发规则）─────────────
# (量程低, 量程高, 正常带低, 正常带高, 极端带 or None)
METRIC_RANGES = {
    "soil_moisture": (5.0, 95.0, 25.0, 75.0, (6.0, 14.5, 86.0, 93.0)),   # 旱情/渍害两段极端
    "air_temp":      (-5.0, 40.0, 12.0, 34.0, None),                     # 一期无规则
    "wind_speed":    (0.0, 25.0, 1.0, 9.0, (18.0, 24.0, None, None)),    # 大风单段极端
    "rainfall":      (0.0, 60.0, 0.0, 25.0, None),                       # 一期无规则
    "pest_index":    (0.0, 100.0, 20.0, 65.0, (82.0, 96.0, None, None)), # 虫害单段极端
    "water_level":   (0.0, 5.0, 0.5, 3.0, None),                         # 一期无规则
}

EXTREME_PROBABILITY = 0.02  # 每条读数 2% 概率落入极端带（保证持续 tick 产生真实告警）


def match(rule: dict, metric: str, value: float) -> bool:
    """(rule, reading) 判定：op ∈ {<, >}，严格比较（边界值单测口径）。"""
    if rule["metric"] != metric:
        return False
    if rule["op"] == "<":
        return value < rule["threshold"]
    if rule["op"] == ">":
        return value > rule["threshold"]
    return False


def evaluate(metric: str, value: float) -> list[dict]:
    """规则引擎纯函数：一条读数命中多条规则 → 多条告警（05 号 §7.2 契约③）。"""
    return [r for r in RULES_BY_METRIC.get(metric, ()) if match(r, metric, value)]


def next_value(metric: str, rng: random.Random | None = None) -> float:
    """生成一条观测值：正常带均匀采样；带规则的 metric 以小概率注入极端带。"""
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
