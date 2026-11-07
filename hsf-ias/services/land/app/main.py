"""hsf land-svc：地块主数据子图（统一编码 / 台账 / 一张图 GeoJSON，:8092）。"""
import sys
import threading

sys.path.insert(0, "/srv")

from strawberry.fastapi import GraphQLRouter

from hsf_common.web import create_app, gql_context

from app.repo import init_db, seed_if_empty
from app.schema import schema

app = create_app("land")
app.include_router(GraphQLRouter(schema, context_getter=gql_context), prefix="/graphql")


@app.on_event("startup")
def _init():
    init_db()
    # 种子依赖 iam 建的 org/party，放后台线程等待就绪，不阻塞 /healthz（CONVENTIONS §2.14）
    threading.Thread(target=seed_if_empty, name="land-seed", daemon=True).start()
