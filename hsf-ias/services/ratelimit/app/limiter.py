"""ratelimit 限流核心：Redis 固定窗口计数（键/TTL/响应三要素按 docs/07 §5.1 / CONVENTIONS-P2 T-2 冻结）。

键 `hsf:rl:{key}:{分钟epoch}`；**Lua 原子脚本单次 RTT**：INCRBY(key, cost)，c==cost（本窗口首次
写入）时 EXPIRE(key, 120)——INCRBY 与 TTL 置位同脚本原子完成，消除"INCR 后 EXPIRE 前"进程崩溃
留下无 TTL 永生键的崩溃窗口（docs/07 §9-⑥：T-2"INCR+EXPIRE 120"的原子化等价实现，键格式/TTL/
响应语义不变）。固定窗口较令牌桶的实现代价是窗口交界突刺（相邻两窗合计最多 2×cap 瞬时通过），
换取跨副本全局一致计数与 O(1) 内存；TTL 120s > 60s 窗长覆盖时钟偏斜，旧窗口键由 Redis 自清。

客户端注入：生产 redis.cluster.RedisCluster（二期一律 Python 客户端，禁 ioredis——hostname
广播集群刷新循环事故，CONVENTIONS-P2 T-3）；测试注入内存 fake（本文件不依赖真实外部服务）。
"""
import time

TTL_SECONDS = 120  # 窗口键存活（T-2/§5.1 冻结）：≥2×窗长，时钟偏斜下上一窗口键也能完成自清

# 原子计数脚本（docs/07 §5.1 冻结语义）：c==cost 即本窗口首次写入时补 EXPIRE；
# KEYS[1]=窗口键，ARGV[1]=cost，ARGV[2]=TTL。Redis Cluster 按 numkeys 路由到键所在分片。
COUNT_SCRIPT = """
local c = redis.call('INCRBY', KEYS[1], ARGV[1])
if c == tonumber(ARGV[1]) then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return c
"""


class FixedWindowLimiter:
    """固定窗口限流器：判定逻辑与 Redis 访问解耦，client 仅要求 eval/ping 最小面。

    并发正确性由 Lua 脚本在 Redis 单线程内原子执行保证（跨副本/跨进程同键同一计数）——
    这是"全局限流"语义的根基：gw 任意副本的请求都落在同一个窗口键上（docs/07 §5）。
    """

    def __init__(self, cap: int, prefix: str = "hsf:rl"):
        self.cap = max(int(cap), 1)
        self._prefix = prefix

    @staticmethod
    def minute_epoch(now: float | None = None) -> int:
        """分钟 epoch = int(unix 秒 // 60)：同一自然分钟内全局同键（T-2 冻结口径）。"""
        return int((time.time() if now is None else now) // 60)

    def redis_key(self, key: str, epoch: int) -> str:
        return f"{self._prefix}:{key}:{epoch}"

    def hit(self, client, key: str, cost: int = 1, now: float | None = None) -> dict:
        """扣减一次配额并判定：返回 {allowed, remain}（冻结响应形态）。

        cur = Lua 原子 INCRBY(cost)+条件 EXPIRE(120)；allowed = cur ≤ cap；
        remain = max(cap − cur, 0)。cost>1（E2E/验证用）一次计入等价 N 请求占用（§5.1）。
        """
        cost = int(cost)
        if cost < 1:
            raise ValueError("cost 必须 ≥ 1")
        rk = self.redis_key(key, self.minute_epoch(now))
        cur = int(client.eval(COUNT_SCRIPT, 1, rk, cost, TTL_SECONDS))
        return {"allowed": cur <= self.cap, "remain": max(0, self.cap - cur)}
