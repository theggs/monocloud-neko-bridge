import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"deploy"))

def module(name,filename):
    spec=importlib.util.spec_from_file_location(name,ROOT/"deploy"/filename)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result

class FakeMaster:
    def __init__(self):self.rows=[];self.posts=0;self.fail_after_create=False;self.calls=0
    def __call__(self,path,data=None):
        self.calls+=1
        if data is None:return self.rows.copy()
        self.posts+=1
        row=dict(data,id=17);row.pop("token",None);self.rows.append(row)
        if self.fail_after_create:
            self.fail_after_create=False;raise TimeoutError("simulated lost response")
        return {"id":17}

class InstallTransactions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.linux=module("transaction_linux","install-linux.py")
        cls.mac=module("transaction_mac","install-mac.py")

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name)
        self.api=FakeMaster();self.fetches=0;self.fail_verify=False
        self.args=argparse.Namespace(master_url="https://neko.example.invalid",backend_name="synthetic-device",python=sys.executable,download_proxy=None,admin_token_file=None,allow_insecure_http=False,repair=False)

    def tearDown(self):self.temp.cleanup()

    def fetch(self,*args,**kwargs):
        self.fetches+=1;return b"synthetic-binary", "a"*64

    def runner(self,args,**kwargs):
        if self.fail_verify and args[0]=="systemd-analyze":raise subprocess.CalledProcessError(1,args)
        return subprocess.CompletedProcess(args,0,stdout="1.4.8\n",stderr="")

    def linux_config(self):
        p=self.home/".config/monocloud-neko-bridge/agent.env";p.parent.mkdir(parents=True,mode=0o700)
        p.write_text("NEKO_SERVER=https://neko.example.invalid\nNEKO_BACKEND_ID=1\nNEKO_BACKEND_TOKEN=synthetic-backend-token\nNEKO_GATEWAY_TYPE=clash\nNEKO_GATEWAY_URL=http://127.0.0.1:19092\nNEKO_GATEWAY_TOKEN=synthetic-gateway-token\n");p.chmod(0o600)
        return p

    def install_mac(self):return self.mac.install(self.args,home=self.home,source=ROOT,api=self.api,fetch=self.fetch,run=self.runner)
    def install_linux(self,repair=False):return self.linux.install(home=self.home,source=ROOT,fetch=self.fetch,run=self.runner,repair=repair)

    def test_linux_verification_failure_can_be_repaired_without_published_binary(self):
        self.linux_config();self.fail_verify=True
        with self.assertRaises(subprocess.CalledProcessError):self.install_linux()
        self.assertFalse((self.home/".local/lib/monocloud-neko-bridge/neko-agent").exists())
        self.fail_verify=False;self.install_linux(repair=True)
        self.assertTrue((self.home/".local/lib/monocloud-neko-bridge/release.txt").exists())

    def test_linux_repair_allows_corrected_private_config(self):
        config=self.linux_config();self.fail_verify=True
        with self.assertRaises(subprocess.CalledProcessError):self.install_linux()
        config.write_text(config.read_text().replace("synthetic-gateway-token","corrected-synthetic-token"))
        self.fail_verify=False;self.install_linux(repair=True)
        self.assertTrue((self.home/".local/lib/monocloud-neko-bridge/release.txt").exists())

    def test_linux_rejects_unusable_service_python_before_download(self):
        self.linux_config();original=self.runner
        def unsupported(args,**kwargs):
            if args[0]=="/usr/bin/python3":raise subprocess.CalledProcessError(1,args)
            return original(args,**kwargs)
        self.runner=unsupported
        with self.assertRaisesRegex(ValueError,"unsupported_service_python"):
            self.install_linux()
        self.assertEqual(self.fetches,0)
        self.assertFalse((self.home/".local/lib/monocloud-neko-bridge").exists())

    def test_linux_conflicting_unit_is_preserved_before_download(self):
        self.linux_config();p=self.home/".config/systemd/user/monocloud-neko-agent.service";p.parent.mkdir(parents=True);p.write_text("existing")
        with self.assertRaises(ValueError):self.install_linux()
        self.assertEqual(p.read_text(),"existing");self.assertEqual(self.fetches,0)

    def test_mac_lost_create_response_resumes_one_backend_and_same_secret(self):
        self.api.fail_after_create=True
        with self.assertRaises(TimeoutError):self.install_mac()
        cfg=self.home/".config/monocloud-neko-bridge-mac/agent.env"
        self.assertFalse(cfg.exists())
        state=cfg.parent/"install-state.json";self.assertEqual(state.stat().st_mode&0o777,0o600)
        original=state.read_text();self.args.repair=True;self.install_mac()
        self.assertEqual(self.api.posts,1);self.assertIn("NEKO_BACKEND_ID=17",cfg.read_text())
        import json,shlex
        intent=json.loads(original)["intent"]
        self.assertIn(intent["backend_token"],cfg.read_text())
        self.assertEqual(cfg.stat().st_mode&0o777,0o600)

    def test_mac_conflicting_plist_prevents_any_remote_operation(self):
        p=self.home/"Library/LaunchAgents/io.monocloud-neko.bridge.plist";p.parent.mkdir(parents=True);p.write_text("existing")
        with self.assertRaises(ValueError):self.install_mac()
        self.assertEqual(self.api.calls,0);self.assertEqual(self.fetches,0);self.assertEqual(p.read_text(),"existing")

    def test_mac_precreate_name_conflict_can_be_corrected(self):
        self.api.rows=[{"id":9,"name":"synthetic-device","url":"agent://another-install"}]
        with self.assertRaises(ValueError):self.install_mac()
        self.args.backend_name="corrected-device";self.args.repair=True
        self.install_mac();self.assertEqual(self.api.posts,1)

    def test_mac_precreate_unreachable_master_can_be_corrected(self):
        api=self.api
        def unreachable(*args,**kwargs):raise OSError("synthetic unavailable")
        self.api=unreachable
        with self.assertRaises(OSError):self.install_mac()
        self.api=api;self.args.master_url="https://corrected.example.invalid";self.args.repair=True
        self.install_mac();self.assertEqual(api.posts,1)

    def test_mac_repair_rejects_changed_install_identity(self):
        self.api.fail_after_create=True
        with self.assertRaises(TimeoutError):self.install_mac()
        self.args.repair=True;self.args.backend_name="another-device"
        with self.assertRaises(ValueError):self.install_mac()
        self.assertEqual(self.api.posts,1)

    def test_mac_partial_file_publish_can_resume(self):
        import install_support
        original=install_support.InstallState.publish
        fired=[]
        def interrupted(state,path,content,mode=0o644):
            original(state,path,content,mode)
            if not fired:fired.append(True);raise OSError("simulated publication failure")
        with patch.object(install_support.InstallState,"publish",interrupted):
            with self.assertRaises(OSError):self.install_mac()
        self.args.repair=True;self.install_mac()
        self.assertEqual(self.api.posts,1)
        self.assertTrue((self.home/"Library/LaunchAgents/io.monocloud-neko.agent.plist").exists())

    def test_mac_unrelated_same_name_backend_is_never_adopted(self):
        self.api.rows=[{"id":9,"name":"synthetic-device","url":"agent://someone-else"}]
        with self.assertRaises(ValueError):self.install_mac()
        self.assertEqual(self.api.posts,0)

    def test_completed_repair_refuses_modified_owned_file(self):
        self.install_mac();p=self.home/".local/lib/monocloud-neko-bridge-mac/adapter.py"
        p.write_text("user changed this file")
        self.args.repair=True
        with self.assertRaises(ValueError):self.install_mac()
        self.assertEqual(p.read_text(),"user changed this file")

    def test_malformed_creation_response_recovers_using_unique_intent(self):
        original=self.api
        def bad_response(path,data=None):
            result=original(path,data)
            return {} if data is not None else result
        self.api=bad_response
        with self.assertRaises(ValueError):self.install_mac()
        self.api=original;self.args.repair=True;self.install_mac()
        self.assertEqual(original.posts,1)

if __name__=="__main__":unittest.main()
