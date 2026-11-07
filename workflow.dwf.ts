// HSF 高标准农田信息平台 —— 独立分析/设计/实现/测试/部署 全流程工作流
// 目标：达到 DV-IAS 同等工业级标准（docs/18 号文档的"独立立项+共享底座"决策落地）

interface DocsOut {
  /** 实际写出的文件路径（工作区相对）。 */
  files: string[];
  /** 三句话以内：交付了什么、关键设计决策。 */
  summary: string;
}

interface ImplOut {
  /** 实际写出的文件路径（工作区相对）。 */
  files: string[];
  /** 两句话以内：实现了什么、自检结果。 */
  summary: string;
}

interface DesignReview {
  /** 需要修订的问题清单；空数组表示通过。 */
  issues: string[];
}

interface DeployOut {
  /** 各服务上线状态。 */
  services: { name: string; status: string; note: string }[];
  /** 网关 __typename 是否 200。 */
  gatewayOk: boolean;
  /** 联邦跨域查询是否取回真实种子数据。 */
  federationOk: boolean;
  /** 验证证据：命令与关键输出（每条一句话+输出摘要）。 */
  evidence: string[];
  /** 未能解决的问题（可为空）。 */
  remainingIssues: string[];
}

interface VerifyOut {
  /** 融合测试报告路径。 */
  reportPath: string;
  /** k6 一句话结论（请求数/失败率/p95）。 */
  k6Summary: string;
  /** E2E 一句话结论（用例数/通过数）。 */
  e2eSummary: string;
  /** 证据：命令与关键输出摘要。 */
  evidence: string[];
}

interface Finding {
  /** 位置："hsf-ias/services/land/app/schema.py:32" 或 "hsf/docs/20-融合测试报告.md"。 */
  where: string;
  /** 一句话：问题或结论。 */
  what: string;
  /** 证据：读到的行或执行的命令与输出摘要。 */
  evidence: string;
  /** verified=独立复核确认；unconfirmed=未能复现/未确认。 */
  status: "verified" | "unconfirmed";
  /** high 保留给宕机/数据丢失/错误结果。 */
  severity: "low" | "medium" | "high";
}

interface WorkflowReport {
  conclusion: string;
  findings: Finding[];
  verified: string[];
  notCovered: string[];
}

// 服务上线状态表（部署阶段逐行喂入）
artifact.table("svc", {
  title: "HSF 服务上线状态",
  key: "name",
  columns: [
    { field: "name", label: "服务" },
    { field: "status", label: "状态" },
    { field: "note", label: "备注" },
  ],
});

phase("独立分析：需求规格与对标基线");
const analysis = await agent("需求分析师", {
  system:
    "你是农业信息化领域的高级需求分析师。先读 D:\\projects\\数字乡村\\hsf\\CONVENTIONS.md 与 " +
    "D:\\projects\\数字乡村\\docs\\18-高标准农田平台融合性分析与决策建议.md、docs\\00 与 docs\\01 作为规格书写范本。" +
    "产出必须可直接指导设计与编码：每个功能给出验收标准，非功能给出量化指标（并发/时延/数据量/可用性）。" +
    "禁止编造无法验证的指标。若资料不足，按最保守口径写并标注假设。",
}).ask<DocsOut>(
  "为 HSF 高标准农田信息平台编写两份文档并保存：\n" +
    "1) hsf/docs/00-方案总览与确认清单.md：项目一句话定位、原文章（高标准农田信息平台'五横三纵'）能力清单与本平台范围映射、" +
    "与 DV-IAS 的复用边界（共享底座清单）、关键技术决策一页纸、确认事项 C1~C6（默认值已定：ns=hsf-prod/hsf-dev、TiDB 三库 hsf_md/hsf_biz/hsf_ts、" +
    "自建 hsf-redis 3 节点、一期范围=统一编码+一张图+项目全周期+设施管护+IoT模拟告警、Monorepo=hsf/hsf-ias）。\n" +
    "2) hsf/docs/01-需求规格说明书.md：用户角色（县农业农村局/乡镇/村/管护主体/合作社）、功能需求（编号 FR-xx，覆盖：统一编码与地块主数据、" +
    "一张图展示、项目全周期状态机、设施台账与二维码巡检工单、墒情/气象/虫情时序与阈值告警、移动端上报、权限与审计）、" +
    "非功能需求（编号 NFR-xx：在线≥5万、读 p95≤500ms、写 p95≤800ms、时序写入≥1万条/s、可用性 99.9%、等保三级+国密）。\n" +
    "全部中文。完成后返回文件清单与摘要。",
);

phase("总体设计与详细设计（三路并行）");
const designs = await Promise.all(
  [
    {
      name: "架构设计师",
      task:
        "编写 hsf/docs/02-技术选型与总体架构.md 与 hsf/docs/03-DDD设计与微服务详细设计.md。" +
        "选型：延续 DV-IAS（K8s+GraphQL Federation+Strawberry+TiDB+Redis 多级缓存），新增决策须写 ADR 式理由：" +
        "GIS 采用 TiDB 存储 GeoJSON + 纯 SVG/OpenLayers 前端渲染（一期不引入 PostGIS/GeoServer，理由：集群无 PostGIS、降低有状态服务，二期再评估）；" +
        "时序用 TiDB hsf_ts（不引入 TimescaleDB，理由同上）。总体架构画'层×域'文字图。DDD：5 个限界上下文（iam/land/project/facility/sim）" +
        "各给出聚合、实体、领域事件、状态机（项目全周期 7 态、巡检工单 4 态、告警 3 态），跨上下文协作用事件+联邦引用。",
      ref: "docs/02、docs/03、docs/04",
    },
    {
      name: "数据架构师",
      task:
        "编写 hsf/docs/04-数据架构与多级缓存.md 与 hsf/docs/05-GraphQL接口与集成设计.md。" +
        "数据：hsf_md（org/party/land_parcel/facility/project 主数据，含统一编码规则：地块 L+6位区划+4位序号 等）、" +
        "hsf_biz（farm_project/facility_workorder/alert_record/sensor 时序归档）、hsf_ts（sensor_reading），" +
        "给出完整 DDL 与索引设计；多级缓存键设计与 TTL 策略（org 树/一张图要素/排行榜类）；读写分离双账号。" +
        "接口：全部 SDL（类型+mutation+@key 实体声明），每个子图一节，含错误码与分页规约。",
      ref: "docs/05、docs/06",
    },
    {
      name: "交付计划师",
      task:
        "编写 hsf/docs/06-实施与验收计划.md：迭代划分（迭代0 环境+底座 / 迭代1 一期功能 / 迭代2 测试验收）、" +
        "与 DV-IAS 对齐的验收口径（引用 docs/13 与 docs/15 的 k6+E2E 双工具标准）、风险清单（含 DV-IAS 踩过的 12 个坑的预防措施，读 docs/14 §五）、" +
        "资源规划（命名空间/配额/端口 30090/31210/31211）。",
      ref: "docs/12、docs/13、docs/14、docs/15",
    },
  ].map((w) =>
    agent(w.name, {
      system:
        "你是资深架构师/数据架构师/交付经理。必须先读 D:\\projects\\数字乡村\\hsf\\CONVENTIONS.md 全文与提示中列出的 DV-IAS 参考文档，" +
        "再动笔。所有决策要能落地到代码与 K8s 清单；不确定性写成假设并标注。文档全中文、表格化、可验收。",
    }).ask<DocsOut>(w.task + "（参考：" + w.ref + "）"),
  ),
);

phase("设计评审与修订");
const review = await agent("设计评审员", {
  system:
    "你是独立设计评审员，从未参与设计。逐份读 hsf/docs/00~06 与 hsf/CONVENTIONS.md，只找问题：" +
    "遗漏的功能、不可实现的决策、与约定冲突之处、DDL/SDL 错误、验收口径缺口。不要编辑任何文件。" +
    "若发现的问题会误导后续编码，务必指出文件与小节位置。",
}).ask<DesignReview>(
  "评审 HSF 全套设计文档（hsf/docs/00~06），返回需要修订的问题清单（空数组=通过）。",
);
if (review.issues.length > 0) {
  await agent("设计修订员", {
    system:
      "你是设计修订编辑。按评审意见修订 hsf/docs/ 下的设计文档，保持与 hsf/CONVENTIONS.md 一致，不新增范围。",
  }).ask<string>(
    "按以下评审意见修订设计文档：\n" + JSON.stringify(review.issues, null, 2),
  );
  log("设计文档已按评审意见修订：" + review.issues.length + " 处");
} else {
  log("设计评审通过，无需修订");
}

phase("Monorepo 代码实现（五路并行）");
const impls = await Promise.all(
  [
    {
      name: "底座工程师",
      task:
        "实现 hsf/hsf-ias/services/common/hsf_common/（crypto.py SM3/SM3-PBKDF2/SM4-GCM/mask_phone、" +
        "db.py 双连接池、cache.py L1+Redis+单飞+抖动、web.py create_app/issue_token(str sub)/gql_context(base64 x-user-name 解码回退明文)/require_user/Exception429、" +
        "__init__.py）与 services/iam/（repo.py 建库 hsf_md+种子：县农业农村局+2乡镇+4村+管护主体+合作社+演示账号13800000601-610/13900000901-905 密码 hsf@2026、" +
        "schema.py 登录+Party 实体+联邦 types 注册、main.py、tests/ 单测、Dockerfile、requirements.txt）。" +
        "同时写 hsf-ias/requirements-base.txt（CONVENTIONS §2.1 的锁定版本）。",
      ref: "dv-ias/services/common 与 dv-ias/services/iam 全部源文件",
    },
    {
      name: "地块与项目工程师",
      task:
        "实现 services/land/（hsf_md：org 复用 iam 建的；land_parcel 表 CHAR(20) 编码+geojson TEXT+面积/土壤/作物属性，" +
        "seed 8 个地块 GeoJSON 矩形围绕 102.75E,25.05N 附近；LandParcel @key(code) 实体 + holder→Party 联邦引用 + " +
        "queries: landParcel/landParcels(分页/villageCode)/landGeojson(village) 返回 FeatureCollection；" +
        "mutation: registerParcel）与 services/project/（hsf_biz：farm_project 状态机储备→申报→审批→施工→监理→验收→绩效，" +
        "TRANSITIONS 字典+非法流转拒绝；project_workitem 可选；FarmProject @key(code) + supervisor→Party + parcels→[LandParcel]；" +
        "mutations: createProject/submitProject/approveProject/startConstruction/acceptanceArchive；tests 状态机单测）。",
      ref: "dv-ias/services/governance（状态机与实体写法）与 land 相关约定",
    },
    {
      name: "设施与物联工程师",
      task:
        "实现 services/facility/（hsf_md.facility 台账：编码 F+区划+序号、类型机井/泵站/渠道/道路、land_parcel_code 外联、" +
        "二维码 token；hsf_biz.facility_workorder 巡检工单 accept→dispatch→resolve→approve 状态机+积分式绩效字段可空；" +
        "Facility @key(code)+parcel→LandParcel；queries/mutations 全套；二维码内容=URL+token）与 services/sim/" +
        "（FastAPI 非 GraphQL：POST /sim/tick 生成 6 类传感器×每村 2 台的时序批量写 hsf_ts.sensor_reading 1000/批、" +
        "GET /alerts 规则引擎（墒情<15% 旱情/墒情>85% 渍害/风速>17m/s 大风/虫情指数>80 虫害）写 hsf_biz.alert_record、" +
        "GET /sensor/latest /alerts/statistics；README 说明模拟逻辑；tests 规则引擎单测）。",
      ref: "dv-ias/services/points 的轮询与批量写法、services/mcp 的 FastAPI 风格",
    },
    {
      name: "网关与前端工程师",
      task:
        "实现 gw/（从 dv-ias/gw/gateway.js 移植：SUBGRAPHS 五个 hsf 子图、限流 env RATE_CAP 默认 6000、" +
        "x-user-name Base64、formatError 脱敏、独立 healthz HTTP server 监听 9090、监听 8090；package.json 不升级依赖版本）与 " +
        "services/web/（纯静态三页：index.html 一张图——fetch /graphql 取 landGeojson 用纯 SVG polygon 渲染地块并支持点击看属性、" +
        "尝试加载本地 vendored leaflet 失败自动回退 SVG；admin/index.html 管理端——项目列表+状态流转按钮+设施台账+工单处置+告警列表；" +
        "mobile/index.html 移动端——地块扫码/上报/告警查看；static/js/gql.js 公共查询封装；" +
        "Dockerfile nginx:alpine 且禁止任何 CDN 引用；GW 默认 http://10.167.2.175:30090）。",
      ref: "dv-ias/gw/gateway.js 与 dv-ias/services/web/static 三页",
    },
    {
      name: "平台工程师",
      task:
        "实现 hsf-ias/k8s/（00-base.yaml：ns hsf-prod/hsf-dev+ResourceQuota requests.cpu 12 limits.memory 48Gi+" +
        "LimitRange+Secret hsf-secrets(三库三账号+JWT+REDIS_PASS，密码用强随机示例值)+PriorityClass hsf-critical/normal/batch；" +
        "10-redis.yaml：infra hsf-redis 3 副本 announce hostname ${HOSTNAME}.hsf-redis-h.infra.svc.cluster.local；" +
        "20-services.yaml：gw(30090,探针9090)+五子图(8091-8095)+web(31210)+sim(31211)，全部 Service 带 metrics:true 与 http-metrics 端口、" +
        "HPA gw/iam、反亲和、资源限额；40-k6.yaml；60-monitors.yaml）与 scripts/（build.sh 构建 8 镜像 hsf/*:TAG 推仓库、" +
        "deploy.sh、smoke-import.sh 逐容器 import app.main、static_gate.py——grep 检查：tidb-read/保留字裸用/CHAR(12)/PartyRef/" +
        "JSONResponse.*generate_latest/CDN 引用 均报错，federation Schema 必须含 types=[、" +
        "seed_sim.sh）与 perf/（baseline.js browse 200VU+report 20VU 对齐 DV-IAS 阈值、stress.js 爬坡、locustfile.py）与 " +
        "test/e2e_browser.py（≥10 用例：三端渲染/登录/错密拒绝/一张图地块渲染/项目创建+全周期流转/设施台账+报修工单流转/告警查询/联邦跨域/限流触发）。",
      ref: "dv-ias/k8s 全部清单、dv-ias/scripts、dv-ias/perf、dv-ias/test",
    },
  ].map((w) =>
    agent(w.name, {
      system:
        "你是 DV-IAS 体系培养出的资深工程师。动手前必须：1) 通读 D:\\projects\\数字乡村\\hsf\\CONVENTIONS.md；" +
        "2) 通读任务中列出的 dv-ias 参考源文件，代码风格/分层/命名完全对齐；3) 严格遵守 §2 的 16 条代码级约定，" +
        "每条都是真实生产事故的修复。写完自查一遍再返回。若参考文件缺失或约定冲突，escalate 说明而不是自行猜测。" +
        "代码注释用中文、精炼、对齐 dv-ias 风格。",
    }).ask<ImplOut>(w.task + "（参考：" + w.ref + "。返回写出的文件清单与自检结论。）"),
  ),
);

phase("静态门禁与修复循环");
let gate = await world.run("python", ["D:/projects/数字乡村/hsf/hsf-ias/scripts/static_gate.py"], {
  output: { tail: 16384 },
});
for (let round = 1; round <= 3 && gate.exitCode !== 0; round++) {
  log("静态门禁第 " + round + " 轮未通过，进入修复");
  await agent(`门禁修复员-第${round}轮`, {
    system:
      "你是代码修复员。按门禁输出修复 hsf-ias 代码，最小改动；若门禁脚本本身缺失或逻辑错误，" +
      "先修脚本再跑。修完必须重跑门禁确认（python hsf-ias/scripts/static_gate.py）。" +
      "若问题无法在代码层解决（约定互相冲突），escalate 说明而不是绕过。",
  }).ask<string>(
    "静态门禁失败，输出如下（tail）：\n" +
      gate.stderr.slice(-4000) +
      "\n请修复后重跑门禁。",
  );
  gate = await world.run("python", ["D:/projects/数字乡村/hsf/hsf-ias/scripts/static_gate.py"], {
    output: { tail: 16384 },
  });
}
const gatePass = gate.exitCode === 0;
log(gatePass ? "静态门禁通过" : "静态门禁 3 轮仍未通过，带问题进入部署阶段并如实记录");

phase("服务器构建部署与排障");
const deploy = await agent("部署工程师", {
  system:
    "你是资深 DevOps 工程师，负责把 HSF 部署到集群并达到工业级。工作区 D:\\projects\\数字乡村，" +
    "服务器 ssh centos7-181 免密。执行前必读：hsf/CONVENTIONS.md（§3 playbook 逐步执行）、" +
    "dv-ias/k8s 与 dv-ias/scripts（镜像移植模板）、docs/14 §五 与 docs/15 §七（全部已知坑与修复）、docs/17（集群当前状态）。\n" +
    "工作纪律：1) 每一步用真实命令与输出验证，禁止臆断；2) 遇 CrashLoopBackOff 读 --previous 日志定位根因，" +
    "最小改动修复后 rebuild+push+set image，循环直到全绿；3) NodePort/端口先 ss -tln 核验；" +
    "4) 任何一步连续 3 次修复无效，escalate 附上日志与你的分析，不要硬编；5) 全部完成前不要返回。" +
    "完成后把 9 个服务（gw/iam/land/project/facility/sim/web + redis 集群）状态逐个汇报。",
}).ask<DeployOut>(
  "执行 CONVENTIONS §3 全部 8 步：同步代码→TiDB 建库建账号→构建 8 镜像 hsf/*:1.0→冒烟 IMPORT-OK 全绿→" +
    "00-base+10-redis(Redis 集群初始化)→20-services+60-monitors→排障至全部 Running→验收" +
    "（网关 {__typename} 200、登录、联邦跨域 landParcel.holder.name 或 farmProject.supervisor.name 取回真实种子数据、" +
    "sim /alerts 有告警、web 三页 200）。\n" +
    "返回：services 数组（9 项，status=Running/Failed）、gatewayOk、federationOk、evidence（每条=命令+输出摘要）、remainingIssues。",
);

// 逐行喂入服务状态表（join 不需要：表按 key 覆盖更新）
for (const s of deploy.services) {
  report(s, "svc");
}

phase("压测与端到端验收");
const verify = await agent("验收测试工程师", {
  system:
    "你是性能与自动化测试工程师，对齐 DV-IAS 验收口径（docs/13、docs/15）。" +
    "系统已部署在 hsf-prod（网关 NodePort 30090）。所有结论必须有真实命令输出证据，" +
    "禁止把'应该可以'写进报告。部署工程师的部署结论如下（可直接引用其证据，但需抽查复核）：\n" +
    JSON.stringify(deploy, null, 2).slice(0, 4000),
}).ask<VerifyOut>(
  "1) k6：确认 hsf-ias/k8s/40-k6.yaml 的 GATEWAY 指向 gw-svc.hsf-prod:8090 后 create job，等待完成取 summary，" +
    "记录 总请求数/失败率/med/p95；若失败率>1% 排查后重跑一次并如实记录两次数据。\n" +
    "2) E2E：用 10.167.2.175:30500/perf/playwright-python:1.45.0 起 pod（configmap 挂 hsf-ias/test/e2e_browser.py，" +
    "先 pip install playwright==1.45.0，PLAYWRIGHT_BROWSERS_PATH=/ms-playwright），跑完收集逐用例 PASS/FAIL。\n" +
    "3) 写 hsf/docs/20-融合测试报告.md：环境/用例表/k6 数据/E2E 明细/发现的问题/证据命令。\n" +
    "返回 reportPath、k6Summary、e2eSummary、evidence。",
);

// 验收数据表
artifact.table("accept", {
  title: "HSF 验收关键指标",
  key: "item",
  columns: [
    { field: "item", label: "验收项" },
    { field: "value", label: "实测" },
    { field: "verdict", label: "结论" },
  ],
});
report(
  { item: "k6 压测", value: verify.k6Summary, verdict: "以报告为准" },
  "accept",
);
report(
  { item: "E2E 自动化", value: verify.e2eSummary, verdict: "以报告为准" },
  "accept",
);

phase("独立复核验收结论");
const cold = await agent("独立复核员", {
  system:
    "你是独立复核员，未参与任何开发部署。只凭公开证据复核：ssh centos7-181 执行 kubectl get pods -n hsf-prod、" +
    "curl 网关与 web 页面、读 hsf/docs/20-融合测试报告.md 与 hsf-ias/scripts/static_gate.py 门禁源码。" +
    "逐条对照部署与验收声明的证据是否属实；读 docs/15 对照口径是否一致。不要编辑任何文件。" +
    "把每条复核结论按 Finding 返回（属实=verified，无法复现=unconfirmed）。",
}).ask<{ findings: Finding[] }>(
  "复核以下声明并返回 findings：" +
    "1) 9 服务 Running；2) 网关与联邦跨域可用；3) k6 数据真实；4) E2E 通过数；5) 报告证据可追溯。",
);

const findings: Finding[] = cold.findings.slice();
if (!gatePass) {
  findings.push({
    where: "hsf-ias/scripts/static_gate.py",
    what: "静态门禁 3 轮后仍未通过（详见部署阶段记录），带着问题上线",
    evidence: "world.run python hsf-ias/scripts/static_gate.py 退出码 " + gate.exitCode,
    status: "verified",
    severity: "medium",
  });
}
for (const issue of deploy.remainingIssues) {
  findings.push({
    where: "hsf-prod",
    what: issue,
    evidence: "部署工程师 remainingIssues 如实上报",
    status: "unconfirmed",
    severity: "medium",
  });
}

phase("汇总交付");
const passCount = verify.e2eSummary.match(/(\d+)\s*\/\s*(\d+)/);
const e2ePass = passCount !== null && passCount[1] === passCount[2];
const conclusion =
  "HSF 高标准农田信息平台一期已独立完成分析/设计/实现/测试/部署：" +
  deploy.services.filter((s) => s.status === "Running").length +
  "/" +
  deploy.services.length +
  " 服务 Running，联邦跨域" +
  (deploy.federationOk ? "已取回真实数据" : "未验证通过") +
  "，k6：" +
  verify.k6Summary +
  "，E2E：" +
  verify.e2eSummary +
  "。" +
  (gatePass ? "静态门禁通过。" : "静态门禁存在未决问题，见 findings。");

await artifact.markdown(
  "report",
  [
    "# HSF 高标准农田信息平台 一期交付报告",
    "",
    "## 结论",
    "",
    conclusion,
    "",
    "## 服务上线状态",
    "",
    "| 服务 | 状态 | 备注 |",
    "|---|---|---|",
    ...deploy.services.map((s) => "| " + s.name + " | " + s.status + " | " + s.note + " |"),
    "",
    "## 验收数据",
    "",
    "- 网关 {__typename}: " + (deploy.gatewayOk ? "200 OK" : "未通过"),
    "- 联邦跨域查询: " + (deploy.federationOk ? "取回真实种子数据" : "未通过"),
    "- k6: " + verify.k6Summary,
    "- E2E: " + verify.e2eSummary,
    "",
    "## 复核发现",
    "",
    ...(findings.length === 0
      ? ["（独立复核未发现问题）"]
      : findings.map(
          (f) =>
            "- [" +
            f.status +
            "/" +
            f.severity +
            "] " +
            f.where +
            " — " +
            f.what +
            "（证据：" +
            f.evidence.slice(0, 160) +
            "）",
        )),
    "",
    "## 证据索引",
    "",
    ...verify.evidence.map((e) => "- " + e),
    ...deploy.evidence.map((e) => "- " + e),
  ].join("\n"),
  {
    title: "HSF 一期交付报告",
    description: "独立分析/设计/实现/测试/部署的融合验收结论与证据索引",
    primary: true,
  },
);

return {
  conclusion,
  findings,
  verified: [
    "静态门禁 python hsf-ias/scripts/static_gate.py（退出码 " + gate.exitCode + "）",
    "部署证据 " + deploy.evidence.length + " 条（部署工程师采集）",
    "验收证据 " + verify.evidence.length + " 条（k6/E2E 真实输出）",
    "独立复核 " + cold.findings.length + " 条发现",
  ],
  notCovered: [
    "真实硬件感知层（墒情/气象/虫情为模拟数据，sim 服务生成）",
    "遥感影像接入与 AI 解译（18 号文档三期范围）",
    "与全国农田建设综合监测监管平台的对接",
    "PostGIS/Cesium 专业 GIS 栈（一期按 ADR 用 GeoJSON+SVG/OpenLayers 简化）",
  ],
};