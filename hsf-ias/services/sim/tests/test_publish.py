"""sim TDD：MQTT 发布契约——topic/payload 冻结形态（docs/07 §2.2）与发布器行为。

MqttPublisher 以 paho client 桩验证（不连 broker）：未连接返回 0、连接后逐条 publish
（QoS1/retain=False）、发布计数；payload 中的 recorded_at 为 ISO8601+08:00 毫秒。
"""
import datetime as dt
import json

from app import mqtt_pub

V_TZ = dt.timezone(dt.timedelta(hours=8))
T0 = dt.datetime(2026, 11, 8, 10, 0, 0, 123000, tzinfo=V_TZ)


def test_topic_format():
    assert mqtt_pub.build_topic("S5306000001", "soil_moisture") == \
        "hsf/sensor/S5306000001/soil_moisture"


def test_payload_frozen_shape():
    """payload 冻结字段：device_code/metric/value/recorded_at/village_code；毫秒+时区偏移。"""
    payload = json.loads(mqtt_pub.build_payload("S5306000001", "soil_moisture", 23.456, T0,
                                                "53060010101"))
    assert payload == {"device_code": "S5306000001", "metric": "soil_moisture",
                       "value": 23.456, "village_code": "53060010101",
                       "recorded_at": "2026-11-08T10:00:00.123+08:00"}


def test_payload_value_rounded_to_3():
    raw = json.loads(mqtt_pub.build_payload("S1", "wind_speed", 8.12345, T0, "53060010101"))
    assert raw["value"] == 8.123


def test_time_step_millisecond_increment():
    """批量 tick 的 recorded_at 步进 1ms（幂等键可区分——docs/07 §2.2 步进约束）。"""
    assert mqtt_pub.time_step(T0, 0) == T0
    assert mqtt_pub.time_step(T0, 3) == T0 + dt.timedelta(milliseconds=3)


class _StubClient:
    def __init__(self, rc=0):
        self.rc = rc
        self.calls = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.calls.append((topic, payload, qos, retain))
        return type("Info", (), {"rc": self.rc})()


class _StubPublisher(mqtt_pub.MqttPublisher):
    """替换 _client 并置连接态，绕开真实 socket（行为单测桩）。"""

    def __init__(self, stub):
        super().__init__(provider=lambda: [], host="h", port=1883, user="u", password="p")
        self._client = stub
        self._connected.set()


def test_publish_counts_only_accepted():
    ok = _StubPublisher(_StubClient(rc=0))
    readings = [(f"S530600000{i}", "soil_moisture", 30.0, T0, "53060010101") for i in (1, 2, 3)]
    assert ok.publish_readings(readings) == 3
    assert ok.published_total() == 3
    topic, payload, qos, retain = ok._client.calls[0]
    assert qos == 1 and retain is False                     # QoS1 + retain=false（§2.3 冻结）
    assert topic.startswith("hsf/sensor/S5306000001/")      # iot 订阅通配 hsf/sensor/+/+


def test_publish_not_connected_returns_zero():
    p = mqtt_pub.MqttPublisher(provider=lambda: [], host="h", port=1883, user="u", password="p")
    assert p.publish_readings([("S1", "soil_moisture", 30.0, T0, "53060010101")]) == 0


def test_publish_rc_failure_not_counted():
    bad = _StubPublisher(_StubClient(rc=1))                 # MQTT_ERR_NO_CONN 语义
    assert bad.publish_readings([("S1", "soil_moisture", 30.0, T0, "53060010101")]) == 0
