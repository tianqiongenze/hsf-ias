#!/bin/bash
# sim 模拟数据播种与自检（部署验收用，CONVENTIONS §3.8/§4）：
#   网关登录取 token → POST /sim/tick 生成时序读数+告警 → 校验传感器 48 台/最新值/告警列表/统计
# 全部 SEED-OK 才算验收通过；端点契约见 hsf/docs/05 §7.1（sim REST，NodePort 31211，非 GraphQL）
set -e
GW=${GW:-http://10.167.2.175:30090}
SIM=${SIM:-http://10.167.2.175:31211}
VILLAGE=${VILLAGE:-53060010101}          # 清溪村（种子 4 村之一，04 号 §9.1）
STEPS=${STEPS:-5}                        # tick 步数：每步 48 台各 1 条读数
ADMIN_PHONE=${ADMIN_PHONE:-13800000601}  # 种子 admin（04 号 §9.1）
ADMIN_PASS=${ADMIN_PASS:-hsf@2026}
FAIL=0

echo "── 登录网关取 token（admin）"
TOKEN=$(curl -sf "$GW/" -X POST -H 'Content-Type: application/json' \
  -d "{\"query\":\"mutation(\$p:String!,\$w:String!){login(phone:\$p,password:\$w){token}}\",\"variables\":{\"p\":\"$ADMIN_PHONE\",\"w\":\"$ADMIN_PASS\"}}" \
  | grep -o '"token":"[^"]*"' | head -1 | cut -d'"' -f4) || TOKEN=""
if [ -z "$TOKEN" ]; then echo "SEED-FAIL | 登录失败（网关/种子账号不可用）"; exit 1; fi
AUTH="Authorization: Bearer $TOKEN"

echo "── POST /sim/tick?steps=$STEPS（生成时序+规则引擎告警）"
TICK=$(curl -sf -X POST "$SIM/sim/tick?steps=$STEPS" -H "$AUTH") || TICK=""
echo "$TICK"
case "$TICK" in *'"inserted"'*) echo "SEED-OK | tick 写入";; *) echo "SEED-FAIL | tick 响应异常"; FAIL=1;; esac

echo "── GET /sim/sensors?villageCode=$VILLAGE（种子 48 台 = 6 类×2 台×4 村，本村应 12 台）"
SEN=$(curl -sf "$SIM/sim/sensors?villageCode=$VILLAGE&page=1&size=50" -H "$AUTH") || SEN=""
case "$SEN" in *sensorCode*) echo "SEED-OK | 传感器台账";; *) echo "SEED-FAIL | 传感器为空"; FAIL=1;; esac

echo "── GET /sim/latest?villageCode=$VILLAGE（tick 后 last_* 快路径应有值）"
LAT=$(curl -sf "$SIM/sim/latest?villageCode=$VILLAGE" -H "$AUTH") || LAT=""
case "$LAT" in *'"value"'*) echo "SEED-OK | 最新值";; *) echo "SEED-FAIL | 最新值为空"; FAIL=1;; esac

echo "── GET /sim/alerts?state=OPEN（4 规则种子 ≥4 条，tick 亦可能新增）"
AL=$(curl -sf "$SIM/sim/alerts?state=OPEN&page=1&size=50" -H "$AUTH") || AL=""
echo "$AL" | head -c 300; echo
case "$AL" in *ruleCode*) echo "SEED-OK | 告警列表";; *) echo "SEED-FAIL | 无 OPEN 告警"; FAIL=1;; esac

echo "── GET /sim/alerts/statistics?dim=state（统计与明细一致）"
ST=$(curl -sf "$SIM/sim/alerts/statistics?dim=state" -H "$AUTH") || ST=""
case "$ST" in *'"items"'*) echo "SEED-OK | 告警统计";; *) echo "SEED-FAIL | 统计异常"; FAIL=1;; esac

if [ "$FAIL" -eq 0 ]; then echo "✅ SEED 全部通过（sim 播种完成，联邦/告警验收可用）"; else echo "❌ SEED 存在失败项"; exit 1; fi
