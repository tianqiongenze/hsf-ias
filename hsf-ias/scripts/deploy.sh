#!/bin/bash
# HSF 部署（滚动更新；顺序对齐 CONVENTIONS §3 playbook 第 5/6 步）
# 前置：TiDB 三库双账号已初始化（04 号 §3.0）、镜像已 build+push、smoke-import 全绿
# 前置：infra/hsf-redis 3 pod Running 后需手动初始化集群（见 k8s/10-redis.yaml 头注释）
# 二期（docs/07 §7 冻结顺序）：25-timescale/30-mqtt 先于 20-services（iot 依赖两 StatefulSet 就绪）；
#   全部清单幂等可重复执行；镜像 tag 只改 manifest（T-3：禁止 kubectl set image 后再 apply 旧清单）
set -e
cd "$(dirname "$0")/.."
kubectl apply -f k8s/00-base.yaml
kubectl apply -f k8s/10-redis.yaml
kubectl apply -f k8s/25-timescale.yaml
kubectl apply -f k8s/30-mqtt.yaml
kubectl apply -f k8s/20-services.yaml
if [ -f k8s/60-monitors.yaml ]; then kubectl apply -f k8s/60-monitors.yaml; fi
for d in gw iam-svc land-svc project-svc facility-svc sim-svc web ratelimit-svc iot-svc; do
  kubectl -n hsf-prod rollout status deploy/$d --timeout=240s
done
echo "✅ 全部部署完成（二期验收：{__typename} 200 → 登录 → sim tick → MQTT→TimescaleDB 可查 → /irrigation-advice → /notify → 全局限流 429）"
