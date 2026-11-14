"""uplink 测试路径引导：不依赖容器 PYTHONPATH（/srv），自动挂载 app 包与 hsf_common。"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))          # services/uplink/tests
_SERVICES = os.path.dirname(os.path.dirname(_HERE))         # .../services
for _p in (os.path.join(_SERVICES, "common"),               # hsf_common
           os.path.dirname(_HERE)):                         # services/uplink（app 包根）
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pathlib import Path                                     # noqa: E402
from unittest import mock                                    # noqa: E402

import pytest                                                # noqa: E402

_SVC_ROOT = os.path.dirname(_HERE)                           # services/uplink（app 包根）


def pytest_collectstart(collector):
    """多服务 tests 同进程收集时 sys.modules['app'] 只有一槽，rs 与 uplink 同名包会串包。
    仅当收集器位于本服务 tests 目录内时：把 uplink 根钉回 sys.path[0] 并清掉缓存的 app 包，
    保证本服务测试模块导入的是 services/uplink/app。两目录互斥，恰有一个 conftest 钩子生效。"""
    path = getattr(collector, "path", None) or getattr(collector, "fspath", None)
    if path is None or not Path(str(path)).is_relative_to(_HERE):
        return
    while _SVC_ROOT in sys.path:
        sys.path.remove(_SVC_ROOT)
    sys.path.insert(0, _SVC_ROOT)
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]


@pytest.fixture
def mock_db():
    with mock.patch("hsf_common.db.query") as q, mock.patch("hsf_common.db.execute") as e:
        e.return_value = 1
        yield q, e
