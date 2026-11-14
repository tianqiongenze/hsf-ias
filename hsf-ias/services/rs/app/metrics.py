"""rs 业务指标（hsf_rs_* 命名前缀；HTTP 指标由 hsf_common.web 提供）。"""
from prometheus_client import REGISTRY, Counter


def _counter(name: str, doc: str) -> Counter:
    """幂等注册（测试环境 conftest 清 sys.modules['app.*'] 后重导入会二次注册同一指标名；
    复用已注册 collector，进程内指标语义不变）。"""
    try:
        return Counter(name, doc)
    except ValueError:
        return REGISTRY._names_to_collectors[name]


SCENES = _counter("hsf_rs_scenes_total", "影像登记成功景数")
VIGOR_SAVED = _counter("hsf_rs_vigor_saved_total", "地块长势记录写入行数")
FALL_ALERTS = _counter("hsf_rs_fall_alerts_total", "非农化预警生成数（RULE_NDVIFALL）")
