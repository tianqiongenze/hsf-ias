"""sim 发布侧业务指标（R-02，docs/24 §4 R-02 approach ①③）。

docs/07 §4.1 冻结指标清单为 iot 侧（hsf_iot_*）；本模块为 sim 侧新增指标（只新增不改既有命名），
命名族对齐验收 grep 口径 `hsf_sim_(queue|publish|inflight)`：
  hsf_sim_queue_size             paho 发送队列长度（未确认消息数=排队+在途；QoS1 PUBACK 前均在册）
  hsf_sim_publish_total          被 broker 接受（rc=0）发布总数
  hsf_sim_publish_fail_total{rc} rc≠SUCCESS 失败计数（按 rc 分桶：queue_size/no_conn/conn_lost/…）
  hsf_sim_publish_retry_total    MQTT_ERR_QUEUE_SIZE 有界重投次数（失败补偿 ①重投）
  hsf_sim_publish_pacing_seconds 自适应降速步长当前值（失败补偿 ②降速；0=不降速）

用途：iot-write 复测时与 iot 实收、broker 丢弃（R-03 hsf_broker_messages_dropped_total）
三方对账闭合——published 名义值 = hsf_sim_publish_total 增量 + hsf_sim_publish_fail_total 增量
± 队列存量（hsf_sim_queue_size 差值），替代 docs/23 §4.2 的推断口径。
HTTP 指标（hsf_http_*）由 hsf_common.web 提供。
"""
from prometheus_client import Counter, Gauge

QUEUE_SIZE = Gauge("hsf_sim_queue_size", "paho 发送队列长度（未确认消息数：排队+在途）")
PUBLISH_TOTAL = Counter("hsf_sim_publish_total", "MQTT 发布被 broker 接受（rc=0）总数")
PUBLISH_FAIL = Counter("hsf_sim_publish_fail_total", "MQTT 发布 rc≠SUCCESS 失败数（按 rc 分桶）",
                       ["rc"])
PUBLISH_RETRY = Counter("hsf_sim_publish_retry_total", "MQTT_ERR_QUEUE_SIZE 有界重投次数")
PUBLISH_PACING = Gauge("hsf_sim_publish_pacing_seconds", "失败补偿自适应降速步长（s）")
# v1.1 异步任务面（ADR-HSF-028）：队列深度 + 任务完成计数（state=done/failed）
JOB_QUEUE_DEPTH = Gauge("hsf_sim_job_queue_depth", "异步批量发布任务队列深度")
JOB_DONE = Counter("hsf_sim_job_done_total", "异步批量发布任务完成数（按终态分桶）", ["state"])
