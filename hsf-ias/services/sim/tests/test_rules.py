"""sim TDD：规则引擎 4 条冻结规则 + 边界值（FR-503）+ 观测生成量程（04 号 §4.7）。"""
import random

from app import rules
from app.rules import METRIC_RANGES, RULES, evaluate, match, metrics, next_value


def test_rules_frozen_set():
    """一期 4 条：旱情/渍害/大风/虫害，metric+op+threshold 与 05 号 §7.2 一致。"""
    got = {(r["rule_code"], r["metric"], r["op"], r["threshold"]) for r in RULES}
    assert got == {("RULE_DROUGHT", "soil_moisture", "<", 15.0),
                   ("RULE_WATERLOGGING", "soil_moisture", ">", 85.0),
                   ("RULE_GALE", "wind_speed", ">", 17.0),
                   ("RULE_PEST", "pest_index", ">", 80.0)}


def test_boundary_drought():
    """墒情边界：14.9 触发 / 15.1 不触发（阈值本身不触发：严格 <）。"""
    assert [r["rule_code"] for r in evaluate("soil_moisture", 14.9)] == ["RULE_DROUGHT"]
    assert evaluate("soil_moisture", 15.1) == []
    assert evaluate("soil_moisture", 15.0) == []


def test_boundary_waterlogging():
    """墒情上界：85.1 触发渍害 / 84.9 不触发。"""
    assert [r["rule_code"] for r in evaluate("soil_moisture", 85.1)] == ["RULE_WATERLOGGING"]
    assert evaluate("soil_moisture", 84.9) == []
    assert evaluate("soil_moisture", 85.0) == []


def test_boundary_gale():
    """风速边界：17.1 触发 / 16.9 不触发。"""
    assert [r["rule_code"] for r in evaluate("wind_speed", 17.1)] == ["RULE_GALE"]
    assert evaluate("wind_speed", 16.9) == []


def test_boundary_pest():
    """虫情指数边界：80.1 触发 / 79.9 不触发。"""
    assert [r["rule_code"] for r in evaluate("pest_index", 80.1)] == ["RULE_PEST"]
    assert evaluate("pest_index", 79.9) == []


def test_no_rule_metrics_never_alert():
    """气温/降雨/水位一期无规则：任何取值不产生告警。"""
    for m in ("air_temp", "rainfall", "water_level"):
        for v in (-100.0, 0.0, 50.0, 999.0):
            assert evaluate(m, v) == []


def test_metric_isolation():
    """规则按 metric 隔离：soil_moisture 的值不会命中风速/虫情规则。"""
    assert evaluate("soil_moisture", 90.0) == [RULES[1]]  # 仅渍害
    assert match(RULES[2], "soil_moisture", 90.0) is False


def test_evaluate_returns_list_for_multi_rule_contract():
    """契约③：一条读数命中多条规则 → 多条告警（返回列表形态承载）。"""
    hits = evaluate("soil_moisture", 10.0)
    assert isinstance(hits, list) and len(hits) == 1  # 冻结 4 条下互斥，形态仍为列表


def test_generated_values_within_ranges():
    """生成值恒在 04 号 §4.7 量程内（含极端注入带）。"""
    rng = random.Random(42)
    for m, (lo, hi, _, _, _) in METRIC_RANGES.items():
        for _ in range(2000):
            v = next_value(m, rng)
            assert lo <= v <= hi, f"{m}={v} 越界"


def test_extremes_eventually_trigger_every_rule():
    """持续 tick 的极端注入必须能让 4 条规则都产生真实告警（E2E 前提）。"""
    rng = random.Random(7)
    seen: set[str] = set()
    for m in metrics():
        for _ in range(20000):
            for hit in evaluate(m, next_value(m, rng)):
                seen.add(hit["rule_code"])
    assert seen == {"RULE_DROUGHT", "RULE_WATERLOGGING", "RULE_GALE", "RULE_PEST"}


def test_deterministic_with_seeded_rng():
    """同一种子可复现（README 模拟逻辑可解释、可测试）。"""
    assert next_value("wind_speed", random.Random(1)) == next_value("wind_speed", random.Random(1))
