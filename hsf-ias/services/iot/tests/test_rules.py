"""iot TDD：规则引擎 4 条冻结规则 + 滞后回差状态机边界值（docs/07 §4.2 单测口径）。

进入阈值与一期完全一致（05 §7.2/FR-503：14.9/15.1、84.9/85.1、16.9/17.1、79.9/80.1）；
退出阈值 = 冻结表值（20/80/14/75），边界值本身退出（≥/≤）。纯函数/进程内状态，无外部依赖。
"""
import random

from app import rules
from app.rules import DEFAULT_HYST_PCT, HysteresisEngine, RULES, _build_rules

BASIC = _build_rules(DEFAULT_HYST_PCT)  # 缺省回差下的冻结规则集（与 RULES 一致）


def _engine() -> HysteresisEngine:
    return HysteresisEngine(BASIC)


def test_rules_frozen_set():
    """进入条件与一期冻结一致（05 §7.2/04 §4.10）。"""
    got = {(r["rule_code"], r["metric"], r["op"], r["threshold"]) for r in RULES}
    assert got == {("RULE_DROUGHT", "soil_moisture", "<", 15.0),
                   ("RULE_WATERLOGGING", "soil_moisture", ">", 85.0),
                   ("RULE_GALE", "wind_speed", ">", 17.0),
                   ("RULE_PEST", "pest_index", ">", 80.0)}


def test_exit_thresholds_frozen_table():
    """退出阈值 = docs/07 §4.2 冻结表：15→20、85→80、17→14、80→75；回差方向与进入相反。"""
    got = {(r["rule_code"], r["exit_op"], r["exit_threshold"]) for r in BASIC}
    assert got == {("RULE_DROUGHT", ">=", 20.0),
                   ("RULE_WATERLOGGING", "<=", 80.0),
                   ("RULE_GALE", "<=", 14.0),
                   ("RULE_PEST", "<=", 75.0)}


def test_enter_boundary_drought():
    """墒情进入边界：14.9 触发 / 15.1、15.0 不触发（严格 <，一期口径不变）。"""
    e = _engine()
    assert [r["rule_code"] for r in e.feed("S1", "soil_moisture", 14.9)] == ["RULE_DROUGHT"]
    e2 = _engine()
    assert e2.feed("S1", "soil_moisture", 15.1) == []
    assert e2.feed("S1", "soil_moisture", 15.0) == []


def test_enter_boundary_waterlogging_gale_pest():
    e = _engine()
    assert [r["rule_code"] for r in e.feed("S1", "soil_moisture", 85.1)] == ["RULE_WATERLOGGING"]
    e = _engine()
    assert [r["rule_code"] for r in e.feed("S1", "wind_speed", 17.1)] == ["RULE_GALE"]
    e = _engine()
    assert [r["rule_code"] for r in e.feed("S1", "pest_index", 80.1)] == ["RULE_PEST"]


def test_hysteresis_drought_full_cycle():
    """冻结单测口径：14.9 触发；随后 16 不重复触发（未退出）；20 → NORMAL；再 14.9 → 新告警。"""
    e = _engine()
    assert len(e.feed("S1", "soil_moisture", 14.9)) == 1        # NORMAL → TRIPPED
    assert e.feed("S1", "soil_moisture", 16.0) == []            # 滞回带内抑制
    assert e.feed("S1", "soil_moisture", 19.9) == []            # 仍 TRIPPED（19.9<20 未退出）
    assert e.feed("S1", "soil_moisture", 20.0) == []            # 退出（边界值本身退出）
    assert not e.is_tripped("S1", "RULE_DROUGHT")
    assert len(e.feed("S1", "soil_moisture", 14.9)) == 1        # 再进入 → 新告警（新 trig_time）


def test_hysteresis_waterlogging_cycle():
    """85.1 触发；84.9→80 区间抑制；80 → NORMAL（冻结表退出值）。"""
    e = _engine()
    assert e.feed("S1", "soil_moisture", 85.1)
    for v in (84.9, 82.0, 80.1):
        assert e.feed("S1", "soil_moisture", v) == []
    assert e.feed("S1", "soil_moisture", 80.0) == []            # 退出
    assert not e.is_tripped("S1", "RULE_WATERLOGGING")
    assert len(e.feed("S1", "soil_moisture", 85.1)) == 1


def test_hysteresis_gale_cycle():
    """17.1 触发；14~17 抑制；14 → NORMAL；17.1 再次进入。"""
    e = _engine()
    assert e.feed("S1", "wind_speed", 17.1)
    for v in (16.9, 15.0, 14.1):
        assert e.feed("S1", "wind_speed", v) == []
    assert e.feed("S1", "wind_speed", 14.0) == []
    assert not e.is_tripped("S1", "RULE_GALE")
    assert len(e.feed("S1", "wind_speed", 17.1)) == 1


def test_hysteresis_pest_cycle():
    """80.1 触发；75~80 抑制；75 → NORMAL。"""
    e = _engine()
    assert e.feed("S1", "pest_index", 80.1)
    for v in (79.9, 77.0, 75.1):
        assert e.feed("S1", "pest_index", v) == []
    assert e.feed("S1", "pest_index", 75.0) == []
    assert not e.is_tripped("S1", "RULE_PEST")


def test_reenter_while_still_extreme_no_new_alert():
    """TRIPPED 中再次满足进入条件 → 不重复触发（回差防告警抖动的核心）。"""
    e = _engine()
    assert e.feed("S1", "soil_moisture", 10.0)
    assert e.feed("S1", "soil_moisture", 8.0) == []
    assert e.feed("S1", "soil_moisture", 14.9) == []
    assert e.is_tripped("S1", "RULE_DROUGHT")


def test_device_slot_isolation():
    """槽位按 (device_code, rule_code) 隔离：S1 告警不影响 S2。"""
    e = _engine()
    assert e.feed("S1", "wind_speed", 20.0)
    assert e.feed("S2", "wind_speed", 20.0)                     # S2 独立触发
    assert e.feed("S1", "wind_speed", 16.0) == []               # S1 抑制
    assert len(e.feed("S2", "wind_speed", 16.0)) == 0
    assert e.is_tripped("S1", "RULE_GALE") and e.is_tripped("S2", "RULE_GALE")


def test_no_rule_metrics_never_alert():
    """气温/降雨/水位无规则：任何取值不产生告警（一期口径不变）。"""
    e = _engine()
    for m in ("air_temp", "rainfall", "water_level"):
        for v in (-100.0, 0.0, 50.0, 999.0):
            assert e.feed("S1", m, v) == []


def test_rebuild_seed_tripped():
    """重启重建：seed_tripped 置 TRIPPED 后，持续异常读数不再立即重复告警；退出后可再触发。"""
    e = _engine()
    assert e.seed_tripped([("S1", "RULE_DROUGHT")]) == 1
    assert e.feed("S1", "soil_moisture", 14.9) == []            # 重建态抑制重复告警
    assert e.feed("S1", "soil_moisture", 20.0) == []            # 退出
    assert len(e.feed("S1", "soil_moisture", 14.9)) == 1        # 再进入 → 新告警


def test_seed_tripped_idempotent():
    e = _engine()
    e.seed_tripped([("S1", "RULE_GALE")])
    assert e.seed_tripped([("S1", "RULE_GALE")]) == 0           # 已 TRIPPED 不重复计数
    assert e.tripped_slots() == [("S1", "RULE_GALE")]


def test_env_hyst_override_uniform():
    """HYST_PCT 显式覆盖 → 全部规则统一回差带（冻结缺省=5 时大风保持表值 14）。"""
    uniform3 = _build_rules(3.0)
    got = {r["rule_code"]: r["exit_threshold"] for r in uniform3}
    assert got == {"RULE_DROUGHT": 18.0, "RULE_WATERLOGGING": 82.0,
                   "RULE_GALE": 14.0, "RULE_PEST": 77.0}
    default = {r["rule_code"]: r["exit_threshold"] for r in _build_rules(DEFAULT_HYST_PCT)}
    assert default == {"RULE_DROUGHT": 20.0, "RULE_WATERLOGGING": 80.0,
                       "RULE_GALE": 14.0, "RULE_PEST": 75.0}


def test_metric_range_guard():
    """值域复核（04 §4.7 冻结量程）：越界值供 mqtt_sub.parse_message 丢弃。"""
    assert rules.METRIC_RANGES["soil_moisture"] == (5.0, 95.0)
    assert rules.METRIC_RANGES["wind_speed"] == (0.0, 25.0)
    lo, hi = rules.METRIC_RANGES["pest_index"]
    assert (lo, hi) == (0.0, 100.0)


def test_long_stream_state_consistency():
    """随机长流：状态机输出与独立重放一致（确认无隐藏状态泄漏）。"""
    rng = random.Random(42)
    e = _engine()
    tripped = 0
    for _ in range(5000):
        v = rng.uniform(5.0, 95.0)
        tripped += len(e.feed("S1", "soil_moisture", v))
    # 独立重放（重建同种子）应得相同触发次数
    e2 = _engine()
    rng2 = random.Random(42)
    replay = 0
    for _ in range(5000):
        replay += len(e2.feed("S1", "soil_moisture", rng2.uniform(5.0, 95.0)))
    assert tripped == replay
