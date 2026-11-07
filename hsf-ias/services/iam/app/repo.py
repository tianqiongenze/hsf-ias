"""iam 持久化：hsf_md 建表 + hsf_biz.audit_log（审计落地）+ 仓储 + 种子（幂等双守卫）。

DDL 冻结于 hsf/docs/04 §3.1~3.3/§4.8；种子基线冻结于 04 号 §9.1（9 组织节点 + 15 演示账号）。
"""
import secrets

from hsf_common import db
from hsf_common.cache import cached, invalidate
from hsf_common.crypto import sm3, sm3_pbkdf2, sm4_decrypt, sm4_encrypt

ORG_TREE_KEY = "hsf:md:org:tree:v1"  # 缓存键 1（04 号 §8.2；v{n} 防多副本删旧键竞态，一期无组织变更接口固定 v1）

DEFAULT_PASSWORD = "hsf@2026"  # 种子口令（SM3-PBKDF2 派生存储，04 号 §9.1）

MAX_PAGE_SIZE = 100        # 顶层列表分页上限（05 号 §2.2）
DEEP_PAGE_LIMIT = 10000    # page*size 软约束，越界返回空页（05 号 §2.2）

DDL = [
    """CREATE TABLE IF NOT EXISTS hsf_md.org(
      code CHAR(16) NOT NULL COMMENT '组织编码：行政区划(6/9/11位)或主体码(12位)',
      name VARCHAR(64) NOT NULL COMMENT '组织名称',
      level VARCHAR(16) NOT NULL COMMENT 'COUNTY/TOWNSHIP/VILLAGE/KEEP_UNIT/COOP',
      parent_code CHAR(16) NULL COMMENT '父组织编码，根节点 NULL',
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (code),
      KEY idx_parent (parent_code)
    ) COMMENT='组织树：县-乡-村+管护主体/合作社（种子9节点，FR-701）'""",
    """CREATE TABLE IF NOT EXISTS hsf_md.party(
      id BIGINT NOT NULL AUTO_INCREMENT,
      name VARCHAR(64) NOT NULL COMMENT '姓名（明文，非敏感分级字段）',
      phone_hash CHAR(64) NOT NULL COMMENT 'SM3(手机号) hex，登录等值索引',
      phone_enc TEXT NOT NULL COMMENT 'SM4-GCM 密文（展示层脱敏 mask_phone）',
      org_code CHAR(16) NOT NULL COMMENT '所属组织 org.code',
      role VARCHAR(16) NOT NULL COMMENT 'admin/town/village/keeper/coop（五角色）',
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      UNIQUE KEY uk_phone (phone_hash),
      KEY idx_org (org_code)
    ) COMMENT='人员与账号主体（JWT sub 即本表 id，FR-701/702）'""",
    """CREATE TABLE IF NOT EXISTS hsf_md.account(
      id BIGINT NOT NULL AUTO_INCREMENT,
      party_id BIGINT NOT NULL COMMENT '1:1 party.id',
      password_salt CHAR(32) NOT NULL COMMENT '16字节随机 hex',
      password_hash CHAR(64) NOT NULL COMMENT 'SM3-PBKDF2(口令,salt,10000轮) hex',
      status VARCHAR(16) NOT NULL DEFAULT 'active' COMMENT 'active/locked（手动停用标志，一期无锁定接口）',
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      UNIQUE KEY uk_account_party (party_id)
    ) COMMENT='登录凭据（口令派生存储；种子口令 hsf@2026）'""",
    """CREATE TABLE IF NOT EXISTS hsf_biz.audit_log(
      id BIGINT NOT NULL AUTO_INCREMENT,
      user_id BIGINT NULL COMMENT '匿名/失败登录为 NULL',
      user_name VARCHAR(64) NULL COMMENT '操作人姓名快照',
      action VARCHAR(64) NOT NULL COMMENT 'login/login_fail/register_parcel/...',
      object_type VARCHAR(32) NULL COMMENT 'parcel/project/facility/workorder/alert',
      object_id VARCHAR(64) NULL COMMENT '业务编码或 id',
      ip VARCHAR(45) NULL COMMENT '客户端 IP（兼容 IPv6）',
      result VARCHAR(16) NOT NULL COMMENT 'OK/DENIED/ERROR',
      detail VARCHAR(512) NULL,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      KEY idx_user_time (user_id, created_at),
      KEY idx_action_time (action, created_at)
    ) COMMENT='审计日志：who/when/action/object/ip/result（FR-704，仅插入）'""",
]


def hash_password(password: str, salt_hex: str) -> str:
    """SM3-PBKDF2 口令派生（10000 轮，NFR-06）。"""
    return sm3_pbkdf2(password, salt_hex, 10000)


def init_db():
    """建表自检：幂等 DDL 全走写池（CONVENTIONS §2.10；建库/建账号由部署脚本执行，04 号 §3.0）。"""
    for ddl in DDL:
        db.execute(ddl)


# ───────── 审计（仅插入，FR-704）─────────
def audit(user_id, user_name, action: str, result: str,
          object_type=None, object_id=None, ip=None, detail=None):
    """审计落地（04 号 §4.8）：login/login_fail 等写 hsf_biz.audit_log，无 UPDATE/DELETE 路径。"""
    db.execute(
        "INSERT INTO hsf_biz.audit_log(user_id,user_name,action,object_type,object_id,ip,result,detail) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (user_id, user_name, action, object_type, object_id, ip, result, detail))


# ───────── 组织（缓存键 1）─────────
def org(code: str):
    return db.query("SELECT * FROM hsf_md.org WHERE code=%s", (code,), one=True)


def org_children(code: str):
    return db.query("SELECT * FROM hsf_md.org WHERE parent_code=%s ORDER BY code", (code,))


def org_tree_cached():
    """组织树（缓存键 1：L2 1800s / L1 60s，04 号 §8.2）。"""
    return cached(ORG_TREE_KEY, 1800, _load_org_tree, l1_ttl=60)


def _load_org_tree():
    rows = db.query("SELECT code,name,level,parent_code FROM hsf_md.org ORDER BY code")
    nodes = {r["code"]: {"code": r["code"], "name": r["name"], "level": r["level"],
                         "parent": r["parent_code"], "children": []} for r in rows}
    roots = []
    for n in nodes.values():
        p = nodes.get(n["parent"])
        (p["children"] if p else roots).append(n)
    return roots


# ───────── 人员与账号 ─────────
def party(pid: int):
    # 联邦热路径（holder/supervisor 等 @key 引用），走 L1+L2 缓存；
    # 失效点：account/party 写路径极少（注册/停用），演示口径 TTL 300s 可接受（docs/04 §8.3）
    from hsf_common.cache import cached
    def _load():
        r = db.query("SELECT p.*, a.id AS account_id FROM hsf_md.party p "
                     "LEFT JOIN hsf_md.account a ON a.party_id=p.id WHERE p.id=%s", (pid,), one=True)
        if r:
            r["phone"] = _decrypt_phone(r["phone_enc"])
        return r
    return cached("iam:party:%d" % pid, 300, _load) or None


def _decrypt_phone(enc: str) -> str:
    try:
        return sm4_decrypt(enc)
    except Exception:
        return ""  # 密文损坏不出明文，展示层降级为 ***


def login_credentials(phone: str):
    """登录凭据联查（phone_hash 等值索引 uk_phone）：停用（locked）账号同错密口径拒绝（03 号 DDD-A3）。"""
    return db.query(
        "SELECT p.id,p.name,p.role,p.org_code,a.password_salt,a.password_hash "
        "FROM hsf_md.party p JOIN hsf_md.account a ON a.party_id=p.id "
        "WHERE p.phone_hash=%s AND a.status='active'", (sm3(phone.encode()),), one=True)


def parties_page(prefix, page: int, size: int):
    """按组织前缀分页（数据范围 §2.4 由 schema 层换算 prefix：''=全县、None=空集、str=前缀过滤）。"""
    if prefix is None:
        return 0, []
    where, args = ("WHERE org_code LIKE %s", (prefix + "%",)) if prefix else ("", ())
    total = db.query(f"SELECT COUNT(*) AS c FROM hsf_md.party {where}", args, one=True)["c"]
    offset = (max(page, 1) - 1) * size
    rows = [] if offset >= DEEP_PAGE_LIMIT else db.query(
        f"SELECT id,name,role,org_code,phone_enc FROM hsf_md.party {where} "
        "ORDER BY id DESC LIMIT %s OFFSET %s", args + (size, offset))
    for r in rows:
        r["phone"] = _decrypt_phone(r.pop("phone_enc"))
    return total, rows


# ───────── 审计查询（仅 admin，FR-704）─────────
def audit_page(user_id, action, time_from, time_to, page: int, size: int):
    """审计过滤查询：按用户/动作/时间窗（读池直查，无缓存，05 号 §3.2）。"""
    where, args = [], []
    if user_id:
        where.append("user_id=%s")
        args.append(int(user_id))
    if action:
        where.append("action=%s")
        args.append(action)
    if time_from:
        where.append("created_at>=%s")
        args.append(_to_dt(time_from, "timeFrom"))
    if time_to:
        where.append("created_at<=%s")
        args.append(_to_dt(time_to, "timeTo"))
    cond = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.query(f"SELECT COUNT(*) AS c FROM hsf_biz.audit_log {cond}", tuple(args), one=True)["c"]
    offset = (max(page, 1) - 1) * size
    rows = [] if offset >= DEEP_PAGE_LIMIT else db.query(
        f"SELECT * FROM hsf_biz.audit_log {cond} ORDER BY id DESC LIMIT %s OFFSET %s",
        tuple(args) + (size, offset))
    return total, rows


def _to_dt(s: str, field: str) -> str:
    """ISO8601(+08:00) → DATETIME 可比较串（截 19 位，T→空格），非法即 VALIDATION。"""
    v = s.strip()[:19].replace("T", " ")
    if len(v) != 19 or v[4] != "-" or v[7] != "-" or v[10] != " " or v[13] != ":" or v[16] != ":":
        raise ValueError(f"{field} 需为 ISO8601 格式（如 2026-11-07T15:04:05+08:00）")
    return v


def create_party(name: str, phone: str, org_code: str, role: str, password: str | None = None):
    """建人+建账号（1:1 uk_account_party）：phone_hash=SM3 登录标识，phone_enc=SM4-GCM 密文（NFR-06）。"""
    salt = secrets.token_hex(16)
    pid = db.execute(
        "INSERT INTO hsf_md.party(name,phone_hash,phone_enc,org_code,role) VALUES(%s,%s,%s,%s,%s)",
        (name, sm3(phone.encode()), sm4_encrypt(phone), org_code, role))
    db.execute("INSERT INTO hsf_md.account(party_id,password_salt,password_hash) VALUES(%s,%s,%s)",
               (pid, salt, hash_password(password or DEFAULT_PASSWORD, salt)))
    return pid


# ───────── 种子数据（示例县：绿野县；04 号 §9.1 冻结基线）─────────
ORG_SEED = [  # (code, name, level, parent_code)：1 县+2 乡+4 村+管护主体+合作社，共 9 节点
    ("530600", "绿野县农业农村局", "COUNTY", None),
    ("530600101", "青禾乡", "TOWNSHIP", "530600"),
    ("530600102", "沃野乡", "TOWNSHIP", "530600"),
    ("53060010101", "清溪村", "VILLAGE", "530600101"),
    ("53060010102", "麦香村", "VILLAGE", "530600101"),
    ("53060010103", "柳林村", "VILLAGE", "530600101"),
    ("53060010201", "稻花村", "VILLAGE", "530600102"),
    ("530600900001", "农田管护队", "KEEP_UNIT", "530600"),
    ("530600910001", "绿野种植合作社", "COOP", "530600"),
]

# 演示账号 15 个：13800000601~610 + 13900000901~905，口令统一 hsf@2026（04 号 §9.1）
PARTY_SEED = [
    ("13800000601", "张县管", "530600", "admin"),
    ("13800000602", "青禾乡管", "530600101", "town"),
    ("13800000603", "沃野乡管", "530600102", "town"),
    ("13800000604", "清溪村管", "53060010101", "village"),
    ("13800000605", "麦香村管", "53060010102", "village"),
    ("13800000606", "柳林村管", "53060010103", "village"),
    ("13800000607", "稻花村管", "53060010201", "village"),
    ("13800000608", "管护队长", "530600900001", "keeper"),
    ("13800000609", "合作社社长", "530600910001", "coop"),
    ("13800000610", "县管备用一", "530600", "admin"),
    ("13900000901", "县管备用二", "530600", "admin"),
    ("13900000902", "乡管备用", "530600101", "town"),
    ("13900000903", "村管备用", "53060010101", "village"),
    ("13900000904", "管护备用", "530600900001", "keeper"),
    ("13900000905", "合作社备用", "530600910001", "coop"),
]


def seed_if_empty():
    """种子幂等双守卫（CONVENTIONS §2.11）：先查 org 再查 party，两表任一非空即跳过，防双副本并发重种（1062）。"""
    if db.query("SELECT 1 FROM hsf_md.org LIMIT 1", one=True):
        return
    if db.query("SELECT 1 FROM hsf_md.party LIMIT 1", one=True):
        return
    db.execute_many("INSERT INTO hsf_md.org(code,name,level,parent_code) VALUES(%s,%s,%s,%s)", ORG_SEED)
    for phone, name, org_code, role in PARTY_SEED:
        create_party(name, phone, org_code, role, DEFAULT_PASSWORD)
    invalidate(ORG_TREE_KEY)  # 组织变更 → 显式失效（04 号 §8.5）
