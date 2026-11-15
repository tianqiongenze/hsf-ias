# ADR-HSF-028：sim tick 响应契约 v1.0→v1.1（同步发布→混合响应+发布池+单调取号）

- 状态：已采纳（2026-11-15，用户批准全量执行）
- 关联：docs/24 R-01/R-02、docs/25 §3.2/§10、docs/07 §2.5、ADR-HSF-018（mosquitto 分账号）

## 背景

docs/25 §3.2 实证 iot-write 判据 3 项未达的结构根因：sim 单 paho 连接同步 QoS1 发布 ~2,900 条/s
为全链路上限（iot 4-worker 刷写侧 buffer 恒<2,000 已解除瓶颈）；同时 4 VU 并发 tick 的
recorded_at 毫秒碰撞造成 onconflict 8.3%（repo.py base=now_local() 步进 1ms 窗口重叠）。

## 决策

1. **混合响应**：steps≤SYNC_MAX_STEPS（env，缺省 4）保持 200 同步+published（一期/E2E 冻结
   口径零改动）；steps>SYNC_MAX 返回 **202+jobId**，后台 JobManager（单 worker 保
   recorded_at 全局有序）经 PublisherPool 发布；GET /sim/tick/jobs/{id} 查进度；
   队列满 503 BUSY。
2. **发布池**：PublisherPool PUB_CLIENTS=4 连接分片并行（唯一 client_id，0 号兼职心跳），
   发布能力 ≈4×；is_connected/publish_readings/published_total 鸭子兼容原 MqttPublisher。
3. **单调取号**：mqtt_pub.MONO（_MonoClock）单条 next()/批量 next_batch(n) 槽位预留，
   心跳与并发 tick 取号互不重叠——幂等键唯一性回平台级保证。
4. **回退**：SIM_ASYNC=off 一键回退 v1.0 完全旧行为（单连接+同步）。

## 影响

- topic/payload/QoS1/retain=false 冻结形态不变（docs/07 §2.1/§2.2/§2.3）。
- iot_tick_duration 口径变更：v1.0=同步发布阻塞时长；v1.1=202 接受→任务 DONE 端到端
  （等价发布时长）；HTTP post p95 达标另行观测。
- 判据（docs/24 §7 R-01）复测见 docs/25 §10：对账闭合、onconflict 0；check/p95/吞吐仍
  3 项未达（reset=环境层立案 R-23；吞吐剩余瓶颈=JobManager 单 worker 串行设计——调优项
  非契约限制，并发 worker 已被 MONO.next_batch 槽位隔离支持）。

## 回退

SIM_ASYNC=off（env）即恢复 v1.0；镜像 sim:3.0.7 内两形态共存，无需回滚镜像。
