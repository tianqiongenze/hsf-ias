# rs（遥感解译）服务

## 职责（docs/08 §2 冻结）
影像登记（POST /scenes，幂等 scene_code）→ NDVI CPU 解译（POST /scenes/{code}/process，
numpy 纯 CPU——V-1 红线无 torch/tf）→ 四级长势落库（rs_vigor）→ 非农化预警（RULE_NDVIFALL，
复用二期 alert_record+notify 管道）→ 查询（/vigor /vigor/latest /alerts）。

## 关键契约
- 长势分级：旺长 ≥0.60 / 正常 [0.40,0.60) / 偏弱 [0.20,0.40) / 长势差 <0.20
- 非农化预警：相邻两景均值降幅 >0.15 且后景 <0.30
- V-2：样本栅格（JSON 波段矩阵）与真实 GeoTIFF 同构，真实影像仅替换 ingest 适配器
- 掩膜：WGS84 线性映射（ADR-001 口径），地块环包围盒近似（不引入栅格化依赖）

## 端点
POST /scenes ｜ POST /scenes/{code}/process（幂等）｜ GET /vigor ｜ GET /vigor/latest
GET /alerts（ruleCode 过滤）｜ POST /sample/seed（验收数据构造）｜ /healthz /readyz /metrics
GraphQL 子图：rsVigor(sceneCode) / rsVigorLatest(parcelCode)（gw SUBGRAPHS 组合）

## 测试
`pytest tests -q`（39+ 用例同口径：不依赖外部服务，db 层 mock）
