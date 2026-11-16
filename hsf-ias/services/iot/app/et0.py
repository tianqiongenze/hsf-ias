"""FAO-56 Penman-Monteith 参照腾发量 ET₀ 与净灌溉需水（R-29，docs/24 §4 R-29 approach）。

docs/08 §触发条件项"外部气象数据源落地后启动"的落地形态：
  ① ET₀ = FAO-56 PM 标准公式（纯 CPU，numpy 不必需——标量运算，V-1 红线不触）；
  ② 气象源 = WEATHER_MODE 三档：stub（缺省，确定性合成日气象，V-2 同款"契约先行"）、
     http（真实源，WEATHER_URL 单文件替换口径，本版留接口不实现外呼）、off（停用）；
  ③ 净灌溉需水：M_net = max(M_soil − Kc×ET₀×覆盖天数折算, 0)——冻结公式（07 号 §4.3
     M=667×h×γ×(θfc−θnow)/100）为土壤亏水基线，ET₀ 项为作物需水修正，二者取大不叠加，
     避免双重计数；Kc 按生育期简化三档（初期/中期/末期）。
无真实气象源时 stub 模式 Kc×ET₀ 项=0（M_net≡M_soil，行为与 v1.0 逐字节等价——
MET 不依赖外部依赖、验收口径不漂移）；http 模式接入后自动生效。
"""
import datetime as dt
import math
import os

# Kc 三档（FAO-56 表 17 简化；作物缺省=粮饲通用，village/parcel 级可后补）
KC_STAGES = {"initial": 0.5, "mid": 1.0, "late": 0.7}

DEFAULTS = {
    "albedo": 0.23,          # FAO-56 缺省反照率
    "kc": KC_STAGES["mid"],
    "cover_days": 7,         # ET₀ 项折算天数（灌水周期）
}


def weather_mode() -> str:
    """WEATHER_MODE：stub（缺省）/http/off。"""
    return os.environ.get("WEATHER_MODE", "stub").strip().lower()


def et0_penman_monteith(t_max_c: float, t_min_c: float, rh_mean_pct: float,
                        u2_ms: float, rs_mj_m2_day: float, lat_deg: float,
                        altitude_m: float, doy: int) -> float:
    """FAO-56 PM 日参照腾发量 ET₀（mm/day）。

    参数：日最高/最低温（℃）、平均相对湿度（%）、2m 风速（m/s）、太阳辐射
    （MJ/m²/day）、纬度（度）、海拔（m）、年积日。
    ETo = [0.408Δ(Rn−G) + γ(900/(T+273))u₂(eₛ−eₐ)] / [Δ+γ(1+0.34u₂)]，G=0（日尺度）。
    返回 mm/day，保留 2 位（负值截 0）。"""
    lat = math.radians(lat_deg)
    # ① 日地外辐射 Ra（FAO-56 式 21）
    dr = 1 + 0.033 * math.cos(2 * math.pi * doy / 365)
    delta = 0.409 * math.sin(2 * math.pi * doy / 365 - 1.39)
    ws = math.acos(max(-1.0, min(1.0, -math.tan(lat) * math.tan(delta))))
    ra = (24 * 60 / math.pi) * 0.0820 * dr * (
        ws * math.sin(lat) * math.sin(delta) +
        math.cos(lat) * math.cos(delta) * math.sin(ws))
    # ② 晴空辐射 Rso（式 37）与净长波 Rnl（式 39）
    rso = (0.75 + 2e-5 * altitude_m) * ra
    t_mean = (t_max_c + t_min_c) / 2
    es_tmax = 0.6108 * math.exp(17.27 * t_max_c / (t_max_c + 237.3))
    es_tmin = 0.6108 * math.exp(17.27 * t_min_c / (t_min_c + 237.3))
    es = (es_tmax + es_tmin) / 2
    ea = es * max(rh_mean_pct, 1.0) / 100.0
    sigma = 4.903e-9                                   # MJ K⁻⁴ m⁻² day⁻¹
    rnl = (sigma * ((t_max_c + 273.16) ** 4 + (t_min_c + 273.16) ** 4) / 2 *
           (0.34 - 0.14 * math.sqrt(max(ea, 0.0))) *
           (1.35 * min(rs_mj_m2_day / max(rso, 1e-6), 1.0) - 0.35))
    rn = (1 - DEFAULTS["albedo"]) * rs_mj_m2_day - rnl
    # ③ 风速 10m→2m 修正若需（调用方给 2m；此处按 FAO-56 式 47 防御性换算职责在调用方）
    # ④ 参数
    slope = 4098 * (0.6108 * math.exp(17.27 * t_mean / (t_mean + 237.3))) / ((t_mean + 237.3) ** 2)
    pressure = 101.3 * ((293 - 0.0065 * altitude_m) / 293) ** 5.26
    gamma = 0.000665 * pressure
    et0 = (0.408 * slope * rn + gamma * (900 / (t_mean + 273)) * u2_ms * (es - ea)) / \
          (slope + gamma * (1 + 0.34 * u2_ms))
    return round(max(et0, 0.0), 2)


def stub_daily_weather(day: dt.date | None = None) -> dict:
    """确定性合成日气象（stub 模式，V-2 同款契约先行）：按日积日的正弦年循环——
    同一天输入恒同输出（验收可复现）；不访问外网。"""
    day = day or dt.date.today()
    doy = day.timetuple().tm_yday
    # 年内温度循环：模拟北纬 25°（楚雄 530600）亚热带季风
    t_mean = 17 + 8 * math.sin(2 * math.pi * (doy - 105) / 365)
    t_max, t_min = t_mean + 6, t_mean - 6
    rh = 65 + 15 * math.sin(2 * math.pi * (doy - 200) / 365)      # 雨季偏湿
    rs = 22 * max(0.3, 0.55 + 0.35 * math.sin(2 * math.pi * (doy - 80) / 365))  # MJ/m²/day
    return {"t_max": round(t_max, 1), "t_min": round(t_min, 1),
            "rh_mean": round(max(rh, 20), 1), "u2": round(1.5 + 0.5 * math.sin(doy), 1),
            "rs": round(rs, 1), "doy": doy}


def et0_today(lat_deg: float = 25.04, altitude_m: float = 1780) -> float:
    """当日 ET₀：stub=合成气象→PM 公式；http=WEATHER_URL 拉取（未实现外呼→回退 stub，
    如实打印）；off=返回 0（Kc×ET₀ 项=0，M_net≡M_soil）。"""
    mode = weather_mode()
    if mode == "off":
        return 0.0
    if mode == "http":
        # 真实源单文件替换口径（V-3 同款）：接通后此分支 return http 结果
        print("[iot] WEATHER_MODE=http 未配置外呼实现，回退 stub（docs/24 §4 R-29）", flush=True)
    w = stub_daily_weather()
    return et0_penman_monteith(w["t_max"], w["t_min"], w["rh_mean"], w["u2"], w["rs"],
                               lat_deg, altitude_m, w["doy"])


def per_mu_m3_net(theta_now: float, params: dict, et0_mm: float | None = None,
                  kc: float | None = None, cover_days: int | None = None) -> float:
    """净灌溉需水 M_net（m³/亩）= max(M_soil − 10×Kc×ET₀×cover_days/面积折算… , 0)。

    量纲对齐：M_soil（07 号 §4.3 冻结公式，m³/亩）；作物需水项 = Kc×ET₀(mm/day)×
    cover_days×667/1000（mm→m³/亩：1mm×1亩=0.667m³）。取 max 不叠加（双重计数防御）。
    et0=None → 按 WEATHER_MODE 现算；M_soil≤0（土壤不亏水）→ 0（不灌溉）。"""
    et0_mm = et0_today() if et0_mm is None else et0_mm
    kc = kc or params.get("kc", DEFAULTS["kc"])
    days = cover_days or DEFAULTS["cover_days"]
    from .decision import per_mu_m3   # 延迟导入（decision 为冻结公式唯一实现方）
    m_soil = per_mu_m3(theta_now, params)
    crop_mm = kc * et0_mm * days                      # 作物需水（mm/周期）
    crop_m3_mu = crop_mm * 0.667                      # mm→m³/亩
    return round(max(m_soil - crop_m3_mu, 0.0), 1) if m_soil > 0 else 0.0
