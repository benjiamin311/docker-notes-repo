# -*- coding: utf-8 -*-
"""烟雾测试：不依赖 docker，覆盖渲染、端口解析与 Web 服务的各路由。

运行：python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import docker_notes as dn  # noqa: E402


def get(url, auth=None):
    req = urllib.request.Request(url)
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8")
        finally:
            e.close()


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


if __name__ == "__main__":
    unittest.main()
