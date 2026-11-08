"""iot 规则引擎（纯函数层，无外部依赖）：一期 4 条冻结规则 + 二期滞后回差状态机。

对齐：hsf/docs/07 §4.2（阈值/回差/状态机冻结）、docs/05 §7.2 与 docs/04 §4.10（一期进入
阈值与幂等键不变，进入条件严格沿用一期边界值单测口径 14.9/15.1、84.9/85.1、16.9/17.1、79.9/80.1）。

状态机：每 (device_code, rule_code) 一个槽位（进程内 dict，单副本固定——D-07-1）——
  NORMAL ──满足进入条件──► TRIPPED（仅此刻产生新告警候选，uk_rule_reading 兜底幂等）
  TRIPPED ──满足退出条件──► NORMAL（不自动改 alert_record.state，闭环仍由人工，05 §7.1 端点 6）
回差防抖动：进入与退出阈值间形成滞回带（如墒情 15~20 区间抑制重复告警）。
"""
import os

# ── 滞后回差（env HYST_PCT 可调，缺省 5——docs/07 §4.2 冻结表缺省口径）────────────
# 各规则回差缺省值与冻结表逐条对应：旱情 15→20、渍害 85→80、大风 17→14（表值 14 对应
# 回差 3）、虫害 80→75；设 HYST_PCT 时对全部规则统一覆盖（运维调旋钮口径）。
DEFAULT_HYST_PCT = 5.0
GALE_HYST_PCT = 3.0  # 大风规则冻结退出阈值 14=17−3（docs/07 §4.2 表）

def _exit_threshold(rule: dict, hyst: float) -> float:
    if rule["op"] == "<":
        return rule["threshold"] + hyst
    return rule["threshold"] - hyst


def _build_rules(hyst: float | None = None) -> tuple[dict, ...]:
    """4 条冻结规则：进入条件与一期完全一致；退出阈值 = 进入阈值 ± 回差。"""
    if hyst is None:
        hyst = float(os.environ.get("HYST_PCT", DEFAULT_HYST_PCT))
    base = (
        {"rule_code": "RULE_DROUGHT", "metric": "soil_moisture", "op": "<", "threshold": 15.0,
         "label": "旱情", "hyst": DEFAULT_HYST_PCT},
        {"rule_code": "RULE_WATERLOGGING", "metric": "soil_moisture", "op": ">", "threshold": 85.0,
         "label": "渍害", "hyst": DEFAULT_HYST_PCT},
        {"rule_code": "RULE_GALE", "metric": "wind_speed", "op": ">", "threshold": 17.0,
         "label": "大风", "hyst": GALE_HYST_PCT},
        {"rule_code": "RULE_PEST", "metric": "pest_index", "op": ">", "threshold": 80.0,
         "label": "虫害", "hyst": DEFAULT_HYST_PCT},
    )
    rules = []
    for r in base:
        band = r["hyst"] if hyst == DEFAULT_HYST_PCT else hyst  # env 显式覆盖时统一口径
        rules.append({**r, "exit_op": ">=" if r["op"] == "<" else "<=",
                      "exit_threshold": _exit_threshold(r, band)})
    return tuple(rules)


RULES = _build_rules()  # 模块级冻结实例（种子 alert_rule 与状态机共用）

RULES_BY_METRIC: dict[str, tuple[dict, ...]] = {}
for _r in RULES:
    RULES_BY_METRIC.setdefault(_r["metric"], []).append(_r)
RULES_BY_METRIC = {k: tuple(v) for k, v in RULES_BY_METRIC.items()}

# ── 值域复核（04 号 §4.7 冻结量程，与 sim/app/rules.py 逐值一致；越界丢弃+计数）──────
# (量程低, 量程高)
METRIC_RANGES = {
    "soil_moisture": (5.0, 95.0),
    "air_temp":      (-5.0, 40.0),
    "wind_speed":    (0.0, 25.0),
    "rainfall":      (0.0, 60.0),
    "pest_index":    (0.0, 100.0),
    "water_level":   (0.0, 5.0),
}


def tripped_enter(rule: dict, value: float) -> bool:
    """进入条件：op ∈ {<, >}，严格比较（一期边界值口径：阈值本身不触发）。"""
    if rule["op"] == "<":
        return value < rule["threshold"]
    if rule["op"] == ">":
        return value > rule["threshold"]
    return False


def tripped_exit(rule: dict, value: float) -> bool:
    """退出条件：回差带外（exit_op ∈ {>=, <=}，边界值本身退出——docs/07 §4.2 单测口径）。"""
    if rule["exit_op"] == ">=":
        return value >= rule["exit_threshold"]
    if rule["exit_op"] == "<=":
        return value <= rule["exit_threshold"]
    return False


class HysteresisEngine:
    """回差状态机：feed 一条读数 → 返回本次由 NORMAL 转 TRIPPED 的规则列表（新告警候选）。

    进程内状态、非线程安全——仅 flush 线程串行调用（docs/07 §4.1 单线程管道，D-07-1）。
    """

    def __init__(self, rules: tuple[dict, ...] = RULES):
        self._rules = rules
        self._by_metric: dict[str, list[dict]] = {}
        for r in rules:
            self._by_metric.setdefault(r["metric"], []).append(r)
        self._state: dict[tuple[str, str], bool] = {}  # (device_code, rule_code) → TRIPPED?

    def seed_tripped(self, pairs: list[tuple[str, str]]) -> int:
        """重启重建（docs/07 §4.2）：查 alert_record 最近 1 条 state≠CLOSED 的 (device, rule)
        置 TRIPPED，防重启后同一持续异常立刻重复告警。返回置位数。"""
        n = 0
        for device_code, rule_code in pairs:
            key = (device_code, rule_code)
            if key not in self._state or not self._state[key]:
                self._state[key] = True
                n += 1
        return n

    def is_tripped(self, device_code: str, rule_code: str) -> bool:
        return bool(self._state.get((device_code, rule_code), False))

    def feed(self, device_code: str, metric: str, value: float) -> list[dict]:
        """一条读数 → 本次新触发（NORMAL→TRIPPED）的规则列表；TRIPPED 中不重复触发。"""
        hits: list[dict] = []
        for r in self._by_metric.get(metric, ()):
            key = (device_code, r["rule_code"])
            if self._state.get(key, False):
                if tripped_exit(r, value):
                    self._state[key] = False  # 回差带外退出 → NORMAL（不写库，闭环人工）
            elif tripped_enter(r, value):
                self._state[key] = True
                hits.append(r)
        return hits

    def tripped_slots(self) -> list[tuple[str, str]]:
        """当前全部 TRIPPED 槽位（观测/排障用）。"""
        return [k for k, v in self._state.items() if v]
