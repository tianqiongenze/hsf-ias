"""iot 业务指标（docs/07 §4.1 冻结命名）；HTTP 指标由 hsf_common.web 提供。"""
from prometheus_client import Counter, Histogram

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
