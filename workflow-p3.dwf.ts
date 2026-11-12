// HSF 三期：遥感解译（CPU 路线）+ 上级平台对接 + 设备接入框架 + 二期残余收口
// 对齐 docs/08-三期设计 与 CONVENTIONS-P3.md；范式与 workflow-p2.dwf.ts 一致。

interface ImplOut { files: string[]; summary: string }
interface DesignReview { issues: string[] }
interface DeployOut {
  services: { name: string; status: string; note: string }[];
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

phase("三期设计：遥感(CPU)/上级对接/设备接入框架");
// 产出：docs/08-三期设计-遥感解译与上级对接与设备接入.md v1.0 + CONVENTIONS-P3.md（U/V/W/X/Y 冻结）
phase("设计评审与修订");
// 评审点：NDVI 阈值可辩护性、上报契约与统一编码映射完整性、ACL pattern 正确性、
//         quota 36C 余量评估、max_inflight=1000 的 broker 上限合规（mosquitto ≤65535）
phase("三期实现（四路并行）");
const impls = await Promise.all([
  { name: "遥感工程师", task: "services/rs（8098）：FastAPI+numpy（V-1 无 torch）——rs_scene/rs_vigor DDL、" +
    "NDVI 管道（掩膜线性映射）、四级长势分级、非农化预警（RULE_NDVIFALL 写 alert_record+notify）、" +
    "REST /scenes /scenes/{code}/process /vigor /vigor/latest /alerts + GraphQL 子图 /graphql + 探针三件套；" +
    "sample/make_sample.py + seed_rs.py 确定性样本（第二景对 L5306000003 降幅 0.2 触发预警）" },
  { name: "对接工程师", task: "services/uplink（8099）：uplink_task DDL+状态机 PENDING→SENT→ACKED/REJECTED、" +
    "payload 组装（L/P/F/S→四类 items）、transport.py 隔离层（stub 回执）、/tasks /tasks/{batch}/send " +
    "/tasks/{batch}/payload + /healthz /readyz /metrics；幂等 uk_batch" },
  { name: "设备接入工程师", task: "iot 3.0：POST /devices/register（SM3 凭证一次下发+hint 落库）+" +
    "registry 刷新复用；30-mqtt.yaml ACL pattern readwrite hsf/sensor/%u/#；" +
    "sim 3.0：max_inflight 100→1000（W-1）；00-base quota 32→36（W-2）" },
  { name: "前端与测试工程师", task: "web 3.0：admin 遥感长势页签+上级上报页签、nginx /rs/ 反代；" +
    "e2e_browser.py ≥25 用例（+6 条三期）；static_gate P3 检查项；k8s/20-services.yaml 增 rs-svc/uplink-svc、" +
    "gw SUBGRAPHS 增 rs、全部 3.0 tag；build.sh/deploy.sh 三期化" },
]);
phase("静态门禁与修复循环"); // python hsf-ias/scripts/static_gate.py → violations=0
phase("服务器构建部署与排障（三期组件）"); // ssh centos7-181 /opt/hsf/hsf-ias；镜像 3.0；rollout 全绿
phase("三期验收测试（E2E≥25 + k6 回归 + 三期新链路）"); // docs/22-三期融合测试报告.md
phase("独立复核三期结论"); // ssh 实测交叉验证，findings
phase("汇总交付"); // README 更新 + git commit + push origin main
return { note: "三期按 CONVENTIONS-P3 Y-1 顺序执行；工作流文件为编排记录，实际执行由 Loomy 主线完成" };
