"""iot 业务指标（docs/07 §4.1 冻结命名 + R-01/R-03 新增）；HTTP 指标由 hsf_common.web 提供。

docs/07 §4.1 冻结清单（hsf_iot_mqtt_received_total … hsf_iot_advice_created_total）全部保留、
语义不变；下方 R-01/R-03 指标为只新增（D-07-8"存量零变更只新增"口径）。
"""
from prometheus_client import Counter, Gauge, Histogram

MQTT_RECEIVED = Counter("hsf_iot_mqtt_received_total", "MQTT 收到并接受的有效读数总数")
MQTT_INVALID = Counter("hsf_iot_mqtt_invalid_total", "MQTT 无效消息丢弃数（按原因）",
                       ["reason"])
READINGS_WRITTEN = Counter("hsf_iot_readings_written_total", "TimescaleDB 时序实际写入行数")
ONCONFLICT_SKIPPED = Counter("hsf_iot_onconflict_skipped_total",
                             "唯一键冲突跳过行数（QoS1 重复/补投幂等，D-07-3）")
FLUSH_SECONDS = Histogram("hsf_iot_flush_seconds", "批量刷写耗时（s）")
RULE_TRIPPED = Counter("hsf_iot_rule_tripped_total", "回差状态机新触发告警数", ["rule_code"])
ADVICE_CREATED = Counter("hsf_iot_advice_created_total", "灌溉建议生成数")
DEVICE_REGISTERED = Counter("hsf_iot_device_registered_total", "设备接入注册数（三期 D-08-4）")

# ── R-01（docs/24 §4 R-01 approach ③）：有界缓冲背压与评估旁路可观测 ────────────────
BUFFER_DROPPED = Counter("hsf_iot_buffer_dropped_total",
                         "读数缓冲溢出丢弃条数（IOT_BUFFER_MAX 背压，防 OOM）")
BUFFER_SIZE = Gauge("hsf_iot_buffer_size", "读数缓冲当前深度（未刷写条数）")
EVAL_DROPPED = Counter("hsf_iot_eval_queue_dropped_total",
                       "评估队列溢出丢弃读数条数（已落库、仅规则/决策评估丢失）")
EVAL_FAILED = Counter("hsf_iot_eval_failed_total", "评估批次异常计数（整批回队重试）")
EVAL_SECONDS = Histogram("hsf_iot_eval_seconds", "批次规则/决策评估耗时（s）")
EVAL_PENDING = Gauge("hsf_iot_eval_queue_depth", "评估队列待处理批次深度")
FLUSH_WORKERS_G = Gauge("hsf_iot_flush_workers", "刷写 worker 线程数（FLUSH_WORKERS）")

# ── R-03（docs/24 §4 R-03）：broker 丢弃监控——iot 内置轻量 $SYS 采集（无新增组件，免 ADR）──
# 采集源=mosquitto $SYS buckets（mosquitto 2.x 文档冻结名）：
#   $SYS/broker/publish/messages/dropped —— 因 inflight/队列上限被丢弃的 PUBLISH 累计（对账环节②）
#   $SYS/broker/store/messages/count     —— 消息存储当前条数（离线队列积压观测）
#   $SYS/broker/clients/connected        —— 连接客户端数（sim/iot/设备活性）
#   $SYS/broker/heap/current             —— broker 堆内存字节（30-mqtt.yaml limits 核验观测面）
# dropped 以 Δ 增量计入 Counter（基线=首次采样值，iot 启动前的历史丢弃不计；broker 重启清零
# 自动识别重置基线）。三方对账：hsf_sim_publish_total + hsf_sim_publish_fail_total ± 队列存量
# ≈ hsf_iot_mqtt_received_total + hsf_broker_messages_dropped_total。
BROKER_DROPPED = Counter("hsf_broker_messages_dropped_total",
                         "broker PUBLISH 丢弃累计（$SYS publish/messages/dropped Δ增量）")
BROKER_STORED = Gauge("hsf_broker_messages_stored",
                      "broker 消息存储当前条数（$SYS store/messages/count）")
BROKER_CLIENTS = Gauge("hsf_broker_clients_connected",
                       "broker 当前连接客户端数（$SYS clients/connected）")
BROKER_HEAP = Gauge("hsf_broker_heap_bytes", "broker 堆内存字节（$SYS heap/current）")
BROKER_SYS_SEEN = Counter("hsf_broker_sys_updates_total", "$SYS 采样消息数（采集链路活性）")
