"""uplink payload 组装/校验/状态机/transport 单测（docs/08 §3 冻结契约）。"""
import json
import sys

sys.path.insert(0, "/srv")

from app import assemble, repo, transport


def test_map_type_frozen_mapping():
    assert assemble.map_type("L5306000001") == "PARCEL"
    assert assemble.map_type("P5306000001") == "PROJECT"
    assert assemble.map_type("F5306000001") == "FACILITY"
    assert assemble.map_type("S5306000001") == "SENSOR"
    assert assemble.map_type("X1") == "OTHER"


def test_validate_payload_rules():
    ok = {"reportBatch": "B1", "countyCode": "530600",
          "items": [{"type": "PARCEL", "code": "L5306000001"}]}
    assert assemble.validate_payload(ok) == []
    bad = {"countyCode": "", "items": [{"type": "XX", "code": ""}]}
    errs = assemble.validate_payload(bad)
    assert any("reportBatch" in e for e in errs)
    assert any("countyCode" in e for e in errs)
    assert any("非法" in e for e in errs)
    assert any("items 为空" in e for e in assemble.validate_payload(
        {"reportBatch": "B2", "countyCode": "530600", "items": []}))


def test_stub_transport_receipt_deterministic():
    ok = {"reportBatch": "B1", "countyCode": "530600",
          "items": [{"type": "PARCEL", "code": "L5306000001"},
                    {"type": "SENSOR", "code": "S5306000001"}]}
    r1, ok1 = transport.send(ok)
    r2, ok2 = transport.send(ok)
    assert ok1 and ok2 and r1 == r2              # 确定性回执（同 payload 同 checksum）
    assert r1["receipt"]["items"] == 2
    bad_r, bad_ok = transport.send({"reportBatch": "", "items": []})
    assert not bad_ok and bad_r["accepted"] is False


def test_state_machine_transitions(mock_db):
    q, e = mock_db
    payload = {"reportBatch": "B1", "countyCode": "530600", "items": [], "summary": {}}
    # DB 行为：payload/receipt 为 JSON 字符串，get_task 内 json.loads 还原
    row = {"id": 1, "batch_code": "B1", "payload": json.dumps(payload), "item_count": 0,
           "state": "PENDING", "receipt": None, "sent_at": None, "created_at": None}
    q.return_value = row
    task = repo.get_task("B1")
    assert task["state"] == "PENDING" and task["payload"]["reportBatch"] == "B1"
    receipt, ok = transport.send(task["payload"])
    repo.mark_sent("B1", receipt, ok)
    e.assert_called()                            # 状态迁移写库被调用
