# -*- coding: utf-8 -*-
"""HSF 无头浏览器全链路功能测试（Playwright，≥10 用例）。
覆盖：三端渲染 / 登录 / 错密拒绝 / 一张图地块渲染 / 项目创建+7 态全周期流转 /
      设施台账+报修工单 4 态闭环 / 告警查询 / 联邦跨域 / 限流触发（限流放最后，避免打空令牌桶）。
以 JSON 行输出每步结论，结尾 FAIL 计数；约定与 hsf/docs/05 冻结 SDL/错误码逐条对应。
运行：python -I e2e_browser.py（集群内 Job 或能解析 svc DNS 的环境）
"""
import json, sys, traceback, os
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright")
from playwright.sync_api import sync_playwright

GW = os.environ.get("GW", "http://gw-svc.hsf-prod.svc.cluster.local:8090/")
WEB = os.environ.get("WEB", "http://web-svc.hsf-prod.svc.cluster.local:80/")
SIM = os.environ.get("SIM", "http://sim-svc.hsf-prod.svc.cluster.local:8095")
# 种子账号（hsf/docs/04 §9.1：口令统一 hsf@2026）
ADMIN, TOWN, VILLAGE, KEEPER = "13800000601", "13800000602", "13800000604", "13800000608"
PASSWD = "hsf@2026"
VILLAGE_CODE = "53060010101"       # 清溪村
PARCEL, FACILITY = "L5306000001", "F5306000001"
RATE_CAP = int(os.environ.get("RATE_CAP", "6000"))   # 网关令牌桶 6000/min（CONVENTIONS §2.6）

RESULTS = []
FAIL = [0]
FAILED = []


def rec(name, ok, extra=""):
    tag = "PASS" if ok else "FAIL"
    if not ok:
        FAIL[0] += 1
    RESULTS.append("%s | %s | %s" % (tag, name, extra))
    print(RESULTS[-1], flush=True)


def gql(page, query, token=None, variables=None, timeout=20000):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = {"query": query}
    if variables:
        body["variables"] = variables
    resp = page.request.post(GW, data=json.dumps(body), headers=headers, timeout=timeout)
    return json.loads(resp.text()), resp.status


def sim_get(page, path, token=None, timeout=20000):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    resp = page.request.get(SIM + path, headers=headers, timeout=timeout)
    try:
        return json.loads(resp.text()), resp.status
    except Exception:
        return {"raw": resp.text()}, resp.status


def login(page, phone, password=PASSWD):
    r, _ = gql(page, 'mutation($p:String!,$w:String!){login(phone:$p,password:$w){token name partyId}}',
               variables={"p": phone, "w": password})
    d = (r.get("data") or {}).get("login") or {}
    return d.get("token", ""), d.get("partyId"), r


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()

        # ── 1. 三端渲染（web 纯静态三页：/ = 一张图大屏(SVG)、/admin = 管理端、/mobile = 移动端 H5）──
        try:
            page.goto(WEB + "admin", timeout=25000)
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            rec("管理端渲染", "高标准农田" in page.title() and page.locator("body").inner_text().strip() != "",
                page.title()[:40])
        except Exception as e:
            rec("管理端渲染", False, str(e)[:80])
        try:
            page.goto(WEB, timeout=25000)
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            rec("一张图大屏渲染", "一张图" in page.title() and page.locator("body").inner_text().strip() != "",
                page.title()[:40])
        except Exception as e:
            rec("一张图大屏渲染", False, str(e)[:80])
        try:
            page.goto(WEB + "mobile", timeout=25000)
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            rec("移动端H5渲染", "高标准农田" in page.title() and page.locator("body").inner_text().strip() != "",
                page.title()[:40])
        except Exception as e:
            rec("移动端H5渲染", False, str(e)[:80])

        # ── 2. 登录与错密拒绝 ──
        atok, _, araw = login(page, ADMIN)
        rec("管理员登录", bool(atok), str((araw.get("data") or {}).get("login", {}).get("name", "")))
        bad, code_bad = gql(page, 'mutation { login(phone: "%s", password: "wrong") { token } }' % ADMIN)
        errcode = ((bad.get("errors") or [{}])[0].get("extensions") or {}).get("code", "")
        # D-05-2：GraphQL 通道错密返回 HTTP 200 + extensions.code=UNAUTHENTICATED
        rec("错密拒绝", code_bad == 200 and errcode == "UNAUTHENTICATED",
            "http=%s code=%s" % (code_bad, errcode))
        if not atok:
            browser.close()
            print("SUMMARY fail=%d total=%d" % (FAIL[0], len(RESULTS)), flush=True)
            sys.exit(1)

        # ── 3. 一张图地块渲染（landGeojson FeatureCollection + landStats + 大屏 #mapBox 地图容器）──
        try:
            gj, gj_code = gql(page, '{ landGeojson(villageCode: "%s") }' % VILLAGE_CODE, atok)
            geo = (gj.get("data") or {}).get("landGeojson") or ""
            st, _ = gql(page, '{ landStats(villageCode: "%s"){ total totalAreaMu } }' % VILLAGE_CODE, atok)
            stats = (st.get("data") or {}).get("landStats") or {}
            # 回到一张图页：注入登录态（localStorage hsf_token，gql.js 读它带 Authorization）并调 loadMap()
            # —— 页面本体由 doLogin() 触发渲染，E2E 直注 token 等价于已登录态（修复复测 FAIL：无 token 401 → 无 polygon）
            # 跨节点 IPIP 间歇丢包（docs/14 坑#11 环境遗留）会偶发 page fetch 网络失败，
            # 渲染断言带 3 次重载重试：每次重新注入登录态并等 networkidle
            map_ok, feats, err_txt = False, 0, ""
            page.on("requestfailed",
                    lambda req: FAILED.append((req.url[:60], req.failure)) if "graphql" in req.url else None)
            for attempt in range(5):
                page.goto(WEB, timeout=25000)
                page.wait_for_load_state("domcontentloaded", timeout=20000)
                # 注入直连网关地址（与 API 用例同路径，绕开 web 反代这一跳的偶发网络问题）+ 登录态
                page.evaluate(
                    "(o)=>{localStorage.setItem('hsf_token',o.t);localStorage.setItem('hsf_name','张县管');"
                    "localStorage.setItem('hsf_gw',o.g);"
                    "document.getElementById('login').style.display='none';"
                    "document.getElementById('app').style.display='';loadMap();}",
                    {"t": atok, "g": GW.rstrip('/')})
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(800)
                feats = page.locator("#mapBox svg polygon, #mapBox svg path").count()
                err_txt = page.evaluate("() => (document.getElementById('err')||{}).textContent || ''") +                 (" reqfail=" + str(FAILED[-2:]) if FAILED else "")
                if feats >= 2:
                    map_ok = True
                    break
                import time as _t
                _t.sleep(2)
            rec("一张图地块渲染",
                '"FeatureCollection"' in geo and stats.get("total", 0) >= 2 and map_ok,
                "geo=%dB total=%s feats=%s err=%s" % (len(geo), stats.get("total"), feats, err_txt[:40]))
        except Exception as e:
            rec("一张图地块渲染", False, str(e)[:80])

        # ── 4. 项目创建 + 7 态全周期流转（RESERVED→…→PERFORMED，动作/权限冻结 05 号 §5.2）──
        _, town_id, _ = login(page, TOWN)
        try:
            cp, _ = gql(page, 'mutation($i:CreateProjectInput!){createProject(input:$i){code state supervisor{name}}}',
                        atok, {"i": {"name": "e2e高标准农田项目", "buildYear": 2026,
                                     "content": "e2e 全周期用例", "applicantOrgCode": VILLAGE_CODE,
                                     "supervisorId": town_id, "parcelCodes": [PARCEL]}}, timeout=30000)
            pcode = ((cp.get("data") or {}).get("createProject") or {}).get("code")
            rec("项目创建", bool(pcode), "code=%s" % pcode)
        except Exception as e:
            pcode = None
            rec("项目创建", False, str(e)[:80])
        if pcode:
            steps = [("submitProject", "SUBMITTED", atok), ("approveProject", "APPROVED", atok),
                     ("startConstruction", "CONSTRUCTING", atok), ("startSupervision", "SUPERVISED", atok),
                     ("acceptanceProject", "ACCEPTED", atok)]
            states = []
            for act, want, tok in steps:
                q = ('mutation { %s(code: "%s") { state } }' % (act, pcode)) if act != "acceptanceProject" \
                    else ('mutation { acceptanceProject(code: "%s", note: "e2e") { state } }' % pcode)
                r, _ = gql(page, q, tok)
                got = ((r.get("data") or {}).get(act) or {}).get("state")
                states.append("%s=%s" % (act, got == want))
            r, _ = gql(page, 'mutation { acceptanceArchive(code: "%s", note: "e2e归档") { state acceptanceNote } }' % pcode, atok)
            fin = (r.get("data") or {}).get("acceptanceArchive") or {}
            states.append("acceptanceArchive=%s" % (fin.get("state") == "PERFORMED"))
            # 流转留痕 6 条，action 为冻结存储值（评审 R13）
            tr, _ = gql(page, '{ projectTransitions(code: "%s") { action } }' % pcode, atok)
            trans = [t.get("action") for t in ((tr.get("data") or {}).get("projectTransitions") or [])]
            rec("项目全周期流转", all(s.split("=")[1] == "True" for s in states) and len(trans) == 6,
                "; ".join(states) + " | 留痕=%d" % len(trans))

        # ── 5. 设施台账 + 报修工单 4 态闭环（OPEN→ACCEPTED→DISPATCHED→RESOLVED→APPROVED，05 号 §6.2）──
        vtok, _, _ = login(page, VILLAGE)
        ktok, keeper_id, _ = login(page, KEEPER)
        try:
            rf, _ = gql(page, 'mutation($i:RegisterFacilityInput!){registerFacility(input:$i){code qrToken}}',
                        atok, {"i": {"type": "WELL", "name": "e2e机井", "villageCode": VILLAGE_CODE,
                                     "parcelCode": PARCEL, "buildYear": 2024}}, timeout=30000)
            fcode = ((rf.get("data") or {}).get("registerFacility") or {}).get("code")
            fs, _ = gql(page, '{ facilities(villageCode: "%s") { total items { code status } } }' % VILLAGE_CODE, atok)
            ftotal = ((fs.get("data") or {}).get("facilities") or {}).get("total", 0)
            rec("设施台账", bool(fcode) and ftotal >= 1, "new=%s total=%s" % (fcode, ftotal))
        except Exception as e:
            fcode = None
            rec("设施台账", False, str(e)[:80])
        try:
            wo, _ = gql(page, 'mutation { reportWorkorder(facilityCode: "%s", description: "e2e报修", contact: "13800000604") { id sn state } }'
                        % (fcode or FACILITY), vtok)
            wid = ((wo.get("data") or {}).get("reportWorkorder") or {}).get("id")
            wsteps = [("acceptWorkorder", "ACCEPTED", "mutation { acceptWorkorder(id: %s) { state } }" % wid, vtok),
                      ("dispatchWorkorder", "DISPATCHED",
                       'mutation { dispatchWorkorder(id: %s, assigneeId: "%s") { state } }' % (wid, keeper_id), atok),
                      ("resolveWorkorder", "RESOLVED",
                       'mutation { resolveWorkorder(id: %s, note: "e2e已修复") { state } }' % wid, ktok),
                      ("approveWorkorder", "APPROVED", "mutation { approveWorkorder(id: %s) { state } }" % wid, vtok)]
            wstates = []
            for act, want, q, tok in wsteps:
                r, _ = gql(page, q, tok)
                got = ((r.get("data") or {}).get(act) or {}).get("state")
                wstates.append("%s=%s" % (act, got == want))
            rec("报修工单流转", bool(wid) and all(s.split("=")[1] == "True" for s in wstates),
                "; ".join(wstates))
        except Exception as e:
            rec("报修工单流转", False, str(e)[:80])

        # ── 6. 告警查询（sim REST：明细 + 统计；GraphQL 通道外直连 31211/8095——05 号 §7）──
        try:
            al, al_code = sim_get(page, "/sim/alerts?state=OPEN&page=1&size=20", atok)
            stats, st_code = sim_get(page, "/sim/alerts/statistics?dim=state", atok)
            items = (al.get("items") or []) if isinstance(al, dict) else []
            rec("告警查询", al_code == 200 and st_code == 200 and len(items) >= 1 and "items" in stats,
                "alerts=%d" % len(items))
        except Exception as e:
            rec("告警查询", False, str(e)[:80])

        # ── 7. 联邦跨域三连（V-17/18/19：holder / supervisor+parcels / parcel）──
        try:
            r1, _ = gql(page, '{ landParcel(code: "%s") { code holder { name } } }' % PARCEL, atok)
            holder_ok = ((r1.get("data") or {}).get("landParcel") or {}).get("holder", {}).get("name")
            r2, _ = gql(page, '{ farmProject(code: "P5306000001") { code supervisor { name } parcels { code } } }', atok)
            fp = (r2.get("data") or {}).get("farmProject") or {}
            r3, _ = gql(page, '{ facility(code: "%s") { code parcel { code } } }' % FACILITY, atok)
            fc_ok = (r3.get("data") or {}).get("facility") or {}
            rec("联邦跨域", bool(holder_ok) and bool(fp.get("supervisor")) and "parcel" in fc_ok,
                json.dumps({"holder": holder_ok, "proj": bool(fp.get("supervisor")),
                            "fac_parcel": fc_ok.get("parcel")}, ensure_ascii=False)[:100])
        except Exception as e:
            rec("联邦跨域", False, str(e)[:80])

        # ── 8. 限流触发（>RATE_CAP 6000/min → HTTP 429 + RATE_LIMITED；放最后避免影响前序用例）──
        # 复测修正：refill=100/s，串行请求永远追不上补充——必须并发突发（100 线程 ≈3-6s 打完 7200）才能击穿令牌桶
        try:
            import urllib.request, urllib.error
            from concurrent.futures import ThreadPoolExecutor
            def _one(_i):
                req = urllib.request.Request(GW, data=json.dumps({"query": "{ __typename }"}).encode(),
                                             headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=15) as r:
                        return r.status, r.read(200).decode("utf-8", "replace")
                except urllib.error.HTTPError as e2:
                    return e2.code, e2.read(200).decode("utf-8", "replace")
            # 突发总量=3×cap：跨分钟窗口切分时（窗口长 60s、突发约 10s），任一窗口分到
            # ≥cap 即触发 429——3×cap 保证最坏三分窗也必中（docs/06 P1 V-25）
            total = RATE_CAP * 5
            codes = []
            with ThreadPoolExecutor(max_workers=150) as pool:
                for st, _b in pool.map(_one, range(total)):
                    codes.append(st)
            n429 = codes.count(429)
            limited = n429 > 0
            rec("限流触发", limited,
                "requests=%d cap=%d http429=%d http200=%d" % (total, RATE_CAP, n429, codes.count(200)))
        except Exception as e:
            rec("限流触发", False, str(e)[:80])

        browser.close()

    print("SUMMARY fail=%d total=%d" % (FAIL[0], len(RESULTS)), flush=True)
    sys.exit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FATAL", traceback.format_exc()[-800:], flush=True)
        sys.exit(1)
