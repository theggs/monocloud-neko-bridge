#!/usr/bin/env python3
"""MonoProxy event stream to read-only Clash-shaped snapshots; no process data."""
from datetime import datetime, timezone
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import socket
import threading
import time

import bridge as http_base
from protocol import Client

LOG = logging.getLogger("monocloud-mac")
# Engine timestamps are Unix seconds plus bounded relative seconds, never ms.
# Fixed bounds keep validation independent of local wall-clock skew.
MAX_RELATIVE_SECONDS = 10 * 366 * 86400
TIME_FAILURES = {"invalid_engine_epoch", "invalid_start", "invalid_close"}


class Unavailable(Exception):
    pass


def ip(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return ""


def text(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("invalid_text")
    return value


def convert(raw, epoch):
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("invalid_connection")
    start = raw["t_start"]
    if not isinstance(start, (int, float)) or isinstance(start, bool) or not 0 <= start <= MAX_RELATIVE_SECONDS:
        raise ValueError("invalid_start")
    identity = raw["id"]
    if not isinstance(identity, (int, str)) or not str(identity) or len(str(identity)) > 128:
        raise ValueError("invalid_id")
    upload, download = raw["tx"], raw["rx"]
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in (upload, download)):
        raise ValueError("invalid_counter")
    chains = []
    for key in ("selectedAdapterName", "selectedPolicyName", "policyName"):
        value = text(raw.get(key, "")).strip()
        if value and value not in chains:
            chains.append(value)
    rule = text(raw.get("matchedRuleType", "")).strip() or "UNKNOWN"
    payload = text(raw.get("matchedRuleString", "")).strip()
    # Some versions include CSV rule type; preserve only its matching payload.
    parts = payload.split(",")
    if len(parts) >= 2 and parts[0].strip().upper() == rule.upper():
        payload = parts[1].strip()
    result = {
        "id": f"mp:{epoch:.6f}:{identity}:{start:.9f}",
        "start": datetime.fromtimestamp(epoch + start, timezone.utc).isoformat().replace("+00:00", "Z"),
        "upload": upload, "download": download,
        "rule": rule, "rulePayload": payload, "chains": chains or ["UNKNOWN"],
        "metadata": {"host": text(raw.get("host", "")), "destinationIP": ip(text(raw.get("ipAddr", ""))), "sourceIP": ip(text(raw.get("fromHost", "")))},
    }
    close = raw.get("t_close", -1)
    if not isinstance(close, (int, float)) or isinstance(close, bool) or (isinstance(close, float) and not math.isfinite(close)) or close > MAX_RELATIVE_SECONDS or (close > 0 and close < start):
        raise ValueError("invalid_close")
    return result, close > 0


class FlowCache:
    def __init__(self, retention=120, max_entries=10000, clock=time.monotonic):
        self.retention = retention
        self.max_entries = max_entries
        self.clock = clock
        self.lock = threading.RLock()
        self.epoch = None
        self.rows = {}
        self.confirmed_closed = set()
        self.ready = False
        self.reason = "starting"
        self.verified = None
        self.stale_after = 30

    def unavailable(self, reason):
        with self.lock:
            if self.ready or reason != self.reason:
                LOG.warning(json.dumps({"event": "collector_state", "status": "degraded", "reason": reason}))
            self.ready = False
            self.reason = reason

    def begin_session(self, epoch):
        if not isinstance(epoch, (int, float)) or isinstance(epoch, bool) or not 1e9 <= epoch < 1e10:
            raise ValueError("invalid_engine_epoch")
        with self.lock:
            if epoch != self.epoch:
                self.rows.clear()
                self.confirmed_closed.clear()
            self.epoch = epoch
            self.ready = False
            self.reason = "syncing"

    def prune(self):
        now = self.clock()
        for key in list(self.rows):
            _, closed = self.rows[key]
            if closed is not None and now - closed >= self.retention:
                del self.rows[key]
                self.confirmed_closed.discard(key)

    def insert(self, flow, closed):
        old = self.rows.get(flow["id"])
        if old:
            if flow["id"] in self.confirmed_closed and not closed:
                return
            if flow["upload"] < old[0]["upload"] or flow["download"] < old[0]["download"]:
                self.unavailable("counter_regressed")
                raise Unavailable("counter_regressed")
            # Later event/snapshot metadata can be sparse. Empty/UNKNOWN means
            # no new information; explicit new values supersede older values.
            previous = old[0]
            flow["metadata"] = {key: value or previous["metadata"][key]
                                for key, value in flow["metadata"].items()}
            if flow["chains"] == ["UNKNOWN"]:
                flow["chains"] = previous["chains"]
            if flow["rule"] == "UNKNOWN":
                flow["rule"] = previous["rule"]
                flow["rulePayload"] = flow["rulePayload"] or previous["rulePayload"]
            elif flow["rule"] == previous["rule"] and not flow["rulePayload"]:
                flow["rulePayload"] = previous["rulePayload"]
        if not old and len(self.rows) >= self.max_entries:
            self.unavailable("cache_capacity")
            raise Unavailable("cache_capacity")
        terminal = (old[1] if old and flow["id"] in self.confirmed_closed else self.clock()) if closed else None
        if closed:
            self.confirmed_closed.add(flow["id"])
        self.rows[flow["id"]] = (flow, terminal)

    def update(self, raw):
        with self.lock:
            self.prune()
            flow, closed = convert(raw, self.epoch)
            self.insert(flow, closed)

    def snapshot(self, rows, has_more):
        with self.lock:
            if not isinstance(has_more, bool):
                raise ValueError("invalid_pagination_flag")
            if has_more:
                self.unavailable("partial_snapshot")
                raise Unavailable("partial_snapshot")
            if not isinstance(rows, list):
                raise ValueError("invalid_snapshot")
            converted = [convert(raw, self.epoch) for raw in rows]
            self.prune()
            active_ids = {flow["id"] for flow, _ in converted}
            for key, (flow, terminal) in list(self.rows.items()):
                if key not in active_ids and terminal is None:
                    self.rows[key] = (flow, self.clock())
            for flow, closed in converted:
                self.insert(flow, closed)
            self.verified = self.clock()
            if not self.ready:
                LOG.info(json.dumps({"event": "collector_state", "status": "healthy", "reason": "snapshot_complete"}))
            self.ready = True
            self.reason = "ok"

    def health(self):
        with self.lock:
            age = None if self.verified is None else self.clock() - self.verified
            good = self.ready and age is not None and age <= self.stale_after
            return {"status": "healthy" if good else "degraded", "reason": "snapshot_stale" if self.ready and age is not None and age > self.stale_after else self.reason, "cached_connections": len(self.rows), "snapshot_age_seconds": None if age is None else round(age, 2)}

    def payload(self, path):
        with self.lock:
            if self.health()["status"] != "healthy":
                raise Unavailable("collector_unavailable")
            self.prune()
            # Current connections first, terminal cache last.
            flows = [flow for flow, terminal in sorted(self.rows.values(), key=lambda v: v[1] is not None)
                     if flow["id"] in self.confirmed_closed or ((flow["metadata"]["host"] or flow["metadata"]["destinationIP"])
                                                 and flow["chains"] != ["UNKNOWN"] and flow["rule"] != "UNKNOWN")]
            if path == "/connections":
                return {"connections": flows}
            proxies = {}
            rules = {}
            for flow in flows:
                for name in flow["chains"]:
                    proxies[name] = {"name": name, "type": {"DIRECT": "Direct", "REJECT": "Reject"}.get(name, "Unknown")}
                key = (flow["rule"], flow["rulePayload"], flow["chains"][-1])
                rules[key] = {"type": key[0], "payload": key[1], "proxy": key[2]}
            if path == "/rules":
                return {"rules": list(rules.values()), "_partial": True, "_source": "observed-connections"}
            if path == "/proxies":
                return {"proxies": proxies, "_partial": True, "_source": "observed-connections"}
            if path == "/providers/proxies":
                return {"providers": {}, "_partial": True}
            raise ValueError("unsupported_path")


class Collector(threading.Thread):
    def __init__(self, cache, path=None, interval=10, retry=1):
        super().__init__(daemon=True)
        self.cache = cache
        # Allow three full synchronization intervals before declaring stale.
        self.cache.stale_after = max(30, 3 * interval)
        self.path = path
        self.interval = interval
        self.retry = retry
        self.stop_event = threading.Event()

    def handle(self, packet):
        command = packet.get("t")
        if command not in (51, 53):
            return
        data = packet.get("d")
        if not isinstance(data, dict):
            raise ValueError("invalid_push_data")
        if command == 51:
            if "tunnel_info" not in data:
                raise ValueError("missing_tunnel")
            self.cache.update(data["tunnel_info"])
        else:
            rows = data.get("tunnels")
            if not isinstance(rows, list):
                raise ValueError("invalid_push_snapshot")
            if data.get("reset") is True:
                self.cache.snapshot(rows, data.get("has_more"))
            else:
                # Non-reset notifications may be incremental; never retire others.
                for row in rows:
                    self.cache.update(row)

    def run(self):
        failures = 0
        while not self.stop_event.is_set():
            try:
                with Client(self.path) as client:
                    status = client.info.get("d")
                    if not isinstance(status, dict):
                        raise ValueError("invalid_status")
                    self.cache.begin_session(status.get("service_start_time"))
                    client.request(14, {"switch": True}, on_push=self.handle)
                    page = client.request(7, on_push=self.handle)
                    self.cache.snapshot(page["tunnels"], page["has_more"])
                    failures = 0
                    last_sync = time.monotonic()
                    while not self.stop_event.is_set():
                        if time.monotonic() - last_sync >= self.interval:
                            page = client.request(7, on_push=self.handle)
                            self.cache.snapshot(page["tunnels"], page["has_more"])
                            last_sync = time.monotonic()
                        client.sock.settimeout(min(1, self.interval))
                        try:
                            self.handle(client.recv())
                        except socket.timeout:
                            continue
            except Exception as error:
                # Unexpected parser/runtime exceptions must also leave the
                # collector alive, explicitly degraded, and able to reconnect.
                allowed = {"partial_snapshot", "cache_capacity", "counter_regressed"}
                if isinstance(error, Unavailable) and str(error) in allowed:
                    reason = str(error)
                elif isinstance(error, ValueError) and str(error) in TIME_FAILURES:
                    reason = str(error)
                elif isinstance(error, (OSError, ValueError, EOFError, KeyError, TypeError, OverflowError, AttributeError)):
                    reason = "dashboard_unavailable"
                else:
                    reason = "collector_internal_error"
                LOG.warning(json.dumps({"event": "collector_error", "error": type(error).__name__, "reason": reason}))
                self.cache.unavailable(reason)
                failures += 1
                self.stop_event.wait(min(30, self.retry * 2 ** min(failures - 1, 5)))


class MacHandler(http_base.Handler):
    def do_GET(self):
        if not self.authorized():
            self.reply(401, {"error": "unauthorized"}); return
        if self.path == "/health":
            state = self.server.cache.health()
            self.reply(200 if state["status"] == "healthy" else 503, state); return
        if self.path not in http_base.PATHS:
            self.reply(404, {"error": "path_not_allowed"}); return
        try:
            self.reply(200, self.server.cache.payload(self.path))
        except Unavailable:
            self.reply(503, {"error": "collector_unavailable"})


def make_server(cache, token, port=19093, max_response_bytes=16 * 1024 * 1024):
    server = http_base.make_server("unused", token, port=port, max_response_bytes=max_response_bytes)
    server.RequestHandlerClass = MacHandler
    server.cache = cache
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cache = FlowCache()
    collector = Collector(cache)
    server = make_server(cache, os.environ["NEKO_GATEWAY_TOKEN"])
    collector.start()
    LOG.info(json.dumps({"event": "listening", "address": "127.0.0.1", "port": server.server_port}))
    try:
        server.serve_forever()
    finally:
        collector.stop_event.set()
        server.server_close()
        collector.join(timeout=6)


if __name__ == "__main__":
    main()
