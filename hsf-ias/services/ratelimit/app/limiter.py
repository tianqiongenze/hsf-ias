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


# ── R-25（2026-11-14，ADR-HSF-025）：滑动窗口可选演进 ─────────────────────────────
# 消固定窗口"窗口交界突刺"（相邻两窗合计最多 2×cap 瞬时通过）：ZSET 按时间戳记成员，
# 每次判定前 ZREMRANGEBYSCORE 剔除窗外旧成员 + ZCARD 计数——任意 60s 滑窗内通过量 ≤ cap。
# 响应契约 {allowed, remain} 不变（docs/07 §5.1 冻结）；键格式变更（{key}:slide 后缀，
# 与固定窗口 {key}:{分钟epoch} 计数键隔离——两种模式不得共用键名，避免 STRING/ZSET 类型冲突），
# 按 docs/07 §5.1 增补 + ADR-HSF-025 登记。默认模式仍为固定窗口（RATE_MODE2=fixed，
# 一期冻结契约不破坏）；内存评估：每成员 ~60B（score 8B+member 16B+skiplist 开销）×cap 上限
# 1200 ≈ 72KB/键，TTL 120s 自清，量级可接受（ZSET vs INCR：O(1) 内存换突刺消除）。

# 滑动窗口原子脚本：KEYS[1]=ZSET 键；ARGV[1]=now(ms) ARGV[2]=cost ARGV[3]=TTL(s)
# ARGV[4]=窗长(ms) ARGV[5]=本次命中唯一 member（调用方 uuid4，跨命中不重、同 hit 内 :i 后缀区分）。
# 剔旧+写入+计数+续期同脚本原子完成（与 COUNT_SCRIPT 同一原子性纪律）。
SLIDE_SCRIPT = """
local now = tonumber(ARGV[1])
local cost = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - tonumber(ARGV[4]))
for i = 0, cost - 1 do
  redis.call('ZADD', KEYS[1], now, ARGV[5] .. ':' .. i)
end
local c = redis.call('ZCARD', KEYS[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return c
"""

SLIDE_KEY_SUFFIX = "slide"      # 滑窗键后缀（{prefix}:{key}:slide，与固窗分钟 epoch 键隔离）


class SlidingWindowLimiter:
    """滑动窗口限流器（RATE_MODE2=sliding 可选启用，默认仍固定窗口——冻结契约不破坏）。

    与 FixedWindowLimiter 同一 client 最小面（eval）与响应形态 {allowed, remain}；
    判定原子性由 SLIDE_SCRIPT 在 Redis 单线程内保证（跨副本同键一致）。
    """

    def __init__(self, cap: int, prefix: str = "hsf:rl", window_seconds: int = 60):
        self.cap = max(int(cap), 1)
        self._prefix = prefix
        self.window_ms = int(window_seconds) * 1000

    def redis_key(self, key: str) -> str:
        return f"{self._prefix}:{key}:{SLIDE_KEY_SUFFIX}"

    def hit(self, client, key: str, cost: int = 1, now: float | None = None) -> dict:
        """扣减一次配额并判定：返回 {allowed, remain}（冻结响应形态，与固窗逐字段一致）。

        c = Lua 原子（剔窗外旧成员 + ZADD cost 个成员 + ZCARD + EXPIRE 120）；
        allowed = c ≤ cap；remain = max(cap − c, 0)。被拒请求同样计入（与固窗"拒绝计数
        仍累计"语义一致，防重试风暴放大）。member 用 uuid4 保证唯一（同 ms 并发不互踩）。
        """
        import uuid
        cost = int(cost)
        if cost < 1:
            raise ValueError("cost 必须 ≥ 1")
        now_ms = int(round((time.time() if now is None else now) * 1000))
        member = uuid.uuid4().hex
        c = int(client.eval(SLIDE_SCRIPT, 1, self.redis_key(key),
                            now_ms, cost, TTL_SECONDS, self.window_ms, member))
        return {"allowed": c <= self.cap, "remain": max(0, self.cap - c)}
