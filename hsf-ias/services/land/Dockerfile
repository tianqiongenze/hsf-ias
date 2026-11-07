# land-svc：地块主数据子图（:8092）。基础镜像含 fastapi/strawberry/pymysql 等（CONVENTIONS §0）
ARG BASE=10.167.2.175:30500/szxc/base:1.2
FROM ${BASE}
ENV PYTHONPATH=/srv
WORKDIR /srv
COPY services/common/hsf_common /srv/hsf_common
COPY services/land/app /srv/app
COPY services/land/tests /srv/tests
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8092", "--workers", "2"]
