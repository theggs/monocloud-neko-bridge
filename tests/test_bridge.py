import http.client
import importlib.util
import json
from unittest.mock import patch
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler

MODULE = Path(__file__).resolve().parents[1] / "linux/bridge.py"

class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert MODULE.exists(), "Linux relay implementation is missing"
        spec = importlib.util.spec_from_file_location("bridge", MODULE)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + "/api.sock"
        self.payload = {"connections": [{"id": "synthetic-1", "start": "2026-11-01T00:00:00Z", "upload": 123, "download": 456, "rule": "Domain", "rulePayload": "example.com", "chains": ["DIRECT"], "metadata": {"host": "example.com", "destinationIP": "192.0.2.1", "sourceIP": "127.0.0.1", "process": "private", "processPath": "/secret"}, "private": "secret"}]}
        self.calls = []
        self.raw = None
        self.delay = 0
        self.upstream_status = 200
        owner = self
        class Upstream(BaseHTTPRequestHandler):
            def do_GET(self):
                import time
                owner.calls.append(self.path)
                time.sleep(owner.delay)
                body = owner.raw if owner.raw is not None else json.dumps(owner.payload).encode()
                try:
                    self.send_response(owner.upstream_status); self.end_headers(); self.wfile.write(body)
                except BrokenPipeError: pass
            def log_message(self, *args): pass
        self.upstream = socketserver.UnixStreamServer(self.path, Upstream)
        self.ut = threading.Thread(target=self.upstream.serve_forever, daemon=True); self.ut.start()
        self.server = self.module.make_server(self.path, "test-token", port=0, max_bytes=4096, timeout=0.1)
        self.bt = threading.Thread(target=self.server.serve_forever, daemon=True); self.bt.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.bt.join()
        self.upstream.shutdown(); self.upstream.server_close(); self.ut.join(); self.temp.cleanup()

    def request(self, path="/connections", method="GET", token="test-token"):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {} if token is None else {"Authorization": "Bearer " + token}
        c.request(method, path, headers=headers); r = c.getresponse(); result = r.status, json.loads(r.read()); c.close(); return result

    def test_slow_clients_are_bounded_and_expire(self):
        import socket
        import time
        self.server.shutdown(); self.server.server_close(); self.bt.join()
        self.server = self.module.make_server(self.path, "test-token", port=0, max_clients=2, client_timeout=0.2)
        self.bt = threading.Thread(target=self.server.serve_forever, daemon=True); self.bt.start()
        clients = []
        try:
            for _ in range(2):
                c = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
                c.sendall(b"GET /connections HTTP/1.1\r\n")
                clients.append(c)
            time.sleep(0.05)
            with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as extra:
                self.assertEqual(extra.recv(1), b"")
            time.sleep(0.3)
            for c in clients: self.assertEqual(c.recv(1), b"")
            self.assertEqual(self.request()[0], 200)
        finally:
            for c in clients: c.close()

    def test_cumulative_counters_and_field_whitelist(self):
        status, body = self.request()
        self.assertEqual(status, 200)
        row = body["connections"][0]
        self.assertEqual((row["upload"], row["download"]), (123, 456))
        self.assertEqual(row["metadata"]["host"], "example.com")
        self.assertNotIn("private", row)
        self.assertNotIn("process", row["metadata"])
        self.assertNotIn("processPath", row["metadata"])
        self.assertEqual(self.request()[1], body)

    def test_authentication_rejected_before_upstream(self):
        for token in (None, "wrong"):
            self.assertEqual(self.request(token=token)[0], 401)
        self.assertEqual(self.calls, [])

    def test_writes_unknown_paths_and_queries_rejected(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            c = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
            c.request(method, "/connections", headers={"Authorization": "Bearer test-token"})
            r = c.getresponse(); self.assertEqual(r.status, 405); r.read(); c.close()
        for path in ("/configs", "/connections?x=1", "/proxies/foo"):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.calls, [])

    def test_missing_upstream_is_unavailable_and_recovers(self):
        self.server.socket_path = self.path + ".missing"
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.request("/health")[1]["status"], "degraded")
        self.server.socket_path = self.path
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request("/health")[1]["status"], "healthy")

    def test_invalid_oversized_and_slow_upstream(self):
        self.raw = b"not json"; self.assertEqual(self.request()[0], 502)
        self.raw = b"x" * 4097; self.assertEqual(self.request()[0], 502)
        self.raw = None; self.delay = 0.3; self.assertEqual(self.request()[0], 503)

    def test_auxiliary_interfaces_strip_private_fields(self):
        cases = [("/rules", {"rules": [{"type": "MATCH", "payload": "", "proxy": "DIRECT", "secret": "x"}]}, "rules"), ("/proxies", {"proxies": {"DIRECT": {"name": "DIRECT", "type": "Direct", "password": "x"}}}, "proxies"), ("/providers/proxies", {"providers": {"p": {"name": "p", "type": "Inline", "vehicleType": "secret", "proxies": [{"name": "n", "type": "Direct", "password": "x"}]}}}, "providers")]
        for path, payload, key in cases:
            self.payload = payload; status, body = self.request(path)
            self.assertEqual(status, 200); self.assertIn(key, body)
            self.assertNotIn("secret", json.dumps(body)); self.assertNotIn("password", json.dumps(body)); self.assertNotIn("vehicleType", json.dumps(body))

    def test_official_null_connections_is_empty_but_missing_key_fails(self):
        self.payload = {"connections": None}
        self.assertEqual(self.request(), (200, {"connections": []}))
        self.payload = {}
        self.assertEqual(self.request()[0], 502)

    def test_deep_json_returns_502_and_updates_health(self):
        self.raw = b'{"connections":' + b'[' * 1500 + b']' * 1500 + b'}'
        self.assertEqual(self.request()[0], 502)
        self.assertEqual(self.request("/health")[1]["reason"], "invalid_upstream_response")

    def test_decoder_recursion_error_returns_502_and_updates_health(self):
        # CPython versions differ in decoder depth limits; force its documented failure boundary.
        handler = self.module.Handler.__new__(self.module.Handler)
        handler.server = self.server
        handler.path = "/connections"
        handler.headers = {"Authorization": "Bearer test-token"}
        replies = []
        handler.reply = lambda status, body: replies.append((status, body))
        with patch.object(self.module.json, "loads", side_effect=RecursionError):
            handler.do_GET()
        self.assertEqual(replies, [(502, {"error": "invalid_upstream_response"})])
        self.assertEqual(self.server.health["reason"], "invalid_upstream_response")

    def test_optional_connection_field_types_are_checked(self):
        row = self.payload["connections"][0]
        for key, value in (("start", 3), ("chains", "DIRECT"), ("chains", [3]),
                           ("rule", {}), ("rulePayload", [])):
            with self.subTest(key=key, value=value):
                self.payload = {"connections": [dict(row, **{key: value})]}
                self.assertEqual(self.request()[0], 502)
        for key in ("host", "sniffHost", "destinationIP", "sourceIP"):
            with self.subTest(metadata=key):
                bad = dict(row, metadata={key: 7})
                self.payload = {"connections": [bad]}
                self.assertEqual(self.request()[0], 502)
        self.payload = {"connections": [dict(row, chains=None)]}
        self.assertEqual(self.request()[0], 200)

    def test_downstream_timeout_does_not_change_upstream_health(self):
        handler = self.module.Handler.__new__(self.module.Handler)
        handler.server = self.server
        handler.path = "/connections"
        handler.headers = {"Authorization": "Bearer test-token"}
        for raw, status, expected in ((None, 200, "ok"), (b"invalid", 200, "invalid_upstream_response"),
                                      (None, 500, "upstream_http_500")):
            with self.subTest(raw=raw):
                self.raw = raw
                self.upstream_status = status
                with patch.object(handler, "reply", side_effect=TimeoutError):
                    with self.assertRaises(TimeoutError):
                        handler.do_GET()
                self.assertEqual(self.server.health["reason"], expected)

    def test_reply_handles_downstream_header_and_body_timeout(self):
        import io
        handler = self.module.Handler.__new__(self.module.Handler)
        handler.server = self.server
        handler.command = "GET"
        handler.request_version = "HTTP/1.0"
        handler.requestline = "GET /connections HTTP/1.0"
        class SlowWriter(io.BytesIO):
            def write(self, data):
                raise TimeoutError("synthetic slow reader")
        handler.wfile = SlowWriter()
        handler.reply(200, {"connections": []})
        handler.wfile = SlowWriter()
        with patch.object(handler, "end_headers"):
            handler.reply(200, {"connections": []})

    def test_unicode_surrogate_is_returned_as_valid_json(self):
        self.payload["connections"][0]["metadata"]["host"] = "bad\ud800.example"
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body["connections"][0]["metadata"]["host"], "bad\ud800.example")

    def test_output_limit_rejects_before_sending_success_headers(self):
        self.server.max_response_bytes = 128
        status, body = self.request()
        self.assertEqual((status, body), (503, {"error": "response_limit"}))
        self.assertEqual(self.server.health["reason"], "ok")

    def test_backlog_can_hold_configured_client_burst(self):
        server = self.module.make_server(self.path, "test-token", port=0, max_clients=16)
        try:
            self.assertGreaterEqual(server.request_queue_size, 16)
        finally:
            server.server_close()

    def test_auxiliary_nested_values_are_rejected_before_encoding(self):
        for path, payload in (("/rules", {"rules": [{"type": []}]}),
                              ("/proxies", {"proxies": {"DIRECT": {"now": {}}}}),
                              ("/providers/proxies", {"providers": {"p": {"name": [], "proxies": []}}})):
            with self.subTest(path=path):
                self.payload = payload
                self.assertEqual(self.request(path)[0], 502)
                self.assertEqual(self.server.health["reason"], "invalid_upstream_response")

    def test_malformed_shape_fails_instead_of_empty_snapshot(self):
        for payload in ({}, {"connections": ["invalid"]}):
            self.payload = payload; self.assertEqual(self.request()[0], 502)

if __name__ == "__main__": unittest.main()
