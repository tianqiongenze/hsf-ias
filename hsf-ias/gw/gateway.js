// HSF GraphQL 联邦网关（Apollo Gateway）——移植自 dv-ias/gw/gateway.js
// 职责：超图组合、JWT 校验与用户头注入（防伪造）、限流（HTTP 调 ratelimit-svc，fail-open）、CORS、健康检查
// 端口：业务 8090（GraphQL）+ 独立探针 9090（/healthz，CONVENTIONS §2.5）
// 二期限流改造（docs/07 §5.2/§5.3/§9-⑥ 冻结）：判定改由 ratelimit-svc（:8096）经 Redis Cluster
//   全局固定窗口计数——gw 任意副本同键计数，跨副本全局一致；非 200/网络错误/500ms 超时一律
//   fail-open 放行（节流告警日志同 key 60s 一条）；429 语义与响应体保持一期冻结形态（docs/06 P1 / V-25）。
//   一期进程内令牌桶保留约 15 行为 RATE_MODE=local 回退路径（ratelimit-svc/Redis 故障且 fail-open
//   不可接受时 manifest 单行切回，不改镜像；回退路径为纯进程内实现，无任何 Redis 客户端依赖）。
// 注：sim 为 REST 服务（docs/05 §0 D-05-3，FastAPI :8095/NodePort 31211），不进联邦组合——
//     SUBGRAPHS 默认为 iam/land/project/facility 四个 GraphQL 子图；sim 如二期 GraphQL 化再追加
const { ApolloGateway, RemoteGraphQLDataSource } = require('@apollo/gateway');
const { ApolloServer } = require('@apollo/server');
const jwt = require('jsonwebtoken');

// 五个 hsf 服务的联邦入口：四个 GraphQL 子图（8091~8094）；可用 SUBGRAPHS env 覆盖（name=url 逗号分隔）
const SUBGRAPHS = (process.env.SUBGRAPHS || [
  'iam=http://iam-svc.hsf-prod.svc.cluster.local:8091/graphql',
  'land=http://land-svc.hsf-prod.svc.cluster.local:8092/graphql',
  'project=http://project-svc.hsf-prod.svc.cluster.local:8093/graphql',
  'facility=http://facility-svc.hsf-prod.svc.cluster.local:8094/graphql',
].join(',')).split(',').map(s => { const [name, url] = s.split('='); return { name, url }; });

const JWT_SECRET = process.env.JWT_SECRET || 'HsfJwt-2026-bN8wQ4vXzA6sD1fG';
// 限流模式开关（docs/07 §5.3 冻结）：svc=HTTP 调 ratelimit-svc（二期常态）；local=进程内桶回退
const RATE_MODE = (process.env.RATE_MODE || 'svc').toLowerCase();
// 限流判定服务（ratelimit-svc，CONVENTIONS-P2 T-2 冻结地址；生产由 manifest env RATE_SVC 注入）
const RATE_SVC = process.env.RATE_SVC || 'http://ratelimit-svc.hsf-prod.svc.cluster.local:8096';
// 超时冻结 500ms（docs/07 §5.2/§9-⑥）：同步限流检查在请求关键路径上，p95 预算 500ms 之下
// 检查预算应 ≤10%——同命名空间 RTT <5ms，500ms 已含 100× 裕度（workflow 任务文本 5s 按此收紧）
const RATE_TIMEOUT_MS = parseInt(process.env.RATE_TIMEOUT_MS || '500', 10) || 500;
// 令牌桶容量：仅 RATE_MODE=local 回退模式读取（docs/07 §5.2 env 冻结表）
const RATE_CAP = parseInt(process.env.RATE_CAP || '6000', 10) || 6000;

class AuthedDataSource extends RemoteGraphQLDataSource {
  willSendRequest({ request, context }) {
    // 剥离客户端伪造的身份头，仅注入网关校验过的身份（docs/05 §1.4 / FR-703）
    if (context.user) {
      request.http.headers.set('x-user-id', String(context.user.sub));
      // HTTP 头仅允许 ByteString，中文用户名必须 Base64 编码传输，子图 b64decode 失败回退明文（CONVENTIONS §2.7）
      request.http.headers.set('x-user-name', Buffer.from(context.user.name || '', 'utf8').toString('base64'));
      request.http.headers.set('x-user-roles', context.user.roles || '');
    } else {
      // 未登录：三个身份头一并清空，杜绝伪造的 x-user-name 透传到子图（V-25 验收项）
      request.http.headers.set('x-user-id', '');
      request.http.headers.set('x-user-name', '');
      request.http.headers.set('x-user-roles', '');
    }
  }
}

// ── RATE_MODE=svc：HTTP 调 ratelimit-svc /limit（全局固定窗口，跨副本共享）──────────
// 非 200 / 网络错误 / 超时一律 fail-open（放行），仅以节流 console.warn 记录（同 key 60s 一条，
// docs/07 §5.2）——限流是保护措施，其自身故障不允许放大为业务不可用（CONVENTIONS-P2 T-3）。
// 返回 null=判定不可用（放行）；{allowed}=服务侧已扣减配额。
const RATE_WARN_INTERVAL_MS = 60000;
const lastRateWarnAt = new Map();  // key → 上次告警时刻；超量时整体清空防内存增长（键空间小：u:{sub}/ip:{ip}）
async function checkRateLimit(key) {
  try {
    const res = await fetch(`${RATE_SVC}/limit?key=${encodeURIComponent(key)}&cost=1`,
                            { signal: AbortSignal.timeout(RATE_TIMEOUT_MS) });
    if (!res.ok) throw new Error(`ratelimit-svc HTTP ${res.status}`);
    const data = await res.json();
    if (!data || typeof data.allowed !== 'boolean') throw new Error('ratelimit-svc 响应缺少 allowed 字段');
    return data;
  } catch (e) {
    const now = Date.now(), last = lastRateWarnAt.get(key) || 0;
    if (now - last > RATE_WARN_INTERVAL_MS) {
      if (lastRateWarnAt.size > 5000) lastRateWarnAt.clear();
      lastRateWarnAt.set(key, now);
      console.warn(`[ratelimit] fail-open（判定不可用，放行）：${e.message} key=${key} svc=${RATE_SVC}`);
    }
    return null;
  }
}

// ── RATE_MODE=local：一期进程内令牌桶原实现（约 15 行，docs/07 §5.3 回退开关冻结保留）──
// 按 user.sub 或 ip 分桶：cap=RATE_CAP/min 为单实例口径，全局容量 = cap × 实例数（局部口径——
// 正因如此二期常态走 svc 模式；本路径仅供 ratelimit-svc/Redis 故障且 fail-open 不可接受时回退）
const buckets = new Map();
function rateLimitLocal(key) {
  const now = Date.now(), cap = RATE_CAP, refill = RATE_CAP / 60000;
  let b = buckets.get(key); if (!b) { b = { t: now, n: cap }; buckets.set(key, b); }
  b.n = Math.min(cap, b.n + (now - b.t) * refill); b.t = now;
  if (b.n < 1) return false; b.n -= 1; return true;
}

const gateway = new ApolloGateway({
  serviceList: SUBGRAPHS,
  buildService: ({ url }) => new AuthedDataSource({ url }),
  // 查询深度限制/APQ 一期不做（docs/05 §0 D-05-8），由 JWT+限流+内网受控覆盖威胁模型
  __exposeQueryPlanExperimental: false,
});

const server = new ApolloServer({
  gateway,
  introspection: process.env.INTROSPECTION !== 'false',
  formatError: (e) => {
    const ext = e.extensions || {};
    // INTERNAL* 前缀脱敏为固定文案（CONVENTIONS §2.6）；业务码 INVALID_TRANSITION/CODE_DUPLICATED 等原样透出（docs/05 D-05-4）
    if (ext.code && String(ext.code).startsWith('INTERNAL')) {
      return { message: '服务内部错误，请稍后重试', extensions: { code: 'UNAVAILABLE' } };
    }
    return { message: e.message, extensions: ext };
  },
});

(async () => {
  // 独立探针 HTTP server：业务 8090 是 GraphQL（对 /healthz 会返回 400），必须另起 9090（CONVENTIONS §2.5 真实事故）
  const http = require('http');
  let gatewayReady = false;
  http.createServer((req, res) => {
    if (req.url.startsWith('/healthz')) { res.writeHead(200, { 'content-type': 'text/plain' }); res.end('ok'); }
    else if (req.url.startsWith('/readyz')) { res.writeHead(gatewayReady ? 200 : 503, { 'content-type': 'text/plain' }); res.end(gatewayReady ? 'ok' : 'composing'); }
    else { res.writeHead(404); res.end(); }
  }).listen(9090, '0.0.0.0');

  // 限流/身份预中间件（express 层）：限流命中直接回 HTTP 429 + RATE_LIMITED（docs/06 P1 验收条款），
  // 不再经 Apollo 抛错（Apollo 上下文异常统一 500，无法表达 429）——V-25 复测缺陷修复。
  // 429 仅由限流判定 allowed=false 触发；svc 模式判定不可用（返回 null）时放行（fail-open，§5.2）。
  const express = require('express');
  const { expressMiddleware } = require('@apollo/server/express4');
  const app = express();
  const httpServer = http.createServer(app);
  let corsFn = null; try { corsFn = require('cors'); } catch (e) { /* cors 为可选增强 */ }
  if (corsFn) app.use(corsFn());
  app.use('/', async (req, res, next) => {
    const ip = (req.socket && req.socket.remoteAddress) || 'anon';
    const auth = req.headers.authorization || '';
    let user = null;
    if (auth.startsWith('Bearer ')) {
      try { user = jwt.verify(auth.slice(7), JWT_SECRET, { algorithms: ['HS256'] }); }
      catch { user = null; }
    }
    // 限流键口径不变（CONVENTIONS §2.6 / 05 §1.4）：登录按 user.sub、匿名按 ip 分键
    const key = user ? `u:${user.sub}` : `ip:${ip}`;
    let denied = false;
    if (RATE_MODE === 'local') {
      denied = !rateLimitLocal(key);              // 回退路径：进程内令牌桶（局部口径）
    } else {
      const verdict = await checkRateLimit(key);  // 二期常态：ratelimit-svc 全局固定窗口
      denied = verdict ? verdict.allowed === false : false;
    }
    if (denied) {
      res.status(429).set('content-type', 'application/json')
         .end(JSON.stringify({ errors: [{ message: '请求过于频繁，请稍后再试', extensions: { code: 'RATE_LIMITED' } }] }));
      return;
    }
    req.hsfUser = user;
    next();
  });

  app.use('/', express.json({ limit: '2mb' }));
  // 错误兜底：客户端提前断开（request aborted）等 body 错误按 400 处理，防进程级未捕获崩溃
  app.use((err, req, res, next) => {
    if (res.headersSent) return;
    res.status(400).set('content-type', 'application/json')
       .end(JSON.stringify({ errors: [{ message: '请求解析失败', extensions: { code: 'BAD_REQUEST' } }] }));
  });
  await server.start();
  app.use('/', expressMiddleware(server, {
    context: async ({ req }) => ({ user: req.hsfUser }),
  }));
  await new Promise((resolve) => httpServer.listen(8090, '0.0.0.0', resolve));
  gatewayReady = true;
  console.log(`HSF gateway ready at http://0.0.0.0:8090/, rate_mode=${RATE_MODE}` +
              (RATE_MODE === 'svc' ? ` svc=${RATE_SVC} (fail-open, timeout ${RATE_TIMEOUT_MS}ms)` : ` (in-process bucket, cap=${RATE_CAP}/min per instance)`) +
              `, subgraphs: ${SUBGRAPHS.map(s => s.name).join(',')}`);
})();
