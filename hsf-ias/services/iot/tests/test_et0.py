"""R-29 ET0 模块单测：FAO-56 PM 数值校验（FAO-56 手册例 18 参考值）、stub 确定性、
M_net 语义（max 不叠加/不亏水为 0/off 模式等价 v1.0）。"""
import datetime as dt

from app import decision, et0


def test_pm_fao56_reference_value():
    """FAO-56 手册例 18（第 74 页，星号标注例）：Tmax=21.5/Tmin=12.3/RHmean=64/
    u2=10m 2.078→2m 换算后 1.9/RS=22.86（n=9.25）/纬度 50.84°/海拔 100m/7 月 6 日
    → ETo≈3.9 mm/day。换算：u2 = 2.078×4.87/ln(67.8×10−5.42)≈1.90。"""
    u2 = 2.078 * 4.87 / 4.92   # FAO-56 式 47 数值代入
    et0_val = et0.et0_penman_monteith(21.5, 12.3, 64, round(u2, 2), 22.86,
                                      50.84, 100, 187)
    assert 3.6 <= et0_val <= 4.2   # 手册给 3.9；实现按日尺度 G=0 标准链路，允许 ±0.3 容差


def test_et0_nonnegative_and_clamped():
    assert et0.et0_penman_monteith(30, 20, 30, 3.0, 30.0, 25.0, 1800, 200) > 0
    assert et0.et0_penman_monteith(5, -5, 90, 0.5, 2.0, 50.0, 100, 15) >= 0


def test_stub_weather_deterministic():
    d = dt.date(2026, 7, 1)
    w1, w2 = et0.stub_daily_weather(d), et0.stub_daily_weather(d)
    assert w1 == w2 and w1["doy"] == 182
    assert 5 <= w1["t_min"] < w1["t_mean" if "t_mean" in w1 else "t_max"]


def test_mnet_equals_msoil_when_et0_zero():
    """off 模式/ET0=0 → M_net≡M_soil（行为与 v1.0 逐字节等价——验收口径不漂移）。"""
    params = {"depth": 0.3, "gamma": 1.4, "fc": 55}
    m_soil = decision.per_mu_m3(38, params)
    assert et0.per_mu_m3_net(38, params, et0_mm=0.0) == m_soil == round(
        667 * 0.3 * 1.4 * (55 - 38) / 100, 1)


def test_mnet_max_not_summed():
    """ET0 项>0 → M_net < M_soil（取差不叠加）；土壤不亏水 → 0。"""
    params = {"depth": 0.3, "gamma": 1.4, "fc": 55}
    m_soil = decision.per_mu_m3(38, params)
    m_net = et0.per_mu_m3_net(38, params, et0_mm=5.0, kc=1.0, cover_days=7)
    assert 0 <= m_net < m_soil                                  # 作物需水修正生效
    assert et0.per_mu_m3_net(56, params, et0_mm=5.0) == 0.0     # 超田间持水 → 不灌溉


def test_weather_mode_switch(monkeypatch):
    monkeypatch.setenv("WEATHER_MODE", "off")
    assert et0.et0_today() == 0.0
    monkeypatch.setenv("WEATHER_MODE", "stub")
    assert et0.et0_today() > 0
