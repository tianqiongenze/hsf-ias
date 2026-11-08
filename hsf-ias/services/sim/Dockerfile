ARG BASE=10.167.2.175:30500/szxc/base:1.2
FROM ${BASE}
# 二期新增依赖（基础镜像不含）：paho-mqtt（MQTT 发布器，与 iot 订阅侧同版锁定）
COPY services/sim/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm -f /tmp/requirements.txt
ENV PYTHONPATH=/srv
WORKDIR /srv
COPY services/common/hsf_common /srv/hsf_common
COPY services/sim/app /srv/app
COPY services/sim/tests /srv/tests
COPY services/sim/README.md /srv/README.md
# 二期改单 worker（docs/07 §1.2 sim 副本=1）：MQTT 发布器为进程内单线程状态，
# 多 worker 会导致心跳双倍发布（48×2 条/2s）——量级核算（D-07-6）以单发布方为前提。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8095", "--workers", "1"]
