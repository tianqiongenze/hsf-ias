"""HSF 全量单测执行期隔离（跨服务同名 app 包，单 pytest 进程）。

机理：各服务 app 包同名（services/<svc>/app），单进程 sys.modules['app'] 只有一槽。
pytest 先完成全部收集（按目录序逐服务导入全部测试模块）再按序执行——收集收尾时
sys.modules['app.*'] 残留最后收集服务（uplink）的模块；先收集服务（facility/iam/iot/
land/project/ratelimit/rs/sim）的测试在执行期的运行时导入与 patch
（mock.patch("app.repo.dt")、__import__("app.mqtt_sub")、用例内 from app import x）
遂解析到 uplink 的模块（实测 7 处失败：iot×3 / rs×2 / ratelimit×2，2026-11-15 全量
单测修复记录）。

本 conftest 做两件事（仅测试基建，不改服务代码与部署面）：
① 收集期快照：pytest_collectreport 逐报告记录"该服务 tests 收集完成时刻"的 app* 模块
  对象表（Directory 级报告晚于其全部子模块 → 快照=该服务测试模块导入完成后的终态）；
② 执行期恢复：pytest_runtest_protocol 逐用例触发——用例属于某服务时，清当前 app* 槽并
  按该服务快照回填 sys.modules。回填对象与该服务测试模块收集期的导入绑定同源（同对象），
  模块级引用、字符串目标 mock.patch、运行时子模块导入三者解析一致。
"""
import sys

_SNAPSHOTS: dict = {}


def _svc_of_nodeid(nodeid: str):
    """收集报告 nodeid（rootdir 相对）位于 services/<svc>/tests 下 → 返回 <svc>。"""
    parts = str(nodeid).replace("\\", "/").split("/")
    if len(parts) >= 3 and parts[0] == "services" and parts[2] == "tests":
        return parts[1]
    return None


def _svc_of_item(item):
    """执行用例绝对路径位于 services/<svc>/tests 下 → 返回 <svc>。"""
    p = getattr(item, "path", None) or getattr(item, "fspath", None)
    if p is None:
        return None
    parts = tuple(str(p).replace("\\", "/").split("/"))
    for i, part in enumerate(parts):
        if part == "tests" and i >= 2 and parts[i - 2] == "services":
            return parts[i - 1]
    return None


def pytest_collectreport(report):
    svc = _svc_of_nodeid(getattr(report, "nodeid", ""))
    if svc is None:
        return
    _SNAPSHOTS[svc] = {n: sys.modules[n] for n in sys.modules
                       if n == "app" or n.startswith("app.")}


def pytest_runtest_protocol(item, nextitem):
    snap = _SNAPSHOTS.get(_svc_of_item(item))
    if not snap:
        return None
    for n in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[n]
    sys.modules.update(snap)
    return None  # 不接管协议，恢复后按 pytest 默认流程执行本用例
