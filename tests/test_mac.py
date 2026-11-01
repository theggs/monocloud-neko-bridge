import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]

class MacTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert (ROOT / "mac/adapter.py").exists(), "Mac adapter implementation is missing"
        sys.path.insert(0, str(ROOT / "linux"))
        sys.path.insert(0, str(ROOT / "mac"))
        import adapter
        cls.m = adapter

    def row(self, **extra):
        row = {"id": 7, "t_start": 10.5, "t_close": -1, "tx": 123, "rx": 456,
               "host": "example.com", "ipAddr": "192.0.2.1", "fromHost": "127.0.0.1",
               "serverHost": "198.51.100.9", "selectedAdapterName": "Node", "selectedPolicyName": "Policy",
               "policyName": "Policy", "matchedRuleType": "DOMAIN-SUFFIX", "matchedRuleString": "example.com",
               "processName": "private", "headerString": "secret"}
        row.update(extra); return row

    def cache(self, **kw):
        self.now = 0
        c = self.m.FlowCache(clock=lambda: self.now, **kw)
        c.begin_session(1700000000); return c

    def test_mapping_preserves_counters_and_engine_relative_time(self):
        c = self.cache(); c.snapshot([self.row()], False)
        row = c.payload("/connections")["connections"][0]
        self.assertEqual((row["upload"], row["download"]), (123,456))
        self.assertEqual(row["start"], "2023-11-14T22:13:30.500000Z")
        self.assertEqual(row["chains"], ["Node", "Policy"])
        self.assertEqual(row["metadata"]["destinationIP"], "192.0.2.1")
        self.assertNotIn("processName", json.dumps(row)); self.assertNotIn("secret", json.dumps(row))
        self.assertEqual(c.payload("/connections")["connections"][0], row)

    def test_pending_metadata_is_withheld_until_final_event(self):
        c=self.cache()
        c.snapshot([self.row(host="",ipAddr="",selectedAdapterName="",selectedPolicyName="",policyName="",matchedRuleType="",tx=0,rx=0)],False)
        self.assertEqual(c.payload("/connections")["connections"],[])
        c.update(self.row(tx=200,rx=1000,t_close=20))
        row=c.payload("/connections")["connections"][0]
        self.assertEqual(row["metadata"]["host"],"example.com")
        self.assertEqual((row["upload"],row["download"]),(200,1000))

    def test_ready_metadata_survives_empty_push_and_snapshot(self):
        for kind in ("push", "snapshot"):
            with self.subTest(kind=kind):
                c=self.cache();c.snapshot([self.row()],False)
                first=c.payload("/connections")["connections"][0]
                sparse=self.row(host="",ipAddr="",fromHost="",selectedAdapterName="",selectedPolicyName="",policyName="",matchedRuleType="UNKNOWN",matchedRuleString="",tx=200,rx=900)
                if kind=="push":
                    self.m.Collector(c).handle({"t":51,"d":{"tunnel_info":sparse}})
                else:
                    c.snapshot([sparse],False)
                current=c.payload("/connections")["connections"][0]
                self.assertEqual(current["id"],first["id"])
                self.assertEqual(current["metadata"],first["metadata"])
                self.assertEqual(current["chains"],["Node","Policy"])
                self.assertEqual((current["rule"],current["rulePayload"]),("DOMAIN-SUFFIX","example.com"))
                self.assertEqual((current["upload"],current["download"]),(200,900))

    def test_nonempty_metadata_changes_are_not_hidden_by_old_values(self):
        c=self.cache();c.snapshot([self.row()],False)
        c.update(self.row(host="changed.example",ipAddr="192.0.2.2",selectedAdapterName="Other",selectedPolicyName="NewPolicy",policyName="NewPolicy",matchedRuleType="IP-CIDR",matchedRuleString="",tx=200,rx=900))
        row=c.payload("/connections")["connections"][0]
        self.assertEqual(row["metadata"]["host"],"changed.example")
        self.assertEqual(row["metadata"]["destinationIP"],"192.0.2.2")
        self.assertEqual(row["chains"],["Other","NewPolicy"])
        self.assertEqual((row["rule"],row["rulePayload"]),("IP-CIDR",""))

    def test_time_bounds_reject_wrong_units_without_using_wallclock(self):
        for epoch in (1700000000000, 10000000000, 10**400):
            with self.subTest(epoch=epoch), self.assertRaisesRegex(ValueError,"invalid_engine_epoch"):
                self.m.FlowCache().begin_session(epoch)
        for field,value,reason in (("t_start",10**400,"invalid_start"),("t_close",10**400,"invalid_close"),("t_start",1700000000,"invalid_start"),("t_start",True,"invalid_start"),("t_close",1700000000,"invalid_close"),("t_close",5,"invalid_close")):
            with self.subTest(field=field,value=value), self.assertRaisesRegex(ValueError,reason):
                self.m.convert(self.row(**{field:value}),1700000000)
        c=self.cache();c.snapshot([self.row(t_start=86400*365,t_close=86400*365+1)],False)
        self.assertEqual(len(c.payload("/connections")["connections"]),1)

    def test_original_failure_reason_survives_snapshot_age(self):
        c=self.cache();c.snapshot([self.row()],False)
        c.unavailable("partial_snapshot");self.now=1000
        self.assertEqual(c.health()["reason"],"partial_snapshot")

    def test_health_threshold_tracks_long_collector_interval(self):
        c=self.cache();self.m.Collector(c,interval=60)
        c.snapshot([self.row()],False);self.now=120
        self.assertEqual(c.health()["status"],"healthy")
        self.now=181
        self.assertEqual(c.health()["reason"],"snapshot_stale")

    def test_rules_target_observed_policy_group_and_keep_partial_marker(self):
        c=self.cache();c.snapshot([self.row()],False)
        rules=c.payload("/rules")
        self.assertEqual(rules["rules"][0]["proxy"],"Policy")
        self.assertTrue(rules["_partial"])
        self.assertEqual(rules["_source"],"observed-connections")

    def test_missing_pending_snapshot_waits_for_real_terminal_event(self):
        c=self.cache()
        c.snapshot([self.row(host="",ipAddr="",selectedAdapterName="",selectedPolicyName="",policyName="",matchedRuleType="",tx=0,rx=0)],False)
        c.snapshot([],False)
        self.assertEqual(c.payload("/connections")["connections"],[])
        c.update(self.row(tx=200,rx=1000,t_close=20))
        self.assertEqual(c.payload("/connections")["connections"][0]["metadata"]["host"],"example.com")

    def test_real_close_after_missing_gets_full_retention(self):
        c=self.cache(retention=120);c.snapshot([self.row()],False);c.snapshot([],False)
        self.now=100;c.update(self.row(tx=200,rx=1000,t_close=20))
        self.now=121;c.snapshot([],False)
        self.assertEqual(len(c.payload("/connections")["connections"]),1)
        self.now=221;c.snapshot([],False)
        self.assertEqual(c.payload("/connections")["connections"],[])

    def test_never_substitutes_proxy_ip_or_guesses_direct(self):
        c=self.cache();c.snapshot([self.row(ipAddr="", selectedAdapterName="",selectedPolicyName="",policyName="",t_close=20)],False)
        row=c.payload("/connections")["connections"][0]
        self.assertEqual(row["metadata"]["destinationIP"], "")
        self.assertEqual(row["chains"], ["UNKNOWN"])

    def test_closed_connection_final_value_retained_then_expires(self):
        c=self.cache(retention=120);c.snapshot([self.row()],False)
        c.update(self.row(tx=150,rx=900,t_close=20))
        self.now=119;c.snapshot([],False);self.assertEqual(c.payload("/connections")["connections"][0]["download"],900)
        self.now=121;c.snapshot([],False);self.assertEqual(c.payload("/connections")["connections"],[])

    def test_session_and_connection_reuse_cannot_reuse_id(self):
        c=self.cache();c.snapshot([self.row()],False);first=c.payload("/connections")["connections"][0]["id"]
        c.snapshot([self.row(t_start=30)],False)
        self.assertNotEqual(c.payload("/connections")["connections"][0]["id"],first)
        c.begin_session(1700000100);c.snapshot([self.row()],False)
        self.assertNotEqual(c.payload("/connections")["connections"][0]["id"],first)

    def test_disconnect_and_partial_snapshot_are_unavailable(self):
        c=self.cache();c.snapshot([self.row()],False);c.unavailable("disconnected")
        with self.assertRaises(self.m.Unavailable):c.payload("/connections")
        with self.assertRaises(self.m.Unavailable):c.snapshot([self.row()],True)
        c.snapshot([self.row()],False);self.assertEqual(len(c.payload("/connections")["connections"]),1)

    def test_overflow_and_stale_snapshot_are_explicit_errors(self):
        c=self.cache(max_entries=1);c.snapshot([self.row()],False)
        with self.assertRaises(self.m.Unavailable):c.update(self.row(id=8))
        with self.assertRaises(self.m.Unavailable):c.payload("/connections")
        c=self.cache();c.snapshot([self.row()],False);self.now=31
        with self.assertRaises(self.m.Unavailable):c.payload("/connections")

    def test_incremental_53_does_not_retire_other_active_connections(self):
        c=self.cache();c.snapshot([self.row(id=7),self.row(id=8)],False)
        collector=self.m.Collector(c)
        collector.handle({"t":53,"d":{"reset":False,"has_more":False,"tunnels":[self.row(id=7,rx=500)]}})
        c.update(self.row(id=8,rx=900))
        rows={x["id"].split(":")[2]:x for x in c.payload("/connections")["connections"]}
        self.assertEqual(rows["8"]["download"],900)

    def test_invalid_push_data_is_recoverable_value_error(self):
        c=self.cache();collector=self.m.Collector(c)
        for packet in ({"t":53,"d":"invalid"},{"t":51,"d":[1]}):
            with self.assertRaises(ValueError):collector.handle(packet)

    def test_auxiliary_data_is_observed_subset_with_no_fake_provider(self):
        c=self.cache();c.snapshot([self.row()],False)
        rules=c.payload("/rules");proxies=c.payload("/proxies")
        self.assertTrue(rules["_partial"]);self.assertTrue(proxies["_partial"])
        self.assertEqual(rules["rules"][0]["payload"],"example.com")
        self.assertIn("Node",proxies["proxies"])
        self.assertEqual(c.payload("/providers/proxies")["providers"],{})


class MacSocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "linux")); sys.path.insert(0, str(ROOT / "mac"))
        import adapter, protocol
        cls.m, cls.protocol = adapter, protocol

    def setUp(self):
        import socketserver, tempfile, threading, base64
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + "/api.sock"
        self.connections = 0
        self.bad_once = False
        self.deep_once = False
        owner = self
        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
        class Peer(socketserver.BaseRequestHandler):
            def send(self, obj):
                data=base64.b64encode(json.dumps(obj).encode())+b"\xdb\xbc"
                self.request.sendall(data[:3]); self.request.sendall(data[3:])
            def read(self):
                while b"\xdb\xbc" not in self.buffer:
                    data=self.request.recv(65536)
                    if not data: raise EOFError()
                    self.buffer+=data
                data,self.buffer=self.buffer.split(b"\xdb\xbc",1)
                return json.loads(base64.b64decode(data))
            def handle(self):
                import time
                owner.connections+=1
                number=owner.connections;self.buffer=b""
                try:
                    if owner.deep_once and number==1:
                        # Valid JSON nesting exceeds the decoder stack, causing
                        # a real RecursionError rather than a mocked exception.
                        nested=b'{"nested":'+b'['*1_000_000+b'0'+b']'*1_000_000+b'}'
                        self.request.sendall(base64.b64encode(nested)+b"\xdb\xbc")
                        return
                    self.send({"v":1,"c":1});self.read();self.send({"v":1,"c":2});self.read()
                    self.send({"v":1,"c":1,"t":1,"d":{"service_start_time":1700000000}})
                    while True:
                        req=self.read();command=req.get("t")
                        if command==14:
                            if owner.bad_once and number==1:
                                self.send({"v":1,"t":53,"d":"invalid"});return
                            self.send({"v":1,"t":51,"d":{"tunnel_info":json.dumps(owner.row())}})
                            self.send({"v":1,"i":req["i"],"t":14,"c":1,"d":{}})
                        elif command==7:
                            row=owner.row()
                            if number>1:row.update(tx=150,rx=900,t_close=20)
                            self.send({"v":1,"i":req["i"],"t":7,"c":1,"d":{"tunnels":[json.dumps(row)],"has_more":False}})
                            if number==1:
                                time.sleep(.05);row.update(tx=150,rx=900,t_close=20)
                                self.send({"v":1,"t":51,"d":{"tunnel_info":json.dumps(row)}})
                                return
                except (EOFError,OSError):pass
        self.server=Server(self.path,Peer)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def row(self):
        return {"id":7,"t_start":10.5,"t_close":-1,"tx":123,"rx":456,"host":"example.com","ipAddr":"192.0.2.1","fromHost":"127.0.0.1","selectedAdapterName":"DIRECT","matchedRuleType":"DOMAIN","matchedRuleString":"example.com"}

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def test_fragmented_handshake_interleaved_push_and_command_guard(self):
        pushes=[]
        with self.protocol.Client(self.path) as c:
            self.assertEqual(c.info["d"]["service_start_time"],1700000000)
            c.request(14,{"switch":True},on_push=pushes.append)
            self.assertEqual(pushes[0]["t"],51)
            self.assertFalse(c.request(7)["has_more"])
            with self.assertRaises(ValueError):c.request(2)
            with self.assertRaises(ValueError):c.request(14,{"switch":False})

    def test_collector_disconnects_and_recovers_with_final_counter(self):
        import time
        cache=self.m.FlowCache()
        collector=self.m.Collector(cache,self.path,interval=.2,retry=.2)
        collector.start();deadline=time.monotonic()+4
        saw_up=False;saw_down=False
        try:
            while time.monotonic()<deadline:
                state=cache.health()["status"]
                if state=="healthy" and self.connections==1:saw_up=True
                if saw_up and state=="degraded":saw_down=True
                if self.connections>=2 and cache.health()["status"]=="healthy":break
                time.sleep(.01)
            self.assertTrue(saw_up)
            self.assertTrue(saw_down)
            self.assertGreaterEqual(self.connections,2)
            self.assertEqual(cache.payload("/connections")["connections"][0]["download"],900)
        finally:
            collector.stop_event.set();collector.join(timeout=3)
        self.assertFalse(collector.is_alive())

    def test_bad_frame_does_not_kill_collector_recovery(self):
        import time
        self.bad_once=True;cache=self.m.FlowCache()
        collector=self.m.Collector(cache,self.path,interval=.2,retry=.05)
        collector.start();deadline=time.monotonic()+4
        try:
            while time.monotonic()<deadline:
                if self.connections>=2 and cache.health()["status"]=="healthy":break
                time.sleep(.01)
            self.assertTrue(collector.is_alive())
            self.assertGreaterEqual(self.connections,2)
            self.assertEqual(cache.health()["status"],"healthy")
        finally:
            collector.stop_event.set();collector.join(timeout=3)

    def test_collector_retains_structured_failure_reason(self):
        import time
        from unittest.mock import patch
        class BrokenClient:
            def __init__(self, *args): pass
            def __enter__(self): raise self_error
            def __exit__(self, *args): pass
        for reason in ("partial_snapshot", "cache_capacity", "counter_regressed"):
            self_error=self.m.Unavailable(reason)
            cache=self.m.FlowCache()
            collector=self.m.Collector(cache,retry=.01)
            with patch.object(self.m,"Client",BrokenClient):
                collector.start();deadline=time.monotonic()+1
                while cache.reason=="starting" and time.monotonic()<deadline:time.sleep(.01)
                collector.stop_event.set();collector.join(timeout=1)
            self.assertEqual(cache.reason,reason)

    def test_unexpected_parser_failure_degrades_then_recovers(self):
        import time
        self.deep_once=True
        cache=self.m.FlowCache();collector=self.m.Collector(cache,self.path,interval=.2,retry=.1)
        collector.start();deadline=time.monotonic()+4
        try:
            while cache.reason=="starting" and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(cache.reason,"collector_internal_error")
            while cache.health()["status"]!="healthy" and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(collector.is_alive())
            self.assertEqual(cache.health()["status"],"healthy")
        finally:
            collector.stop_event.set();collector.join(timeout=3)

    def test_time_validation_failure_has_independent_health_reason(self):
        import time
        from unittest.mock import patch
        class InvalidTimeClient:
            def __init__(self,*args):pass
            def __enter__(self):raise ValueError("invalid_engine_epoch")
            def __exit__(self,*args):pass
        cache=self.m.FlowCache();collector=self.m.Collector(cache,retry=.01)
        with patch.object(self.m,"Client",InvalidTimeClient):
            collector.start();deadline=time.monotonic()+1
            while cache.reason=="starting" and time.monotonic()<deadline:time.sleep(.005)
            collector.stop_event.set();collector.join(timeout=1)
        self.assertEqual(cache.health()["reason"],"invalid_engine_epoch")

    def test_mac_http_bounds_serialized_response_before_success(self):
        import http.client, threading
        cache=self.m.FlowCache();cache.begin_session(1700000000);cache.snapshot([self.row()],False)
        server=self.m.make_server(cache,"test-token",port=0,max_response_bytes=256)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            c=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            c.request("GET","/connections",headers={"Authorization":"Bearer test-token"})
            response=c.getresponse()
            self.assertEqual(response.status,503)
            self.assertEqual(json.loads(response.read()),{"error":"response_limit"})
            self.assertEqual(cache.health()["status"],"healthy")
            c.close()
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_mac_gateway_bind_skips_reverse_dns_lookup(self):
        from unittest.mock import patch
        with patch.object(self.m.socket,"getfqdn",side_effect=AssertionError("reverse DNS lookup")), \
             patch.object(self.m.socket,"gethostbyaddr",side_effect=AssertionError("reverse DNS lookup")), \
             patch.object(self.m.socket,"getnameinfo",side_effect=AssertionError("reverse DNS lookup")):
            server=self.m.make_server(self.m.FlowCache(),"test-token",port=0)
        try:
            self.assertEqual(server.server_name,"127.0.0.1")
            self.assertEqual(server.server_port,server.server_address[1])
        finally:
            server.server_close()

    def test_mac_http_requires_auth_and_rejects_writes(self):
        import http.client,threading
        cache=self.m.FlowCache();cache.begin_session(1700000000);cache.snapshot([self.row()],False)
        server=self.m.make_server(cache,"test-token",port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,token="test-token",method="GET"):
            c=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            c.request(method,path,headers={} if token is None else {"Authorization":"Bearer "+token})
            r=c.getresponse();status=r.status;body=r.read();c.close();return status,body
        try:
            self.assertEqual(request("/connections",None)[0],401)
            self.assertEqual(request("/connections","wrong")[0],401)
            self.assertEqual(request("/connections")[0],200)
            self.assertEqual(request("/connections",method="DELETE")[0],405)
            self.assertEqual(request("/configs")[0],404)
            cache.unavailable("disconnected")
            self.assertEqual(request("/connections")[0],503)
        finally:
            server.shutdown();server.server_close();thread.join()

if __name__ == "__main__":unittest.main()
