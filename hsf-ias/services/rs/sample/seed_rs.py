#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""seed_rs.py——遥感样本验收数据 seeder（docs/08 §2.5 冻结交付物）。

按 §2.5 冻结口径**调 /scenes 登记两景**（acquired_at 间隔 30 天；第二景对 L5306000003
构造 NDVI 降幅触发 RULE_NDVIFALL 非农化预警），再逐景触发 /scenes/{code}/process 解译——
验收数据确定性可复现。波段矩阵由 make_sample.make() 生成（唯一实现点 app/sample._bands）。

等价替代：rs 服务自带 POST /sample/seed（服务端同一确定性场景，E2E 用例 20 使用）——
本脚本用于无法登 admin 页签/需从外部构造登记请求的场合（容器外冒烟、联调回放）。

用法：
    python services/rs/sample/seed_rs.py [--gw URL] [--rs URL]
    # GW 缺省 http://10.167.2.175:30099（拿 admin token）；RS 缺省 http://10.167.2.175:31214
    # 环境变量 GW/RS 可覆盖；账号取 env HSF_ADMIN_USER/HSF_ADMIN_PASS（缺省 admin/admin123）
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))                   # services/rs（app 包根）

from make_sample import make, SCENE_PLANS                     # noqa: E402


def _post(url: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def login_admin(gw: str, user: str, pwd: str) -> str:
    """gw GraphQL 登录取 admin token（05 §2.1 冻结 mutation：login(phone,password)——
    iam schema 仅受理 phone（P3-3 复核发现：早期版本误用 username 必登录失败）。"""
    q = 'mutation($u:String!,$p:String!){login(phone:$u,password:$p){token}}'
    code, data = _post(gw.rstrip("/") + "/",
                       {"query": q, "variables": {"u": user, "p": pwd}})
    tok = ((data.get("data") or {}).get("login") or {}).get("token")
    if code != 200 or not tok:
        raise SystemExit(f"seed_rs: 登录失败 http={code} resp={str(data)[:120]}")
    return tok


def main() -> int:
    ap = argparse.ArgumentParser(description="HSF rs 样本两景登记+解译（docs/08 §2.5）")
    ap.add_argument("--gw", default=os.environ.get("GW", "http://10.167.2.175:30099"))
    ap.add_argument("--rs", default=os.environ.get("RS", "http://10.167.2.175:31214"))
    ap.add_argument("--user", default=os.environ.get("HSF_ADMIN_USER", "13800000601"))
    ap.add_argument("--password", default=os.environ.get("HSF_ADMIN_PASS", "hsf@2026"))
    a = ap.parse_args()

    tok = login_admin(a.gw, a.user, a.password)
    print(f"seed_rs: 登录 OK（gw={a.gw}）")
    now = dt.datetime.now()
    # 冻结口径：两景 acquired_at 间隔 30 天（第一景=今天-30d 09:30，第二景=今天 09:30）
    acquired = [(now - dt.timedelta(days=30)).strftime("%Y-%m-%d 09:30:00"),
                now.strftime("%Y-%m-%d 09:30:00")]
    results = []
    for (code, ndvi_map), acq in zip(SCENE_PLANS, acquired):
        payload = make(ndvi_map)
        payload.update({"sceneCode": code, "acquiredAt": acq})
        s1, r1 = _post(a.rs.rstrip("/") + "/scenes", payload, tok)
        s2, r2 = _post(a.rs.rstrip("/") + f"/scenes/{code}/process", {}, tok)
        results.append({"sceneCode": code, "register": s1, "process": s2,
                        "parcels": r2.get("parcels"), "alerts": r2.get("alerts")})
        print(f"seed_rs: {code} 登记={s1} 解译={s2} 地块={r2.get('parcels')} 预警={r2.get('alerts')}")
    ok = all(r["register"] in (200, 409) and r["process"] == 200 for r in results)
    print("seed_rs: " + ("✅ 两景登记+解译完成（确定性可复现）" if ok else "❌ 存在失败，见上行"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
