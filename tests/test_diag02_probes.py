"""DIAG-02: различение отказов пробами — юнит-проверки без внешней сети.

Поднимает локальный HTTP-сервер и проверяет, что пять разных состояний
не схлопываются в одно «недоступно»: закрытый порт, таймаут, 401, 5xx,
200 без JSON, JSON без нужной модели; плюс CLI переместился/пропал,
TCP-порт, дедупликация и ограничитель параллельности.

Запуск в образе хаба:

    docker cp tests/test_diag02_probes.py aihub-hub-test:/tmp/t2.py
    docker exec aihub-hub-test python /tmp/t2.py
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/app")

from app import observations, probes  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="application/json"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/ok":
            self._send(200, json.dumps({"data": [{"id": "model-a"}]}))
        elif self.path == "/no-model":
            self._send(200, json.dumps({"data": [{"id": "model-b"}]}))
        elif self.path == "/html":
            self._send(200, "<html>hello</html>", "text/html")
        elif self.path == "/auth":
            self._send(401, json.dumps({"detail": "invalid token"}))
        elif self.path == "/boom":
            self._send(503, json.dumps({"detail": "down"}))
        elif self.path == "/slow":
            time.sleep(3)
            self._send(200, json.dumps({"data": []}))
        else:
            self._send(404, json.dumps({"detail": "not found"}))


def closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class HttpProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def url(self, path):
        return "http://127.0.0.1:" + str(self.port) + path

    def test_ok_with_required_value(self):
        spec = {"url": self.url("/ok"), "expect": {"status": 200, "require_equals": {"data[].id": "model-a"}}}
        result = probes.probe_http_json(spec, 3)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["detail"]["http_status"], 200)

    def test_model_missing_is_catalog_not_connection(self):
        spec = {"url": self.url("/no-model"), "expect": {"status": 200, "require_equals": {"data[].id": "model-a"}}}
        result = probes.probe_http_json(spec, 3)
        self.assertEqual(result["state"], "failed")
        self.assertIn("нет значения", result["reason"])
        self.assertNotIn("соединени", result["reason"])

    def test_200_without_json_is_body(self):
        spec = {"url": self.url("/html"), "expect": {"status": 200, "json": True}}
        result = probes.probe_http_json(spec, 3)
        self.assertEqual(result["state"], "failed")
        self.assertIn("не JSON", result["reason"])
        self.assertIn("text/html", result["reason"])

    def test_auth_is_separate(self):
        result = probes.probe_http_json({"url": self.url("/auth")}, 3)
        self.assertEqual(result["state"], "failed")
        self.assertIn("авторизация", result["reason"])
        self.assertEqual(result["detail"]["http_status"], 401)

    def test_server_error(self):
        result = probes.probe_http_json({"url": self.url("/boom")}, 3)
        self.assertIn("HTTP 503", result["reason"])

    def test_timeout(self):
        result = probes.probe_http_json({"url": self.url("/slow")}, 0.5)
        self.assertEqual(result["state"], "failed")
        self.assertIn("таймаут", result["reason"])

    def test_closed_port_is_connection(self):
        result = probes.probe_http_json({"url": "http://127.0.0.1:" + str(closed_port()) + "/x"}, 1)
        self.assertEqual(result["state"], "failed")
        self.assertIn("порт закрыт", result["reason"])

    def test_protocol_mismatch_is_reported(self):
        spec = {"url": self.url("/ok"), "expect": {"status": 200, "require_keys": ["Browser"]}}
        result = probes.probe_http_json(spec, 3)
        self.assertEqual(result["state"], "failed")
        self.assertIn("Browser", result["reason"])


class CliProbe(unittest.TestCase):
    def test_ok_when_registered_path_matches(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            path = fh.name
        try:
            result = probes.probe_cli_binary({"binary": "codex", "path": path}, 2)
            self.assertEqual(result["state"], "ok")
        finally:
            os.unlink(path)

    def test_moved_cli_is_degraded_with_both_paths(self):
        directory = tempfile.mkdtemp()
        found = os.path.join(directory, "codex-test-bin")
        open(found, "w").close()
        registered = os.path.join(directory, "old", "codex-test-bin")
        result = probes.probe_cli_binary({"binary": "codex-test-bin", "path": registered,
                                          "search": [os.path.join(directory, "*")]}, 2)
        self.assertEqual(result["state"], "degraded")
        self.assertIn("переместился", result["reason"])
        self.assertIn(found, result["detail"]["found_path"])

    def test_missing_cli(self):
        result = probes.probe_cli_binary({"binary": "no-such-cli-xyz", "path": "/nowhere/no-such-cli-xyz"}, 2)
        self.assertEqual(result["state"], "failed")
        self.assertIn("не найден", result["reason"])


class TcpProbe(unittest.TestCase):
    def test_open_and_closed(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            self.assertEqual(probes.probe_tcp_port({"host": "127.0.0.1", "port": port}, 2)["state"], "ok")
        finally:
            server.close()
        closed = probes.probe_tcp_port({"host": "127.0.0.1", "port": closed_port()}, 1)
        self.assertEqual(closed["state"], "failed")
        self.assertIn("закрыт", closed["reason"])


class Plan(unittest.TestCase):
    def test_plan_dedup_key_and_skip_without_probe(self):
        agents = [
            {"id": "a", "probe": {"kind": "http_json", "url": "http://h:1/x?q=1"}},
            {"id": "b", "probe": {"kind": "http_json", "url": "http://h:1/x?q=2"}},
            {"id": "c"},
            {"id": "d", "probe": {"kind": "tcp_port", "host": "h", "port": 2}},
        ]
        items = probes.plan(agents)
        self.assertEqual([i["agent_id"] for i in items], ["a", "b", "d"])
        self.assertEqual(items[0]["dedup_key"], items[1]["dedup_key"])
        self.assertEqual(items[0]["dedup_key"], "http_json|http://h:1/x")


class Run(unittest.TestCase):
    def setUp(self):
        self.recorded = []
        self._record = observations.record
        self._latest = observations.latest
        observations.record = lambda *a, **k: self.recorded.append((a, k))
        observations.latest = lambda: {}

    def tearDown(self):
        observations.record = self._record
        observations.latest = self._latest

    def test_dedup_and_reuse(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:" + str(port) + "/ok"
        agents = [{"id": "a", "probe": {"kind": "http_json", "url": url}},
                  {"id": "b", "probe": {"kind": "http_json", "url": url}}]
        try:
            first = probes.run(agents)
            self.assertEqual(first["ran"], 1)
            self.assertEqual(first["deduplicated"], 1)
            self.assertEqual(len(first["results"]), 2)
            self.assertTrue(all(r["state"] == "ok" for r in first["results"]))
            self.assertEqual(len(self.recorded), 2)
            self.assertEqual(probes.last()["ran"], 1)
            observations.latest = lambda: {
                "a": {"availability": {"state": "ok", "checked_at": probes._now().isoformat(), "ttl_s": 180,
                                       "reason": None, "detail": {}, "source": "probe:http_json"}}}
            second = probes.run(agents)
            self.assertEqual(second["reused"], 1)
            self.assertEqual(second["ran"], 1)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
