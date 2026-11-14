"""hsf_common 测试路径引导（同各服务 conftest 口径）：挂载 hsf_common 包。"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))              # services/common/tests
_SERVICES = os.path.dirname(os.path.dirname(_HERE))             # .../services
for _p in (os.path.join(_SERVICES, "common"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)
