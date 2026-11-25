import json, tempfile, threading, time, unittest, urllib.request
from pathlib import Path
import paper
from app import App,handler,TOKEN
from core_host import Core
from http.server import ThreadingHTTPServer
Q={"last":100,"ask":101,"bid":99,"ts":time.time()}
class PaperTest(unittest.TestCase):
    def test_long_close(self):
        s=paper.initial(); p=paper.order(s,dict(symbol="BTCUSDT",side="long",kind="market",margin=100,leverage=3),Q)
        self.assertAlmostEqual(p["qty"],300/101)
        self.assertAlmostEqual(paper.available(s),9899.82)
        paper.close(s,p["id"],{"bid":110,"ask":111})
        self.assertGreater(s["history"][0]["pnl"],0)
        self.assertFalse(s["positions"])
    def test_limit_and_insufficient(self):
        s=paper.initial(); d=dict(symbol="BTCUSDT",side="long",kind="limit",margin=100,leverage=3,limit=95)
        p=paper.order(s,d,Q); self.assertEqual(len(s["pending"]),1)
        paper.tick(s,{"BTCUSDT":{"last":94,"ask":95,"bid":94}})
        self.assertEqual(s["positions"][0]["id"],p["id"]); self.assertFalse(s["pending"])
        with self.assertRaises(ValueError):paper.order(s,{**d,"margin":1e8},Q)
        with self.assertRaises(ValueError):paper.order(s,{**d,"margin":float("nan")},Q)
    def test_bots(self):
        s=paper.initial()
        b=paper.new_bot(s,dict(name="Test",symbol="BTCUSDT",strategy="Grid",deposit=1000,lower=90,upper=110,levels=10,sl=10,tp=10))
        paper.bot_action(s,b["id"],"toggle",Q)
        for p in (105,99,105):paper.tick(s,{"BTCUSDT":{"last":p,"bid":p-.01,"ask":p+.01}})
        self.assertGreaterEqual(b["trades"],2)
        paper.bot_action(s,b["id"],"delete",Q); self.assertFalse(s["bots"])
    def test_basis_two_legs(self):
        s=paper.initial()
        b=paper.new_bot(s,dict(name="Hedge",symbol="BTCUSDT",strategy="Basis",deposit=1000,lower=80,upper=120,levels=10,sl=10,tp=10,basis_entry=.5,basis_exit=.1))
        q={"last":102,"ask":102.1,"bid":102,"spot_bid":99.9,"spot_ask":100,"spot_ts":time.time()}
        with self.assertRaises(ValueError): paper.bot_action(s,b["id"],"toggle",Q)
        paper.bot_action(s,b["id"],"toggle",q);paper.tick(s,{"BTCUSDT":q})
        self.assertIsNotNone(b["basis_position"]);self.assertEqual(b["trades"],2)
        q.update(last=100,ask=100.05,bid=100,spot_bid=100,spot_ask=100.01);paper.tick(s,{"BTCUSDT":q})
        self.assertIsNone(b["basis_position"]);self.assertEqual(b["trades"],4);self.assertGreater(b["pnl"],0)
        paper.bot_action(s,b["id"],"delete",q);self.assertGreater(s["balance"],10000)
    def test_zip_traversal(self):
        import zipfile,hashlib,io
        with tempfile.TemporaryDirectory() as tmp:
            c=Core(tmp); f=io.BytesIO()
            with zipfile.ZipFile(f,"w") as z:z.writestr("../evil.py","x")
            blob=f.getvalue()
            with self.assertRaises(ValueError):c.install(blob,hashlib.sha256(blob).hexdigest())
            self.assertFalse((Path(tmp)/"evil.py").exists())
class AIModelTest(unittest.TestCase):
    def test_explicit_configuration_and_structured_response(self):
        from unittest.mock import patch
        from ai_provider import AI
        a=AI()
        with self.assertRaises(ValueError):a.analyze("Тест",[],{})
        a.configure("openai","test-model","test-secret")
        self.assertNotIn("test-secret",json.dumps(a.status()))
        answer=dict(bias="neutral",summary="insufficient data",levels=[],checklist=["check"],risk="paper",impact="uncertain")
        with patch("ai_provider.request",return_value={"choices":[{"message":{"content":json.dumps(answer)}}]}) as m:
            r=a.analyze("Тест",[],{});self.assertEqual(r["response"]["bias"],"neutral")
            self.assertEqual(m.call_args.args[2],"test-secret")
        a.configure("none","","");self.assertFalse(a.status()["ready"])
class ApiTest(unittest.TestCase):
    def test_http(self):
        with tempfile.TemporaryDirectory() as d:
            a=App(d); server=ThreadingHTTPServer(("127.0.0.1",0),handler(a)); t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
            try:
                url="http://127.0.0.1:"+str(server.server_port)
                with urllib.request.urlopen(url+"/",timeout=5) as f:self.assertIn(b"enaverse",f.read())
                with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(url+"/api/state",timeout=5)
                self.assertEqual(cm.exception.code,403)
                def req(path,data=None):
                    h={"X-Enaverse-Token":TOKEN,"Content-Type":"application/json"}
                    with urllib.request.urlopen(urllib.request.Request(url+path,data=json.dumps(data).encode() if data else None,headers=h),timeout=5) as f:return json.load(f)
                state=req("/api/state"); self.assertEqual(state["available"],10000); self.assertIsNone(state["quotes"]["BTCUSDT"])
                req("/api/action",{"action":"settings","key":"theme","value":"light"});self.assertEqual(req("/api/state")["state"]["settings"]["theme"],"light")
                a.quotes["BTCUSDT"]={**Q,"ts":time.time()}
                req("/api/action",{"action":"order","symbol":"BTCUSDT","side":"long","kind":"market","margin":100,"leverage":1})
                self.assertEqual(len(req("/api/state")["state"]["positions"]),1)
                a.news=[{"id":"https://example.com/news","title":"Test article"}]
                req("/api/action",{"action":"save_news","id":"https://example.com/news"});a.news=[]
                self.assertEqual(req("/api/state")["news"][0]["title"],"Test article")
                req("/api/action",{"action":"reset"});self.assertEqual(req("/api/state")["available"],10000)
            finally:server.shutdown();server.server_close();t.join()
if __name__=="__main__":unittest.main()
