"""ratelimit 测试路径引导：不依赖容器 PYTHONPATH（/srv），自动挂载 app 包与 hsf_common。"""
import os
import sys
from pathlib import Path

import pytest
from prometheus_client import REGISTRY

_HERE = os.path.dirname(os.path.abspath(__file__))          # services/ratelimit/tests
_SERVICES = os.path.dirname(os.path.dirname(_HERE))         # .../services
for _p in (os.path.join(_SERVICES, "common"),               # hsf_common
           os.path.dirname(_HERE)):                         # services/ratelimit（app 包根）
    if _p not in sys.path:
        sys.path.insert(0, _p)
if _HERE not in sys.path:                                   # 兄弟 test 模块互导（importlib 模式
    sys.path.insert(0, _HERE)                               # 不自动挂 tests 目录，prepend 已废弃）

_SVC_ROOT = os.path.dirname(_HERE)                           # services/ratelimit（app 包根）
_OWN_APP = Path(_SVC_ROOT) / "app"
_OTHER_ROOTS = [os.path.join(_SERVICES, d) for d in sorted(os.listdir(_SERVICES))
                if d not in ("common", os.path.basename(_SVC_ROOT))
                and os.path.isdir(os.path.join(_SERVICES, d))]

# conftest 载入时刻（早于本服务 app.main 导入）已注册的 hsf_http_* collector 快照：
# 全量单测下=此前服务经 hsf_common.web 注册的同名族；本服务单独跑=空（不清理任何对象）
_PREEXISTING_HTTP_COLLECTORS = [c for c, names in list(REGISTRY._collector_to_names.items())
                                if any(n.startswith("hsf_http_") for n in names)]


def pytest_collectstart(collector):
    """多服务 tests 同进程收集时 sys.modules['app'] 只有一槽：同名 app 包串包——且常规包
    （rs/uplink 的 app/ 带 __init__.py）在 sys.path 上必然压过本服务的命名空间 app/（多数
    服务无 __init__.py），仅钉根 sys.path[0] 不够。故本钩子（仅当收集器位于本服务 tests
    目录内时生效；在测试模块导入前触发，对本次导入生效）做三件事：
    ① 移出其他服务根目录（防其他服务常规 app 包鸠占鹊巢；common 保留供 hsf_common 导入）；
    ② 把本服务根钉回 sys.path[0]；
    ③ 清除"非本服务"的 app* 缓存（异服务模块与命名空间父包——__file__ 为 None 或不在
      本服务 app/ 下）；本服务已导入模块保留缓存，避免 main.py 等模块级 prometheus 注册
      被二次导入触发 Duplicated timeseries（rs/app/metrics.py _counter 同题防护）。"""
    path = getattr(collector, "path", None) or getattr(collector, "fspath", None)
    if path is None or not Path(str(path)).is_relative_to(_HERE):
        return
    for _p in _OTHER_ROOTS:
        while _p in sys.path:
            sys.path.remove(_p)
    while _SVC_ROOT in sys.path:
        sys.path.remove(_SVC_ROOT)
    sys.path.insert(0, _SVC_ROOT)
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        f = getattr(sys.modules[name], "__file__", None)
        if f is None or not Path(str(f)).resolve().is_relative_to(_OWN_APP):
            del sys.modules[name]
    # main.py:24-25 自注册 hsf_http_requests_total/hsf_http_request_seconds（与
    # hsf_common/web.py:12-13 同名同标签——容器内各服务独立进程无冲突；全量单测同进程
    # 则此前服务经 hsf_common.web 已注册同名族，不清让位将 DuplicateTimeseries）。
    # 仅清理"conftest 载入时（早于本服务 app.main 导入）已注册"的 hsf_http_* collector
    # （快照法——pytest 9 collectstart 存在晚于测试模块导入的触发时点，无差别按名清理
    # 会误删本服务已注册 collector，test_limit.py::test_metrics 实测踩坑）；本服务自注册
    # collector 不入快照永不误删，test_limit.py:196 断言由其满足。
    for _coll in _PREEXISTING_HTTP_COLLECTORS:
        if _coll in REGISTRY._collector_to_names:
            REGISTRY.unregister(_coll)
