# -*- coding: utf-8 -*-
"""烟雾测试：不依赖 docker，覆盖渲染、端口解析与 Web 服务的各路由。

运行：python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import base64
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import docker_notes as dn  # noqa: E402


def request(url, method="GET", body=None, auth=None, ctype="application/json", extra=None):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None and ctype:
        req.add_header("Content-Type", ctype)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    for k, v in (extra or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8")
        finally:
            e.close()


def get(url, auth=None):
    return request(url, auth=auth)


class TestRender(unittest.TestCase):
    def test_demo_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            dn.render_all(dn.demo_inventory(), d)
            with open(os.path.join(d, "inventory.md"), encoding="utf-8") as f:
                md = f.read()
            with open(os.path.join(d, "inventory.html"), encoding="utf-8") as f:
                doc = f.read()
            self.assertIn("电商后端", md)
            self.assertIn("18080→8080/tcp", md)
            self.assertEqual(doc.count('<section class="card"'), 4)
            for token in ("__HOST__", "__SUBTITLE__", "__CARDS__"):
                self.assertNotIn(token, doc)
            # 未备注的项目要被点名
            self.assertIn("还没写中文备注", doc)

    def test_ports_text(self):
        base = {"ports": [], "note": {}}
        self.assertEqual(dn.ports_text(dict(base)), "-")
        self.assertEqual(
            dn.ports_text({"ports": [{"container": "5432/tcp", "host_ip": "", "host_port": ""}]}),
            "-", "只 expose 未 publish 的端口不应显示")
        self.assertEqual(
            dn.ports_text({"ports": [{"container": "80/tcp", "host_ip": "0.0.0.0", "host_port": "8080"}]}),
            "8080→80/tcp")
        self.assertEqual(
            dn.ports_text({"ports": [{"container": "3306/tcp", "host_ip": "127.0.0.1", "host_port": "13306"}]}),
            "127.0.0.1:13306→3306/tcp")
        self.assertEqual(
            dn.ports_text({"ports": [{"container": "host", "host_ip": "", "host_port": "host"}]}),
            "host 网络")

    def test_parse_path_map(self):
        self.assertEqual(dn.parse_path_map("/opt:/mnt/opt"), [("/opt", "/mnt/opt")])
        self.assertEqual(dn.parse_path_map("/opt:/mnt/opt;/data:/mnt/data"),
                         [("/opt", "/mnt/opt"), ("/data", "/mnt/data")])
        self.assertEqual(dn.remap_path("/opt/shop/docker-compose.yml",
                                       [("/opt", "/mnt/opt")]),
                         "/mnt/opt/shop/docker-compose.yml")
        self.assertEqual(dn.remap_path("/srv/x.yml", [("/opt", "/mnt/opt")]), "/srv/x.yml")

    def test_parse_xnote_regex(self):
        text = ('x-note:\n'
                '  name: "电商后端"\n'
                '  desc: "主链路；含中文标点"\n'
                '  tier: P0\n'
                '\n'
                'services:\n'
                '  api:\n'
                '    image: x\n')
        note = dn.parse_xnote_regex(text)
        self.assertEqual(note["name"], "电商后端")
        self.assertEqual(note["desc"], "主链路；含中文标点")
        self.assertEqual(note["tier"], "P0")


class TestServe(unittest.TestCase):
    def setUp(self):
        self.inv = dn.demo_inventory()
        self.state = dn.State(self.inv)
        self.state.error = "docker 不可用（测试用错误）"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), dn.make_handler(self.state))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, p):
        return "http://127.0.0.1:%d%s" % (self.port, p)

    def test_index_shows_banner_and_cards(self):
        code, body = get(self.url("/"))
        self.assertEqual(code, 200)
        self.assertIn('class="banner show"', body)
        self.assertEqual(body.count('<section class="card"'), 4)

    def test_healthz_reports_failure(self):
        code, body = get(self.url("/healthz"))
        self.assertEqual(code, 503)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("docker", data["error"])

    def test_metrics(self):
        code, body = get(self.url("/metrics"))
        self.assertEqual(code, 200)
        self.assertIn("docker_notes_project_cpu_percent{project=\"shop\",name=\"电商后端\"}", body)
        self.assertIn("docker_notes_collect_success 0", body)

    def test_api_json(self):
        code, body = get(self.url("/api/inventory.json"))
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(body)["projects"]), 4)

    def test_404(self):
        code, _ = get(self.url("/nope"))
        self.assertEqual(code, 404)


class TestServeAuth(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), dn.make_handler(dn.State(dn.demo_inventory()),
                                                                           basic_auth="u:p"))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_auth(self):
        url = "http://127.0.0.1:%d/" % self.port
        code, _ = get(url)
        self.assertEqual(code, 401)
        code, body = get(url, auth="u:p")
        self.assertEqual(code, 200)
        self.assertIn("Docker Compose 项目看板", body)


class TestNoteStore(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "notes.json")
            s = dn.NoteStore(path)
            s.update([{"scope": "project", "key": "shop",
                       "fields": {"name": "电商后端", "tier": "P0"}},
                      {"scope": "service", "key": "shop/api",
                       "fields": {"name": "订单接口"}}])
            self.assertTrue(os.path.isfile(path))
            again = dn.NoteStore(path)
            self.assertEqual(again.data["projects"]["shop"]["name"], "电商后端")
            self.assertEqual(again.data["services"]["shop/api"]["name"], "订单接口")
            # 空值 = 删除该字段；删空后整条记录消失
            again.update([{"scope": "project", "key": "shop", "fields": {"tier": ""}}])
            self.assertNotIn("tier", dn.NoteStore(path).data["projects"]["shop"])

    def test_apply_overrides(self):
        inv = dn.demo_inventory()
        store = dn.NoteStore(None)
        store.data["projects"]["shop"] = {"name": "改过的项目名"}
        store.data["services"]["shop/api"] = {"name": "改过的服务名"}
        store.apply(inv)
        proj = [p for p in inv["projects"] if p["name"] == "shop"][0]
        self.assertEqual(proj["note"]["name"], "改过的项目名")
        self.assertTrue(proj.get("note_override"))
        api = [s for s in proj["services"] if s["service"] == "api"][0]
        self.assertEqual(api["note"]["name"], "改过的服务名")

    def test_rejects_bad_scope(self):
        store = dn.NoteStore(None)
        with self.assertRaises(dn.Fail):
            store.update([{"scope": "nope", "key": "x", "fields": {}}])

    def test_missing_file_is_readable(self):
        store = dn.NoteStore(os.path.join(tempfile.gettempdir(), "nope-not-exist.json"))
        self.assertEqual(store.data, {"projects": {}, "services": {}})

    def test_net_rates_use_previous_sample(self):
        def first_service(inv):
            return inv["projects"][0]["services"][0]

        base = dn.demo_inventory()
        st = dn.State(base)
        now = time.time()
        for p in base["projects"]:
            for s in p["services"]:
                if s.get("net_rx_bytes") is None:
                    continue
                st._prev_net[s["container"]] = (s["net_rx_bytes"] - 10000,
                                                s["net_tx_bytes"] - 5000,
                                                now - 10)
        inv = dn.demo_inventory()
        st.set_ok(inv)
        svc = first_service(inv)
        self.assertAlmostEqual(svc["net_rx_rate"], 1000, delta=120)
        self.assertAlmostEqual(svc["net_tx_rate"], 500, delta=60)
        self.assertIn("net_rx_rate", inv["projects"][0]["totals"])
        # 第一次采样（没有上一次）时不应伪造速率
        fresh = dn.demo_inventory()
        dn.State(fresh).set_ok(fresh)
        self.assertNotIn("net_rx_rate", first_service(fresh))


class TestWriteApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notes = os.path.join(self.tmp.name, "notes.json")
        self.store = dn.NoteStore(self.notes)
        inv = dn.demo_inventory()
        self.store.apply(inv)
        self.state = dn.State(inv, store=self.store)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), dn.make_handler(
            self.state, store=self.store, allow_actions=False, allow_exec=False))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def url(self, p):
        return "http://127.0.0.1:%d%s" % (self.port, p)

    def test_edit_project_note(self):
        code, body = request(self.url("/api/notes"), "POST", {"items": [
            {"scope": "project", "key": "shop",
             "fields": {"name": "电商平台", "tier": "P0", "owner": "平台组"}}]})
        self.assertEqual(code, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertIn("电商平台", payload["html"])
        self.assertIn("data-key=\"shop\"", payload["html"])
        # 页面和磁盘都生效
        code, page = get(self.url("/"))
        self.assertIn("电商平台", page)
        with open(self.notes, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["projects"]["shop"]["name"], "电商平台")

    def test_edit_service_note(self):
        code, body = request(self.url("/api/notes"), "POST", {"items": [
            {"scope": "service", "key": "shop/api", "fields": {"name": "订单接口（新）"}}]})
        self.assertEqual(code, 200)
        self.assertIn("订单接口（新）", json.loads(body)["html"])

    def test_clear_field_falls_back_to_compose(self):
        code, _ = request(self.url("/api/notes"), "POST", {"items": [
            {"scope": "project", "key": "shop", "fields": {"name": "临时名字"}}]})
        self.assertEqual(code, 200)
        code, body = request(self.url("/api/notes"), "POST", {"items": [
            {"scope": "project", "key": "shop", "fields": {"name": ""}}]})
        self.assertEqual(code, 200)
        # 清空覆盖后回落到 compose 里的名字
        self.assertIn("电商后端", json.loads(body)["html"])
        self.assertNotIn("临时名字", json.loads(body)["html"])

    def test_rejects_bad_scope(self):
        code, body = request(self.url("/api/notes"), "POST", {"items": [
            {"scope": "hack", "key": "x", "fields": {"name": "x"}}]})
        self.assertEqual(code, 400)
        self.assertIn("scope", json.loads(body)["error"])

    def test_requires_json_content_type(self):
        code, _ = request(self.url("/api/notes"), "POST", {"items": []}, ctype="text/plain")
        self.assertEqual(code, 415)

    def test_rejects_cross_origin(self):
        code, _ = request(self.url("/api/notes"), "POST", {"items": []},
                          extra={"Origin": "http://evil.example"})
        self.assertEqual(code, 403)

    def test_actions_need_flag(self):
        code, body = request(self.url("/api/action"), "POST",
                             {"container": "shop-api", "action": "restart"})
        self.assertEqual(code, 403)
        self.assertIn("ALLOW_ACTIONS", json.loads(body)["error"])

    def test_exec_needs_flag(self):
        code, body = request(self.url("/api/exec"), "POST",
                             {"container": "shop-api", "cmd": "ls"})
        self.assertEqual(code, 403)
        self.assertIn("ALLOW_EXEC", json.loads(body)["error"])


class TestActionsWithoutAuth(unittest.TestCase):
    """开了 ALLOW_ACTIONS 但没设 BASIC_AUTH 时必须拒绝，否则整网段都能停容器。"""

    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), dn.make_handler(
            dn.State(dn.demo_inventory()), allow_actions=True, allow_exec=True))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_refuse(self):
        url = "http://127.0.0.1:%d/api/action" % self.port
        code, body = request(url, "POST", {"container": "shop-api", "action": "stop"})
        self.assertEqual(code, 403)
        self.assertIn("BASIC_AUTH", json.loads(body)["error"])


class TestCardMarkup(unittest.TestCase):
    def test_attributes_for_filters_and_actions(self):
        doc = dn.render_html(dn.demo_inventory(), {"live": True, "allow_actions": True})
        self.assertIn('data-state="running"', doc)
        self.assertIn('data-state="exited"', doc)
        self.assertIn('data-health="none"', doc)
        self.assertIn('data-tier="P0"', doc)
        self.assertIn('data-tier="none"', doc)
        self.assertIn('class="btn openbtn"', doc)
        self.assertIn('data-act="edit"', doc)
        self.assertIn('data-group="state"', doc)
        self.assertIn('id="chart"', doc)
        self.assertIn('window.__DN = {"live": true', doc)

    def test_open_port_and_host_binding(self):
        p = [x for x in dn.demo_inventory()["projects"] if x["name"] == "shop"][0]
        self.assertEqual(dn._open_target(p), ("18080", ""))
        crm = [x for x in dn.demo_inventory()["projects"] if x["name"] == "legacy-crm"][0]
        self.assertEqual(dn._open_target(crm), ("8000", ""))

    def test_static_export_has_no_live_flag(self):
        doc = dn.render_html(dn.demo_inventory())
        self.assertIn('"live": false', doc)

    def test_project_power_buttons(self):
        doc = dn.render_html(dn.demo_inventory(), {"live": True, "allow_actions": True})
        for act in ("start", "stop", "restart"):
            self.assertIn('data-pact="%s"' % act, doc)
        self.assertNotIn("pwr start off", doc)

    def test_project_power_buttons_dimmed_without_flag(self):
        doc = dn.render_html(dn.demo_inventory(), {"live": True})
        self.assertIn("pwr start off", doc)

    def test_zoom_controls_present(self):
        doc = dn.render_html(dn.demo_inventory())
        self.assertIn('id="zoomout"', doc)
        self.assertIn('id="zoomin"', doc)
        self.assertIn('id="zoomreset"', doc)


class TestProjectActions(unittest.TestCase):
    """项目级启停：鉴权 + 参数校验走真实 HTTP。"""

    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), dn.make_handler(
            dn.State(dn.demo_inventory()), "admin:pw",
            allow_actions=True, allow_exec=True))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def post(self, payload):
        return request("http://127.0.0.1:%d/api/action" % self.port, "POST", payload,
                       auth="admin:pw")

    def test_unknown_project(self):
        code, body = self.post({"project": "nope", "action": "stop"})
        self.assertEqual(code, 404)
        self.assertIn("找不到项目", json.loads(body)["error"])

    def test_bad_action(self):
        code, body = self.post({"project": "shop", "action": "rm -rf"})
        self.assertEqual(code, 400)

    def test_requires_target(self):
        code, body = self.post({"action": "restart"})
        self.assertEqual(code, 400)

    def test_project_action_reports_each_container(self):
        # 本机 / CI 上都没有 shop-api 这些容器，应当是「每个容器都失败」
        code, body = self.post({"project": "shop", "action": "restart"})
        self.assertEqual(code, 500)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual(len(payload["results"]), 3)
        self.assertTrue(payload["error"])


if __name__ == "__main__":
    unittest.main()
