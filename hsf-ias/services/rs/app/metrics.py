"""rs 业务指标（hsf_rs_* 命名前缀；HTTP 指标由 hsf_common.web 提供）。"""
from prometheus_client import Counter

SCENES = Counter("hsf_rs_scenes_total", "影像登记成功景数")
VIGOR_SAVED = Counter("hsf_rs_vigor_saved_total", "地块长势记录写入行数")
FALL_ALERTS = Counter("hsf_rs_fall_alerts_total", "非农化预警生成数（RULE_NDVIFALL）")
