"""HSF 数据访问：TiDB 双连接池（写池 hsf_app / 读池 hsf_ro）——读写分离在应用层落地（04 号文档§7）。

MVP 说明：读池与写池指向同一实例（tidb.infra.svc.cluster.local:4000，CONVENTIONS §0），
但使用只读账号 hsf_ro 做权限级隔离；后续可通过 DB_HOST_READ 将读池固定到专用 tidb-server
（一次 env 修改即可）。
"""
import os
import threading

import pymysql

_local = threading.local()


def _cfg(user_env: str, pass_env: str) -> dict:
    host_port = os.environ.get("DB_HOST_WRITE", "tidb.infra.svc.cluster.local:4000").split(":")
    ro_host = os.environ.get("DB_HOST_READ", "tidb.infra.svc.cluster.local:4000").split(":")
    if user_env.endswith("_RO"):
        host, port = ro_host[0], int(ro_host[1])
    else:
        host, port = host_port[0], int(host_port[1])
    return dict(
        host=host, port=port,
        user=os.environ[user_env], password=os.environ[pass_env],
        charset="utf8mb4", autocommit=True,
        connect_timeout=3, read_timeout=5, write_timeout=5,
    )


class _Pool:
    """极简线程级连接复用池（每工作线程一根连接，FastAPI 线程池模型下够用且零依赖）。"""

    def __init__(self, user_env: str, pass_env: str):
        self.user_env, self.pass_env = user_env, pass_env

    def conn(self):
        key = f"conn_{self.user_env}"
        c = getattr(_local, key, None)
        if c is None:
            try:
                c.close()
            except Exception:
                pass
            c = pymysql.connect(**_cfg(self.user_env, self.pass_env))
            setattr(_local, key, c)
        try:
            c.ping(reconnect=True)
        except Exception:
            c = pymysql.connect(**_cfg(self.user_env, self.pass_env))
            setattr(_local, key, c)
        return c


write_pool = _Pool("DB_USER", "DB_PASS")
read_pool = _Pool("DB_USER_RO", "DB_PASS_RO")


def query(sql: str, args: tuple = (), one: bool = False):
    """只读查询 → 走读池。"""
    with read_pool.conn().cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql: str, args: tuple = ()) -> int:
    """写操作 → 走写池，返回 lastrowid/affected。"""
    conn = write_pool.conn()
    with conn.cursor() as cur:
        cur.execute(sql, args)
    return cur.lastrowid if cur.lastrowid and cur.lastrowid > 0 else cur.rowcount


def execute_many(sql: str, args_list: list) -> int:
    conn = write_pool.conn()
    with conn.cursor() as cur:
        cur.executemany(sql, args_list)
    return cur.rowcount
