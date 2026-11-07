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

    for rule, loc, snippet in findings:
        print("VIOLATION | %-24s | %s | %s" % (rule, loc, snippet))
    print("SUMMARY violations=%d files=%d root=%s" % (len(findings), n_files, root))
    if findings:
        print("❌ STATIC-GATE FAIL（逐条对照 hsf/CONVENTIONS.md §2 修复后重跑）")
        return 1
    print("✅ STATIC-GATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
