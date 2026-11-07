"""project 缓存键常量与 TTL（04 号 §8.2 冻结：键 4 项目详情）。"""


def proj_key(code: str) -> str:
    """键4：项目详情，hsf:proj:{code}；任意流转 mutation 后失效（04 号 §8.5）。"""
    return f"hsf:proj:{code}"


PROJ_TTL = 300  # L2 TTL（s）
L1_TTL = 60  # L1 TTL ≤ L2 TTL/3（04 号 §8.4）
