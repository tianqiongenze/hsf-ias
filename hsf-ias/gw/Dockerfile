# HSF 联邦网关：Node 20 + Apollo Gateway（构建上下文=hsf-ias/ 根，与 dv-ias 同法）
FROM node:20-alpine
ENV NPM_CONFIG_REGISTRY=https://registry.npmmirror.com
WORKDIR /app
COPY gw/package.json .
RUN npm install --omit=dev
COPY gw/gateway.js .
# 8090 业务（GraphQL）/ 9090 探针（healthz）
EXPOSE 8090 9090
CMD ["node", "gateway.js"]
