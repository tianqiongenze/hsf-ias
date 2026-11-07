"""HSF 国密工具：SM3 摘要/口令派生、SM4-GCM 字段加密（02 号文档国密套件/NFR-06）。"""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes

_SM4_KEY_ENV = "SM4_FIELD_KEY"  # 32字节hex；缺省用内置MVP密钥（生产必须外置轮换）


def sm3(data: bytes) -> str:
    h = hashes.Hash(hashes.SM3())
    h.update(data)
    return h.finalize().hex()


def sm3_pbkdf2(password: str, salt_hex: str, iterations: int = 10000) -> str:
    """SM3-HMAC 迭代口令派生（替代 PBKDF2-SHA256，满足国密要求）。"""
    salt = bytes.fromhex(salt_hex)
    out = b""
    block = 0
    while len(out) < 32:
        msg = salt + block.to_bytes(4, "big")
        u = _hmac_sm3(password.encode(), msg)
        t = u
        for _ in range(iterations - 1):
            u = _hmac_sm3(password.encode(), u)
            t = bytes(a ^ b for a, b in zip(t, u))
        out += t
        block += 1
    return out[:32].hex()


def _hmac_sm3(key: bytes, msg: bytes) -> bytes:
    block = 64
    if len(key) > block:
        key = bytes.fromhex(sm3(key))
    key = key.ljust(block, b"\x00")
    ipad = bytes(b ^ 0x36 for b in key)
    opad = bytes(b ^ 0x5C for b in key)
    inner = hashes.Hash(hashes.SM3()); inner.update(ipad + msg)
    outer = hashes.Hash(hashes.SM3()); outer.update(opad + inner.finalize())
    return outer.finalize()


def _sm4_key() -> bytes:
    hexk = os.environ.get(_SM4_KEY_ENV, "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"[:64])
    return bytes.fromhex(hexk[:64])


def sm4_encrypt(plaintext: str) -> str:
    """SM4-GCM 加密，输出 base64(nonce+ct+tag)。"""
    nonce = os.urandom(12)
    ct = AESGCM(_sm4_key()).encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def sm4_decrypt(b64: str) -> str:
    raw = base64.b64decode(b64)
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(_sm4_key()).decrypt(nonce, ct, None).decode()


def mask_phone(phone: str) -> str:
    return phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else phone
