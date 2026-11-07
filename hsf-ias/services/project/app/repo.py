"""project 持久化：farm_project/project_parcel/project_transition DDL + 仓储 + 种子（幂等）。

同事务写 audit_log（04 号 §4.8）与 domain_event Outbox（04 号 §4.9，DDL 与 iam 冻结版一致，
CREATE IF NOT EXISTS 幂等兜底——各子图写各自消费的公共表）。
org/party（iam）与 land_parcel（land）仅跨库 SELECT 校验（ADR-HSF-004）。
"""
import json
import time
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor
from pymysql.err import IntegrityError

from hsf_common import cache, db

from . import domain
from .cache_keys import L1_TTL, PROJ_TTL, proj_key

DDL = [
    """CREATE TABLE IF NOT EXISTS hsf_biz.farm_project(
      id                 BIGINT       NOT NULL AUTO_INCREMENT,
      code               CHAR(20)     NOT NULL COMMENT '统一编码 P+6位区划+4位序号',
      name               VARCHAR(128) NOT NULL,
      build_year         SMALLINT     NOT NULL COMMENT '项目年度',
      content            TEXT         NULL COMMENT '建设内容',
      applicant_org_code CHAR(16)     NOT NULL COMMENT '申报单位 org.code（列宽对齐 org.code CHAR(16)——CONVENTIONS §2.3）',
      supervisor_id      BIGINT       NULL COMMENT '监理人 party.id（联邦引用 Party，FR-304）',
      state              VARCHAR(16)  NOT NULL DEFAULT 'RESERVED' COMMENT '7态：RESERVED/SUBMITTED/APPROVED/CONSTRUCTING/SUPERVISED/ACCEPTED/PERFORMED',
      acceptance_note    VARCHAR(512) NULL COMMENT '验收归档结论（acceptanceArchive 回填，FR-305）',
      created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at         DATETIME     NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_proj_code (code),
      KEY idx_state_year (state, build_year),
      KEY idx_applicant (applicant_org_code),
      KEY idx_supervisor (supervisor_id)
    ) COMMENT='高标准农田项目全周期（FR-301~305）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.project_parcel(
      project_code CHAR(20) NOT NULL COMMENT 'farm_project.code',
      parcel_code  CHAR(20) NOT NULL COMMENT 'land_parcel.code（跨库逻辑引用，hsf_md）',
      created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (project_code, parcel_code),
      KEY idx_parcel (parcel_code)
    ) COMMENT='项目-地块多对多（FR-301）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.project_transition(
      id         BIGINT       NOT NULL AUTO_INCREMENT,
      project_id BIGINT       NOT NULL COMMENT 'farm_project.id',
      action     VARCHAR(24)  NOT NULL COMMENT '冻结存储值=05号§5.2 mutation 名（评审 R13）',
      from_state VARCHAR(16)  NOT NULL,
      to_state   VARCHAR(16)  NOT NULL,
      actor_id   BIGINT       NOT NULL COMMENT '操作人 party.id',
      note       VARCHAR(255) NULL,
      created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      KEY idx_project (project_id, created_at)
    ) COMMENT='状态机留痕（FR-302：每次流转记录操作人与时间）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.audit_log(
      id          BIGINT       NOT NULL AUTO_INCREMENT,
      user_id     BIGINT       NULL COMMENT '匿名/失败登录为 NULL',
      user_name   VARCHAR(64)  NULL COMMENT '操作人姓名（快照，防后续改名不可追溯）',
      action      VARCHAR(64)  NOT NULL COMMENT 'login/login_fail/register_parcel/proj_transition/wo_transition/qr_scan…',
      object_type VARCHAR(32)  NULL COMMENT 'parcel/project/facility/workorder/alert',
      object_id   VARCHAR(64)  NULL COMMENT '业务编码或 id',
      ip          VARCHAR(45)  NULL COMMENT '客户端 IP（兼容 IPv6）',
      result      VARCHAR(16)  NOT NULL COMMENT 'OK/DENIED/ERROR（越权 403 亦记录，FR-704）',
      detail      VARCHAR(512) NULL,
      created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      KEY idx_user_time (user_id, created_at),
      KEY idx_action_time (action, created_at)
    ) COMMENT='审计日志：who/when/action/object/ip/result（FR-704）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.domain_event(
      id           BIGINT      NOT NULL AUTO_INCREMENT,
      topic        VARCHAR(64) NOT NULL COMMENT '事件主题 hsf.<ctx>.<entity>.<verb-past>（ADR-HSF-004）',
      payload      JSON        NOT NULL COMMENT '事件载荷 JSON',
      created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
      published_at DATETIME    NULL COMMENT '二期投递器置位；一期恒 NULL',
      PRIMARY KEY (id),
      KEY idx_topic_time (topic, created_at)
    ) COMMENT='领域事件 Outbox（ADR-HSF-004）'""",
]


def init_db():
    for ddl in DDL:
        db.execute(ddl)


@contextmanager
def _txn():
    """写池显式事务：业务 UPDATE+留痕+审计+Outbox 同事务（03 号 §6②）。"""
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
    cur.execute(
        "INSERT INTO hsf_biz.audit_log(user_id,user_name,action,object_type,object_id,ip,result,detail) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (ctx.get("user_id") or None, ctx.get("user_name") or None, action, object_type, object_id,
         _ip(ctx), result, detail))


def audit_denied(ctx: dict, action: str, object_type: str, object_id: str, detail: str | None = None):
    """越权/非法流转审计（result=DENIED；不落业务库、不留痕、只记审计——03 号 §4.3）。"""
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


def parcels_exist_active(codes: list) -> set:
    """校验关联地块存在且 ACTIVE（03 号 §4.3 不变量②），返回命中的编码集。"""
    if not codes:
        return set()
    marks = ",".join(["%s"] * len(codes))
    rows = db.query(f"SELECT code FROM hsf_md.land_parcel WHERE code IN ({marks}) AND status='ACTIVE'",
                    tuple(codes))
    return {r["code"] for r in rows}


# ───────── 查询（读池）─────────
def get(code: str):
    return db.query("SELECT * FROM hsf_biz.farm_project WHERE code=%s", (code,), one=True)


def proj_cached(code: str):
    """点查走缓存键 4（04 号 §8.2；createProject/任意流转后已失效回源）。"""
    return cache.cached(proj_key(code), PROJ_TTL, lambda: get(code), l1_ttl=L1_TTL)


def supervisor_id_of(code: str):
    r = db.query("SELECT supervisor_id FROM hsf_biz.farm_project WHERE code=%s", (code,), one=True)
    return r["supervisor_id"] if r else None


def parcels_of(code: str) -> list:
    return [r["parcel_code"] for r in
            db.query("SELECT parcel_code FROM hsf_biz.project_parcel WHERE project_code=%s "
                     "ORDER BY parcel_code", (code,))]


def list_projects(page: int, size: int, state=None, build_year=None, applicant_prefix=None):
    """分页（ORDER BY id DESC，05 号 §2.2）；town 范围=applicant_org_code 前缀 9 位。"""
    where, args = ["1=1"], []
    if state:
        where.append("state=%s")
        args.append(state)
    if build_year:
        where.append("build_year=%s")
        args.append(build_year)
    if applicant_prefix:
        where.append("applicant_org_code LIKE %s")
        args.append(applicant_prefix + "%")
    cond = "WHERE " + " AND ".join(where)
    total = db.query(f"SELECT COUNT(*) c FROM hsf_biz.farm_project {cond}", tuple(args), one=True)["c"]
    rows = db.query(f"SELECT * FROM hsf_biz.farm_project {cond} ORDER BY id DESC LIMIT %s OFFSET %s",
                    tuple(args) + (size, (page - 1) * size))
    return total, rows


def transitions(code: str) -> list:
    """流转留痕时间线（FR-302）。"""
    return db.query(
        "SELECT t.* FROM hsf_biz.project_transition t "
        "JOIN hsf_biz.farm_project p ON p.id=t.project_id WHERE p.code=%s ORDER BY t.id", (code,))


# ───────── 写路径（写池同事务）─────────
def _take_project_code() -> str:
    """写池事务外取号：MAX(RIGHT(code,4))+1（04 号 §2.2 冻结；uk_proj_code 兜底）。"""
    conn = db.write_pool.conn()
    with conn.cursor() as cur:
        cur.execute("SELECT IFNULL(MAX(CAST(RIGHT(code,4) AS UNSIGNED)),0)+1 FROM hsf_biz.farm_project "
                    "WHERE code LIKE %s", (domain.COUNTY_PREFIX + "%",))
        seq = int(cur.fetchone()[0] or 1)
    return domain.make_code(seq)


def create(name: str, build_year: int, content, applicant_org_code: str,
           supervisor_id, parcel_codes: list, ctx: dict) -> dict:
    """立项：校验→取号→同事务[INSERT+关联+audit+Outbox]（FR-301；初始 RESERVED）。"""
    if not org_exists(applicant_org_code):
        raise domain.BusinessError("VALIDATION", "申报单位组织不存在")
    if not domain.valid_build_year(build_year):
        raise domain.BusinessError("VALIDATION", "项目年度非法")
    if supervisor_id and not party_exists(int(supervisor_id)):
        raise domain.BusinessError("NOT_FOUND", "监理人不存在")
    parcels = list(dict.fromkeys(parcel_codes))  # 去重保序
    missing = set(parcels) - parcels_exist_active(parcels)
    if missing:
        raise domain.BusinessError("NOT_FOUND", f"关联地块不存在或已退役: {','.join(sorted(missing))}")

    for _ in range(3):  # 取号冲突重试 ≤3（04 号 §2.2）
        code = _take_project_code()
        try:
            with _txn() as cur:
                cur.execute(
                    "INSERT INTO hsf_biz.farm_project(code,name,build_year,content,"
                    "applicant_org_code,supervisor_id,state) VALUES(%s,%s,%s,%s,%s,%s,'RESERVED')",
                    (code, name, build_year, content, applicant_org_code,
                     int(supervisor_id) if supervisor_id else None))
                if parcels:
                    cur.executemany(
                        "INSERT INTO hsf_biz.project_parcel(project_code,parcel_code) VALUES(%s,%s)",
                        [(code, p) for p in parcels])
                audit(cur, ctx, "proj_create", "project", code, "OK")
                cur.execute("INSERT INTO hsf_biz.domain_event(topic,payload) VALUES(%s,%s)",
                            (domain.TOPIC_CREATED,
                             json.dumps({"project_code": code, "parcels": parcels},
                                        ensure_ascii=False)))
        except IntegrityError as e:
            if e.args and e.args[0] == 1062:
                continue  # 并发取号撞 uk_proj_code → 重取号
            raise
        cache.invalidate(proj_key(code))  # 先 DB 提交 → 后删缓存（04 号 §8.4/8.5）
        return get(code)
    raise domain.BusinessError("CODE_DUPLICATED", "编码取号冲突，请重试")


def transition(code: str, action: str, ctx: dict, note: str | None = None) -> dict:
    """状态机流转：行锁读→校验→同事务[UPDATE+留痕+audit+Outbox]→失效缓存（FR-302/303）。

    非法流转抛 INVALID_TRANSITION：事务回滚（不落库、不留痕），审计由上层记 DENIED。
    """
    with _txn() as cur:
        cur.execute("SELECT * FROM hsf_biz.farm_project WHERE code=%s FOR UPDATE", (code,))
        row = cur.fetchone()
        if not row:
            raise domain.BusinessError("NOT_FOUND", "项目不存在")
        new_state = domain.transition(row["state"], action)
        sets, args = ["state=%s", "updated_at=NOW()"], [new_state]
        if action == "acceptanceArchive":
            if not note or not note.strip():
                raise domain.BusinessError("VALIDATION", "验收结论不能为空")
            sets.append("acceptance_note=%s")
            args.append(note)
        args.append(code)
        cur.execute(f"UPDATE hsf_biz.farm_project SET {', '.join(sets)} WHERE code=%s", tuple(args))
        cur.execute(
            "INSERT INTO hsf_biz.project_transition(project_id,action,from_state,to_state,actor_id,note) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (row["id"], action, row["state"], new_state, ctx["user_id"], note))
        audit(cur, ctx, "proj_transition", "project", code, "OK", f"{row['state']}→{new_state}")
        cur.execute("INSERT INTO hsf_biz.domain_event(topic,payload) VALUES(%s,%s)",
                    (domain.EVENT_TOPICS[action],
                     json.dumps({"project_code": code, "from": row["state"], "to": new_state,
                                 "actor_id": ctx["user_id"]}, ensure_ascii=False)))
    cache.invalidate(proj_key(code))  # 先 DB 提交 → 后删缓存（04 号 §8.5：任意流转失效键 4）
    return get(code)


# ───────── 种子（幂等，依赖 iam org/party 与 land 地块先行）─────────
def _seed_base_ready() -> bool:
    return (bool(db.query("SELECT 1 FROM hsf_md.org LIMIT 1", one=True))
            and bool(db.query("SELECT 1 FROM hsf_md.party LIMIT 1", one=True))
            and bool(db.query("SELECT 1 FROM hsf_md.land_parcel LIMIT 1", one=True)))


def seed_if_empty(retries: int = 30, interval: float = 4.0):
    """种子 2 项目：P1 走完全周期到绩效（V-18 联邦跨域数据源），P2 储备态（E2E 流转起点）。

    以 main 的 daemon 线程调用（等待 iam/land 种子就绪，不阻塞探针——CONVENTIONS §2.14）；
    farm_project 非空即跳过；并发双副本撞 uk_proj_code 时让位退出。
    """
    for _ in range(retries):
        if db.query("SELECT 1 FROM hsf_biz.farm_project LIMIT 1", one=True):
            return
        if _seed_base_ready():
            break
        time.sleep(interval)
    else:
        return  # 基础数据始终未就绪：保留空库，重启本副本补种
    if db.query("SELECT 1 FROM hsf_biz.farm_project LIMIT 1", one=True):
        return
    admin = db.query("SELECT id FROM hsf_md.party WHERE role='admin' ORDER BY id LIMIT 1", one=True)
    keeper = db.query("SELECT id FROM hsf_md.party WHERE role='keeper' ORDER BY id LIMIT 1", one=True)
    if not admin:
        return
    ctx = {"user_id": admin["id"], "user_name": "系统种子", "user_roles": ["admin"], "request": None}
    try:
        p1 = create("绿野县2026年青禾乡高标准农田建设项目", 2026,
                    "高标准农田建设2000亩：土地平整、灌溉排水、田间道路",
                    "530600101", keeper["id"] if keeper else None,
                    ["L5306000001", "L5306000002"], ctx)
        for act in ("submitProject", "approveProject", "startConstruction", "startSupervision"):
            transition(p1["code"], act, ctx)
        transition(p1["code"], "acceptanceProject", ctx, "县级初验合格")
        transition(p1["code"], "acceptanceArchive", ctx, "验收合格，转入绩效评价与管护利用")
        create("绿野县2026年沃野乡高标准农田储备项目", 2026,
               "储备项目：待申报审批", "530600102", None, ["L5306000003", "L5306000004"], ctx)
    except (domain.BusinessError, IntegrityError):
        pass  # 双副本并发：他副本已种（取号冲突/状态已推进）即视为达标
