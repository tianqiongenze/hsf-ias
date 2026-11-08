"""sim 测试路径引导：不依赖容器 PYTHONPATH（/srv），自动挂载 app 包与 hsf_common。"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))          # services/sim/tests
_SERVICES = os.path.dirname(os.path.dirname(_HERE))         # .../services
for _p in (os.path.join(_SERVICES, "common"),               # hsf_common
           os.path.dirname(_HERE)):                         # services/sim（app 包根）
    if _p not in sys.path:
        sys.path.insert(0, _p)
