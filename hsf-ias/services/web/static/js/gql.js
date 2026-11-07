// HSF 前端公共查询封装（三页共用；纯原生实现，零外网依赖）
// 网关默认同源（nginx /graphql 反代，端口无关）；直连可用 localStorage.hsf_gw='http://<node>:30099' 覆盖
const GW = localStorage.getItem('hsf_gw') ?? '';
// sim REST 默认走 web 同源代理（nginx /sim/ 反代，免跨域）；覆盖为 http://<node>:31211 可直连（需 sim 开跨域头）
const SIM = localStorage.getItem('hsf_sim') ?? '/sim';
const TOK = () => localStorage.getItem('hsf_token') || '';

// GraphQL 查询：业务失败抛 Error（message=中文文案，err.code=extensions.code）
async function gql(query, variables = {}) {
  const r = await fetch(GW + '/graphql', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(TOK() ? { Authorization: 'Bearer ' + TOK() } : {}) },
    body: JSON.stringify({ query, variables }),
  });
  const j = await r.json();
  if (j.errors) {
    const e = new Error(j.errors[0].message);
    e.code = (j.errors[0].extensions || {}).code;
    throw e;
  }
  return j.data;
}

// sim REST GET（如 sim('/alerts',{state:'OPEN'})）：非 2xx 抛 Error
async function sim(path, params = {}) {
  const qs = Object.entries(params).filter(([, v]) => v !== '' && v !== null && v !== undefined)
    .map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
  const r = await fetch(SIM + path + (qs ? '?' + qs : ''), { headers: TOK() ? { Authorization: 'Bearer ' + TOK() } : {} });
  if (!r.ok) throw new Error('sim HTTP ' + r.status);
  return r.json();
}

// 登录并持久化（HSF 演示口令 hsf@2026；错密 → UNAUTHENTICATED）
async function login(phone, password) {
  const d = await gql(`mutation($p:String!,$w:String!){login(phone:$p,password:$w){token name roles partyId}}`, { p: phone, w: password });
  localStorage.setItem('hsf_token', d.login.token);
  localStorage.setItem('hsf_name', d.login.name);
  return d.login;
}
function logout() { localStorage.removeItem('hsf_token'); localStorage.removeItem('hsf_name'); location.reload(); }

// HTML 转义（工单描述等用户输入防注入）
function esc(s) { return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

// ── 种子与枚举中文映射（docs/04 §9.1、docs/05 §4~§7 冻结值）──
const VILLAGES = [['53060010101', '清溪村'], ['53060010102', '麦香村'], ['53060010103', '柳林村'], ['53060010201', '稻花村']];
const vName = c => ((VILLAGES.find(x => x[0] === c) || ['', ''])[1]);
const PROJECT_STATE = { RESERVED: '储备', SUBMITTED: '申报', APPROVED: '审批', CONSTRUCTING: '施工', SUPERVISED: '监理', ACCEPTED: '验收', PERFORMED: '绩效' };
const WORKORDER_STATE = { OPEN: '待受理', ACCEPTED: '已受理', DISPATCHED: '已派单', RESOLVED: '待销项', APPROVED: '已销项' };
const FACILITY_TYPE = { WELL: '机井', PUMP_STATION: '泵站', CANAL: '渠道', ROAD: '道路' };
const FACILITY_STATUS = { NORMAL: '正常', BROKEN: '损坏', REPAIRING: '维修中', RETIRED: '停用' };
const RULE_NAME = { RULE_DROUGHT: '旱情', RULE_WATERLOGGING: '渍害', RULE_GALE: '大风', RULE_PEST: '虫害' };
const ALERT_STATE = { OPEN: '未处置', PROCESSING: '处置中', CLOSED: '已关闭' };
const METRIC_NAME = { soil_moisture: '墒情%', air_temp: '气温℃', wind_speed: '风速m/s', rainfall: '雨量mm', pest_index: '虫情', water_level: '水位m' };
