#!/usr/bin/env python3
"""Launch the official Agent using systemd's private EnvironmentFile."""
import os
from pathlib import Path

if __name__ == "__main__":
    executable = str(Path(__file__).resolve().parent / "neko-agent")
    args = [executable]
    for name, flag in (("NEKO_SERVER", "--server-url"), ("NEKO_BACKEND_ID", "--backend-id"), ("NEKO_BACKEND_TOKEN", "--backend-token"), ("NEKO_GATEWAY_TYPE", "--gateway-type"), ("NEKO_GATEWAY_URL", "--gateway-url"), ("NEKO_GATEWAY_TOKEN", "--gateway-token")):
        value = os.environ.get(name, "")
        if not value:
            raise SystemExit("Missing required setting: " + name)
        args.extend((flag, value))
    os.execv(executable, args)
