"""iot MQTT 订阅线程：paho-mqtt 连 hsf-mqtt:1883，订阅 hsf/sensor/+/+（QoS1，
clean_session=False，client_id=hsf-iot-0，重连 1s→30s 退避——docs/07 §2.3 冻结）。

读数四重校验（§2.2：topic↔payload 一致性 + metric 冻结命名 + 04 §4.7 值域 + 台账准入）
通过后进进程内缓冲；刷写由 app.repo.flusher_loop 按 500 条或 2s 先到者刷执行。
"""
import datetime as dt
import json
import threading

import paho.mqtt.client as mqtt

from . import metrics, rules

TOPIC_FILTER = "hsf/sensor/+/+"   # {deviceCode}/{metric} 二级通配（CONVENTIONS-P2 T-2）
TOPIC_PREFIX = ("hsf", "sensor")  # hsf/sensor/{device}/{metric}
CLIENT_ID = "hsf-iot-0"
SUBSCRIBE_QOS = 1
KEEPALIVE = 30


class ReadingsBuffer:
    """线程安全读数缓冲：MQTT 回调线程 append，刷写线程 swap/put_front。"""

    def __init__(self):
        self._items: list[tuple] = []
        self._lock = threading.Lock()

    def append(self, row: tuple) -> int:
        with self._lock:
            self._items.append(row)
            return len(self._items)

    def swap(self) -> list[tuple]:
        """原子取走全部缓冲（刷写线程独占调用）。"""
        with self._lock:
            items, self._items = self._items, []
        return items

    def put_front(self, rows: list[tuple]) -> None:
        """刷写失败整批回队（保序放回队首，幂等键保证重试不重复落库）。"""
        with self._lock:
            self._items = list(rows) + self._items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def parse_message(topic: str, payload: bytes, registry) -> tuple[tuple | None, str | None]:
    """消息解析与四重校验 → ((device_code, metric, value, recorded_at), None) 或 (None, 原因)。

    校验顺序（docs/07 §2.2）：topic 形态 → payload JSON → topic↔payload 一致性 →
    metric 冻结命名 → 值域复核 → 台账准入 → recorded_at 可解析。"""
    parts = topic.split("/") if isinstance(topic, str) else []
    if len(parts) != 4 or parts[:2] != list(TOPIC_PREFIX) or not parts[2] or not parts[3]:
        return None, "topic"
    topic_device, topic_metric = parts[2], parts[3]
    try:
        data = json.loads(payload)
    except Exception:
        return None, "json"
    if not isinstance(data, dict):
        return None, "json"
    device_code, metric = data.get("device_code"), data.get("metric")
    value = data.get("value")
    if device_code != topic_device or metric != topic_metric:
        return None, "mismatch"            # 防发布端 bug 污染时序（§2.2 一致性校验）
    rng = rules.METRIC_RANGES.get(metric)
    if rng is None:
        return None, "metric"              # 04 §4.7 冻结 6 类之外
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "value"
    value = float(value)
    if not (rng[0] <= value <= rng[1]):
        return None, "range"               # 值域复核：极端脏数据不进聚合
    if registry is None or registry.get(device_code) is None:
        return None, "device"              # 台账准入：未知设备丢弃（不做动态注册）
    try:
        ts = dt.datetime.fromisoformat(str(data.get("recorded_at")))
        if ts.tzinfo is None:
            return None, "recorded_at"     # sim 恒发 ISO8601+08:00；naive 视为脏数据
    except Exception:
        return None, "recorded_at"
    return (device_code, metric, value, ts), None


class MqttSubscriber:
    """订阅线程壳：connect/subscribe 在线程内执行，on_message 只做校验+入缓冲（不碰 DB）。

    断线由 loop_forever 自动重连（reconnect_delay_set(1, 30)）；clean_session=False +
    固定 client_id → iot 重启窗口内 broker 保留会话补投（docs/07 §2.3）。"""

    def __init__(self, buffer: ReadingsBuffer, registry, host: str, port: int,
                 user: str, password: str):
        self._buffer = buffer
        self._registry = registry
        self._host, self._port = host, port
        self._user, self._password = user, password
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._client: mqtt.Client | None = None
        self._thread: threading.Thread | None = None

    # -- paho 回调（CallbackAPIVersion.VERSION2 签名；reason_code 为 ReasonCode，==0 判成功）--
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            client.subscribe(TOPIC_FILTER, qos=SUBSCRIBE_QOS)
            self._connected.set()
        else:
            self._connected.clear()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        self._connected.clear()

    def _on_message(self, client, userdata, msg):
        row, reason = parse_message(msg.topic, msg.payload, self._registry)
        if row is None:
            metrics.MQTT_INVALID.labels(reason).inc()
            if reason == "device":
                self._registry.maybe_refresh()  # 新设备注册后 ≤10s 接入（台账节流刷新）
            return
        metrics.MQTT_RECEIVED.inc()
        self._buffer.append(row)

    def start(self) -> None:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID,
                             protocol=mqtt.MQTTv311, clean_session=False)
        client.username_pw_set(self._user, self._password)
        client.reconnect_delay_set(1, 30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect_async(self._host, self._port, keepalive=KEEPALIVE)
        self._client = client
        self._thread = threading.Thread(target=client.loop_forever,
                                        name="iot-mqtt-sub", daemon=True)
        self._thread.start()

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
