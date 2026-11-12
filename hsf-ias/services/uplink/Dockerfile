ARG BASE=10.167.2.175:30500/szxc/base:1.2
FROM ${BASE}
# 三期新增：无增量依赖（复用 base 锁定依赖——uplink 纯 TiDB+FastAPI，无 numpy/MQTT）
ENV PYTHONPATH=/srv
WORKDIR /srv
COPY services/common/hsf_common /srv/hsf_common
COPY services/uplink/app /srv/app
COPY services/uplink/tests /srv/tests
COPY services/uplink/README.md /srv/README.md
# 单 worker 固定（无进程内状态——保守口径，与全家族一致）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099", "--workers", "1"]
