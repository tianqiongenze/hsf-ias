"""facility GraphQL Federation 子图（SDL 契约 = hsf/docs/05 §6.1 冻结）。"""
import enum

import strawberry

from hsf_common.crypto import mask_phone, sm4_decrypt
from hsf_common.web import Exception429, require_user

from . import repo


# ── 枚举（05 号 §6.1 冻结存储值）────────────────────────────────────────────

@strawberry.enum
class FacilityType(enum.Enum):
    WELL = "WELL"                  # 机井
    PUMP_STATION = "PUMP_STATION"  # 泵站
    CANAL = "CANAL"                # 渠道
    ROAD = "ROAD"                  # 道路


@strawberry.enum
class FacilityStatus(enum.Enum):
    NORMAL = "NORMAL"
    BROKEN = "BROKEN"
    REPAIRING = "REPAIRING"
    RETIRED = "RETIRED"


@strawberry.enum
class WorkorderState(enum.Enum):
    OPEN = "OPEN"
    ACCEPTED = "ACCEPTED"
    DISPATCHED = "DISPATCHED"
    RESOLVED = "RESOLVED"
    APPROVED = "APPROVED"


# ── 联邦桩（iam / land 拥有同名实体，本子图仅持 key）────────────────────────

@strawberry.federation.type(keys=["id"])
class Party:
    id: strawberry.ID

    @classmethod
    def resolve_reference(cls, id: strawberry.ID):
        return cls(id=id)


@strawberry.federation.type(keys=["code"])
class LandParcel:
    code: str

    @classmethod
    def resolve_reference(cls, code: str):
        return cls(code=code)


# ── 实体与非实体类型 ───────────────────────────────────────────────────────

def _fac_type(r, with_token: bool = False) -> "Facility":
    """行 → Facility。qrToken 仅 facilityByToken/点查路径下发，不随台账列表（05 号 §6.1）。"""
    return Facility(code=r["code"], type=FacilityType(r["type"]), name=r["name"],
                    village_code=r["village_code"], build_year=r["build_year"],
                    status=FacilityStatus(r["status"]),
                    parcel_code=r["parcel_code"], qr_token=r["qr_token"] if with_token else None,
                    created_at=str(r["created_at"]) if r["created_at"] else None)


@strawberry.federation.type(keys=["code"])
class Facility:
    code: str
    type: FacilityType
    name: str
    village_code: str
    build_year: int | None
    status: FacilityStatus
    created_at: str | None
    parcel_code: str | None = None
    qr_token: str | None = None  # 扫码 token；仅 byToken/点查路径赋值（不随列表下发）

    @strawberry.field
    def parcel(self) -> LandParcel | None:
        """所属地块（联邦桩 → land.LandParcel，FR-401②）。"""
        return LandParcel(code=self.parcel_code) if self.parcel_code else None

    @strawberry.field
    def qr_url(self) -> str | None:
        """二维码内容 = 移动端 URL+token（FR-402 一期交付口径，URL 模板见 03 号 §4.4）。"""
        return repo.qr_url(self.qr_token) if self.qr_token else None

    @strawberry.field
    def workorders(self, page: int = 1, size: int = 20) -> "WorkorderPage":
        size = min(size, 50)  # 嵌套列表上限（05 号 §2.2）
        d = repo.workorders_of_facility(self.code, page, size)
        return WorkorderPage(total=d["total"], page=page, size=size,
                             items=[_wo_type(r) for r in d["rows"]])

    @classmethod
    def resolve_reference(cls, code: str):
        r = repo.facility(code)
        return _fac_type(r) if r else None


def _wo_type(r) -> "Workorder":
    contact_masked = None
    if r["contact_enc"]:
        try:
            contact_masked = mask_phone(sm4_decrypt(r["contact_enc"]))
        except Exception:
            contact_masked = "***"  # 密文不可解（密钥轮换）时不得泄明文
    return Workorder(
        id=str(r["id"]), sn=r["sn"], facility_code=r["facility_code"],
        state=WorkorderState(r["state"]), description=r["description"],
        contact_masked=contact_masked, reporter_id=str(r["reporter_id"]),
        assignee_id=str(r["assignee_id"]) if r["assignee_id"] else None,
        resolve_note=r["resolve_note"], perf_score=r["perf_score"],  # 一期恒 null（FR-404）
        created_at=str(r["created_at"]) if r["created_at"] else None,
        updated_at=str(r["updated_at"]) if r["updated_at"] else None)


@strawberry.type
class Workorder:
    id: strawberry.ID
    sn: str
    state: WorkorderState
    description: str | None
    contact_masked: str | None
    reporter_id: strawberry.ID
    assignee_id: strawberry.ID | None
    resolve_note: str | None
    perf_score: int | None
    created_at: str | None
    updated_at: str | None
    facility_code: str | None = None

    @strawberry.field
    def facility(self) -> Facility | None:
        r = repo.facility(self.facility_code) if self.facility_code else None
        return _fac_type(r) if r else None

    @strawberry.field
    def reporter(self) -> Party:
        """上报人（联邦桩 → iam.Party，基础字段由 iam 解析）。"""
        return Party(id=self.reporter_id)

    @strawberry.field
    def assignee(self) -> Party | None:
        return Party(id=self.assignee_id) if self.assignee_id else None

    @strawberry.field
    def transitions(self) -> list["WorkorderTransition"]:
        return [WorkorderTransition(id=str(t["id"]), action=t["action"],
                                    from_state=WorkorderState(t["from_state"]),
                                    to_state=WorkorderState(t["to_state"]),
                                    actor_id=str(t["actor_id"]), note=t["note"],
                                    created_at=str(t["created_at"]) if t["created_at"] else None)
                for t in repo.workorder_transitions(int(self.id))]


@strawberry.type
class WorkorderTransition:
    id: strawberry.ID
    action: str  # 冻结存储值=05 号 §6.2 mutation 名
    from_state: WorkorderState
    to_state: WorkorderState
    actor_id: strawberry.ID
    note: str | None
    created_at: str | None


@strawberry.type
class FacilityPage:
    total: int
    page: int
    size: int
    items: list[Facility]


@strawberry.type
class WorkorderPage:
    total: int
    page: int
    size: int
    items: list[Workorder]


@strawberry.input
class RegisterFacilityInput:
    type: FacilityType
    name: str
    village_code: str
    parcel_code: str | None = None
    build_year: int | None = None


# ── 数据范围（05 号 §2.4：查询越权→空集，写越权→FORBIDDEN）───────────────────

def _scope(ctx, kind: str) -> tuple[str, tuple]:
    """返回 (where 片段, args)。admin 全县 / town 乡内村集 / village 本村 /
    keeper 所辖（工单=assignee=me；设施=其工单关联设施）/ coop 上报维度。"""
    roles = set(ctx.get("user_roles") or [])
    uid = ctx.get("user_id") or 0
    if "admin" in roles:
        return "1=1", ()
    p = repo.party_org(uid)
    if "town" in roles:
        if p:
            return "village_code LIKE %s", (p["org_code"][:9] + "%",)
    elif "village" in roles:
        if p:
            return "village_code = %s", (p["org_code"],)
    elif "keeper" in roles:
        if kind == "workorder":
            return "assignee_id = %s", (uid,)
        return ("code IN (SELECT facility_code FROM hsf_biz.facility_workorder "
                "WHERE assignee_id=%s)", (uid,))
    elif "coop" in roles:
        if kind == "workorder":
            return "reporter_id = %s", (uid,)
        return ("parcel_code IN (SELECT code FROM hsf_md.land_parcel WHERE holder_id=%s)",
                (uid,))
    return "1=0", ()  # 未登录/未知角色/组织缺失 → 空集


# 领域事件名（03 号 §4.4 事件字典）：action → 过去分词
_EVENT_PAST = {"accept": "accepted", "dispatch": "dispatched",
               "resolve": "resolved", "approve": "approved"}


def _apply(eid: int, action: str, ctx, note: str = "", assignee_id: int | None = None):
    try:
        r = repo.transition(eid, action, ctx["user_id"], note, assignee_id)
    except repo.BizError as e:
        raise Exception429(e.code, e.msg)
    _audit(ctx, "wo_transition", "workorder", r["id"],
           detail=f"{action}:{r['state']}")
    repo.emit(f"hsf.facility.workorder.{_EVENT_PAST[action]}",
              {"wo_id": int(r["id"]), "sn": r["sn"], "state": r["state"],
               "actor_id": ctx["user_id"], "facility_code": r["facility_code"]})
    return _wo_type(r)


def _audit(ctx, action: str, object_type: str, object_id, result: str = "OK", detail: str = ""):
    req = ctx.get("request")
    ip = req.client.host if (req and req.client) else ""
    repo.audit(ctx.get("user_id"), ctx.get("user_name") or "", action, object_type,
               object_id, result, detail, ip)


# ── Query / Mutation（05 号 §6.1 冻结）──────────────────────────────────────

@strawberry.type
class Query:
    @strawberry.field
    def facility(self, code: str) -> Facility | None:
        r = repo.facility(code)
        return _fac_type(r, with_token=True) if r else None  # 点查下发 token（详情/复制张贴）

    @strawberry.field
    def facilities(self, info, type: FacilityType | None = None,
                   village_code: str | None = None, status: FacilityStatus | None = None,
                   page: int = 1, size: int = 20) -> FacilityPage:
        require_user(info)  # 登录（05 号 §6.3）
        ctx = info.context
        size = min(size, 100)  # 顶层列表上限
        d = repo.facilities(type.value if type else None, village_code,
                            status.value if status else None,
                            _scope(ctx, "facility"), page, size)
        return FacilityPage(total=d["total"], page=page, size=size,
                            items=[_fac_type(r) for r in d["rows"]])  # 列表不带 token

    @strawberry.field
    def workorders(self, info, state: WorkorderState | None = None,
                   village_code: str | None = None, facility_code: str | None = None,
                   page: int = 1, size: int = 20) -> WorkorderPage:
        ctx = require_user(info)
        size = min(size, 100)
        d = repo.workorders(state.value if state else None, village_code, facility_code,
                            _scope(ctx, "workorder"), page, size)
        return WorkorderPage(total=d["total"], page=page, size=size,
                             items=[_wo_type(r) for r in d["rows"]])

    @strawberry.field
    def workorder(self, id: strawberry.ID) -> Workorder | None:
        r = repo.workorder(int(id))
        return _wo_type(r) if r else None

    @strawberry.field
    def facility_by_token(self, info, token: str) -> Facility | None:
        """移动端扫码直达（FR-601）；token 失效 → NOT_FOUND（05 号 §6.3）。"""
        require_user(info)
        r = repo.facility_by_token(token)
        if not r:
            raise Exception429("NOT_FOUND", "二维码无效或已失效")
        return _fac_type(r, with_token=True)


@strawberry.type
class Mutation:
    @strawberry.mutation
    def register_facility(self, info, input: RegisterFacilityInput) -> Facility:
        """登记设施（admin/village，05 号 §6.3）：生成 F 编码并签发 qr_token。"""
        ctx = require_user(info, roles=["admin", "village"])
        try:
            r = repo.register_facility(input.type.value, input.name, input.village_code,
                                       input.parcel_code, input.build_year)
        except repo.BizError as e:
            _audit(ctx, "register_facility", "facility", input.name, result="DENIED", detail=e.msg)
            raise Exception429(e.code, e.msg)
        _audit(ctx, "register_facility", "facility", r["code"])
        repo.emit("hsf.facility.facility.registered",
                  {"code": r["code"], "type": r["type"], "village_code": r["village_code"]})
        return _fac_type(r, with_token=True)

    @strawberry.mutation
    def report_workorder(self, info, facility_code: str, description: str,
                         contact: str | None = None) -> Workorder:
        """上报工单（任意登录角色；FR-602 移动上报复用本 mutation）。"""
        ctx = require_user(info)
        try:
            r = repo.report_workorder(facility_code, description, contact, ctx["user_id"])
        except repo.BizError as e:
            raise Exception429(e.code, e.msg)
        _audit(ctx, "wo_transition", "workorder", r["id"], detail="report:OPEN")
        repo.emit("hsf.facility.workorder.created",
                  {"wo_id": int(r["id"]), "sn": r["sn"], "state": r["state"],
                   "actor_id": ctx["user_id"], "facility_code": r["facility_code"]})
        return _wo_type(r)

    @strawberry.mutation
    def accept_workorder(self, info, id: strawberry.ID) -> Workorder:
        """受理 OPEN→ACCEPTED（village/town/admin，05 号 §6.2）。"""
        ctx = require_user(info, roles=["village", "town", "admin"])
        return _apply(int(id), "accept", ctx)

    @strawberry.mutation
    def dispatch_workorder(self, info, id: strawberry.ID, assignee_id: strawberry.ID) -> Workorder:
        """派单 ACCEPTED→DISPATCHED（town/admin；assignee 须为 keeper）。"""
        ctx = require_user(info, roles=["town", "admin"])
        return _apply(int(id), "dispatch", ctx, assignee_id=int(assignee_id))

    @strawberry.mutation
    def resolve_workorder(self, info, id: strawberry.ID, note: str) -> Workorder:
        """办结 DISPATCHED→RESOLVED（assignee 本人或 admin；note 必填）。"""
        ctx = require_user(info)
        wo = repo.workorder(int(id))
        if not wo:
            raise Exception429("NOT_FOUND", "工单不存在")
        if "admin" not in set(ctx.get("user_roles") or []) and \
                ctx.get("user_id") != int(wo["assignee_id"] or 0):
            raise Exception429("FORBIDDEN", "仅被派单管护员或管理员可办结")
        return _apply(int(id), "resolve", ctx, note)

    @strawberry.mutation
    def approve_workorder(self, info, id: strawberry.ID) -> Workorder:
        """销项 RESOLVED→APPROVED（village/town/admin；绩效积分可空，FR-404）。"""
        ctx = require_user(info, roles=["village", "town", "admin"])
        return _apply(int(id), "approve", ctx)


# Federation Schema：显式 types 列表（CONVENTIONS §2.4——未注册实体不进 SDL，网关组合 400）
schema = strawberry.federation.Schema(
    query=Query, mutation=Mutation,
    types=[Facility, Party, LandParcel], enable_federation_2=True)
