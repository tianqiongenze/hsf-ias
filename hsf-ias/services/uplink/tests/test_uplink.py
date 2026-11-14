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


def test_build_payload_embeds_report_batch(mock_db):
    """创建任务回填 reportBatch（docs/08 §3.1 契约首字段）——缺失则 stub 校验必 REJECTED
    （2026-11-13 评审修订前真实缺陷：build_payload 返回空 reportBatch → send 全 REJECTED）。"""
    q, _ = mock_db
    row = {"code": "P5306000001", "name": "示范项目", "state": "ARCHIVED",
           "build_year": "2026", "content": "高标准农田", "applicant_org_code": "53060010101"}
    q.side_effect = [[row], [], [], []]          # 四类来源查询：项目 1 条 + 其余空
    p = assemble.build_payload("530600", batch="B20261113001")
    assert p["reportBatch"] == "B20261113001"
    assert p["countyCode"] == "530600"
    assert assemble.validate_payload(p) == []    # stub 校验全通过 → send 必 ACKED


def test_build_payload_excludes_other_sensors(mock_db):
    """P3-3 收口（2026-11-14）：非统一编码设备不进 items，仅 EXCLUDED 计数留痕——
    历史脏数据（DEV-*）不再致 stub 拒绝整批（D-08-2 四类契约与状态机闭环前提）。"""
    q, _ = mock_db
    proj = {"code": "P5306000001", "name": "示范项目", "state": "ARCHIVED",
            "build_year": "2026", "content": "", "applicant_org_code": None}
    good = {"sensor_code": "S5306000001", "metric": "soil_moisture",
            "village_code": "53060010101", "parcel_code": None, "state": "ACTIVE"}
    dirty = {"sensor_code": "DEV-P3ACC-01", "metric": "soil_moisture",
             "village_code": "53060010101", "parcel_code": None, "state": "ACTIVE"}
    q.side_effect = [[proj], [], [], [good, dirty]]   # 项目/地块/设施/传感器
    p = assemble.build_payload("530600", batch="B1")
    assert {it["type"] for it in p["items"]} == {"PROJECT", "SENSOR"}   # 无 OTHER
    assert all(it["code"] != "DEV-P3ACC-01" for it in p["items"])
    assert p["summary"]["EXCLUDED"] == 1 and p["excludedOther"] == 1    # 如实计数不静默
    assert assemble.validate_payload(p) == []                           # stub 校验通过 → ACKED
