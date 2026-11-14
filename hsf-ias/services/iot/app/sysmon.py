"""iot broker $SYS 监视器（R-03，docs/24 §4 R-03 approach ①）：轻量 MQTT 订阅采集，
**无新增部署组件**（采集逻辑内嵌 iot 进程，避免重型 sidecar——新增组件需走 ADR，联动 R-17）。

订阅 `$SYS/broker/#`（QoS0，clean_session=True——$SYS 为快照态数据，断线补投无意义），
将 mosquitto 2.x 冻结 $SYS buckets 直读进 iot /metrics：
  publish/messages/dropped（别名 mqtt/publish/dropped）→ hsf_broker_messages_dropped_total（Δ）
  store/messages/count（别名 messages/stored）          → hsf_broker_messages_stored
  clients/connected                                    → hsf_broker_clients_connected
  heap/current                                         → hsf_broker_heap_bytes
前置：k8s/30-mqtt.yaml mosquitto.conf 开 `sys_interval 10`、acl 增 `topic read $SYS/broker/#`
（2026-11-14 实测：无该行时 hsf 账号订阅 $SYS 收不到任何消息——采集链路缺口即此）。

连接生命周期独立于业务订阅线程（MqttSubscriber）：采集失败只影响观测面，不进 readyz、
不影响收数链路；loop_forever 自动重连（1s→30s 退避）。
"""
import threading

import paho.mqtt.client as mqtt

from . import metrics

SYS_FILTER = "$SYS/broker/#"
SYS_PREFIX = "$SYS/broker/"
CLIENT_ID = "hsf-iot-sysmon"
KEEPALIVE = 60

# 叶名 → 指标映射（兼容 mosquitto 2.x 新旧两套别名；未列出的 bucket 不落指标防 series 膨胀）
DROPPED_LEAVES = frozenset({"publish/messages/dropped", "mqtt/publish/dropped"})
STORED_LEAVES = frozenset({"store/messages/count", "messages/stored"})
CLIENTS_LEAF = "clients/connected"
HEAP_LEAF = "heap/current"


class BrokerSysMonitor:
    """$SYS 采集线程壳：连接即订阅，on_message 只做解析+指标更新（不碰 DB）。"""

    def __init__(self, host: str, port: int, user: str, password: str):
        self._host, self._port = host, port
        self._user, self._password = user, password
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._client: mqtt.Client | None = None
        self._thread: threading.Thread | None = None
        self._dropped_last: int | None = None      # Δ 基线：None=未采样
        self._drop_resets = 0                      # broker 重启清零识别次数（观测）

    # -- paho 回调（CallbackAPIVersion.VERSION2 签名）--
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            client.subscribe(SYS_FILTER, qos=0)
            self._connected.set()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        self._connected.clear()

    def _on_message(self, client, userdata, msg):
        metrics.BROKER_SYS_SEEN.inc()
        leaf = msg.topic[len(SYS_PREFIX):] if msg.topic.startswith(SYS_PREFIX) else msg.topic
        try:
            value = int(float(msg.payload.decode("ascii", "ignore")))
        except (ValueError, UnicodeDecodeError):
            return                                  # 非数值 bucket（版本串等）不落指标
        if leaf in DROPPED_LEAVES:
            self._track_dropped(value)
        elif leaf in STORED_LEAVES:
            metrics.BROKER_STORED.set(value)
        elif leaf == CLIENTS_LEAF:
            metrics.BROKER_CLIENTS.set(value)
        elif leaf == HEAP_LEAF:
            metrics.BROKER_HEAP.set(value)

    def _track_dropped(self, value: int) -> None:
        """丢弃计数 Δ 增量：首采记基线；broker 重启（值回退）重置基线不产生负增量。"""
        last = self._dropped_last
        if last is None:
            self._dropped_last = value              # 基线=iot 启动时 broker 已有累计
            return
        if value < last:                            # broker 重启清零：重置基线（历史量已断档）
            self._drop_resets += 1
            self._dropped_last = value
            return
        if value > last:
            metrics.BROKER_DROPPED.inc(value - last)
            self._dropped_last = value

    def start(self) -> None:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID,
                             protocol=mqtt.MQTTv311, clean_session=True)
        client.username_pw_set(self._user, self._password)
        client.reconnect_delay_set(1, 30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect_async(self._host, self._port, keepalive=KEEPALIVE)
        self._client = client
        self._thread = threading.Thread(target=client.loop_forever,
                                        name="iot-broker-sysmon", daemon=True)
        self._thread.start()

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def drop_resets(self) -> int:
        return self._drop_resets

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
