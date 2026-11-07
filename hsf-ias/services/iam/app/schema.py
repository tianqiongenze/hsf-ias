"""iam GraphQL Federation 子图 Schema（SDL 契约冻结于 hsf/docs/05 §3.1）。

联邦 types 显式注册 [Party, Organization]（CONVENTIONS §2.4：未注册实体不进 SDL，网关组合 400）；
Organization 为普通类型非 @key 实体（05 号 D-05-5：无跨子图引用方）。
"""
import strawberry

from hsf_common.crypto import mask_phone
from hsf_common.web import Exception429, issue_token, require_user

from . import repo


@strawberry.type
class Organization:
    """组织节点（普通类型：无跨子图引用方，@key 无必要——D-05-5）。"""
    code: str
    name: str
    level: str

    @strawberry.field
    def parent(self) -> "Organization | None":
        r = repo.org(self.code)
        if r and r.get("parent_code"):
            p = repo.org(r["parent_code"])
            return _org_node(p) if p else None
        return None

    @strawberry.field
    def children(self) -> list["Organization"]:
        return [_org_node(r) for r in repo.org_children(self.code)]


def _org_node(r) -> Organization:
    return Organization(code=r["code"], name=r["name"], level=r["level"])


@strawberry.federation.type(keys=["id"])
class Party:
    """平台账号主体（联邦实体，拥有者=iam；JWT sub 即 party.id）。"""
    id: strawberry.ID
    name: str
    role: str
    phone_masked: str
    org_code: str

    @strawberry.field
    def org(self) -> Organization | None:
        r = repo.org(self.org_code)
        return _org_node(r) if r else None

    @classmethod
    def resolve_reference(cls, id: strawberry.ID):
        r = repo.party(int(id))
        return _party_from_row(r) if r else None


def _party_from_row(r) -> Party:
    phone = r.get("phone") or ""
    return Party(id=str(r["id"]), name=r["name"], role=r["role"],
                 phone_masked=mask_phone(phone) if phone else "***",
                 org_code=r["org_code"])


@strawberry.type
class AuthPayload:
    token: str
    name: str
    roles: list[str]
    party_id: strawberry.ID


@strawberry.type
class PartyPage:
    total: int
    page: int
    size: int
    items: list[Party]


@strawberry.type
class AuditLog:
    """审计日志（只读投影；仅插入由各子图写路径落库，04 号 §4.8）。"""
    id: strawberry.ID
    user_id: str | None
    user_name: str | None
    action: str
    object_type: str | None
    object_id: str | None
    ip: str | None
    result: str
    created_at: str | None


@strawberry.type
class AuditLogPage:
    total: int
    page: int
    size: int
    items: list[AuditLog]


def _iso(dt) -> str | None:
    """DATETIME → ISO8601+08:00（D-05-6；库内为本地 +08:00 裸时间）。"""
    return f"{dt.isoformat()}+08:00" if dt else None


def _audit_from_row(r) -> AuditLog:
    return AuditLog(id=str(r["id"]),
                    user_id=str(r["user_id"]) if r.get("user_id") is not None else None,
                    user_name=r.get("user_name"), action=r["action"],
                    object_type=r.get("object_type"), object_id=r.get("object_id"),
                    ip=r.get("ip"), result=r["result"], created_at=_iso(r.get("created_at")))


def _ctx_party(info) -> Party | None:
    uid = info.context.get("user_id") or 0
    if not uid:
        return None  # 未登录 → null（V-20 断言口径）
    r = repo.party(uid)
    return _party_from_row(r) if r else None


def _scope_prefix(me, org_code: str | None):
    """parties 数据范围换算（05 号 §2.4）：admin 全县（可传参）；其余锁定本人组织（乡码 9 位/村码 11 位
    /主体码 12 位天然作前缀）；范围外交集 → 空集。返回 ''=不限、None=空集、str=LIKE 前缀。"""
    if me is None:
        return None
    if me["role"] == "admin":
        return org_code or ""
    uo = me["org_code"]
    if not org_code:
        return uo
    if org_code.startswith(uo) or uo.startswith(org_code):
        return org_code if len(org_code) >= len(uo) else uo
    return None


@strawberry.type
class Query:
    @strawberry.field
    def org_tree(self, info: strawberry.Info) -> list[Organization]:
        require_user(info)  # 任意登录（05 号 §3.2）
        return [Organization(code=n["code"], name=n["name"], level=n["level"])
                for n in repo.org_tree_cached()]

    @strawberry.field
    def party(self, info: strawberry.Info, id: strawberry.ID) -> Party | None:
        require_user(info)  # 联邦实体解析入口，登录即可（05 号 §3.2）
        return Party.resolve_reference(id)

    @strawberry.field
    def parties(self, info: strawberry.Info, org_code: str | None = None, page: int = 1, size: int = 20) -> PartyPage:
        ctx = require_user(info)
        size = min(size, repo.MAX_PAGE_SIZE)  # 超限静默取上限（§2.2）
        prefix = _scope_prefix(repo.party(ctx["user_id"]), org_code)
        total, rows = repo.parties_page(prefix, page, size)
        return PartyPage(total=total, page=max(page, 1), size=size,
                         items=[_party_from_row(r) for r in rows])

    @strawberry.field
    def me(self, info: strawberry.Info) -> Party | None:
        return _ctx_party(info)

    @strawberry.field
    def audit_logs(self, info: strawberry.Info, user_id: str | None = None, action: str | None = None,
                   time_from: str | None = None, time_to: str | None = None,
                   page: int = 1, size: int = 20) -> AuditLogPage:
        require_user(info, roles=["admin"])  # 仅 admin（FR-704，越权 FORBIDDEN——评审 R16）
        size = min(size, repo.MAX_PAGE_SIZE)
        try:
            total, rows = repo.audit_page(user_id, action, time_from, time_to, page, size)
        except ValueError as e:
            raise Exception429("VALIDATION", str(e))
        return AuditLogPage(total=total, page=max(page, 1), size=size,
                            items=[_audit_from_row(r) for r in rows])


@strawberry.type
class Mutation:
    @strawberry.mutation
    def login(self, info: strawberry.Info, phone: str, password: str) -> AuthPayload:
        r = repo.login_credentials(phone)
        if not r or repo.hash_password(password, r["password_salt"]) != r["password_hash"]:
            _audit_login(info, None, "login_fail", phone)  # 错密/停用账号同口径（03 号 DDD-A3）
            raise Exception429("UNAUTHENTICATED", "手机号或口令错误")
        _audit_login(info, r, "login", phone)
        roles = [r["role"]]
        return AuthPayload(token=issue_token(r["id"], r["name"], roles), name=r["name"],
                           roles=roles, party_id=str(r["id"]))


def _audit_login(info, r, action: str, phone: str):
    """登录审计（FR-704）：成功 login/OK，失败 login_fail/DENIED；手机号脱敏入 detail，口令不落日志。"""
    req = info.context.get("request")
    ip = req.client.host if (req and getattr(req, "client", None)) else None
    repo.audit(r["id"] if r else None, r["name"] if r else None, action,
               "OK" if r else "DENIED",
               object_type="party", object_id=str(r["id"]) if r else None,
               ip=ip, detail=f"phone={mask_phone(phone)}")


# §2.4 强制：显式 types 注册——未注册实体不进 SDL，网关组合直接 400（DV-IAS 真实生产事故）
schema = strawberry.federation.Schema(query=Query, mutation=Mutation,
                                      types=[Party, Organization], enable_federation_2=True)
