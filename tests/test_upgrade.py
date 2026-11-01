import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRun:
    def __init__(self, active=("bridge", "agent")):
        self.calls = []
        self.active = set(active)
        self.fail_bootstrap_once = False

    def __call__(self, args, **kwargs):
        args = [str(item) for item in args]
        self.calls.append(args)
        if args[0] == "git":
            return SimpleNamespace(returncode=0, stdout="v0.0.2\n", stderr="")
        if args[0] == "systemctl":
            if "is-active" in args:
                role = "bridge" if "bridge" in args[-1] else "agent"
                return SimpleNamespace(returncode=0 if role in self.active else 3)
            if "stop" in args:
                self.active = set()
            if "start" in args:
                self.active.add("bridge" if "bridge" in args[-1] else "agent")
            return SimpleNamespace(returncode=0)
        if args[0] == "launchctl":
            role = "bridge" if "bridge" in args[-1] else "agent"
            if args[1] == "print":
                return SimpleNamespace(returncode=0 if role in self.active else 1)
            if args[1] == "bootout":
                self.active.discard(role)
                return SimpleNamespace(returncode=0)
            if args[1] == "bootstrap":
                if self.fail_bootstrap_once:
                    self.fail_bootstrap_once = False
                    raise subprocess.CalledProcessError(5, args)
                self.active.add(role)
                return SimpleNamespace(returncode=0)
        if args[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="9.9.9\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def commands(self):
        return [" ".join(call) for call in self.calls]

    def find(self, *tokens):
        for index, command in enumerate(self.commands()):
            if all(token in command for token in tokens):
                return index
        self.fail("command not found: " + " ".join(tokens) + " in " + repr(self.commands()))


class UpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upgrade = load("upgrade_module", "upgrade.py")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.run = FakeRun()
        self.fetches = []
        self.probes = []
        self.probe_error = None
        self.probe_status = 200

    def tearDown(self):
        self.temp.cleanup()

    def fetch(self, asset, proxy=None):
        self.fetches.append(asset)
        return b"new agent", "abc123"

    def probe(self, url, token):
        self.probes.append((url, token))
        if self.probe_error:
            raise self.probe_error
        return self.probe_status, {"status": "starting", "reason": "awaiting_first_request"}

    def settings(self, kind, extra=None):
        values = {"NEKO_SERVER": "https://neko.example.invalid", "NEKO_BACKEND_ID": "3",
                  "NEKO_BACKEND_TOKEN": "synthetic-backend-token", "NEKO_GATEWAY_TYPE": "clash",
                  "NEKO_GATEWAY_URL": "http://127.0.0.1:" + ("19092" if kind == "linux" else "19093"),
                  "NEKO_GATEWAY_TOKEN": "synthetic-gateway-token"}
        values.update(extra or {})
        return values

    def install(self, kind="linux", agent="agent-v1.4.8", current=False, python=None):
        paths = self.upgrade.installation(self.home, kind)
        paths["target"].mkdir(parents=True)
        paths["state"].mkdir(parents=True)
        values = self.settings(kind)
        paths["config"].write_text("".join(key + "=" + value + "\n" for key, value in values.items()))
        paths["config"].chmod(0o600)
        files = {}
        if kind == "linux":
            payload = ({"bridge.py": (ROOT / "linux/bridge.py").read_bytes(),
                        "run_agent.py": (ROOT / "linux/run_agent.py").read_bytes(),
                        "monocloud-neko-bridge.service": (ROOT / "deploy/monocloud-neko-bridge.service").read_bytes(),
                        "monocloud-neko-agent.service": (ROOT / "deploy/monocloud-neko-agent.service").read_bytes()}
                       if current else {})
            files[paths["target"] / "bridge.py"] = payload.get("bridge.py", b"old bridge")
            files[paths["target"] / "run_agent.py"] = payload.get("run_agent.py", b"old run")
            files[paths["units"] / "monocloud-neko-bridge.service"] = payload.get(
                "monocloud-neko-bridge.service", b"old bridge unit")
            files[paths["units"] / "monocloud-neko-agent.service"] = payload.get(
                "monocloud-neko-agent.service", b"old agent unit")
            identity = {}
        else:
            paths["launch"].mkdir(parents=True)
            paths["logs"].mkdir(parents=True)
            files[paths["target"] / "adapter.py"] = b"old adapter"
            files[paths["target"] / "protocol.py"] = b"old protocol"
            files[paths["target"] / "launcher.py"] = b"old launcher"
            files[paths["target"] / "bridge.py"] = b"old bridge"
            files[paths["launch"] / "io.monocloud-neko.bridge.plist"] = b"old plist"
            files[paths["launch"] / "io.monocloud-neko.agent.plist"] = b"old plist"
            identity = {"master": "https://neko.example.invalid", "backend_name": "synthetic-device",
                        "python": python or sys.executable}
        files[paths["target"] / "neko-agent"] = b"old agent"
        files[paths["target"] / "release.txt"] = ("Agent=" + agent + "\nAssetSHA256=olddigest\n"
                                                 + ("BackendID=7\n" if kind == "mac" else "")).encode()
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (paths["target"] / "neko-agent").chmod(0o755)
        artifacts = {str(path): hashlib.sha256(content).hexdigest() for path, content in files.items()}
        if kind == "mac":
            artifacts[str(paths["config"])] = hashlib.sha256(paths["config"].read_bytes()).hexdigest()
        journal = {"schema": 1, "kind": kind, "phase": "complete", "identity": identity,
                   "intent": {}, "artifacts": artifacts}
        state = paths["state"] / "install-state.json"
        state.write_text(json.dumps(journal))
        state.chmod(0o600)
        return paths, files, values

    def install_via_journal(self, kind):
        paths = self.upgrade.installation(self.home, kind)
        values = self.settings(kind)
        config = "".join(key + "=" + value + "\n" for key, value in values.items()).encode()
        paths["config"].parent.mkdir(parents=True, exist_ok=True)
        files = {}
        if kind == "linux":
            files[paths["target"] / "bridge.py"] = b"old bridge"
            files[paths["target"] / "run_agent.py"] = b"old run"
            files[paths["units"] / "monocloud-neko-bridge.service"] = b"old bridge unit"
            files[paths["units"] / "monocloud-neko-agent.service"] = b"old agent unit"
            identity = {}
        else:
            paths["launch"].mkdir(parents=True)
            files[paths["target"] / "adapter.py"] = b"old adapter"
            files[paths["target"] / "protocol.py"] = b"old protocol"
            files[paths["target"] / "launcher.py"] = b"old launcher"
            files[paths["target"] / "bridge.py"] = b"old bridge"
            files[paths["launch"] / "io.monocloud-neko.bridge.plist"] = b"old plist"
            files[paths["launch"] / "io.monocloud-neko.agent.plist"] = b"old plist"
            identity = {"master": "https://neko.example.invalid", "backend_name": "synthetic-device",
                        "python": sys.executable}
        files[paths["target"] / "neko-agent"] = b"old agent"
        files[paths["target"] / "release.txt"] = ("Agent=agent-v1.4.8\nAssetSHA256=olddigest\n"
                                                 + ("BackendID=7\n" if kind == "mac" else "")).encode()
        paths_list = list(files) + ([paths["config"]] if kind == "mac" else [])
        with self.upgrade.InstallState(paths["state"], kind) as state:
            state.prepare(identity, paths_list)
            if kind == "mac":
                state.publish(paths["config"], config, 0o600)
            else:
                paths["config"].write_bytes(config)
                paths["config"].chmod(0o600)
            for path, content in files.items():
                state.publish(path, content, 0o755 if path.name == "neko-agent" else 0o644)
            state.finish()
        return paths

    def upgrade_now(self, kind="linux", **kwargs):
        return self.upgrade.upgrade(home=self.home, source=ROOT, kind=kind, run=self.run,
                                    fetch=self.fetch, probe=self.probe, **kwargs)

    def test_linux_upgrade_replaces_files_and_keeps_config(self):
        paths, old, values = self.install()
        config_before = paths["config"].read_bytes()
        result = self.upgrade_now()
        self.assertEqual(result["status"], "upgraded")
        self.assertEqual((paths["target"] / "bridge.py").read_bytes(), (ROOT / "linux/bridge.py").read_bytes())
        self.assertEqual((paths["target"] / "run_agent.py").read_bytes(), (ROOT / "linux/run_agent.py").read_bytes())
        self.assertEqual(paths["config"].read_bytes(), config_before)
        self.assertEqual(result["restarted"], ["bridge", "agent"])
        self.assertLess(self.run.find(" stop "), self.run.find(" start "))
        self.assertEqual(self.probes, [(values["NEKO_GATEWAY_URL"], values["NEKO_GATEWAY_TOKEN"])])
        self.assertTrue(Path(result["backup"]).is_dir())
        self.assertEqual((paths["target"] / "release.txt").read_bytes(), old[paths["target"] / "release.txt"])
        journal = json.loads((paths["state"] / "install-state.json").read_text())
        digest = hashlib.sha256((paths["target"] / "bridge.py").read_bytes()).hexdigest()
        self.assertEqual(journal["artifacts"][str(paths["target"] / "bridge.py")], digest)
        self.assertEqual(journal["bridge_version"], "v0.0.2")

    def test_installer_journal_is_accepted_for_both_platforms(self):
        for kind in ("linux", "mac"):
            with self.subTest(kind=kind):
                self.tearDown()
                self.setUp()
                self.install_via_journal(kind)
                result = self.upgrade_now(kind)
                self.assertEqual(result["status"], "upgraded")
                self.assertEqual(self.upgrade_now(kind)["status"], "up-to-date")

    def test_missing_setting_aborts_before_touching_anything(self):
        paths, _, _ = self.install()
        paths["config"].write_text(paths["config"].read_text().replace(
            "NEKO_SERVER=https://neko.example.invalid\n", ""))
        with self.assertRaises(self.upgrade.MissingSettings) as caught:
            self.upgrade_now()
        self.assertEqual(caught.exception.keys, ["NEKO_SERVER"])
        self.assertEqual(self.run.calls, [])
        self.assertEqual(self.fetches, [])

    def test_permissive_config_is_rejected_without_writes(self):
        paths, old, _ = self.install()
        paths["config"].chmod(0o644)
        with self.assertRaises(ValueError) as caught:
            self.upgrade_now()
        self.assertEqual(str(caught.exception), "insecure_or_missing_private_config")
        self.assertEqual(self.run.calls, [])
        self.assertEqual((paths["target"] / "bridge.py").read_bytes(), old[paths["target"] / "bridge.py"])

    def test_current_install_is_reported_up_to_date(self):
        self.install(current=True)
        result = self.upgrade_now()
        self.assertEqual(result["status"], "up-to-date")
        self.assertEqual(self.run.calls, [])
        self.assertEqual(self.probes, [])

    def test_modified_installation_is_rejected_before_stopping(self):
        paths, _, _ = self.install()
        (paths["target"] / "bridge.py").write_bytes(b"locally edited")
        with self.assertRaises(ValueError) as caught:
            self.upgrade_now()
        self.assertEqual(str(caught.exception), "existing_installation_file_conflict")
        self.assertEqual(self.run.calls, [])

    def test_lock_contention_is_reported(self):
        paths, _, _ = self.install()
        with self.upgrade.InstallState(paths["state"], "linux", upgrade=True):
            with self.assertRaises(ValueError) as caught:
                self.upgrade_now()
        self.assertEqual(str(caught.exception), "installation_locked")

    def test_health_failure_restores_previous_files_and_restarts(self):
        paths, old, _ = self.install()
        journal_before = (paths["state"] / "install-state.json").read_bytes()
        self.probe_error = ValueError("health_check_timeout")
        with self.assertRaises(self.upgrade.UpgradeError) as caught:
            self.upgrade_now()
        self.assertEqual(caught.exception.code, "health_check_timeout")
        self.assertTrue(caught.exception.restored)
        self.assertEqual((paths["target"] / "bridge.py").read_bytes(), old[paths["target"] / "bridge.py"])
        self.assertEqual((paths["state"] / "install-state.json").read_bytes(), journal_before)
        self.assertEqual(len([command for command in self.run.commands() if " stop " in command]), 2)
        self.assertIn("daemon-reload", " ".join(self.run.commands()))
        self.assertIn("start", " ".join(self.run.commands()))

    def test_rejected_gateway_token_is_not_rolled_back_silently(self):
        paths, old, _ = self.install()
        self.probe_status = 401
        with self.assertRaises(self.upgrade.UpgradeError) as caught:
            self.upgrade_now()
        self.assertEqual(caught.exception.code, "gateway_token_rejected")
        self.assertEqual((paths["target"] / "bridge.py").read_bytes(), old[paths["target"] / "bridge.py"])

    def test_starting_or_degraded_health_is_accepted(self):
        for status in (502, 503):
            with self.subTest(status=status):
                self.tearDown()
                self.setUp()
                self.install()
                self.probe_status = status
                self.assertEqual(self.upgrade_now()["status"], "upgraded")

    def test_dry_run_lists_changes_without_writing(self):
        paths, old, _ = self.install()
        result = self.upgrade_now(dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertIn(".local/lib/monocloud-neko-bridge/bridge.py", result["changes"])
        self.assertEqual((paths["target"] / "bridge.py").read_bytes(), old[paths["target"] / "bridge.py"])
        self.assertNotIn("stop", " ".join(self.run.commands()))
        self.assertFalse((self.home / ".local/state/monocloud-neko-upgrade").exists())

    def test_new_pinned_agent_is_downloaded_and_recorded(self):
        paths, _, _ = self.install(agent="agent-v1.4.8")
        support = sys.modules["install_support"]
        with patch.object(self.upgrade, "VERSION", "agent-v9.9.9"), \
             patch.object(support, "VERSION", "agent-v9.9.9"):
            result = self.upgrade_now()
        self.assertEqual(result["status"], "upgraded")
        self.assertEqual(self.fetches, ["neko-agent_agent-v9.9.9_linux_amd64.tar.gz"])
        self.assertEqual((paths["target"] / "neko-agent").read_bytes(), b"new agent")
        self.assertEqual((paths["target"] / "release.txt").read_text(),
                         "Agent=agent-v9.9.9\nAssetSHA256=abc123\n")

    def test_mac_upgrade_copies_linux_bridge_and_regenerates_plists(self):
        paths, old, values = self.install("mac")
        config_before = paths["config"].read_bytes()
        result = self.upgrade_now("mac")
        self.assertEqual(result["status"], "upgraded")
        self.assertEqual((paths["target"] / "bridge.py").read_bytes(), (ROOT / "linux/bridge.py").read_bytes())
        self.assertEqual(paths["config"].read_bytes(), config_before)
        self.assertEqual(result["restarted"], ["bridge", "agent"])
        plist = plistlib.loads((paths["launch"] / "io.monocloud-neko.bridge.plist").read_bytes())
        self.assertEqual(plist["Label"], "io.monocloud-neko.bridge")
        self.assertEqual(plist["ProgramArguments"][-1], "bridge")
        self.assertLess(self.run.find("bootout", "agent"), self.run.find("bootout", "bridge"))
        self.assertLess(self.run.find("bootstrap", "bridge"), self.run.find("bootstrap", "agent"))
        self.assertEqual(len(self.probes), 1)

    def test_mac_bootstrap_retries_after_throttle_error(self):
        self.install("mac")
        self.run.fail_bootstrap_once = True
        with patch.object(self.upgrade.time, "sleep"):
            result = self.upgrade_now("mac")
        self.assertEqual(result["status"], "upgraded")
        bootstraps = [command for command in self.run.commands() if "bootstrap" in command and "bridge" in command]
        self.assertEqual(len(bootstraps), 2)

    def test_mac_missing_python_aborts(self):
        self.install("mac", python="/nonexistent/python3")
        with self.assertRaises(ValueError) as caught:
            self.upgrade_now("mac")
        self.assertEqual(str(caught.exception), "persistent_python_is_not_executable")
        self.assertEqual(self.run.calls, [])

    def test_missing_installation_journal_is_reported(self):
        paths = self.upgrade.installation(self.home, "linux")
        paths["state"].mkdir(parents=True)
        values = self.settings("linux")
        paths["config"].write_text("".join(key + "=" + value + "\n" for key, value in values.items()))
        paths["config"].chmod(0o600)
        with self.assertRaises(ValueError) as caught:
            self.upgrade_now()
        self.assertEqual(str(caught.exception), "installation_not_found")


if __name__ == "__main__":
    unittest.main()
