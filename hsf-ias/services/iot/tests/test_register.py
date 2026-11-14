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
