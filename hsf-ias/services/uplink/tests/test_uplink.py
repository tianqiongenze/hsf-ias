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
    # R-24 起 PARCEL 项须含空间矢量（geoWkt/geoWktHint 至少一项）——本用例以 hint 满足
    ok = {"reportBatch": "B1", "countyCode": "530600",
          "items": [{"type": "PARCEL", "code": "L5306000001", "geoWktHint": "POLYGON(102.1 27.1,...)"}]}
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
          "items": [{"type": "PARCEL", "code": "L5306000001", "geoWktHint": "POLYGON(102.1 27.1,...)"},
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


# ── R-24（2026-11-14，schemaVersion 1.2 / ADR-HSF-024）：空间矢量增强 ─────────────

_PARCEL_ROW = {"code": "L5306000001", "name": "示范地块", "area_mu": 12.5, "holder_id": 1,
               "village_code": "53060010101", "holder_name": "张三",
               # GeoJSON 未闭合环（4 顶点，首≠尾——RFC 7946 允许，序列化侧负责闭合）
               "geo_json": '{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[102.1, 27.1], [102.2, 27.1], [102.2, 27.2], [102.1, 27.2]]]}, "properties": {}}'}


def _parcel_payload(q):
    q.side_effect = [[], [_PARCEL_ROW], [], []]   # 项目空 + 地块 1 条 + 设备/传感器空
    return assemble.build_payload("530600", batch="B1")


def test_build_payload_geo_wkt_full_closed_ring(mock_db):
    """geoWkt=完整闭合环：点数=源数据+闭合补点（首点补尾），顶点逐一与源一致（上图入库口径）。"""
    q, _ = mock_db
    p = _parcel_payload(q)
    it = next(it for it in p["items"] if it["type"] == "PARCEL")
    src = json.loads(_PARCEL_ROW["geo_json"])["geometry"]["coordinates"][0]
    assert it["geoWkt"].startswith("POLYGON((") and it["geoWkt"].endswith("))")
    pts = [tuple(float(x) for x in pt.split()) for pt in
           it["geoWkt"][len("POLYGON(("):-len("))")].split(",")]
    assert len(pts) == len(src) + 1              # 4 顶点 + 闭合补点 = 5 位置
    assert pts[:len(src)] == [tuple(map(float, xy)) for xy in src]   # 全量序列化，无截断
    assert pts[-1] == pts[0]                     # 闭合环
    assert assemble.validate_payload(p) == []    # stub 校验（含 geoWkt 闭合检查）通过 → ACKED


def test_build_payload_keeps_geo_wkt_hint_compat(mock_db):
    """geoWktHint 保留兼容（v1.0/1.1 消费方不受影响）：前 3 点截断提示形态不变。"""
    q, _ = mock_db
    p = _parcel_payload(q)
    it = next(it for it in p["items"] if it["type"] == "PARCEL")
    assert it["geoWktHint"] == "POLYGON(102.1 27.1,102.2 27.1,102.2 27.2,...)"


def test_build_payload_geo_wkt_already_closed_not_duplicated():
    """已闭合环（首=尾）不重复补点：位置数=源数据。"""
    closed = json.dumps({"geometry": {"type": "Polygon",
                         "coordinates": [[[102.1, 27.1], [102.2, 27.1], [102.2, 27.2],
                                          [102.1, 27.1]]]}})
    wkt = assemble._wkt_full(closed)
    assert wkt.count(",") == 3 and wkt.endswith("102.1 27.1))")


def test_build_payload_geo_wkt_invalid_returns_empty():
    """geo_json 缺失/非法/顶点不足 → geoWkt 空串（geoWktHint 兜底，validate 按至少一项放行）。"""
    assert assemble._wkt_full("") == ""
    assert assemble._wkt_full("not-json") == ""
    assert assemble._wkt_full(json.dumps({"geometry": {"coordinates": [[]]}})) == ""
    assert assemble._wkt_full(json.dumps({"geometry": {"coordinates": [
        [[102.1, 27.1], [102.2, 27.2]]]}})) == ""   # 2 顶点不成环


def test_build_payload_schema_version_1_2_phases_reserved(mock_db):
    """schemaVersion=1.2 且 phases 三阶段预留结构在位（status=PENDING/summary=None——状态机不动）。"""
    q, _ = mock_db
    p = _parcel_payload(q)
    assert p["schemaVersion"] == "1.2"
    assert set(p["phases"]) == {"planPhase", "buildPhase", "acceptPhase"}
    assert all(ph == {"status": "PENDING", "summary": None} for ph in p["phases"].values())


def test_build_payload_parcel_without_geo_rejected_with_reason(mock_db):
    """无 geo_json 的 PARCEL：geoWkt/geoWktHint 均空 → stub REJECTED 并给出明确原因
    （上图入库口径下缺几何=数据缺陷，与 P3-3 防御纵深同原则——拒收并留痕，不带病上报）。"""
    q, _ = mock_db
    row = dict(_PARCEL_ROW, geo_json="")
    q.side_effect = [[], [row], [], []]
    p = assemble.build_payload("530600", batch="B1")
    it = next(it for it in p["items"] if it["type"] == "PARCEL")
    assert it["geoWkt"] == "" and it["geoWktHint"] == ""
    errs = assemble.validate_payload(p)
    assert any("缺空间矢量" in e and "L5306000001" in e for e in errs)


def test_validate_payload_rejects_parcel_without_geo():
    """v1.2 口径：PARCEL 两项空间矢量全缺 → 校验错误（1.0/1.1 历史归档 payload 不受影响——
    schemaVersion 非 1.2 时同规则仍适用 geoWkt/hint 检查？否——hint 自 1.0 起即存在，
    空矢量属数据缺陷而非版本差异，故 PARCEL 检查与版本无关）。"""
    bad = {"reportBatch": "B1", "countyCode": "530600", "schemaVersion": "1.1",
           "items": [{"type": "PARCEL", "code": "L5306000001"}]}
    errs = assemble.validate_payload(bad)
    assert any("缺空间矢量" in e for e in errs)


def test_validate_payload_rejects_open_ring():
    """geoWkt 未闭合环/位置不足/非 POLYGON 形态 → 校验错误（完整性校验规则扩展）。"""
    base = {"reportBatch": "B1", "countyCode": "530600", "schemaVersion": "1.2",
            "phases": {k: {"status": "PENDING", "summary": None}
                       for k in ("planPhase", "buildPhase", "acceptPhase")}}
    cases = [
        "POLYGON((102.1 27.1,102.2 27.1,102.2 27.2,102.1 27.2))",   # 未闭合
        "POLYGON((102.1 27.1,102.2 27.1,102.1 27.1))",              # <4 位置
        "POLYGON(102.1 27.1,...)",                                  # 形态非法
    ]
    for wkt in cases:
        errs = assemble.validate_payload({**base, "items": [
            {"type": "PARCEL", "code": "L5306000001", "geoWkt": wkt, "geoWktHint": "x"}]})
        assert any("geoWkt" in e for e in errs), wkt


def test_validate_payload_schema_version_whitelist():
    """schemaVersion 白名单（1.0/1.1 历史归档重发放行；未知版本拒绝）。"""
    ok11 = {"reportBatch": "B1", "countyCode": "530600", "schemaVersion": "1.1",
            "items": [{"type": "SENSOR", "code": "S5306000001"}]}
    assert assemble.validate_payload(ok11) == []
    bad = {"reportBatch": "B1", "countyCode": "530600", "schemaVersion": "9.9",
           "items": [{"type": "SENSOR", "code": "S5306000001"}]}
    assert any("schemaVersion 非法" in e for e in assemble.validate_payload(bad))
    miss12 = {"reportBatch": "B1", "countyCode": "530600", "schemaVersion": "1.2",
              "items": [{"type": "SENSOR", "code": "S5306000001"}]}
    assert any("phases" in e for e in assemble.validate_payload(miss12))


def test_validate_payload_oversize_rejected():
    """体积限值：> 8MiB → REJECTED 促成分批（不留档案外超大 payload）。"""
    big = {"reportBatch": "B1", "countyCode": "530600", "schemaVersion": "1.1",
           "items": [{"type": "PARCEL", "code": f"L{i}", "geoWktHint": "x" * 1024 * 1024}
                     for i in range(9)]}          # ≈9MiB
    errs = assemble.validate_payload(big)
    assert any("超过限值" in e for e in errs)


def test_payload_size_recorded_within_limit(mock_db, capsys):
    """验收口径"payload 体积记录在案（<设定限值）"：真实样本体积实测打印（测试留痕）且 < 限值。"""
    q, _ = mock_db
    p = _parcel_payload(q)
    size = assemble.payload_size(p)
    print(f"R-24 payload 体积实测：{size} bytes（限值 {assemble.MAX_PAYLOAD_BYTES}）")
    assert size < assemble.MAX_PAYLOAD_BYTES
