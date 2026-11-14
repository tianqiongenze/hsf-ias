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


# ── R-02（docs/24 §4）：发布侧失败补偿（重投/降速）与 /metrics 指标 ──────────────────
import paho.mqtt.client as _mqtt  # noqa: E402
from prometheus_client import REGISTRY  # noqa: E402


def _gauge(name: str) -> float:
    return REGISTRY.get_sample_value(name) or 0.0


def _counter(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class _ScriptedClient:
    """按脚本序逐次返回 rc 的 paho 桩（验证重投路径）；未连接态模拟 _out_messages。"""

    def __init__(self, rcs):
        self.rcs = list(rcs)
        self.calls = 0
        self._out_messages = {}

    def publish(self, topic, payload, qos=0, retain=False):
        rc = self.rcs[min(self.calls, len(self.rcs) - 1)]
        self.calls += 1
        if rc == _mqtt.MQTT_ERR_SUCCESS:
            self._out_messages[self.calls] = object()   # 模拟在途消息
        return type("Info", (), {"rc": rc})()


def test_r02_queue_size_retry_then_accept():
    """队列满（rc=15）→ 有界重投 → 第二拍接受：published 计入、RETRY/FAIL{queue_size} 增量。"""
    before_retry = _counter("hsf_sim_publish_retry_total")
    before_fail = _counter("hsf_sim_publish_fail_total", rc="queue_size")
    c = _ScriptedClient([_mqtt.MQTT_ERR_QUEUE_SIZE, _mqtt.MQTT_ERR_SUCCESS])
    p = _StubPublisher(c)
    n = p.publish_readings([("S1", "soil_moisture", 30.0, T0, "53060010101")])
    assert n == 1 and p.published_total() == 1              # 重投后被接受
    assert _counter("hsf_sim_publish_retry_total") - before_retry == 1
    assert _counter("hsf_sim_publish_fail_total", rc="queue_size") - before_fail == 1


def test_r02_persistent_queue_full_raises_pacing():
    """持续队列满：重投耗尽 → published=0、fail 计满、降速步长上调（≤PACING_MAX）并进 gauge。"""
    c = _ScriptedClient([_mqtt.MQTT_ERR_QUEUE_SIZE])
    p = _StubPublisher(c)
    n = p.publish_readings([("S1", "soil_moisture", 30.0, T0, "53060010101")])
    assert n == 0                                           # 重试 2 次仍失败 → 不计入
    assert c.calls == 1 + mqtt_pub.PUB_RETRIES              # 1 首发 + PUB_RETRIES 重投
    assert 0 < p._pacing <= mqtt_pub.PACING_MAX
    assert _gauge("hsf_sim_publish_pacing_seconds") == p._pacing


def test_r02_no_conn_no_retry_but_counted_by_rc():
    """NO_CONN（断连）仅按 rc 桶计数、不重投（断连降速/重投无意义——量级由心跳下一轮补齐）。"""
    before_fail = _counter("hsf_sim_publish_fail_total", rc="no_conn")
    before_retry = _counter("hsf_sim_publish_retry_total")
    c = _ScriptedClient([_mqtt.MQTT_ERR_NO_CONN])
    p = _StubPublisher(c)
    assert p.publish_readings([("S1", "soil_moisture", 30.0, T0, "53060010101")]) == 0
    assert c.calls == 1                                     # 未重投
    assert _counter("hsf_sim_publish_fail_total", rc="no_conn") - before_fail == 1
    assert _counter("hsf_sim_publish_retry_total") == before_retry
    assert p._pacing == 0.0                                 # 背压降速不因断连触发


def test_r02_pacing_decay_on_success():
    """成功节流衰减：降速步长在连续 PACING_DECAY_EVERY 条成功后减半趋 0（健康回零开销）。"""
    p = _StubPublisher(_ScriptedClient([_mqtt.MQTT_ERR_SUCCESS]))
    p._pacing = mqtt_pub.PACING_MAX
    for _ in range(mqtt_pub.PACING_DECAY_EVERY):
        p.publish_readings([("S1", "soil_moisture", 30.0, T0, "53060010101")])
    assert p._pacing < mqtt_pub.PACING_MAX                  # 减半生效
    for _ in range(mqtt_pub.PACING_DECAY_EVERY * 4):
        p.publish_readings([("S1", "soil_moisture", 30.0, T0, "53060010101")])
    assert p._pacing == 0.0                                 # 逐级衰减至零开销


def test_r02_queue_gauge_and_publish_total():
    """指标直读对账面：接受数进 hsf_sim_publish_total；队列长度=未确认消息数（观测面）。"""
    before_total = _counter("hsf_sim_publish_total")
    c = _ScriptedClient([_mqtt.MQTT_ERR_SUCCESS])
    p = _StubPublisher(c)
    p.publish_readings([("S1", "soil_moisture", 30.0, T0, "53060010101")])
    assert _counter("hsf_sim_publish_total") - before_total == 1
    assert _gauge("hsf_sim_queue_size") == 1.0              # _out_messages 1 条在途


def test_r02_rc_name_buckets():
    """rc 分桶命名：已知 rc 具名、未知 rc 落 rc{n} 桶（paho 升级前向兼容）。"""
    assert mqtt_pub.rc_name(_mqtt.MQTT_ERR_QUEUE_SIZE) == "queue_size"
    assert mqtt_pub.rc_name(_mqtt.MQTT_ERR_NO_CONN) == "no_conn"
    assert mqtt_pub.rc_name(99) == "rc99"
