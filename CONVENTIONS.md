# HSF 高标准农田信息平台 — 工程约定与避坑手册（所有子代理必读）

> 本文件是 HSF 项目全生命周期的强制约定。来源：DV-IAS 项目(docs/00~17)的完整实战经验，
> 包含全部已踩过的坑及修复方式。任何代理在分析/设计/实现/测试/部署前必须先读本文，
> 再读对应的 DV-IAS 参考文件。与 DV-IAS 冲突之处以本文为准。

## 0. 环境事实（不要重新发现，直接采用）

- 工作区根：`D:\projects\数字乡村`；DV-IAS 参考实现在 `dv-ias/`，设计文档在 `docs/`
- 服务器：`ssh centos7-181`（免密，10.167.2.175 master）+ 10.167.2.176 worker1；代码副本在服务器 `/opt/dv-ias`
- 镜像仓库：`10.167.2.175:30500`（HTTP，无需认证；catalog 见 /v2/_catalog）
- **可用基础镜像（已注册）**：`10.167.2.175:30500/szxc/base:1.2`（python3.13 + fastapi/strawberry/pymysql 等，HSF 直接用）；工具镜像 `perf/k6:0.1.3`（entrypoint=/bin/sh -c）、`perf/locust:latest`、`perf/playwright-python:1.45.0`
- TiDB：`tidb.infra.svc.cluster.local:4000`（root 无密码可用），NodePort 30210；**不存在 tidb-read 主机名**
- Pulsar/Milvus/Nebula/Prometheus(kube-prometheus-stack)/Grafana 在 infra/monitoring 命名空间可用
- 集群曾清理过本项目 DV-IAS 的资源，hsf-* 资源全新创建；NodePort 30085/31201/31202 已释放
- 服务器可上外网（docker pull 成功过）；**局域网浏览器客户端不一定能访问 CDN**——前端严禁依赖 CDN，库文件必须进镜像

## 1. HSF 项目结构与命名（强制）

```
hsf/
  docs/            设计文档（00总览 01SRS 02选型与架构 03DDD 04数据与缓存 05接口 20融合测试报告）
  hsf-ias/         Monorepo（完全镜像 dv-ias/ 的结构风格）
    gw/            Node Apollo Gateway（subgraphs: iam,land,project,facility,sim）
    services/
      common/hsf_common/   crypto.py db.py cache.py web.py（从 dv-ias/services/common/szxc_common 移植改名）
      iam/         组织与用户（县农业农村局/乡镇/村/管护主体/合作社，角色：admin/town/village/keeper/coop）
      land/        地块主数据（统一编码 L+行政区划+序号，GeoJSON 几何，土壤/作物属性，一张图数据源）
      project/     项目全周期（储备→申报→审批→施工→监理→验收→绩效 状态机）
      facility/    设施台账与管护（机井/泵站/渠道/道路，二维码，巡检工单复用 accept→dispatch→resolve→approve 状态机）
      sim/         IoT 模拟与告警（墒情/气象/虫情时序生成写入 hsf_ts，规则引擎告警，模拟设备注册）
    k8s/           00-base.yaml(hsf-prod/hsf-dev+配额+Secret+PriorityClass hsf-*) 10-redis.yaml 20-services.yaml 40-k6.yaml 60-monitors.yaml
    scripts/       build.sh deploy.sh smoke-import.sh static_gate.py seed_sim.sh
    perf/          baseline.js stress.js locustfile.py
    test/          e2e_browser.py（Playwright，≥10 用例）
```

- 命名空间：`hsf-prod` / `hsf-dev`；镜像：`10.167.2.175:30500/hsf/<svc>:1.0`
- 子图端口：iam 8091、land 8092、project 8093、facility 8094、sim 8095；网关 8090(业务)+8091**冲突**——网关用 **8090 业务 / 9090 healthz**；web 80
- NodePort：网关 **30099**（2026-11-07 实际记录：30090 被跨租户占用顺延，见 20-services.yaml 头注）、web **31210**、sim **31211**（上线前必须 `ss -tln` 核验未占用，冲突则顺延并记录）
- 数据库：`hsf_md`(主数据) `hsf_biz`(业务) `hsf_ts`(时序)；账号 `hsf_app`(rw)/`hsf_ro`(ro)，Secret 名 `hsf-secrets`
- Redis：`hsf-redis` 3 节点集群（infra 命名空间，镜像 `base/redis:8-alpine`，headless svc `hsf-redis-h`）
- GraphQL 实体（跨子图同名强制）：`Party`(@key id) / `LandParcel`(@key code) / `FarmProject`(@key code) / `Facility`(@key code)

## 2. 代码级强制约定（DV-IAS 血泪教训，逐条遵守）

1. **依赖锁定**（requirements-base.txt）：`pydantic==2.10.6`、`graphql-core==3.2.5`、`strawberry-graphql==0.254.1`、`PyJWT`、`pymysql`、`redis`、`cryptography`、`uvicorn`、`fastapi`、`prometheus-client`、`httpx`。PyJWT≥2.10 要求 `sub` 必须是字符串：`issue_token` 里 `str(sub)`。
2. **SQL 保留字**：所有可能保留字（column/order/desc/group/condition…）一律不用做列名；非用不可必反引号。
3. **编码列宽**：CHAR(16)（地块/项目/设施编码最长 18 位时用 CHAR(20)，宁可宽不可窄）。
4. **Federation 三件套**：实体类 `@strawberry.federation.type(keys=[...])` + **`resolve_reference` 类方法（公开名，无下划线！）** + **`strawberry.federation.Schema(query=..., mutation=..., types=[Party, LandParcel, FarmProject, Facility], enable_federation_2=True)` 显式 types 列表**——未注册实体不进 SDL，网关组合直接 400（DV-IAS 真实生产事故）。⚠️ 2026-11-07 部署实测：strawberry-graphql 0.254.1 的 `entities_resolver` 只查 `hasattr(origin, "resolve_reference")`，下划线写法 `_resolve_reference` 会被静默跳过、走 convert_argument 兜底后抛 "Unable to resolve reference for X"（federation/schema.py:169 与 197）——跨域组合全部失败。实体解析器必须用公开名。
5. **gw 探针**：业务 8090 是 GraphQL（/healthz 会 400），必须另起 HTTP server 监听 **9090** 提供 /healthz，K8s liveness/readiness 指向 9091?? ——以实际清单为准：**探针端口=9090**，容器 ports 同时声明 8090+9090。
6. **限流**：令牌桶 cap=6000/min（env RATE_CAP 可调），按 user.sub 或 ip 分桶；formatError 对 INTERNAL* 脱敏为"服务内部错误"。
7. **身份头**：gw 校验 JWT 后注入 `x-user-id`/`x-user-roles`，**x-user-name 必须 Base64 编码**（Node fetch 拒绝非 ByteString 中文头）；子图 `gql_context` 里 b64decode(validate=True) 失败则按明文回退。
8. **/metrics**：FastAPI 必须 `return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)`——JSONResponse 会转义引号致 Prometheus 解析失败。
9. **DB 双连接池**：write_pool(env DB_USER/DB_PASS) + read_pool(env DB_USER_RO/DB_PASS_RO)，host 都是 `tidb.infra.svc.cluster.local:4000`。
10. **建表自检**：自增主键、created_at DATETIME DEFAULT CURRENT_TIMESTAMP、业务幂等唯一键 uk_*、查询列有索引、预热分裂 `SHARD_ROW_ID_BITS=4 PRE_SPLIT_REGIONS=8` 仅用于无显式主键的大表。
11. **种子幂等**：seed_if_empty 先查 org 表再查 party 表，双守卫防双副本并发重种（1062）。
12. **web 前端**：纯静态 HTML/JS（无构建链），**禁止 CDN**；一张图用"纯 SVG 渲染 GeoJSON"方案（无外部依赖、局域网必可用），Leaflet 若构建期能下载则作为增强（Dockerfile RUN curl），运行时检测 `window.L` 缺失自动回退 SVG。
13. **时序写入**：sim 服务写 hsf_ts（表 sensor_reading: id BIGINT、sensor_code CHAR(20)、metric VARCHAR(16)、value DECIMAL(10,3)、recorded_at DATETIME(3)、KEY idx_code_time(sensor_code, recorded_at)），批量 INSERT 1000/批。
14. **探针**：所有服务 /healthz(不依赖DB) /readyz(校验DB)；readiness periodSeconds 5，liveness initialDelaySeconds 20。
15. **资源与优先级**：requests cpu 150-250m/mem 256-384Mi，limits cpu 1-2/mem 512Mi-1Gi；PriorityClass hsf-critical(gw)/hsf-normal/hsf-batch；ResourceQuota requests cpu 12 / limits.memory 48Gi；podAntiAffinity preferred 跨主机。
16. **ServiceMonitor**：labels `{release: kube-prometheus-stack, metrics: "true"}`，每个 Service 体积 `{name: http-metrics, port: <业务端口>, targetPort: 同}` 且 Service 带 `metrics: "true"` label（gw 例外：gw 不带 metrics label，其 /metrics 无）。

## 3. 部署与验证 playbook（部署工程师执行顺序）

1. `tar -czf - hsf-ias | ssh centos7-181 "rm -rf /opt/hsf-ias && tar -xzf - -C /opt"`（在 hsf/ 目录执行）
2. TiDB 初始化（临时 pod 跑，参照 DV-IAS dbdrop 方式）：
   `CREATE DATABASE hsf_md; CREATE DATABASE hsf_biz; CREATE DATABASE hsf_ts;`
   `CREATE USER 'hsf_app'@'%' IDENTIFIED BY '<gen>'; GRANT ALL ON hsf_md.* TO ...; 同 hsf_biz/hsf_ts;`
   `CREATE USER 'hsf_ro'@'%'; GRANT SELECT ON hsf_md.* TO ...; 同...; FLUSH PRIVILEGES;` 密码写入 k8s Secret
3. `docker build -f hsf-ias/services/common/Dockerfile.base`（或直接用 szxc/base:1.2 作 BASE）→ 逐服务 build+push tag 1.0
4. `TAG=1.0 bash scripts/smoke-import.sh`——**全部 IMPORT-OK 才可部署**
5. `kubectl apply -f k8s/00-base.yaml && kubectl apply -f k8s/10-redis.yaml`（redis 集群初始化：3 pod Running 后逐个 `redis-cli --cluster create ... --cluster-yes`，参照 /opt/dv-ias/k8s/10-redis.yaml 的 announce hostname 写法，`--cluster-announce-hostname "${HOSTNAME}.hsf-redis-h.infra.svc.cluster.local"` 必须用 ${}）
6. `kubectl apply -f k8s/20-services.yaml && kubectl apply -f k8s/60-monitors.yaml`；`kubectl -n hsf-prod rollout status deploy/<each> --timeout=240s`
7. 排障循环：CrashLoopBackOff → `kubectl logs <pod> --previous` 找根因 → 改代码 → 本地 rebuild+push → `kubectl set image` → 再看。已知坑清单见 §2。
8. 验收：`curl http://10.167.2.175:30099/ -X POST -H 'Content-Type: application/json' -d '{"query":"{__typename}"}'` → 登录 mutation → **联邦跨域**：`{ landParcel(code:"...") { code holder { name } } }` 或 `{ farmProject(code:...) { code supervisor { name } facilities { code } } }` 一次组合 → sim 告警查询 → web 三页 HTTP 200。

## 4. 验收口径（工业级标准，对齐 DV-IAS）

- 全部 pod Running 且探针通过；冒烟 IMPORT-OK 全绿
- 网关 `{__typename}` 200；至少 1 条联邦跨域查询返回真实种子数据
- E2E ≥10 用例 0 FAIL（三端渲染/登录/地块一张图/项目流转/设施报修/告警/积分式激励不需要）
- k6：browse 200VU + report 20VU 场景，0 失败，p95 记录在案
- Prometheus targets hsf-* 全 UP；Grafana 可查
- 融合测试报告 docs/20 有真实命令与输出证据
