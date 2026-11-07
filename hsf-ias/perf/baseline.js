// HSF k6 基线压测（ browse 200VU + report 20VU，阈值对齐 DV-IAS：CONVENTIONS §4、hsf/docs/06 §8）
// 场景：browse=地块/一张图公开读（含联邦跨域 holder 解析，压网关组合）；report=设施报修（登录+写）
// 运行：k6 run -e GATEWAY=http://gw-svc.hsf-prod.svc.cluster.local:8090/ baseline.js
import http from 'k6/http';
import { check, sleep } from 'k6';

const GATEWAY = __ENV.GATEWAY || 'http://gw-svc.hsf-prod.svc.cluster.local:8090/';

export const options = {
  scenarios: {
    browse: {  // S1 读路径：地块详情+权属联邦解析（landParcel 公开查询）
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

export function browse() {
  const code = PARCELS[__ITER % 8];
  const r = http.post(GATEWAY, JSON.stringify({
    query: `{ landParcel(code:"${code}"){ code name areaMu holder { name } } }`,
  }), { headers: { 'Content-Type': 'application/json' } });
  check(r, { 'browse-200': (res) => res.status === 200 });
  sleep(1 + Math.random() * 3);
}

// 每 VU 登录一次（负载模型：真实用户登录一次后持续操作；SM3-PBKDF2 每次约 100ms 纯 CPU，
// 逐迭代登录会把 iam 打成 CPU 饱和并触发 HPA 扩容抖动——docs/20 §3.4 复测定位的 p95 尾部主因）
const VU_TOKEN = (() => {
  const login = http.post(GATEWAY, JSON.stringify({
    query: 'mutation($p:String!,$w:String!){login(phone:$p,password:$w){token}}',
    variables: { p: '13800000604', w: 'hsf@2026' },
  }), { headers: { 'Content-Type': 'application/json' } });
  try { return login.json('data.login.token'); } catch (e) { return ''; }
})();

export function report() {
  const token = VU_TOKEN;
  check(token, { 'login-ok': () => !!token });
  if (!token) { sleep(1); return; }
  const r = http.post(GATEWAY, JSON.stringify({
    query: 'mutation($f:String!,$d:String!){reportWorkorder(facilityCode:$f,description:$d){id sn state}}',
    variables: { f: 'F5306000001', d: 'k6基线报修' },
  }), { headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` } });
  check(r, { 'report-200': (res) => res.status === 200 });
  sleep(2);
}
