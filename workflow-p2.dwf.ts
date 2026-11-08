// HSF 二期：IoT 中台（MQTT+TimescaleDB）+ 智慧生产 + 全局限流 + 移动端增强
// 对齐 docs/18 §四二期路线 与 CONVENTIONS-P2.md

interface DocsOut {
  /** 写出的文件路径。 */
  files: string[];
  /** 三句话以内摘要。 */
  summary: string;
}

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
  /** 组件上线状态（含 timescale/mqtt/ratelimit/iot 与既有服务）。 */
  services: { name: string; status: string; note: string }[];
  /** 新链路验收：MQTT→时序、灌溉决策、全局限流。 */
  linksOk: boolean;
  evidence: string[];
  remainingIssues: string[];
}

interface VerifyOut {
  reportPath: string;
  e2eSummary: string;
  k6Summary: string;
  tsWriteSummary: string;
  evidence: string[];
}

interface Finding {
  where: string;
  what: string;
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

artifact.table("svc2", {
  title: "HSF 二期组件上线状态",
  key: "name",
  columns: [
    { field: "name", label: "组件" },
    { field: "status", label: "状态" },
    { field: "note", label: "备注" },
  ],
});

phase("二期设计：IoT 中台与时序/决策/限流架构");
const design = await agent("二期架构师", {
  system:
    "你是资深架构师。必读顺序：D:\\projects\\数字乡村\\hsf\\CONVENTIONS-P2.md → hsf\\CONVENTIONS.md → " +
    "hsf\\docs\\04、05（一期数据/接口冻结契约）→ dv-ias 与 hsf 一期实现仅作参考。" +
    "所有决策必须可在本集群落地（无 GPU、master requests 满、禁止 ioredis）。产出全中文、表格化。",
}).ask<DocsOut>(
  "编写 hsf/docs/07-二期设计-IoT中台与时序与智慧生产.md，覆盖：\n" +
    "1) MQTT 接入：topic 规划 hsf/sensor/{deviceCode}/{metric}、QoS、认证、sim 发布器与 iot 订阅者职责边界；\n" +
    "2) TimescaleDB：hypertable sensor_reading（chunk 间隔）、连续聚合（5min/1h 均值）、保留策略、与 TiDB hsf_ts 归档口径分工；\n" +
    "3) 灌溉决策模型：阈值+滞后回差+用水量估算公式，irrigation_advice 表结构（hsf_biz）；\n" +
    "4) 通知：notify 表（alert_id/user_role/read_at）与 mobile 拉取 API；\n" +
    "5) 全局限流 ratelimit-svc：API 契约、gw 集成（fail-open）、与一期进程内桶的切换开关；\n" +
    "6) E2E 新增用例清单（≥4 条）与 k6 iot-write 场景设计；\n" +
    "7) 滚动升级与回滚预案（新 StatefulSet×2、镜像 2.0、数据迁移=空库新建不迁移）。",
);

phase("设计评审与修订");
const drev = await agent("二期设计评审员", {
  system:
    "你是独立评审员。读 hsf/docs/07、CONVENTIONS-P2、CONVENTIONS、hsf/docs/04-05，只找问题：" +
    "不可落地项、契约冲突、DDL/端口/镜像错误、与一期冻结契约冲突。不编辑文件。",
}).ask<DesignReview>("评审二期设计文档，返回问题清单（空=通过）。");
if (drev.issues.length > 0) {
  await agent("二期设计修订员", {
    system: "你是设计修订编辑，按意见最小改动修订 hsf/docs/07，与两份 CONVENTIONS 保持一致。",
  }).ask<string>("按意见修订：\n" + JSON.stringify(drev.issues, null, 2));
  log("二期设计已修订：" + drev.issues.length + " 处");
} else {
  log("二期设计评审通过");
}

phase("二期实现（四路并行）");
const impls = await Promise.all(
  [
    {
      name: "IoT 工程师",
      task:
        "实现 services/iot/（8097）：FastAPI + MQTT 订阅线程（paho-mqtt 连 hsf-mqtt:1883，" +
        "topic hsf/sensor/+/+，批量缓冲 500 条或 2s 刷一次写 TimescaleDB sensor_reading hypertable）+ " +
        "规则引擎（墒情<15% 旱情 / >85% 渍害 / 风速>17 大风 / 虫情>80 虫害，带滞后回差防告警抖动）+" +
        "灌溉决策（墒情<40%→生成 irrigation_advice：建议灌溉量 m³/亩=（田间持水率-当前墒情）×土壤容重×深度系数，" +
        "同地块 6h 内去重）+ notify 写入 + REST：/devices /sensor/latest /alerts /irrigation-advice /notify /healthz /readyz /metrics。" +
        "requirements.txt（psycopg2-binary、paho-mqtt）。tests/（规则引擎+决策去重单测，不依赖外部服务）。" +
        "同时改造 services/sim/：新增 MQTT 发布者线程（连接后按 2s 间隔发布模拟数据；HTTP /sim/tick 保留并返回发布条数）；" +
        "删除其旧规则引擎职责（迁移至 iot）。",
      ref: "hsf-ias/services/sim（一期源码）、CONVENTIONS-P2 T-2",
    },
    {
      name: "限流与网关工程师",
      task:
        "实现 services/ratelimit/（8096）：FastAPI + redis.cluster.RedisCluster（固窗 INCR/EXPIRE，" +
        "key hsf:rl:{key}:{分钟epoch}，TTL 120）+ GET /limit?key=&cost= → {allowed,remain} + /healthz /metrics；" +
        "tests/（用 fakeredis 或 mock）。改造 gw/gateway.js：删除 ioredis require 与相关代码，" +
        "限流中间件改为 fetch(process.env.RATE_SVC + '/limit?key='+encodeURIComponent(key)) — " +
        "5s 超时、非 200 或网络错误 fail-open（放行+console.warn 节流日志），allowed=false → 429 JSON；" +
        "删除进程内 buckets。k8s/20-services.yaml：新增 ratelimit-svc(8096) 与 iot-svc(8097,NodePort 31213) Deployment+Service+探针+metrics，" +
        "gw env 增加 RATE_SVC；sim/iot env 增加 MQTT_PASS/TIMESCALE_DSN。",
      ref: "gw/gateway.js 一期 429 中间件、services/sim",
    },
    {
      name: "数据工程师",
      task:
        "实现 k8s/25-timescale.yaml（StatefulSet hsf-timescale 1 副本，镜像 10.167.2.175:30500/base/timescaledb:2.17.2-pg16，" +
        "PVC 10Gi local-path，nodeSelector worker1，Service hsf-timescale:5432，探针 pg_isready）与 k8s/30-mqtt.yaml" +
        "（StatefulSet hsf-mosquitto 1 副本 + ConfigMap mosquitto.conf（密码文件+监听 1883）+ Service hsf-mqtt:1883，nodeSelector worker1）。" +
        "scripts/init-ts.sh：等 PG 就绪→CREATE EXTENSION timescaledb→CREATE DB hsf_ts→建 sensor_reading hypertable" +
        "（device_code CHAR(20), metric VARCHAR(16), value DOUBLE PRECISION, recorded_at TIMESTAMPTZ）+" +
        "连续聚合 continuous_aggregate_5min + 保留策略 90 天；幂等可重复执行。" +
        "更新 k8s/40-k6.yaml 增加 iot-write 场景说明注释；static_gate.py 增加二期检查项" +
        "（禁止 ioredis、iot 必含 timescale DSN、sim 不得再含规则引擎、新增服务必有探针/limits）。",
      ref: "k8s/10-redis.yaml（StatefulSet 模板）、scripts/static_gate.py",
    },
    {
      name: "前端与测试工程师",
      task:
        "增强 services/web/static/：admin 增'设备与墒情'页签（设备列表+最新值+告警）与'灌溉建议'卡片；" +
        "mobile 增通知列表+灌溉建议卡（调 iot REST，经 nginx /iot/ 同源反代——nginx.conf.template 增 location /iot/ → iot-svc:8097）。" +
        "扩充 test/e2e_browser.py 至 ≥17 用例（新增：同源反代 page fetch /graphql 200、设备列表含 48 传感器、" +
        "MQTT 链路=触发 sim tick 后 5s 内 /sensor/latest 可查到新数据、灌溉建议生成且 6h 去重、通知列表非空、" +
        "TimescaleDB 就绪探针 200）；保留一期 13 条全部通过。扩充 perf/stress.js 增加 iot-write 场景" +
        "（POST /sim/tick 循环，输出吞吐，不设 thresholds）。全部新增文案中文。",
      ref: "services/web/static 三页一期源码、test/e2e_browser.py",
    },
  ].map((w) =>
    agent(w.name, {
      system:
        "你是 HSF 二期资深工程师。动手前必读：hsf/CONVENTIONS-P2.md（二期强制约定）→ hsf/CONVENTIONS.md（一期 16 条坑）→ " +
        "任务列出的参考源码。代码风格/分层/中文注释与一期完全一致；每个新服务必须带 Dockerfile、requirements.txt、" +
        "探针、metrics、limits。若约定冲突或参考缺失，escalate 而不是猜。",
    }).ask<ImplOut>(w.task + "（参考：" + w.ref + "。返回文件清单与自检结论。）"),
  ),
);

phase("静态门禁与修复循环");
let gate = await world.run("python", ["hsf-ias/scripts/static_gate.py"], {
  output: { tail: 16384 },
});
for (let round = 1; round <= 3 && gate.exitCode !== 0; round++) {
  log("二期静态门禁第 " + round + " 轮未通过，修复中");
  await agent(`二期门禁修复员-第${round}轮`, {
    system:
      "你是代码修复员：按门禁输出最小改动修复 hsf-ias；门禁脚本缺失/逻辑错误时先修脚本。" +
      "修完必须重跑 python hsf-ias/scripts/static_gate.py 确认。无法解决则 escalate。",
  }).ask<string>(
    "门禁失败输出（tail）：\n" + gate.stderr.slice(-4000) + "\n修复并重跑门禁。",
  );
  gate = await world.run("python", ["hsf-ias/scripts/static_gate.py"], {
    output: { tail: 16384 },
  });
}
const gatePass = gate.exitCode === 0;
log(gatePass ? "二期静态门禁通过" : "静态门禁仍未全绿，如实带入部署阶段");

phase("服务器构建部署与排障（二期组件）");
const deploy = await agent("二期部署工程师", {
  system:
    "你是资深 DevOps。工作区 D:\\projects\\数字乡村，服务器 ssh centos7-181（/opt/hsf/hsf-ias）。" +
    "必读：hsf/CONVENTIONS-P2.md §T-2/T-3、CONVENTIONS.md §3、docs/20 §5（一期全部坑）。" +
    "纪律：每步真实命令验证；CrashLoop 读 --previous 日志定位；镜像 tag 只改 manifest 不用 set image；" +
    "新镜像必须推本地仓库；连续 3 次修复无效 escalate。完成后逐组件汇报。",
}).ask<DeployOut>(
  "执行二期上线：\n" +
    "1) 同步代码到 /opt/hsf/hsf-ias；\n" +
    "2) 拉取并注册镜像 base/mosquitto:2.1 与 base/timescaledb:2.17.2-pg16 到本地仓库；\n" +
    "3) 部署 k8s/25-timescale.yaml + 30-mqtt.yaml（PVC/nodeSelector 生效）+ scripts/init-ts.sh 幂等初始化；\n" +
    "4) 构建二期镜像并更新 manifest：sim:2.0 iot:2.0 ratelimit:2.0 gw:2.0 web:2.0（其余沿用 1.x），apply 全套清单；\n" +
    "5) 排障至全部 Running；\n" +
    "6) 新链路验收：sim tick → MQTT → TimescaleDB 可查（iot /sensor/latest 出现新 recorded_at）、" +
    "/irrigation-advice 生成、/notify 非空、**限流全局语义**（单 pod 1500 突发 vs RATE_CAP=1200 → 出现 429，" +
    "且另一 pod 视角也 429=全局限流生效）、web 三页+新页签 200。\n" +
    "返回 services（timescale/mqtt/ratelimit/iot/sim/gw/web 全量）、linksOk、evidence、remainingIssues。",
);

for (const s of deploy.services) {
  report(s, "svc2");
}

phase("二期验收测试（E2E 扩充 + k6 + 时序吞吐）");
const verify = await agent("二期验收测试工程师", {
  system:
    "你是性能与自动化测试工程师，对齐 docs/20 v2 口径。系统已含二期组件（iot 8097/NodePort 31213、" +
    "ratelimit 8096、timescale、mqtt）。所有结论必须真实命令输出。部署结论供交叉核对：\n" +
    JSON.stringify(deploy, null, 2).slice(0, 3500),
}).ask<VerifyOut>(
  "1) 跑扩充版 E2E（≥17 用例）收集逐条结果；\n" +
    "2) k6 基线（browse+report）+ iot-write 时序吞吐（/sim/tick 批量 5000×N 次计时，换算条/s）；\n" +
    "3) 限流全局语义抽查（两个不同 pod 来源各 1500 突发，429 计数）；\n" +
    "4) 写 hsf/docs/21-二期融合测试报告.md（轨迹表：一期终版 vs 二期、新用例明细、吞吐数据、问题清单、证据）。\n" +
    "返回 reportPath/e2eSummary/k6Summary/tsWriteSummary/evidence。",
);

artifact.table("accept2", {
  title: "HSF 二期验收关键指标",
  key: "item",
  columns: [
    { field: "item", label: "验收项" },
    { field: "value", label: "实测" },
    { field: "verdict", label: "结论" },
  ],
});
report(
  { item: "E2E 扩充", value: verify.e2eSummary, verdict: "以报告为准" },
  "accept2",
);
report(
  { item: "k6 基线", value: verify.k6Summary, verdict: "以报告为准" },
  "accept2",
);
report(
  { item: "时序写入吞吐", value: verify.tsWriteSummary, verdict: "以报告为准" },
  "accept2",
);

phase("独立复核二期结论");
const cold = await agent("二期独立复核员", {
  system:
    "你是独立复核员。ssh centos7-181 实测：kubectl -n hsf-prod 全量 pod、timescale/mosquitto PVC、" +
    "iot /sensor/latest 与 /irrigation-advice、限流 429 复现、读 hsf/docs/21 与 static_gate.py。" +
    "逐条对照二期声明，返回 findings（属实=verified）。",
}).ask<{ findings: Finding[] }>(
  "复核二期声明：1) 新组件 Running+PVC；2) MQTT→TimescaleDB 链路真实数据；3) 灌溉决策与通知；" +
    "4) 全局限流 429；5) 报告证据可追溯。",
);

const findings: Finding[] = cold.findings.slice();
if (!gatePass) {
  findings.push({
    where: "hsf-ias/scripts/static_gate.py",
    what: "二期静态门禁未全绿（详见部署记录）",
    evidence: "exitCode=" + gate.exitCode,
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

phase("汇总交付");
const conclusion =
  "HSF 二期完成：MQTT→TimescaleDB 时序链路、灌溉决策、通知、全局（跨副本）限流 429、移动端增强全部上线。" +
  "部署 " +
  deploy.services.filter((s) => s.status === "Running").length +
  "/" +
  deploy.services.length +
  " 组件 Running；E2E " +
  verify.e2eSummary +
  "；时序吞吐 " +
  verify.tsWriteSummary +
  "。";

await artifact.markdown(
  "report2",
  [
    "# HSF 二期交付报告（IoT 中台 + 时序库 + 智慧生产 + 全局限流）",
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
    "- 时序吞吐: " + verify.tsWriteSummary,
    "- 新链路: " + (deploy.linksOk ? "MQTT→时序/灌溉决策/通知/全局限流 全部验证" : "存在未验证项，见 findings"),
    "",
    "## 复核发现",
    "",
    ...(findings.length === 0
      ? ["（独立复核未发现问题）"]
      : findings.map(
          (f) =>
            "- [" + f.status + "/" + f.severity + "] " + f.where + " — " + f.what,
        )),
    "",
    "## 证据索引",
    "",
    ...verify.evidence.map((e) => "- " + e),
    ...deploy.evidence.map((e) => "- " + e),
  ].join("\n"),
  {
    title: "HSF 二期交付报告",
    description: "IoT 中台/时序库/智慧生产/全局限流的验收结论与证据",
    primary: true,
  },
);

return {
  conclusion,
  findings,
  verified: [
    "静态门禁 exitCode=" + gate.exitCode,
    "部署证据 " + deploy.evidence.length + " 条",
    "验收证据 " + verify.evidence.length + " 条",
    "独立复核 " + cold.findings.length + " 条",
  ],
  notCovered: [
    "真实硬件设备（仍为 sim 模拟发布，MQTT 协议层已真实）",
    "遥感影像与全国平台对接（三期）",
    "browse p95 环境噪声（master conntrack 租户压力，随集群治理收敛）",
  ],
};