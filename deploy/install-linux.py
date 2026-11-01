#!/usr/bin/env python3
"""Stage and validate a Linux installation; --repair resumes only owned files."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

from install_support import InstallState, VERSION, fetch_release, read_private_env, safe_error_code

ASSET = "neko-agent_agent-v1.4.8_linux_amd64.tar.gz"


def install(home=None, source=None, fetch=None, run=None, repair=False):
    home = Path(home or Path.home())
    source = Path(source or Path(__file__).resolve().parents[1])
    fetch, run = fetch or fetch_release, run or subprocess.run
    target = home / ".local/lib/monocloud-neko-bridge"
    folder = home / ".config/monocloud-neko-bridge"
    units = home / ".config/systemd/user"
    config = folder / "agent.env"
    values = read_private_env(config)
    required = ("NEKO_SERVER", "NEKO_BACKEND_ID", "NEKO_BACKEND_TOKEN", "NEKO_GATEWAY_TYPE", "NEKO_GATEWAY_URL", "NEKO_GATEWAY_TOKEN")
    if any(not values.get(key) or values[key].startswith("replace-with-") for key in required):
        raise ValueError("private_config_not_ready")
    if not values["NEKO_BACKEND_ID"].isdigit() or int(values["NEKO_BACKEND_ID"]) <= 0:
        raise ValueError("invalid_backend_id")
    # Validate the exact runtime used by both published systemd units.
    try:
        run(["/usr/bin/python3", "-c", "import sys; assert sys.version_info >= (3, 9); import http.server, pathlib, socketserver"],
            check=True, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("unsupported_service_python") from None
    files = {target / name: (source / "linux" / name).read_bytes() for name in ("bridge.py", "run_agent.py")}
    unit_files = {units / ("monocloud-neko-" + role + ".service"): (source / "deploy" / ("monocloud-neko-" + role + ".service")).read_bytes() for role in ("bridge", "agent")}
    paths = list(files) + list(unit_files) + [target / "neko-agent", target / "release.txt"]
    with InstallState(folder, "linux", repair) as state:
        state.prepare({}, paths)
        binary, digest = fetch(ASSET)
        with tempfile.TemporaryDirectory(prefix="neko-linux-stage-") as temporary:
            staging = Path(temporary)
            executable = staging / "neko-agent"
            executable.write_bytes(binary); executable.chmod(0o700)
            result = run([str(executable), "--version"], check=True, capture_output=True, text=True, timeout=30)
            if result.stdout.strip() not in ("1.4.8", VERSION):
                raise ValueError("unexpected_agent_version")
            staged_units = []
            for path, content in unit_files.items():
                p = staging / path.name; p.write_bytes(content); staged_units.append(str(p))
            run(["systemd-analyze", "--user", "verify", *staged_units], check=True, capture_output=True, text=True, timeout=30)
        for path, content in files.items(): state.publish(path, content)
        state.publish(target / "neko-agent", binary, 0o755)
        for path, content in unit_files.items(): state.publish(path, content)
        state.publish(target / "release.txt", ("Agent=" + VERSION + "\nAssetSHA256=" + digest + "\n").encode())
        state.finish()
    return {"version": VERSION, "sha256": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true", help="Resume an installation recorded by this installer")
    args = parser.parse_args()
    if os.uname().sysname != "Linux" or os.uname().machine != "x86_64":
        raise SystemExit("Requires Linux amd64")
    try:
        result = install(repair=args.repair)
    except Exception as error:
        raise SystemExit("Installation failed (" + safe_error_code(error) + "); keep private install-state.json; resolve conflicts and rerun --repair") from None
    print("Installed", result["version"], "SHA256", result["sha256"], "; services validated, not started")


if __name__ == "__main__": main()
