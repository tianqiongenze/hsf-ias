"""project GraphQL Federation 子图（SDL 冻结于 05 号 §5.1；转移表 §5.2；数据范围 §2.4）。"""
import enum

import strawberry

from hsf_common.web import Exception429, require_user

from . import domain, repo


def _iso(v) -> str | None:
    """DATETIME → ISO8601+08:00（05 号 §2.3 D-05-6；兼容缓存 JSON 回读的 str）。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if "T" in s else s.replace(" ", "T") + "+08:00"
    return v.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _normalize(r) -> dict:
    """行 → 类型安全 dict（Decimal/datetime 归一，与缓存 JSON 回读形态一致）。"""
    r = dict(r)
    r["created_at"] = _iso(r.get("created_at"))
    r["updated_at"] = _iso(r.get("updated_at"))
    return r


@strawberry.enum
class ProjectState(enum.Enum):
    """7 态（存储值=04 号 §4.1 冻结枚举，05 号 §5.1 SDL enum；成员名=SDL 枚举值）。"""

    RESERVED = "RESERVED"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    CONSTRUCTING = "CONSTRUCTING"
    SUPERVISED = "SUPERVISED"
    ACCEPTED = "ACCEPTED"
    PERFORMED = "PERFORMED"


@strawberry.federation.type(keys=["id"])
class Party:
    """跨子图实体桩（iam 拥有；FR-304 supervisor 联邦引用）。"""

    id: strawberry.ID

    @classmethod
    def resolve_reference(cls, id: strawberry.ID):
        return cls(id=id)


@strawberry.federation.type(keys=["code"])
class LandParcel:
    """跨子图实体桩（land 拥有；FR-301 parcels 联邦引用列表）。"""

    code: str

    @classmethod
    def resolve_reference(cls, code: str):
        return cls(code=code)


@strawberry.federation.type(keys=["code"])
class FarmProject:
    code: str
    name: str
    build_year: int
    content: str | None
    applicant_org_code: str
    state: ProjectState
    acceptance_note: str | None
    created_at: str | None
    updated_at: str | None

    @strawberry.field
    def supervisor(self) -> "Party | None":
        """监理人联邦桩（FR-304：gateway 经 iam 子图解析 name 等）。"""
        pid = repo.supervisor_id_of(self.code)
        return Party(id=str(pid)) if pid else None

    @strawberry.field
    def parcels(self) -> list[LandParcel]:
        """关联地块联邦桩列表（FR-301：gateway 经 land 子图解析详情）。"""
        return [LandParcel(code=c) for c in repo.parcels_of(self.code)]

    @classmethod
    def resolve_reference(cls, code: str):
        r = repo.proj_cached(code)
        return _to_type(_normalize(r)) if r else None


def _to_type(r: dict) -> FarmProject:
    return FarmProject(code=r["code"], name=r["name"], build_year=int(r["build_year"]),
                       content=r["content"], applicant_org_code=r["applicant_org_code"],
                       state=ProjectState(r["state"]), acceptance_note=r.get("acceptance_note"),
                       created_at=r["created_at"], updated_at=r["updated_at"])


@strawberry.type
class FarmProjectPage:
    total: int
    page: int
    size: int
    items: list[FarmProject]


@strawberry.type
class ProjectTransition:
    id: strawberry.ID
    action: str  # 冻结存储值=mutation 名（05 号 §5.2，评审 R13）
    from_state: ProjectState
    to_state: ProjectState
    actor_id: strawberry.ID
    note: str | None
    created_at: str | None


@strawberry.input
class CreateProjectInput:
    """必填字段在前（dataclass 风格：有默认值字段不得先于无默认值字段）。"""

    name: str
    build_year: int
    applicant_org_code: str
    parcel_codes: list[str]
    content: str | None = None
    supervisor_id: strawberry.ID | None = None


def _role_of(ctx: dict) -> str:
    for r in (ctx.get("user_roles") or []):
        if r in ("admin", "town", "village", "keeper", "coop"):
            return r
    return ""


def _town_guard(ctx: dict, row: dict):
    """town 写范围=本乡镇申报/流转（05 号 §2.4 写归属）；越权记 DENIED 审计。"""
    if _role_of(ctx) != "town":
        return
    org = repo.party_org(ctx["user_id"]) or ""
    if not org or not row["applicant_org_code"].startswith(org):
        repo.audit_denied(ctx, "proj_transition", "project", row["code"],
                          f"越权流转非本乡镇项目 {row['applicant_org_code']}")
        raise Exception429("FORBIDDEN", "无权限操作该乡镇以外的项目")


@strawberry.type
class Query:
    @strawberry.field
    def farm_project(self, info, code: str) -> "FarmProject | None":
        """项目详情（admin/town；其他角色空集——05 号 §2.4；缓存键 4）。"""
        ctx = require_user(info)
        if _role_of(ctx) not in ("admin", "town"):
            return None
        r = repo.proj_cached(code)
        return _to_type(_normalize(r)) if r else None

    @strawberry.field
    def farm_projects(self, info, state: "ProjectState | None" = None, build_year: int | None = None,
                      page: int = 1, size: int = 20) -> FarmProjectPage:
        """项目分页（admin 全县 / town 本乡镇前缀；village/keeper/coop 空集——05 号 §2.4）。"""
        ctx = require_user(info)
        page, size = max(page, 1), min(size, 100)
        role = _role_of(ctx)
        prefix = None
        if role == "town":
            prefix = repo.party_org(ctx["user_id"]) or ""
        elif role != "admin":
            return FarmProjectPage(total=0, page=page, size=size, items=[])
        total, rows = repo.list_projects(page, size, state.value if state else None,
                                         build_year, prefix)
        return FarmProjectPage(total=total, page=page, size=size,
                               items=[_to_type(_normalize(r)) for r in rows])

    @strawberry.field
    def project_transitions(self, info, code: str) -> list[ProjectTransition]:
        """流转留痕时间线（FR-302；admin/town，其他角色空集——05 号 §5.3）。"""
        ctx = require_user(info)
        if _role_of(ctx) not in ("admin", "town"):
            return []
        if not repo.get(code):
            raise Exception429("NOT_FOUND", "项目不存在")
        return [ProjectTransition(id=str(t["id"]), action=t["action"],
                                  from_state=ProjectState(t["from_state"]),
                                  to_state=ProjectState(t["to_state"]),
                                  actor_id=str(t["actor_id"]), note=t["note"],
                                  created_at=_iso(t["created_at"]))
                for t in repo.transitions(code)]


@strawberry.type
class Mutation:
    @strawberry.mutation
    def create_project(self, info, input: CreateProjectInput) -> FarmProject:
        """立项（admin/town）：生成 P 编码，初始 RESERVED（FR-301）。"""
        ctx = require_user(info, roles=list(domain.CREATE_ROLES))
        if _role_of(ctx) == "town":
            org = repo.party_org(ctx["user_id"]) or ""
            if not org or not input.applicant_org_code.startswith(org):
                repo.audit_denied(ctx, "proj_create", "project", input.applicant_org_code,
                                  "越权为非本乡镇单位立项")
                raise Exception429("FORBIDDEN", "无权限为该乡镇以外的申报单位立项")
        try:
            r = repo.create(input.name, input.build_year, input.content,
                            input.applicant_org_code, input.supervisor_id,
                            input.parcel_codes, ctx)
        except domain.BusinessError as e:
            raise Exception429(e.code, e.msg)
        return _to_type(_normalize(r))

    @strawberry.mutation
    def submit_project(self, info, code: str) -> FarmProject:
        """申报 RESERVED→SUBMITTED（town/admin）。"""
        return _apply(info, code, "submitProject")

    @strawberry.mutation
    def approve_project(self, info, code: str) -> FarmProject:
        """审批 SUBMITTED→APPROVED（admin）。"""
        return _apply(info, code, "approveProject")

    @strawberry.mutation
    def start_construction(self, info, code: str) -> FarmProject:
        """开工 APPROVED→CONSTRUCTING（admin/town）。"""
        return _apply(info, code, "startConstruction")

    @strawberry.mutation
    def start_supervision(self, info, code: str) -> FarmProject:
        """转监理 CONSTRUCTING→SUPERVISED（admin/town）。"""
        return _apply(info, code, "startSupervision")

    @strawberry.mutation
    def acceptance_project(self, info, code: str, note: str | None = None) -> FarmProject:
        """验收 SUPERVISED→ACCEPTED（admin；note 可选随留痕落库）。"""
        return _apply(info, code, "acceptanceProject", note)

    @strawberry.mutation
    def acceptance_archive(self, info, code: str, note: str) -> FarmProject:
        """验收归档 ACCEPTED→PERFORMED（admin；note 必填入 acceptance_note，FR-305）。"""
        return _apply(info, code, "acceptanceArchive", note)


def _apply(info, code: str, action: str, note: str | None = None) -> FarmProject:
    """流转公共通道：角色校验→town 范围→状态机→错误映射（INVALID_TRANSITION 记 DENIED 审计）。"""
    ctx = require_user(info, roles=list(domain.ACTION_ROLES[action]))
    try:
        row = repo.get(code)
        if not row:
            raise domain.BusinessError("NOT_FOUND", "项目不存在")
        _town_guard(ctx, row)
        return _to_type(_normalize(repo.transition(code, action, ctx, note)))
    except domain.BusinessError as e:
        if e.code == "INVALID_TRANSITION":  # 不落库、不留痕、只记审计（03 号 §4.3）
            repo.audit_denied(ctx, "proj_transition", "project", code,
                              f"{action} 非法流转: {e.msg}")
        raise Exception429(e.code, e.msg)


# 显式 types 列表：未注册实体不进 SDL，网关组合直接 400（CONVENTIONS §2.4）
schema = strawberry.federation.Schema(query=Query, mutation=Mutation,
                                      types=[FarmProject, Party, LandParcel],
                                      enable_federation_2=True)
