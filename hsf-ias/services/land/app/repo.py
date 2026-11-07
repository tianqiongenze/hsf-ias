"""land 持久化：land_parcel DDL + 仓储（读写分离/同事务审计+Outbox）+ 种子（幂等）。

org/party 表由 iam 子图建（CONVENTIONS §1），本服务只跨库 SELECT 校验（hsf_app 三库授权，
ADR-HSF-004：不做跨服务 HTTP 调用）。
"""
import json
import time
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor
from pymysql.err import IntegrityError

from hsf_common import cache, db

from . import domain
from .cache_keys import GEOJSON_PREFIX, L1_TTL, PARCEL_TTL, GEOJSON_TTL, geojson_key, parcel_key

DDL = [
    """CREATE TABLE IF NOT EXISTS hsf_md.land_parcel(
      id           BIGINT        NOT NULL AUTO_INCREMENT,
      code         CHAR(20)      NOT NULL COMMENT '统一编码 L+6位区划+4位序号（11位实长，列宽预留）',
      name         VARCHAR(64)   NOT NULL COMMENT '地块名称',
      village_code CHAR(16)      NOT NULL COMMENT '所属村 org.code（11位村码；列宽对齐 org.code CHAR(16)——CONVENTIONS §2.3 宁可宽不可窄）',
      holder_id    BIGINT        NULL COMMENT '权属人 party.id（联邦引用 Party@key(id)，FR-104）',
      area_mu      DECIMAL(10,2) NULL COMMENT '面积（亩）',
      soil_type    VARCHAR(32)   NULL COMMENT '土壤类型（红壤/水稻土/黄壤…）',
      crop_type    VARCHAR(32)   NULL COMMENT '作物属性（水稻/玉米/马铃薯…）',
      geo_json     JSON          NULL COMMENT 'GeoJSON geometry（一期 Polygon，WGS84 经纬度；ADR-HSF-001；驱动异常按 A-04-1 回退 MEDIUMTEXT）',
      status       VARCHAR(16)   NOT NULL DEFAULT 'ACTIVE' COMMENT 'ACTIVE/RETIRED（不删行，退役置状态）',
      created_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at   DATETIME      NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_parcel_code (code),
      KEY idx_village (village_code, status),
      KEY idx_holder (holder_id)
    ) COMMENT='地块主数据：登记/分页/GeoJSON 服务（FR-101~104）'""",
]

# 种子：8 地块=4 村各 2（04 号 §2.3），GeoJSON 矩形围绕 102.75E,25.05N；权属=村管/合作社交替（V-17/V-24 数据源）
SEED_PARCELS = [
    ("L5306000001", "清溪村坝区1号田", "53060010101", 12.5, "水稻土", "水稻", "village"),
    ("L5306000002", "清溪村缓坡2号地", "53060010101", 8.2, "红壤", "玉米", "coop"),
    ("L5306000003", "麦香村河谷1号田", "53060010102", 15.8, "水稻土", "水稻", "village"),
    ("L5306000004", "麦香村台地2号地", "53060010102", 6.4, "黄壤", "马铃薯", "coop"),
    ("L5306000005", "柳林村坝区1号田", "53060010103", 18.0, "水稻土", "水稻", "village"),
    ("L5306000006", "柳林村缓坡2号地", "53060010103", 9.6, "红壤", "玉米", "coop"),
    ("L5306000007", "稻花村河谷1号田", "53060010201", 20.3, "水稻土", "水稻", "village"),
    ("L5306000008", "稻花村台地2号地", "53060010201", 7.1, "黄壤", "马铃薯", "coop"),
]


def init_db():
    for ddl in DDL:
        db.execute(ddl)


@contextmanager
def _txn():
    """写池显式事务：业务+审计+Outbox 同事务提交（03 号 §6② / ADR-HSF-004）。"""
    conn = db.write_pool.conn()
    conn.begin()
    try:
        yield conn.cursor(DictCursor)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _ip(ctx) -> str | None:
    try:
        req = ctx.get("request")
        return (req.headers.get("x-forwarded-for") or req.client.host) if req else None
    except Exception:
        return None


def audit(cur, ctx: dict, action: str, object_type: str, object_id: str, result: str, detail: str | None = None):
    """审计同事务落库（FR-704；user_name 快照防改名不可追溯）。"""
    cur.execute(
        "INSERT INTO hsf_biz.audit_log(user_id,user_name,action,object_type,object_id,ip,result,detail) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (ctx.get("user_id") or None, ctx.get("user_name") or None, action, object_type, object_id,
         _ip(ctx), result, detail))


def audit_denied(ctx: dict, action: str, object_type: str, object_id: str, detail: str | None = None):
    """越权/非法操作审计（result=DENIED，写路径必留痕——FR-704）。"""
    db.execute(
        "INSERT INTO hsf_biz.audit_log(user_id,user_name,action,object_type,object_id,ip,result,detail) "
        "VALUES(%s,%s,%s,%s,%s,%s,'DENIED',%s)",
        (ctx.get("user_id") or None, ctx.get("user_name") or None, action, object_type, object_id,
         _ip(ctx), detail))


# ───────── 基础校验（跨库 SELECT，同 TiDB 实例，ADR-HSF-004）─────────
def org_exists(code: str) -> bool:
    return bool(db.query("SELECT 1 FROM hsf_md.org WHERE code=%s", (code,), one=True))


def party_exists(pid: int) -> bool:
    return bool(db.query("SELECT id FROM hsf_md.party WHERE id=%s", (pid,), one=True))


def party_org(pid: int) -> str | None:
    r = db.query("SELECT org_code FROM hsf_md.party WHERE id=%s", (pid,), one=True)
    return r["org_code"] if r else None


# ───────── 查询（读池）─────────
def get(code: str):
    return db.query("SELECT * FROM hsf_md.land_parcel WHERE code=%s", (code,), one=True)


def holder_id_of(code: str):
    r = db.query("SELECT holder_id FROM hsf_md.land_parcel WHERE code=%s", (code,), one=True)
    return r["holder_id"] if r else None


def _where(village_like=None, village_eq=None, holder_id=None) -> tuple[str, list]:
    where, args = ["status='ACTIVE'"], []  # RETIRED 不进一张图与按村默认列表（03 号 §4.2）
    if village_like:
        where.append("village_code LIKE %s")
        args.append(village_like + "%")
    if village_eq:
        where.append("village_code=%s")
        args.append(village_eq)
    if holder_id:
        where.append("holder_id=%s")
        args.append(holder_id)
    return "WHERE " + " AND ".join(where), args


def list_parcels(page: int, size: int, village_like=None, village_eq=None, holder_id=None):
    cond, args = _where(village_like, village_eq, holder_id)
    total = db.query(f"SELECT COUNT(*) c FROM hsf_md.land_parcel {cond}", tuple(args), one=True)["c"]
    rows = db.query(
        f"SELECT * FROM hsf_md.land_parcel {cond} ORDER BY id DESC LIMIT %s OFFSET %s",
        tuple(args) + (size, (page - 1) * size))
    return total, rows


def stats(village_like=None, village_eq=None, holder_id=None):
    """地块总数/总面积（FR-203；COUNT/SUM 读池直查不缓存——05 号 §4.2）。"""
    cond, args = _where(village_like, village_eq, holder_id)
    r = db.query(f"SELECT COUNT(*) c, SUM(area_mu) s FROM hsf_md.land_parcel {cond}", tuple(args), one=True)
    return int(r["c"] or 0), (float(r["s"]) if r["s"] is not None else None)


def geojson_rows(village_code: str, village_like=None, village_eq=None, holder_id=None):
    cond, args = _where(village_like, village_eq, holder_id)
    cond += " AND village_code LIKE %s"
    args.append(village_code + "%")
    args.append(domain.MAX_GEOJSON_FEATURES)
    return db.query(
        f"SELECT code,name,village_code,area_mu,soil_type,crop_type,geo_json "
        f"FROM hsf_md.land_parcel {cond} ORDER BY code LIMIT %s", tuple(args))


# ───────── 写路径（写池同事务：业务+审计+Outbox）─────────
def _take_parcel_code() -> str:
    """写池事务外取号：MAX(RIGHT(code,4))+1（04 号 §2.2 冻结；uk_parcel_code 兜底）。"""
    conn = db.write_pool.conn()
    with conn.cursor() as cur:
        cur.execute("SELECT IFNULL(MAX(CAST(RIGHT(code,4) AS UNSIGNED)),0)+1 FROM hsf_md.land_parcel "
                    "WHERE code LIKE %s", (domain.COUNTY_PREFIX + "%",))
        seq = int(cur.fetchone()[0] or 1)
    return domain.make_code(seq)


def register(name: str, village_code: str, holder_id, area_mu, soil_type, crop_type, geojson, ctx: dict) -> dict:
    """登记地块：取号→同事务[INSERT+audit+Outbox]→失效缓存（FR-101/102；04 号 §8.5 失效映射）。"""
    for _ in range(3):  # 取号冲突重试 ≤3（04 号 §2.2）
        code = _take_parcel_code()
        try:
            with _txn() as cur:
                cur.execute(
                    "INSERT INTO hsf_md.land_parcel(code,name,village_code,holder_id,area_mu,"
                    "soil_type,crop_type,geo_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (code, name, village_code, holder_id, area_mu, soil_type, crop_type, geojson))
                audit(cur, ctx, "register_parcel", "parcel", code, "OK")
                cur.execute("INSERT INTO hsf_biz.domain_event(topic,payload) VALUES(%s,%s)",
                            ("hsf.land.parcel.registered",
                             json.dumps({"parcel_code": code, "village_code": village_code},
                                        ensure_ascii=False)))
        except IntegrityError as e:
            if e.args and e.args[0] == 1062:
                continue  # 并发取号撞 uk_parcel_code → 重取号
            raise
        cache.invalidate_prefix(GEOJSON_PREFIX)  # 先 DB 提交 → 后删缓存（04 号 §8.4）
        cache.invalidate(parcel_key(code))
        return get(code)
    raise domain.BusinessError("CODE_DUPLICATED", "编码取号冲突，请重试")


# ───────── 缓存读（键 2/3，04 号 §8.2）─────────
def parcel_cached(code: str):
    return cache.cached(parcel_key(code), PARCEL_TTL, lambda: get(code), l1_ttl=L1_TTL)


def geojson_cached(village_code: str, scope_filters: dict) -> dict:
    """村级 FeatureCollection cache-aside；coop 子集加 holder 后缀防跨角色脏读（键 2 冻结键仅村维度）。"""
    key = geojson_key(village_code)
    if scope_filters.get("holder_id"):
        key = f"{key}:holder:{scope_filters['holder_id']}"
    return cache.cached(key, GEOJSON_TTL,
                        lambda: domain.feature_collection(geojson_rows(village_code, **scope_filters)),
                        l1_ttl=L1_TTL)


# ───────── 种子（幂等双守卫，CONVENTIONS §2.11）─────────
def seed_if_empty(retries: int = 30, interval: float = 4.0):
    """种子 8 地块（04 号 §2.3）。org/party 为 iam 所建，未就绪则等待重试（由 main 以 daemon
    线程调用，不阻塞探针——CONVENTIONS §2.14）；land_parcel 非空即跳过，防双副本并发重种。"""
    for _ in range(retries):
        if db.query("SELECT 1 FROM hsf_md.land_parcel LIMIT 1", one=True):
            return
        if db.query("SELECT 1 FROM hsf_md.org LIMIT 1", one=True) and \
                db.query("SELECT 1 FROM hsf_md.party LIMIT 1", one=True):
            break
        time.sleep(interval)
    else:
        return  # 基础数据始终未就绪：保留空库，随 iam 就绪后重启本副本补种
    if db.query("SELECT 1 FROM hsf_md.land_parcel LIMIT 1", one=True):
        return
    coop = db.query("SELECT id FROM hsf_md.party WHERE role='coop' ORDER BY id LIMIT 1", one=True)
    coop_id = coop["id"] if coop else None
    village_admins = {r["org_code"]: r["id"]
                      for r in db.query("SELECT id, org_code FROM hsf_md.party WHERE role='village'")}
    rows = []
    for i, (code, name, village, area, soil, crop, kind) in enumerate(SEED_PARCELS, 1):
        # 2×4 网格矩形（0.0015°×0.0012° ≈ 12 亩级），围绕 102.75E,25.05N（FR-103②）
        lng0 = 102.747 + ((i - 1) % 4) * 0.002
        lat0 = 25.0485 + ((i - 1) // 4) * 0.002
        holder = coop_id if kind == "coop" else village_admins.get(village)
        rows.append((code, name, village, holder, area, soil, crop,
                     domain.rect_geojson(lng0, lat0, 0.0015, 0.0012)))
    try:
        db.execute_many(
            "INSERT INTO hsf_md.land_parcel(code,name,village_code,holder_id,area_mu,"
            "soil_type,crop_type,geo_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    except IntegrityError:
        pass  # 双副本并发重种（1062）——已有种子即达标（CONVENTIONS §2.11）
