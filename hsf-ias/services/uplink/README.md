# uplink（上级平台上报通道）服务

## 职责（docs/08 §3 冻结）
统一编码（L/P/F/S）→ 监管平台上报粒度（PARCEL/PROJECT/FACILITY/SENSOR）→ 上报任务
状态机（PENDING→SENT→ACKED/REJECTED，uk_batch 幂等）→ payload 完整导出 JSON 归档。

## V-3 红线
真实监管平台内网不可达——`app/transport.py` 为唯一替换点（StubTransport 确定性回执，
`UPTRANSPORT=real` 切换 RealTransport 占位）。真实联调时状态机/幂等/重试零改动。

## 端点
POST /tasks（幂等创建+自主组装 payload）｜ POST /tasks/{batch}/send（发送+回执）
GET /tasks ｜ GET /tasks/{batch}/payload（导出归档，docs/05 §8.1"导出通道"落地）
/healthz /readyz /metrics

## 测试
`pytest tests -q`（映射/校验/回执确定性/状态机，db 层 mock）
