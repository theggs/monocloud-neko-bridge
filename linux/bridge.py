#!/usr/bin/env python3
"""Read-only, loopback-only Mihomo Unix socket gateway for stock neko-agent."""
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import socket
import socketserver
import threading
import time

PATHS = {"/connections", "/rules", "/proxies", "/providers/proxies"}
LOG = logging.getLogger("monocloud-neko-bridge")


def pick(row, keys, string_values=False):
    if not isinstance(row, dict):
        raise ValueError("invalid row")
    result = {k: row[k] for k in keys if k in row}
    if string_values and any(not isinstance(value, str) for value in result.values()):
        raise ValueError("invalid string field")
    return result


def sanitize(path, body):
    if not isinstance(body, dict):
        raise ValueError("invalid object")
    if path == "/connections":
        rows = body["connections"]
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise ValueError("invalid connections")
        result = []
        for row in rows:
            clean = pick(row, ("id", "start", "upload", "download", "rule", "rulePayload", "chains"))
            if not isinstance(clean.get("id"), str) or not clean["id"]:
                raise ValueError("invalid connection id")
            for key in ("upload", "download"):
                value = clean.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError("invalid counter")
            for key in ("start", "rule", "rulePayload"):
                if key in clean and not isinstance(clean[key], str):
                    raise ValueError("invalid connection string")
            chains = clean.get("chains")
            if chains is not None and (not isinstance(chains, list) or
                                       any(not isinstance(value, str) for value in chains)):
                raise ValueError("invalid connection chains")
            clean["metadata"] = pick(row["metadata"], ("host", "sniffHost", "destinationIP", "sourceIP"))
            if any(not isinstance(value, str) for value in clean["metadata"].values()):
                raise ValueError("invalid connection metadata")
            result.append(clean)
        return {"connections": result}
    if path == "/rules":
        if not isinstance(body["rules"], list):
            raise ValueError("invalid rules")
        return {"rules": [pick(row, ("type", "payload", "proxy"), string_values=True) for row in body["rules"]]}
    if path == "/proxies":
        return {"proxies": {k: pick(v, ("name", "type", "now"), string_values=True) for k, v in body["proxies"].items()}}
    result = {}
    for k, row in body["providers"].items():
        clean = pick(row, ("name", "type"), string_values=True)
        clean["proxies"] = [pick(v, ("name", "type", "now"), string_values=True) for v in row["proxies"]]
        result[k] = clean
    return {"providers": result}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, max_clients=8, **kwargs):
        self.request_queue_size = max(self.request_queue_size, max_clients)
        super().__init__(*args, **kwargs)

    def server_bind(self):
        # HTTPServer.server_bind() resolves the bound address with socket.getfqdn(),
        # which performs a reverse DNS lookup. macOS can attribute that lookup to
        # local network access and prompt for permission, so bind through TCPServer
        # and use the literal loopback address instead of a resolved hostname.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def get_request(self):
        client, address = super().get_request()
        client.settimeout(self.client_timeout)
        return client, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def transition(self, status, reason):
        with self.state_lock:
            if self.health["status"] != status or self.health.get("reason") != reason:
                LOG.warning(json.dumps({"event": "upstream_state", "status": status, "reason": reason}))
            self.health = {"status": status, "reason": reason, "checked_at": int(time.time())}


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 closes every connection, avoiding unread bodies on rejected methods.
    def log_message(self, *args):
        pass

    def reply(self, status, data, max_response_bytes=None):
        if max_response_bytes is None:
            max_response_bytes = getattr(self.server, "max_response_bytes", 16777216)
        body = bytearray()
        encoder = json.JSONEncoder(ensure_ascii=True, allow_nan=False)
        for chunk in encoder.iterencode(data):
            encoded = chunk.encode()
            if len(body) + len(encoded) > max_response_bytes:
                LOG.warning("response_limit")
                status = 503
                body = b'{"error": "response_limit"}'
                break
            body.extend(encoded)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            LOG.debug("client_disconnected")

    def authorized(self):
        value = self.headers.get("Authorization", "")
        return hmac.compare_digest(value.encode(), ("Bearer " + self.server.token).encode())

    def do_GET(self):
        if not self.authorized():
            self.reply(401, {"error": "unauthorized"}); return
        if self.path == "/health":
            with self.server.state_lock:
                state = dict(self.server.health)
            self.reply(200 if state["status"] == "healthy" else 503, state); return
        if self.path not in PATHS:
            self.reply(404, {"error": "path_not_allowed"}); return
        conn = UnixHTTPConnection(self.server.socket_path, self.server.upstream_timeout)
        try:
            conn.request("GET", self.path, headers={"Accept": "application/json"})
            response = conn.getresponse()
            raw = response.read(self.server.max_bytes + 1)
            if response.status != 200:
                self.server.transition("degraded", "upstream_http_" + str(response.status))
                status, body = 502, {"error": "upstream_http_error"}
            else:
                if len(raw) > self.server.max_bytes:
                    raise ValueError("response_limit")
                body = sanitize(self.path, json.loads(raw))
                self.server.transition("healthy", "ok")
                status = 200
        except (OSError, http.client.HTTPException):
            self.server.transition("degraded", "upstream_unavailable")
            status, body = 503, {"error": "upstream_unavailable"}
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            self.server.transition("degraded", "invalid_upstream_response")
            status, body = 502, {"error": "invalid_upstream_response"}
        finally:
            conn.close()
        self.reply(status, body)

    def reject_write(self):
        self.reply(405, {"error": "method_not_allowed"})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = reject_write


def make_server(socket_path, token, port=19092, max_bytes=16777216, timeout=5, max_clients=8, client_timeout=5,
                max_response_bytes=16777216):
    if not token or min(max_bytes, timeout, max_clients, client_timeout) <= 0 or max_response_bytes < 64:
        raise ValueError("nonempty token, positive limits and response limit >= 64 required")
    server = BridgeServer(("127.0.0.1", port), Handler, max_clients=max_clients)
    server.socket_path = socket_path
    server.token = token
    server.max_response_bytes = max_response_bytes
    server.max_bytes = max_bytes
    server.upstream_timeout = timeout
    server.client_timeout = client_timeout
    server.slots = threading.BoundedSemaphore(max_clients)
    server.state_lock = threading.Lock()
    server.health = {"status": "starting", "reason": "awaiting_first_request"}
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    server = make_server(os.path.expanduser("~/.Monocloud/monocloud-core-api.sock"), os.environ["NEKO_GATEWAY_TOKEN"])
    LOG.info(json.dumps({"event": "listening", "address": "127.0.0.1", "port": server.server_port}))
    notify = os.environ.get("NOTIFY_SOCKET")
    if notify:
        address = "\0" + notify[1:] if notify.startswith("@") else notify
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.connect(address)
            notifier.sendall(b"READY=1")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
