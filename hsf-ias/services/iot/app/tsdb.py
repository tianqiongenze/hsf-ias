"""iot TimescaleDB 访问：psycopg2 线程级连接复用 + execute_values 批量写。

对齐：hsf/docs/07 §3.2（hypertable DDL/写入形态冻结）、CONVENTIONS-P2 T-2。
连接串 env TIMESCALE_DSN（manifest 注入 postgresql://hsf:{TIMESCALE_PASS}@hsf-timescale:5432/hsf_ts）。
hypertable/连续聚合/策略由 scripts/init-ts.sh 初始化（D-07-8：iot 不做 DDL，只写数据）。
"""
import os
import threading

import psycopg2
import psycopg2.extensions
import psycopg2.extras   # execute_values 所在模块（W2-4：W2-3 修复误删此导入致 flush 全败回队）

DSN = os.environ.get("TIMESCALE_DSN", "postgresql://hsf:hsf@localhost:5432/hsf_ts")
PAGE_SIZE = 500  # execute_values 页大小（docs/07 §3.2 冻结）

_local = threading.local()


def _connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(DSN, connect_timeout=3)


def conn() -> psycopg2.extensions.connection:
    """每线程一根连接（复用而非每次重建）。

    部署实测缺陷链（W2-1/W2-2/W2-3）：
      W2-1 写后无 commit（事务悬挂不落库）→ 补 commit；
      W2-2 conn() 每次调用先 close 旧连接 → 写入与 commit 分属三个连接，事务被回滚 → 改为健康复用；
      W2-3 复用探测的 SELECT 1 之后无条件 rollback → 会把连接上未提交的业务写入一并回滚
           （rowcount 已取得、指标照涨、数据蒸发）→ 仅在事务状态 IDLE 时才 rollback 清理探测事务，
           有未提交业务事务时保持原状返回（后续 INSERT 加入同一事务，由调用方统一 commit）。"""
    c = getattr(_local, "conn", None)
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("SELECT 1")
            if c.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                c.rollback()          # 仅清理探测自身开启的事务；业务事务不动
            return c
        except Exception:
            try:
                c.close()
            except Exception:
                pass
    c = _connect()
    _local.conn = c
    return c


def query(sql: str, args: tuple = (), one: bool = False):
    """只读查询（DictCursor 行 → dict）；psycopg2 默认非 autocommit，读后提交以释放事务。"""
    with conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    conn().commit()
    return (rows[0] if rows else None) if one else [dict(r) for r in rows]


def execute_values_insert(sql: str, rows: list[tuple], page_size: int = PAGE_SIZE) -> int:
    """单批分页插入（docs/07 §3.2 写入形态：VALUES %s + ON CONFLICT DO NOTHING），
    返回实际插入行数——差值即 QoS1 重复跳过数。
    W2-1：psycopg2 默认 autocommit=False，INSERT 后必须显式 commit（否则数据停留在
    未提交事务中，外部连接不可见）。
    W3-2：psycopg2.extras.execute_values 内部分页 cur.execute 会覆盖 rowcount（仅剩
    最后一页 ≤500 的计数，iot-write 218 万条时指标只计 2.5 万）——改为手动分页并
    累计每页 rowcount，与表行数差值口径一致。"""
    if not rows:
        return 0
    c = conn()
    total = 0
    with c.cursor() as cur:
        for i in range(0, len(rows), page_size):
            psycopg2.extras.execute_values(cur, sql, rows[i:i + page_size],
                                           page_size=page_size)
            total += max(cur.rowcount, 0)
    c.commit()
    return total


def ping() -> None:
    """readyz 三连接校验之一（TiDB+Timescale+MQTT）：异常向上抛由探针转 503。"""
    with conn().cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    conn().commit()
