#!/usr/bin/env python3
"""Upgrade an existing collector from this checkout; no config editing required."""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from install_support import (InstallState, atomic_write, mac_plist, read_private_env,
                             safe_error_code)

REQUIRED = ("NEKO_SERVER", "NEKO_BACKEND_ID", "NEKO_BACKEND_TOKEN", "NEKO_GATEWAY_TYPE",
            "NEKO_GATEWAY_URL", "NEKO_GATEWAY_TOKEN")
UNITS = {"bridge": "monocloud-neko-bridge.service"}
LABELS = {"bridge": "io.monocloud-neko.bridge"}
# Files published by earlier versions; they are no longer upgraded or verified.
RETIRED = ("neko-agent", "release.txt", "run_agent.py",
           "monocloud-neko-agent.service", "io.monocloud-neko.agent.plist")


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
                paths["units"] / UNITS["bridge"]: (source / "deploy" / UNITS["bridge"]).read_bytes()}
    python = Path(python or "")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("persistent_python_is_not_executable")
    files = {paths["target"] / name: (source / "mac" / name).read_bytes()
             for name in ("adapter.py", "protocol.py", "launcher.py")}
    # The installer publishes linux/bridge.py as the mac gateway module.
    files[paths["target"] / "bridge.py"] = (source / "linux/bridge.py").read_bytes()
    label, content = mac_plist("bridge", paths["target"], paths["logs"], python)
    files[paths["launch"] / (label + ".plist")] = content
    return files


def python_from_plist(paths):
    """Recover the persistent interpreter from an installation that has no journal."""
    try:
        data = plistlib.loads((paths["launch"] / (LABELS["bridge"] + ".plist")).read_bytes())
        python = Path(data["ProgramArguments"][0])
    except Exception:
        raise ValueError("persistent_python_not_recoverable") from None
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("persistent_python_is_not_executable")
    return str(python)


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
        run(["systemctl", "--user", "stop", *[UNITS[role] for role in ("bridge",) if role in roles]],
            check=True, capture_output=True)
        return
    for role in ("bridge",):
        if role in roles:
            run(["launchctl", "bootout", "gui/" + str(service_uid()) + "/" + LABELS[role]],
                check=True, capture_output=True)


def start_services(kind, run, paths, roles):
    for role in ("bridge",):
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
    try:
        for path in paths:
            name = str(len(index)).zfill(4)
            exists = path.exists()
            entry = {"path": str(path), "backup": name, "exists": exists}
            if exists:
                entry["mode"] = path.stat().st_mode & 0o777
                (folder / name).write_bytes(path.read_bytes())
                (folder / name).chmod(entry["mode"])
            index.append(entry)
        (folder / "index.json").write_text(json.dumps(index, indent=2))
        (folder / "index.json").chmod(0o600)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return folder, index


def restore_backup(folder, index):
    for entry in index:
        path = Path(entry["path"])
        if entry.get("exists", True):
            atomic_write(path, (folder / entry["backup"]).read_bytes(), entry.get("mode", 0o600))
        else:
            path.unlink(missing_ok=True)


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


def upgrade(home=None, source=None, kind=None, run=subprocess.run, probe=probe_health,
            dry_run=False, adopt=False):
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
            retired = {name for name in state.data["artifacts"] if Path(name).name in RETIRED}
            state.verify(ignore={str(paths["config"]), *retired})
            python = (state.data.get("identity") or {}).get("python")
        else:
            if not (paths["target"] / "bridge.py").is_file():
                raise ValueError("legacy_installation_incomplete")
            python = python_from_plist(paths) if kind == "mac" else None
        files = from_checkout(source, kind, paths, python)
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
    parser.add_argument("--adopt", action="store_true",
                        help="Upgrade an installation that predates install-state.json; existing files are not verified")
    args = parser.parse_args()
    try:
        result = upgrade(dry_run=args.dry_run, adopt=args.adopt)
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
        warning = ("warning: no installation journal; adopted this installation without verifying existing files"
                   if result["status"] != "dry-run" else
                   "warning: no installation journal; would adopt this installation without verifying existing files")
        print(warning, file=sys.stderr)
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
