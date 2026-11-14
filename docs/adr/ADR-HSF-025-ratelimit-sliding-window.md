# ADR-HSF-025：限流滑动窗口可选演进——RATE_MODE2（R-25，默认保持固定窗口）

| 项 | 内容 |
|---|---|
| 编号 | ADR-HSF-025（R-17 建库同批立卷；决策日 2026-11-14） |
| 状态 | 已采纳（Accepted，2026-11-14 实施并单测验收；**默认模式仍为固定窗口**） |
| 关联 | docs/24 §4 R-25（对标③：固定窗口实现代价=窗口交界突刺，相邻两窗合计最多 2×cap）、docs/07 §5.1/§5.2/§5.3（限流冻结契约——本 ADR 为增补，冻结表不改）、CONVENTIONS-P2 T-2、ADR-HSF-013（Redis 集群客户端红线）、`services/ratelimit/app/limiter.py`（SLIDE_SCRIPT/SlidingWindowLimiter 实现） |
| 决策日 | 2026-11-14 |

## 背景

- 冻结固有语义（docs/07 §5.1）：固定窗口存在窗口交界突刺（相邻窗口各打满=瞬时 2×cap 通过），换区跨副本
  全局一致计数与 O(1) 内存——对标③指出该实现代价可用滑动窗口消除。
- 一期冻结契约（响应 {allowed,remain}、键前缀 `hsf:rl:`、cap=1200/min/key、429 语义、§5.4 六步验证）不可
  破坏——E2E 用例 8（限流 429 确定性复现）与容量模型均按固定窗口理解。

## 决策

1. **默认保持固定窗口**（env `RATE_MODE2` 未设置或 `fixed`）——一期冻结契约零变化；
2. **滑动窗口作为可选模式**（`RATE_MODE2=sliding`，manifest 单行切换，不改镜像）：
   ZSET 按时间戳记成员，Lua 原子脚本（ZREMRANGEBYSCORE 剔旧 + ZADD cost 个唯一成员 + ZCARD + EXPIRE
   120）——任意 60s 滑窗内通过 ≤cap；被拒请求同样计入成员（对齐固窗"拒绝计数仍累计"语义，防重试风暴
   放大）；member=调用方 uuid4（同毫秒并发不互踩）；
3. **键格式变更按冻结清单纪律登记**（docs/24 R-25 实施路径要求）：滑窗键 `hsf:rl:{key}:slide` 与固窗键
   `hsf:rl:{key}:{分钟epoch}` **隔离**——两模式不得共用键名（STRING/ZSET 类型冲突）；响应契约
   {allowed, remain} 与固窗逐字段一致（gw 零感知）；docs/07 §5.1 增补行随本 ADR 落位；
4. **内存评估**：每成员 ~60B × cap 1200 ≈ 72KB/键，TTL 120s 自清——O(1) 计数换突刺消除，量级可接受
   （docs/24 R-25"ZSET vs INCR，TTL 清理"评估项）。

## 影响

- 兼容性：默认固窗=E2E 用例 8（429 确定性复现）、k6/容量模型、§5.4 六步全局语义全部不受影响；
  sliding 为显式 opt-in，启用前须复测 §5.4 六步（跨副本语义同源）+ 集群级"1500 突发相邻窗口合计 ≤1.2×cap"。
- 双实现并存成本：limiter.py 双类共存（FixedWindowLimiter 冻结保留为回退路径——与 gw RATE_MODE=local
  回退开关同纪律），单测两套面各自钉住语义防漂移。
- 回退：`RATE_MODE2=fixed` 单行回冻结行为；代码回退=还原 limiter.py（SlidingWindowLimiter 独立类，删除
  不影响固窗面）。
- 验证方式（本会话已执行）：pytest `services/ratelimit/tests/` 25 条全 PASS（固窗 17 条冻结语义 + 滑窗
  8 条：cap 边界/跨界不放新鲜额度（固窗对照同场景放行=2×cap 突刺）/含被拒计数的滑动恢复/键隔离/
  cost=1200/同 ms 并发唯一成员/非法 cost/端点响应形态不变）；集群级突发复测为部署窗口验收项（未执行，
  如实留档）。
