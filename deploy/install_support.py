"""Shared private publication journal and official Agent delegation."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

# The official Agent is installed by upstream tooling; this is only the version
# we recommend and pass to its installer.
PINNED_AGENT_VERSION = "agent-v1.4.8"
AGENT_INSTALL_URL = "https://raw.githubusercontent.com/foru17/neko-master/main/apps/agent/install.sh"


def agent_env(kind, values, home):
    """Environment for the upstream Agent installer; tokens stay in the environment."""
    home = Path(home)
    return {"NEKO_SERVER": values["NEKO_SERVER"],
            "NEKO_BACKEND_ID": values["NEKO_BACKEND_ID"],
            "NEKO_BACKEND_TOKEN": values["NEKO_BACKEND_TOKEN"],
            "NEKO_GATEWAY_TYPE": values["NEKO_GATEWAY_TYPE"],
            "NEKO_GATEWAY_URL": values["NEKO_GATEWAY_URL"],
            "NEKO_GATEWAY_TOKEN": values["NEKO_GATEWAY_TOKEN"],
            "NEKO_INSTALL_DIR": str(home / ".local/bin"),
            "NEKO_AGENT_CONFIG_DIR": str(home / ".config/neko-agent"),
            "NEKO_AGENT_STATE_DIR": str(home / ".local/state/neko-agent"),
            "NEKO_AGENT_VERSION": PINNED_AGENT_VERSION,
            # Upstream systemd autostart needs root; macOS launchd stays per-user.
            "NEKO_AUTO_START": "true" if kind == "mac" else "false"}


def agent_command(kind, home):
    """Printable upstream install command that reads tokens from the private config."""
    config = Path(home) / (".config/monocloud-neko-bridge-mac/agent.env" if kind == "mac"
                           else ".config/monocloud-neko-bridge/agent.env")
    autostart = "true" if kind == "mac" else "false"
    return "\n".join([
        "set -a",
        ". " + shlex.quote(str(config)),
        "set +a",
        'export NEKO_AGENT_CONFIG_DIR="$HOME/.config/neko-agent"',
        'export NEKO_AGENT_STATE_DIR="$HOME/.local/state/neko-agent"',
        'tmp="$(mktemp)"',
        "trap 'rm -f \"$tmp\"' EXIT",
        "curl -fsSL " + AGENT_INSTALL_URL + ' -o "$tmp" && \\',
        "env \\",
        '  NEKO_SERVER="$NEKO_SERVER" NEKO_BACKEND_ID="$NEKO_BACKEND_ID" \\',
        '  NEKO_BACKEND_TOKEN="$NEKO_BACKEND_TOKEN" NEKO_GATEWAY_TYPE="$NEKO_GATEWAY_TYPE" \\',
        '  NEKO_GATEWAY_URL="$NEKO_GATEWAY_URL" NEKO_GATEWAY_TOKEN="$NEKO_GATEWAY_TOKEN" \\',
        '  NEKO_INSTALL_DIR="$HOME/.local/bin" \\',
        '  NEKO_AGENT_CONFIG_DIR="$HOME/.config/neko-agent" \\',
        '  NEKO_AGENT_STATE_DIR="$HOME/.local/state/neko-agent" \\',
        '  NEKO_AGENT_VERSION="' + PINNED_AGENT_VERSION + '" NEKO_AUTO_START="' + autostart + '" \\',
        '  sh "$tmp"'])


def fetch_agent_installer(url=AGENT_INSTALL_URL):
    opener = urllib.request.build_opener()
    with opener.open(url, timeout=45) as response:
        data = response.read(1048577)
    if len(data) > 1048576:
        raise ValueError("agent_installer_too_large")
    return data.decode()


def install_agent(kind, values, home, fetch=None, run=None):
    """Run the upstream installer with this installation's gateway settings."""
    fetch, run = fetch or fetch_agent_installer, run or subprocess.run
    script = fetch(AGENT_INSTALL_URL)
    environment = dict(os.environ)
    environment.update(agent_env(kind, values, home))
    result = run(["sh"], input=script, env=environment, text=True, capture_output=True)
    if result.returncode != 0:
        if getattr(result, "stderr", ""):
            sys.stderr.write(result.stderr)
        raise ValueError("agent_install_failed")
    return result


def atomic_write(path, content, mode=0o600, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.chmod(mode)
        if exclusive:
            os.link(temporary, path)  # Atomic no-clobber publication.
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_private_env(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("insecure_or_missing_private_config")
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values = shlex.split(value)
        if len(values) != 1:
            raise ValueError("invalid_private_config")
        result[key.strip()] = values[0]
    return result


def encode_env(values):
    return "".join(key + "=" + shlex.quote(str(value)) + "\n" for key, value in values.items()).encode()


def mac_plist(role, target, logs, python):
    """Build one LaunchAgent job exactly as the mac installer publishes it."""
    label = "io.monocloud-neko." + role
    data = {"Label": label, "ProgramArguments": [str(python), "-B", str(target / "launcher.py"), role],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10, "ExitTimeOut": 30,
            "ProcessType": "Background", "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
            "StandardOutPath": str(logs / (role + "-launch.log")),
            "StandardErrorPath": str(logs / (role + "-launch.log"))}
    return label, plistlib.dumps(data)


class InstallState:
    """An exclusive per-user journal; only journal-owned bytes may be resumed."""
    def __init__(self, folder, kind, repair=False, upgrade=False, adopt=False):
        self.folder, self.kind, self.repair, self.upgrade = Path(folder), kind, repair, upgrade
        self.adopt = adopt
        self.path = self.folder / "install-state.json"
        self.data = None
        self.lock = None

    def __enter__(self):
        if self.folder.is_symlink():
            raise ValueError("installation_directory_is_symlink")
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.folder.chmod(0o700)
        descriptor = os.open(self.folder / "install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(descriptor, "a+")
        try:
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("installation_locked") from None
            if self.path.exists():
                if self.path.is_symlink() or self.path.stat().st_mode & 0o077 or self.path.stat().st_size > 1048576:
                    raise ValueError("insecure_installation_journal")
                self.data = json.loads(self.path.read_text())
                if self.data.get("schema") != 1 or self.data.get("kind") != self.kind:
                    raise ValueError("incompatible_installation_journal")
                if self.upgrade and self.data.get("phase") != "complete":
                    raise ValueError("incomplete_installation_journal")
                if not self.repair and not self.upgrade:
                    raise ValueError("installation_exists_use_repair_to_resume")
            elif self.upgrade and not self.adopt:
                raise ValueError("installation_not_found")
            return self
        except BaseException:
            self.lock.close()
            raise

    def __exit__(self, *_):
        self.lock.close()

    def save(self):
        atomic_write(self.path, json.dumps(self.data, indent=2).encode())

    def prepare(self, identity, paths, intent=None):
        if self.data is not None and self.data.get("identity") != identity:
            # Only a pre-create, pre-publication intent can be safely retargeted.
            if (self.data.get("phase") != "prepared" or self.data.get("artifacts")
                    or self.data.get("backend_id") is not None):
                raise ValueError("installation_identity_changed")
            self.data["identity"] = identity
            self.save()
        artifacts = {} if self.data is None else self.data["artifacts"]
        for path in paths:
            path = Path(path)
            if path.is_symlink():
                raise ValueError("installation_target_is_symlink")
            if path.exists():
                expected = artifacts.get(str(path))
                if not expected or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError("existing_installation_file_conflict")
        if self.data is None:
            self.data = {"schema": 1, "kind": self.kind, "identity": identity,
                         "intent": intent or {}, "artifacts": {}, "phase": "prepared"}
            self.save()

    def publish(self, path, content, mode=0o644):
        path = Path(path)
        digest = hashlib.sha256(content).hexdigest()
        expected = self.data["artifacts"].get(str(path))
        if expected is not None and expected != digest:
            raise ValueError("installation_payload_changed")
        if path.is_symlink():
            raise ValueError("installation_target_is_symlink")
        if path.exists():
            if expected != digest or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("existing_installation_file_conflict")
            path.chmod(mode)
            return
        # Claim before publication: a crash in either order remains recoverable.
        self.data["artifacts"][str(path)] = digest
        self.data["phase"] = "publishing"
        self.save()
        atomic_write(path, content, mode, exclusive=True)

    def finish(self):
        self.data["phase"] = "complete"
        self.save()

    def verify(self, ignore=()):
        """Confirm every recorded artifact still holds the bytes this journal published."""
        if self.data is None:
            raise ValueError("installation_not_found")
        for name, digest in self.data["artifacts"].items():
            if name in ignore:
                continue
            path = Path(name)
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("existing_installation_file_conflict")

    def replace(self, path, content, mode=0o644):
        """Atomically replace a journal-owned artifact and record its new digest."""
        path = Path(path)
        if str(path) not in self.data["artifacts"]:
            raise ValueError("unrecorded_installation_artifact")
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing_installation_artifact")
        atomic_write(path, content, mode)
        self.data["artifacts"][str(path)] = hashlib.sha256(content).hexdigest()
        self.save()


def safe_error_code(error):
    """Return only fixed identifiers/status, never remote text or private paths."""
    if isinstance(error, urllib.error.HTTPError):
        return "http_" + str(error.code)
    if isinstance(error, ValueError) and re.fullmatch(r"[a-z_]+", str(error)):
        return str(error)
    return type(error).__name__
