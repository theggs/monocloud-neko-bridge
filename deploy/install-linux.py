#!/usr/bin/env python3
"""Deploy the Linux bridge; the official neko-agent is installed by upstream tooling."""
import argparse
import os
from pathlib import Path
import subprocess

from install_support import (InstallState, agent_command, install_agent, read_private_env,
                             safe_error_code)

REQUIRED = ("NEKO_SERVER", "NEKO_BACKEND_ID", "NEKO_BACKEND_TOKEN", "NEKO_GATEWAY_TYPE",
            "NEKO_GATEWAY_URL", "NEKO_GATEWAY_TOKEN")


def install(home=None, source=None, run=None, repair=False):
    home = Path(home or Path.home())
    source = Path(source or Path(__file__).resolve().parents[1])
    run = run or subprocess.run
    target = home / ".local/lib/monocloud-neko-bridge"
    folder = home / ".config/monocloud-neko-bridge"
    units = home / ".config/systemd/user"
    config = folder / "agent.env"
    values = read_private_env(config)
    if any(not values.get(key) or values[key].startswith("replace-with-") for key in REQUIRED):
        raise ValueError("private_config_not_ready")
    if not values["NEKO_BACKEND_ID"].isdigit() or int(values["NEKO_BACKEND_ID"]) <= 0:
        raise ValueError("invalid_backend_id")
    # Validate the exact runtime used by the published systemd unit.
    try:
        run(["/usr/bin/python3", "-c",
             "import sys; assert sys.version_info >= (3, 9); import http.server, pathlib, socketserver"],
            check=True, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("unsupported_service_python") from None
    try:
        run(["systemd-analyze", "--user", "verify",
             str(source / "deploy/monocloud-neko-bridge.service")],
            check=True, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("invalid_service_unit") from None
    files = {target / "bridge.py": (source / "linux/bridge.py").read_bytes()}
    unit_files = {units / "monocloud-neko-bridge.service":
                  (source / "deploy/monocloud-neko-bridge.service").read_bytes()}
    paths = list(files) + list(unit_files)
    with InstallState(folder, "linux", repair) as state:
        state.prepare({}, paths)
        for path, content in {**files, **unit_files}.items():
            state.publish(path, content)
        state.finish()
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true", help="Resume an installation recorded by this installer")
    parser.add_argument("--install-agent", action="store_true",
                        help="Run the official neko-agent installer after deploying the bridge")
    args = parser.parse_args()
    if os.uname().sysname != "Linux" or os.uname().machine != "x86_64":
        raise SystemExit("Requires Linux amd64")
    try:
        values = install(repair=args.repair)
        home = Path.home()
        if args.install_agent:
            install_agent("linux", values, home)
            print("Bridge installed; the official agent installer has run.")
            print("Next: keep NEKO_AGENT_CONFIG_DIR/STATE_DIR exported, then run")
            print("  nekoagent start backend-" + values["NEKO_BACKEND_ID"])
        else:
            print("Bridge installed; service validated, not started.")
            print("Install the official neko-agent with the command below")
            print("(upstream guide: https://github.com/foru17/neko-master/blob/main/docs/agent/install.md):")
            print()
            print(agent_command("linux", home))
    except Exception as error:
        raise SystemExit("Installation failed (" + safe_error_code(error)
                         + "); keep private install-state.json; resolve conflicts and rerun --repair") from None


if __name__ == "__main__":
    main()
