#!/usr/bin/env python3
"""Upgrade an existing collector from this checkout; no config editing required."""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from install_support import (InstallState, VERSION, agent_asset, atomic_write, expected_agent_version,
                             fetch_release, mac_plist, read_private_env, safe_error_code)

REQUIRED = ("NEKO_SERVER", "NEKO_BACKEND_ID", "NEKO_BACKEND_TOKEN", "NEKO_GATEWAY_TYPE",
            "NEKO_GATEWAY_URL", "NEKO_GATEWAY_TOKEN")
UNITS = {"bridge": "monocloud-neko-bridge.service", "agent": "monocloud-neko-agent.service"}
LABELS = {"bridge": "io.monocloud-neko.bridge", "agent": "io.monocloud-neko.agent"}


class UpgradeError(Exception):
    def __init__(self, code, restored=None):
        super().__init__(code)
        self.code, self.restored = code, restored


class MissingSettings(ValueError):
    """Private config lacks settings the new version needs; nothing was changed."""

    def __init__(self, keys):
        super().__init__("private_config_not_ready")
        self.keys = keys


def detect_kind():
    system, machine = platform.system(), platform.machine()
    if system == "Linux" and machine == "x86_64":
        return "linux"
    if system == "Darwin" and machine == "arm64":
        return "mac"
    raise ValueError("unsupported_platform")


def installation(home, kind):
    if kind == "linux":
        return {"target": home / ".local/lib/monocloud-neko-bridge",
                "config": home / ".config/monocloud-neko-bridge/agent.env",
                "state": home / ".config/monocloud-neko-bridge",
                "units": home / ".config/systemd/user"}
    return {"target": home / ".local/lib/monocloud-neko-bridge-mac",
            "config": home / ".config/monocloud-neko-bridge-mac/agent.env",
            "state": home / ".config/monocloud-neko-bridge-mac",
            "launch": home / "Library/LaunchAgents",
            "logs": home / ".local/state/monocloud-neko-bridge-mac"}


def from_checkout(source, kind, paths, python):
    if kind == "linux":
        return {paths["target"] / "bridge.py": (source / "linux/bridge.py").read_bytes(),
                paths["target"] / "run_agent.py": (source / "linux/run_agent.py").read_bytes(),
                paths["units"] / UNITS["bridge"]: (source / "deploy" / UNITS["bridge"]).read_bytes(),
                paths["units"] / UNITS["agent"]: (source / "deploy" / UNITS["agent"]).read_bytes()}
    python = Path(python or "")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("persistent_python_is_not_executable")
    files = {paths["target"] / name: (source / "mac" / name).read_bytes()
             for name in ("adapter.py", "protocol.py", "launcher.py")}
    # The installer publishes linux/bridge.py as the mac gateway module.
    files[paths["target"] / "bridge.py"] = (source / "linux/bridge.py").read_bytes()
    for role in ("bridge", "agent"):
        label, content = mac_plist(role, paths["target"], paths["logs"], python)
        files[paths["launch"] / (label + ".plist")] = content
    return files


def installed_agent(target):
    info = {}
    release = target / "release.txt"
    if release.is_file():
        for line in release.read_text().splitlines():
            key, _, value = line.partition("=")
            info[key] = value
    return info


def python_from_plist(paths):
    """Recover the persistent interpreter from an installation that has no journal."""
    try:
        data = plistlib.loads((paths["launch"] / (LABELS["bridge"] + ".plist")).read_bytes())
        python = Path(data["ProgramArguments"][0])
    except Exception:
        raise ValueError("persistent_python_is_not_executable") from None
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("persistent_python_is_not_executable")
    return str(python)


def agent_payload(kind, paths, state, fetch, run, proxy=None):
    """Return payload for neko-agent and release.txt when the pinned Agent differs."""
    info = installed_agent(paths["target"])
    if info.get("Agent") == VERSION:
        if not (paths["target"] / "neko-agent").is_file():
            raise ValueError("agent_binary_missing")
        return {}
    asset = agent_asset(kind)
    binary, digest = fetch(asset, proxy)
    with tempfile.TemporaryDirectory(prefix="neko-upgrade-stage-") as temporary:
        staged = Path(temporary) / "neko-agent"
        staged.write_bytes(binary)
        staged.chmod(0o700)
        result = run([str(staged), "--version"], check=True, capture_output=True, text=True, timeout=30)
        if (result.stdout or "").strip() not in (expected_agent_version(), VERSION):
            raise ValueError("unexpected_agent_version")
    release = {"Agent": VERSION, "AssetSHA256": digest}
    if kind == "mac":
        backend = state.get("backend_id")
        if not isinstance(backend, int):
            raise ValueError("missing_recorded_backend")
        release["BackendID"] = str(backend)
    content = "".join(key + "=" + value + "\n" for key, value in release.items()).encode()
    return {paths["target"] / "neko-agent": (binary, 0o755),
            paths["target"] / "release.txt": (content, 0o644)}


def pending(files):
    result = {}
    for path, value in files.items():
        content, mode = value if isinstance(value, tuple) else (value, 0o644)
        if path.is_symlink():
            raise ValueError("installation_target_is_symlink")
        if (not path.is_file() or path.read_bytes() != content
                or (path.stat().st_mode & 0o777) != mode):
            result[path] = (content, mode)
    return result


def service_uid():
    return os.getuid()


def active_services(kind, run):
    if kind == "linux":
        return [role for role, unit in UNITS.items()
                if run(["systemctl", "--user", "is-active", "--quiet", unit],
                       check=False, capture_output=True).returncode == 0]
    return [role for role, label in LABELS.items()
            if run(["launchctl", "print", "gui/" + str(service_uid()) + "/" + label],
                   check=False, capture_output=True).returncode == 0]


def stop_services(kind, run, roles):
    if not roles:
        return
    if kind == "linux":
        run(["systemctl", "--user", "stop", *[UNITS[role] for role in ("agent", "bridge") if role in roles]],
            check=True, capture_output=True)
        return
    for role in ("agent", "bridge"):
        if role in roles:
            run(["launchctl", "bootout", "gui/" + str(service_uid()) + "/" + LABELS[role]],
                check=True, capture_output=True)


def start_services(kind, run, paths, roles):
    for role in ("bridge", "agent"):
        if role not in roles:
            continue
        if kind == "linux":
            run(["systemctl", "--user", "start", UNITS[role]], check=True, capture_output=True)
            continue
        for attempt in range(5):
            try:
                run(["launchctl", "bootstrap", "gui/" + str(service_uid()),
                     str(paths["launch"] / (LABELS[role] + ".plist"))], check=True, capture_output=True)
                break
            except (subprocess.CalledProcessError, OSError):
                if attempt == 4:
                    raise
                time.sleep(1)


def probe_health(url, token, timeout=1.0, attempts=30, delay=0.5):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url.rstrip("/") + "/health",
                                     headers={"Authorization": "Bearer " + token})
    for attempt in range(attempts):
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                return 401, {}
            if error.code in (502, 503):
                try:
                    return error.code, json.load(error)
                except (ValueError, OSError):
                    return error.code, {}
        except (urllib.error.URLError, OSError):
            pass
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise UpgradeError("health_check_timeout")


def create_backup(home, paths):
    root = home / ".local/state/monocloud-neko-upgrade"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder, suffix = root / stamp, 0
    while folder.exists():
        suffix += 1
        folder = root / (stamp + "-" + str(suffix))
    folder.mkdir(mode=0o700)
    index = []
    for path in paths:
        name = str(len(index)).zfill(4)
        mode = path.stat().st_mode & 0o777
        (folder / name).write_bytes(path.read_bytes())
        (folder / name).chmod(mode)
        index.append({"path": str(path), "backup": name, "mode": mode})
    (folder / "index.json").write_text(json.dumps(index, indent=2))
    (folder / "index.json").chmod(0o600)
    return folder, index


def restore_backup(folder, index):
    for entry in index:
        atomic_write(Path(entry["path"]), (folder / entry["backup"]).read_bytes(), entry["mode"])


def checkout_version(source, run):
    try:
        result = run(["git", "-C", str(source), "describe", "--tags", "--always", "--dirty"],
                     check=False, capture_output=True, text=True, timeout=10)
        return (result.stdout or "").strip() or None
    except Exception:
        return None


def display(path, home):
    try:
        return str(path.relative_to(home))
    except ValueError:
        return str(path)


def upgrade(home=None, source=None, kind=None, run=subprocess.run, fetch=fetch_release,
            probe=probe_health, dry_run=False, proxy=None, adopt=False):
    home = Path(home or Path.home())
    source = Path(source or Path(__file__).resolve().parents[1])
    kind = kind or detect_kind()
    paths = installation(home, kind)
    values = read_private_env(paths["config"])
    missing = [key for key in REQUIRED
               if not values.get(key) or values[key].startswith("replace-with-")]
    if missing:
        raise MissingSettings(missing)

    with InstallState(paths["state"], kind, upgrade=True, adopt=adopt) as state:
        if state.data is not None:
            state.verify(ignore={str(paths["config"])})
            python = (state.data.get("identity") or {}).get("python")
            records = state.data
        else:
            python = python_from_plist(paths) if kind == "mac" else None
            backend = installed_agent(paths["target"]).get("BackendID", "")
            records = {"backend_id": int(backend) if backend.isdigit() else None}
        files = from_checkout(source, kind, paths, python)
        files.update(agent_payload(kind, paths, records, fetch, run, proxy))
        changed = pending(files)
        if not changed:
            return {"status": "up-to-date", "changes": [], "adopted": state.data is None}
        if dry_run:
            return {"status": "dry-run", "adopted": state.data is None,
                    "changes": [display(path, home) for path in changed],
                    "restart": active_services(kind, run)}

        backup, index = create_backup(home, [*changed, *([state.path] if state.path.exists() else [])])
        roles = active_services(kind, run)
        health = None
        try:
            stop_services(kind, run, roles)
            for path, (content, mode) in changed.items():
                if state.data is None:
                    atomic_write(path, content, mode)
                else:
                    state.replace(path, content, mode)
            if kind == "linux" and any(str(path).endswith(".service") for path in changed):
                run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
            start_services(kind, run, paths, roles)
            if "bridge" in roles:
                status, health = probe(values["NEKO_GATEWAY_URL"], values["NEKO_GATEWAY_TOKEN"])
                if status == 401:
                    raise UpgradeError("gateway_token_rejected")
                if status not in (200, 502, 503):
                    raise UpgradeError("health_check_failed")
            version = checkout_version(source, run)
            if version and state.data is not None:
                state.data["bridge_version"] = version
                state.save()
        except Exception as error:
            try:
                stop_services(kind, run, roles)
            except Exception:
                pass
            restore_backup(backup, index)
            if kind == "linux" and any(str(path).endswith(".service") for path in changed):
                try:
                    run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
                except Exception:
                    pass
            try:
                start_services(kind, run, paths, roles)
            except Exception:
                print("warning: previous files were restored but services may not be running",
                      file=sys.stderr)
            code = error.code if isinstance(error, UpgradeError) else safe_error_code(error)
            raise UpgradeError(code, restored=str(backup)) from None
        return {"status": "upgraded", "changes": [display(path, home) for path in changed],
                "backup": str(backup), "restarted": roles, "health": health,
                "adopted": state.data is None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show planned changes without touching services")
    parser.add_argument("--download-proxy", help="Optional HTTPS proxy used only to download a new Agent")
    parser.add_argument("--adopt", action="store_true",
                        help="Upgrade an installation that predates install-state.json; existing files are not verified")
    args = parser.parse_args()
    try:
        result = upgrade(dry_run=args.dry_run, proxy=args.download_proxy, adopt=args.adopt)
    except MissingSettings as error:
        print("Cannot upgrade: private config is missing required settings: " + ", ".join(error.keys),
              file=sys.stderr)
        raise SystemExit("No files were changed") from None
    except Exception as error:
        code = error.code if isinstance(error, UpgradeError) else safe_error_code(error)
        if isinstance(error, UpgradeError) and error.restored:
            raise SystemExit("Upgrade failed (" + code + "); previous files restored from " + error.restored) from None
        if code == "installation_not_found":
            raise SystemExit("Upgrade failed (installation_not_found); no installation journal was found. "
                             "Rerun with --adopt only for a dedicated directory you trust.") from None
        raise SystemExit("Upgrade failed (" + code + "); no files were changed") from None
    if result.get("adopted"):
        print("warning: no installation journal; adopted this installation without verifying existing files",
              file=sys.stderr)
    if result["status"] == "up-to-date":
        print("Already up to date")
        return
    if result["status"] == "dry-run":
        print("Would update " + str(len(result["changes"])) + " file(s):")
        for item in result["changes"]:
            print("  " + item)
        if result["restart"]:
            print("Would restart: " + ", ".join(result["restart"]))
        return
    print("Upgraded " + str(len(result["changes"])) + " file(s); backup " + result["backup"])
    if result["restarted"]:
        print("Restarted: " + ", ".join(result["restarted"]))
    if result["health"] is not None:
        print("Health: " + json.dumps(result["health"]))


if __name__ == "__main__":
    main()
