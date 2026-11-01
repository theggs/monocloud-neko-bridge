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
        self.probes = []
        self.probe_error = None
        self.probe_status = 200

    def tearDown(self):
        self.temp.cleanup()

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

    def collector_files(self, kind, paths, current=False):
        if kind == "linux":
            bridge = (ROOT / "linux/bridge.py").read_bytes() if current else b"old bridge"
            unit = (ROOT / "deploy/monocloud-neko-bridge.service").read_bytes() if current else b"old unit"
            return {paths["target"] / "bridge.py": bridge,
                    paths["units"] / "monocloud-neko-bridge.service": unit}
        return {paths["target"] / name: (ROOT / "mac" / name).read_bytes() if current else b"old " + name.encode()
                for name in ("adapter.py", "protocol.py", "launcher.py", "bridge.py")}

    def write_config(self, paths, kind):
        values = self.settings(kind)
        paths["config"].parent.mkdir(parents=True, exist_ok=True)
        paths["config"].write_text("".join(key + "=" + value + "\n" for key, value in values.items()))
        paths["config"].chmod(0o600)
        return values

    def install(self, kind="linux", current=False, python=None):
        paths = self.upgrade.installation(self.home, kind)
        paths["target"].mkdir(parents=True)
        paths["state"].mkdir(parents=True)
        values = self.write_config(paths, kind)
        files = self.collector_files(kind, paths, current)
        if kind == "mac":
            paths["launch"].mkdir(parents=True)
            if current:
                label, content = self.upgrade.mac_plist("bridge", paths["target"], paths["logs"], Path(python or sys.executable))
            else:
                label, content = "io.monocloud-neko.bridge", b"old plist"
            files[paths["launch"] / (label + ".plist")] = content
            identity = {"master": "https://neko.example.invalid", "backend_name": "synthetic-device",
                        "python": python or sys.executable}
        else:
            identity = {}
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
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
        files = self.collector_files(kind, paths)
        if kind == "mac":
            paths["launch"].mkdir(parents=True)
            files[paths["launch"] / "io.monocloud-neko.bridge.plist"] = b"old plist"
            identity = {"master": "https://neko.example.invalid", "backend_name": "synthetic-device",
                        "python": sys.executable}
        else:
            identity = {}
        paths_list = list(files) + ([paths["config"]] if kind == "mac" else [])
        with self.upgrade.InstallState(paths["state"], kind) as state:
            state.prepare(identity, paths_list)
            if kind == "mac":
                state.publish(paths["config"], config, 0o600)
            else:
                paths["config"].parent.mkdir(parents=True, exist_ok=True)
                paths["config"].write_bytes(config)
                paths["config"].chmod(0o600)
            for path, content in files.items():
                state.publish(path, content)
            state.finish()
        return paths

    def upgrade_now(self, kind="linux", **kwargs):
        return self.upgrade.upgrade(home=self.home, source=ROOT, kind=kind, run=self.run,
                                    probe=self.probe, **kwargs)

    def test_linux_upgrade_replaces_files_and_keeps_config(self):
        paths, old, values = self.install()
        config_before = paths["config"].read_bytes()
        result = self.upgrade_now()
        self.assertEqual(result["status"], "upgraded")
        self.assertEqual((paths["target"] / "bridge.py").read_bytes(), (ROOT / "linux/bridge.py").read_bytes())
        self.assertEqual(paths["config"].read_bytes(), config_before)
        self.assertEqual(result["restarted"], ["bridge"])
        self.assertLess(self.run.find(" stop "), self.run.find(" start "))
        self.assertEqual(self.probes, [(values["NEKO_GATEWAY_URL"], values["NEKO_GATEWAY_TOKEN"])])
        self.assertTrue(Path(result["backup"]).is_dir())
        journal = json.loads((paths["state"] / "install-state.json").read_text())
        digest = hashlib.sha256((paths["target"] / "bridge.py").read_bytes()).hexdigest()
        self.assertEqual(journal["artifacts"][str(paths["target"] / "bridge.py")], digest)
        self.assertEqual(journal["bridge_version"], "v0.0.2")

    def test_legacy_agent_files_can_be_removed_after_migration(self):
        for kind in ("linux", "mac"):
            with self.subTest(kind=kind):
                self.tearDown()
                self.setUp()
                paths, _, _ = self.install(kind)
                legacy = {paths["target"] / "run_agent.py": b"old runner",
                          paths["target"] / "neko-agent": b"old agent",
                          paths["target"] / "release.txt": b"Agent=agent-v1.4.8\n"}
                if kind == "linux":
                    legacy[paths["units"] / "monocloud-neko-agent.service"] = b"old unit"
                else:
                    legacy[paths["launch"] / "io.monocloud-neko.agent.plist"] = b"old plist"
                journal = json.loads((paths["state"] / "install-state.json").read_text())
                for path, content in legacy.items():
                    path.write_bytes(content)
                    journal["artifacts"][str(path)] = hashlib.sha256(content).hexdigest()
                (paths["state"] / "install-state.json").write_text(json.dumps(journal))
                self.assertEqual(self.upgrade_now(kind)["status"], "upgraded")
                for path in legacy:
                    path.unlink()
                self.assertEqual(self.upgrade_now(kind)["status"], "up-to-date")

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

    def test_mac_upgrade_copies_linux_bridge_and_regenerates_plist(self):
        paths, old, values = self.install("mac")
        config_before = paths["config"].read_bytes()
        result = self.upgrade_now("mac")
        self.assertEqual(result["status"], "upgraded")
        self.assertEqual((paths["target"] / "bridge.py").read_bytes(), (ROOT / "linux/bridge.py").read_bytes())
        self.assertEqual(paths["config"].read_bytes(), config_before)
        self.assertEqual(result["restarted"], ["bridge"])
        plist = plistlib.loads((paths["launch"] / "io.monocloud-neko.bridge.plist").read_bytes())
        self.assertEqual(plist["Label"], "io.monocloud-neko.bridge")
        self.assertEqual(plist["ProgramArguments"][-1], "bridge")
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
        self.write_config(paths, "linux")
        with self.assertRaises(ValueError) as caught:
            self.upgrade_now()
        self.assertEqual(str(caught.exception), "installation_not_found")

    def test_legacy_install_requires_adopt_and_can_be_adopted(self):
        paths, old, _ = self.install()
        (paths["state"] / "install-state.json").unlink()
        with self.assertRaises(ValueError) as caught:
            self.upgrade_now()
        self.assertEqual(str(caught.exception), "installation_not_found")
        result = self.upgrade_now(adopt=True)
        self.assertEqual(result["status"], "upgraded")
        self.assertTrue(result["adopted"])
        self.assertEqual((paths["target"] / "bridge.py").read_bytes(), (ROOT / "linux/bridge.py").read_bytes())
        self.assertFalse((paths["state"] / "install-state.json").exists())
        self.assertEqual(self.upgrade_now(adopt=True)["status"], "up-to-date")

    def test_adopt_rollback_removes_files_it_created(self):
        paths, old, _ = self.install()
        unit = paths["units"] / "monocloud-neko-bridge.service"
        unit.unlink()
        (paths["state"] / "install-state.json").unlink()
        self.probe_error = ValueError("health_check_timeout")
        with self.assertRaises(self.upgrade.UpgradeError) as caught:
            self.upgrade_now(adopt=True)
        self.assertEqual(caught.exception.code, "health_check_timeout")
        self.assertFalse(unit.exists())
        self.assertEqual((paths["target"] / "bridge.py").read_bytes(), old[paths["target"] / "bridge.py"])
        self.assertFalse((paths["state"] / "install-state.json").exists())

    def test_adopt_does_not_bypass_journal_verification(self):
        paths, old, _ = self.install()
        (paths["target"] / "bridge.py").write_bytes(b"locally edited")
        with self.assertRaises(ValueError) as caught:
            self.upgrade_now(adopt=True)
        self.assertEqual(str(caught.exception), "existing_installation_file_conflict")
        self.assertEqual(self.run.calls, [])
        self.assertEqual((paths["target"] / "bridge.py").read_bytes(), b"locally edited")

    def test_adopt_dry_run_reports_adoption_without_writing(self):
        paths, old, _ = self.install()
        (paths["state"] / "install-state.json").unlink()
        result = self.upgrade_now(adopt=True, dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertTrue(result["adopted"])
        self.assertEqual((paths["target"] / "bridge.py").read_bytes(), old[paths["target"] / "bridge.py"])
        self.assertFalse((self.home / ".local/state/monocloud-neko-upgrade").exists())

    def test_mac_adopt_requires_readable_plist(self):
        paths, old, _ = self.install("mac")
        (paths["state"] / "install-state.json").unlink()
        (paths["launch"] / "io.monocloud-neko.bridge.plist").write_bytes(b"not a plist")
        with self.assertRaises(ValueError) as caught:
            self.upgrade_now("mac", adopt=True)
        self.assertEqual(str(caught.exception), "persistent_python_not_recoverable")
        self.assertEqual(self.run.calls, [])
        self.assertEqual((paths["target"] / "bridge.py").read_bytes(), old[paths["target"] / "bridge.py"])

    def test_adopt_requires_existing_installation_files(self):
        paths = self.upgrade.installation(self.home, "linux")
        paths["state"].mkdir(parents=True)
        self.write_config(paths, "linux")
        with self.assertRaises(ValueError) as caught:
            self.upgrade_now(adopt=True)
        self.assertEqual(str(caught.exception), "legacy_installation_incomplete")
        self.assertEqual(self.run.calls, [])
        self.assertFalse((paths["target"] / "bridge.py").exists())


if __name__ == "__main__":
    unittest.main()
