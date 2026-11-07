ARG BASE=10.167.2.175:30500/szxc/base:1.2
FROM ${BASE}
ENV PYTHONPATH=/srv
WORKDIR /srv
COPY services/common/hsf_common /srv/hsf_common
COPY services/facility/app /srv/app
COPY services/facility/tests /srv/tests
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8094", "--workers", "2"]
