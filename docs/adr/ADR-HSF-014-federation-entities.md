# ADR-HSF-014：联邦实体四件套跨子图同名（迁移登记）

> 迁移登记（R-17，2026-11-14）：自 `hsf/docs/02` §3.5 迁移为独立 ADR 文件（四要素齐备）。

| 项 | 内容 |
|---|---|
| 状态 | 已采纳（一期冻结）｜ 决策日期 2026-11-07（§3.5 编号固化）｜ 决策人：架构设计师（工作流） |
| 背景 | 跨子图协作（项目↔地块/负责人、工单↔设施/地块、告警↔地块）需要统一实体引用面；strawberry 联邦组合对未注册实体直接 400（DV-IAS 真实生产事故） |
| 决策 | 联邦实体四件套跨子图同名：`Party`(@key id) / `LandParcel`(@key code) / `FarmProject`(@key code) / `Facility`(@key code)；Schema 构造必须**显式 types 列表**（未注册实体不进 SDL）+ `enable_federation_2=True`（R-12 收口补入冻结检查面）+ 公开名 `resolve_reference`（下划线写法被 strawberry-graphql 0.254.1 静默跳过——CONVENTIONS §2.4 事故记录） |
| 理由 | 同名实体是 Apollo 组合的合并前提；显式 types 列表把"漏注册"从运行期 400 前移为可门禁检查 |
| 影响与限制 | 新增跨子图实体属治理触发器②（docs/02 §9）——必须新开 ADR；三期 rs 未新增实体（docs/08 §2.4 合规留痕），以内嵌 `RsVigor` 类型进 types 列表（`services/rs/app/main.py:175`，R-12 补 enable_federation_2=True） |
| 验证方式 | static_gate check_federation（五子图构造+types+enable_federation_2 三查）；gw 组合后 `_service { sdl }` 实体断言 |
