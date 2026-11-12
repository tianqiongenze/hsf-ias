# CONVENTIONS-P3：HSF 三期强制约定（遥感/上级对接/设备接入框架）

> 依据：`docs/08-三期设计`（本三件套同批冻结）、`docs/01` A2（集群无 GPU→三期 AI 用 CPU 量化策略）、
> `docs/00` §3 映射表（卫星遥感/无人机=三期、上级平台导出通道=三期）、`docs/02` ADR-HSF-001 演进触发器
> （接入遥感影像→启动二期评估项 OpenLayers/PostGIS 同评，本次结论：仍不引入，理由见 docs/08 §1）。
> 二期 `CONVENTIONS-P2.md` 全部条款继续生效（T-1~T-3）；一期 `CONVENTIONS.md` 16 条坑继续生效。

## U. 三期服务与镜像（U-1/U-2）

- **U-1 新增服务**（仅 2 个，控制部署面——沿用 P2 的"控制有状态服务数"纪律）：
  - `rs`（遥感解译，:8098，NodePort **31214**）：影像元数据登记 + NDVI CPU 解译 + 长势分级 + 非农化预警 + GraphQL 子图；
  - `uplink`（上级上报，:8099，ClusterIP）：统一编码→监管平台上报粒度映射 + 上报任务状态机 + 回执 stub（内网无真实平台，契约化对接）。
- **U-2 镜像 tag 口径**：三期统一 **3.0**——`rs:3.0 / uplink:3.0` 新增；`gw/sim/web/iot` 重建为 3.0；
  `ratelimit/iam/land/project/facility` **沿用 2.0/1.0.1 不重建**（无代码变更）。修复循环按二期实践升 patch（3.0.1…）。

## V. 三期依赖与红线（V-1~V-4）

- **V-1 CPU 解译红线**：三期无 GPU（docs/01 A2）——遥感解译**只允许 numpy 纯 CPU 数值计算**
  （NDVI=(NIR-Red)/(NIR+Red) 波段运算），禁止引入 torch/tensorflow/任何模型推理依赖；
  真正的深度学习解译（作物分类模型）**显式列为三期不做**，交付"NDVI 长势分级 + 规则化非农化预警"业务闭环。
- **V-2 样本数据红线**：集群内网无法访问外网卫星影像源——解译输入采用**仓库内置合成样本栅格**
  （CSV/JSON 波段矩阵，含元数据头），处理链路与真实 GeoTIFF 完全同构（登记→波段提取→NDVI→按地块聚合→分级落库），
  真实影像只需替换输入适配器（`rs/app/ingest.py` 单文件），**契约不变**。
- **V-3 上报契约红线**：监管平台在内网不可达——uplink 采用**契约化 stub**：上报 payload 按统一编码
  （L/P/F/S→上报粒度）完整组装并可导出 JSON 归档，回执由 stub 按**确定性规则**生成（校验 payload 完整性→回执成功）；
  真实平台联调时仅替换 `uplink/app/transport.py` 单文件（HTTP 客户端），状态机/幂等/重试不变。
- **V-4 ioredis 红线延续**：三期全部新代码继续禁用 ioredis（static_gate 检查项 8 扩展到新服务）。

## W. 二期残余收口（W-1/W-2）

- **W-1 sim 异步发布**：`mqtt_pub.py` `max_inflight_messages_set(100)`→**1000**，mosquitto ConfigMap
  `max_inflight_messages 1000` 同步调大（二期实测 2.4 万条/迭代 p95=8.29s 的主因是 inflight=100 × PUBACK RTT）；
  响应契约**不变**（仍同步返回 published 条数——遵守 docs/07 §2.5 冻结），预期 tick p95 降一个量级、
  iot-write check 84%→≥98%。
- **W-2 ResourceQuota**：`00-base.yaml` hsf-prod-quota `limits.cpu 32→36`（集群 45C 中留 ≥9C 给其他租户，
  评估记录于 docs/08 §6）；limits.memory 48Gi 不变。

## X. 三期验收增量（X-1~X-3）

- **X-1 E2E ≥25 条**：二期 19 条全保留 + 新增 ≥6 条（影像登记→NDVI 解译→长势查询 / 非农化预警 /
  上报任务生成→payload 校验→stub 回执→状态闭环 / 设备注册→凭证下发 / admin 新页签渲染 / 同源反代 rs）。
- **X-2 k6 回归**：browse/report 阈值不变（0 失败、p95 判据同二期）；iot-write 复跑验证 W-1 效果（check≥98% 观测）。
- **X-3 门禁增量**：static_gate 新增 P3 检查项——rs 必含 numpy 无 torch/tf、uplink 必含 transport 隔离层、
  E2E≥25、新服务探针/limits、清单镜像 tag=3.0 一致性。

## Y. 三期部署顺序（Y-1，冻结）

1. 同步代码 → 2. 构建推送 3.0 镜像（gw/sim/iot/web/rs/uplink）→ 3. 00-base（quota+Secret 无新增键）apply →
4. 20-services apply（rs/uplink 新建，gw/sim/iot/web 滚动）→ 5. rollout status 全绿 →
6. 新链路验收：影像登记→NDVI→长势查询→非农化预警 / 上报任务→回执闭环 / 设备注册→凭证 /
   二期四链路回归（MQTT→时序、灌溉决策、通知、限流 429）→ 7. E2E+k6 → docs/22。
