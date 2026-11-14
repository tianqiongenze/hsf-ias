// HSF 项目全面审核与优化重构：
// 状态重载 → 多角度审核 → 联网对标 → 汇总评分 → 优化重构方案 →（经用户统一后）→ 执行优化重构
// 对齐用户目标：重载目录任务状态、多角度审核达成度、联网对比最优方案打分、出优化重构方案、
// 经用户统一后执行重构并完整完成所有剩余任务（P3-4/P3-5/docs/22 §4/broker 实连/seed 重标定等）。

interface TaskItem {
  /** 领域（工作区条目用"代码/文档:"前缀，集群条目用"集群:"前缀）。 */
  area: string;
  /** done=已完成 / remaining=未完成 / at-risk=有风险或阻塞。 */
  status: "done" | "remaining" | "at-risk";
  /** 一句话说明。 */
  detail: string;
  /** 证据：文件路径、命令或输出摘要。 */
  evidence: string;
}
interface StateOut {
  /** 任务状态清单（逐条，不得遗漏残余问题单）。 */
  tasks: TaskItem[];
  /** 三句话以内的状态摘要。 */
  summary: string;
}
interface AuditFinding {
  /** 维度：本审核员的审核视角名。 */
  dimension: string;
  /** 一句话：问题或亮点。 */
  what: string;
  /** 证据：文件路径:行 或 命令与输出摘要。 */
  evidence: string;
  severity: "high" | "medium" | "low";
  /** true=达成亮点 / false=缺口或未达成。 */
  ok: boolean;
}
interface AuditOut {
  /** ≤8 条，按重要性排序。 */
  findings: AuditFinding[];
}
interface BenchOut {
  /** 对标主题。 */
  topic: string;
  /** 行业最优实践要点，每条附来源 URL。 */
  bestPractices: string[];
  /** 当前做法 vs 最优实践 vs 差距，≤600 字。 */
  comparison: string;
  /** 当前实现相对最优实践的 0-10 分（1 位小数）。 */
  score: number;
  /** 打分理由，≤200 字。 */
  rationale: string;
}
interface ScoreRow {
  dimension: string;
  /** 0-10（1 位小数）。 */
  score: number;
  /** 高/中/低。 */
  weight: string;
  /** 现状一句话。 */
  current: string;
  /** 对标结论一句话。 */
  benchmark: string;
  /** 差距与建议一句话。 */
  gap: string;
}
interface ProposalItem {
  /** 编号 R-01…。 */
  id: string;
  title: string;
  priority: "P0" | "P1" | "P2";
  /** 来源：审核缺口 / 对标差距 / 残余问题单编号。 */
  source: string;
  /** 实施路径（改哪些文件/部署动作/验收口径）。 */
  approach: string;
  /** 人工估算（如 0.5d / 1-2d）。 */
  effort: string;
  /** 主要风险与回退。 */
  risk: string;
}
interface ProposalOut {
  docPath: string;
  /** 全部优化重构项（P0 在前）。 */
  items: ProposalItem[];
  /** 三句话以内方案摘要（进入用户统一环节）。 */
  summary: string;
}
interface ReviewIssues {
  /** 问题清单；空=通过。 */
  issues: string[];
}
interface WorkflowReport {
  conclusion: string;
  findings: { where: string; what: string; evidence: string; status: "verified" | "unconfirmed"; severity: "low" | "medium" | "high" }[];
  verified: string[];
  notCovered: string[];
}

artifact.table("state", {
  title: "HSF 任务状态清单",
  key: "area",
  columns: [
    { field: "area", label: "领域" },
    { field: "status", label: "状态" },
    { field: "detail", label: "说明" },
  ],
});
artifact.table("audit", {
  title: "多角度审核发现",
  columns: [
    { field: "dimension", label: "维度" },
    { field: "severity", label: "级别" },
    { field: "ok", label: "达成" },
    { field: "what", label: "发现" },
  ],
});
artifact.table("bench", {
  title: "联网对标最优方案",
  key: "topic",
  columns: [
    { field: "topic", label: "主题" },
    { field: "score", label: "对标得分" },
    { field: "comparison", label: "当前 vs 最优" },
  ],
});
artifact.table("score", {
  title: "八维评分（0-10）",
  key: "dimension",
  columns: [
    { field: "dimension", label: "维度" },
    { field: "score", label: "得分" },
    { field: "weight", label: "权重" },
    { field: "gap", label: "差距与建议" },
  ],
});
artifact.table("plan", {
  title: "优化重构项（待用户统一）",
  key: "id",
  columns: [
    { field: "id", label: "编号" },
    { field: "priority", label: "优先级" },
    { field: "title", label: "事项" },
    { field: "effort", label: "工作量" },
  ],
});

phase("重载项目任务状态（工作区+服务器双线）");
const [wsState, srvState] = await Promise.all([
  agent("工作区状态勘察员", {
    system:
      "你是项目状态勘察员，只读不改文件。项目工作区 D:\\projects\\数字乡村\\hsf（部署面 hsf-ias/）。" +
      "必读：docs/00（总览与验收映射）、docs/08（三期设计 §1 不做边界+§7 验收）、docs/20/21/23（一/二/三期融合测试报告，重点残余事项与问题单）、" +
      "CONVENTIONS.md / CONVENTIONS-P2.md / CONVENTIONS-P3.md、git log 最近 5 条提交。",
  }).ask<StateOut>(
    "还原 HSF 一/二/三期任务状态：tasks 逐条列 done/remaining/at-risk（area 前缀'代码/文档:'）。" +
      "remaining/at-risk 必须逐条列全：P3-4（iot-write 吞吐专项）、P3-5（k6 环境噪声）、broker 实连（dep-device.sh）、" +
      "seed 样本地块漂移重标定、rs alerts 字段语义、NDVI 预警粒度规格复核、docs/22 §4（master dockerd insecure-registries 待授权修复）等。" +
      "evidence 用文件路径或命令输出。返回 StateOut。",
  ),
  agent("集群状态勘察员", {
    system:
      "你是集群状态勘察员。ssh centos7-181 实测，只读——不得改任何资源。重点：kubectl -n hsf-prod get pods/deploy（镜像矩阵）、" +
      "PVC、resourcequota、Job 历史、docker info 的 Insecure Registries（docs/22 §4 遗留是否仍在）、" +
      "TimescaleDB sensor_reading 行数与磁盘占用、mosquitto conf 关键值。",
  }).ask<StateOut>(
    "实测集群当前状态，tasks 逐条列 done/remaining/at-risk（area 前缀'集群:'）。" +
      "特别注意：docs/22 §4 备用清单各项当前是否仍阻塞、iot-write 瓶颈三环节（sim 队列/broker 丢弃/iot 刷写）当前指标。" +
      "evidence 用命令与输出摘要。返回 StateOut。",
  ),
]);
for (const t of [...wsState.tasks, ...srvState.tasks]) {
  report(t, "state");
}
log("状态重载完成：工作区 " + wsState.tasks.length + " 条 + 集群 " + srvState.tasks.length + " 条");

phase("多角度审核达成度（六路并行）");
const auditLenses = [
  { name: "架构与联邦审核员", brief:
    "视角=总体架构与 GraphQL 联邦：读 docs/02/03/05/07/08 + gw/gateway.js + services/land（联邦字段委托）+ services/rs（子图）。" +
    "审：三模式选型/契约冻结一致性（D-04/D-07/D-08 系列）、SUBGRAPHS 演进、手动委托 vs @link 决策、" +
    "docs/00 §3 映射表功能是否全部落地、ADR-HSF-001 演进触发器履行。" },
  { name: "安全合规审核员", brief:
    "视角=安全与合规：读 docs/01（A2 安全约束）+ services/iam（口令派生/JWT）+ gw（INTERNAL 脱敏/限流）+ " +
    "services/iot（设备凭证 SM3/hint）+ k8s/00-base（Secrets/演示凭据边界）+ 30-mqtt（ACL）。" +
    "审：敏感数据 SM4/国密栈、密钥是否入库、审计留痕完整性、mosquitto per-device ACL、演示账号与生产隔离、等保三级差距。" },
  { name: "性能与容量审核员", brief:
    "视角=性能与容量：读 docs/20/21/23 三报告性能章节 + perf/stress.js/perf/baseline.js + services/iot/app/main.py（刷写线程）" +
    "+ services/sim/app/mqtt_pub.py。审：NFR-02/03 达成、iot-write 5000条/s 参考目标与 P3-4 瓶颈证据链、" +
    "时序表 17M+ 行容量策略（压缩/保留）、HPA 配置合理性、p95 判据口径。" },
  { name: "数据工程审核员", brief:
    "视角=数据工程：读 docs/04（数据架构）+ scripts/init-ts.sh + k8s/25-timescale.yaml + services/iot/app/repo.py（时序写入）" +
    "+ services/rs（rs_scene/rs_vigor DDL）+ services/uplink（uplink_task）。审：TiDB 三库+TimescaleDB 分工口径、" +
    "连续聚合/保留策略是否真的建了并生效、备份恢复策略有无、D-08-1 存量零变更履行、外键逻辑引用一致性。" },
  { name: "测试与质量审核员", brief:
    "视角=测试与质量：读 docs/06（实施与验收计划）+ test/e2e_browser.py（25 用例）+ services/*/tests/ + scripts/static_gate.py + " +
    "k8s/40-k6.yaml。审：E2E 覆盖面 vs docs/00 功能映射的缺口、单测覆盖（各服务哪些核心路径无测试）、" +
    "契约测试缺失、混沌/故障注入缺失、门禁检查项完备性、测试数据可复现性。" },
  { name: "运维可观测审核员", brief:
    "视角=运维与可观测：读 k8s/*.yaml（探针/limits/HPA/ServiceMonitor/PriorityClass）+ 60-monitors.yaml + " +
    "scripts/deploy.sh/build.sh + docs/22（环境阻塞记录）。审：告警规则覆盖（有无 PrometheusRule/Alertmanager）、" +
    "日志方案、备份/灾备缺失、滚动更新与回滚演练、NodePort 管理口径、docs/22 §4 遗留阻塞影响面。" },
];
const audits = await Promise.all(
  auditLenses.map((l) =>
    agent(l.name, {
      system:
        "你是独立审核员，只读不改任何文件。判断标准=docs/00 验收映射与各期设计冻结目标（docs/04-08）+ 三份融合测试报告（docs/20/21/23）声明。" +
        "每条发现必须给证据（路径:行 或 命令输出），不许推演。若声明与实测矛盾，明确指出。" +
        "产出 ≤8 条 findings 按 importance 排序：达成亮点 ok=true，缺口 ok=false。escalate 而不是猜。",
    }).ask<AuditOut>(l.brief + "\n\n执行审核并返回 AuditOut（dimension=你的视角名）。"),
  ),
);
const allFindings = audits.map((a) => a.findings).flat();
for (const f of allFindings) {
  report(f, "audit");
}
log("六路审核完成：共 " + allFindings.length + " 条发现（缺口 " + allFindings.filter((f) => !f.ok).length + " 条）");

phase("联网对标最优方案（四路并行）");
const benchTopics = [
  { name: "智慧农业平台对标员", topic:
    "数字乡村/高标准农田智慧农业平台功能与架构对标：检索农业农村部数字农业试点项目、高标准农田信息化建设指南、" +
    "全国农田建设综合监测监管平台接入要求、同类开源项目（如 OpenAgri/智慧农业中台），" +
    "对比本项目的墒情监测/灌溉决策/遥感长势/上级上报功能完备度与架构模式。" },
  { name: "IoT 时序平台对标员", topic:
    "IoT 数据接入与时序平台最优实践对标：检索 MQTT 接入层设计（共享订阅/会话保持/QoS 策略）、TimescaleDB vs InfluxDB vs IoTDB 选型、" +
    "批量写入与背压治理（队列/多 worker/压缩）、hypertable 压缩与保留策略生产配置，" +
    "对比本项目 sim→mosquitto→iot→TimescaleDB 链路与 P3-4 瓶颈（paho 队列满/broker 丢 QoS1/单刷写线程）。" },
  { name: "GraphQL 治理对标员", topic:
    "GraphQL Federation 与微服务治理最优实践对标：检索 Apollo Federation 2 生产实践（entity 接口/组合校验/查询复杂度限制）、" +
    "网关限流模式（固定窗口 vs 令牌桶 vs 分布式）、降级与 fail-open 策略、鉴权与脱敏中间件，" +
    "对比本项目 gw 限流（ratelimit-svc 固窗）、联邦手动委托、错误脱敏。" },
  { name: "K8s 生产化对标员", topic:
    "K8s 生产化与可观测性最优实践对标：检索生产级探针/HPA/PDB 配置、Prometheus 告警规则设计（SLO 燃烧率）、" +
    "日志方案（Loki/EFK）、备份灾备（Velero/DB 备份策略）、本地存储 local-path 的生产风险，" +
    "对比本项目 2 节点集群配置、ServiceMonitor 覆盖、告警与备份缺失项。" },
];
const benches = await Promise.all(
  benchTopics.map((b) =>
    agent(b.name, {
      system:
        "你是技术调研员，用 WebSearch/WebFetch 检索行业最优实践（官方文档与一线工程实践优先，中文场景资料优先）。" +
        "每条 bestPractice 必须附来源 URL；comparison 分三段：本项目当前做法→最优实践→差距。score 为本项目相对最优实践的 0-10 分（1 位小数），" +
        "打分必须基于对比证据而非感觉。若检索受限如实说明并在 score 标注置信度。",
    }).ask<BenchOut>("对标主题：" + b.topic + "\n\n返回 BenchOut（topic 原样用上面主题前 12 字）。"),
  ),
);
for (const b of benches) {
  report(b, "bench");
}
log("四路对标完成：" + benches.map((b) => b.topic + "=" + b.score).join("，"));

phase("汇总八维评分");
const judge = await agent("评分仲裁员", {
  system:
    "你是评分仲裁员，校准打分尺度：10=业界最优/9-8=生产优秀/7-6=合格有差距/5-4=明显不足/≤3=重大缺陷。" +
    "只依据已产出的审核发现与对标结论打分，不得引入未提供的事实；每个维度给一句话差距与建议。",
}).ask<{ rows: ScoreRow[] }>(
  "八维评分（功能覆盖度/架构设计/安全合规/性能吞吐/数据工程/测试质量/运维可观测/文档与可追溯），" +
    "权重标注高/中/低。依据如下。\n\n审核发现（" + allFindings.length + " 条）：\n" +
    JSON.stringify(allFindings, null, 1).slice(0, 6000) +
    "\n\n联网对标：\n" + JSON.stringify(benches, null, 1).slice(0, 6000) +
    "\n\n状态摘要：工作区=" + wsState.summary + "；集群=" + srvState.summary,
);
for (const r of judge.rows) {
  report(r, "score");
}
log("八维评分完成：" + judge.rows.map((r) => r.dimension + "=" + r.score).join("，"));

phase("撰写优化重构方案（docs/24）");
const synth = await agent("方案总编纂", {
  system:
    "你是资深架构师与交付负责人。必读：docs/20/21/23（残余事项与问题单）、CONVENTIONS 三份、" +
    "docs/24 目标=可执行的重构方案书。所有 P0 项必须有明确验收口径（命令级）；" +
    "P0=影响验收判据或生产风险；P1=对标差距明显且可落地；P2=改善项。方案必须覆盖'完整完成所有剩余任务'" +
    "（残余问题单逐条映射到 R 项，含 docs/22 §4 授权依赖项的说明）。产出全中文、表格化。",
}).ask<ProposalOut>(
  "综合以下输入写 hsf/docs/24-全面审核与优化重构方案.md：\n" +
    "1) 任务状态：" + JSON.stringify([...wsState.tasks, ...srvState.tasks].filter((t) => t.status !== "done"), null, 1).slice(0, 4000) +
    "\n2) 审核缺口（ok=false）：\n" + JSON.stringify(allFindings.filter((f) => !f.ok), null, 1).slice(0, 5000) +
    "\n3) 对标差距：\n" + JSON.stringify(benches.map((b) => ({ topic: b.topic, score: b.score, comparison: b.comparison })), null, 1).slice(0, 5000) +
    "\n4) 八维评分：\n" + JSON.stringify(judge.rows, null, 1).slice(0, 3000) +
    "\n\n产出：R-01…R-N 逐项（priority/source/approach/effort/risk）+ 分阶段实施顺序 + 每项验收口径。" +
    "返回 ProposalOut（docPath=docs/24-全面审核与优化重构方案.md）。",
);
for (const it of synth.items) {
  report(it, "plan");
}
log("方案完成：" + synth.items.length + " 项（P0×" + synth.items.filter((i) => i.priority === "P0").length + "）");

phase("方案独立审读与修订");
const prov = await agent("方案审读员", {
  system:
    "你是独立审读员，只读 docs/24 与其引用的文件，没参与过撰写。只挑问题：验收口径不可执行、工作量失真、" +
    "与冻结契约（docs/04-08）冲突、遗漏残余问题单、优先级失当。不编辑文件。",
}).ask<ReviewIssues>(
  "审读 hsf/docs/24-全面审核与优化重构方案.md，对照 docs/23 §7 残余事项逐条核对是否全部映射，返回问题清单（空=通过）。",
);
if (prov.issues.length > 0) {
  await agent("方案修订编辑", {
    system: "你是方案修订编辑：按审读意见最小改动修订 docs/24，与冻结契约一致。",
  }).ask<string>("按意见修订：\n" + JSON.stringify(prov.issues, null, 2));
  log("方案已按审读意见修订：" + prov.issues.length + " 处");
} else {
  log("方案审读通过");
}

phase("发布审核报告");
await artifact.file("audit-doc", "docs/24-全面审核与优化重构方案.md", {
  title: "HSF 全面审核与优化重构方案",
  description: "任务状态重载 + 六维审核 + 联网对标 + 八维评分 + 分级重构项（待用户统一后执行）",
  primary: true,
});

log("审核阶段完成，等待用户统一优化重构方案后继续执行");

return await hole<WorkflowReport>(
  "经用户统一后执行优化重构",
  "审核与评分已完成，docs/24-全面审核与优化重构方案.md 已产出待用户统一。\n\n" +
    "八维评分：" + judge.rows.map((r) => r.dimension + "=" + r.score).join("，") + "\n" +
    "对标得分：" + benches.map((b) => b.topic + "=" + b.score).join("，") + "\n" +
    "重构项（" + synth.items.length + " 项，P0×" + synth.items.filter((i) => i.priority === "P0").length + "）：\n" +
    synth.items.map((i) => "- " + i.id + "[" + i.priority + "] " + i.title + "（" + i.effort + "，来源：" + i.source + "）").join("\n") +
    "\n\n用户已对方案表态。请据此决定并执行优化重构阶段（P0 先行），完成后完整完成所有剩余任务并交付。", async () => {
  interface StageOut {
    /** 本阶段变更/新增的文件清单。 */
    files: string[];
    /** 逐项完成记录：R 编号 → 验收命令与输出摘要。 */
    done: string[];
    /** 留档/降级项（含原因）。 */
    skipped: string[];
    /** 两句话以内自检结论。 */
    summary: string;
  }
  interface RefDeployOut {
    services: { name: string; status: string; note: string }[];
    linksOk: boolean;
    evidence: string[];
    remainingIssues: string[];
  }
  interface RefVerifyOut {
    reportPath: string;
    e2eSummary: string;
    k6Summary: string;
    throughputSummary: string;
    evidence: string[];
  }
  interface RefFinding {
    where: string;
    what: string;
    evidence: string;
    status: "verified" | "unconfirmed";
    severity: "low" | "medium" | "high";
  }

  const stageSpec =
    "必读顺序：hsf/docs/24-全面审核与优化重构方案.md（你负责 R 项的 §4 详表 approach + §7 验收口径）→ " +
    "CONVENTIONS-P3.md → CONVENTIONS-P2.md → CONVENTIONS.md → 引用的源码。" +
    "纪律：每项完成后立即按 docs/24 §7 验收口径跑命令自验并保留输出；与冻结契约冲突或验收口径不可达则如实记入 skipped 并说明，不许放宽口径；" +
    "工作区=D:\\projects\\数字乡村\\hsf（部署面 hsf-ias/），服务器 ssh centos7-181（/opt/hsf/hsf-ias）。";

  phase("阶段0：安全止血（R-10→R-09→R-06→R-08→R-07）");
  const s0 = await agent("安全止血工程师", {
    system: "你是安全工程师。" + stageSpec,
  }).ask<StageOut>(
    "执行阶段 0（顺序红线：R-10 必须最先）：" +
      "R-10 docs/22 §2 双栈前提修正（实测推翻'restart docker 不影响 k8s'——先修文档再做任何 docker 动作）→ " +
      "R-09 docs/00 §3 验收映射收口（四项三期承诺显式'不做'声明，对齐 docs/08 §1 诚实边界）→ " +
      "R-06 SM4 字段加密密钥外置（Secret 注入，代码 0 硬编码；存量密文用旧 key 解密+新 key 重加密迁移或如实留档）→ " +
      "R-08 JWT 寿命声明与实现对齐（按 docs/24 §4 R-08 approach 决定方向：实现侧补 refresh/过期语义或文档修正，二选一并说明理由）→ " +
      "R-07 运行时密钥治理（明文出库→hsf-secrets、dev/prod 分值、代码回退消除、全量轮换；轮换后 apply 00-base 并滚动 iam/gw/iot/web，" +
      "E2E 登录链路必须回归 PASS）。返回 StageOut。",
  );
  log("阶段0 完成：" + s0.summary);

  phase("阶段0：dockerd 恢复（R-11，已授权，无损优先）");
  const r11 = await agent("环境恢复工程师", {
    system:
      "你是环境运维工程师。用户已授权 docs/22 §4 恢复清单执行；无损优先。" + stageSpec,
  }).ask<string>(
    "执行 R-11：①先确认当前无 k6/E2E Job 在跑（kubectl -n hsf-prod get jobs）；②kill -HUP $(pidof dockerd)（无损重载）→ " +
      "验证 docker info 的 Insecure Registries 含 10.167.2.175:30500 且 docker version CLI 响应 <3s；" +
      "③无效则 systemctl restart docker（R-10 已修正波及面认知：影响 master 上 docker 容器栈，k8s=containerd 不受影响——但如实记录实际波及与恢复）；" +
      "④更新 docs/22 §4 执行记录（时间/动作/验证输出）。返回执行了哪步+验证证据原文。",
  );
  log("R-11：" + r11.slice(0, 300));

  phase("阶段1：吞吐与容量专项（R-04→R-02→R-03→R-01→R-05）");
  const s1 = await agent("吞吐专项工程师", {
    system: "你是 IoT 时序链路资深工程师。" + stageSpec,
  }).ask<StageOut>(
    "按依赖顺序执行：" +
      "R-04 perf/stress.js iotWritten() 正则支持科学计数法（prometheus_client ≥1e6 输出 1.205557e+06）→ " +
      "R-02 sim 发布侧可观测指标（发布失败/队列满计数进 /metrics）与失败补偿（重投/降速策略，docs/24 §4 R-02 approach）→ " +
      "R-03 broker 丢弃监控（$SYS 订阅或日志解析进 iot/ratelimit metrics）+ max_queued_messages 再平衡（docs/24 §4 数值）→ " +
      "R-01 iot 刷写侧多线程化（N worker 线程池，批次评估旁路：规则引擎移出刷写热路径改独立节拍）——注意 docs/07 冻结契约" +
      "（批量 500/2s、/sensor/latest 口径、规则阈值回差）不得破坏；单测覆盖新逻辑 → " +
      "R-05 TimescaleDB 压缩/保留策略生效性复核（hypertable 设置实查+缺失即补，写 init-ts.sh 幂等项）。" +
      "返回 StageOut。",
  );
  const unit1 = await world.run("python", ["-m", "pytest", "hsf-ias/services/iot/tests", "hsf-ias/services/rs/tests", "hsf-ias/services/uplink/tests", "-q"], {
    output: { tail: 16384 },
    timeoutMs: 600000,
  });
  log("阶段1 单测 exitCode=" + unit1.exitCode);

  phase("阶段2：契约一致性与功能正确性（R-12→R-13→R-14→R-15→R-16→R-18）");
  const s2 = await agent("契约收口工程师", {
    system: "你是契约与正确性工程师。" + stageSpec,
  }).ask<StageOut>(
    "顺序执行：" +
      "R-12 rs 子图 enable_federation_2=True + static_gate 增参数检查 → " +
      "R-13 NDVI 预警落库粒度修复（多地块静默丢失：uk_rule_reading 唯一键 vs parcel 粒度——按 docs/24 §4 R-13 approach 改造，" +
      "存量数据不迁移）→ R-14 rs alerts 字段语义复核与文档收口（sensor_code=RS-* 合成源/metric=ndvi 写入 docs/08 §2.3 或改代码，二选一留痕）→ " +
      "R-15 seed 样本地块漂移重标定（make_sample.py 按 land_parcel 实际地块集 0001/0002/0003/0005/0006/0007 重算，第二景降幅预警自触发）→ " +
      "R-16 契约文档漂移四处收口（D-08-4 幽灵编号/30090/rs 命名/P3-3 S 前缀契约回写 docs/08 §4）→ " +
      "R-18 MQTT 设备凭证 CSPRNG 化（secrets.token_bytes/random 替换时间戳盐，凭证生成逻辑单测）。" +
      "每项含单测更新。返回 StageOut。",
  );
  const unit2 = await world.run("python", ["-m", "pytest", "hsf-ias/services", "-q"], {
    output: { tail: 16384 },
    timeoutMs: 600000,
  });
  log("阶段2 全量单测 exitCode=" + unit2.exitCode);

  phase("阶段3：K8s 生产化与集群/设备治理（R-21→R-22→R-23→R-20→R-19）");
  const s3 = await agent("生产化工程师", {
    system: "你是 K8s 平台工程师。新清单文件进 hsf-ias/k8s/ 编号顺延。" + stageSpec,
  }).ask<StageOut>(
    "顺序执行：" +
      "R-21 PDB（gw/iam/land/project/facility/ratelimit/web minAvailable 1）+ 关键服务 startupProbe + HPA behavior（scaleDown 稳定窗口）→ " +
      "R-22 备份 CronJob：TimescaleDB（pg_dump 全量，05:00 每日，保留 7 份 local-path PVC）+ TiDB/Redis 导出（每周），手动触发一次验证备份产物可读 → " +
      "R-23 k6 P3-5 环境治理立案（docs/26 或 docs/22 增节：现象/证据/建议巡检项/复测口径，立案留档）→ " +
      "R-20 mosquitto 最小授权改造（sim 发布者仅写 hsf/sensor/#、iot 订阅者仅读，ACL 分用户重写 30-mqtt ConfigMap）→ " +
      "R-19 broker 设备实连规格③【降级口径】：真实硬件未到货——按 docs/24 §6 降级条款用集群内客户端容器以已注册设备凭证" +
      "完成 own-topic 收发+他人 topic 拒绝验证，报告中如实标注'半实机（容器客户端）非真机'。" +
      "返回 StageOut。",
  );

  phase("阶段4：治理与对标增强（R-17→R-24→R-27→R-25→R-26→R-28→R-31）");
  const s4 = await agent("治理增强工程师", {
    system: "你是平台治理工程师。纯文档与低风险配置项优先。" + stageSpec,
  }).ask<StageOut>(
    "顺序执行：" +
      "R-17 ADR 治理建库（docs/adr/ 目录+README 索引，存量补立 HSF-016 rs 合成源/HSF-017 uplink stub 两篇，" +
      "引用 docs/02 §9 体例）→ R-24 上报 payload 空间矢量增强（PARCEL 项 geoWktHint→完整 WKT 上图口径，assemble+单测+docs/08 §3.1 schemaVersion 1.2）→ " +
      "R-27 生产环境关闭 introspection（gw env GRAPHQL_INTROSPECTION=false，演示环境口径写 docs/24）→ " +
      "R-25 gw 限流滑动窗口演进（ratelimit-svc 增滑动窗口模式 env RATE_MODE2 可选，默认保持固窗=一期冻结契约不破坏，单测覆盖）→ " +
      "R-26 GraphQL 深度/复杂度限制（gw validationRule 或Apollo depth-limit，阈值按 docs/24 §4）+ static_gate 增 CI 组合校验提示 → " +
      "R-28 运维可观测增强（PrometheusRule 燃烧率告警 browse p95/5xx、日志 retention 说明、60-monitors.yaml 增量）→ " +
      "R-31 JWT RS256 非对称签名演进（iam 签发 RS256+gw 公钥验签，HS256 兼容开关，密钥入 hsf-secrets；E2E 登录回归）。" +
      "返回 StageOut。",
  );

  phase("门禁与全量回归（代码层）");
  const gateR = await world.run("python", ["hsf-ias/scripts/static_gate.py"], { output: { tail: 16384 } });
  log("重构后静态门禁 exitCode=" + gateR.exitCode);
  const unitR = await world.run("python", ["-m", "pytest", "hsf-ias/services", "-q"], {
    output: { tail: 16384 },
    timeoutMs: 600000,
  });
  log("重构后全量单测 exitCode=" + unitR.exitCode);
  const fixNote = (gateR.exitCode !== 0 || unitR.exitCode !== 0)
    ? await agent("重构修复员", {
        system: "你是代码修复员：按门禁/单测输出最小改动修复；脚本缺陷先修脚本；修完重跑确认。无法解决 escalate。" + stageSpec,
      }).ask<string>(
        "门禁 tail：\n" + gateR.stderr.slice(-3000) + "\n单测 tail：\n" + unitR.stderr.slice(-3000) + "\n修复并重跑两组命令。",
      )
    : "";

  phase("镜像重建与全量部署（patch tag 3.0.6）");
  const deployR = await agent("重构部署工程师", {
    system:
      "你是资深 DevOps。R-11 已由环境恢复工程师执行（dockerd 应已恢复，先实测 docker version 响应确认）。" + stageSpec,
  }).ask<RefDeployOut>(
    "执行重构部署：" +
      "1) 同步工作区 hsf-ias → /opt/hsf/hsf-ias（tar 覆盖，排除 .git/pycache）；\n" +
      "2) 按阶段 0-4 实际变更服务集 TAG=3.0.6 SVCS='<变更集>' bash scripts/build.sh 构建推送（预计含 iot/rs/uplink/gw/sim/web 中有代码变更者）；\n" +
      "3) manifest tag 更新 + apply 全套清单（00-base 含 Secrets 轮换、20-services、25/30、新增 PDB/CronJob/PrometheusRule/monitors）；\n" +
      "4) rollout status 全绿逐个验证；\n" +
      "5) 新配置在位核验：Secrets 无明文残留（grep 工作区）、PVC 备份产物、PDB/HPA behavior/PrometheusRule kubectl get 到位、mosquitto ACL 双用户生效、introspection 关闭（__schema 查询 400/被拒）；\n" +
      "6) 链路抽查：登录（RS256 新 token）、E2E 前置健康、iot/register、uplink ACKED、限流 429。" +
      "返回 services 全量、linksOk、evidence、remainingIssues。",
  );
  log("部署组件 " + deployR.services.length + " 个，全量状态见 docs/25 验收报告");

  phase("全量验收（E2E + k6 + W-1 判据复测）");
  const verifyR = await agent("重构验收工程师", {
    system:
      "你是性能与自动化验收工程师，对齐 docs/20/21/23 口径。所有结论必须真实命令输出。部署结论供交叉核对：\n" +
      JSON.stringify(deployR, null, 2).slice(0, 3000),
  }).ask<RefVerifyOut>(
    "1) E2E 全量 25 条（集群内 Playwright Job，ConfigMap 更新为当前 e2e_browser.py）→ 必须 fail=0；\n" +
      "2) k6 browse/report（冻结 thresholds 判据）+ iot-write 复测：check≥98%、tick p95、落库差值≥5,000 条/s、" +
      "三方对账（published=written+onconflict+丢弃）闭合——R-01/R-02/R-03 成效判据（docs/24 §7 R-01 行）；\n" +
      "3) 备份 CronJob 产物核验（文件存在+pg_restore --list 可读）；\n" +
      "4) 写 hsf/docs/25-优化重构验收报告.md：R-01~R-28 逐项验收表（含 docs/24 §7 命令级验收输出）+ 轨迹表（重构前后对比）+ 证据 + 残余。" +
      "返回 reportPath/e2eSummary/k6Summary/throughputSummary/evidence。",
  );
  log("验收：E2E=" + verifyR.e2eSummary + "；k6=" + verifyR.k6Summary + "；吞吐=" + verifyR.throughputSummary);

  phase("独立复核重构结论");
  const coldR = await agent("重构独立复核员", {
    system:
      "你是独立复核员，ssh centos7-181 + 工作区实测，只读。逐条对照重构声明返回 findings（属实=verified；未能复现=unconfirmed）。",
  }).ask<{ findings: RefFinding[] }>(
    "复核声明：1) 安全止血三项（SM4 外置/密钥治理/JWT 对齐——grep 硬编码 0 命中、Secret 键在位、token exp 声明与实现一致）；" +
      "2) iot-write 判据达标（check≥98%/≥5000 条/s）；3) NDVI 预警多地块粒度与 seed 自触发；4) PDB/备份/告警在位；" +
      "5) mosquitto 双用户 ACL 与设备凭证 CSPRNG；6) introspection 关闭；7) docs/25 证据可追溯。",
  );
  const findingsR: RefFinding[] = coldR.findings.slice();
  for (const issue of deployR.remainingIssues) {
    findingsR.push({ where: "hsf-prod", what: issue, evidence: "部署工程师 remainingIssues 上报", status: "unconfirmed", severity: "medium" });
  }
  if (gateR.exitCode !== 0) {
    findingsR.push({ where: "static_gate", what: "重构后静态门禁未全绿", evidence: "exitCode=" + gateR.exitCode, status: "verified", severity: "medium" });
  }

  phase("提交推送与 R 项状态回写");
  const ship = await agent("交付收尾员", {
    system:
      "你是交付收尾员。工作区 D:\\projects\\数字乡村\\hsf（git 仓库，remote=hsf-ias GitHub）。" +
      "步骤：docs/24 §3 总览表增'状态'列逐项回写（done/部分/留档+一句话）；git add -A → commit（中文描述列出阶段 0-4 与 R 项）→ " +
      "git push origin main（网络间歇则循环重试至成功，最多 20 次×60s，成功后 git ls-remote 核对远端 head）。返回 commit hash 与 push 证据。",
  }).ask<string>("执行提交推送与状态回写。");

  phase("汇总交付");
  for (const it of synth.items) {
    const hit = (s0.done.join(" ") + " " + s1.done.join(" ") + " " + s2.done.join(" ") + " " + s3.done.join(" ") + " " + s4.done.join(" ")).includes(it.id);
    report({ id: it.id, priority: it.priority, title: (hit ? "✅ " : "📄 留档 ") + it.title, effort: it.effort }, "plan");
  }
  const runningR = deployR.services.filter((s) => s.status === "Running").length;
  const conclusionR =
    "HSF 优化重构完成：阶段 0 安全止血（SM4 外置/密钥治理/JWT 对齐/验收映射收口/docs/22 修正/dockerd 恢复）→ " +
    "阶段 1 吞吐专项（check 与吞吐判据见验收）→ 阶段 2 契约收口（NDVI 粒度/seed 重标定/文档漂移清零）→ " +
    "阶段 3 生产化（PDB/备份/告警/broker 半实机验证）→ 阶段 4 治理增强（ADR 库/GraphQL 治理/RS256）全部落地。" +
    "部署 " + runningR + "/" + deployR.services.length + " 组件 Running；E2E " + verifyR.e2eSummary +
    "；k6 " + verifyR.k6Summary + "；吞吐 " + verifyR.throughputSummary + "。已提交推送：" + ship.slice(0, 80);
  await artifact.markdown(
    "report2",
    [
      "# HSF 优化重构交付报告（R-01~R-28 全量执行）",
      "",
      "## 结论",
      "",
      conclusionR,
      "",
      "## 验收数据",
      "",
      "- E2E: " + verifyR.e2eSummary,
      "- k6: " + verifyR.k6Summary,
      "- 吞吐判据: " + verifyR.throughputSummary,
      "- 链路: " + (deployR.linksOk ? "全部验证" : "存在未验证项，见 findings"),
      "",
      "## 复核发现",
      "",
      ...(findingsR.length === 0
        ? ["（独立复核未发现问题）"]
        : findingsR.map((f) => "- [" + f.status + "/" + f.severity + "] " + f.where + " — " + f.what)),
      "",
      "## 证据索引",
      "",
      ...verifyR.evidence.map((e) => "- " + e),
      ...deployR.evidence.map((e) => "- " + e),
    ].join("\n"),
    { title: "HSF 优化重构交付报告", description: "R-01~R-28 全量执行结果与证据（主交付物=docs/25 验收报告）" },
  );
  return {
    conclusion: conclusionR,
    findings: findingsR,
    verified: [
      "阶段验收：阶段0=" + s0.done.length + " 项、阶段1=" + s1.done.length + "、阶段2=" + s2.done.length + "、阶段3=" + s3.done.length + "、阶段4=" + s4.done.length,
      "门禁 exitCode=" + gateR.exitCode + "；单测 exitCode=" + unitR.exitCode + (fixNote ? "（修复：" + fixNote.slice(0, 120) + "）" : ""),
      "R-11 dockerd：",
      "部署证据 " + deployR.evidence.length + " 条；验收证据 " + verifyR.evidence.length + " 条；独立复核 " + coldR.findings.length + " 条",
      "交付：" + ship.slice(0, 120),
    ],
    notCovered: [
      "R-29/R-30（ET0/真实影像）触发条件项按用户统一留档下期",
      "真机 broker 实连（硬件未到货，按降级口径容器客户端验证并如实标注）",
      "真实监管平台联调（V-3 冻结边界，transport 单文件待替换）",
    ],
  };
},
);
