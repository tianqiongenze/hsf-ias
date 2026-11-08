// HSF k6 负载/压力测试脚本（对 GraphQL 联邦网关 30099 + sim REST 31211 + iot 8097）
// 场景：browse-读爬坡、report-写、tick-时序直写（绕网关直打 sim，压测前提——hsf/docs/05 §7）；
//       iot-write（二期，docs/07 §6.3 冻结）：HTTP 触发 sim tick 经 MQTT→iot 缓冲→TimescaleDB 落库，
//       单独报告**不进 thresholds**——独立运行：k6 run -e SCENARIO=iot-write stress.js
// 运行: k6 run -e GATEWAY=http://10.167.2.175:30099/ stress.js
//       k6 run -e SCENARIO=iot-write -e SIM=http://10.167.2.175:31211 stress.js
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter } from 'k6/metrics';

const GW = __ENV.GATEWAY || 'http://gw-svc.hsf-prod.svc.cluster.local:8090/';
const SIM = __ENV.SIM || 'http://sim-svc.hsf-prod.svc.cluster.local:8095';
const IOT = __ENV.IOT || 'http://iot-svc.hsf-prod.svc.cluster.local:8097';
const PARCELS = ['L5306000001', 'L5306000002', 'L5306000003', 'L5306000004',
                 'L5306000005', 'L5306000006', 'L5306000007', 'L5306000008'];
const VILLAGES = ['53060010101', '53060010102', '53060010103', '53060010201'];
// 登录号按 04 §9.1 冻结的 15 个种子账号取号——修复一期 '13800000'+2 位模板生成 10 位错号、
// 仅 13800000604 合法的缺陷（docs/07 §6.4，废除 %600 虚拟池，不新增种子账号）
const PHONES = ['13800000601', '13800000602', '13800000603', '13800000604', '13800000605',
                '13800000606', '13800000607', '13800000608', '13800000609', '13800000610',
                '13900000901', '13900000902', '13900000903', '13900000904', '13900000905'];
// 每 VU 懒加载自己的令牌（登录放函数内，__VU 仅在请求上下文可用；module 级 let 按 VU 隔离）
let _tok = '';
function tokenOf() {
  if (_tok) return _tok;
  const phone = PHONES[(__VU - 1) % PHONES.length];
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

// ── 二期 iot-write 度量（§6.3：Trend 记 tick 时延，Counter 记发布条数；吞吐=Δwritten/elapsed）──
const IOT_WRITE = __ENV.SCENARIO === 'iot-write';
const iotTickDur = new Trend('iot_tick_duration', true);
const iotPublished = new Counter('iot_published');
// iot /metrics 文本中解析 hsf_iot_readings_written_total（写侧落库累计，全链路口径）
function iotWritten() {
  const r = http.get(`${IOT}/metrics`);
  const m = /hsf_iot_readings_written_total\s+([0-9.]+)/.exec(r.body || '');
  return m ? parseFloat(m[1]) : -1;
}

export const options = IOT_WRITE ? {
  scenarios: {
    'iot-write': {  // 4 VU × 每 VU 50 次迭代 × steps=500（单迭代 2.4 万条，总量 480 万条，§3.6 口径）
      executor: 'per-vu-iterations', vus: 4, iterations: 50, exec: 'iotWrite',
    },
  },
  // 冻结：不设 thresholds——iot-write 数值只写入 docs/21 报告，不参与判定（T-2）
} : {
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

// setup：记录 iot 落库计数基线（供吞吐=Δ/elapsed；全链路 sim 生成→MQTT→iot 缓冲→TimescaleDB）
export function setup() {
  if (!IOT_WRITE) return { t: Date.now(), written: 0 };
  return { t: Date.now(), written: iotWritten() };
}

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

// iot-write：POST /sim/tick?steps=500 循环（不顶格 TICK_MAX_STEPS=5000，留 10× 余量，§6.3）；
// 单迭代 2.4 万条 QoS1 同步发布 ≈2.4~6s（inflight=100），timeout 120s 兜底；迭代间 sleep 5s 排缓冲
export function iotWrite() {
  const t0 = Date.now();
  const r = http.post(`${SIM}/sim/tick?steps=500`, null, { headers: hdr(), timeout: '120s' });
  iotTickDur.add(Date.now() - t0);
  const ok = check(r, { 'tick-200': (r) => r.status === 200 });
  try {
    const j = r.json();
    iotPublished.add(j.published != null ? j.published : (j.inserted != null ? j.inserted : 0));
  } catch (e) {
    if (ok) iotPublished.add(24000);   // sim:2.0 契约=published（docs/07 §9-⑤）；兜底按 steps×48 计
  }
  sleep(5);
}

// teardown：读 iot /metrics 落库终值并输出吞吐报告（teardown 允许 HTTP；handleSummary 会吞掉
// k6 默认汇总且不同镜像版本能力不一，故选 teardown+console.log——两模式均保留默认 end-of-test 汇总）
export function teardown(data) {
  if (!IOT_WRITE) return;
  const base = data && data.written != null ? data : { t: Date.now(), written: 0 };
  const end = { t: Date.now(), written: iotWritten() };
  const el = Math.max((end.t - base.t) / 1000, 1);
  const dw = Math.max(end.written - base.written, 0);
  console.log(`
═══ iot-write 时序吞吐报告（docs/07 §6.3：单独记录，不进 thresholds）═══
  执行时长            ：${el.toFixed(1)} s
  TimescaleDB 落库    ：${dw} 条（iot /metrics hsf_iot_readings_written_total 差值：${base.written} → ${end.written}）
  全链路吞吐          ：${(dw / el).toFixed(1)} 条/s（sim 生成→MQTT→iot 缓冲→TimescaleDB）
  发布条数            ：见 Counter iot_published（sim:2.0 tick 响应 published；单迭代 500×48=2.4 万条）
  参考目标            ：≥5000 条/s（观测记录，无硬阈值；判读另见 hsf_iot_flush_seconds p95 与 onconflict_skipped≈0）
`);
}
