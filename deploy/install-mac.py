#!/usr/bin/env python3
"""Create an isolated Mac installation; validate jobs without starting them."""
import argparse
import ipaddress
import uuid
import json
import os
from pathlib import Path
import platform
import plistlib
import secrets
import shutil
import subprocess
import tempfile
import urllib.request
from urllib.parse import urlsplit

from install_support import InstallState, VERSION, encode_env, fetch_release, safe_error_code

BASE = "https://github.com/foru17/neko-master/releases/download/agent-v1.4.8/"
ASSET = "neko-agent_agent-v1.4.8_darwin_arm64.tar.gz"
class NoManagementRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # Do not forward management credentials, even to another path/origin.
        return None


def validate_master_url(master, allow_insecure_http=False):
    parsed = urlsplit(master)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid_master_url")
    _ = parsed.port  # Reject malformed ports before any install journal or API request.
    if any(character.isspace() or ord(character) < 32 for character in master):
        raise ValueError("invalid_master_url")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    if parsed.scheme == "http" and not loopback and not allow_insecure_http:
        raise ValueError("remote_http_requires_explicit_allow_insecure_http")
    return master.rstrip("/")


def build_api(master, token_file=None, allow_insecure_http=False):
    master = validate_master_url(master, allow_insecure_http)
    headers = {"Content-Type": "application/json"}
    if token_file is not None:
        token_file = Path(token_file).expanduser()
        if token_file.stat().st_mode & 0o077:
            raise ValueError("insecure_management_token_permissions")
        token = token_file.read_text().strip()
        if not token or "\n" in token or "\r" in token:
            raise ValueError("invalid_management_token")
        headers["Authorization"] = "Bearer " + token
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoManagementRedirects())
    def api(path, data=None):
        request = urllib.request.Request(master + path,
            data=None if data is None else json.dumps(data).encode(), headers=headers)
        with direct.open(request, timeout=15) as response:
            return json.load(response)
    return api


def install(args, home=None, source=None, api=None, fetch=None, run=None):
    home = Path(home or Path.home())
    source = Path(source or Path(__file__).resolve().parents[1])
    fetch, run = fetch or fetch_release, run or subprocess.run
    master = validate_master_url(args.master_url, args.allow_insecure_http)
    api = api or build_api(master, args.admin_token_file, args.allow_insecure_http)
    name = args.backend_name.strip()
    if not name or len(name) > 200 or any(ord(c) < 32 for c in name):
        raise ValueError("invalid_backend_name")
    target = home / ".local/lib/monocloud-neko-bridge-mac"
    folder = home / ".config/monocloud-neko-bridge-mac"
    config = folder / "agent.env"
    logs = home / ".local/state/monocloud-neko-bridge-mac"
    launch = home / "Library/LaunchAgents"
    python = Path(args.python or "").expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("persistent_python_is_not_executable")
    run([str(python), "-c", "import sys; assert sys.version_info >= (3, 9)"], check=True, capture_output=True, text=True, timeout=30)
    files = {target / file: (source / "mac" / file).read_bytes() for file in ("adapter.py", "protocol.py", "launcher.py")}
    files[target / "bridge.py"] = (source / "linux/bridge.py").read_bytes()
    plists = {}
    for role in ("bridge", "agent"):
        label = "io.monocloud-neko." + role
        data = {"Label": label, "ProgramArguments": [str(python), "-B", str(target / "launcher.py"), role],
                "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10, "ExitTimeOut": 30,
                "ProcessType": "Background", "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
                "StandardOutPath": str(logs / (role + "-launch.log")),
                "StandardErrorPath": str(logs / (role + "-launch.log"))}
        plists[launch / (label + ".plist")] = plistlib.dumps(data)
    paths = list(files) + list(plists) + [target / "neko-agent", target / "release.txt", config]
    identity = {"master": master, "backend_name": name, "python": str(python)}
    with InstallState(folder, "mac", args.repair) as state:
        intent = {"backend_token": "ag_" + secrets.token_urlsafe(24),
                  "gateway_token": secrets.token_urlsafe(32),
                  "backend_url": "agent://monocloud-mac-" + uuid.uuid4().hex}
        state.prepare(identity, paths, intent)
        intent = state.data["intent"]
        rows = api("/api/backends")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("unexpected_backend_list")
        matching = [row for row in rows if row.get("name") == name]
        if len(matching) > 1 or (matching and matching[0].get("url") != intent["backend_url"]):
            raise ValueError("backend_name_owned_by_another_installation")
        if state.data.get("backend_id") is not None:
            if not matching or matching[0].get("id") != state.data["backend_id"]:
                raise ValueError("recorded_backend_missing_or_changed")
        binary, digest = fetch(ASSET, args.download_proxy)
        with tempfile.TemporaryDirectory(prefix="neko-mac-stage-") as temporary:
            staging = Path(temporary)
            executable = staging / "neko-agent"
            executable.write_bytes(binary); executable.chmod(0o700)
            result = run([str(executable), "--version"], check=True, capture_output=True, text=True, timeout=30)
            if result.stdout.strip() not in ("1.4.8", VERSION):
                raise ValueError("unexpected_agent_version")
            for path, content in plists.items():
                candidate = staging / path.name; candidate.write_bytes(content)
                run(["plutil", "-lint", str(candidate)], check=True, capture_output=True, text=True, timeout=30)
        if matching:
            backend = matching[0].get("id")
        else:
            state.data["phase"] = "backend_create_pending"; state.save()
            result = api("/api/backends", {"name": name, "url": intent["backend_url"],
                         "type": "clash", "token": intent["backend_token"]})
            backend = result.get("id") if isinstance(result, dict) else None
        if not isinstance(backend, int) or isinstance(backend, bool) or backend <= 0:
            raise ValueError("invalid_created_backend_id")
        state.data["backend_id"] = backend; state.save()
        values = {"NEKO_SERVER": master, "NEKO_BACKEND_ID": str(backend),
                  "NEKO_BACKEND_TOKEN": intent["backend_token"], "NEKO_GATEWAY_TYPE": "clash",
                  "NEKO_GATEWAY_URL": "http://127.0.0.1:19093", "NEKO_GATEWAY_TOKEN": intent["gateway_token"]}
        for path, content in files.items(): state.publish(path, content)
        state.publish(target / "neko-agent", binary, 0o755)
        state.publish(config, encode_env(values), 0o600)
        for path, content in plists.items(): state.publish(path, content)
        logs.mkdir(mode=0o700, parents=True, exist_ok=True); logs.chmod(0o700)
        state.publish(target / "release.txt", ("Agent=" + VERSION + "\nAssetSHA256=" + digest + "\nBackendID=" + str(backend) + "\n").encode())
        state.finish()
    return {"id": backend, "version": VERSION, "sha256": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-url", required=True, help="Master base URL; no credentials in URL")
    parser.add_argument("--backend-name", default="monocloud-mac")
    parser.add_argument("--admin-token-file", type=Path, help="0600 file containing a Master management Bearer token")
    parser.add_argument("--download-proxy", help="Optional HTTPS download proxy URL")
    parser.add_argument("--python", default=shutil.which("python3"), help="Persistent Python runtime for LaunchAgent")
    parser.add_argument("--allow-insecure-http", action="store_true", help="Explicitly allow remote HTTP on a separately trusted/encrypted network")
    parser.add_argument("--repair", action="store_true", help="Resume only files/backend owned by the private installation journal")
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("Requires macOS arm64")
    try:
        result = install(args)
    except Exception as error:
        raise SystemExit("Installation failed (" + safe_error_code(error) + "); keep private install-state.json (backend may already exist); resolve conflicts and rerun the same arguments with --repair") from None
    print("Installed backend", result["id"], result["version"], "SHA256", result["sha256"], "; jobs validated, not started")


if __name__ == "__main__": main()
