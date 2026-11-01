"""Audited read-only MonoProxy Dashboard protocol (1.3.0)."""
import base64
import json
import os
from pathlib import Path
import socket
import time

DELIMITER = b"\xdb\xbc"
MAX_FRAME = 16 * 1024 * 1024
# Built-in local IPC constant observed in MonoProxy 1.3.0; not an account secret.
# Override for compatible client versions without changing source.
LOCAL_PASSWORD = os.environ.get("MONOPROXY_LOCAL_PASSWORD") or "eec9454dbc233c716dd4edacea2d46dc"


class Client:
    def __init__(self, path=None, timeout=5):
        self.path = str(path or Path.home() / "MonoProxy/Tmp/db.connect")
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.buffer = b""
        self.seq = 2

    def __enter__(self):
        try:
            self.sock.connect(self.path)
            greeting = self.recv()
            if greeting.get("v") != 1 or greeting.get("c") != 1:
                raise ValueError("unsupported_greeting")
            self.send({"v": 1, "i": 1, "c": 1})
            if self.recv().get("c") != 2:
                raise ValueError("handshake_rejected")
            self.send({"v": 1, "i": 2, "d": {"password": LOCAL_PASSWORD}})
            self.info = self.recv()
            if self.info.get("c") != 1:
                raise ValueError("authentication_failed")
            return self
        except Exception:
            self.sock.close()
            raise

    def __exit__(self, *_):
        self.sock.close()

    def send(self, packet):
        self.sock.sendall(base64.b64encode(json.dumps(packet, separators=(",", ":")).encode()) + DELIMITER)

    def recv(self):
        while DELIMITER not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("dashboard_disconnected")
            self.buffer += chunk
            if len(self.buffer) > MAX_FRAME:
                raise ValueError("frame_limit")
        raw, self.buffer = self.buffer.split(DELIMITER, 1)
        result = json.loads(base64.b64decode(raw, validate=True))
        if not isinstance(result, dict):
            raise ValueError("invalid_packet")
        return result

    def request(self, command, data=None, on_push=None):
        if command not in (7, 14) or (command == 14 and data != {"switch": True}):
            raise ValueError("command_not_allowed")
        self.seq += 1
        packet = {"v": 1, "i": self.seq, "t": command}
        if data is not None:
            packet["d"] = data
        self.send(packet)
        deadline = time.monotonic() + self.timeout
        try:
            while time.monotonic() < deadline:
                self.sock.settimeout(max(.001, deadline - time.monotonic()))
                result = self.recv()
                if result.get("i") == self.seq and result.get("t") == command:
                    if result.get("c") != 1:
                        raise ValueError("command_failed")
                    data = result.get("d")
                    if not isinstance(data, dict):
                        raise ValueError("invalid_response_data")
                    return data
                if on_push:
                    on_push(result)
            raise TimeoutError("response_deadline")
        finally:
            self.sock.settimeout(self.timeout)
