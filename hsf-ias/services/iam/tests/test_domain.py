"""iam 单测：国密口令派生、JWT 签发校验（sub 字符串）、手机号脱敏、SDL 联邦注册断言、
鉴权错误契约（extensions.code）、缓存降级（均不依赖 DB/Redis）。"""
import base64
import os
import sys

sys.path.insert(0, "/srv")  # 容器内 /srv 平铺（Dockerfile ENV PYTHONPATH=/srv）
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))  # 本地回退：services/iam → app
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(_here)), "common"))  # → hsf_common

import jwt

from hsf_common.crypto import mask_phone, sm3, sm3_pbkdf2, sm4_decrypt, sm4_encrypt
from hsf_common.web import Exception429, issue_token

os.environ.setdefault("REDIS_URL", "127.0.0.1:6390")  # 无 Redis 环境：快速拒连，走降级路径

from app.schema import schema


# ───────── 国密 ─────────
def test_sm3_deterministic_and_differs():
    assert sm3(b"abc") == sm3(b"abc")
    assert sm3(b"abc") != sm3(b"abd")
    assert len(sm3(b"x")) == 64  # 32字节hex


def test_password_hash_salt_changes_output():
    h1 = sm3_pbkdf2("hsf@2026", "aa" * 16, 100)
    h2 = sm3_pbkdf2("hsf@2026", "bb" * 16, 100)
    assert h1 != h2 and len(h1) == 64


def test_sm4_roundtrip_and_mask():
    enc = sm4_encrypt("13800000601")
    assert enc != "13800000601"
    assert sm4_decrypt(enc) == "13800000601"
    assert mask_phone("13800000601") == "138****0601"  # 05 号 §2.3 冻结示例


# ───────── JWT（PyJWT≥2.10：sub 必须字符串，CONVENTIONS §2.1）─────────
_JWT_SECRET = os.environ.get("JWT_SECRET", "HsfJwt-2026-bN8wQ4vXzA6sD1fG")  # 与 gw 同源


def test_jwt_roundtrip_int_sub_coerced_to_str():
    tok = issue_token(7, "张县管", ["admin"])
    payload = jwt.decode(tok, _JWT_SECRET, algorithms=["HS256"])
    assert payload["sub"] == "7" and payload["roles"] == ["admin"]


def test_jwt_roundtrip_str_sub_passthrough():
    tok = issue_token("601", "张县管", ["admin"])
    assert jwt.decode(tok, _JWT_SECRET, algorithms=["HS256"])["sub"] == "601"


# ───────── SDL 联邦契约（CONVENTIONS §2.4 / 05 号 §3.1 冻结）─────────
def test_federation_sdl_explicit_types():
    sdl = str(schema)
    assert 'type Party @key(fields: "id")' in sdl  # 实体进 SDL（types 显式注册）
    assert sdl.count("@key(") == 1                 # 指令用法仅 Party 一处（@link import 行不含括号）；
    assert "Organization @key" not in sdl          # Organization 为普通类型（D-05-5）
    assert "type Organization {" in sdl
    assert "phoneMasked" in sdl and "orgCode" in sdl   # snake_case → camelCase（A-05-3）
    assert "type AuthPayload {" in sdl and "partyId" in sdl  # 类型名 CamelCase，字段名 camelCase
    assert "orgTree" in sdl and "auditLogs" in sdl
    assert "login(phone:" in sdl


def test_unauthenticated_error_contract():
    """D-05-2：未登录 orgTree → errors[0].extensions.code=UNAUTHENTICATED（HTTP 200 通道）。"""
    r = schema.execute_sync("query { orgTree { code } }",
                            context_value={"user_id": 0, "user_roles": [], "request": None})
    assert r.errors and r.errors[0].extensions["code"] == "UNAUTHENTICATED"
    assert r.errors[0].message == "请先登录"  # 中文文案不脱敏（§2.1 规约①）


def test_forbidden_error_contract():
    """auditLogs 仅 admin：越权 → FORBIDDEN（FR-704，评审 R16）。"""
    r = schema.execute_sync("query { auditLogs { total } }",
                            context_value={"user_id": 8, "user_roles": ["keeper"], "request": None})
    assert r.errors and r.errors[0].extensions["code"] == "FORBIDDEN"


def test_me_null_without_login():
    """V-20：无 token 查 me → me=null（业务空而非报错）。"""
    r = schema.execute_sync("query { me { id } }",
                            context_value={"user_id": 0, "user_roles": [], "request": None})
    assert not r.errors and r.data["me"] is None


def test_exception429_carries_extensions():
    e = Exception429("VALIDATION", "村码不存在")
    assert e.extensions == {"code": "VALIDATION"} and e.msg == "村码不存在"


# ───────── GraphQL 上下文（身份头解码，CONVENTIONS §2.7）─────────
class _Req:
    def __init__(self, headers):
        self.headers = headers


def test_gql_context_b64_name_and_roles():
    from hsf_common.web import gql_context
    b64name = base64.b64encode("张县管".encode()).decode()
    ctx = gql_context(_Req({"x-user-id": "601", "x-user-name": b64name, "x-user-roles": "admin"}))
    assert ctx["user_id"] == 601 and ctx["user_name"] == "张县管" and ctx["user_roles"] == ["admin"]


def test_gql_context_plaintext_name_fallback():
    from hsf_common.web import gql_context
    ctx = gql_context(_Req({"x-user-id": "602", "x-user-name": "青禾乡管", "x-user-roles": "town"}))
    assert ctx["user_name"] == "青禾乡管" and ctx["user_roles"] == ["town"]  # 明文回退


def test_gql_context_anonymous():
    from hsf_common.web import gql_context
    ctx = gql_context(_Req({}))
    assert ctx["user_id"] == 0 and ctx["user_name"] == "" and ctx["user_roles"] == [""]


# ───────── 缓存三防与降级（P8：Redis 不可用不阻塞业务）─────────
def test_cache_degrades_without_redis():
    from hsf_common import cache
    cache.l1.delete("hsf:test:degrade")
    calls = []
    val = cache.cached("hsf:test:degrade", 60, lambda: (calls.append(1), "v")[1], l1_ttl=5)
    assert val == "v" and calls == [1]  # L2 不可用 → 直查 loader
    val2 = cache.cached("hsf:test:degrade", 60, lambda: calls.append(2), l1_ttl=5)
    assert val2 == "v" and calls == [1]  # 二次命中 L1，不回源


def test_cache_empty_value_not_in_l1():
    from hsf_common import cache
    cache.l1.delete("hsf:test:empty")
    assert cache.cached("hsf:test:empty", 60, lambda: None) is None
    assert cache.l1.get("hsf:test:empty")[1] is False  # 空值不进 L1（键 12 防穿透）
