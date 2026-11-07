"""HSF facility-svc：设施台账与管护子图（:8094，二维码/工单 4 态闭环）。"""
import sys

sys.path.insert(0, "/srv")

from strawberry.fastapi import GraphQLRouter

from hsf_common.web import create_app, gql_context

from app.repo import init_db, seed_if_empty
from app.schema import schema

app = create_app("facility")
app.include_router(GraphQLRouter(schema, context_getter=gql_context), prefix="/graphql")


@app.on_event("startup")
def _init():
    init_db()
    seed_if_empty()  # 双守卫幂等：设施 12 + 工单 5（party 未就绪时工单种子下次启动重试）
