ARG BASE=10.167.2.175:30500/szxc/base:1.2
FROM ${BASE}
# 三期新增依赖（CONVENTIONS-P3 U-1/V-1）：numpy（纯 CPU 数值，无 torch/tf）
COPY services/rs/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm -f /tmp/requirements.txt
ENV PYTHONPATH=/srv
WORKDIR /srv
COPY services/common/hsf_common /srv/hsf_common
COPY services/rs/app /srv/app
COPY services/rs/tests /srv/tests
COPY services/rs/README.md /srv/README.md
# 单 worker 固定（解译同步执行、无进程内可变状态——保守口径与全家族一致，性能足够：样本级 <10s）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8098", "--workers", "1"]
