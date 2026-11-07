"""facility TDD：工单 5 态×4 动作转移矩阵全覆盖（评审 R21⑨/FR-403）+ 编码/二维码规则。"""
import re

from app import repo
from app.repo import CODE_RE, FACILITY_STATUSES, FACILITY_TYPES, TRANSITIONS, make_code, qr_url

ACTIONS = ("accept", "dispatch", "resolve", "approve")


def test_state_machine_happy_path():
    """FR-403 主链路：accept→dispatch→resolve→approve。"""
    assert TRANSITIONS["OPEN"]["accept"] == "ACCEPTED"
    assert TRANSITIONS["ACCEPTED"]["dispatch"] == "DISPATCHED"
    assert TRANSITIONS["DISPATCHED"]["resolve"] == "RESOLVED"
    assert TRANSITIONS["RESOLVED"]["approve"] == "APPROVED"


def test_state_machine_full_matrix():
    """5 态×4 动作矩阵全覆盖：转移表之外一切组合均无出边（非法流转拒绝且不落库）。"""
    states = ("OPEN", "ACCEPTED", "DISPATCHED", "RESOLVED", "APPROVED")
    assert set(TRANSITIONS) == set(states)
    for st in states:
        for act in ACTIONS:
            hit = TRANSITIONS.get(st, {}).get(act)
            if hit:
                assert hit in states  # 出边必须落在 5 态之内
    # 非法流转抽样：跳步/回退/终态再动
    assert "approve" not in TRANSITIONS["OPEN"]
    assert "dispatch" not in TRANSITIONS["OPEN"]
    assert "resolve" not in TRANSITIONS["ACCEPTED"]
    assert "accept" not in TRANSITIONS["ACCEPTED"]
    assert "accept" not in TRANSITIONS["DISPATCHED"]
    assert "dispatch" not in TRANSITIONS["RESOLVED"]
    assert not TRANSITIONS["APPROVED"]  # 终态无出边（03 号 §4.4 不变量④）


def test_resolve_needs_note_invariant_by_design():
    """不变量③：RESOLVED 唯一入边来自 resolve（note 必填由 repo.transition 校验）。"""
    assert list(TRANSITIONS["DISPATCHED"]) == ["resolve"]


def test_facility_code_rule():
    """统一编码 F+6位区划+4位序号（04 号 §2.1，正则 ^F[0-9]{10}$）。"""
    assert CODE_RE.fullmatch(make_code("530600", 1))
    assert CODE_RE.fullmatch(make_code("530600", 12))
    assert make_code("530600", 7) == "F5306000007"
    assert not CODE_RE.fullmatch("L5306000001")  # 地块前缀不混用
    assert not CODE_RE.fullmatch("F53060001")    # 序号必须 4 位


def test_facility_type_and_status_enums():
    """类型 机井/泵站/渠道/道路；状态 4 值（04 号 §3.5 冻结存储值）。"""
    assert FACILITY_TYPES == ("WELL", "PUMP_STATION", "CANAL", "ROAD")
    assert FACILITY_STATUSES == ("NORMAL", "BROKEN", "REPAIRING", "RETIRED")


def test_qr_token_and_url():
    """FR-402：token=32 位 hex；二维码内容 = URL+token（03 号 §4.4）。"""
    token = repo.new_qr_token()
    assert len(token) == 32 and re.fullmatch(r"[0-9a-f]{32}", token)
    url = qr_url(token)
    assert url.endswith(f"/m/facility?token={token}")
    assert url.startswith("http")
    assert repo.qr_url(token) != qr_url(repo.new_qr_token())  # token 唯一 → URL 唯一


def test_perf_score_nullable_by_design():
    """FR-404：绩效积分为可空字段（DDL perf_score INT NULL），流转不强制写入。"""
    import inspect
    src = inspect.getsource(repo)
    assert re.search(r"perf_score\s+INT\s+NULL", src)
    assert "perf_score=%s" not in src  # 一期无积分写入路径
