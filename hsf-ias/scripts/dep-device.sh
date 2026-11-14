#!/usr/bin/env bash
# dep-device.sh——设备 broker 凭证追加（docs/08 §4 后补规格①，CONVENTIONS-P3）。
#
# 背景：POST /devices/register 下发的明文凭证只在响应出现一次（库内仅 SM3 摘要+sha256 hint，
# 事后无法从库还原）；broker password_file 初始仅含运维账号 hsf（passwd-init -c 覆盖重建），
# allow_anonymous false 下已注册设备无法认证——真实硬件到货联调前，用本脚本把设备账号
# 追加进 broker passwd 并滚动生效。**规格③实机验证（own topic 收发/他人 topic 拒绝）为
# 上线前核验项（docs/08 §6），须在具备集群与设备的运行环境执行。**
#
# 用法：scripts/dep-device.sh <deviceCode> <mqttPassword> [namespace]
#   deviceCode/mqttPassword 取设备注册响应的 mqttUsername/mqttPassword 字段（当次同步执行）。
#
# 红线：
#   1) 禁用 mosquitto_passwd -c——-c 整文件覆盖重建会抹掉既有设备条目（docs/08 §4 规格①冻结）；
#   2) 追加后必须 rollout restart broker 使 password_file 重载（mosquitto 不热读 passwd 追加）；
#   3) passwd-init 配套改造（规格②：passwd 已存在且含 hsf 账号时跳过 -c 重建）已入 30-mqtt.yaml——
#      否则 broker pod 每次重启都会抹掉本脚本追加的设备条目。
set -euo pipefail

DEV="${1:-}"
PASS="${2:-}"
NS="${3:-hsf-prod}"
POD="hsf-mosquitto-0"

[ -n "$DEV" ] || { echo "用法: $0 <deviceCode> <mqttPassword> [namespace]"; exit 2; }
[ -n "$PASS" ] || { echo "用法: $0 <deviceCode> <mqttPassword> [namespace]"; exit 2; }

command -v kubectl >/dev/null || { echo "❌ 缺 kubectl"; exit 3; }

kubectl get pod "$POD" -n "$NS" >/dev/null 2>&1 || { echo "❌ broker pod $POD 不在 $NS"; exit 4; }

# 规格①：以注册响应一次性下发的明文凭证执行追加（无 -c；-b 批量模式不落 shell history 之外文件）
kubectl exec -n "$NS" "$POD" -- \
  mosquitto_passwd -b /mosquitto/data/passwd "$DEV" "$PASS"

# ACL pattern 行（30-mqtt.yaml 冻结 pattern readwrite hsf/sensor/%u/#）按 username=%u 生效，
# 无需按设备改 ACL；滚动重启使 password_file 重新加载（规格①步骤 2）
kubectl -n "$NS" rollout restart statefulset/hsf-mosquitto
kubectl -n "$NS" rollout status statefulset/hsf-mosquitto --timeout=120s

echo "✅ 设备 $DEV 凭证已追加并生效（ns=$NS）。"
echo "   规格③实机验证（上线前核验，docs/08 §6）："
echo "   1) own topic 收发：mosquitto_pub -h <broker> -u $DEV -P '<凭证>' -t hsf/sensor/$DEV/test -m ok"
echo "   2) 他人 topic 拒绝：-t hsf/sensor/<其他设备>/test 应被 ACL 拒绝"
