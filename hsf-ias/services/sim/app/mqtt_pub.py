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

from . import metrics

TOPIC_FMT = "hsf/sensor/{device_code}/{metric}"
PUBLISH_QOS = 1
KEEPALIVE = 30

# ── R-02 失败补偿常量（docs/24 §4 R-02 approach ②；健康链路零开销——pacing 恒 0 不睡眠）──
PUB_RETRIES = 2            # MQTT_ERR_QUEUE_SIZE 有界重投次数（env MQTT_PUB_RETRIES 可调，0=关闭）
PUB_QUEUE_MAX = 50000      # paho 发送队列上限（env MQTT_PUB_QUEUE_MAX；0=不限=旧行为。
                           # paho 缺省 0=无限——队列满计数指标要有真实背压信号须有界；
                           # 50k ≈ iot-write 单迭代 2.4 万条 ×2 余量，正常 tick 不触顶）
PACING_STEP = 0.002        # 每次队列满失败步长 +2ms（降速②）
PACING_MAX = 0.02          # 降速上限 20ms/条（饱和背压下让 paho IO 线程有机会排空队列）
PACING_RETRY_SLEEP = 0.005 # 重投间隔基数 5ms（随重试序号线性退避）
PACING_DECAY_EVERY = 500   # 连续 500 条成功 → 步长减半趋 0（健康后自动回到零开销）

_RC_NAMES = {4: "no_conn", 7: "conn_lost", 9: "payload_size", 12: "acl_denied", 15: "queue_size"}


def rc_name(rc) -> str:
    """paho rc → 指标分桶名（未知 rc 落 rc{n} 桶，paho 升级前向兼容；R-02 按 rc 分桶口径）。"""
    try:
        return _RC_NAMES.get(int(rc), f"rc{int(rc)}")
    except (TypeError, ValueError):
        return "rc_unknown"


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
    broker 离线队列只服务订阅侧补投——发布端不重放，量级由心跳下一轮自然补齐）。

    R-02（docs/24 §4）：发布侧可观测指标与失败补偿——
      ① 指标：队列长度/接受数/rc≠SUCCESS 按 rc 分桶计数/重投数/降速步长（app/metrics.py，
         /metrics 直读替代 docs/23 §4.2 推断口径）；
      ② 失败补偿=重投+降速：MQTT_ERR_QUEUE_SIZE（发送队列满=瞬态背压）有界重投（线性退避）
         并触发自适应降速步长（成功后按节流衰减回 0）；NO_CONN/CONN_LOST 等不可瞬态恢复的
         rc 仅计数不重投（断连降速无意义，量级由心跳下一轮自然补齐）；
      ③ 响应契约不变：publish_readings 仍同步返回被接受（rc==0）条数（docs/07 §2.5 冻结），
         异步化仅评估不实施；健康链路（pacing=0、无失败）行为与补偿加入前逐字节等价。"""

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
        # R-02 失败补偿状态（重试/降速 env 可调；缺省=有界重投 2 次 + 降速上限 20ms）
        self._retries = max(0, int(os.environ.get("MQTT_PUB_RETRIES", str(PUB_RETRIES))))
        self._queue_max = max(0, int(os.environ.get("MQTT_PUB_QUEUE_MAX", str(PUB_QUEUE_MAX))))
        self._pacing = 0.0
        self._ok_streak = 0
        self._pace_lock = threading.Lock()

    # -- paho 回调（CallbackAPIVersion.VERSION2 签名；reason_code 为 ReasonCode）--
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self._connected.set()
        self._update_queue_gauge()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        self._connected.clear()
        self._update_queue_gauge()

    def start(self) -> None:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hsf-sim-pub",
                             protocol=mqtt.MQTTv311, clean_session=True)
        client.username_pw_set(self._user, self._password)
        client.reconnect_delay_set(1, 30)
        # W-1（docs/08 §5，CONVENTIONS-P3）：inflight 100→1000——二期 iot-write 实测 2.4 万条/迭代
        # p95=8.29s 的主因即 inflight 窗口 × PUBACK RTT 的同步发布阻塞；broker 侧 max_inflight_messages
        # 已同步调 1000（k8s/30-mqtt.yaml），QoS1 语义与幂等键不变
        client.max_inflight_messages_set(1000)
        if self._queue_max > 0:
            # R-02：发送队列有界化（paho 缺省 0=无限）——MQTT_ERR_QUEUE_SIZE 才会成为真实背压信号，
            # 队列满计数/重投/降速构成闭环；上限远大于单 tick 批量（2.4 万条），常态不触顶
            client.max_queued_messages_set(self._queue_max)
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

    # -- R-02 失败补偿（重投/降速）与指标 ------------------------------------------------

    def _pace(self, ok: bool) -> None:
        """自适应降速步长：队列满失败 → 阶跃上调（≤PACING_MAX）；连续 PACING_DECAY_EVERY
        条成功 → 减半趋 0（健康链路自动回到零开销）。步长为进程内观测状态，心跳线程与
        tick 线程并发更新仅影响调节速度，无正确性影响（锁内更新保证读改写原子）。"""
        with self._pace_lock:
            if ok:
                self._ok_streak += 1
                if self._pacing > 0 and self._ok_streak >= PACING_DECAY_EVERY:
                    new = self._pacing / 2
                    self._pacing = 0.0 if new < 0.001 else new   # <1ms 视为已恢复，直接回零开销
                    self._ok_streak = 0
                    metrics.PUBLISH_PACING.set(self._pacing)
            else:
                self._ok_streak = 0
                if self._pacing < PACING_MAX:
                    self._pacing = min(PACING_MAX, self._pacing + PACING_STEP)
                    metrics.PUBLISH_PACING.set(self._pacing)

    def _publish_one(self, topic: str, payload: str) -> bool:
        """单条发布（含失败补偿）：rc=0 记 True；QUEUE_SIZE 有界重投（线性退避），
        其余 rc 仅按桶计数；返回是否被 broker 接受。"""
        info = None
        for attempt in range(self._retries + 1):
            info = self._client.publish(topic, payload, qos=PUBLISH_QOS, retain=False)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                self._pace(True)
                return True
            metrics.PUBLISH_FAIL.labels(rc_name(info.rc)).inc()
            if info.rc == mqtt.MQTT_ERR_QUEUE_SIZE and attempt < self._retries:
                metrics.PUBLISH_RETRY.inc()          # 补偿①重投：瞬态背压，短退避后再试
                time.sleep(PACING_RETRY_SLEEP * (attempt + 1))
                continue
            break                                    # 非 QUEUE_SIZE 或重投耗尽：仅计数不重投
        if info is not None and info.rc == mqtt.MQTT_ERR_QUEUE_SIZE:
            self._pace(False)                        # 补偿②降速：仅背压信号触发自适应降速
        return False

    def _update_queue_gauge(self) -> None:
        """paho 发送队列长度（未确认消息=排队+在途；paho 无公开 API，防御性读 _out_messages）。"""
        try:
            metrics.QUEUE_SIZE.set(len(self._client._out_messages or ()))
        except Exception:
            pass  # 观测面异常不致发布路径失败

    def publish_readings(self, readings: list[tuple]) -> int:
        """同步发布一批读数，返回被 broker 接受（rc==0）的条数；未连接返回 0。
        降速步长在批内生效（失败上调后下一拍起效，成功衰减后逐拍回落）。"""
        if self._client is None or not self._connected.is_set():
            self._update_queue_gauge()
            return 0
        published = 0
        pacing = self._pacing
        for device_code, metric, value, recorded_at, village_code in readings:
            if pacing > 0:
                time.sleep(pacing)                   # 补偿②降速：仅在发生过队列满后非零
            accepted = self._publish_one(build_topic(device_code, metric),
                                         build_payload(device_code, metric, value,
                                                       recorded_at, village_code))
            published += 1 if accepted else 0
            pacing = self._pacing
        self._published_total += published
        metrics.PUBLISH_TOTAL.inc(published)
        self._update_queue_gauge()
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
