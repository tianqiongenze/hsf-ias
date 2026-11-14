# ADR-HSF-020：设备接入框架正式编号 D-08-4 与 deviceCode S 前缀契约回写（R-16）

| 项 | 内容 |
|---|---|
| 编号 | ADR-HSF-020（R-17 补登——docs/24 §4 R-16 实施路径④"冻结口径变更按 docs/02 §9 出 ADR 登记（联动 R-17）"） |
| 状态 | 已采纳（Accepted，2026-11-14 实施并验收） |
| 关联 | docs/24 §4 R-16（审核缺口#4 幽灵编号 + P3-3 契约回写残余）、R-17（补登）、docs/08 §4（设备接入）、docs/23 §6 P3-3、`services/iot/app/main.py:171`（register S 前缀校验实现）、`services/iot/app/metrics.py:17`（D-08-4 引用方） |
| 决策日 | 2026-11-14 |

## 背景

- 幽灵编号：`docs/23:191` 与 `services/iot/app/metrics.py` 引用 **D-08-4（设备接入）**，而 docs/08 仅
  D-08-1/2/3 有编号（§1 三期范围/§2 遥感/§3 上报）——引用与定义面断裂。
- 实质契约修订未回写：P3-3 已实施 register S 前缀校验（`main.py:171` 非 S 前缀 → 400 INVALID_ARGUMENT，
  镜像 iot:3.0.5），但 docs/08:163-165 对 deviceCode 无前缀约束——冻结契约文本落后于实现。
- deviceCode 兼作 MQTT username/topic（`pattern readwrite hsf/sensor/%u/#`）与 `sensor_code`：
  无前缀约束时非统一编码设备（DEV-* 等）可逃逸"统一编码"口径（P3-3 收口的对象）。

## 决策

1. docs/08 §4 设备接入框架正式编号 **D-08-4**（最小漂移修正——使 docs/23 与 metrics.py 的既有引用成立，
   不改任何实现）；
2. deviceCode 统一编码约束回写进冻结 API 契约："**deviceCode 必须为统一编码 S 前缀（传感器编码）；
   非 S 前缀 → 400 INVALID_ARGUMENT**"——与 `main.py:171` 校验一致，注明 P3-3 已实施（2026-11-14 收口）；
3. docs/05/docs/08 增"rs REST=snake_case / GraphQL=camelCase 双口径冻结声明"（R-16③，杜绝再次以改
   断言收口）。

## 影响

- 契约面：docs/08 §4（D-08-4 + S 前缀行）为设备注册唯一冻结事实源；docs/23 的引用自此有效。
- 兼容性：无存量非 S 前缀设备（dep-device.sh 从未对任何设备执行——docs/23 §7.5），约束零迁移成本；
  R-18（CSPRNG 化）后注册的设备全部满足 S 前缀。
- 回退：还原 `main.py:171` 校验 + 回写契约文本（需推翻本 ADR，新编号登记）。
- 验证方式：`grep -n "D-08-4" hsf/docs/08-*.md` 有定义行；`grep -n "S 前缀" hsf/docs/08-*.md` §4 register
  行命中；E2E 用例 24（注册 API：凭证字段+幂等 409）回归。
