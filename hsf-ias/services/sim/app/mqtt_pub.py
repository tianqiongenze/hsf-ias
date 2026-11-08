"""sim MQTT 发布器（二期新增）：paho-mqtt 连 hsf-mqtt:1883，把模拟读数发布到
hsf/sensor/{deviceCode}/{metric}（QoS1，retain=false——docs/07 §2.1/§2.3 冻结）。

两个入口共用同一发布通道（07 号 §2.5）：
  ① 心跳线程：连接后按 MQTT_INTERVAL（缺省 2s）间隔发布全部 ACTIVE 设备读数（48 条/轮）；
  ② POST /sim/tick：批量触发同步发布（k6/E2E 数据面），响应含 published 条数。
payload 冻结形态（07 号 §2.2）：{"device_code","metric","value","recorded_at"(ISO8601+08:00
毫秒，sim 生成时刻，iot 不改写),"village_code"}；recorded_at 步进 1ms 保证幂等键可区分。
"""
import datetime as dt
import json
import os
import threading
import time

import paho.mqtt.client as mqtt

TOPIC_FMT = "hsf/sensor/{device_code}/{metric}"
PUBLISH_QOS = 1
KEEPALIVE = 30


def build_topic(device_code: str, metric: str) -> str:
    return TOPIC_FMT.format(device_code=device_code, metric=metric)


def build_payload(device_code: str, metric: str, value: float, recorded_at: dt.datetime,
                  village_code: str) -> str:
    """冻结 payload（docs/07 §2.2）；recorded_at = sim 生成时刻（ISO8601+08:00 毫秒）。"""
    return json.dumps({"device_code": device_code, "metric": metric,
                       "value": round(float(value), 3),
                       "recorded_at": recorded_at.isoformat(timespec="milliseconds"),
                       "village_code": village_code}, ensure_ascii=False)


def now_local() -> dt.datetime:
    """生成时刻（带本地时区；容器 TZ=Asia/Shanghai，A-04-3 全链路口径）。"""
    return dt.datetime.now().astimezone()


class MqttPublisher:
    """发布线程壳：connect 后 loop_forever（自动重连）；心跳线程按间隔取读数并发布。

    provider() 由调用方注入（main.py 接 repo.heartbeat_readings），返回读数元组列表
    (device_code, metric, value, recorded_at, village_code)；未连接时不消费（读数即弃，
    broker 离线队列只服务订阅侧补投——发布端不重放，量级由心跳下一轮自然补齐）。"""

    def __init__(self, provider, host: str, port: int, user: str, password: str,
                 interval: float | None = None):
        self._provider = provider
        self._host, self._port = host, port
        self._user, self._password = user, password
        self._interval = float(interval if interval is not None
                               else os.environ.get("MQTT_INTERVAL", "2"))
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._client: mqtt.Client | None = None
        self._hb_thread: threading.Thread | None = None
        self._published_total = 0

    # -- paho 回调（CallbackAPIVersion.VERSION2 签名；reason_code 为 ReasonCode）--
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self._connected.set()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        self._connected.clear()

    def start(self) -> None:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hsf-sim-pub",
                             protocol=mqtt.MQTTv311, clean_session=True)
        client.username_pw_set(self._user, self._password)
        client.reconnect_delay_set(1, 30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.connect_async(self._host, self._port, keepalive=KEEPALIVE)
        self._client = client
        threading.Thread(target=client.loop_forever, name="sim-mqtt-io", daemon=True).start()
        self._hb_thread = threading.Thread(target=self._heartbeat, name="sim-mqtt-hb",
                                           daemon=True)
        self._hb_thread.start()

    def _heartbeat(self) -> None:
        """心跳循环：连接后按 MQTT_INTERVAL 间隔发布一轮全部 ACTIVE 设备读数。"""
        while not self._stop.is_set():
            self._stop.wait(self._interval)
            if self._stop.is_set() or not self._connected.is_set():
                continue
            try:
                self.publish_readings(self._provider())
            except Exception as e:
                print(f"[sim] 心跳发布失败（下轮重试）：{e}", flush=True)

    def publish_readings(self, readings: list[tuple]) -> int:
        """同步发布一批读数，返回被 broker 接受（rc==0）的条数；未连接返回 0。"""
        if self._client is None or not self._connected.is_set():
            return 0
        published = 0
        for device_code, metric, value, recorded_at, village_code in readings:
            info = self._client.publish(build_topic(device_code, metric),
                                        build_payload(device_code, metric, value,
                                                      recorded_at, village_code),
                                        qos=PUBLISH_QOS, retain=False)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                published += 1
        self._published_total += published
        return published

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def published_total(self) -> int:
        return self._published_total

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass


def time_step(base: dt.datetime, i: int) -> dt.datetime:
    """批量 tick 的 recorded_at 步进 1ms（一期 repo.py 同法，幂等键可区分）。"""
    return base + dt.timedelta(milliseconds=i)
