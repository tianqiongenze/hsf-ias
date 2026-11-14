"""设备注册凭证单测（docs/08 §7"iot（注册凭证）"，不依赖外部服务）。

覆盖 docs/08 §4 冻结契约：SM3(deviceCode+盐)[0:16] 凭证、凭证一次下发、
device_cred 只存 SM3 摘要 + sha256 前 8 位 hint（不落明文）、重复注册幂等返回 None（API 409）。
"""
import hashlib
import json
import sys
from unittest import mock

import pytest

sys.path.insert(0, "/srv")

from hsf_common.crypto import sm3

from app import repo


@pytest.fixture
def mock_db():
    """hsf_common.db 全 mock（单测不连真实 TiDB，同 rs/uplink conftest 口径）。"""
    with mock.patch("hsf_common.db.query") as q, mock.patch("hsf_common.db.execute") as e:
        e.return_value = 1
        yield q, e


def test_register_returns_one_time_credential(mock_db):
    q, e = mock_db
    q.side_effect = [None, None]           # sensor 无重复 + device_cred 无重复 → 允许注册
    out = repo.register_device("S5306001099", "soil_moisture", "53060010101", "L5306000001")
    assert out is not None
    assert out["mqttUsername"] == "S5306001099"
    assert len(out["mqttPassword"]) == 16  # SM3(...)[:16]，docs/08 §4 冻结长度
    assert out["aclPattern"] == "hsf/sensor/S5306001099/#"
    # 台账 + 凭证 + domain_event 三次写库（事件 topic 在参数位）
    assert e.call_count == 3
    sensor_sql, cred_sql, event_sql = (c.args[0] for c in e.call_args_list)
    assert "INSERT INTO hsf_biz.sensor" in sensor_sql
    assert "INSERT INTO hsf_biz.device_cred" in cred_sql
    assert "INSERT INTO hsf_biz.domain_event" in event_sql
    assert e.call_args_list[2].args[1][0] == "iot.device.registered"


def test_register_credential_persisted_hash_and_hint_only(mock_db):
    """docs/08 §4"不落明文"：device_cred 存 SM3 摘要 + sha256 前 8 位 hint，无明文片段。"""
    q, e = mock_db
    q.side_effect = [None, None]
    out = repo.register_device("S5306001100", "air_temp", "53060010101", None)
    pwd = out["mqttPassword"]
    cred_args = e.call_args_list[1].args[1]
    stored_hash, stored_hint = cred_args[1], cred_args[2]
    assert stored_hash == sm3(pwd.encode())            # SM3(明文凭证) 摘要
    assert stored_hint == hashlib.sha256(pwd.encode()).hexdigest()[:8]
    assert pwd[:8] != stored_hint                      # hint 非"明文前缀"（sha256 口径）
    assert pwd not in json.dumps(cred_args, default=str)   # 凭证明文不出现在落库参数


def test_register_idempotent_duplicate_returns_none(mock_db):
    """已存在（台账或凭证任一命中）→ None；main 层映射 409 CONFLICT（API 口径幂等）。"""
    q, e = mock_db
    q.side_effect = [{"sensor_code": "S5306001099"}, None]   # 台账已存在
    assert repo.register_device("S5306001099", "soil_moisture",
                                "53060010101", "L5306000001") is None
    q.side_effect = [None, {"device_code": "S5306001099"}]   # 仅凭证已存在同样拒绝
    assert repo.register_device("S5306001099", "soil_moisture",
                                "53060010101", "L5306000001") is None
    assert e.call_count == 0                                 # 幂等路径零写库


def test_register_metric_and_parcel_contract(mock_db):
    """注册行携带 metric/village/parcel；parcelCode 可为空=未挂地块（docs/08 §3.1 SENSOR 同口径）。"""
    q, e = mock_db
    q.side_effect = [None, None]
    out = repo.register_device("S5306001101", "wind_speed", "53060010101", None)
    sensor_args = e.call_args_list[0].args[1]
    assert sensor_args[0] == "S5306001101" and sensor_args[1] == "wind_speed"
    assert sensor_args[2] == "53060010101" and sensor_args[3] is None
    assert out["parcelCode"] is None


# ── R-18（docs/24 §4，审核缺口#8）：设备凭证 CSPRNG 化 ────────────────────────────

def test_register_credential_csprng_differs_across_sessions(mock_db):
    """同码两次注册会话（mock 放行幂等检查）→ secret 互异（CSPRNG 不可预测性，R-18 验收③）。"""
    q, e = mock_db
    q.side_effect = [None, None, None, None]     # 两轮注册的台账+凭证幂等检查均放行
    s1 = repo.register_device("S5306001099", "soil_moisture", "53060010101",
                              "L5306000001")["mqttPassword"]
    s2 = repo.register_device("S5306001099", "soil_moisture", "53060010101",
                              "L5306000001")["mqttPassword"]
    assert s1 != s2, "两次会话生成的凭证必须互异（随机盐不可复现）"
    assert len(s1) == len(s2) == 16              # SM3 截断长度契约不变


def test_register_credential_not_time_derived(mock_db):
    """冻结时钟下连续两次注册 → secret 仍互异——证明盐非时间派生
    （原实现 salt=now()strftime 在本测试下两次同盐同凭证，即 R-18 修复判据）。"""
    q, e = mock_db
    q.side_effect = [None, None, None, None]
    with mock.patch("app.repo.dt") as fake_dt:
        fake_dt.datetime.now.return_value = fake_dt.datetime(
            2026, 11, 14, 12, 0, 0, 0)           # 时钟完全冻结（含微秒）
        s1 = repo.register_device("S5306001102", "air_temp", "53060010101",
                                  None)["mqttPassword"]
        s2 = repo.register_device("S5306001102", "air_temp", "53060010101",
                                  None)["mqttPassword"]
    assert s1 != s2, "时钟冻结下凭证仍应互异（盐=CSPRNG 非时间戳，R-18）"


def test_register_storage_side_unchanged_after_csprng(mock_db):
    """R-18 存储侧不回归：device_cred 仍只存 SM3 摘要 + sha256 前 8 位 hint（docs/08 §4 口径），
    响应仍含 16 位明文凭证一次下发。"""
    q, e = mock_db
    q.side_effect = [None, None]
    out = repo.register_device("S5306001103", "rainfall", "53060010101", None)
    pwd = out["mqttPassword"]
    cred_args = e.call_args_list[1].args[1]
    assert cred_args[1] == sm3(pwd.encode())                       # SM3 摘要
    assert cred_args[2] == hashlib.sha256(pwd.encode()).hexdigest()[:8]   # sha256 hint
    assert len(pwd) == 16
    assert pwd not in json.dumps(cred_args, default=str)           # 明文不落库
