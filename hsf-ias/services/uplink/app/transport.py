"""transport 隔离层（V-3 红线：真实平台联调仅替换本文件，状态机/幂等/重试不变）。

StubTransport：内网无真实监管平台——按确定性规则生成回执（payload 完整性校验通过→
accepted=true + checksum；失败→accepted=false + 原因清单），docs/08 §3.2 D-08-3。
真实 Transport 实现 HTTP 客户端对接"全国农田建设综合监测监管平台"后，main.py 按
`UPTRANSPORT=real` 环境变量切换（默认 stub）。
"""
import hashlib
import json
import os

from . import assemble


class StubTransport:
    """契约化回执 stub（内网口径）：校验 payload 完整性→确定性回执。"""

    name = "stub"

    def send(self, payload: dict) -> tuple[dict, bool]:
        errors = assemble.validate_payload(payload)
        if errors:
            return {"accepted": False, "errors": errors[:10], "transport": self.name}, False
        checksum = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        return {"accepted": True, "receipt": {"taskId": f"MOCK-{checksum}",
                                              "checksum": checksum,
                                              "items": len(payload.get("items") or [])},
                "transport": self.name}, True


class RealTransport:                                      # pragma: no cover（三期不做网络联调）
    """真实平台 HTTP 客户端占位（V-3：字段/地址/鉴权按监管平台接入规范补充后启用）。"""

    name = "real"
    ENDPOINT = os.environ.get("UPLINK_ENDPOINT", "")

    def send(self, payload: dict) -> tuple[dict, bool]:
        raise NotImplementedError("真实监管平台接入规范待甲方提供（docs/08 §3 显式不做）")


def send(payload: dict) -> tuple[dict, bool]:
    impl = RealTransport() if os.environ.get("UPTRANSPORT") == "real" else StubTransport()
    return impl.send(payload)
