"""HSF rs TiDB 访问：rs_scene/rs_vigor DDL 与读写 + 非农化预警落库（复用二期告警/通知管道）。

D-08-1：只新增表，存量零变更；db 层复用 hsf_common（一期同款双账号连接池）。
"""
import datetime as dt
import json

from hsf_common import db

from . import metrics, ndvi

DDL = [
    """CREATE TABLE IF NOT EXISTS hsf_biz.rs_scene(
  id           BIGINT NOT NULL AUTO_INCREMENT,
  scene_code   CHAR(32) NOT NULL COMMENT '影像景编码（幂等键）',
  source       VARCHAR(32) NOT NULL DEFAULT 'SAMPLE' COMMENT 'SAMPLE=合成样本/SENTINEL=真实（预留）',
  acquired_at  DATETIME NOT NULL COMMENT '成像时刻（naive 本地，A-04-3 口径）',
  bands        JSON NOT NULL COMMENT '波段清单与矩阵（样本栅格内联）',
  width        INT NOT NULL, height INT NOT NULL,
  cell_size_m  DECIMAL(8,2) NOT NULL DEFAULT 10.0 COMMENT '像元分辨率（米）',
  state        VARCHAR(16) NOT NULL DEFAULT 'REGISTERED' COMMENT 'REGISTERED/PROCESSED/FAILED',
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id), UNIQUE KEY uk_scene (scene_code)
) COMMENT='遥感影像登记（D-08-1 新增，存量零变更）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.rs_vigor(
  id           BIGINT NOT NULL AUTO_INCREMENT,
  scene_code   CHAR(32) NOT NULL,
  parcel_code  CHAR(20) NOT NULL COMMENT '跨库逻辑引用 land_parcel（D-04-3）',
  ndvi_mean    DECIMAL(6,4) NOT NULL, ndvi_min DECIMAL(6,4) NOT NULL, ndvi_max DECIMAL(6,4) NOT NULL,
  vigor_level  VARCHAR(16) NOT NULL COMMENT '旺长/正常/偏弱/长势差（docs/08 §2.3 样本期标定阈值）',
  pixel_count  INT NOT NULL,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id), UNIQUE KEY uk_vigor (scene_code, parcel_code),
  KEY idx_vigor_scene (scene_code)
) COMMENT='按地块 NDVI 长势（解译结果）'""",
]


def init_db() -> None:
    """幂等建表（D-08-1：存量零变更）。"""
    for ddl in DDL:
        db.execute(ddl.replace("（%", "（百分数").replace("%）", "百分数）"))  # W2-1 同源教训：无裸百分号


def count_scenes() -> int:
    row = db.query("SELECT count(*) AS n FROM hsf_biz.rs_scene", one=True)
    return int(row["n"]) if row else 0


def insert_scene(scene_code: str, source: str, acquired_at: str,
                 red: list, nir: list, width: int, height: int,
                 cell_size_m: float) -> int | None:
    """登记影像（幂等：scene_code 冲突返回 None 不重复插入）。"""
    bands = json.dumps({"red": red, "nir": nir}, ensure_ascii=False, separators=(",", ":"))
    n = db.execute(
        "INSERT IGNORE INTO hsf_biz.rs_scene(scene_code,source,acquired_at,bands,width,height,cell_size_m) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (scene_code, source, acquired_at[:19], bands, width, height, cell_size_m))
    metrics.SCENES.inc(n)
    return None if n == 0 else db.query(
        "SELECT id FROM hsf_biz.rs_scene WHERE scene_code=%s", (scene_code,), one=True)["id"]


def get_scene(scene_code: str) -> dict | None:
    row = db.query("SELECT id, scene_code, source, acquired_at, bands, width, height, "
                   "cell_size_m, state FROM hsf_biz.rs_scene WHERE scene_code=%s",
                   (scene_code,), one=True)
    if row is None:
        return None
    row["bands"] = json.loads(row["bands"])
    row["acquired_at"] = row["acquired_at"]
    return row


def mark_scene(scene_code: str, state: str) -> None:
    db.execute("UPDATE hsf_biz.rs_scene SET state=%s WHERE scene_code=%s",
               (state, scene_code))


def record_event(topic: str, payload: dict) -> None:
    """domain_event 留痕（outbox 模式，ADR-HSF-004 沿用；§2.2 ⑤ 解译失败/预警同源）。"""
    db.execute("INSERT INTO hsf_biz.domain_event(topic,payload) VALUES(%s,%s)",
               (topic, json.dumps(payload, ensure_ascii=False)))


def parcels_geojson() -> list[dict]:
    """地块掩膜源（与 land 子图同源同表）：code + rings（Polygon 全部环，外环在前）。
    一期实际列名 geo_json（下划线），GeoJSON 顶层 coordinates 或 geometry.coordinates 双兼容。"""
    rows = db.query("SELECT code, geo_json FROM hsf_md.land_parcel WHERE geo_json IS NOT NULL")
    out = []
    for r in rows:
        try:
            g = json.loads(r["geo_json"])
            coords = (g.get("geometry") or {}).get("coordinates") or g.get("coordinates") or []
            out.append({"code": r["code"], "rings": coords})
        except Exception:
            continue
    return out


def save_vigor(scene_code: str, vigors: list[dict]) -> int:
    """解译结果批量落库（uk_vigor 幂等：重解译不重复）。"""
    if not vigors:
        return 0
    rows = [(scene_code, v["parcel_code"], v["ndvi_mean"], v["ndvi_min"], v["ndvi_max"],
             v["vigor_level"], v["pixel_count"]) for v in vigors]
    marks = ",".join(["%s"] * len(rows))
    n = db.execute(f"INSERT IGNORE INTO hsf_biz.rs_vigor(scene_code,parcel_code,ndvi_mean,"
                   f"ndvi_min,ndvi_max,vigor_level,pixel_count) VALUES {marks}", rows)
    metrics.VIGOR_SAVED.inc(n)
    return n


def vigor_by_scene(scene_code: str) -> list[dict]:
    return db.query("SELECT scene_code, parcel_code, ndvi_mean, ndvi_min, ndvi_max, "
                    "vigor_level, pixel_count FROM hsf_biz.rs_vigor WHERE scene_code=%s "
                    "ORDER BY parcel_code", (scene_code,))


def jwt_role(user: dict) -> str | None:
    """JWT 角色透传（对齐 iot repo.jwt_role 语义：admin/town/village/keeper 首角色）。"""
    roles = user.get("roles") or []
    return roles[0] if roles else None


def vigor_list(scene_code: str | None, parcel_code: str | None, level: str | None,
               page: int, size: int, role: str | None) -> dict:
    """分页查询（三期口径：长势数据对四角色全量可见——无村界敏感字段，简化范围语义）。"""
    where, args = "WHERE 1=1", []
    if scene_code:
        where += " AND scene_code=%s"; args.append(scene_code)
    if parcel_code:
        where += " AND parcel_code=%s"; args.append(parcel_code)
    if level:
        where += " AND vigor_level=%s"; args.append(level)
    total = (db.query(f"SELECT count(*) AS n FROM hsf_biz.rs_vigor {where}", tuple(args),
                      one=True) or {}).get("n", 0)
    items = db.query(f"SELECT scene_code,parcel_code,ndvi_mean,ndvi_min,ndvi_max,vigor_level,"
                     f"pixel_count,created_at FROM hsf_biz.rs_vigor {where} "
                     f"ORDER BY id DESC LIMIT %s OFFSET %s",
                     tuple(args) + (size, (page - 1) * size))
    return {"total": int(total), "page": page, "size": size, "items": items}


def vigor_latest(parcel_code: str) -> dict | None:
    return db.query("SELECT scene_code,parcel_code,ndvi_mean,ndvi_min,ndvi_max,vigor_level,"
                    "pixel_count,created_at FROM hsf_biz.rs_vigor WHERE parcel_code=%s "
                    "ORDER BY id DESC LIMIT 1", (parcel_code,), one=True)


def prev_mean(parcel_code: str, before_created: str) -> float | None:
    """该地块上一景 NDVI 均值（非农化预警用，按登记时间序）。"""
    row = db.query("SELECT v.ndvi_mean FROM hsf_biz.rs_vigor v "
                   "JOIN hsf_biz.rs_scene s ON s.scene_code=v.scene_code "
                   "WHERE v.parcel_code=%s AND s.acquired_at < %s "
                   "ORDER BY s.acquired_at DESC LIMIT 1",
                   (parcel_code, before_created), one=True)
    return float(row["ndvi_mean"]) if row else None


def alerts_list(rule_code: str, page: int, size: int, role: str | None) -> dict:
    where, args = "WHERE a.rule_code=%s", [rule_code]
    total = (db.query(f"SELECT count(*) AS n FROM hsf_biz.alert_record a {where}",
                      tuple(args), one=True) or {}).get("n", 0)
    # 列名对齐一期冻结 DDL（iot/app/repo.py：alert_record 列为 sensor_code/trig_value，
    # 无 device_code/value——2026-11-14 部署实测 1054 修复，D-08-1 存量零变更）
    items = db.query(f"SELECT a.id, a.rule_code, a.sensor_code, a.metric, a.trig_value, a.trig_time, "
                     f"a.village_code, a.parcel_code, a.state FROM hsf_biz.alert_record a {where} "
                     f"ORDER BY a.id DESC LIMIT %s OFFSET %s",
                     tuple(args) + (size, (page - 1) * size))
    return {"total": int(total), "page": page, "size": size, "items": items}


def _trig_time_with_parcel_offset(acq: str, parcel_idx: int) -> str:
    """R-13 方案 b（docs/24 §4；docs/23 复核附录#12 多地块预警静默丢失修复）：
    trig_time = 触发读数 acquired_at + 景内地块序号 × 1000 微秒。

    机理：uk_rule_reading(sensor_code, rule_code, trig_time) 库级唯一键下，同景多地块降幅时
    sensor_code（RS-<scene> 每景同值）与 trig_time（每景同取 acquired_at）全同 → 覆盖 parcel
    粒度 NOT EXISTS → 仅落首条、其余静默丢失。偏移使幂等键内嵌地块序号，不改 DDL
    （D-08-1 存量零变更红线）、存量数据不迁移。
    步长取 1000μs（=1ms）：冻结 DDL trig_time 为 DATETIME(3) 毫秒精度，亚毫秒偏移会被列取整
    折叠回同值，1ms/地块在列精度下保持可辨识；sensor_code 已内嵌 scene_code → 唯一性按景内
    判定，降幅地块 >1000 时偏移溢出到 acquired_at 秒级之后，键仍单调可辨识。
    序号取景内全部地块（parcel_code 排序）而非仅降幅子集——阈值重标定改变降幅集合时序号
    稳定，重解译/重放 INSERT IGNORE 幂等去重语义不变（docs/08 §2.3 重载口径声明，R-14）。
    """
    base = dt.datetime.strptime(acq, "%Y-%m-%d %H:%M:%S")
    return (base + dt.timedelta(microseconds=1000 * parcel_idx)).strftime(
        "%Y-%m-%d %H:%M:%S.%f")[:23]


def ndvi_fall_alerts(vigors: list[dict], scene_code: str, acquired_at: str) -> int:
    """非农化预警：相邻两景降幅判定 → alert_record + notify（复用二期管道，D-08-1 §2.3）。

    R-13 落库粒度：trig_time 按景内确定性地块序偏移（见 _trig_time_with_parcel_offset），
    同景多地块降幅各自落一行，不再被 uk_rule_reading 静默去重；重解译同序同偏移，幂等不变。"""
    # acquired_at 兼容 datetime（get_scene 返回 pymysql DATETIME 对象）与 str（seed 路径）——
    # 2026-11-14 部署实测 TypeError: 'datetime.datetime' object is not subscriptable 修复
    acq = (acquired_at.strftime("%Y-%m-%d %H:%M:%S")
           if hasattr(acquired_at, "strftime") else str(acquired_at))[:19]
    # R-13：景内全地块确定性地块序（parcel_code 排序，与 ndvi.aggregate 输出序一致）
    parcel_idx = {code: i for i, code in enumerate(sorted(v["parcel_code"] for v in vigors))}
    created = 0
    for v in vigors:
        prev = prev_mean(v["parcel_code"], acq)
        if not ndvi.fall_alerts(prev, v["ndvi_mean"]):
            continue
        trig_time = _trig_time_with_parcel_offset(acq, parcel_idx[v["parcel_code"]])
        n = db.execute(
            # sensor_code=RS-<scene> 合成源标识（非真实传感器；截断 20 对齐冻结 DDL CHAR(20)）；
            # trig_time=acquired_at+地块序号 1000μs 偏移（R-13 方案 b，docs/08 §2.3 重载口径）
            "INSERT IGNORE INTO hsf_biz.alert_record(rule_code,sensor_code,metric,trig_value,"
            "trig_time,village_code,parcel_code,state) "
            "SELECT 'RULE_NDVIFALL', %s, 'ndvi', %s, %s, "
            "COALESCE(l.village_code,''), l.code, 'OPEN' FROM hsf_md.land_parcel l "
            "WHERE l.code=%s AND NOT EXISTS(SELECT 1 FROM hsf_biz.alert_record r "
            "WHERE r.rule_code='RULE_NDVIFALL' AND r.parcel_code=%s AND r.trig_time=%s)",
            (("RS-" + scene_code)[:20], v["ndvi_mean"], trig_time, v["parcel_code"],
             v["parcel_code"], trig_time))
        created += n
        if n:
            db.execute(
                "INSERT INTO hsf_biz.domain_event(topic,payload) VALUES(%s,%s)",
                ("rs.ndvi.fall", json.dumps({"parcel": v["parcel_code"], "scene": scene_code,
                                             "ndvi": v["ndvi_mean"]}, ensure_ascii=False)))
            _notify(v, scene_code, prev, trig_time)
    metrics.FALL_ALERTS.inc(created)
    return created


def _notify(v: dict, scene_code: str, prev: float | None, trig_time: str) -> None:
    """预警通知（对齐二期 notify 契约：kind=ALERT，四角色行——村/乡镇口径）。

    R-13：按偏移后 trig_time 精确回查本地块 alert 行（原 _scene_acquired 按景查 acquired_at
    在多地块偏移后不再命中本地块行）。"""
    title = "非农化预警"
    content = (f"地块 {v['parcel_code']} 遥感长势异常：NDVI 由 "
               f"{prev:.3f} 降至 {v['ndvi_mean']:.3f}（降幅超 0.15 且低于 0.30），"
               f"疑似非农化/非粮化变化，请实地核查")
    roles = ["admin", "town", "village"]
    for role in roles:
        db.execute(
            "INSERT IGNORE INTO hsf_biz.notify(alert_id,advice_id,kind,title,content,"
            "user_role,village_code,dedup_key) "
            "SELECT a.id, NULL, 'ALERT', %s, %s, %s, a.village_code, %s "
            "FROM hsf_biz.alert_record a WHERE a.rule_code='RULE_NDVIFALL' "
            "AND a.parcel_code=%s AND a.trig_time=%s LIMIT 1",
            (title, content, role, f"{scene_code}:{v['parcel_code']}:{role}",
             v["parcel_code"], trig_time))


def now_local() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
