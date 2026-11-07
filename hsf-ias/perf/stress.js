// HSF k6 负载/压力测试脚本（对 GraphQL 联邦网关 30099 + sim REST 31211）
// 场景：browse-读爬坡、report-写、tick-时序直写（绕网关直打 sim，压测前提——hsf/docs/05 §7）
// 运行: k6 run -e GATEWAY=http://10.167.2.175:30099/ stress.js
import http from 'k6/http';
import { check, sleep } from 'k6';

const GW = __ENV.GATEWAY || 'http://gw-svc.hsf-prod.svc.cluster.local:8090/';
const SIM = __ENV.SIM || 'http://sim-svc.hsf-prod.svc.cluster.local:8095';
const PARCELS = ['L5306000001', 'L5306000002', 'L5306000003', 'L5306000004',
                 'L5306000005', 'L5306000006', 'L5306000007', 'L5306000008'];
const VILLAGES = ['53060010101', '53060010102', '53060010103', '53060010201'];
// 每 VU 懒加载自己的令牌（登录放函数内，__VU 仅在请求上下文可用；module 级 let 按 VU 隔离）
let _tok = '';
function tokenOf() {
  if (_tok) return _tok;
  const seq = (__VU - 1) % 600 + 1;
  const phone = seq === 1 ? '13800000604' : '13800000' + String(604 + (seq - 1) % 15).slice(-2);
  const r = http.post(GW, JSON.stringify({ query: `mutation { login(phone: "${phone}", password: "hsf@2026") { token } }` }),
    { headers: { 'Content-Type': 'application/json' } });
  try { _tok = r.json().data.login.token; } catch (e) {}
  return _tok;
}
function hdr() {
  const t = tokenOf();
  return { 'Content-Type': 'application/json', ...(t ? { Authorization: 'Bearer ' + t } : {}) };
}

const BROWSE_Q = (c, v) => JSON.stringify({
  query: `{ landParcel(code:"${c}"){ code name holder { name } } landStats(villageCode:"${v}"){ total totalAreaMu } }`
});
const REPORT_Q = (f, n) => JSON.stringify({
  query: `mutation { reportWorkorder(facilityCode: "${f}", description: "k6-stress-${n}") { id sn state } }`
});

export const options = {
  scenarios: {
    browse: {  // 爬坡：100→200→400 VU
      executor: 'ramping-vus', startVUs: 10,
      stages: [
        { duration: '1m', target: 200 },
        { duration: '2m', target: 200 },
        { duration: '1m', target: 400 },
        { duration: '2m', target: 400 },
        { duration: '30s', target: 0 },
      ],
      exec: 'browse',
    },
    report: {  // 写：报修工单
      executor: 'constant-vus', vus: 30, duration: '4m', startTime: '1m', exec: 'report',
    },
    tick: {    // 时序直写：k6 直打 sim 31211（机器流量不走网关，05 号 §7）
      executor: 'constant-vus', vus: 10, duration: '4m', startTime: '1m', exec: 'tick',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    // 读场景 p95 目标 < 800ms；写与时序直写宽松
    'http_req_duration{scenario:browse}': ['p(95)<800'],
  },
};

export function browse() {
  const r = http.post(GW, BROWSE_Q(PARCELS[__ITER % 8], VILLAGES[__ITER % 4]), { headers: hdr() });
  check(r, { '200': (r) => r.status === 200 && !r.body.includes('errors') });
  sleep(0.3);
}

export function report() {
  const r = http.post(GW, REPORT_Q('F5306000001', __ITER), { headers: hdr() });
  check(r, { 'reported': (r) => r.status === 200 && !r.body.includes('errors') });
  sleep(1);
}

export function tick() {
  // tick 契约（05 号 §7.3）：每步 48 台×1 条，INSERT 1000/批；steps 越大单请求写入越多
  const r = http.post(`${SIM}/sim/tick?steps=20`, null, { headers: hdr() });
  check(r, { 'tick-200': (r) => r.status === 200 });
  sleep(1);
}
