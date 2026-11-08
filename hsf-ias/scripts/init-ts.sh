#!/bin/bash
# HSF 二期 TimescaleDB 初始化（全幂等，可重复执行）——docs/07 §3.1~§3.5 冻结 DDL/策略
# 步骤：等 pg_isready → 确认库 hsf_ts → CREATE EXTENSION timescaledb → 建 schema/hypertable/
#       唯一索引（D-07-3：承接 MQTT QoS1 重复/补投）→ 连续聚合 5min/1h + 刷新/保留策略 →
#       压缩策略 + 原始 90 天保留。全部语句 IF NOT EXISTS / if_not_exists => TRUE，
#       重跑=核对既有对象（V2-04：策略 job 恒为 6 条 = 2 刷新 + 3 保留 + 1 压缩）
# 库/账号口径（docs/07 §3.1）：以 hsf/TIMESCALE_PASS 连 hsf_ts——与 iot TIMESCALE_DSN 同一 DSN，
#   hsf 为超级用户（POSTGRES_USER 引导），CREATE EXTENSION 权限随之满足；密码缺省读 hsf-secrets
# 执行方式（二选一）：
#   A. 本机有 kubectl（默认）：自动 exec 进 hsf-timescale-0 容器内 psql，经 ClusterIP 直连 svc
#   B. 临时 Pod（CONVENTIONS §3.2 临时 pod 模式）：
#        kubectl -n hsf-prod run init-ts --rm -i --restart=Never \
#          --image=10.167.2.175:30500/base/timescaledb:2.17.2-pg16 \
#          --env="TIMESCALE_PASS=<Secret TIMESCALE_PASS>" -- bash -s < scripts/init-ts.sh
#      （B 路径容器自带 psql，pod 侧无 kubectl 时自动走直连 DSN：hsf-timescale:5432/hsf_ts）
set -e
NS=${NS:-hsf-prod}
TS_HOST=${TS_HOST:-hsf-timescale.hsf-prod.svc.cluster.local}   # 同 iot TIMESCALE_DSN 主机（docs/07 §1.4）
TS_PORT=${TS_PORT:-5432}
TS_USER=${TS_USER:-hsf}
TS_DB=${TS_DB:-hsf_ts}
WAIT_SEC=${WAIT_SEC:-180}    # pg_isready 等待上限（StatefulSet 首次 initdb 需要时间）

# 密码缺省从 hsf-secrets 读取（docs/07 §1.4：StatefulSet/iot/init-ts.sh 三方共用同一键）
if [ -z "$TIMESCALE_PASS" ]; then
  TIMESCALE_PASS=$(kubectl -n "$NS" get secret hsf-secrets -o jsonpath='{.data.TIMESCALE_PASS}' 2>/dev/null | base64 -d)
fi
if [ -z "$TIMESCALE_PASS" ]; then
  echo "INIT-TS-FAIL | TIMESCALE_PASS 为空（Secret hsf-secrets/TIMESCALE_PASS 缺失或未同步）"; exit 1
fi

# ── 执行通道选择：优先 kubectl exec 进时序库 pod（pod 内 psql）；无 pod 则本机 psql 直连 DSN ──
TS_POD=${TS_POD-$(kubectl -n "$NS" get pod -l app=hsf-timescale -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)}
if [ -n "$TS_POD" ] && kubectl -n "$NS" get pod "$TS_POD" >/dev/null 2>&1; then
  MODE="kubectl-exec:$TS_POD"
  # PGUSER 必须显式：exec 进容器 psql 默认取 OS 用户（root）→ FATAL role "root" does not exist
  # （2026-11-08 部署实测）；PGPASSWORD+PGUSER 与 local-psql 通道的 -U/-w 语义对齐
  tsql()    { kubectl -n "$NS" exec -i "$TS_POD" -- env PGPASSWORD="$TIMESCALE_PASS" PGUSER="$TS_USER" psql "$@"; }
  pgready() { kubectl -n "$NS" exec "$TS_POD" -- pg_isready -h "$TS_HOST" -p "$TS_PORT" -U "$TS_USER" -t 2 -q; }
else
  command -v psql >/dev/null 2>&1 || { echo "INIT-TS-FAIL | 无 kubectl 可用 pod 且本机无 psql（用执行方式 B）"; exit 1; }
  MODE="local-psql"
  tsql()    { PGPASSWORD="$TIMESCALE_PASS" psql "$@"; }
  pgready() { pg_isready -h "$TS_HOST" -p "$TS_PORT" -U "$TS_USER" -t 2 -q; }
fi
echo "INIT-TS | 通道=$MODE host=$TS_HOST:$TS_PORT db=$TS_DB user=$TS_USER"

# ── 1. 等 PG 就绪（pg_isready 不需密码；最长 WAIT_SEC 秒）──
DEADLINE=$((SECONDS + WAIT_SEC))
until pgready; do
  [ $SECONDS -ge $DEADLINE ] && { echo "INIT-TS-FAIL | ${WAIT_SEC}s 内 pg_isready 未就绪（pod/服务未起？）"; exit 1; }
  sleep 3
done
echo "INIT-TS-OK | pg_isready 就绪"

# ── 2. 确认数据库 hsf_ts（StatefulSet POSTGRES_DB 引导已建则跳过；缺则补建——幂等）──
HAS_DB=$(tsql -X -A -t -d postgres -c "SELECT 1 FROM pg_database WHERE datname='$TS_DB'" | tr -d '[:space:]')
if [ "$HAS_DB" = "1" ]; then
  echo "INIT-TS-OK | 数据库 $TS_DB 已存在（跳过 CREATE DATABASE）"
else
  tsql -X -v ON_ERROR_STOP=1 -d postgres -c "CREATE DATABASE $TS_DB"
  echo "INIT-TS-OK | CREATE DATABASE $TS_DB"
fi

# ── 3. 冻结 DDL（docs/07 §3.2/§3.4/§3.5 逐条对应；全幂等）──
tsql -X -v ON_ERROR_STOP=1 -d "$TS_DB" <<'SQL'
-- 库名与 schema 同名：initdb 默认仅 public，先建 schema 否则 CREATE TABLE 报 schema 不存在（§3.2）
CREATE SCHEMA IF NOT EXISTS hsf_ts;
CREATE EXTENSION IF NOT EXISTS timescaledb;   -- 引导权限口径：POSTGRES_USER=hsf 即超级用户（§3.1）

CREATE TABLE IF NOT EXISTS hsf_ts.sensor_reading(
  device_code CHAR(20)         NOT NULL,
  metric      VARCHAR(16)      NOT NULL,
  value       DOUBLE PRECISION NOT NULL,
  recorded_at TIMESTAMPTZ      NOT NULL
);
-- PostgreSQL 无行内 COMMENT 子句（那是 MySQL/TiDB 语法），列注释一律后置语句幂等覆盖（§3.2）
COMMENT ON COLUMN hsf_ts.sensor_reading.device_code IS '设备编码=sensor.sensor_code（S+6位区划+4位序号，列宽预留对齐 CONVENTIONS §2.3 宁可宽不可窄；PG bpchar 等值比较忽略尾部空格）';
COMMENT ON COLUMN hsf_ts.sensor_reading.metric      IS '04 §4.7 冻结 6 类';
COMMENT ON COLUMN hsf_ts.sensor_reading.value       IS '观测值（DECIMAL→DOUBLE：时序聚合允许浮点，量纲与一期一致）';
COMMENT ON COLUMN hsf_ts.sensor_reading.recorded_at IS '观测时刻（sim 生成，ISO8601+08:00）';

SELECT create_hypertable('hsf_ts.sensor_reading', 'recorded_at',
       chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);   -- §3.3 chunk=1 天（90 chunk）

-- 幂等唯一索引（含时间分区列，hypertable 合法）：以数据库幂等承接 QoS1 重复/补投（D-07-3）。
-- 不能用 CREATE INDEX IF NOT EXISTS：psql 默认 search_path=public，按名判重查不到 hsf_ts
-- schema 里的既有索引 → 重跑时真去 CREATE → 压缩已启用的 hypertable 报
-- "operation not supported on hypertables that have compression enabled"（2026-11-08 二次执行实测）。
-- 故以 pg_indexes（schema 限定）判重后创建，跨 search_path 幂等
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'hsf_ts' AND indexname = 'uk_reading'
  ) THEN
    CREATE UNIQUE INDEX uk_reading ON hsf_ts.sensor_reading(device_code, metric, recorded_at);
  END IF;
END $$;

-- ── 连续聚合 5min（报表/趋势图/大屏；§3.4）──
CREATE MATERIALIZED VIEW IF NOT EXISTS hsf_ts.sensor_reading_5min
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '5 minutes', recorded_at) AS bucket,
       device_code, metric,
       avg(value) AS avg_v, min(value) AS min_v, max(value) AS max_v, count(*) AS cnt
FROM hsf_ts.sensor_reading GROUP BY 1, 2, 3 WITH NO DATA;
SELECT add_continuous_aggregate_policy('hsf_ts.sensor_reading_5min',
       start_offset => INTERVAL '3 hours', end_offset => INTERVAL '5 minutes',
       schedule_interval => INTERVAL '10 minutes', if_not_exists => TRUE);
SELECT add_retention_policy('hsf_ts.sensor_reading_5min', INTERVAL '90 days', if_not_exists => TRUE);

-- ── 连续聚合 1h（归档口径主载体，3 年在线，NFR-07；§3.4）──
CREATE MATERIALIZED VIEW IF NOT EXISTS hsf_ts.sensor_reading_1h
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', recorded_at) AS bucket,
       device_code, metric,
       avg(value) AS avg_v, min(value) AS min_v, max(value) AS max_v, count(*) AS cnt
FROM hsf_ts.sensor_reading GROUP BY 1, 2, 3 WITH NO DATA;
SELECT add_continuous_aggregate_policy('hsf_ts.sensor_reading_1h',
       start_offset => INTERVAL '1 day', end_offset => INTERVAL '1 hour',
       schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE);
SELECT add_retention_policy('hsf_ts.sensor_reading_1h', INTERVAL '3 years', if_not_exists => TRUE);

-- ── 压缩与原始 90 天保留（§3.5；策略参数关系名一律 hsf_ts. 限定——psql 默认 search_path=public）──
-- 压缩启用必须幂等守卫：compression 已启用后重跑 SET (timescaledb.compress) 会报
-- "operation not supported on hypertables that have compression enabled"（2026-11-08 二次执行实测），
-- 故仅当未启用时执行；策略语句本身 if_not_exists => TRUE 幂等
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM timescaledb_information.hypertables
    WHERE hypertable_schema = 'hsf_ts' AND hypertable_name = 'sensor_reading'
      AND compression_enabled
  ) THEN
    ALTER TABLE hsf_ts.sensor_reading SET (
      timescaledb.compress,
      timescaledb.compress_segmentby = 'device_code, metric',
      timescaledb.compress_orderby   = 'recorded_at DESC');
  END IF;
END $$;
SELECT add_compression_policy('hsf_ts.sensor_reading', INTERVAL '1 day', if_not_exists => TRUE);
SELECT add_retention_policy('hsf_ts.sensor_reading',   INTERVAL '90 days', if_not_exists => TRUE);
SQL
echo "INIT-TS-OK | 冻结 DDL 执行通过（schema/扩展/hypertable/uk_reading/5min+1h 聚合/压缩/保留）"

# ── 4. 初始化结果核对（V2-04 口径：扩展存在、hypertable 建成、策略 job 恒 6 条）──
echo "── timescaledb 版本"
tsql -X -A -t -d "$TS_DB" -c "SELECT extname || ' ' || extversion FROM pg_extension WHERE extname='timescaledb';"
echo "── hypertable（应含 hsf_ts.sensor_reading）"
tsql -X -A -t -d "$TS_DB" -c "SELECT hypertable_schema || '.' || hypertable_name FROM timescaledb_information.hypertables;"
echo "── 策略 job（应恒 6 条 = 2 refresh + 3 retention + 1 compression，V2-04）"
tsql -X -A -t -d "$TS_DB" -c "SELECT job_id || ':' || proc_name || ':' || hypertable_name FROM timescaledb_information.jobs ORDER BY job_id;"
echo "✅ INIT-TS 全部通过（幂等初始化完成；重复执行为核对模式）"
