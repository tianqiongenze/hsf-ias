"""project TDD：FR-303 状态机转移矩阵全覆盖（7 态×6 动作穷举）+ 编码正则，纯函数不依赖 DB。"""
import sys

sys.path.insert(0, "/srv")  # 容器内 WORKDIR=/srv；本地以 PYTHONPATH=. 运行

import pytest

from app.domain import (ACTIONS, ACTION_ROLES, CODE_RE, CREATE_ROLES, EVENT_TOPICS, STATES,
                        PROJECT_TRANSITIONS, make_code, transition, valid_build_year,
                        BusinessError)


# ── 冻结转移表逐条命中（05 号 §5.2 #1~#6）──
def test_frozen_transitions_hit():
    assert PROJECT_TRANSITIONS[("RESERVED", "submitProject")] == "SUBMITTED"
    assert PROJECT_TRANSITIONS[("SUBMITTED", "approveProject")] == "APPROVED"
    assert PROJECT_TRANSITIONS[("APPROVED", "startConstruction")] == "CONSTRUCTING"
    assert PROJECT_TRANSITIONS[("CONSTRUCTING", "startSupervision")] == "SUPERVISED"
    assert PROJECT_TRANSITIONS[("SUPERVISED", "acceptanceProject")] == "ACCEPTED"
    assert PROJECT_TRANSITIONS[("ACCEPTED", "acceptanceArchive")] == "PERFORMED"


# ── 合法链：储备→申报→审批→施工→监理→验收→绩效 逐态推进 ──
def test_happy_path_walks_all_seven_states():
    state = "RESERVED"
    chain = {"submitProject": "SUBMITTED", "approveProject": "APPROVED",
             "startConstruction": "CONSTRUCTING", "startSupervision": "SUPERVISED",
             "acceptanceProject": "ACCEPTED", "acceptanceArchive": "PERFORMED"}
    for act, expect in chain.items():
        state = transition(state, act)
        assert state == expect
    assert state == "PERFORMED"


# ── FR-303：7 态×6 动作矩阵穷举，表外 36 组合全部拒绝 ──
def test_full_matrix_all_illegal_combinations_rejected():
    legal = 0
    for s in STATES:
        for a in ACTIONS:
            if (s, a) in PROJECT_TRANSITIONS:
                assert transition(s, a) == PROJECT_TRANSITIONS[(s, a)]
                legal += 1
            else:
                with pytest.raises(BusinessError) as ei:
                    transition(s, a)
                assert ei.value.code == "INVALID_TRANSITION"
    assert legal == 6  # 冻结表恰好 6 条合法流转


# ── 典型非法路径（05 号 §5.2 右列：跳态/回退必须被拒）──
def test_jump_forward_rejected():
    with pytest.raises(BusinessError):
        transition("RESERVED", "startConstruction")   # 储备→施工（跳态）
    with pytest.raises(BusinessError):
        transition("SUBMITTED", "startConstruction")  # 申报→施工（跳态）
    with pytest.raises(BusinessError):
        transition("APPROVED", "startSupervision")    # 审批→监理（跳态）


def test_rollback_rejected():
    with pytest.raises(BusinessError):
        transition("ACCEPTED", "submitProject")       # 验收→申报（回退）
    with pytest.raises(BusinessError):
        transition("PERFORMED", "submitProject")      # 绩效→申报（回退）


def test_terminal_state_no_out_edges():
    for a in ACTIONS:  # PERFORMED 终态无出边（03 号 §6 约束③）
        with pytest.raises(BusinessError) as ei:
            transition("PERFORMED", a)
        assert ei.value.code == "INVALID_TRANSITION"


def test_unknown_state_and_action_rejected():
    with pytest.raises(BusinessError):
        transition("UNKNOWN", "submitProject")
    with pytest.raises(BusinessError):
        transition("RESERVED", "nonexistentAction")


# ── 动作=mutation 名冻结存储值（评审 R13：project_transition.action 断言对象）──
def test_action_names_are_frozen_mutation_names():
    assert ACTIONS == ("submitProject", "approveProject", "startConstruction",
                       "startSupervision", "acceptanceProject", "acceptanceArchive")
    assert set(ACTIONS) == set(EVENT_TOPICS)  # 每个动作一个领域事件 topic（03 号 §4.7）


# ── 角色表与编码 ──
def test_action_roles_match_frozen_table():
    assert CREATE_ROLES == ("admin", "town")
    assert ACTION_ROLES["approveProject"] == ("admin",)
    assert ACTION_ROLES["acceptanceArchive"] == ("admin",)
    assert "town" in ACTION_ROLES["submitProject"]


def test_project_code_format():
    assert make_code(1) == "P5306000001"
    assert CODE_RE.match(make_code(8)) and len(make_code(8)) == 11
    assert not CODE_RE.match("L5306000001")  # 地块前缀不进项目编码体系


def test_build_year_guard():
    assert valid_build_year(2026)
    assert not valid_build_year(1899) and not valid_build_year(99999)
    assert not valid_build_year("2026")
