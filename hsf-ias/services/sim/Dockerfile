ARG BASE=10.167.2.175:30500/szxc/base:1.2
FROM ${BASE}
ENV PYTHONPATH=/srv
WORKDIR /srv
COPY services/common/hsf_common /srv/hsf_common
COPY services/sim/app /srv/app
COPY services/sim/tests /srv/tests
COPY services/sim/README.md /srv/README.md
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8095", "--workers", "2"]
