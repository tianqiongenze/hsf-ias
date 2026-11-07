"""facility 持久化：设施台账（hsf_md.facility）+ 工单 4 态状态机（hsf_biz.facility_workorder）。

对齐：hsf/docs/04 §3.5/§4.4~4.5（DDL 冻结）、§2.1~2.2（编码 F+区划+序号/取号重试/工单流水号）、
docs/03 §4.4（聚合与状态机）、docs/05 §6.2（转移表冻结）。
"""
import datetime as dt
import json
import os
import re
import secrets

from hsf_common import db
from hsf_common.cache import counter, invalidate, invalidate_prefix

# ── 业务规则（冻结口径，单测对象）───────────────────────────────────────────

# 工单 5 态转移矩阵（05 号 §6.2：accept→dispatch→resolve→approve；APPROVED 终态无出边；
# 非法流转一律拒绝且不落库 → INVALID_TRANSITION，5 态×4 动作矩阵单测全覆盖）
TRANSITIONS = {
    "OPEN":       {"accept": "ACCEPTED"},
    "ACCEPTED":   {"dispatch": "DISPATCHED"},
    "DISPATCHED": {"resolve": "RESOLVED"},
    "RESOLVED":   {"approve": "APPROVED"},
    "APPROVED":   {},  # 终态无出边（03 号 §4.4 不变量④）
}

# 设施类型/状态枚举（04 号 §3.5 冻结存储值）
FACILITY_TYPES = ("WELL", "PUMP_STATION", "CANAL", "ROAD")          # 机井/泵站/渠道/道路
FACILITY_STATUSES = ("NORMAL", "BROKEN", "REPAIRING", "RETIRED")

CODE_RE = re.compile(r"^F\d{10}$")  # 统一编码：F+6位区划+4位序号（04 号 §2.1）

# 二维码（03 号 §4.4 值对象 QrCode：内容=移动端 URL+token，FR-402 一期交付 token+URL）
WEB_BASE = os.environ.get("WEB_BASE", "http://10.167.2.175:31210")

# 种子演示账号（04 号 §9.1 次序：1 admin / 4~7 village / 8 keeper / 9 coop）
SEED_ADMIN_ID, SEED_VILLAGE_ID, SEED_KEEPER_ID = 1, 4, 8

# 村码 → 种子地块（04 号 §2.3：4 村各 2 块，L5306000001~08；软引用，不依赖 land 种子先建）
SEED_VILLAGES = (
    ("53060010101", "清溪村", ("L5306000001", "L5306000002")),
    ("53060010102", "麦香村", ("L5306000003", "L5306000004")),
    ("53060010103", "柳林村", ("L5306000005", "L5306000006")),
    ("53060010201", "稻花村", ("L5306000007", "L5306000008")),
)


class BizError(Exception):
    """业务校验错误：code ∈ 05 号 §2.1 错误码（schema 层转 GraphQL extensions）。"""

    def __init__(self, code: str, msg: str):
        self.code, self.msg = code, msg
        super().__init__(msg)


# ── DDL（04 号冻结；村码列宽按 CONVENTIONS §2.3"宁可宽不可窄"上调至 16，
#    static_gate 禁用 12 宽编码列；村码实长 11 位不受影响）────────────────────────
DDL = [
    """CREATE TABLE IF NOT EXISTS hsf_md.facility(
  id           BIGINT      NOT NULL AUTO_INCREMENT,
  code         CHAR(20)    NOT NULL COMMENT '统一编码 F+6位区划+4位序号',
  type         VARCHAR(16) NOT NULL COMMENT 'WELL(机井)/PUMP_STATION(泵站)/CANAL(渠道)/ROAD(道路)',
  name         VARCHAR(64) NOT NULL,
  village_code CHAR(16)    NOT NULL COMMENT '所属村',
  parcel_code  CHAR(20)    NULL COMMENT '所属地块 land_parcel.code（FR-401 外联）',
  build_year   SMALLINT    NULL COMMENT '建设年度',
  status       VARCHAR(16) NOT NULL DEFAULT 'NORMAL' COMMENT 'NORMAL/BROKEN/REPAIRING/RETIRED',
  qr_token     CHAR(32)    NULL COMMENT '扫码 token=secrets.token_hex(16)，注册时生成（FR-402）',
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME    NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_fac_code (code),
  UNIQUE KEY uk_qr_token (qr_token),
  KEY idx_village_type (village_code, type, status),
  KEY idx_parcel (parcel_code)
) COMMENT='设施台账：机井/泵站/渠道/道路 + 二维码（FR-401/402/405）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.facility_workorder(
  id            BIGINT       NOT NULL AUTO_INCREMENT,
  sn            VARCHAR(32)  NOT NULL COMMENT 'WO+yyyyMMdd+6位序号（幂等流水号）',
  facility_code CHAR(20)     NOT NULL COMMENT 'facility.code（跨库逻辑引用，hsf_md）',
  village_code  CHAR(16)     NOT NULL COMMENT '冗余设施所属村（范围过滤免联 hsf_md）',
  state         VARCHAR(16)  NOT NULL DEFAULT 'OPEN'
    COMMENT 'OPEN待受理/ACCEPTED已受理/DISPATCHED已派单/RESOLVED已办结/APPROVED已销项',
  description   TEXT         NOT NULL COMMENT '问题描述',
  contact_enc   TEXT         NULL COMMENT '联系方式 SM4-GCM 密文（FR-602；展示层 contactMasked）',
  reporter_id   BIGINT       NOT NULL COMMENT '上报人 party.id',
  assignee_id   BIGINT       NULL COMMENT '派单对象 keeper party.id',
  resolve_note  TEXT         NULL COMMENT '办结反馈（resolve 动作必填）',
  perf_score    INT          NULL COMMENT '绩效积分（一期可空不启用，FR-404）',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_wo_sn (sn),
  KEY idx_facility (facility_code, created_at),
  KEY idx_state_village (state, village_code, created_at),
  KEY idx_assignee (assignee_id, state),
  KEY idx_reporter (reporter_id)
) COMMENT='设施工单：受理→派单→办结→销项（FR-403/404）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.workorder_transition(
  id         BIGINT       NOT NULL AUTO_INCREMENT,
  wo_id      BIGINT       NOT NULL COMMENT 'facility_workorder.id',
  action     VARCHAR(16)  NOT NULL COMMENT '冻结存储值=05 号 §6.2 mutation 名',
  from_state VARCHAR(16)  NOT NULL,
  to_state   VARCHAR(16)  NOT NULL,
  actor_id   BIGINT       NOT NULL,
  note       VARCHAR(255) NULL,
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_wo (wo_id, created_at)
) COMMENT='工单留痕（FR-404：每态记录操作人/时间）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.domain_event(
  id           BIGINT      NOT NULL AUTO_INCREMENT,
  topic        VARCHAR(64) NOT NULL COMMENT '事件主题 hsf.<ctx>.<entity>.<verb-past>（ADR-HSF-004）',
  payload      JSON        NOT NULL COMMENT '事件载荷 JSON',
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  published_at DATETIME    NULL COMMENT '二期投递器置位；一期恒 NULL（一期无 broker）',
  PRIMARY KEY (id),
  KEY idx_topic_time (topic, created_at)
) COMMENT='领域事件 Outbox（写 mutation 追加 1 行，ADR-HSF-004）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.audit_log(
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  user_id     BIGINT       NULL COMMENT '匿名/失败登录为 NULL',
  user_name   VARCHAR(64)  NULL COMMENT '操作人姓名（快照）',
  action      VARCHAR(64)  NOT NULL COMMENT 'register_facility/wo_transition/qr_scan…',
  object_type VARCHAR(32)  NULL COMMENT 'parcel/project/facility/workorder/alert',
  object_id   VARCHAR(64)  NULL COMMENT '业务编码或 id',
  ip          VARCHAR(45)  NULL COMMENT '客户端 IP（兼容 IPv6）',
  result      VARCHAR(16)  NOT NULL COMMENT 'OK/DENIED/ERROR',
  detail      VARCHAR(512) NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_user_time (user_id, created_at),
  KEY idx_action_time (action, created_at)
) COMMENT='审计日志：只插入，应用层无 UPDATE/DELETE（FR-704）'""",
]


def init_db():
    for ddl in DDL:
        db.execute(ddl)


# ── 编码与二维码（04 号 §2.1/§2.2；03 号 §4.4 QrCode）───────────────────────

def make_code(region: str, seq: int) -> str:
    """F+6位区划+4位序号（11 位实长，CHAR(20) 列宽预留）。"""
    return f"F{region}{seq:04d}"


def next_facility_code(region: str = "530600") -> str:
    """取号：MAX(RIGHT(code,4))+1（前缀走 uk_fac_code 索引扫描，04 号 §2.2）。"""
    r = db.query("SELECT IFNULL(MAX(CAST(RIGHT(code,4) AS UNSIGNED)),0)+1 n "
                 "FROM hsf_md.facility WHERE code LIKE %s", (f"F{region}%",), one=True)
    return make_code(region, int(r["n"] or 1))


def new_qr_token() -> str:
    """32 位随机 hex（uk_qr_token 唯一约束兜底）。"""
    return secrets.token_hex(16)


def qr_url(token: str) -> str:
    """二维码内容 = 移动端 URL+token（03 号 §4.4：`<web>/m/facility?token=<t>`，FR-402）。"""
    return f"{WEB_BASE}/m/facility?token={token}"


# ── 设施台账 ───────────────────────────────────────────────────────────────

def _fac_row(code: str):
    return db.query("SELECT * FROM hsf_md.facility WHERE code=%s", (code,), one=True)


def facility(code: str):
    """设施点查（读键 5：hsf:fac:{code}，300s）。"""
    return cached_json(f"hsf:fac:{code}", 300, lambda: _fac_row(code), 60)


def facility_by_token(token: str):
    """扫码反查（读键 6：hsf:fac:qr:{token}，600s；token 失效返回 None → NOT_FOUND）。"""
    if not token:
        return None
    return cached_json(f"hsf:fac:qr:{token}", 600,
                       lambda: db.query("SELECT * FROM hsf_md.facility WHERE qr_token=%s",
                                        (token,), one=True), 120)


def register_facility(type_: str, name: str, village_code: str,
                      parcel_code: str | None = None, build_year: int | None = None) -> dict:
    """登记设施：校验村/地块存在 → 取号（撞 uk 重试 ≤3）→ 生成 qr_token（05 号 §6.3/04 号 §2.2）。"""
    if type_ not in FACILITY_TYPES:
        raise BizError("VALIDATION", f"设施类型非法：{type_}")
    if not (name or "").strip():
        raise BizError("VALIDATION", "设施名称必填")
    org = db.query("SELECT code FROM hsf_md.org WHERE code=%s AND level='VILLAGE'",
                   (village_code,), one=True)
    if not org:
        raise BizError("NOT_FOUND", "所属村不存在")
    if parcel_code and not db.query("SELECT 1 FROM hsf_md.land_parcel WHERE code=%s",
                                    (parcel_code,), one=True):
        raise BizError("NOT_FOUND", "关联地块不存在")
    region = village_code[:6] or "530600"  # 行政区划固定取县级 6 位码（04 号 §2.1）
    last_err = None
    for _ in range(3):  # 撞 uk_fac_code 重试 ≤3（04 号 §2.2 取号约定）
        code = next_facility_code(region)
        try:
            db.execute(
                "INSERT INTO hsf_md.facility(code,type,name,village_code,parcel_code,"
                "build_year,status,qr_token) VALUES(%s,%s,%s,%s,%s,%s,'NORMAL',%s)",
                (code, type_, name.strip(), village_code, parcel_code, build_year, new_qr_token()))
            invalidate(f"hsf:fac:{code}")
            return _fac_row(code)
        except Exception as e:  # 并发取号撞唯一键 → 重取
            if "1062" not in str(e) and "Duplicate" not in str(e):
                raise
            last_err = e
    raise BizError("CODE_DUPLICATED", f"设施编码取号重试失败：{last_err}")


def facilities(type_: str | None, village_code: str | None, status: str | None,
               scope: tuple[str, tuple], page: int = 1, size: int = 20) -> dict:
    """台账组合过滤（FR-405：类型/村/状态任选）+ 数据范围（05 号 §2.4）。"""
    where, args = ["1=1"], []
    if type_:
        where.append("type=%s"); args.append(type_)
    if village_code:
        where.append("village_code LIKE %s"); args.append(village_code + "%")
    if status:
        where.append("status=%s"); args.append(status)
    scope_sql, scope_args = scope
    where.append(scope_sql); args.extend(scope_args)
    cond = " AND ".join(where)
    args_tuple = tuple(args)
    total = int(db.query(f"SELECT COUNT(*) c FROM hsf_md.facility WHERE {cond}",
                         args_tuple, one=True)["c"])
    rows = db.query(f"SELECT * FROM hsf_md.facility WHERE {cond} ORDER BY id DESC LIMIT %s OFFSET %s",
                    args_tuple + (size, (max(page, 1) - 1) * size))
    return {"total": total, "rows": rows}


def party_org(user_id: int):
    """数据范围依据：操作人所属组织与角色（hsf_md.party，05 号 §2.4）。"""
    if not user_id:
        return None
    return db.query("SELECT org_code, role FROM hsf_md.party WHERE id=%s", (user_id,), one=True)


# ── 工单（4 态状态机，05 号 §6.2 转移表 + 03 号 §4.4 不变量）──────────────────

def next_sn() -> str:
    """WO+yyyyMMdd+6位流水：Redis INCR hsf:seq:workorder（键 11），降级时间戳片段。"""
    seq = counter("hsf:seq:workorder", 1)
    if seq < 0:  # Redis 不可用降级（DV-IAS governance 已验证模式）
        seq = int(dt.datetime.now().strftime("%H%M%S%f")[:8])
    return f"WO{dt.date.today():%Y%m%d}{seq:06d}"


def workorder(wo_id: int):
    return db.query("SELECT * FROM hsf_biz.facility_workorder WHERE id=%s", (wo_id,), one=True)


def workorder_transitions(wo_id: int):
    return db.query("SELECT * FROM hsf_biz.workorder_transition WHERE wo_id=%s ORDER BY id", (wo_id,))


def workorders_of_facility(facility_code: str, page: int = 1, size: int = 20) -> dict:
    """本设施工单（嵌套分页，size≤50 由 schema 层收紧）。"""
    total = int(db.query("SELECT COUNT(*) c FROM hsf_biz.facility_workorder WHERE facility_code=%s",
                         (facility_code,), one=True)["c"])
    rows = db.query("SELECT * FROM hsf_biz.facility_workorder WHERE facility_code=%s "
                    "ORDER BY id DESC LIMIT %s OFFSET %s",
                    (facility_code, size, (max(page, 1) - 1) * size))
    return {"total": total, "rows": rows}


def workorders(state: str | None, village_code: str | None, facility_code: str | None,
               scope: tuple[str, tuple], page: int = 1, size: int = 20) -> dict:
    """工单列表（不走缓存：写多读少+留痕即时性，05 号 §6.3）。"""
    where, args = ["1=1"], []
    if state:
        where.append("state=%s"); args.append(state)
    if village_code:
        where.append("village_code LIKE %s"); args.append(village_code + "%")
    if facility_code:
        where.append("facility_code=%s"); args.append(facility_code)
    scope_sql, scope_args = scope
    where.append(scope_sql); args.extend(scope_args)
    cond = " AND ".join(where)
    args_tuple = tuple(args)
    total = int(db.query(f"SELECT COUNT(*) c FROM hsf_biz.facility_workorder WHERE {cond}",
                         args_tuple, one=True)["c"])
    rows = db.query(f"SELECT * FROM hsf_biz.facility_workorder WHERE {cond} "
                    "ORDER BY id DESC LIMIT %s OFFSET %s",
                    args_tuple + (size, (max(page, 1) - 1) * size))
    return {"total": total, "rows": rows}


def report_workorder(facility_code: str, description: str,
                     contact: str | None, reporter_id: int) -> dict:
    """上报工单 → OPEN（不变量①：设施存在且非 RETIRED；contact SM4-GCM 密文落库）。"""
    fac = _fac_row(facility_code)
    if not fac:
        raise BizError("NOT_FOUND", "设施不存在")
    if fac["status"] == "RETIRED":
        raise BizError("VALIDATION", "设施已退役，不能上报工单")
    if not (description or "").strip():
        raise BizError("VALIDATION", "问题描述必填")
    contact_enc = None
    if contact:
        from hsf_common.crypto import sm4_encrypt
        contact_enc = sm4_encrypt(contact)
    wo_id = db.execute(
        "INSERT INTO hsf_biz.facility_workorder(sn,facility_code,village_code,state,"
        "description,contact_enc,reporter_id) VALUES(%s,%s,%s,'OPEN',%s,%s,%s)",
        (next_sn(), facility_code, fac["village_code"], description.strip(), contact_enc, reporter_id))
    db.execute("INSERT INTO hsf_biz.workorder_transition(wo_id,action,from_state,to_state,actor_id) "
               "VALUES(%s,'report','OPEN','OPEN',%s)", (wo_id, reporter_id))
    _invalidate_workorder(fac["village_code"])
    return workorder(wo_id)


def transition(wo_id: int, action: str, actor_id: int,
               note: str = "", assignee_id: int | None = None) -> dict:
    """聚合行为：状态机校验 → 写主库+留痕（同线程顺序保证；非法流转不落库）。"""
    wo = workorder(wo_id)
    if not wo:
        raise BizError("NOT_FOUND", "工单不存在")
    allowed = TRANSITIONS.get(wo["state"], {})
    if action not in allowed:
        raise BizError("INVALID_TRANSITION", f"工单处于 {wo['state']} 状态，不允许 {action}")
    if action == "resolve" and not (note or "").strip():  # 不变量③：办结反馈必填
        raise BizError("VALIDATION", "办结必须填写反馈内容")
    if action == "dispatch":  # 不变量②：派单对象必须是管护员
        p = db.query("SELECT role FROM hsf_md.party WHERE id=%s", (assignee_id or 0,), one=True)
        if not p or p["role"] != "keeper":
            raise BizError("VALIDATION", "派单对象必须是管护员(keeper)")
    new_state = allowed[action]
    new_assignee = assignee_id if action == "dispatch" else wo["assignee_id"]
    if action == "resolve":
        db.execute("UPDATE hsf_biz.facility_workorder SET state=%s, resolve_note=%s, "
                   "updated_at=NOW() WHERE id=%s", (new_state, note.strip(), wo_id))
    else:
        db.execute("UPDATE hsf_biz.facility_workorder SET state=%s, assignee_id=%s, "
                   "updated_at=NOW() WHERE id=%s", (new_state, new_assignee, wo_id))
    db.execute("INSERT INTO hsf_biz.workorder_transition(wo_id,action,from_state,to_state,actor_id,note) "
               "VALUES(%s,%s,%s,%s,%s,%s)",
               (wo_id, action, wo["state"], new_state, actor_id, note or None))
    _invalidate_workorder(wo["village_code"])
    return workorder(wo_id)


def _invalidate_workorder(village_code: str):
    """写路径失效：键 5（设施点查前缀）+ 键 9（村工单统计前缀）。"""
    invalidate_prefix("hsf:stat:workorder:")
    invalidate_prefix("hsf:fac:")


def audit(user_id, user_name: str, action: str, object_type: str,
          object_id: str, result: str = "OK", detail: str = "", ip: str = ""):
    """审计只插入（FR-704；无 UPDATE/DELETE 路径）。"""
    db.execute("INSERT INTO hsf_biz.audit_log(user_id,user_name,action,object_type,object_id,"
               "ip,result,detail) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
               (user_id, user_name, action, object_type, str(object_id)[:64], ip, result,
                detail[:512] or None))


def emit(topic: str, payload: dict):
    """领域事件 Outbox（ADR-HSF-004：写 mutation 追加 1 行；一期不投递）。"""
    db.execute("INSERT INTO hsf_biz.domain_event(topic,payload) VALUES(%s,%s)",
               (topic, json.dumps(payload, ensure_ascii=False, default=str)))


# ── 缓存（读键 5/6；值含 datetime → 走 cached 的 default=str 序列化）────────────

def cached_json(key: str, ttl: float, loader, l1_ttl: float):
    from hsf_common.cache import cached
    return cached(key, ttl, loader, l1_ttl=l1_ttl)


# ── 种子（04 号 §9.1：设施 12 = 机井4/泵站2/渠道3/道路3，分属 4 村；工单 5 单覆盖各态）──

def seed_if_empty():
    if not db.query("SELECT 1 FROM hsf_md.facility LIMIT 1", one=True):
        _seed_facilities()
    # 工单种子依赖 party（keeper 校验/上报人），party 未就绪则跳过待下次启动重试
    if not db.query("SELECT 1 FROM hsf_md.party LIMIT 1", one=True):
        return
    if not db.query("SELECT 1 FROM hsf_biz.facility_workorder LIMIT 1", one=True):
        _seed_workorders()


def _seed_facilities():
    """INSERT IGNORE（uk_fac_code）幂等：双守卫防双副本并发重种（CONVENTIONS §2.11）。"""
    rows = []  # (type, 名称, 村序, parcel位序 or None, build_year, status)
    plan = [
        ("WELL", "1号机井", 0, 0, 2021, "NORMAL"),
        ("PUMP_STATION", "排灌泵站", 0, None, 2022, "NORMAL"),
        ("ROAD", "田间道", 0, None, 2023, "NORMAL"),
        ("WELL", "1号机井", 1, 0, 2021, "BROKEN"),
        ("PUMP_STATION", "排灌泵站", 1, None, 2022, "NORMAL"),
        ("CANAL", "斗渠", 1, 1, 2022, "NORMAL"),
        ("WELL", "1号机井", 2, 0, 2021, "NORMAL"),
        ("CANAL", "斗渠", 2, 1, 2023, "NORMAL"),
        ("ROAD", "田间道", 2, None, 2024, "NORMAL"),
        ("WELL", "1号机井", 3, 0, 2022, "NORMAL"),
        ("CANAL", "斗渠", 3, 1, 2023, "NORMAL"),
        ("ROAD", "田间道", 3, None, 2024, "NORMAL"),
    ]
    seq = 1
    for type_, base_name, v_idx, p_idx, year, status in plan:
        village_code, village_name, parcels = SEED_VILLAGES[v_idx]
        rows.append((make_code("530600", seq), type_, f"{village_name}{base_name}", village_code,
                     parcels[p_idx] if p_idx is not None else None, year, status,
                     new_qr_token()))
        seq += 1
    db.execute_many(
        "INSERT IGNORE INTO hsf_md.facility(code,type,name,village_code,parcel_code,"
        "build_year,status,qr_token) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)", rows)


def _seed_workorders():
    """5 单覆盖 OPEN/ACCEPTED/DISPATCHED/RESOLVED/APPROVED 全态（04 号 §9.1）。"""
    facs = {r["code"]: r for r in db.query("SELECT code,village_code,status FROM hsf_md.facility")}
    from hsf_common.crypto import sm4_encrypt
    desc = "机井水泵无法启动，请安排检修"
    contact = sm4_encrypt("13800000604")
    plan = [  # (facility_code, 目标态)
        ("F5306000002", "OPEN"),
        ("F5306000005", "ACCEPTED"),
        ("F5306000007", "DISPATCHED"),
        ("F5306000008", "RESOLVED"),
        ("F5306000010", "APPROVED"),
    ]
    for i, (code, target) in enumerate(plan):
        fac = facs.get(code)
        if not fac:
            continue
        wo_id = db.execute(
            "INSERT INTO hsf_biz.facility_workorder(sn,facility_code,village_code,state,"
            "description,contact_enc,reporter_id) VALUES(%s,%s,%s,'OPEN',%s,%s,%s)",
            (next_sn(), code, fac["village_code"], f"种子工单{i + 1}：{desc}", contact,
             SEED_VILLAGE_ID))
        chain = {"ACCEPTED": ["accept"], "DISPATCHED": ["accept", "dispatch"],
                 "RESOLVED": ["accept", "dispatch", "resolve"], "APPROVED": ["accept", "dispatch", "resolve", "approve"]}
        for act in chain.get(target, []):
            if act == "dispatch":
                transition(wo_id, act, SEED_ADMIN_ID, assignee_id=SEED_KEEPER_ID)
            elif act == "resolve":
                transition(wo_id, act, SEED_KEEPER_ID, note="已更换水泵密封圈并试水正常")
            else:
                transition(wo_id, act, SEED_ADMIN_ID)
