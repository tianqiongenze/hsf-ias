# ADR-HSF-001：GIS 方案——TiDB 存储 GeoJSON + 前端纯 SVG 渲染

> 迁移登记（R-17，2026-11-14）：正文自 `hsf/docs/02` §3.1 原样迁移（唯一事实源仍为 docs/02，本文件为
> ADR 目录形态的等价载体；两处不一致时以 docs/02 §3 为准并回修本文件）。

| 项 | 内容 |
|---|---|
| 状态 | 已采纳（一期冻结）｜ 决策日期 2026-11-07 ｜ 决策人：架构设计师（工作流） |
| 背景 | ① 原文章要求"时空数据库（地块/灌排设施/道路）+ GIS 中台（空间分析/三维/专题图）+ 一张图"；② 集群事实：**无 PostGIS、无 GeoServer**，共享多租户 2 节点；③ 局域网客户端可能无法访问外网 CDN（`CONVENTIONS.md` §0），前端严禁 CDN；④ 一期 GIS 诉求仅为**查询级**：按村返回 FeatureCollection（FR-103）、SVG 渲染与点击属性（FR-201/202）、统计角标（FR-203）——无空间分析（缓冲区/相交/测距）、无三维、无瓦片服务诉求 |
| 决策 | 1. **存储**：`hsf_md.land_parcel.geojson` 以 JSON 列存储 GeoJSON geometry（Polygon 起步），坐标 WGS84；2. **服务**：land 子图提供 `landGeojson(villageCode)` 返回合法 FeatureCollection（服务端拼装，不落文件）；3. **渲染**：web 一张图页**纯 SVG `<polygon>` 渲染**为主路径；构建期若能下载则将轻量 GIS 库（Leaflet）vendored 进镜像作增强，运行时检测 `window.L` 缺失**自动回退 SVG**，两态均可用（`CONVENTIONS.md` §2.12 原文冻结） |
| 理由（编号） | R1 集群无 PostGIS/GeoServer——引入即新增 2 个有状态服务+存储+备份+升级链路，违背 P1；R2 一期无空间函数诉求，JSON 列全量返回+前端渲染即可满足 FR-103/201/202/203 全部验收标准；R3 禁 CDN 约束下纯 SVG（浏览器原生）是唯一"零依赖必可用"方案，vendored Leaflet 仅作渐进增强；R4 数据量边界内性能充裕：种子 8 地块、单村地块数百级以内，FeatureCollection 全量返回 <1MB（一期按"单村地块 <1 万"为容量护栏，见影响与限制）；R5 降低有状态服务数，K8s 部署面（Deployment 数）与故障面同步缩小 |
| 备选与不选理由 | ① **PostGIS+GeoServer**：功能最强但集群无此栈，需新建有 StatefulSet/独立库与运维，二期随空间分析需求一并评估（触发器见下）；② **OpenLayers**：功能覆盖投影/要素交互强于 SVG 裸写，但 vendored 体积与复杂度高于 Leaflet，一期"点选+渲染"场景不必要——**列为二期 GIS 增强评估项**（本 ADR 与任务指令"SVG/OpenLayers"的差异：以 `CONVENTIONS.md` §2.12 冻结的"SVG 主路径 + Leaflet vendored 增强"为准，OpenLayers 与 PostGIS 绑定在二期同评）；③ **Cesium（三维）**：三期数字孪生场景再看（`hsf/docs/00` §3"不承诺"）；④ **瓦片预生成服务**：无底图数据源（无外网瓦片），不成立 |
| 影响与限制（诚实声明） | ① 不支持 SQL 空间函数（`ST_*`）查询——空间过滤仅能按村/按属性，几何过滤在前端；② 无服务端空间分析能力（缓冲区/相交/面积重算以登记值为准）；③ geometry 校验为应用层格式校验（JSON 结构+环闭合抽查），非拓扑校验；④ 坐标系仅 WGS84，不含投影转换（渲染按经纬度线性映射） |
| 演进触发器 | 任一满足即启动二期评估：单村地块 >1 万；出现相交/缓冲区/沿路统计类需求；接入遥感影像（三期）；甲方要求接入国土空间一张图底图服务 |
| 验证方式 | FR-103（FeatureCollection 合法性与数量）、FR-201/202（E2E SVG 元素数≥种子地块数、点击属性面板）、FR-204（static_gate grep 零 CDN 引用） |
