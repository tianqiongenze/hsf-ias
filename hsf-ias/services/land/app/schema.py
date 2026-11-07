"""land GraphQL Federation 子图（SDL 冻结于 05 号 §4.1；错误码 §2.1；数据范围 §2.4）。"""
import json

import strawberry

from hsf_common.web import Exception429, require_user

from . import cache_keys, domain, repo


@strawberry.federation.type(keys=["id"])
class Party:
    """跨子图实体桩（iam 拥有基础字段；FR-104 holder 联邦引用）。"""

    id: strawberry.ID

    @classmethod
    def resolve_reference(cls, id: strawberry.ID):
        return cls(id=id)


def _iso(v) -> str | None:
    """DATETIME → ISO8601+08:00（05 号 §2.3 D-05-6；兼容缓存 JSON 回读的 str）。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if "T" in s else s.replace(" ", "T") + "+08:00"
    return v.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _normalize(r) -> dict:
    """行 → 类型安全 dict（Decimal/datetime 归一，缓存 JSON 回读前后形态一致）。"""
    r = dict(r)
    area = r.get("area_mu")
    r["area_mu"] = float(area) if area is not None else None
    r["created_at"] = _iso(r.get("created_at"))
    geo = r.get("geo_json")
    if geo is not None and not isinstance(geo, str):
        geo = json.dumps(geo, ensure_ascii=False)
    r["geo_json"] = geo
    return r


@strawberry.federation.type(keys=["code"])
class LandParcel:
    code: str
    name: str
    village_code: str
    area_mu: float | None
    soil_type: str | None
    crop_type: str | None
    status: str
    geojson: str | None
    created_at: str | None

    @strawberry.field
    def holder(self) -> "Party | None":
        """权属人联邦桩（FR-104：gateway 经 iam 子图解析 name 等）。"""
        pid = repo.holder_id_of(self.code)
        return Party(id=str(pid)) if pid else None

    @classmethod
    def resolve_reference(cls, code: str):
        r = repo.parcel_cached(code)
        return _to_type(_normalize(r)) if r else None


def _to_type(r: dict) -> LandParcel:
    return LandParcel(code=r["code"], name=r["name"], village_code=r["village_code"],
                      area_mu=r["area_mu"], soil_type=r["soil_type"], crop_type=r["crop_type"],
                      status=r["status"], geojson=r.get("geo_json"), created_at=r["created_at"])


@strawberry.type
class LandParcelPage:
    total: int
    page: int
    size: int
    items: list[LandParcel]


@strawberry.type
class LandStats:
    village_code: str
    total: int
    total_area_mu: float | None


@strawberry.input
class RegisterParcelInput:
    name: str
    village_code: str
    holder_id: strawberry.ID | None = None
    area_mu: float | None = None
    soil_type: str | None = None
    crop_type: str | None = None
    geojson: str | None = None


def _role_of(ctx: dict) -> str:
    for r in (ctx.get("user_roles") or []):
        if r in ("admin", "town", "village", "keeper", "coop"):
            return r
    return ""


def _scope(ctx: dict) -> dict:
    """05 号 §2.4 数据范围：admin 全县 / town 乡内村集 / village 本村 / keeper 空集 / coop holder=me。"""
    role = _role_of(ctx)
    org = repo.party_org(ctx["user_id"]) or ""
    if role == "admin":
        return {"kind": "all"}
    if role == "town":
        # 组织缺失按空集处理，防 LIKE '%%' 全县泄漏
        return {"kind": "prefix", "org": org} if org else {"kind": "deny"}
    if role == "village":
        return {"kind": "exact", "org": org} if org else {"kind": "deny"}
    if role == "coop":
        return {"kind": "holder", "holder": ctx["user_id"]}
    return {"kind": "deny"}  # keeper 及未知角色：无地块范围


def _out_of_scope(scope: dict, village_code: str | None) -> bool:
    if village_code is None:
        return False
    if scope["kind"] == "prefix":
        return not village_code.startswith(scope["org"])
    if scope["kind"] == "exact":
        return village_code != scope["org"]
    return False


def _filters(scope: dict, village_code: str | None):
    """scope+入参 → 仓储过滤参数（交集语义）；deny 返回 None 表示空集。"""
    if scope["kind"] == "deny" or _out_of_scope(scope, village_code):
        return None
    if scope["kind"] == "all":
        return {"village_like": village_code, "village_eq": None, "holder_id": None}
    if scope["kind"] == "prefix":
        return {"village_like": village_code or scope["org"], "village_eq": None, "holder_id": None}
    if scope["kind"] == "exact":
        return {"village_like": None, "village_eq": scope["org"], "holder_id": None}
    return {"village_like": village_code, "village_eq": None, "holder_id": scope["holder"]}


@strawberry.type
class Query:
    @strawberry.field
    def land_parcel(self, code: str) -> "LandParcel | None":
        """按编码点查（公开查询，05 号 §4.2；缓存键 3）。"""
        r = repo.parcel_cached(code)
        return _to_type(_normalize(r)) if r else None

    @strawberry.field
    def land_parcels(self, info, village_code: str | None = None, page: int = 1,
                     size: int = 20) -> LandParcelPage:
        """按村分页（登录+数据范围 §2.4）。"""
        ctx = require_user(info)
        page, size = max(page, 1), min(size, 100)  # 顶层列表 size≤100 静默钳制（05 号 §2.2）
        f = _filters(_scope(ctx), village_code)
        if f is None:
            return LandParcelPage(total=0, page=page, size=size, items=[])
        total, rows = repo.list_parcels(page, size, **f)
        return LandParcelPage(total=total, page=page, size=size,
                              items=[_to_type(_normalize(r)) for r in rows])

    @strawberry.field
    def land_geojson(self, info, village_code: str) -> str:
        """一张图数据源：村级 FeatureCollection JSON 字符串（FR-103；缓存键 2）。"""
        ctx = require_user(info)
        if not repo.org_exists(village_code):
            raise Exception429("VALIDATION", "村码不存在")
        f = _filters(_scope(ctx), village_code)
        if f is None:
            return json.dumps(domain.empty_feature_collection(), ensure_ascii=False)
        return json.dumps(repo.geojson_cached(village_code, f), ensure_ascii=False)

    @strawberry.field
    def land_stats(self, info, village_code: str) -> LandStats:
        """一张图统计：地块总数/总面积（FR-203；读池直查不缓存）。"""
        ctx = require_user(info)
        if not repo.org_exists(village_code):
            raise Exception429("VALIDATION", "村码不存在")
        f = _filters(_scope(ctx), village_code)
        if f is None:
            total, area = 0, None
        else:
            total, area = repo.stats(**f)
        return LandStats(village_code=village_code, total=total, total_area_mu=area)


@strawberry.type
class Mutation:
    @strawberry.mutation
    def register_parcel(self, info, input: RegisterParcelInput) -> LandParcel:
        """登记地块（admin/village；village 强制本村——05 号 §2.4 写归属）。"""
        ctx = require_user(info, roles=["admin", "village"])
        scope = _scope(ctx)
        if scope["kind"] == "exact" and input.village_code != scope["org"]:
            repo.audit_denied(ctx, "register_parcel", "parcel", input.village_code, "越权登记非本村地块")
            raise Exception429("FORBIDDEN", "无权限登记该村地块")
        # 不变量（03 号 §4.2）：村存在 / 权属人存在 / 面积非负 / 几何合法
        if not repo.org_exists(input.village_code):
            raise Exception429("VALIDATION", "村码不存在")
        holder = int(input.holder_id) if input.holder_id else None
        if holder and not repo.party_exists(holder):
            raise Exception429("NOT_FOUND", "权属人不存在")
        if input.area_mu is not None and input.area_mu < 0:
            raise Exception429("VALIDATION", "面积不能为负")
        try:
            geo = domain.validate_geojson(input.geojson)
        except domain.BusinessError as e:
            raise Exception429(e.code, e.msg)
        r = repo.register(input.name, input.village_code, holder, input.area_mu,
                          input.soil_type, input.crop_type, geo, ctx)
        return _to_type(_normalize(r))


# 显式 types 列表：未注册实体不进 SDL，网关组合直接 400（CONVENTIONS §2.4）
schema = strawberry.federation.Schema(query=Query, mutation=Mutation,
                                      types=[LandParcel, Party], enable_federation_2=True)
