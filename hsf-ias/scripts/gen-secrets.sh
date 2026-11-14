#!/bin/bash
# HSF 运行时密钥生成与注入（R-07，2026-11-14）——部署期在部署机执行，真值不入 git。
# 依据：docs/24 §4 R-07（真值出库：Secret manifest 改占位符，部署期由本脚本注入）；
#       CONVENTIONS §2.18（运行时密钥治理）；docs/04 §7.3 / docs/07 §1.4 键名冻结。
# 生成规则（CSPRNG）：
#   - openssl rand -hex N：N=16→32 hex 字符（口令类）；JWT_SECRET 24→48；SM4_FIELD_KEY 32→64（32 字节）
#   - prod/dev 分值：JWT_SECRET 强制分值（R-07 验收②）；其余键为共享单实例（TiDB/Redis/MQTT/Timescale
#     各一套），两命名空间同值——组件级分值需独立实例，已在 00-base.yaml 注释显式声明
# 幂等语义：真值文件 k8s/gen/hsf-secrets.env 已存在则复用（重复执行=重新 apply 同值，不轮换）；
#   全量轮换需显式传入 ROTATE=1（生成新值文件后再 apply——轮换必须配套组件改密+服务滚动，
#   见 CONVENTIONS §2.18 顺序：组件改密 → 本脚本 → 全量服务滚动）
# 安全：gen/ 目录写 .gitignore 排除；真值文件 chmod 600；不回显真值（仅键名与长度）
set -euo pipefail
cd "$(dirname "$0")/.."
GEN_DIR=k8s/gen
VALUES_FILE=$GEN_DIR/hsf-secrets.env

mkdir -p "$GEN_DIR"
# gen/ 双保险：目录内全部产物不入 git（.gitignore 条目另见仓库根）
grep -qxF "$GEN_DIR/" .gitignore 2>/dev/null || echo "$GEN_DIR/" >> .gitignore

if [ -f "$VALUES_FILE" ] && [ "${ROTATE:-0}" != "1" ]; then
  echo "gen-secrets: 复用既有真值文件 $VALUES_FILE（ROTATE=1 可强制重新生成）"
  # R-20/ADR-HSF-018（2026-11-14）增量补键：既有真值文件缺 MQTT_PASS_SIM/MQTT_PASS_IOT 时
  # 追加生成（存量键不轮换——幂等复用语义不变）
  ADD_KEY() { # $1=key $2=hex 长度
    if ! grep -q "^$1=" "$VALUES_FILE"; then
      echo "$1=$(openssl rand -hex "$2")" >> "$VALUES_FILE"
      chmod 600 "$VALUES_FILE"
      echo "gen-secrets: 增量补键 $1（R-20 分账号）"
    fi
  }
  ADD_KEY MQTT_PASS_SIM 16
  ADD_KEY MQTT_PASS_IOT 16
  # R-31/ADR-HSF-027（2026-11-14）增量补键：JWT RS256 密钥族——存量真值文件缺键时追加
  # （存量键不轮换——幂等复用语义不变）。值=PEM 的 base64 单行（--from-env-file 不接受多行）
  if ! grep -q "^JWT_PRIVATE_KEY_PROD=" "$VALUES_FILE"; then
    # prod/dev 分值（JWT_SECRET 同原则：dev 私钥泄露不得可签发 prod 可信令牌）——两对独立密钥
    _R31_GEN() { # $1=输出前缀
      openssl genrsa -out "/tmp/gs_$1.pem" 2048 2>/dev/null
      openssl rsa -in "/tmp/gs_$1.pem" -pubout -out "/tmp/gs_$1.pub" 2>/dev/null
      echo "JWT_PRIVATE_KEY_$1=$(base64 -w0 < "/tmp/gs_$1.pem")"
      echo "JWT_PUBLIC_KEY_$1=$(base64 -w0 < "/tmp/gs_$1.pub")"
      rm -f "/tmp/gs_$1.pem" "/tmp/gs_$1.pub"
    }
    {
      echo "JWT_ALG=HS256"                                   # 冻结兼容开关默认值（切换 RS256 仅改本键+滚动）
      echo "JWT_KID=rsa-$(date +%Y%m)"
      _R31_GEN PROD
      _R31_GEN DEV
      echo "JWT_PUBLIC_KEY_PREV="
      echo "JWT_KID_PREV="
    } >> "$VALUES_FILE"
    chmod 600 "$VALUES_FILE"
    echo "gen-secrets: 增量补键 JWT_ALG/JWT_KID/JWT_PRIVATE_KEY(_PROD/_DEV)/JWT_PUBLIC_KEY(_PROD/_DEV)(+PREV)（R-31 RS256 密钥族，prod/dev 分值，默认 HS256 模式）"
  fi
elif [ "${ROTATE:-0}" = "1" ] && [ "${ACK_ROTATE:-0}" != "1" ]; then
  echo "gen-secrets: ROTATE=1 需显式 ACK_ROTATE=1——轮换必须配套组件改密+全量服务滚动（CONVENTIONS §2.18）" >&2
  exit 2
else
  echo "gen-secrets: 生成新真值集（CSPRNG=openssl rand）→ $VALUES_FILE"
  {
    echo "DB_PASS=$(openssl rand -hex 16)"
    echo "DB_PASS_RO=$(openssl rand -hex 16)"
    echo "REDIS_PASS=$(openssl rand -hex 16)"
    echo "MQTT_PASS=$(openssl rand -hex 16)"
    echo "MQTT_PASS_SIM=$(openssl rand -hex 16)"   # R-20/ADR-HSF-018：sim 发布账号 hsf-sim
    echo "MQTT_PASS_IOT=$(openssl rand -hex 16)"   # R-20/ADR-HSF-018：iot 订阅账号 hsf-iot
    echo "TIMESCALE_PASS=$(openssl rand -hex 16)"
    echo "JWT_SECRET_PROD=$(openssl rand -hex 24)"
    echo "JWT_SECRET_DEV=$(openssl rand -hex 24)"
    echo "SM4_FIELD_KEY=$(openssl rand -hex 32)"
    # R-31/ADR-HSF-027：JWT RS256 密钥族（RSA-2048 ×2 对，prod/dev 分值；值=PEM 的 base64 单行）
    _R31_GEN() { # $1=输出前缀
      openssl genrsa -out "/tmp/gs_$1.pem" 2048 2>/dev/null
      openssl rsa -in "/tmp/gs_$1.pem" -pubout -out "/tmp/gs_$1.pub" 2>/dev/null
      echo "JWT_PRIVATE_KEY_$1=$(base64 -w0 < "/tmp/gs_$1.pem")"
      echo "JWT_PUBLIC_KEY_$1=$(base64 -w0 < "/tmp/gs_$1.pub")"
      rm -f "/tmp/gs_$1.pem" "/tmp/gs_$1.pub"
    }
    echo "JWT_ALG=HS256"
    echo "JWT_KID=rsa-$(date +%Y%m)"
    _R31_GEN PROD
    _R31_GEN DEV
    echo "JWT_PUBLIC_KEY_PREV="
    echo "JWT_KID_PREV="
  } > "$VALUES_FILE"
  chmod 600 "$VALUES_FILE"
fi

# 载入真值（set -a 导出给 kubectl --from-env-file 之前先做非空断言）
set -a
# shellcheck disable=SC1090
. "$VALUES_FILE"
set +a
for k in DB_PASS DB_PASS_RO REDIS_PASS MQTT_PASS MQTT_PASS_SIM MQTT_PASS_IOT TIMESCALE_PASS JWT_SECRET_PROD JWT_SECRET_DEV SM4_FIELD_KEY JWT_ALG JWT_KID JWT_PRIVATE_KEY_PROD JWT_PUBLIC_KEY_PROD JWT_PRIVATE_KEY_DEV JWT_PUBLIC_KEY_DEV; do
  [ -n "${!k:-}" ] || { echo "gen-secrets: 真值缺失 $k" >&2; exit 1; }
done
# R-31：密钥族校验（RSA 形态 + prod/dev 分值——签名密钥同 JWT_SECRET 分值原则）。
# 经临时文件校验（进程替换 /dev/fd 在部分 openssl 构建不可读——MSYS 实测；临时文件两端可移植）
_R31_CHK() { # $1=NS  $2=真值变量名  $3=形态(check=私钥/pubin=公钥)  $4=错误文案
  local _f; _f=$(mktemp); chmod 600 "$_f"
  echo "${!2}" | base64 -d > "$_f"
  if ! openssl rsa "-in" "$_f" "$3" -noout >/dev/null 2>&1; then
    rm -f "$_f"; echo "$4" >&2; exit 1
  fi
  rm -f "$_f"
}
_R31_CHK PROD JWT_PRIVATE_KEY_PROD -check "gen-secrets: JWT_PRIVATE_KEY_PROD 非法 RSA 私钥（R-31 校验）"
_R31_CHK PROD JWT_PUBLIC_KEY_PROD -pubin "gen-secrets: JWT_PUBLIC_KEY_PROD 非法 RSA 公钥（R-31 校验）"
_R31_CHK DEV JWT_PRIVATE_KEY_DEV -check "gen-secrets: JWT_PRIVATE_KEY_DEV 非法 RSA 私钥（R-31 校验）"
_R31_CHK DEV JWT_PUBLIC_KEY_DEV -pubin "gen-secrets: JWT_PUBLIC_KEY_DEV 非法 RSA 公钥（R-31 校验）"
[ "$JWT_PRIVATE_KEY_PROD" != "$JWT_PRIVATE_KEY_DEV" ] || { echo "gen-secrets: prod/dev RS256 私钥分值冲突" >&2; exit 1; }
[ "${#SM4_FIELD_KEY}" -eq 64 ] || { echo "gen-secrets: SM4_FIELD_KEY 须 64 hex 字符" >&2; exit 1; }
[ "$JWT_SECRET_PROD" != "$JWT_SECRET_DEV" ] || { echo "gen-secrets: prod/dev JWT_SECRET 分值冲突" >&2; exit 1; }

mk_secret() { # $1=namespace $2=env-file
  kubectl create secret generic hsf-secrets -n "$1" --from-env-file="$2" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "gen-secrets: Secret hsf-secrets/$1 已注入（键数=$(grep -c '=' "$2")）"
}

# hsf-prod / hsf-dev（共享键同值 + JWT_SECRET 分值 + SM4_FIELD_KEY）
TMP1=$(mktemp); TMP2=$(mktemp)
trap 'rm -f "$TMP1" "$TMP2"' EXIT
cat > "$TMP1" <<EOF
DB_PASS=$DB_PASS
DB_PASS_RO=$DB_PASS_RO
REDIS_PASS=$REDIS_PASS
MQTT_PASS=$MQTT_PASS
MQTT_PASS_SIM=$MQTT_PASS_SIM
MQTT_PASS_IOT=$MQTT_PASS_IOT
TIMESCALE_PASS=$TIMESCALE_PASS
JWT_SECRET=$JWT_SECRET_PROD
JWT_ALG=$JWT_ALG
JWT_KID=$JWT_KID
JWT_PRIVATE_KEY=$JWT_PRIVATE_KEY_PROD
JWT_PUBLIC_KEY=$JWT_PUBLIC_KEY_PROD
JWT_PUBLIC_KEY_PREV=$JWT_PUBLIC_KEY_PREV
JWT_KID_PREV=$JWT_KID_PREV
SM4_FIELD_KEY=$SM4_FIELD_KEY
EOF
cat > "$TMP2" <<EOF
DB_PASS=$DB_PASS
DB_PASS_RO=$DB_PASS_RO
REDIS_PASS=$REDIS_PASS
MQTT_PASS=$MQTT_PASS
MQTT_PASS_SIM=$MQTT_PASS_SIM
MQTT_PASS_IOT=$MQTT_PASS_IOT
TIMESCALE_PASS=$TIMESCALE_PASS
JWT_SECRET=$JWT_SECRET_DEV
JWT_ALG=$JWT_ALG
JWT_KID=$JWT_KID
JWT_PRIVATE_KEY=$JWT_PRIVATE_KEY_DEV
JWT_PUBLIC_KEY=$JWT_PUBLIC_KEY_DEV
JWT_PUBLIC_KEY_PREV=$JWT_PUBLIC_KEY_PREV
JWT_KID_PREV=$JWT_KID_PREV
SM4_FIELD_KEY=$SM4_FIELD_KEY
EOF
chmod 600 "$TMP1" "$TMP2"
mk_secret hsf-prod "$TMP1"
mk_secret hsf-dev "$TMP2"

# infra（redis requirepass 单键）
TMP3=$(mktemp); chmod 600 "$TMP3"
cat > "$TMP3" <<EOF
REDIS_PASS=$REDIS_PASS
EOF
kubectl create secret generic hsf-secrets -n infra --from-env-file="$TMP3" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
rm -f "$TMP3"
echo "gen-secrets: Secret hsf-secrets/infra 已注入"
echo "gen-secrets: 完成（真值仅存在于 $VALUES_FILE 与集群 Secret；git 工作区不含真值）"
