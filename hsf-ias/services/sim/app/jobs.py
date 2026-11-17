"""sim 异步发布与连接池（docs/07 §2.5 v1.1 + ADR-HSF-028，2026-11-15）。

R-01 判据兑现的结构改造（docs/25 §3.2 实证瓶颈链）：
  ① PublisherPool——N 个 MqttPublisher 连接（PUB_CLIENTS，缺省 4）按下标分片并行发布，
     解除单 paho 连接 ~2,900 条/s 的发布结构上限（4 连接 ≈ 4×，发布能力 ≈11k 条/s）；
  ② JobManager——steps>SYNC_MAX 的批量 tick 后台化：HTTP 202+jobId 即时返回
     （解除单请求 2.4 万条 ×41s 的串行等待，tick p95 判据 p95≤1.5s 兑现口），
     单 worker 串行"生成→池并行发布"；GET /sim/tick/jobs/{jobId} 查进度；
  ③ 单调时钟（mqtt_pub.MONO）与 SIM_ASYNC=off 回退开关（回退=完全一期同步行为）。

冻结不动（docs/07 §2.1/§2.2/§2.3）：topic 形态、payload 形态、QoS1、retain=false、
steps≤SYNC_MAX 的同步响应（published 条数——E2E 用例 16 等常规链路零改动）。
"""
import collections
import queue
import threading
import time

from . import metrics
from . import mqtt_pub
from .mqtt_pub import MqttPublisher


class PublisherPool:
    """N 连接发布池：readings 按 i%size 分片，每片由独立连接并行发布（线程 join 汇总）。

    0 号连接兼职心跳（provider 注入），其余纯发布（provider 恒空、无心跳线程）。
    对 main.py 鸭子类型兼容 MqttPublisher：is_connected/publish_readings/published_total/stop。"""

    def __init__(self, provider, host: str, port: int, user: str, password: str,
                 size: int = 4, interval: float | None = None):
        self._size = max(1, int(size))
        self._clients: list[MqttPublisher] = []
        for i in range(self._size):
            self._clients.append(MqttPublisher(
                provider if i == 0 else (lambda: []),
                host, port, user, password,
                interval=interval if i == 0 else None,
                client_id=f"hsf-sim-pub-{i}",
                heartbeat=(i == 0)))

    def start(self) -> None:
        for c in self._clients:
            c.start()

    def publish_readings(self, readings: list[tuple]) -> int:
        """分片并行发布：总接受数 = Σ各连接接受数（fsync 语义不变，QoS1 逐条确认）。"""
        if self._size == 1:
            return self._clients[0].publish_readings(readings)
        shards = [readings[i::self._size] for i in range(self._size)]
        results = [0] * self._size
        threads: list[threading.Thread] = []

        def _run(idx: int, shard: list[tuple]) -> None:
            results[idx] = self._clients[idx].publish_readings(shard)

        for i, shard in enumerate(shards):
            if not shard:
                continue
            t = threading.Thread(target=_run, args=(i, shard), name=f"sim-pool-{i}")
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        return sum(results)

    def is_connected(self) -> bool:
        """池连接态：任一连接在线即可发布（心跳在 0 号，其状态最关键）。"""
        return any(c.is_connected() for c in self._clients)

    def published_total(self) -> int:
        return sum(c.published_total() for c in self._clients)

    def stop(self) -> None:
        for c in self._clients:
            c.stop()


class JobManager:
    """批量 tick 后台任务管理器：有界队列 + 单 worker（生成串行→池并行发布）。

    生成串行是正确性设计：repo.generate_readings 经 MONO.next_batch 取号，
    单 worker 下跨任务基点天然有序，recorded_at 全局唯一（onconflict≈0）。
    任务记录保留最近 keep 条（OrderedDict 淘汰），status 对未知/已淘汰 id 返回 None。"""

    def __init__(self, pool: PublisherPool, gen, queue_max: int = 8, keep: int = 64,
                 workers: int = 1):
        self._pool = pool
        self._gen = gen
        self._q: queue.Queue[str] = queue.Queue(maxsize=max(1, int(queue_max)))
        self._keep = max(8, int(keep))
        self._jobs: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self._lock = threading.Lock()
        self._seq = 0
        self._stop = threading.Event()
        # v1.1 调优项（docs/25 §10）：并发 job worker——recorded_at 全局有序由
        # MONO.next_batch 槽位预留保证（并发任务取号互不重叠），单 worker 不再是
        # 吞吐结构上限；workers=1 为缺省（保守等价旧行为）
        self._workers = max(1, int(workers))

    def submit(self, steps: int) -> str | None:
        """入队一个批量发布任务；队列满返回 None（端点转 503 BUSY）。"""
        with self._lock:
            self._seq += 1
            jid = f"J{time.strftime('%Y%m%d%H%M%S')}-{self._seq:04d}"
            self._jobs[jid] = {"jobId": jid, "steps": int(steps), "state": "QUEUED",
                               "published": 0, "error": None}
            while len(self._jobs) > self._keep:      # 淘汰最旧（含未完成的极旧任务不回收
                self._jobs.popitem(last=False)       # ——仅观测面收缩，worker 以队列为准）
        try:
            self._q.put_nowait(jid)
        except queue.Full:
            with self._lock:
                self._jobs.pop(jid, None)
            metrics.JOB_QUEUE_DEPTH.set(self._q.qsize())
            return None
        metrics.JOB_QUEUE_DEPTH.set(self._q.qsize())
        return jid

    def status(self, job_id: str) -> dict | None:
        with self._lock:
            j = self._jobs.get(job_id)
            return dict(j) if j else None

    def depth(self) -> int:
        return self._q.qsize()

    def _set(self, job_id: str, **fields) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j is not None:
                j.update(fields)

    def worker_loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._q.get()
            if job_id is None:
                break
            job = self.status(job_id)
            if job is None:
                continue
            self._set(job_id, state="PUBLISHING")
            try:
                readings = self._gen(job["steps"])
                published = self._pool.publish_readings(readings)
                self._set(job_id, state="DONE", published=int(published))
                metrics.JOB_DONE.labels(state="done").inc()
            except Exception as e:                   # 生成/发布异常不致 worker 退出
                self._set(job_id, state="FAILED", error=str(e)[:200])
                metrics.JOB_DONE.labels(state="failed").inc()

    def start(self) -> None:
        for i in range(self._workers):
            threading.Thread(target=self.worker_loop, name=f"sim-job-worker-{i}",
                             daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait("__stop__")
        except queue.Full:
            pass
