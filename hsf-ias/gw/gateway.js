// HSF GraphQL 联邦网关（Apollo Gateway）——移植自 dv-ias/gw/gateway.js
// 职责：超图组合、JWT 校验与用户头注入（防伪造）、限流（简易令牌桶）、CORS、健康检查
// 端口：业务 8090（GraphQL）+ 独立探针 9090（/healthz，CONVENTIONS §2.5）
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
// 限流令牌桶容量（req/min），env RATE_CAP 可调，默认 6000（CONVENTIONS §2.6；600/min 卡死单核压测为 dv-ias 教训）
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

// 令牌桶限流（进程内，按 user.sub 或 ip 分桶）：cap=RATE_CAP/min 为单实例口径，
// 全局容量 = cap × 实例数（一期决策：ioredis Cluster 对 hostname 广播集群存在刷新循环，
// 详见 docs/20 §5.3 与二期 ADR；gw 实例数 3-4，全局 3600-4800/min）
const buckets = new Map();
function rateLimit(key) {
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
  // 不再经 Apollo 抛错（Apollo 上下文异常统一 500，无法表达 429）——V-25 复测缺陷修复
  const express = require('express');
  const { expressMiddleware } = require('@apollo/server/express4');
  const app = express();
  const httpServer = http.createServer(app);
  let corsFn = null; try { corsFn = require('cors'); } catch (e) { /* cors 为可选增强 */ }
  if (corsFn) app.use(corsFn());
  app.use('/', (req, res, next) => {
    const ip = (req.socket && req.socket.remoteAddress) || 'anon';
    const auth = req.headers.authorization || '';
    let user = null;
    if (auth.startsWith('Bearer ')) {
      try { user = jwt.verify(auth.slice(7), JWT_SECRET, { algorithms: ['HS256'] }); }
      catch { user = null; }
    }
    const key = user ? `u:${user.sub}` : `ip:${ip}`;
    if (!rateLimit(key)) {
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
  console.log(`HSF gateway ready at http://0.0.0.0:8090/, rate_cap=${RATE_CAP}/min, subgraphs: ${SUBGRAPHS.map(s => s.name).join(',')}`);
})();
