"""R-31（ADR-HSF-027）JWT RS256 非对称签名演进单测：签发/验签双路径 + 兼容开关 + fail-closed。

HS256 默认路径=冻结契约（docs/05 §1.4），RS256 路径=私钥签发（kid 头）+公钥验签；
伪造 HS256 令牌在 RS256 模式必须被拒（对标③验收口径）。密钥用测试内嵌 RSA-2048
（cryptography 库生成——requirements-base.txt 冻结依赖），不依赖外部密钥文件。
"""
import base64
import os
import time

import jwt
import pytest

os.environ.setdefault("JWT_SECRET", "r31-test-secret-not-a-real-credential")

from hsf_common import web  # noqa: E402


def _b64pem(pem: str) -> str:
    return base64.b64encode(pem.encode()).decode()


@pytest.fixture(scope="module")
def rsa_keypair():
    """测试 RSA-2048 密钥对（cryptography 生成，PEM 文本）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


# ── 默认路径：HS256 冻结契约逐字节不变 ─────────────────────────────────────────

def test_default_hs256_issue_and_decode():
    """JWT_ALG 未设置 → HS256 签发/验签（冻结路径），claims 形态不变。"""
    assert web.JWT_ALG == "HS256"
    tok = web.issue_token(7, "管理员", ["admin"], ttl_seconds=7200)
    hdr = jwt.get_unverified_header(tok)
    assert hdr["alg"] == "HS256"
    claims = web.decode_token(tok)
    assert claims["sub"] == "7" and claims["name"] == "管理员" and claims["roles"] == ["admin"]
    assert claims["exp"] - int(time.time()) <= 7200


def test_decode_token_rejects_bad_signature():
    """HS256 模式错密令牌拒绝（既有 E2E 用例 5 语义）。"""
    forged = jwt.encode({"sub": "1", "name": "x", "roles": [], "exp": int(time.time()) + 60},
                        "wrong-secret", algorithm="HS256")
    with pytest.raises(Exception):
        web.decode_token(forged)


# ── RS256 路径：私钥签发 + 公钥验签 + kid 头 ───────────────────────────────────

def test_rs256_issue_decode_roundtrip(rsa_keypair, monkeypatch):
    """JWT_ALG=RS256：私钥签发（alg=RS256 + kid 头），公钥验签还原同一 claims。"""
    priv, pub = rsa_keypair
    monkeypatch.setattr(web, "JWT_ALG", "RS256")
    monkeypatch.setattr(web, "JWT_PRIVATE_KEY", priv)
    monkeypatch.setattr(web, "JWT_PUBLIC_KEY", pub)
    monkeypatch.setattr(web, "JWT_KID", "rsa-test")
    tok = web.issue_token(42, "张三", ["village"], ttl_seconds=60)
    hdr = jwt.get_unverified_header(tok)
    assert hdr["alg"] == "RS256" and hdr.get("kid") == "rsa-test"   # kid 头支持多公钥轮换
    claims = web.decode_token(tok)
    assert claims["sub"] == "42" and claims["roles"] == ["village"]


def test_rs256_forged_hs256_rejected(rsa_keypair, monkeypatch):
    """验收口径（docs/24 R-31）：伪造 HS256 旧共享密钥令牌 → RS256 公钥验签拒绝。"""
    priv, pub = rsa_keypair
    monkeypatch.setattr(web, "JWT_ALG", "RS256")
    monkeypatch.setattr(web, "JWT_PRIVATE_KEY", priv)
    monkeypatch.setattr(web, "JWT_PUBLIC_KEY", pub)
    forged = jwt.encode({"sub": "1", "name": "attacker", "roles": ["admin"],
                         "exp": int(time.time()) + 600}, os.environ["JWT_SECRET"], algorithm="HS256")
    with pytest.raises(jwt.InvalidAlgorithmError):
        web.decode_token(forged)


def test_rs256_cross_keypair_rejected(rsa_keypair, monkeypatch):
    """他钥签名令牌拒绝（非配对公钥）。"""
    priv, pub = rsa_keypair
    other_pub = priv  # 故意用私钥当"公钥"（必然验签失败且形态合法）
    monkeypatch.setattr(web, "JWT_ALG", "RS256")
    monkeypatch.setattr(web, "JWT_PRIVATE_KEY", priv)
    monkeypatch.setattr(web, "JWT_PUBLIC_KEY", other_pub)
    tok = web.issue_token(1, "x", [], ttl_seconds=60)
    with pytest.raises(Exception):
        web.decode_token(tok)


def test_rs256_fail_closed_no_public_key(rsa_keypair, monkeypatch):
    """fail-closed：RS256 模式公钥缺失 → decode_token 即抛（不允许回退对称密钥）。"""
    priv, _ = rsa_keypair
    monkeypatch.setattr(web, "JWT_ALG", "RS256")
    monkeypatch.setattr(web, "JWT_PRIVATE_KEY", priv)
    monkeypatch.setattr(web, "JWT_PUBLIC_KEY", "")
    tok = web.issue_token(1, "x", [], ttl_seconds=60)   # 私钥在位可签发
    with pytest.raises(RuntimeError):
        web.decode_token(tok)


def test_pem_base64_decoding(rsa_keypair, monkeypatch):
    """Secret 键值口径：PEM 的 base64 单行 → _pem 还原 PEM 文本；非法 base64 输入显式报错。"""
    priv, pub = rsa_keypair
    monkeypatch.setenv("JWT_PRIVATE_KEY", _b64pem(priv))
    assert web._pem("JWT_PRIVATE_KEY") == priv
    monkeypatch.setenv("JWT_PRIVATE_KEY", "not-base64!!!")
    with pytest.raises(RuntimeError):
        web._pem("JWT_PRIVATE_KEY")
    monkeypatch.setenv("JWT_PRIVATE_KEY", "")
    assert web._pem("JWT_PRIVATE_KEY") == ""
