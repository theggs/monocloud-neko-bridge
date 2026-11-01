import importlib.util
from pathlib import Path
import tempfile
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"deploy"))

class ManagementApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location("install_mac",ROOT/"deploy/install-mac.py")
        cls.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.module)

    def test_management_authentication_uses_private_token_file(self):
        class Peer(BaseHTTPRequestHandler):
            def do_GET(self):
                valid=self.headers.get("Authorization")=="Bearer synthetic-management-token"
                self.send_response(200 if valid else 401);self.end_headers()
                self.wfile.write(b'{"ok":true}' if valid else b'{}')
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(("127.0.0.1",0),Peer)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as t:
                token=Path(t)/"management.secret";token.write_text("synthetic-management-token");token.chmod(0o600)
                api=self.module.build_api("http://127.0.0.1:"+str(server.server_port),token)
                self.assertEqual(api("/api/backends"),{"ok":True})
                token.chmod(0o644)
                with self.assertRaises(ValueError):self.module.build_api("http://127.0.0.1:"+str(server.server_port),token)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_management_token_never_follows_redirects(self):
        import urllib.error
        received=[]
        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("Authorization"));self.send_response(200);self.end_headers();self.wfile.write(b'{}')
            def log_message(self,*args):pass
        target=ThreadingHTTPServer(("127.0.0.1",0),Target)
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302);self.send_header("Location","http://127.0.0.1:"+str(target.server_port)+"/capture");self.end_headers()
            def log_message(self,*args):pass
        source=ThreadingHTTPServer(("127.0.0.1",0),Redirect)
        threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (target,source)]
        for thread in threads:thread.start()
        try:
            with tempfile.TemporaryDirectory() as t:
                token=Path(t)/"management.secret";token.write_text("synthetic-management-token");token.chmod(0o600)
                api=self.module.build_api("http://127.0.0.1:"+str(source.server_port),token)
                with self.assertRaises(urllib.error.HTTPError) as captured:api("/api/backends")
                captured.exception.close()
                self.assertEqual(received,[])
        finally:
            for server in (source,target):server.shutdown();server.server_close()
            for thread in threads:thread.join()

    def test_master_url_rejects_embedded_credentials_and_queries(self):
        for url in ("file:///etc/passwd","http://user:password@example.invalid","https://example.invalid?token=placeholder"):
            with self.assertRaises(ValueError):self.module.build_api(url)

if __name__=="__main__":unittest.main()
