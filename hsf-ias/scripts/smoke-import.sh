#!/bin/bash
# 部署前冒烟：各服务应用模块可导入、GraphQL 联邦 schema 可构建（不连 DB/Redis）
# 全部 IMPORT-OK 才可部署（CONVENTIONS §3.4）
# 注：web 为 nginx 纯静态镜像（无 app.main），不在 import 冒烟范围，构建时由 Dockerfile 自身保障
set -e
set -o pipefail
REG=10.167.2.175:30500
TAG=${TAG:-1.0}
BASE=${BASE_IMAGE:-$REG/hsf/base:$TAG}
cd "$(dirname "$0")/.."
for s in iam land project facility sim; do
  echo "── import-smoke: $s"
  docker run --rm \
    -v "$PWD/services/$s/app":/srv/app \
    -v "$PWD/services/common/hsf_common":/srv/hsf_common \
    -e PYTHONPATH=/srv -e DB_HOST_WRITE=x:1 -e DB_HOST_READ=x:1 -e DB_USER=x -e DB_PASS=x \
    -e DB_USER_RO=x -e DB_PASS_RO=x -e REDIS_URL=x:1 -e REDIS_PASS=x \
    $BASE python -c "import sys; sys.path.insert(0,'/srv'); import app.main; print('IMPORT-OK $s')"
done
echo "✅ 全部服务导入冒烟通过"
