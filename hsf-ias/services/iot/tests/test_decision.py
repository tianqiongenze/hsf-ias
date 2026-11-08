"""iot TDD：灌溉决策——冻结公式/阈值边界/6h 窗口去重键/自动闭环（docs/07 §4.3 单测口径）。

纯函数层（无外部依赖）：决策落库幂等由 uk_advice_dedup + INSERT IGNORE 承接（app/repo.py），
本组单测冻结其幂等键构造与状态判定逻辑。
"""
import datetime as dt

from app import decision

PARAMS = dict(decision.DEFAULTS)  # 缺省冻结参数（40/55/55/0.30/1.40/6）

T_0723 = dt.datetime(2026, 11, 8, 7, 23, 45, 123456)   # 落 6~12 窗口
T_1159 = dt.datetime(2026, 11, 8, 11, 59, 59)
T_1200 = dt.datetime(2026, 11, 8, 12, 0, 0)
T_1800 = dt.datetime(2026, 11, 8, 18, 0, 0)
T_0000 = dt.datetime(2026, 11, 8, 0, 0, 0)
T_2359 = dt.datetime(2026, 11, 8, 23, 59, 59)


def test_formula_frozen_example():
    """冻结示例：θ_now=38 → M = 667×0.3×1.4×(55−38)/100 ≈ 47.6 m³/亩（07 号 §4.3）。"""
    assert decision.per_mu_m3(38.0, PARAMS) == 47.6


def test_formula_zero_deficit_and_saturation():
    """墒情达田间持水率 → 缺口 0；缺水越多建议量越大（单调性）。"""
    assert decision.per_mu_m3(55.0, PARAMS) == 0.0
    assert decision.per_mu_m3(20.0, PARAMS) > decision.per_mu_m3(30.0, PARAMS)


def test_total_m3_area_missing_is_none():
    """面积缺失（land_parcel.area_mu=NULL）→ total_m3=NULL（DDL 允许空）。"""
    assert decision.total_m3(47.6, None) is None
    assert decision.total_m3(47.6, 12.0) == 571.2


def test_trigger_threshold_boundaries():
    """墒情 <40 生成建议：39.9 生成 / 40.1、40.0 不生成（严格 <，冻结单测口径）。"""
    assert decision.needs_advice(39.9, PARAMS) is True
    assert decision.needs_advice(40.1, PARAMS) is False
    assert decision.needs_advice(40.0, PARAMS) is False


def test_auto_close_boundaries():
    """墒情 ≥55 自动闭环：54.9 保持 OPEN / 55.0 闭环（回差 15 个百分点）。"""
    assert decision.should_auto_close(54.9, PARAMS) is False
    assert decision.should_auto_close(55.0, PARAMS) is True
    assert decision.should_auto_close(75.0, PARAMS) is True


def test_window_key_alignment():
    """6h 窗口起点按本地 0/6/12/18 点对齐：dedup_key={parcel}:{YYYYMMDDHH}。"""
    assert decision.window_key("L5306000001", T_0723) == "L5306000001:2026110806"
    assert decision.window_key("L5306000001", T_1159) == "L5306000001:2026110806"
    assert decision.window_key("L5306000001", T_1200) == "L5306000001:2026110812"
    assert decision.window_key("L5306000001", T_1800) == "L5306000001:2026110818"
    assert decision.window_key("L5306000001", T_0000) == "L5306000001:2026110800"
    assert decision.window_key("L5306000001", T_2359) == "L5306000001:2026110818"


def test_same_window_dedup_key_stable():
    """同窗口第二次极端读数 → 同幂等键（uk_advice_dedup + INSERT IGNORE → 行数不变）。"""
    a = decision.window_key("L5306000002", dt.datetime(2026, 11, 8, 13, 5, 0))
    b = decision.window_key("L5306000002", dt.datetime(2026, 11, 8, 18, 0, 0) -
                            dt.timedelta(seconds=1))
    assert a == b == "L5306000002:2026110812"


def test_window_key_differs_across_window_and_parcel():
    """跨窗口/跨地块键不同：6h 后同地块再触发 → 新建议（新窗口键）。"""
    assert decision.window_key("L5306000003", T_1200) != decision.window_key("L5306000003", T_1800)
    assert decision.window_key("L5306000003", T_1200) != decision.window_key("L5306000004", T_1200)


def test_window_key_char_padding_stripped():
    """CHAR(20) 列宽预留的尾部空格不进幂等键（CONVENTIONS §2.3 宁可宽不可窄）。"""
    assert decision.window_key("L5306000001        ", T_0723) == \
        decision.window_key("L5306000001", T_0723)


def test_dedup_hours_override():
    """IRR_DEDUP_HOURS 旋钮：dedup_hours=4 → 窗口 0/4/8/12 对齐（运维调参口径）。"""
    ts = dt.datetime(2026, 11, 8, 9, 30, 0)
    assert decision.window_key("L5306000001", ts, dedup_hours=4) == "L5306000001:2026110808"


def test_params_env_defaults_frozen():
    """缺省参数与 docs/07 §4.3 冻结表一致（env 未设时的行为可预期）。"""
    assert decision.DEFAULTS == {"start": 40.0, "stop": 55.0, "fc": 55.0,
                                 "depth": 0.30, "gamma": 1.40, "dedup_hours": 6}
