# HSF ratelimit-svc：全局限流（8096）——构建上下文=hsf-ias/ 根，与 sim 同法（CONVENTIONS §3.3）
ARG BASE=10.167.2.175:30500/szxc/base:1.2
FROM ${BASE}
ENV PYTHONPATH=/srv
WORKDIR /srv
COPY services/common/hsf_common /srv/hsf_common
COPY services/ratelimit/app /srv/app
COPY services/ratelimit/tests /srv/tests
COPY services/ratelimit/requirements.txt /srv/requirements.txt
COPY services/ratelimit/README.md /srv/README.md
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8096", "--workers", "2"]
