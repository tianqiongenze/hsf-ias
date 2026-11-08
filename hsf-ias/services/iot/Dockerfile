ARG BASE=10.167.2.175:30500/szxc/base:1.2
FROM ${BASE}
# 二期新增依赖（基础镜像不含）：psycopg2-binary / paho-mqtt
COPY services/iot/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm -f /tmp/requirements.txt
ENV PYTHONPATH=/srv
WORKDIR /srv
COPY services/common/hsf_common /srv/hsf_common
COPY services/iot/app /srv/app
COPY services/iot/tests /srv/tests
COPY services/iot/README.md /srv/README.md
# 单 worker 固定（docs/07 §4.1 D-07-1）：latest 缓存与回差状态机为进程内状态，
# 多 worker 会导致 MQTT 会话/缓冲/状态机分裂——manifest 同样禁 HPA、副本固定 1。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8097", "--workers", "1"]
