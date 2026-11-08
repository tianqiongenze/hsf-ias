// HSF k6 基线压测（ browse 200VU + report 20VU，阈值对齐 DV-IAS：CONVENTIONS §4、hsf/docs/06 §8）
// 场景：browse=地块/一张图读（含联邦跨域 holder 解析，压网关组合）；report=设施报修（登录+写）
// 运行：k6 run -e GATEWAY=http://gw-svc.hsf-prod.svc.cluster.local:8090/ baseline.js
//
// 二期限流适配（docs/07 §6.4 冻结设计）：RATE_MODE=svc 固定窗口 1200/min/key 下，
//   无登录读流量全部聚合在单个 ip: 键（200 VU ≈4800/min）必触发 429——browse 场景改为
//   每 VU 登录一次（login-once，docs/20 R-2 负载模型），登录号循环取自 04 §9.1 冻结的
//   15 个种子账号（13800000601~610 / 13900000901~905，不新增账号）：200 browse VU 共享
//   15 个 u: 键（每键 ≈13 VU × 17~24 req/min ≈ 230~320/min < 1200）不受影响；
//   report 场景同步按冻结号码表取号（废除一期单账号 604 与 %600 虚拟池缺陷）。
import http from 'k6/http';
import { check, sleep } from 'k6';

const GATEWAY = __ENV.GATEWAY || 'http://gw-svc.hsf-prod.svc.cluster.local:8090/';

export const options = {
  scenarios: {
    browse: {  // S1 读路径：地块详情+权属联邦解析（登录态，限流键=账号）
      executor: 'ramping-vus',
      startVUs: 10, stages: [
        { duration: '30s', target: 200 },
        { duration: '1m', target: 200 },
        { duration: '15s', target: 0 },
      ],
      exec: 'browse',
    },
    report: {  // S2 写路径：报修工单上报（任意登录角色）
      executor: 'constant-vus',
      vus: 20, duration: '1m', startTime: '30s', exec: 'report',
    },
  },
  thresholds: {  // DV-IAS 同款阈值
    'http_req_duration{scenario:browse}': ['p(95)<500'],
    'http_req_duration{scenario:report}': ['p(95)<800'],
    'http_req_failed': ['rate<0.05'],
  },
};

const PARCELS = ['L5306000001', 'L5306000002', 'L5306000003', 'L5306000004',
                 'L5306000005', 'L5306000006', 'L5306000007', 'L5306000008'];  // 种子 8 地块（04 号 §2.3）

// 04 §9.1 冻结 15 个种子账号（docs/07 §6.4：修 stress.js 号码模板缺陷的同一张表）
const PHONES = ['13800000601', '13800000602', '13800000603', '13800000604', '13800000605',
                '13800000606', '13800000607', '13800000608', '13800000609', '13800000610',
                '13900000901', '13900000902', '13900000903', '13900000904', '13900000905'];

// 每 VU 模块级变量独立（k6 per-VU isolate）：首迭代 login-once，此后复用 token；
// __VU 仅请求上下文可用（docs/14 坑 S5）——登录在 exec 函数内执行，不在 init 上下文。
let TOKEN = '';

function loginOnce() {
  if (TOKEN) return TOKEN;
  const phone = PHONES[__VU % PHONES.length];
  const login = http.post(GATEWAY, JSON.stringify({
    query: 'mutation($p:String!,$w:String!){login(phone:$p,password:$w){token}}',
    variables: { p: phone, w: 'hsf@2026' },
  }), { headers: { 'Content-Type': 'application/json' } });
  try { TOKEN = login.json('data.login.token') || ''; } catch (e) { TOKEN = ''; }
  return TOKEN;
}

export function browse() {
  const token = loginOnce();
  check(token, { 'login-ok': () => !!token });
  const code = PARCELS[__ITER % 8];
  const r = http.post(GATEWAY, JSON.stringify({
    query: `{ landParcel(code:"${code}"){ code name areaMu holder { name } } }`,
  }), { headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` } });
  check(r, { 'browse-200': (res) => res.status === 200 });
  sleep(1 + Math.random() * 3);
}

export function report() {
  const token = loginOnce();
  check(token, { 'login-ok': () => !!token });
  if (!token) { sleep(1); return; }
  const r = http.post(GATEWAY, JSON.stringify({
    query: 'mutation($f:String!,$d:String!){reportWorkorder(facilityCode:$f,description:$d){id sn state}}',
    variables: { f: 'F5306000001', d: 'k6基线报修' },
  }), { headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` } });
  check(r, { 'report-200': (res) => res.status === 200 });
  sleep(2);
}
