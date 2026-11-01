import importlib.util
from pathlib import Path
import os
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p=ROOT/"mac/launcher.py"
        assert p.exists(), "Mac service launcher is missing"
        spec=importlib.util.spec_from_file_location("mac_launcher",p)
        cls.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.m)

    def test_configuration_permissions_and_literal_values(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"agent.env";p.write_text("NEKO_BACKEND_ID=6\nNEKO_GATEWAY_TOKEN=test-token\n")
            p.chmod(0o600);c=self.m.load_config(p)
            self.assertEqual(c["NEKO_BACKEND_ID"],"6")
            self.assertEqual(c["NEKO_GATEWAY_TOKEN"],"test-token")
            p.chmod(0o644)
            with self.assertRaises(ValueError):self.m.load_config(p)

    def test_child_exit_propagates_and_logs_are_rotated(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"agent.log"
            rc=self.m.run_logged([sys.executable,"-c","[print('synthetic-line') for _ in range(100)];raise SystemExit(3)"],os.environ.copy(),p,max_bytes=128)
            self.assertEqual(rc,3)
            self.assertTrue(p.exists());self.assertTrue(Path(str(p)+".1").exists())
            self.assertLessEqual(len(list(Path(t).glob("agent.log*"))),3)

    def test_uncooperative_child_is_killed_within_stop_deadline(self):
        import subprocess,signal,time
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t);pidfile=folder/"child.pid"
            child="import os,signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path("+repr(str(pidfile))+").write_text(str(os.getpid()));time.sleep(60)"
            command=[sys.executable,"-c",child]
            runner="import sys,os;from pathlib import Path;sys.path.insert(0,"+repr(str(ROOT/"mac"))+");from launcher import run_logged;raise SystemExit(run_logged("+repr(command)+",os.environ.copy(),Path("+repr(str(folder/"service.log"))+"),stop_timeout=.2))"
            process=subprocess.Popen([sys.executable,"-c",runner],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            child_pid=None
            try:
                deadline=time.monotonic()+3
                while not pidfile.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue(pidfile.exists(),"Supervisor did not start its child")
                child_pid=int(pidfile.read_text());process.terminate();process.wait(timeout=3)
                with self.assertRaises(ProcessLookupError):os.kill(child_pid,0)
            finally:
                if process.poll() is None:process.kill()
                process.communicate(timeout=3)
                if child_pid is not None:
                    try:os.kill(child_pid,signal.SIGKILL)
                    except ProcessLookupError:pass

    def test_exited_leader_with_inherited_pipe_has_bounded_drain(self):
        import subprocess,signal,time
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t);pidfile=folder/"descendant.pid"
            descendant="import time;time.sleep(60)"
            child=("import subprocess,sys;from pathlib import Path;"
                   "p=subprocess.Popen([sys.executable,'-c',"+repr(descendant)+"]);"
                   "Path("+repr(str(pidfile))+").write_text(str(p.pid))")
            runner=("import sys,os;from pathlib import Path;sys.path.insert(0,"+repr(str(ROOT/"mac"))+");"
                    "from launcher import run_logged;raise SystemExit(run_logged("+repr([sys.executable,"-c",child])+",os.environ.copy(),Path("+repr(str(folder/"service.log"))+"),stop_timeout=.2))")
            process=subprocess.Popen([sys.executable,"-c",runner],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                self.assertEqual(process.wait(timeout=3),0)
            finally:
                if process.poll() is None:process.kill()
                process.communicate(timeout=3)
                if pidfile.exists():
                    try:os.kill(int(pidfile.read_text()),signal.SIGKILL)
                    except ProcessLookupError:pass

    def test_signal_during_spawn_is_forwarded_when_pid_becomes_available(self):
        import subprocess,signal,time
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t);pidfile=folder/"child.pid"
            child="import os,time;from pathlib import Path;Path("+repr(str(pidfile))+").write_text(str(os.getpid()));time.sleep(60)"
            command=[sys.executable,"-c",child]
            runner=("import sys,os,signal,subprocess;from pathlib import Path;sys.path.insert(0,"+repr(str(ROOT/"mac"))+")\n"
                    "import launcher\noriginal=launcher.subprocess.Popen\n"
                    "def spawn(*args,**kwargs):\n child=original(*args,**kwargs)\n Path("+repr(str(pidfile))+").write_text(str(child.pid))\n os.kill(os.getpid(),signal.SIGTERM)\n return child\n"
                    "launcher.subprocess.Popen=spawn\n"
                    "raise SystemExit(launcher.run_logged("+repr(command)+",os.environ.copy(),Path("+repr(str(folder/"service.log"))+"),stop_timeout=.2))")
            process=subprocess.Popen([sys.executable,"-c",runner],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                result=process.wait(timeout=3)
                self.assertEqual(result,(-signal.SIGTERM)%256,"Supervisor failed to forward and reap its child")
                if pidfile.exists():
                    with self.assertRaises(ProcessLookupError):os.kill(int(pidfile.read_text()),0)
            finally:
                if process.poll() is None:process.kill()
                process.communicate(timeout=3)
                if pidfile.exists():
                    try:os.kill(int(pidfile.read_text()),signal.SIGKILL)
                    except ProcessLookupError:pass

if __name__=="__main__":unittest.main()
