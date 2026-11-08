# -*- coding: utf-8 -*-
"""HSF 静态门禁（grep 式，进 CI/部署前置）：逐条对应 CONVENTIONS.md §2 的真实事故修复项。

检查项（命中即报错，退出码 1）：
  1. tidb-read        —— §0/§2.9 不存在 tidb-read 主机名，双池 host 一律 tidb.infra.svc.cluster.local:4000
  2. 保留字裸用       —— §2.2 保留字做列名必须反引号（检查 DDL 列定义形态）
  3. CHAR(12)         —— §2.3 编码列宽最小 CHAR(16)（最长 18 位时 CHAR(20)，宁可宽不可窄）
  4. PartyRef         —— §2.4 联邦桩必须 @key + _resolve_reference 模式，不允许旁路桩类型
  5. JSONResponse+generate_latest —— §2.8 /metrics 必须 Response(generate_latest())，JSONResponse 转义引号毁 Prometheus
  6. CDN 引用         —— §0/§2.12 局域网客户端不可达 CDN，前端库文件必须进镜像
  7. federation Schema —— §2.4 四子图 schema.py 必须用 strawberry.federation.Schema 且显式 types=[...]，
                          未注册实体不进 SDL、网关组合直接 400

二期检查项（docs/07 §6.4 冻结，CONVENTIONS-P2 T-2/T-3）：
  8. ioredis          —— T-3 红线：全仓代码+package.json 禁用 ioredis（检查 require/import/依赖键
                          使用形态；注释豁免；自动生成锁文件豁免，失配由 npm ci 自行拦截）
  9. iot 缺 timescale DSN —— services/iot 必含 TIMESCALE_DSN 与 paho-mqtt（paho.mqtt 同计；目录缺失=交付缺失）
 10. sim 规则引擎残留 —— D-07-1 职责移交：services/sim 源码不得再含 RULE_DROUGHT 等 4 条冻结规则符号
                          （注释行豁免；值域生成 next_value/METRIC_RANGES 属 sim 保留职责不在检查面）
 11. 新服务缺探针/limits —— iot/ratelimit 代码必含 /healthz+/readyz+/metrics（§2.14）；
                          k8s 清单对应 workload 必含 readinessProbe+livenessProbe+resources.limits（§2.15）
 12. gw 缺 RATE_MODE/RATE_SVC —— §5.2/§5.3 gw 必含 RATE_MODE（svc 默认/local 回退开关）与 RATE_SVC
 13. E2E<17           —— §6.1 E2E 用例数 ≥17（e2e_browser.py 的 rec() 首参去重计数，try/except 同名合并）

用法：python static_gate.py [--root 仓库根]（默认脚本上级目录）
"""
import argparse
import re
import sys
from pathlib import Path

# Windows GBK 控制台兼容：强制 UTF-8 输出，异常字符降级不抛错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SELF = Path(__file__).resolve()

# 各规则作用的文件后缀
EXT_ALL = {".py", ".sql", ".sh", ".js", ".ts", ".html", ".css", ".yaml", ".yml"}
EXT_CODE = {".py", ".sql"}          # SQL/代码规则
EXT_WEB = {".html", ".js", ".css", ".py", ".ts"}  # CDN 规则

# 四子图 federation schema 检查路径（相对仓库根）
SUBGRAPH_SCHEMAS = ["services/iam/app/schema.py", "services/land/app/schema.py",
                    "services/project/app/schema.py", "services/facility/app/schema.py"]

CDN_PATTERNS = [
    "unpkg.com", "cdn.jsdelivr.net", "cdnjs.cloudflare.com", "ajax.googleapis.com",
    "fonts.googleapis.com", "fonts.gstatic.com", "bootcdn.net", "cdn.bootcss.com",
    "staticfile.org", "baomitu.com", "lib.sinaapp.com",
]
RESERVED = ("column", "order", "desc", "group", "condition")
# 保留字裸用做列名：未反引号的保留字后紧跟 SQL 类型关键字（DDL 列定义形态）
RESERVED_RE = re.compile(
    r"(?<![`\w])(%s)\s+(CHAR\(|VARCHAR\(|BIGINT\b|INT\b|DATETIME|DECIMAL\(|TEXT\b|DATE\b|FLOAT\b)"
    % "|".join(RESERVED), re.IGNORECASE)
JSON_METRICS_RE = re.compile(r"JSONResponse\([^)]*generate_latest", re.IGNORECASE | re.DOTALL)

# ── 二期检查项常量（docs/07 §6.4；CONVENTIONS-P2 T-2/T-3）──────────────────────
# 锁文件豁免：自动生成物，检查项 ① 冻结范围=代码+package.json（package-lock 失配由 npm ci 拦截）
LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
# ioredis 使用形态：require()/import 导入 或 package.json 依赖键——只禁使用，注释里的历史决策说明豁免
IOREDIS_RE = re.compile(
    r"""require\s*\(\s*['"]ioredis|from\s+['"]ioredis|['"]ioredis['"]\s*:|import\s+[^;\n]*['"]ioredis['"]""")
# 二期新增服务（iot=时序+规则引擎+决策；ratelimit=全局限流，CONVENTIONS-P2 T-2）
P2_NEW_SVCS = ("iot", "ratelimit")
P2_ENDPOINTS = ("/healthz", "/readyz", "/metrics")   # CONVENTIONS §2.14 / docs/07 §6.4④
# D-07-1 冻结规则符号（sim 一期 4 条告警规则移交 iot；值域生成不在检查面）
P2_RULE_SYMBOLS_RE = re.compile(
    r"\b(RULE_DROUGHT|RULE_WATERLOGGING|RULE_GALE|RULE_PEST)\b")
P2_E2E_MIN = 17        # docs/07 §6.1：一期 13 条全保留 + 新增 6 条 = 19 ≥ 17


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.resolve()
        if rel == SELF:                      # 门禁自身含规则字面量，跳过
            continue
        if any(part in (".git", "node_modules", "__pycache__", ".zcode") for part in p.parts):
            continue
        yield p


def check_file(path: Path, findings: list):
    ext = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        findings.append(("读文件失败", f"{path}:0", str(e)))
        return
    for lineno, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        # 1. tidb-read 主机名
        if ext in EXT_ALL and "tidb-read" in low:
            findings.append(("tidb-read", f"{path}:{lineno}", line.strip()[:120]))
        # 2. 保留字裸用列定义
        if ext in EXT_CODE and RESERVED_RE.search(line):
            findings.append(("保留字裸用", f"{path}:{lineno}", line.strip()[:120]))
        # 3. CHAR(12) 编码列宽不足
        if ext in EXT_CODE and re.search(r"CHAR\(12\)", line, re.IGNORECASE):
            findings.append(("CHAR(12)", f"{path}:{lineno}", line.strip()[:120]))
        # 4. PartyRef 旁路桩
        if ext in EXT_CODE and "PartyRef" in line:
            findings.append(("PartyRef", f"{path}:{lineno}", line.strip()[:120]))
        # 5. JSONResponse 包 generate_latest（/metrics 被转义）——注释行豁免（约定原文常被注释引用）
        if ext in EXT_CODE and "generate_latest" in low and "JSONResponse" in line \
                and not line.lstrip().startswith(("#", "--", "//")):
            findings.append(("JSONResponse*generate_latest", f"{path}:{lineno}", line.strip()[:120]))
        # 6. CDN 引用
        if ext in EXT_WEB:
            hit = next((d for d in CDN_PATTERNS if d.lower() in low), None)
            if hit is None and re.search(r"(?<![\w.])cdn\.[\w-]+\.(com|net|org)", low):
                hit = "cdn.*"
            if hit:
                findings.append(("CDN引用", f"{path}:{lineno}", line.strip()[:120]))
        # 8. ioredis 使用（P2/T-3 红线：二期一律不用；使用形态=import/require/依赖键，
        #    注释与自动生成锁文件豁免——IOREDIS_RE 命中即真实引用）
        if (ext in EXT_ALL or ext == ".json") and path.name not in LOCK_FILES \
                and IOREDIS_RE.search(line):
            findings.append(("ioredis", f"{path}:{lineno}", line.strip()[:120]))
    # 5 的跨行形态（JSONResponse(...) 括号内换行跟 generate_latest）
    if path.suffix.lower() in EXT_CODE and JSON_METRICS_RE.search(text):
        findings.append(("JSONResponse*generate_latest", f"{path}:0", "跨行 JSONResponse(... generate_latest ...)"))


def check_federation(root: Path, findings: list):
    """§2.4：四子图必须 strawberry.federation.Schema(... types=[...])；裸 strawberry.Schema 视为未启用联邦。"""
    for rel in SUBGRAPH_SCHEMAS:
        p = root / rel
        if not p.is_file():
            findings.append(("federation Schema", rel + ":0", "子图 schema 文件缺失（四子图必须显式 types 注册）"))
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        flat = re.sub(r"\s+", "", text)
        if "strawberry.federation.Schema(" not in flat and "strawberry.federation.Schema(" not in text:
            findings.append(("federation Schema", rel + ":0", "未使用 strawberry.federation.Schema"))
        elif "types=[" not in flat:
            findings.append(("federation Schema", rel + ":0", "Schema 未显式声明 types=[...]（未注册实体不进 SDL，网关组合 400）"))
        if re.search(r"(?<![\w.])strawberry\.Schema\(", text):
            findings.append(("federation Schema", rel + ":0", "误用非联邦 strawberry.Schema"))


def _dir_text(base: Path) -> str:
    """目录内全部文件文本拼接（跳过 __pycache__；供 P2 存在性检查用）。"""
    return "".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in sorted(base.rglob("*")) if p.is_file() and "__pycache__" not in p.parts)


def check_iot_dsn(root: Path, findings: list):
    """§6.4②：services/iot 必含 TIMESCALE_DSN 与 paho-mqtt/paho.mqtt；目录缺失=二期交付缺失（T-2）。"""
    base = root / "services" / "iot"
    if not base.is_dir():
        findings.append(("iot缺timescale DSN", "services/iot:0", "iot 服务目录缺失（二期必交付，CONVENTIONS-P2 T-2）"))
        return
    text = _dir_text(base)
    if "TIMESCALE_DSN" not in text:
        findings.append(("iot缺timescale DSN", "services/iot:0",
                         "未含 TIMESCALE_DSN（时序连接必须走该 env，docs/07 §1.4：postgresql://hsf:***@hsf-timescale:5432/hsf_ts）"))
    if not re.search(r"paho[-.]mqtt", text):
        findings.append(("iot缺timescale DSN", "services/iot:0",
                         "未含 paho-mqtt/paho.mqtt（MQTT 订阅依赖，T-2：paho-mqtt 订阅 hsf/sensor/+/+）"))


def check_sim_rules(root: Path, findings: list):
    """§6.4③/D-07-1：sim 源码不得再含规则引擎符号（4 条冻结规则已移交 iot；值域生成不在检查面）。"""
    base = root / "services" / "sim"
    if not base.is_dir():
        return                       # sim 目录缺失由其它机制兜底，此处不重复报
    for p in sorted(base.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue             # 注释行豁免（迁移说明性注释允许，同检查项 5 豁免口径）
            if P2_RULE_SYMBOLS_RE.search(line):
                findings.append(("sim规则引擎残留", f"{p}:{lineno}", line.strip()[:120]))


def check_p2_new_services(root: Path, findings: list):
    """§6.4④：新增服务（iot/ratelimit）代码必含 /healthz+/readyz+/metrics；
    k8s 清单对应 workload 必存在且含 readinessProbe+livenessProbe+resources.limits。"""
    for svc in P2_NEW_SVCS:
        # 代码面：探针/metrics 三端点
        base = root / "services" / svc
        if not base.is_dir():
            findings.append(("新服务缺探针/limits", f"services/{svc}:0",
                             "新服务目录缺失（二期必交付，CONVENTIONS-P2 T-2）"))
        else:
            text = _dir_text(base)
            for ep in P2_ENDPOINTS:
                if ep not in text:
                    findings.append(("新服务缺探针/limits", f"services/{svc}:0",
                                     f"代码未含 {ep}（CONVENTIONS §2.14：/healthz 不依赖DB /readyz 校验依赖 /metrics 暴露）"))
        # 清单面：iot/ratelimit 的 Deployment 必存在且带探针与 resources.limits（§2.14/§2.15）
        found = False
        for yml in sorted((root / "k8s").glob("*.yaml")):
            text = yml.read_text(encoding="utf-8", errors="ignore")
            for doc in re.split(r"(?m)^---\s*$", text):
                if not re.search(r"kind:\s*(Deployment|StatefulSet)\b", doc):
                    continue
                # 精确归属：仅命中 name:/image: 含本服务名的 workload 文档，避免头注/别处提及误报
                if not re.search(r"(?:name|image):\s*\S*" + svc + r"(-svc)?\b", doc):
                    continue
                found = True
                for req, hint in (("readinessProbe", "readiness 探针"), ("livenessProbe", "liveness 探针"),
                                  ("limits:", "resources.limits")):
                    if req not in doc:
                        findings.append(("新服务缺探针/limits", f"{yml}:0",
                                         f"{svc} workload 缺 {hint}（CONVENTIONS §2.14/§2.15）"))
        if not found:
            findings.append(("新服务缺探针/limits", "k8s:0",
                             f"k8s 清单缺 {svc} workload（workflow 冻结任务：20-services.yaml 增量必含 Deployment+Service+探针+metrics）"))


def check_gw_rate_env(root: Path, findings: list):
    """§6.4⑤：gw 必含 RATE_MODE（svc 默认/local 回退开关，§5.3）与 RATE_SVC（§5.2）。"""
    gw = root / "gw"
    if not gw.is_dir():
        findings.append(("gw缺RATE_MODE/RATE_SVC", "gw:0", "gw 目录缺失"))
        return
    text = _dir_text(gw)
    for envname in ("RATE_MODE", "RATE_SVC"):
        if envname not in text:
            findings.append(("gw缺RATE_MODE/RATE_SVC", "gw:0",
                             f"未含 {envname}（docs/07 §5.2/§5.3：svc 模式默认 + local 回退开关必须以代码存在为前提）"))


def check_e2e_count(root: Path, findings: list):
    """§6.4⑥：E2E 用例数 ≥17（rec(name,...) 首参去重计数——try/except 同名重复合并为同一用例）。"""
    p = root / "test" / "e2e_browser.py"
    if not p.is_file():
        findings.append(("E2E<17", "test/e2e_browser.py:0", "E2E 脚本缺失"))
        return
    names = set(re.findall(r'rec\(\s*"([^"]+)"', p.read_text(encoding="utf-8", errors="ignore")))
    if len(names) < P2_E2E_MIN:
        findings.append(("E2E<17", "test/e2e_browser.py:0",
                         f"用例数={len(names)}（要求 ≥{P2_E2E_MIN}，docs/07 §6.1：一期 13 条全保留 + 新增 6 条）"))


def main():
    ap = argparse.ArgumentParser(description="HSF 静态门禁")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                    help="仓库根（默认脚本上级目录）")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    findings = []
    n_files = 0
    for p in iter_files(root):
        n_files += 1
        check_file(p, findings)
    check_federation(root, findings)
    check_iot_dsn(root, findings)
    check_sim_rules(root, findings)
    check_p2_new_services(root, findings)
    check_gw_rate_env(root, findings)
    check_e2e_count(root, findings)

    for rule, loc, snippet in findings:
        print("VIOLATION | %-24s | %s | %s" % (rule, loc, snippet))
    print("SUMMARY violations=%d files=%d root=%s" % (len(findings), n_files, root))
    if findings:
        print("❌ STATIC-GATE FAIL（逐条对照 hsf/CONVENTIONS.md §2 与 docs/07 §6.4 二期检查项修复后重跑）")
        return 1
    print("✅ STATIC-GATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
