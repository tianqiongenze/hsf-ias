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
  // 三期 rs 子图（docs/08 §2.4/§6：rsVigor/rsVigorLatest 顶层查询经 gw 组合）
  'rs=http://rs-svc.hsf-prod.svc.cluster.local:8098/graphql',
].join(',')).split(',').map(s => { const [name, url] = s.split('='); return { name, url }; });

// R-07（2026-11-14）：JWT_SECRET 外置 fail-closed——env 缺失启动即失败（fail fast），禁止源码内置回退密钥；
// 值由 20-services.yaml secretKeyRef JWT_SECRET 注入（prod/dev 分值，scripts/gen-secrets.sh 部署期生成）
const JWT_SECRET = process.env.JWT_SECRET;
if (!JWT_SECRET) { console.error('FATAL: env JWT_SECRET 未设置（R-07 fail-closed，禁止内置回退）'); process.exit(1); }
// R-31（2026-11-14，ADR-HSF-027）：JWT 验签算法演进——HS256 冻结兼容开关 / RS256 非对称签名。
// JWT_ALG=HS256（默认）→ 冻结路径逐字节不变；JWT_ALG=RS256 → 公钥验签（私钥不出 iam），
// 按 token 头 kid 选公钥支持轮换（当前公钥 JWT_PUBLIC_KEY + 轮换期旧公钥 JWT_PUBLIC_KEY_PREV）。
// 密钥经 hsf-secrets 注入，值=PEM 的 base64 单行（kubectl --from-env-file 不接受多行——
// gen-secrets.sh 生成口径）；伪造 HS256 共享密钥令牌在 RS256 模式被 algorithms 白名单拒绝。
const JWT_ALG = (process.env.JWT_ALG || 'HS256').toUpperCase();
const JWT_KID = process.env.JWT_KID || 'hsf-jwt-1';
const JWT_KID_PREV = process.env.JWT_KID_PREV || '';
const b64pem = (v) => (v ? Buffer.from(v, 'base64') : null);      // base64(PEM) → PEM buffer
const JWT_PUBLIC_KEY = b64pem(process.env.JWT_PUBLIC_KEY);
const JWT_PUBLIC_KEY_PREV = b64pem(process.env.JWT_PUBLIC_KEY_PREV);
if (JWT_ALG === 'RS256' && !JWT_PUBLIC_KEY) {
  console.error('FATAL: JWT_ALG=RS256 但 JWT_PUBLIC_KEY 未注入（R-31 fail-closed，禁止回退对称密钥验签）');
  process.exit(1);
}

// JWT 验签统一入口（中间件与功能测试共用）：HS256 → JWT_SECRET 对称验签（冻结路径）；
// RS256 → kid 选择公钥（kid=JWT_KID_PREV 且旧公钥在位 → 旧钥；缺 kid/未知 kid → 当前公钥）
// 后 RS256 白名单验签。任何异常由调用方按"未登录"处理（401/无身份头注入，语义不变）。
function verifyJwt(token) {
  if (JWT_ALG !== 'RS256') {
    return jwt.verify(token, JWT_SECRET, { algorithms: ['HS256'] });
  }
  let kid = '';
  try {
    const h = jwt.decode(token, { complete: true });
    kid = (h && h.header && h.header.kid) || '';
  } catch (e) { kid = ''; }
  const key = (kid && JWT_KID_PREV && kid === JWT_KID_PREV && JWT_PUBLIC_KEY_PREV)
    ? JWT_PUBLIC_KEY_PREV : JWT_PUBLIC_KEY;
  return jwt.verify(token, key, { algorithms: ['RS256'] });
}
// 限流模式开关（docs/07 §5.3 冻结）：svc=HTTP 调 ratelimit-svc（二期常态）；local=进程内桶回退
const RATE_MODE = (process.env.RATE_MODE || 'svc').toLowerCase();
// 限流判定服务（ratelimit-svc，CONVENTIONS-P2 T-2 冻结地址；生产由 manifest env RATE_SVC 注入）
const RATE_SVC = process.env.RATE_SVC || 'http://ratelimit-svc.hsf-prod.svc.cluster.local:8096';
// 超时冻结 500ms（docs/07 §5.2/§9-⑥）：同步限流检查在请求关键路径上，p95 预算 500ms 之下
// 检查预算应 ≤10%——同命名空间 RTT <5ms，500ms 已含 100× 裕度（workflow 任务文本 5s 按此收紧）
const RATE_TIMEOUT_MS = parseInt(process.env.RATE_TIMEOUT_MS || '500', 10) || 500;
// 令牌桶容量：仅 RATE_MODE=local 回退模式读取（docs/07 §5.2 env 冻结表）
const RATE_CAP = parseInt(process.env.RATE_CAP || '6000', 10) || 6000;

// ── R-26（2026-11-14，ADR-HSF-026）：查询深度/复杂度护栏 ─────────────────────────
// 推翻 docs/05 §0 D-05-8"深度/复杂度限制一期不做"（原由 JWT+限流+内网受控覆盖威胁模型；
// 对标③要求叠加操作限制）——阈值口径 docs/24 §4 R-26：恶意深查询 20 层 → 4xx 拒绝，
// 现有业务查询形态全通过（E2E 25/25 为回归证据）。默认 QUERY_DEPTH_MAX=10（业务查询实测
// 最大 6 层，裕度 1.6×+；20 层必拒）；QUERY_FIELD_COUNT_MAX=300 粗粒度复杂度护栏（总可选
// 字段数，含片段展开，按现有查询形态调参）。回退开关：QUERY_GUARD=off（docs/24 R-26
// 风险行"阈值过严误伤报表查询——灰度 env 开关；回退=开关关闭"）；两阈值设 0 同效关闭。
const QUERY_GUARD = (process.env.QUERY_GUARD || 'on').toLowerCase() !== 'off';
const QUERY_DEPTH_MAX = parseInt(process.env.QUERY_DEPTH_MAX || '10', 10) || 10;
const QUERY_FIELD_COUNT_MAX = parseInt(process.env.QUERY_FIELD_COUNT_MAX || '300', 10) || 300;

// 深度/字段数计算（graphql-js parse AST；片段展开计数、环防护）：
// 深度口径=字段嵌套层数（{a{b{c}}} depth=3；片段按其定义展开取最大，环引用跳过防死循环）；
// 字段数=全部 Field 节点计数（片段被多处 spread 时按展开处重复计——保守口径）。
// 语法错误不在此拦截（交 Apollo 原生 GRAPHQL_PARSE_FAILED，避免双重处理）。
function queryDepthAndFieldCount(query) {
  const { parse } = require('graphql');
  const doc = parse(query);
  const frags = {};
  for (const d of doc.definitions) {
    if (d.kind === 'FragmentDefinition') frags[d.name.value] = d.selectionSet;
  }
  let fields = 0;
  const depthOf = (selSet, seen) => {
    if (!selSet || !Array.isArray(selSet.selections)) return 0;
    let max = 0;
    for (const sel of selSet.selections) {
      if (sel.kind === 'Field') {
        fields += 1;
        max = Math.max(max, 1 + depthOf(sel.selectionSet, seen));
      } else if (sel.kind === 'InlineFragment') {
        max = Math.max(max, depthOf(sel.selectionSet, seen));
      } else if (sel.kind === 'FragmentSpread') {
        const name = sel.name && sel.name.value;
        if (!name || seen.has(name)) continue;           // 片段环/自引用防护
        const f = frags[name];
        if (f) { seen.add(name); max = Math.max(max, depthOf(f, seen)); seen.delete(name); }
      }
    }
    return max;
  };
  let depth = 0;
  // 深度/字段数均只从操作（query/mutation）根起算——片段按 spread 处内联展开计入
  // （片段定义体不在 FragmentDefinition 处重复计数；未被引用的片段定义不贡献深度/字段数）
  for (const d of doc.definitions) {
    if (d.kind === 'OperationDefinition') depth = Math.max(depth, depthOf(d.selectionSet, new Set()));
  }
  return { depth, fields };
}

// 查询护栏判定：违规返回提示文案（4xx 拒绝），合规/关闭/不可解析返回 null（放行）
function queryGuardViolation(query) {
  if (!QUERY_GUARD || typeof query !== 'string' || !query.trim()) return null;
  let m;
  try { m = queryDepthAndFieldCount(query); } catch (e) { return null; }  // 语法错误交 Apollo
  if (m.depth > QUERY_DEPTH_MAX) {
    return `查询深度 ${m.depth} 超过上限 ${QUERY_DEPTH_MAX}，请拆分查询`;
  }
  if (m.fields > QUERY_FIELD_COUNT_MAX) {
    return `查询字段数 ${m.fields} 超过上限 ${QUERY_FIELD_COUNT_MAX}，请精简选择集`;
  }
  return null;
}

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
  // 查询深度/复杂度限制：一期 D-05-8 明确不做 → R-26（2026-11-14，ADR-HSF-026）推翻——
  // express 层护栏见上方 queryGuardViolation（docs/05 §1.4 随 ADR 增补）；APQ 仍不做（不变）
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
      try { user = verifyJwt(auth.slice(7)); }   // R-31：统一验签入口（JWT_ALG 切换，见上方）
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
  // R-26 查询深度/复杂度护栏（express 层，GraphQL 解析前）：违规 → 400 + QUERY_TOO_DEEP
  // （D-05-2 错误形态一致：HTTP 4xx + errors[0].extensions.code）；QUERY_GUARD=off 整体关闭。
  // 语法错误在 queryGuardViolation 内放行（交 Apollo 原生解析错误，不双重处理）。
  app.use('/', (req, res, next) => {
    if (!QUERY_GUARD || req.method !== 'POST' || !req.body) return next();
    const queries = Array.isArray(req.body) ? req.body.map(o => o && o.query)   // 批量操作逐条判定
                                            : [req.body.query];
    for (const q of queries) {
      const violation = queryGuardViolation(q);
      if (violation) {
        res.status(400).set('content-type', 'application/json')
           .end(JSON.stringify({ errors: [{ message: violation, extensions: { code: 'QUERY_TOO_DEEP' } }] }));
        return;
      }
    }
    next();
  });
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
