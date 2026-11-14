"""R-13（docs/24 §4，docs/23 复核附录#12）单测：NDVI 预警落库粒度——同景多地块降幅静默丢失修复。

方案 b（trig_time 幂等键内嵌地块序号偏移，不改 DDL、存量数据不迁移）：
  1. 同景双地块降幅 → 两次 alert INSERT、trig_time 互异（uk_rule_reading 不再三元组全同）、
     parcel_code 各异；
  2. 偏移为景内全地块确定性地块序（仅降幅子集变化不改变已落地块的偏移 → 重解译幂等）；
  3. 重放同景 → trig_time 序列逐次相同（INSERT IGNORE 去重语义保持）；
  4. 步长 1000μs=1ms（冻结 DDL trig_time=DATETIME(3) 列精度约束，亚毫秒偏移会被取整折叠）。
"""
import sys
from datetime import datetime as _dt
from unittest import mock

sys.path.insert(0, "/srv")

from app import repo


def _vigors(codes_means: list[tuple[str, float]]) -> list[dict]:
    out = []
    for code, mean in codes_means:
        out.append({"parcel_code": code, "ndvi_mean": mean, "ndvi_min": mean - 0.02,
                    "ndvi_max": mean + 0.02, "vigor_level": "长势差", "pixel_count": 9})
    return out


def _run_fall_alerts(vigors, acq="2026-11-08 09:30:00", prev_mean=0.55):
    """mock hsf_common.db 跑 ndvi_fall_alerts；返回 (execute 参数列表, mock 对象)。"""
    with mock.patch("hsf_common.db.query") as q, mock.patch("hsf_common.db.execute") as e:
        e.return_value = 1
        q.return_value = {"ndvi_mean": prev_mean}       # prev_mean 命中上一景（降幅触发）
        repo.ndvi_fall_alerts(vigors, "RS-TEST-SCENE", acq)
        return [c.args for c in e.call_args_list], e


def _alert_inserts(calls):
    """抽取 alert_record 的 INSERT IGNORE 调用参数（其余为 domain_event/notify 写入）。"""
    return [args for sql, *rest in calls
            if "INSERT IGNORE INTO hsf_biz.alert_record" in sql
            for args in rest]


def test_two_parcel_fall_inserts_distinct_trig_time_and_parcel():
    """回归核心：同景双地块降幅 → 2 行 alert（修复前 uk_rule_reading 三元组全同仅落首条）。"""
    calls, _ = _run_fall_alerts(_vigors([("L5306000001", 0.12), ("L5306000003", 0.10)]))
    alerts = _alert_inserts(calls)
    assert len(alerts) == 2, "双地块降幅必须落 2 行（R-13 修复目标：多地块不再静默丢失）"
    parcels = [a[3] for a in alerts]
    assert parcels == ["L5306000001", "L5306000003"], "parcel 粒度各自落行"
    tt = [a[2] for a in alerts]
    assert tt[0] != tt[1], f"trig_time 必须互异（uk_rule_reading 不再冲突）：{tt}"
    assert tt[0] == "2026-11-08 09:30:00.000"
    assert tt[1] == "2026-11-08 09:30:00.001"          # 地块序 1 → +1000μs=1ms


def test_offset_uses_full_scene_parcel_order_not_fall_subset():
    """偏移序=景内全部地块（排序后），非降幅子集——降幅集合变化（阈值重标定）时已落地块
    的 trig_time 稳定，幂等去重不被破坏。"""
    # 3 地块景：仅中间序位地块降幅 → 偏移应为序号 1（+1ms），而非降幅子集序号 0
    calls, _ = _run_fall_alerts(_vigors([("L5306000001", 0.70), ("L5306000003", 0.10),
                                         ("L5306000005", 0.68)]))
    alerts = _alert_inserts(calls)
    assert len(alerts) == 1
    assert alerts[0][2] == "2026-11-08 09:30:00.001", (
        "偏移必须按景内全地块序（0003 排序位 1），非降幅子集序")


def test_replay_same_scene_yields_identical_trig_times():
    """重放同景（同地块集同均值）→ trig_time 序列逐次相同 → INSERT IGNORE 幂等去重保持。"""
    vigors = _vigors([("L5306000001", 0.12), ("L5306000003", 0.10)])
    calls1, _ = _run_fall_alerts(vigors)
    calls2, _ = _run_fall_alerts(vigors)
    assert [a[2] for a in _alert_inserts(calls1)] == [a[2] for a in _alert_inserts(calls2)]
    assert [a[3] for a in _alert_inserts(calls1)] == [a[3] for a in _alert_inserts(calls2)]


def test_trig_time_step_is_1000us_for_column_precision():
    """步长=1000μs（DATETIME(3) 毫秒精度列可辨识的最小稳定步长，亚毫秒会被列取整折叠）。"""
    calls, _ = _run_fall_alerts(_vigors([("L5306000001", 0.12), ("L5306000003", 0.10),
                                         ("L5306000005", 0.11)]))
    tt = [a[2] for a in _alert_inserts(calls)]
    parsed = [_dt.strptime(t, "%Y-%m-%d %H:%M:%S.%f") for t in tt]
    assert (parsed[1] - parsed[0]).total_seconds() == 0.001
    assert (parsed[2] - parsed[1]).total_seconds() == 0.001


def test_single_fall_keeps_alert_and_notify_contract():
    """单地块降幅（存量形态）：仍 1 行 alert + 1 条 domain_event + 3 行 notify，
    SQL 形态（INSERT IGNORE + NOT EXISTS parcel 粒度）不回归。"""
    calls, _ = _run_fall_alerts(_vigors([("L5306000005", 0.10)]))
    inserts = [c[0] for c in calls]
    alerts = _alert_inserts(calls)
    assert len(alerts) == 1
    assert "NOT EXISTS(SELECT 1 FROM hsf_biz.alert_record r" in inserts[0], (
        "NOT EXISTS parcel 粒度幂等护栏不回归")
    assert any(c[1][0] == "rs.ndvi.fall" for c in calls), "domain_event 留痕不回归（topic 参数）"
    assert sum(1 for c in inserts if "hsf_biz.notify" in c) == 3, "3 角色通知行不回归"
    # notify 回查按偏移后 trig_time（R-13：原按景 acquired_at 查在多地块下命中错行）
    notify_call = next(c for c in calls if "hsf_biz.notify" in c[0])
    assert notify_call[1][-1] == alerts[0][2], "notify 必须以偏移后 trig_time 回查本地块行"


def test_r15_recalibrated_sample_two_parcel_fall_granularity():
    """R-13 验收锚（R-15 重标定样本复用，docs/24 §4 R-13 验收口径）：重标定样本第二景
    双地块降幅（0003/0005）→ ndvi_fall_alerts 落 2 行、parcel_code 各异、trig_time 互异——
    即"GET /alerts?rule_code=RULE_NDVIFALL 两条且 parcel_code 各异（方案 b）"的落库级等价验证。
    prev_mean 按真实标定值逐地块返回（scene1 目标），非降幅地块不误触发。"""
    from app import ndvi as ndvi_mod, sample as sample_mod

    def _rect_ring(lng0, lat0, w, h):
        return [[round(lng0, 6), round(lat0, 6)], [round(lng0 + w, 6), round(lat0, 6)],
                [round(lng0 + w, 6), round(lat0 + h, 6)], [round(lng0, 6), round(lat0 + h, 6)],
                [round(lng0, 6), round(lat0, 6)]]

    seed_all = ["L5306000001", "L5306000002", "L5306000003", "L5306000004",
                "L5306000005", "L5306000006", "L5306000007", "L5306000008"]
    parcels = []
    for code in ("L5306000001", "L5306000002", "L5306000003",
                 "L5306000005", "L5306000006", "L5306000007"):
        i = seed_all.index(code) + 1
        parcels.append({"code": code,
                        "rings": [_rect_ring(102.747 + ((i - 1) % 4) * 0.002,
                                             25.0485 + ((i - 1) // 4) * 0.002,
                                             0.0015, 0.0012)]})
    scene1_prev = sample_mod.SCENE_PLANS[0][1]                    # 第一景标定值=prev_mean 来源
    red, nir, _, _ = sample_mod._bands(sample_mod.SCENE_PLANS[1][1])   # 第二景（双地块降幅）
    scene = {"bands": {"red": red, "nir": nir}, "width": sample_mod.W,
             "height": sample_mod.H, "cell_size_m": sample_mod.CELL}
    vigors = ndvi_mod.process(scene, parcels)["vigors"]
    with mock.patch("hsf_common.db.query") as q, mock.patch("hsf_common.db.execute") as e:
        e.return_value = 1
        q.side_effect = lambda sql, args=None, one=False: (
            {"ndvi_mean": scene1_prev.get(args[0])} if args else None)   # prev_mean 逐地块真实值
        repo.ndvi_fall_alerts(vigors, "RS-SAMPLE-R15-20261108", "2026-11-08 09:30:00")
        calls = [c.args for c in e.call_args_list]
    alerts = _alert_inserts(calls)
    assert len(alerts) == 2, "重标定样本双地块降幅必须落 2 行预警（R-13 粒度修复 + R-15 样本联动）"
    assert {a[3] for a in alerts} == {"L5306000003", "L5306000005"}, "降幅地块恰为 0003/0005"
    assert alerts[0][2] != alerts[1][2], "两行 trig_time 互异（uk 不再冲突）"
