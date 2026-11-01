import contextlib
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import install_support


class InstallSupportTests(unittest.TestCase):
    def test_agent_env_targets_official_installer(self):
        values = {"NEKO_SERVER": "https://neko.example.invalid", "NEKO_BACKEND_ID": "3",
                  "NEKO_BACKEND_TOKEN": "synthetic-backend-token", "NEKO_GATEWAY_TYPE": "clash",
                  "NEKO_GATEWAY_URL": "http://127.0.0.1:19092",
                  "NEKO_GATEWAY_TOKEN": "synthetic-gateway-token"}
        home = Path("/home/example")
        linux = install_support.agent_env("linux", values, home)
        self.assertEqual(linux["NEKO_GATEWAY_TOKEN"], "synthetic-gateway-token")
        self.assertEqual(linux["NEKO_AGENT_VERSION"], "agent-v1.4.8")
        self.assertEqual(linux["NEKO_AUTO_START"], "false")
        self.assertEqual(linux["NEKO_INSTALL_DIR"], "/home/example/.local/bin")
        self.assertEqual(install_support.agent_env("mac", values, home)["NEKO_AUTO_START"], "true")

    def test_agent_command_never_prints_tokens(self):
        linux = install_support.agent_command("linux", Path("/home/example"))
        self.assertIn(install_support.AGENT_INSTALL_URL, linux)
        self.assertIn('NEKO_GATEWAY_TOKEN="$NEKO_GATEWAY_TOKEN"', linux)
        self.assertIn('. /home/example/.config/monocloud-neko-bridge/agent.env', linux)
        self.assertIn('NEKO_AUTO_START="false"', linux)
        self.assertIn('export NEKO_AGENT_CONFIG_DIR="$HOME/.config/neko-agent"', linux)
        self.assertIn('NEKO_AGENT_VERSION="agent-v1.4.8"', linux)
        self.assertIn('-o "$tmp" && \\', linux)
        self.assertNotIn("synthetic", linux)
        mac = install_support.agent_command("mac", Path("/home/example"))
        self.assertIn("monocloud-neko-bridge-mac/agent.env", mac)
        self.assertIn('NEKO_AUTO_START="true"', mac)

    def test_agent_install_uses_environment_and_reports_failure(self):
        seen = {}

        def fetch(url):
            return "install-script"

        def run(args, **kwargs):
            seen["args"] = args
            seen["env"] = kwargs["env"]
            seen["input"] = kwargs["input"]
            return SimpleNamespace(returncode=1, stderr="upstream installer detail")

        values = {"NEKO_SERVER": "https://neko.example.invalid", "NEKO_BACKEND_ID": "3",
                  "NEKO_BACKEND_TOKEN": "synthetic-backend-token", "NEKO_GATEWAY_TYPE": "clash",
                  "NEKO_GATEWAY_URL": "http://127.0.0.1:19092",
                  "NEKO_GATEWAY_TOKEN": "synthetic-gateway-token"}
        with patch.dict("os.environ", {"KEEP_ME": "kept", "NEKO_SERVER": "https://caller.invalid"}):
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                with self.assertRaises(ValueError) as caught:
                    install_support.install_agent("linux", values, Path("/home/example"), fetch=fetch, run=run)
        self.assertEqual(str(caught.exception), "agent_install_failed")
        self.assertIn("upstream installer detail", captured.getvalue())
        self.assertEqual(seen["args"], ["sh"])
        self.assertEqual(seen["input"], "install-script")
        self.assertEqual(seen["env"]["NEKO_BACKEND_TOKEN"], "synthetic-backend-token")
        self.assertEqual(seen["env"]["NEKO_SERVER"], "https://neko.example.invalid")
        self.assertEqual(seen["env"]["KEEP_ME"], "kept")

    def test_oversized_agent_installer_is_rejected(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return b"x" * limit

        class Opener:
            def open(self, url, timeout):
                return Response()

        with patch.object(install_support.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaises(ValueError) as caught:
                install_support.fetch_agent_installer()
        self.assertEqual(str(caught.exception), "agent_installer_too_large")

    def test_concurrent_installers_cannot_share_journal(self):
        with tempfile.TemporaryDirectory() as t:
            folder = Path(t) / "private"
            with install_support.InstallState(folder, "test"):
                with self.assertRaisesRegex(ValueError, "installation_locked"):
                    with install_support.InstallState(folder, "test"):
                        pass

    def test_error_codes_never_include_private_or_remote_text(self):
        import urllib.error
        self.assertEqual(install_support.safe_error_code(ValueError("existing_installation_file_conflict")),
                         "existing_installation_file_conflict")
        self.assertEqual(install_support.safe_error_code(ValueError("private path or token")), "ValueError")
        error = urllib.error.HTTPError("https://example.invalid", 401, "secret body", {}, io.BytesIO())
        try:
            self.assertEqual(install_support.safe_error_code(error), "http_401")
        finally:
            error.close()

    def test_atomic_publication_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "file"
            p.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                install_support.atomic_write(p, b"new", exclusive=True)
            self.assertEqual(p.read_bytes(), b"existing")
            self.assertEqual(list(Path(t).iterdir()), [p])


if __name__ == "__main__":
    unittest.main()
