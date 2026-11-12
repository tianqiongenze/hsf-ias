"""HSF uplink TiDB 访问：uplink_task DDL + 状态机读写（D-08-3）。"""
import json

from hsf_common import db

from . import metrics

DDL = [
    """CREATE TABLE IF NOT EXISTS hsf_biz.uplink_task(
  id           BIGINT NOT NULL AUTO_INCREMENT,
  batch_code   CHAR(32) NOT NULL COMMENT '批次编码（幂等键）',
  payload      JSON NOT NULL, item_count INT NOT NULL,
  state        VARCHAR(16) NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/SENT/ACKED/REJECTED',
  receipt      JSON NULL COMMENT '回执（stub 或真实平台）',
  sent_at      DATETIME NULL,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id), UNIQUE KEY uk_batch (batch_code)
) COMMENT='上级平台上报任务（D-08-3 新增，存量零变更）'""",
]


def init_db() -> None:
    for ddl in DDL:
        db.execute(ddl.replace("（%", "（百分数").replace("%）", "百分数）"))  # W2-1 同源教训


def count_tasks() -> int:
    row = db.query("SELECT count(*) AS n FROM hsf_biz.uplink_task", one=True)
    return int(row["n"]) if row else 0


def insert_task(batch_code: str, payload: dict, item_count: int) -> int | None:
    n = db.execute("INSERT IGNORE INTO hsf_biz.uplink_task(batch_code,payload,item_count) "
                   "VALUES(%s,%s,%s)",
                   (batch_code, json.dumps(payload, ensure_ascii=False), item_count))
    return None if n == 0 else db.query(
        "SELECT id FROM hsf_biz.uplink_task WHERE batch_code=%s", (batch_code,),
        one=True)["id"]


def get_task(batch_code: str) -> dict | None:
    row = db.query("SELECT id, batch_code, payload, item_count, state, receipt, sent_at, "
                   "created_at FROM hsf_biz.uplink_task WHERE batch_code=%s",
                   (batch_code,), one=True)
    if row is None:
        return None
    row["payload"] = json.loads(row["payload"])
    row["receipt"] = json.loads(row["receipt"]) if row["receipt"] else None
    return row


def mark_sent(batch_code: str, receipt: dict, ok: bool) -> None:
    db.execute("UPDATE hsf_biz.uplink_task SET state=%s, receipt=%s, sent_at=NOW() "
               "WHERE batch_code=%s",
               ("SENT" if ok else "REJECTED",
                json.dumps(receipt, ensure_ascii=False), batch_code))


def mark_acked(batch_code: str, receipt: dict) -> None:
    db.execute("UPDATE hsf_biz.uplink_task SET state='ACKED', receipt=%s WHERE batch_code=%s",
               (json.dumps(receipt, ensure_ascii=False), batch_code))
    metrics.ACKED.inc()


def tasks_list(state: str | None, page: int, size: int) -> dict:
    where, args = ("WHERE state=%s", [state]) if state else ("WHERE 1=1", [])
    total = (db.query(f"SELECT count(*) AS n FROM hsf_biz.uplink_task {where}",
                      tuple(args), one=True) or {}).get("n", 0)
    items = db.query(f"SELECT batch_code, item_count, state, receipt, sent_at, created_at "
                     f"FROM hsf_biz.uplink_task {where} ORDER BY id DESC LIMIT %s OFFSET %s",
                     tuple(args) + (size, (page - 1) * size))
    for it in items:
        it["payload"] = None                            # 列表不回 payload（体积），详情走 /payload
    return {"total": int(total), "page": page, "size": size, "items": items}
