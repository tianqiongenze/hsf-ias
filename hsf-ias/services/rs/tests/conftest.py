"""rs 测试路径引导：不依赖容器 PYTHONPATH（/srv），自动挂载 app 包与 hsf_common。"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))          # services/rs/tests
_SERVICES = os.path.dirname(os.path.dirname(_HERE))         # .../services
for _p in (os.path.join(_SERVICES, "common"),               # hsf_common
           os.path.dirname(_HERE)):                         # services/rs（app 包根）
    if _p not in sys.path:
        sys.path.insert(0, _p)

from unittest import mock                                   # noqa: E402

import pytest                                               # noqa: E402


@pytest.fixture
def mock_db():
    """hsf_common.db 全 mock（单测不连真实 TiDB——CONVENTIONS §4 同口径）。"""
    with mock.patch("hsf_common.db.query") as q, mock.patch("hsf_common.db.execute") as e:
        e.return_value = 1
        yield q, e
