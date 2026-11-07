"""hsf project-svc：项目全周期子图（7 态状态机 / 项目-地块关联 / 监理联邦引用，:8093）。"""
import sys
import threading

sys.path.insert(0, "/srv")

from strawberry.fastapi import GraphQLRouter

from hsf_common.web import create_app, gql_context

from app.repo import init_db, seed_if_empty
from app.schema import schema

app = create_app("project")
app.include_router(GraphQLRouter(schema, context_getter=gql_context), prefix="/graphql")


@app.on_event("startup")
def _init():
    init_db()
    # 种子依赖 iam 的 org/party 与 land 的地块，放后台线程等待就绪，不阻塞 /healthz（CONVENTIONS §2.14）
    threading.Thread(target=seed_if_empty, name="project-seed", daemon=True).start()
