"""iot 灌溉决策（纯函数层，无外部依赖）：阈值 + 回差 + 用水量公式 + 6h 窗口去重键。

对齐：hsf/docs/07 §4.3（参数/公式/状态机/单测断言冻结）、docs/07 §4.4（irrigation_advice DDL）。
用水量公式（冻结）：M（m³/亩）= 667 × h × γ × (θ_fc − θ_now) / 100；
总灌溉量 Total = M × area_mu（面积缺失为 NULL）。工程简化模型，输出为"决策参考值"。
"""
import os

# ── 参数（env 可调，缺省冻结——docs/07 §4.3 表）─────────────────────────────────
DEFAULTS = {
    "start": 40.0,    # IRR_START_PCT 触发阈值（%）
    "stop": 55.0,     # IRR_STOP_PCT 恢复阈值（回差 15 个百分点，防抖动）
    "fc": 55.0,       # IRR_FC_PCT 田间持水率 θ_fc（%，与 sensor 量纲一致，04 §4.7 量程 5~95）
    "depth": 0.30,    # IRR_DEPTH_M 计划湿润层深度 h（m），粮油作物典型根系层
    "gamma": 1.40,    # IRR_SOIL_GAMMA 土壤干容重 γ（t/m³），红壤/水稻土典型值
    "dedup_hours": 6, # IRR_DEDUP_HOURS 同地块去重窗口（h）
}


def load_params() -> dict:
    """启动时读 env（缺省冻结值）；repo 持有后传入各纯函数，便于单测显式构造。"""
    return {
        "start": float(os.environ.get("IRR_START_PCT", DEFAULTS["start"])),
        "stop": float(os.environ.get("IRR_STOP_PCT", DEFAULTS["stop"])),
        "fc": float(os.environ.get("IRR_FC_PCT", DEFAULTS["fc"])),
        "depth": float(os.environ.get("IRR_DEPTH_M", DEFAULTS["depth"])),
        "gamma": float(os.environ.get("IRR_SOIL_GAMMA", DEFAULTS["gamma"])),
        "dedup_hours": int(os.environ.get("IRR_DEDUP_HOURS", DEFAULTS["dedup_hours"])),
    }


def per_mu_m3(theta_now: float, params: dict) -> float:
    """M（m³/亩）= 667 × h × γ × (θ_fc − θ_now) / 100，保留 1 位小数。

    冻结示例：θ_now=38 → 667×0.3×1.4×(55−38)/100 ≈ 47.6 m³/亩（典型灌水定额 20~60 区间）。"""
    m = 667.0 * params["depth"] * params["gamma"] * (params["fc"] - theta_now) / 100.0
    return round(m, 1)


def total_m3(per_mu: float, area_mu: float | None) -> float | None:
    """Total = M × area_mu；地块面积缺失（land_parcel.area_mu 为 NULL）→ None。"""
    if area_mu is None:
        return None
    return round(per_mu * float(area_mu), 1)


def window_key(parcel_code: str, recorded_at, dedup_hours: int = DEFAULTS["dedup_hours"]) -> str:
    """6h 去重幂等键：`{parcel_code}:{窗口起点}`（uk_advice_dedup，docs/07 §4.3）。

    窗口起点 = recorded_at 按本地时间 0/6/12/18 点对齐（dedup_hours=6 的等价冻结实现，
    dedup_hours 可经 IRR_DEDUP_HOURS 调整）；parcel_code 去尾部空格（CHAR 列宽预留，
    CONVENTIONS §2.3）。同一窗口内重复极端读数 → 同键 → INSERT IGNORE 行数不变。"""
    start = recorded_at.replace(hour=(recorded_at.hour // int(dedup_hours)) * int(dedup_hours),
                                minute=0, second=0, microsecond=0)
    return f"{parcel_code.strip()}:{start:%Y%m%d%H}"


def needs_advice(theta_now: float, params: dict) -> bool:
    """墒情 < IRR_START(40) → 生成建议（严格小于，边界单测：39.9 生成 / 40.1 不生成）。"""
    return theta_now < params["start"]


def should_auto_close(theta_now: float, params: dict) -> bool:
    """墒情 ≥ IRR_STOP(55) → OPEN 建议自动闭环（边界单测：54.9 保持 OPEN / 55.0 闭环）。"""
    return theta_now >= params["stop"]
