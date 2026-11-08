"""iot TDD：MQTT 消息四重校验（docs/07 §2.2 冻结）——topic↔payload 一致性、metric 冻结
命名、值域复核、台账准入；recorded_at 须为带时区 ISO8601。纯函数 + dict 桩，无外部依赖。
"""
import datetime as dt
import json

from app.mqtt_sub import parse_message

V_TZ = dt.timezone(dt.timedelta(hours=8))


class _StubRegistry:
    def __init__(self, devices):
        self._d = devices

    def get(self, code):
        return self._d.get(code)

    def maybe_refresh(self):
        pass


REG = _StubRegistry({"S5306000001": {"metric": "soil_moisture", "village_code": "53060010101",
                                     "parcel_code": "L5306000001"}})


def _payload(device="S5306000001", metric="soil_moisture", value=23.456,
             ts="2026-11-08T10:00:00.123+08:00") -> bytes:
    return json.dumps({"device_code": device, "metric": metric, "value": value,
                       "recorded_at": ts, "village_code": "53060010101"}).encode()


def test_valid_message_roundtrip():
    row, reason = parse_message("hsf/sensor/S5306000001/soil_moisture", _payload(), REG)
    assert reason is None
    assert row[0] == "S5306000001" and row[1] == "soil_moisture"
    assert row[2] == 23.456
    assert row[3].tzinfo is not None and row[3].utcoffset() == dt.timedelta(hours=8)


def test_topic_shape_rejected():
    for bad in ("hsf/sensor/S1", "hsf/sensor/S1/soil_moisture/extra", "cmd/S1/soil_moisture",
                "hsf/sensor//soil_moisture", "hsf/sensor/S1/"):
        assert parse_message(bad, _payload(), REG) == (None, "topic")


def test_topic_payload_mismatch_rejected():
    """topic 解析的 device/metric 与 payload 不一致 → 丢弃（防发布端 bug 污染时序）。"""
    assert parse_message("hsf/sensor/S5306000001/soil_moisture", _payload(device="S5306000002"),
                         REG) == (None, "mismatch")
    assert parse_message("hsf/sensor/S5306000001/wind_speed", _payload(), REG) == (None, "mismatch")


def test_unknown_metric_and_device_rejected():
    assert parse_message("hsf/sensor/S5306000001/leaf_wetness", _payload(metric="leaf_wetness"),
                         REG) == (None, "metric")
    assert parse_message("hsf/sensor/S5306009999/soil_moisture", _payload(device="S5306009999"),
                         REG) == (None, "device")


def test_value_guard():
    """非数值/布尔（bool 是 int 子类需显式排除）/越界 → 丢弃（04 §4.7 值域复核）。"""
    assert parse_message("hsf/sensor/S5306000001/soil_moisture", _payload(value="23.5"), REG) \
        == (None, "value")
    assert parse_message("hsf/sensor/S5306000001/soil_moisture", _payload(value=True), REG) \
        == (None, "value")
    assert parse_message("hsf/sensor/S5306000001/soil_moisture", _payload(value=96.0), REG) \
        == (None, "range")
    assert parse_message("hsf/sensor/S5306000001/soil_moisture", _payload(value=4.9), REG) \
        == (None, "range")
    row, reason = parse_message("hsf/sensor/S5306000001/soil_moisture", _payload(value=5.0), REG)
    assert reason is None and row[2] == 5.0                      # 边界值在量程内（闭区间）


def test_recorded_at_requires_aware_iso8601():
    assert parse_message("hsf/sensor/S5306000001/soil_moisture", _payload(ts="not-a-time"),
                         REG) == (None, "recorded_at")
    assert parse_message("hsf/sensor/S5306000001/soil_moisture",
                         _payload(ts="2026-11-08T10:00:00.123"), REG) == (None, "recorded_at")


def test_broken_json_rejected():
    assert parse_message("hsf/sensor/S5306000001/soil_moisture", b"{not json", REG) \
        == (None, "json")
    assert parse_message("hsf/sensor/S5306000001/soil_moisture", b"[1,2,3]", REG) \
        == (None, "json")
