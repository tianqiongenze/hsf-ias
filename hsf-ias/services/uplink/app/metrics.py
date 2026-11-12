"""uplink 业务指标（hsf_uplink_* 前缀；HTTP 指标由 hsf_common.web 提供）。"""
from prometheus_client import Counter

TASKS = Counter("hsf_uplink_tasks_total", "上报任务创建批数")
SENDS = Counter("hsf_uplink_sends_total", "上报发送结果数", ["state"])
ACKED = Counter("hsf_uplink_acked_total", "平台回执确认数（ACKED）")
