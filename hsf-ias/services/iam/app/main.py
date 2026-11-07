"""HSF iam-svc：身份与组织子图（GraphQL Federation，:8091）。"""
import sys

sys.path.insert(0, "/srv")

from strawberry.fastapi import GraphQLRouter

from hsf_common.web import create_app, gql_context

from app.repo import init_db, seed_if_empty
from app.schema import schema

app = create_app("iam")
app.include_router(GraphQLRouter(schema, context_getter=gql_context), prefix="/graphql")


@app.on_event("startup")
def _init():
    init_db()       # hsf_md 三表 + hsf_biz.audit_log（幂等）
    seed_if_empty()  # 9 组织节点 + 15 演示账号，双守卫防并发重种
