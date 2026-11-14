# ADR-HSF-018：mosquitto 最小授权改造——sim/iot 读写分离 ACL（R-20）

| 项 | 内容 |
|---|---|
| 编号 | ADR-HSF-018 |
| 状态 | 已接受（Accepted，2026-11-14 实施并集群验证） |
| 关联 | docs/24 §4 R-20（审核缺口#9：ACL 读写不分离）、R-17（本文件为 docs/adr/ 目录首个 ADR，HSF-016/017 由 R-17 存量补立后顺延迁移登记）、R-07（Secret 增键与 gen-secrets.sh 生成规则变更）、R-19（设备 pattern 行语义不变） |
| 决策日 | 2026-11-14 |

## 背景

- 冻结口径（docs/07 §2.4，2026-11-08 二期冻结）：mosquitto 单账号 `hsf`，ACL `topic readwrite hsf/sensor/#`，
  理由为"sim 只 pub、iot 只 sub，单一 readwrite 账号即可满足且避免两账号密钥分发"。
- 审核缺口#9（docs/24 §2.2，medium）：单账号 readwrite 违反最小授权——sim 凭证被泄露即可**订阅**
  全部传感器读数（业务数据外泄面），iot 凭证泄露即可**伪造**传感器读数写入（数据完整性面）。
- 对标①②（数字乡村/IoT 时序平台）均要求接入面"最小授权/读写分离"。
- 三期设备接入框架（docs/08 §4）已有分账号先例：真实设备以 deviceCode 为 username + `pattern
  readwrite hsf/sensor/%u/#`（own-topic 语义）——运维面（sim/iot）与设备面分权的一致性缺口。

## 决策

1. **ACL 分用户**（`k8s/30-mqtt.yaml` ConfigMap `acl` 键重写）：
   - `user hsf-sim` → `topic write hsf/sensor/#`（sim 仅发布）；
   - `user hsf-iot` → `topic read hsf/sensor/#` + `topic read $SYS/broker/#`（iot 仅订阅；$SYS 只读为
     R-03 BrokerSysMonitor 采集最小授权，延续 2026-11-14 实测口径）；
   - `pattern readwrite hsf/sensor/%u/#` 保留（设备 own-topic 语义，R-19 联动不变）——注意 pattern 行
     对 sim/iot 账号亦生效，但 `%u` 展开后仅授权 `hsf/sensor/hsf-sim/#`、`hsf/sensor/hsf-iot/#` 两个
     退化前缀（无生产流量、无业务语义），不构成越权路径，留档说明；
   - 旧账号 `hsf` 保留在 passwd（`MQTT_PASS` 键不删除）但 ACL 不再授权 sensor 读写——**回退开关**=
     恢复 `user hsf / topic readwrite hsf/sensor/#` 一行并重启 broker 即回冻结前行为。
2. **Secret 增键**（docs/07 §1.4 / docs/04 §7.3 冻结清单增补，00-base.yaml 两命名空间同步空占位符）：
   `MQTT_PASS_SIM`（hsf-sim 账号密码）、`MQTT_PASS_IOT`（hsf-iot 账号密码）——由
   `scripts/gen-secrets.sh` CSPRNG 生成（真值不入 git，R-07 口径不变；脚本对既有真值文件**增量补键**，
   存量键不轮换）。
3. **passwd-init 增两运维账号**（幂等追加，禁 `-c`）：passwd 已存在时逐账号 `grep -q '^hsf-sim:'` 缺失
   才 `mosquitto_passwd -b` 追加——dep-device.sh 设备条目保护语义（docs/08 §4 规格①②）不变。
4. **sim/iot 分账号注入**（`k8s/20-services.yaml`）：`MQTT_USER=hsf-sim`/`hsf-iot` + `MQTT_PASS` 分别引用
   `MQTT_PASS_SIM`/`MQTT_PASS_IOT`（代码零改动——`MQTT_USER` env 支持为二期既有实现
   `services/sim/app/main.py:40`、`services/iot/app/main.py:25`）。
5. **下行 topic `hsf/cmd/#` 维持不放开**（三期内维持 docs/07 §2.1 冻结口径）。

## 影响

- 契约变更：docs/07 §2.4 账号/ACL 冻结表、§1.4 Secret 增键清单、docs/04 §7.3 键名清单三处回写
  （冻结清单增补按 docs/02 §9 出本 ADR 留痕）。
- 部署顺序红线：gen-secrets.sh（Secret 增键）→ 30-mqtt.yaml（acl+passwd-init）+ broker 滚动 →
  20-services.yaml（sim/iot env 分账号）滚动——顺序错配会使 passwd-init 读不到新键（pod CrashLoop）
  或服务用旧账号被新 ACL 拒绝（链路中断）。
- 验收：iot 凭证发布被 ACL 拒绝、sim 凭证订阅被拒绝、业务链路（tick→时序落库）回归、E2E 25/25。
- 回退：恢复单账号 acl 一行 + 重启 broker + sim/iot env 回 `MQTT_USER=hsf`/`MQTT_PASS`（键均在，
  无需重新生成凭据）。
