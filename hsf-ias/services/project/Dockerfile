# project-svc：项目全周期子图（:8093）。基础镜像含 fastapi/strawberry/pymysql 等（CONVENTIONS §0）
ARG BASE=10.167.2.175:30500/szxc/base:1.2
FROM ${BASE}
ENV PYTHONPATH=/srv
WORKDIR /srv
COPY services/common/hsf_common /srv/hsf_common
COPY services/project/app /srv/app
COPY services/project/tests /srv/tests
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8093", "--workers", "2"]
