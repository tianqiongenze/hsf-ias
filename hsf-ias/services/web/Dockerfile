# HSF web：纯静态三页（nginx:alpine），无构建链、运行时零外网依赖（CONVENTIONS §2.12）
# 构建上下文=hsf-ias/ 根（COPY services/web/...，与 gw/Dockerfile 一致）
FROM nginx:alpine

# 构建期尽力 vendored Leaflet 作为增强（供 /vendor/leaflet/ 本地加载）；
# 下载失败不阻断构建——运行时检测 window.L 缺失自动回退纯 SVG 渲染
RUN mkdir -p /usr/share/nginx/html/vendor \
 && (wget -q -T 15 -O /tmp/leaflet.tgz https://registry.npmmirror.com/leaflet/-/leaflet-1.9.4.tgz \
  && tar -xzf /tmp/leaflet.tgz -C /tmp \
  && cp -r /tmp/package/dist /usr/share/nginx/html/vendor/leaflet) \
 || echo "构建期离线：跳过 Leaflet vendored，运行时自动回退纯 SVG"

# 同源代理上游（由 nginx 官方镜像的 envsubst 模板机制渲染；hsf-dev 部署时覆盖环境变量即可）
# 二期增量 IOT_UPSTREAM（docs/07 §4.7：默认值同法 + manifest env 显式声明，缺省时 envsubst 渲染空上游会致 nginx 拒启）
ENV GW_UPSTREAM=gw-svc.hsf-prod.svc.cluster.local:8090 \
    SIM_UPSTREAM=sim-svc.hsf-prod.svc.cluster.local:8095 \
    IOT_UPSTREAM=iot-svc.hsf-prod.svc.cluster.local:8097

COPY services/web/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY services/web/static/ /usr/share/nginx/html/

EXPOSE 80
