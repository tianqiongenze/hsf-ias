"""sim TDD（二期 2.0）：观测值生成器——值域量程/极端注入/确定性（04 号 §4.7）。

二期职责边界（docs/07 §2.5）：sim 只造数发数，规则引擎/告警职责已移交 iot 服务
（回差状态机单测见 services/iot/tests/test_rules.py）——本文件不再含任何阈值断言。
"""
import random

from app import rules
from app.rules import METRIC_RANGES, metrics, next_value


def test_metric_set_frozen():
    """6 类 metric（04 号 §4.7 冻结命名）不增不减。"""
    assert metrics() == ("soil_moisture", "air_temp", "wind_speed",
                         "rainfall", "pest_index", "water_level")


def test_generated_values_within_ranges():
    """生成值恒在 04 号 §4.7 量程内（含极端注入带）。"""
    rng = random.Random(42)
    for m, (lo, hi, _, _, _) in METRIC_RANGES.items():
        for _ in range(2000):
            v = next_value(m, rng)
            assert lo <= v <= hi, f"{m}={v} 越界"


def test_normal_band_sampling():
    """常态读数落在正常带（极端注入概率 2%——P(2000 条全正常带)≈0，极端带可能少量出现）。"""
    rng = random.Random(42)
    normal = 0
    total = 500
    for _ in range(total):
        _, _, n_lo, n_hi, _ = METRIC_RANGES["wind_speed"]
        v = next_value("wind_speed", rng)
        if n_lo <= v <= n_hi:
            normal += 1
    assert normal / total > 0.9  # 期望 98%，余量防随机波动


def test_extremes_within_extreme_bands():
    """极端注入必落入冻结极端带：soil_moisture 旱情段 6~14.5 / 渍害段 86~93。"""
    rng = random.Random(7)
    seen_low = seen_high = False
    for _ in range(50000):
        v = next_value("soil_moisture", rng)
        _, _, n_lo, n_hi, _ = METRIC_RANGES["soil_moisture"]
        if not (n_lo <= v <= n_hi):
            assert 6.0 <= v <= 14.5 or 86.0 <= v <= 93.0
            seen_low = seen_low or v <= 14.5
            seen_high = seen_high or v >= 86.0
        if seen_low and seen_high:
            break
    assert seen_low and seen_high  # 极端注入可覆盖旱情/渍害两段（iot 告警 E2E 前提）


def test_no_rule_metrics_stay_in_normal_band():
    """气温/降雨/水位无极端带：恒在正常带内。"""
    rng = random.Random(42)
    for m in ("air_temp", "rainfall", "water_level"):
        _, _, n_lo, n_hi, extreme = METRIC_RANGES[m]
        assert extreme is None
        for _ in range(1000):
            assert n_lo <= next_value(m, rng) <= n_hi


def test_deterministic_with_seeded_rng():
    """同一种子可复现（模拟逻辑可解释、可测试）。"""
    assert next_value("wind_speed", random.Random(1)) == next_value("wind_speed", random.Random(1))


def test_no_rule_constants_in_sim():
    """static_gate 口径（07 号 §2.5/§6.4）：sim 不再持有任何规则与阈值常量。"""
    assert not hasattr(rules, "RULES")
    assert not hasattr(rules, "evaluate")
    assert not hasattr(rules, "match")
