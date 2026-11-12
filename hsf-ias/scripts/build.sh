#!/bin/bash
# HSF 镜像构建（在 175 服务器 /opt/hsf/hsf-ias 下执行，构建上下文=仓库根）
# 一期 8 镜像：hsf/base + hsf/gw + hsf/{iam,land,project,facility,sim,web}，tag 1.0（CONVENTIONS §1/§3.3）
# 二期（CONVENTIONS-P2 T-2）：tag 2.0——sim/iot/ratelimit/gw/web 重建
# 三期（CONVENTIONS-P3 U-2）：tag 3.0——gw/sim/iot/web 重建 + rs/uplink 新增；ratelimit 2.0、四子图 1.0.1 不动
set -e
REG=10.167.2.175:30500
TAG=${TAG:-3.0}
P3=0
[ "${TAG}" = "3.0" ] && P3=1
P2=0
[ "${TAG}" = "2.0" ] && P2=1
cd "$(dirname "$0")/.."

echo "── 构建基础镜像（公共依赖，仅首次较慢；无 Dockerfile.base 时直接用 DV-IAS 已注册 szxc/base:1.2）"
if [ -f services/common/Dockerfile.base ]; then
  docker build -f services/common/Dockerfile.base -t $REG/hsf/base:$TAG .
  docker push $REG/hsf/base:$TAG
  BASE=$REG/hsf/base:$TAG
else
  BASE=${BASE_IMAGE:-$REG/szxc/base:1.2}
  echo "   （跳过 base 构建，使用 BASE=$BASE）"
fi

echo "── 构建网关 gw"
docker build -f gw/Dockerfile -t $REG/hsf/gw:$TAG .
docker push $REG/hsf/gw:$TAG

if [ "$P3" = "1" ]; then
  # 三期服务集：gw + sim/iot/web(3.0 重建) + rs/uplink(3.0 新增)
  for s in sim iot web rs uplink; do
    echo "── 构建 $s"
    docker build -f services/$s/Dockerfile --build-arg BASE=$BASE -t $REG/hsf/$s:$TAG .
    docker push $REG/hsf/$s:$TAG
  done
  echo "✅ 三期镜像构建并推送完成 tag=$TAG（hsf/gw + hsf/{sim,iot,web,rs,uplink}）"
elif [ "$P2" = "1" ]; then
  for s in sim iot ratelimit web; do
    echo "── 构建 $s"
    docker build -f services/$s/Dockerfile --build-arg BASE=$BASE -t $REG/hsf/$s:$TAG .
    docker push $REG/hsf/$s:$TAG
  done
  echo "✅ 二期镜像构建并推送完成 tag=$TAG（hsf/gw + hsf/{sim,iot,ratelimit,web}）"
else
  for s in iam land project facility sim web; do
    echo "── 构建 $s"
    docker build -f services/$s/Dockerfile --build-arg BASE=$BASE -t $REG/hsf/$s:$TAG .
    docker push $REG/hsf/$s:$TAG
  done
  echo "✅ 全部镜像构建并推送完成 tag=$TAG（hsf/base + hsf/gw + hsf/{iam,land,project,facility,sim,web}）"
fi
