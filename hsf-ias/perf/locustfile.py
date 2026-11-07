# -*- coding: utf-8 -*-
"""HSF Locust 性能测试（对 GraphQL 联邦网关 30099 与 sim REST 31211）。
场景：浏览(地块/一张图/设施台账，读高频)、报修(写)、告警查询、项目查询。多用户并发，Web UI 实时聚合。
运行：locust -f locustfile.py --host=http://gw-svc.hsf-prod.svc.cluster.local:8090
"""
import random
import json
from locust import HttpUser, task, between

PARCELS = ["L530600000%d" % i for i in range(1, 9)]          # 种子 8 地块（04 号 §2.3）
VILLAGES = ["53060010101", "53060010102", "53060010103", "53060010201"]
FACILITIES = ["F530600000%d" % i for i in range(1, 7)]       # 种子设施（12 台中取前 6）
PROJECT_STATES = ["RESERVED", "SUBMITTED", "APPROVED", "CONSTRUCTING", "SUPERVISED", "ACCEPTED", "PERFORMED"]
SIM = "http://sim-svc.hsf-prod.svc.cluster.local:8095"


def _post(client, query, name, token=None, variables=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = {"query": query}
    if variables:
        body["variables"] = variables
    return client.post("/", json=body, name=name, headers=headers)


class Farmer(HttpUser):
    """种植户/村端混合浏览：地块详情(含联邦 holder 解析)、一张图统计、设施台账、告警。"""
    wait_time = between(0.5, 2.0)
    token = ""

    def on_start(self):
        r = _post(self.client,
                  'mutation { login(phone: "13800000604", password: "hsf@2026") { token } }',
                  "login-village")
        try:
            self.token = r.json()["data"]["login"]["token"]
        except Exception:
            self.token = ""

    @task(6)
    def land_parcel(self):
        c = random.choice(PARCELS)
        _post(self.client, '{ landParcel(code: "%s") { code name areaMu holder { name } } }' % c,
              "landParcel", self.token)

    @task(4)
    def land_stats(self):
        v = random.choice(VILLAGES)
        _post(self.client, '{ landStats(villageCode: "%s") { total totalAreaMu } }' % v,
              "landStats", self.token)

    @task(4)
    def land_geojson(self):
        v = random.choice(VILLAGES)
        _post(self.client, '{ landGeojson(villageCode: "%s") }' % v, "landGeojson", self.token)

    @task(3)
    def facilities(self):
        v = random.choice(VILLAGES)
        _post(self.client,
              '{ facilities(villageCode: "%s") { total items { code type status } } }' % v,
              "facilities", self.token)

    @task(2)
    def report_workorder(self):
        _post(self.client,
              'mutation { reportWorkorder(facilityCode: "%s", description: "locust-%d") { id sn } }'
              % (random.choice(FACILITIES), random.randint(1, 99999)),
              "reportWorkorder", self.token)

    @task(1)
    def sim_alerts(self):
        # 告警为 sim REST 独立服务（非网关 GraphQL），直连 31211/8095
        return self.client.get(SIM + "/sim/alerts?state=OPEN&page=1&size=20", name="sim-alerts",
                               headers={"Authorization": "Bearer " + self.token} if self.token else {})


class Officer(HttpUser):
    """管理端高频：项目台账/流转留痕查询、工单列表、审计。"""
    wait_time = between(1, 3)
    token = ""

    def on_start(self):
        r = _post(self.client,
                  'mutation { login(phone: "13800000601", password: "hsf@2026") { token } }',
                  "login-admin")
        try:
            self.token = r.json()["data"]["login"]["token"]
        except Exception:
            self.token = ""

    @task(5)
    def farm_projects(self):
        st = random.choice(PROJECT_STATES)
        _post(self.client, '{ farmProjects(state: %s) { total items { code state } } }' % st,
              "farmProjects", self.token)

    @task(3)
    def project_transitions(self):
        _post(self.client, '{ projectTransitions(code: "P5306000001") { action fromState toState } }',
              "projectTransitions", self.token)

    @task(3)
    def workorders(self):
        _post(self.client, '{ workorders(state: OPEN) { total items { id sn state } } }',
              "workorders", self.token)

    @task(2)
    def audit_logs(self):
        _post(self.client, '{ auditLogs(page: 1, size: 20) { total items { action result } } }',
              "auditLogs", self.token)

    @task(2)
    def publish_alert_stats(self):
        import json as _json
        r = self.client.get(SIM + "/sim/alerts/statistics?dim=state", name="sim-alert-statistics",
                            headers={"Authorization": "Bearer " + self.token} if self.token else {})
        return r
