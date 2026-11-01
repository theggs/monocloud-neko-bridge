#!/usr/bin/env python3
"""LaunchAgent supervisor: private configuration, graceful stop, bounded logs."""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shlex
import signal
import selectors
import subprocess
import sys
import time


def load_config(path):
    if path.stat().st_mode & 0o077:
        raise ValueError("insecure_config_permissions")
    result = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values = shlex.split(value)
        if len(values) != 1 or not (key.startswith("NEKO_") or key == "MONOPROXY_LOCAL_PASSWORD"):
            raise ValueError("invalid_config_entry")
        result[key] = values[0]
    return result


def run_logged(command, env, path, max_bytes=1048576, stop_timeout=15):
    if stop_timeout <= 0:
        raise ValueError("invalid_stop_timeout")
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger = logging.Logger("managed-child", logging.INFO)
    logger.addHandler(handler)
    process = None
    previous = {}
    selector = selectors.DefaultSelector()
    deadline = None
    buffer = b""
    def send_group(signum):
        if process is None or process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
    def stop(signum, frame):
        nonlocal deadline
        if deadline is None:
            deadline = time.monotonic() + stop_timeout
            send_group(signal.SIGTERM)
    try:
        # Install handlers before spawning: a signal during Popen records a stop
        # deadline, then is forwarded as soon as the child PID is available.
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        selector.register(process.stdout, selectors.EVENT_READ)
        if deadline is not None:
            send_group(signal.SIGTERM)
        while process.poll() is None or selector.get_map():
            if process.poll() is not None and deadline is None and selector.get_map():
                stop(signal.SIGTERM, None)
            if deadline is not None and time.monotonic() >= deadline:
                if process.poll() is not None:
                    # A descendant may retain stdout after its leader exits.
                    # Stop draining rather than signal a potentially reused PGID.
                    break
                send_group(signal.SIGKILL)
            for key, _ in selector.select(timeout=.1):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer += chunk
                while b"\n" in buffer or len(buffer) >= 8192:
                    if b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                    else:
                        line, buffer = buffer[:8192], buffer[8192:]
                    logger.info(line.decode("utf-8", errors="replace").rstrip())
            if not selector.get_map() and process.poll() is None:
                time.sleep(.05)
        if buffer:
            logger.info(buffer.decode("utf-8", errors="replace").rstrip())
        return process.wait()
    finally:
        try:
            selector.close()
            if process is not None:
                # Do not signal a group after a normal, already-reaped exit.
                if process.returncode is None:
                    send_group(signal.SIGTERM)
                    try:
                        process.wait(timeout=stop_timeout)
                    except subprocess.TimeoutExpired:
                        send_group(signal.SIGKILL); process.wait()
                if process.stdout:
                    process.stdout.close()
            handler.close()
        finally:
            for signum, old in previous.items():
                signal.signal(signum, old)


def main():
    role = sys.argv[1]
    if role != "bridge":
        raise ValueError("unknown_role")
    root = Path(__file__).resolve().parent
    cfg = load_config(Path.home() / ".config/monocloud-neko-bridge-mac/agent.env")
    env = os.environ.copy()
    env.update(cfg)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    logs = Path.home() / ".local/state/monocloud-neko-bridge-mac"
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = [sys.executable, "-B", str(root / "adapter.py")]
    return run_logged(command, env, logs / (role + ".log"))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Never print command arguments or configuration values.
        print("service_start_failed: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
