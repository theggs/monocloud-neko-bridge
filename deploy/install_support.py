"""Shared first-install staging and private, resumable publication journal."""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import tarfile
import tempfile
import urllib.error
import urllib.request

VERSION = "agent-v1.4.8"
BASE = "https://github.com/foru17/neko-master/releases/download/" + VERSION + "/"


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


def fetch_release(asset, proxy=None):
    opener = (urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy}))
              if proxy else urllib.request.build_opener())
    def read(name, limit):
        with opener.open(BASE + name, timeout=45) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("release_response_too_large")
        return data
    checksums = read("checksums.txt", 1048576).decode()
    expected = None
    for line in checksums.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == asset:
            expected = fields[0]
    if expected is None or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise ValueError("release_checksum_entry_missing_or_invalid")
    archive = read(asset, 64 * 1024 * 1024)
    digest = hashlib.sha256(archive).hexdigest()
    if digest != expected.lower():
        raise ValueError("release_checksum_mismatch")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = [member for member in tar if Path(member.name).name == "neko-agent" and member.isfile()]
        if len(members) != 1 or members[0].size > 64 * 1024 * 1024:
            raise ValueError("unexpected_release_archive")
        binary = tar.extractfile(members[0]).read()
    return binary, digest


class InstallState:
    """An exclusive per-user journal; only journal-owned bytes may be resumed."""
    def __init__(self, folder, kind, repair=False):
        self.folder, self.kind, self.repair = Path(folder), kind, repair
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
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                if self.path.is_symlink() or self.path.stat().st_mode & 0o077 or self.path.stat().st_size > 1048576:
                    raise ValueError("insecure_installation_journal")
                self.data = json.loads(self.path.read_text())
                if self.data.get("schema") != 1 or self.data.get("kind") != self.kind:
                    raise ValueError("incompatible_installation_journal")
                if not self.repair:
                    raise ValueError("installation_exists_use_repair_to_resume")
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


def safe_error_code(error):
    """Return only fixed identifiers/status, never remote text or private paths."""
    if isinstance(error, urllib.error.HTTPError):
        return "http_" + str(error.code)
    if isinstance(error, ValueError) and re.fullmatch(r"[a-z_]+", str(error)):
        return str(error)
    return type(error).__name__
