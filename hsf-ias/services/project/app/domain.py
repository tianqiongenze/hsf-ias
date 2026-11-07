"""project 领域层（纯函数零 IO）：项目全周期 7 态状态机、统一编码、动作角色表。

依据：03 号 §4.3/§6（状态机冻结）、05 号 §5.1/§5.2（SDL 与转移表冻结）、04 号 §2.1（编码）。
"""
import re

# 7 态：储备→申报→审批→施工→监理→验收→绩效（存储值=04 号 §4.1 冻结枚举；PERFORMED 终态无出边）
STATES = ("RESERVED", "SUBMITTED", "APPROVED", "CONSTRUCTING", "SUPERVISED", "ACCEPTED", "PERFORMED")

# 动作（=mutation 名，冻结存储值——05 号 §5.2，评审 R13）
ACTIONS = ("submitProject", "approveProject", "startConstruction",
           "startSupervision", "acceptanceProject", "acceptanceArchive")

# 转移表：键=(当前态, 动作)（03 号 §6 冻结写法；表外一切组合非法——FR-303）
PROJECT_TRANSITIONS = {
    ("RESERVED", "submitProject"): "SUBMITTED",
    ("SUBMITTED", "approveProject"): "APPROVED",
    ("APPROVED", "startConstruction"): "CONSTRUCTING",
    ("CONSTRUCTING", "startSupervision"): "SUPERVISED",
    ("SUPERVISED", "acceptanceProject"): "ACCEPTED",
    ("ACCEPTED", "acceptanceArchive"): "PERFORMED",
}

# 各动作允许角色（05 号 §5.2 冻结：admin/town/village/keeper/coop）
ACTION_ROLES = {
    "submitProject": ("town", "admin"),
    "approveProject": ("admin",),
    "startConstruction": ("admin", "town"),
    "startSupervision": ("admin", "town"),
    "acceptanceProject": ("admin",),
    "acceptanceArchive": ("admin",),
}

# 立项允许角色（03 号 §4.3 #1：admin/town）
CREATE_ROLES = ("admin", "town")

# 领域事件 topic（03 号 §4.7 字典冻结）
TOPIC_CREATED = "hsf.project.created"
EVENT_TOPICS = {
    "submitProject": "hsf.project.submitted",
    "approveProject": "hsf.project.approved",
    "startConstruction": "hsf.project.construction.started",
    "startSupervision": "hsf.project.supervision.started",
    "acceptanceProject": "hsf.project.accepted",
    "acceptanceArchive": "hsf.project.performed",
}

# 统一编码：P+6 位行政区划（县级 530600）+4 位县内序号，^P\d{10}$（04 号 §2.1 冻结）
CODE_RE = re.compile(r"^P\d{10}$")
COUNTY_PREFIX = "P530600"


class BusinessError(Exception):
    """领域业务错误：code 对齐 05 号 §2.1 错误码注册表（非法流转=INVALID_TRANSITION，V-22 口径）。"""

    def __init__(self, code: str, msg: str):
        self.code, self.msg = code, msg
        super().__init__(msg)


def make_code(seq: int) -> str:
    """序号 → 项目编码（P5306000001 起，04 号 §2.3 同基线）。"""
    return f"{COUNTY_PREFIX}{seq:04d}"


def transition(current: str, action: str) -> str:
    """纯函数状态机：返回新状态；表外组合（跨态/回退/终态操作）→ INVALID_TRANSITION。

    不落库、不留痕、只记审计由仓储层保证（03 号 §4.3）。
    """
    nxt = PROJECT_TRANSITIONS.get((current, action))
    if nxt is None:
        raise BusinessError("INVALID_TRANSITION", f"项目处于 {current} 状态，不允许 {action}")
    return nxt


def valid_build_year(year: int) -> bool:
    """项目年度防 SMALLINT 溢出与明显误录（DB 列 SMALLINT，04 号 §4.1）。"""
    return isinstance(year, int) and 1990 <= year <= 2100
