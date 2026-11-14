# ADR-HSF-012：镜像仓库与 BASE 镜像（迁移登记）

> 迁移登记（R-17，2026-11-14）：自 `hsf/docs/02` §3.5 迁移为独立 ADR 文件（四要素齐备）。

| 项 | 内容 |
|---|---|
| 状态 | 已采纳（一期冻结）｜ 决策日期 2026-11-07（§3.5 编号固化）｜ 决策人：架构设计师（工作流） |
| 背景 | 集群内网无外网镜像源信任面（局域网客户端不可达外网 CDN/Registry），必须全量私有化；共享仓库 10.167.2.175:30500 已沉淀可复用 BASE |
| 决策 | 镜像统一 `10.167.2.175:30500/hsf/<svc>:<tag>`；BASE=`10.167.2.175:30500/szxc/base:1.2`（python3.13 + fastapi/strawberry/pymysql 等，HSF 直接用）；二期/三期 tag 口径：二期统一 2.0、三期统一 3.0（新增 rs/uplink；gw/sim/iot/web 重建），修复循环升 patch（2.0.x/3.0.x，CONVENTIONS-P3 U-2）；mosquitto/timescale 等第三方镜像拉取后重新 tag 注册至同仓库 |
| 理由 | 单仓库+单 BASE 收敛供应链与构建时间；tag 主版本随期数演进，manifest 为唯一事实源（T-3：镜像 tag 只改 manifest） |
| 影响与限制 | dockerd insecure-registries 前提（docs/22 §4 待授权项 R-11）——未恢复前构建 CLI 慢响应风险留档；镜像全部留存本地仓库（2026-11-13 资源下线记录口径），回退可任意 tag 重指 |
| 验证方式 | `curl -s http://10.167.2.175:30500/v2/_catalog` 含 hsf/ 前缀仓库集；20-services.yaml image 与仓库逐 tag 对账（static_gate 检查项：三期 tag=3.0 一致性） |
