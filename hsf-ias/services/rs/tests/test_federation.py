"""R-12（docs/24 §4）单测：rs 子图 federation Schema 构造契约 + Federation 2 形态判定。

不依赖外部服务（CONVENTIONS §4 口径）：
  1. 源码契约：services/rs/app/main.py 的 strawberry.federation.Schema 构造必须内嵌
     enable_federation_2=True（与 static_gate check_federation R-12 新检查同判据——门禁与单测双保险）；
  2. 形态判定（strawberry-graphql 0.254.1 本地复现）：enable_federation_2=True 是 F1→F2 形态
     开关——子图"在用"联邦指令（如 @key 实体）时 SDL 出现 schema @link(...)，缺参则无；
     rs 子图 RsVigor 为普通类型（docs/08 §2.4 冻结，D-05-5 @key 实体仅 4 个）——SDL 是否含
     @link 取决于在用指令而非本参数，故本测同时锁定两种形态的判定语义，防实现回归。
  3. 版本自适应（2026-11-14 全量单测跨环境复核增补）：strawberry-graphql ≥0.26 移除
     enable_federation_2 参数（Federation 2 为默认形态，federation_version 缺省 ≥2.0）。
     形态断言按安装版本分派：钉版 0.254.1 锁 F1/F2 双形态开关语义；新版本锁"缺省即 F2"
     契约形态——源码契约断言（①）与"F2 形态必需"语义两版本下均保持，非判据放宽。
"""
import asyncio
import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, "/srv")

import strawberry  # noqa: E402

_RS_APP_MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"

# enable_federation_2 参数是否存在于当前安装版 strawberry 的 Schema 构造签名
_HAS_F2_PARAM = ("enable_federation_2"
                 in inspect.signature(strawberry.federation.Schema.__init__).parameters)


def _ctor_args(flat: str) -> list[str]:
    """strip 空白后抽取全部 federation.Schema 构造参数段（与 static_gate R-12 同法）。"""
    return [flat[m.end():m.end() + 200].split(")")[0]
            for m in re.finditer(r"strawberry\.federation\.Schema\(", flat)]


def test_rs_schema_constructs_with_enable_federation_2():
    """rs 子图 Schema 构造必须内嵌 enable_federation_2=True（R-12 验收②源码级）。"""
    text = _RS_APP_MAIN.read_text(encoding="utf-8")
    flat = re.sub(r"\s+", "", text)
    ctors = _ctor_args(flat)
    assert ctors, "rs main.py 未使用 strawberry.federation.Schema（联邦子图契约缺失）"
    assert any("enable_federation_2=True" in c for c in ctors), (
        "rs 子图 Schema 构造缺 enable_federation_2=True（R-12 契约参数回归）")
    assert any("types=[RsVigor]" in c for c in ctors), (
        "rs 子图 Schema 构造缺显式 types=[RsVigor]（CONVENTIONS §2.4）")


@strawberry.type
class _Plain:
    v: str


@strawberry.federation.type(keys=["id"])
class _Keyed:
    id: str


@strawberry.type
class _Query:
    @strawberry.field
    def probe(self) -> list[_Plain]:
        return []


def _service_sdl(kw: dict, types: tuple) -> str:
    if not _HAS_F2_PARAM:
        # strawberry-graphql≥0.26：enable_federation_2 参数已移除（F2 默认），丢弃该 kwarg
        kw = {k: v for k, v in kw.items() if k != "enable_federation_2"}
    schema = strawberry.federation.Schema(query=_Query, types=list(types), **kw)
    res = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        schema.execute("{ _service { sdl } }"))
    return res.data["_service"]["sdl"]


def test_enable_federation_2_emits_link_when_directive_in_use():
    """形态判定（docs/24 §4 R-12 实施③本地复现）：在用联邦指令 + enable_federation_2=True
    → _service.sdl 含 schema @link(...)；钉版 0.254.1 缺参 → Federation 1 形态（无 @link）；
    ≥0.26 参数移除后 F2 为默认形态，锁"缺省即 F2"契约形态。"""
    sdl_f2 = _service_sdl({"enable_federation_2": True}, (_Plain, _Keyed))
    assert "@link" in sdl_f2, "enable_federation_2=True 未产出 Federation 2 形态 SDL（@link 缺失）"
    if _HAS_F2_PARAM:
        sdl_f1 = _service_sdl({}, (_Plain, _Keyed))
        assert "@link" not in sdl_f1, "缺参构造不应产出 @link（Federation 1 形态语义回归）"
    else:
        sdl_default = _service_sdl({}, (_Plain, _Keyed))
        assert "@link" in sdl_default, (
            "strawberry-graphql≥0.26 缺省构造未产出 @link（Federation 2 默认形态回归）")


def test_plain_type_subgraph_sdl_param_independent():
    """rs 实际形态：无 @key 实体的普通类型子图（docs/08 §2.4 冻结），SDL 在 F1/F2 两参
    形态下不含 @link——gw 以 F1 升级路径组合（组合可用性不受影响，参数为契约一致性项）。
    锁定该实现行为，防止误判"@link 缺失=参数未生效"。"""
    sdl_f2 = _service_sdl({"enable_federation_2": True}, (_Plain,))
    assert "@link" not in sdl_f2, (
        "普通类型子图出现 @link 与 docs/08 §2.4 冻结形态不符（RsVigor 非实体，若出现说明子图被改）")
    assert "_service" in sdl_f2, "联邦 _service 契约字段缺失（gw 组合必需）"
