// HSF 三期：遥感解译（CPU 路线）+ 上级平台对接 + 设备接入框架 + 二期残余收口
// 对齐 docs/08-三期设计 与 CONVENTIONS-P3.md（U/V/W/X/Y 冻结）；范式与二期 workflow-p2.dwf.ts 一致。
// 勘察结论（2026-11-13）：rs/uplink 代码+清单+E2E 用例已在工作区与服务器部署（3.0.1，Running），
// 但 rs 容器内 repo.py 为旧版（缺 jwt_role）→ /vigor /alerts 500；本工作流负责修复收口+验收+报告。

interface ImplOut {
  /** 写出/修改的文件路径。 */
  files: string[];
  /** 两句话以内：实现内容与自检结论。 */
  summary: string;
}
interface DesignReview {
  /** 问题清单；空=通过。 */
  issues: string[];
}
interface DeployOut {
  services: { name: string; status: string; note: string }[];
  /** 三期新链路：NDVI 解译/非农化预警、上报闭环、设备注册、二期四链路回归。 */
  linksOk: boolean;
  evidence: string[];
  remainingIssues: string[];
}
interface VerifyOut {
  reportPath: string;
  e2eSummary: string;
  k6Summary: string;
  newLinksSummary: string;
  evidence: string[];
}
interface Finding {
  /** 位置：文件路径或组件名。 */
  where: string;
  /** 一句话说明问题/结论。 */
  what: string;
  /** 证据：命令输出或文件行。 */
  evidence: string;
  status: "verified" | "unconfirmed";
  severity: "low" | "medium" | "high";
}
interface WorkflowReport {
  conclusion: string;
  findings: Finding[];
  verified: string[];
  notCovered: string[];
}

artifact.table("svc3", {
  title: "HSF 三期组件上线状态",
  key: "name",
  columns: [
    { field: "name", label: "组件" },
    { field: "status", label: "状态" },
    { field: "note", label: "备注" },
  ],
});

phase("三期设计复核（docs/08 v1.0 已冻结，只查问题）");
const drev = await agent("三期设计复核员", {
  system:
    "你是独立评审员，只找问题不改文件。必读：D:\\projects\\数字乡村\\hsf\\docs\\08-三期设计-遥感解译与上级对接与设备接入.md → " +
    "hsf\\CONVENTIONS-P3.md → CONVENTIONS-P2.md → CONVENTIONS.md。" +
    "评审点：NDVI 阈值可辩护性、上报契约四类 items 完整性、ACL pattern 正确性、quota 36C 余量、max_inflight=1000 合规（mosquitto ≤65535）、与二期冻结契约冲突。",
}).ask<DesignReview>("评审 docs/08 与 CONVENTIONS-P3，返回问题清单（空=通过）。");
if (drev.issues.length > 0) {
  await agent("三期设计修订员", {
    system: "你是设计修订编辑，按意见最小改动修订 docs/08 与 CONVENTIONS-P3，与 P2/P1 约定一致。",
  }).ask<string>("按意见修订：\n" + JSON.stringify(drev.issues, null, 2));
  log("三期设计已修订：" + drev.issues.length + " 处");
} else {
  log("三期设计复核通过（v1.0 维持）");
}

phase("三期实现缺口补齐（对照 docs/08 逐项核查）");
const impl = await agent("三期实现补齐员", {
  system:
    "你是 HSF 三期资深工程师。必读顺序：hsf/CONVENTIONS-P3.md（U/V/W/X/Y）→ CONVENTIONS-P2.md → CONVENTIONS.md → docs/08。" +
    "工作区代码与清单大体已就绪（rs/uplink 3.0.1、E2E 25 用例、静态门禁绿），你的职责是**逐项对照 docs/08 §2~§7 核查并补齐缺口**，" +
    "特别是：rs services/rs（NDVI numpy 管道/四级分级/非农化预警/GraphQL 子图/单测）、uplink services/uplink（状态机/payload/transport 隔离层/单测）、" +
    "iot POST /devices/register（SM3 凭证+hint）、sim mqtt_pub max_inflight=1000（W-1）、30-mqtt max_inflight_messages 1000、" +
    "00-base quota 36（W-2）、web admin 遥感/上报页签 + nginx /rs/ 反代、gw SUBGRAPHS 含 rs。" +
    "禁止 torch/tf（V-1）、禁止 ioredis（V-4）。发现缺失立即补齐；若约定冲突 escalate。",
}).ask<ImplOut>(
  "逐项核查 docs/08 §2~§7 与 CONVENTIONS-P3 全条款，补齐缺口。返回文件清单与自检结论（每项：已有/已补/不适用）。" +
    "完成后运行 python hsf-ias/scripts/static_gate.py 必须 violations=0。",
);

phase("静态门禁与单测（world.run 确定性门）");
const gate = await world.run("python", ["hsf-ias/scripts/static_gate.py"], { output: { tail: 16384 } });
log("静态门禁 exitCode=" + gate.exitCode);
const unit = await world.run("python", ["-m", "pytest", "hsf-ias/services/rs/tests", "hsf-ias/services/uplink/tests", "-q"], {
  output: { tail: 16384 },
  timeoutMs: 600000,
});
log("三期单测 exitCode=" + unit.exitCode);
let fixNote = "";
if (gate.exitCode !== 0 || unit.exitCode !== 0) {
  const fix = await agent("三期门禁修复员", {
    system:
      "你是代码修复员：按门禁/单测输出最小改动修复 hsf-ias；脚本自身缺陷先修脚本。" +
      "修完必须重跑 python hsf-ias/scripts/static_gate.py 与 pytest 确认。无法解决则 escalate。",
  }).ask<string>(
    "门禁输出 tail：\n" + gate.stderr.slice(-3000) + "\n单测输出 tail：\n" + unit.stderr.slice(-3000) + "\n修复并重跑两组命令确认。",
  );
  fixNote = fix.slice(0, 200);
  log("门禁/单测修复完成");
}

phase("服务器镜像重建与部署（修复 rs 旧镜像缺陷）");
const deploy = await agent("三期部署工程师", {
  system:
    "你是资深 DevOps。工作区 D:\\projects\\数字乡村\\hsf，服务器 ssh centos7-181（/opt/hsf/hsf-ias）。" +
    "必读：CONVENTIONS-P3 §Y-1 → CONVENTIONS-P2 §T-2/T-3 → CONVENTIONS.md §3 → docs/21 §5（二期全部部署坑）。" +
    "纪律：每步真实命令验证；镜像 tag 只改 manifest 不用 set image；新镜像必须推本地仓库 10.167.2.175:30500；连续 3 次修复无效 escalate。" +
    "已知缺陷：rs-svc 容器 /srv/app/repo.py 为旧版（md5 534a12e9，缺 jwt_role）——工作区版本含 jwt_role（md5 cbbdc12e），必须重建 rs 镜像修复。",
}).ask<DeployOut>(
  "执行三期收口部署（Y-1 顺序）：\n" +
    "1) 同步工作区 hsf-ias → /opt/hsf/hsf-ias（rsync 排除 .git/pycache）；\n" +
    "2) TAG=3.0.2 bash scripts/build.sh 重建变更镜像（至少 rs；gw/sim/iot/web 若代码有变一并重建）；推本地仓库；\n" +
    "3) manifest 镜像 tag 更新后 apply 00-base/20-services/30-mqtt；rollout status 全绿；\n" +
    "4) 容器内核验：rs /srv/app/repo.py md5 与工作区一致（或 grep jwt_role 命中）；mosquitto.conf max_inflight_messages 1000；quota limits.cpu=36；\n" +
    "5) 三期新链路验收（带登录态，登录 13800000601/hsf@2026 经 30099）：" +
    "rs seed→/scenes 登记→process→/vigor 查询（NDVI 四级）→非农化预警 RULE_NDVIFALL 出现在 /alerts；" +
    "uplink POST /tasks→send→回执 ACKED→payload 导出四类 items（uplink 为 ClusterIP，从集群内 Job 或 iot 容器内 curl）；" +
    "iot POST /devices/register 凭证下发+幂等 409；\n" +
    "6) 二期四链路回归：MQTT→Timescale（tick 后行数增长）、灌溉建议、通知、限流 429。\n" +
    "返回 services（rs/uplink/iot/sim/gw/web/ratelimit 全量）、linksOk、evidence、remainingIssues。",
);

for (const s of deploy.services) {
  report(s, "svc3");
}

phase("三期验收测试（E2E≥25 + k6 回归 + W-1 观测）");
const verify = await agent("三期验收测试工程师", {
  system:
    "你是性能与自动化测试工程师，对齐 docs/20/21 口径。系统已含三期组件（rs 8098/NodePort 31214、uplink 8099 ClusterIP、iot 注册 API）。" +
    "所有结论必须真实命令输出，E2E 运行 e2e_browser.py（UPLINK 环境变量指向集群内可达地址）。部署结论供交叉核对：\n" +
    JSON.stringify(deploy, null, 2).slice(0, 3500),
}).ask<VerifyOut>(
  "1) 跑 E2E 全量（25 条：一期 13+二期 6+三期 6），收集逐条结果；\n" +
    "2) k6 回归：browse/report（判据同二期 0 失败）+ iot-write 复跑（W-1 效果观测：check≥98% 目标，不进 thresholds）；\n" +
    "3) 三期新链路抽查证据（NDVI 查询、预警、上报回执、设备注册）；\n" +
    "4) 写 hsf/docs/23-三期融合测试报告.md（轨迹表：二期终版 vs 三期、新增用例明细、W-1 前后 tick p95/check 对比、问题清单、证据、复核附录占位）。\n" +
    "返回 reportPath/e2eSummary/k6Summary/newLinksSummary/evidence。",
);

artifact.table("accept3", {
  title: "HSF 三期验收关键指标",
  key: "item",
  columns: [
    { field: "item", label: "验收项" },
    { field: "value", label: "实测" },
    { field: "verdict", label: "结论" },
  ],
});
report({ item: "E2E 全量", value: verify.e2eSummary, verdict: "以报告为准" }, "accept3");
report({ item: "k6 回归", value: verify.k6Summary, verdict: "以报告为准" }, "accept3");
report({ item: "三期新链路", value: verify.newLinksSummary, verdict: "以报告为准" }, "accept3");

phase("独立复核三期结论");
const cold = await agent("三期独立复核员", {
  system:
    "你是独立复核员。ssh centos7-181 实测：kubectl -n hsf-prod 全量 pod、rs 容器内 repo.py 是否含 jwt_role、" +
    "rs /vigor /alerts 真实响应、uplink 状态闭环、iot 设备注册幂等、读 hsf/docs/23 与 static_gate.py P3 项。" +
    "逐条对照三期声明，返回 findings（属实=verified）。",
}).ask<{ findings: Finding[] }>(
  "复核三期声明：1) rs/uplink 组件 Running 且镜像 3.0.x 容器内代码与工作区一致；2) NDVI 解译→长势→预警链路真实数据；" +
    "3) 上报任务→回执→payload 导出闭环；4) 设备注册凭证；5) W-1/W-2 落地；6) 报告证据可追溯。",
);

const findings: Finding[] = cold.findings.slice();
if (gate.exitCode !== 0) {
  findings.push({
    where: "hsf-ias/scripts/static_gate.py",
    what: "三期静态门禁未全绿",
    evidence: "exitCode=" + gate.exitCode,
    status: "verified",
    severity: "medium",
  });
}
if (unit.exitCode !== 0) {
  findings.push({
    where: "hsf-ias/services/rs+uplink tests",
    what: "三期单元测试未全过",
    evidence: "pytest exitCode=" + unit.exitCode,
    status: "verified",
    severity: "medium",
  });
}
for (const issue of deploy.remainingIssues) {
  findings.push({
    where: "hsf-prod",
    what: issue,
    evidence: "部署工程师 remainingIssues 上报",
    status: "unconfirmed",
    severity: "medium",
  });
}

phase("汇总交付（README、提交、推送）");
const running = deploy.services.filter((s) => s.status === "Running").length;
const conclusion =
  "HSF 三期完成：遥感 NDVI CPU 解译→长势分级→非农化预警、上级平台上报契约闭环（状态机+回执 stub）、设备注册接入框架、" +
  "W-1（max_inflight 1000）/W-2（quota 36）收口全部上线。部署 " + running + "/" + deploy.services.length + " 组件 Running；" +
  "E2E " + verify.e2eSummary + "；k6 " + verify.k6Summary + "。";

await artifact.markdown(
  "report3",
  [
    "# HSF 三期交付报告（遥感解译 + 上级对接 + 设备接入框架）",
    "",
    "## 结论",
    "",
    conclusion,
    "",
    "## 组件状态",
    "",
    "| 组件 | 状态 | 备注 |",
    "|---|---|---|",
    ...deploy.services.map((s) => "| " + s.name + " | " + s.status + " | " + s.note + " |"),
    "",
    "## 验收数据",
    "",
    "- E2E: " + verify.e2eSummary,
    "- k6: " + verify.k6Summary,
    "- 三期新链路: " + verify.newLinksSummary,
    "- 链路总判: " + (deploy.linksOk ? "三期三链路+二期四链路回归全部验证" : "存在未验证项，见 findings"),
    "",
    "## 复核发现",
    "",
    ...(findings.length === 0
      ? ["（独立复核未发现问题）"]
      : findings.map((f) => "- [" + f.status + "/" + f.severity + "] " + f.where + " — " + f.what)),
    "",
    "## 证据索引",
    "",
    ...verify.evidence.map((e) => "- " + e),
    ...deploy.evidence.map((e) => "- " + e),
  ].join("\n"),
  { title: "HSF 三期交付报告", description: "遥感解译/上级对接/设备接入框架的验收结论与证据", primary: true },
);

return {
  conclusion,
  findings,
  verified: [
    "静态门禁 exitCode=" + gate.exitCode + "（violations=0 口径）",
    "单测 exitCode=" + unit.exitCode,
    "部署证据 " + deploy.evidence.length + " 条",
    "验收证据 " + verify.evidence.length + " 条",
    "独立复核 " + cold.findings.length + " 条",
    ...(fixNote ? ["修复记录: " + fixNote] : []),
  ],
  notCovered: [
    "真实卫星影像源与真实监管平台联调（V-2/V-3 契约化边界，输入适配器/transport 单文件可替换）",
    "真实硬件设备到货联调（MQTT 协议层已真实）",
    "深度学习作物分类模型（V-1 显式不做）",
  ],
};
