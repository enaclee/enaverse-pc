"""Loopback-only web application; no exchange credentials or live trading."""
import copy,html,json,math,os,secrets,statistics,sys,threading,time,urllib.parse,urllib.request,webbrowser
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree
import paper
from core_host import Core
from ai_provider import AI

ROOT=Path(__file__).resolve().parent
HOME=Path(os.environ.get("ENAVERSE_HOME") or Path(os.environ.get("LOCALAPPDATA") or Path.home()/".local/share")/"enaverse")
TOKEN=secrets.token_urlsafe(32)
def json_get(url):
    req=urllib.request.Request(url,headers={"User-Agent":"enaverse/5.3"})
    with urllib.request.urlopen(req,timeout=9) as f: return json.loads(f.read(8*1024*1024))
def bybit(network,endpoint,**params):
    base="https://api-testnet.bybit.com" if network=="testnet" else "https://api.bybit.com"
    r=json_get(base+"/v5/"+endpoint+"?"+urllib.parse.urlencode(params))
    if r.get("retCode")!=0: raise RuntimeError(r.get("retMsg","Ошибка Bybit"))
    return r["result"]
def quote(row):
    q={k:paper.number(row[v],0 if k in ("volume","oi") else 1e-12) for k,v in dict(last="lastPrice",ask="ask1Price",bid="bid1Price",high="highPrice24h",low="lowPrice24h",volume="turnover24h",oi="openInterest").items()}
    q["change"]=paper.number(row["price24hPcnt"],-1,100)*100
    q["funding"]=paper.number(row["fundingRate"],-1,1)*100
    if q["ask"]<q["bid"]: raise ValueError("Crossed quote")
    q["ts"]=time.time(); return q
def feed_news():
    result=[]
    for source,url in [("Cointelegraph","https://cointelegraph.com/rss"),("CoinDesk","https://www.coindesk.com/arc/outboundfeeds/rss/")]:
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"enaverse/5.3"})
            with urllib.request.urlopen(req,timeout=8) as f: raw=f.read(2*1024*1024)
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper(): continue
            tree=ElementTree.fromstring(raw)
            for n in tree.findall(".//item")[:35]:
                title=n.findtext("title",""); link=n.findtext("link",""); desc=n.findtext("description","")
                if urllib.parse.urlparse(link).scheme!="https" or not title: continue
                import re
                desc=html.unescape(re.sub("<[^>]+>"," ",desc))[:450]
                t=(title+" "+desc).lower()
                cat="Регуляция" if any(x in t for x in ("regulat"," sec ","law","court")) else "DeFi" if any(x in t for x in ("defi","protocol","dex")) else "Ончейн" if any(x in t for x in ("on-chain","onchain","whale","wallet")) else "Рынок"
                result.append(dict(id=link,title=title,description=desc,source=source,time=n.findtext("pubDate",""),category=cat,impact="Не оценено"))
        except Exception: continue
    if not result: raise RuntimeError("Источники RSS недоступны")
    import email.utils
    def stamp(n):
        try: return email.utils.parsedate_to_datetime(n["time"]).timestamp()
        except Exception: return 0
    return sorted(result,key=stamp,reverse=True)
class App:
    def __init__(self,home):
        self.home=Path(home); self.home.mkdir(parents=True,exist_ok=True)
        self.path=self.home/"paper.json"; self.lock=threading.RLock()
        self.state=json.loads(self.path.read_text("utf-8")) if self.path.exists() else paper.initial()
        self.core=Core(self.home); self.ai=AI(); self.quotes={}; self.error="Ожидание Bybit"; self.market_generation=0
        self.stop=threading.Event(); self.news=[]; self.news_error="Ожидание RSS"; self.rates={"USDT":1,"USD":1}
        self.news_at=0
    def save(self):
        tmp=self.path.with_suffix(".tmp"); tmp.write_text(json.dumps(self.state,ensure_ascii=False,allow_nan=False),encoding="utf-8"); os.replace(tmp,self.path)
    def fresh(self,symbol):
        q=self.quotes.get(symbol); return q if q and time.time()-q["ts"]<=15 else None
    def market_loop(self):
        while not self.stop.is_set():
            with self.lock:
                network=self.state["settings"]["network"]; generation=self.market_generation
                basis_symbols={b["symbol"] for b in self.state["bots"] if b["strategy"]=="Basis"}
            try:
                rows=bybit(network,"market/tickers",category="linear")["list"]
                q={}
                for row in rows:
                    if row["symbol"] in paper.PAIRS:
                        try: q[row["symbol"]]=quote(row)
                        except (ValueError,KeyError): pass
                if not q: raise RuntimeError("Нет корректных котировок")
                for sym in basis_symbols.intersection(q):
                    try:
                        spot=bybit(network,"market/tickers",category="spot",symbol=sym)["list"][0]
                        bid=paper.number(spot["bid1Price"],1e-12); ask=paper.number(spot["ask1Price"],bid)
                        q[sym].update(spot_bid=bid,spot_ask=ask,spot_ts=time.time())
                    except Exception: pass
                with self.lock:
                    if generation==self.market_generation:
                        self.quotes=q; self.error=""; before=copy.deepcopy(self.state)
                        try:
                            paper.tick(self.state,{k:v for k,v in q.items() if time.time()-v["ts"]<=15})
                            if before!=self.state: self.save()
                        except Exception: self.state=before; raise
            except Exception as e:
                with self.lock:
                    if generation==self.market_generation: self.error=str(e)[:300]
            self.stop.wait(4)
    def news_loop(self):
        while not self.stop.is_set():
            try:
                news=feed_news()
                with self.lock: self.news=news; self.news_at=time.time(); self.news_error=""
            except Exception as e:
                with self.lock: self.news_error=str(e)
            try:
                r=json_get("https://open.er-api.com/v6/latest/USD")["rates"]
                with self.lock: self.rates.update({k:paper.number(r[k],.00001) for k in ("EUR","RUB")})
            except Exception: pass
            self.stop.wait(180)
    def snapshot(self):
        with self.lock:
            s=copy.deepcopy(self.state)
            for p in s["positions"]:
                q=self.fresh(p["symbol"]); p["pnl"]=paper.pnl(p,q["bid" if p["side"]=="long" else "ask"]) if q else None
                p["liquidation"]=paper.liquidation(p)
            articles={x["id"]:x for x in self.news}
            for id,item in s.get("saved_articles",{}).items(): articles.setdefault(id,item)
            items=[]
            for id,item in articles.items():
                assessment=s.get("news_assessments",{}).get(id)
                items.append({**item,"impact":assessment or item.get("impact","Не оценено")})
            return dict(state=s,available=paper.available(s),quotes={k:self.fresh(k) for k in paper.PAIRS},market_error=self.error,core=self.core.status(),ai=self.ai.status(),news=items,news_error=self.news_error,news_at=self.news_at,rates=self.rates)
    def details(self,symbol):
        if symbol not in paper.PAIRS: raise ValueError("Неизвестная пара")
        with self.lock: network=self.state["settings"]["network"]; interval=self.state["settings"]["timeframe"]
        book=bybit(network,"market/orderbook",category="linear",symbol=symbol,limit=15)
        result={"bids":book["b"],"asks":book["a"],"time":book["ts"],"trend":None,"volatility":None,"levels":None}
        try:
            rows=bybit(network,"market/kline",category="linear",symbol=symbol,interval=interval,limit=32)["list"]
            # discard the newest possibly open candle
            bars=sorted(rows,key=lambda r:int(r[0]))[:-1]; closes=[float(r[4]) for r in bars]
            if len(closes)>=3:
                result["trend"]=(closes[-1]/closes[0]-1)*100
                result["volatility"]=statistics.stdev(math.log(b/a) for a,b in zip(closes,closes[1:]))*100
                result["levels"]=[min(float(r[3]) for r in bars),max(float(r[2]) for r in bars)]
        except Exception: pass
        return result
    def action(self,d):
        a=d["action"]
        if a=="ai_config":return self.ai.configure(d["mode"],d.get("model",""),d.get("key",""))
        if a=="ai_analyze":
            symbol=d["symbol"]
            if symbol not in paper.PAIRS:raise ValueError("Неизвестная пара")
            with self.lock:
                q=copy.deepcopy(self.fresh(symbol))
                nid=d.get("news_id")
                item=next((x for x in self.news if x["id"]==nid),None) or self.state.get("saved_articles",{}).get(nid)
                if nid and not item:raise ValueError("Материал больше недоступен")
            # Do not send portfolio, secrets or arbitrary files. Only explicitly requested market/news context.
            q={k:v for k,v in (q or {}).items() if k in ("last","ask","bid","high","low","volume","oi","change","funding","ts")}
            detail=d.get("details") or {}; forecast=d.get("forecast") or {}
            detail={k:detail[k] for k in ("trend","volatility","levels") if k in detail}
            forecast={k:forecast[k] for k in ("ok","error","trade_ready","cached_age_ms","decision_gate","confirm_long","confirm_short","best") if k in forecast}
            news={k:item[k] for k in ("title","description","source","time") if k in item} if item else None
            result=self.ai.analyze(d["prompt"],d.get("history",[]),{"symbol":symbol,"quote":q,"details":detail,"enacore":forecast,"news":news})
            if item:
                impact={"positive":"Позитивное","negative":"Негативное","mixed":"Смешанное","uncertain":"Неопределённое"}[result["response"]["impact"]]
                with self.lock:
                    assessments=self.state.setdefault("news_assessments",{})
                    assessments[nid]="AI: "+impact+" · "+result["model"]
                    while len(assessments)>1000:assessments.pop(next(iter(assessments)))
                    self.save()
            return result
        if a=="core_rpc":
            return self.core.rpc(d["method"],d.get("args"))
        if a=="core_install":
            import base64
            return self.core.install(base64.b64decode(d["archive"],validate=True),d["digest"])
        if a=="core_rollback": return self.core.rollback()
        if a=="core_stop": self.core.stop(); return {"ok":True}
        with self.lock:
            s=self.state; before=copy.deepcopy(s)
            try:
                if a=="order": paper.order(s,d,self.fresh(d["symbol"]))
                elif a=="close":
                    p=next(p for p in s["positions"] if p["id"]==d["id"]); paper.close(s,p["id"],self.fresh(p["symbol"]))
                elif a=="cancel": s["pending"]=[p for p in s["pending"] if p["id"]!=d["id"]]
                elif a=="new_bot": paper.new_bot(s,d)
                elif a=="bot":
                    b=next(b for b in s["bots"] if b["id"]==d["id"]); paper.bot_action(s,b["id"],d["operation"],self.fresh(b["symbol"]))
                elif a=="save_news":
                    id=str(d["id"])[:2048]
                    articles=s.setdefault("saved_articles",{})
                    if id in s["saved"]: s["saved"].remove(id); articles.pop(id,None)
                    elif len(s["saved"])<1000:
                        item=next((x for x in self.news if x["id"]==id),None)
                        if not item: raise ValueError("Материал не найден")
                        s["saved"].append(id); articles[id]=item
                elif a=="settings":
                    k=d["key"]; v=d["value"]
                    allowed={"theme":("dark","light"),"currency":("USDT","USD","EUR","RUB"),"timeframe":("1","5","15","60","240","D"),"compact":(True,False),"network":("main","testnet")}
                    if k not in allowed or v not in allowed[k]: raise ValueError("Некорректная настройка")
                    if k=="network" and v!=s["settings"][k]:
                        if s["positions"] or s["pending"] or s["bots"]: raise ValueError("Сначала закройте позиции, ордера и ботов")
                        self.quotes={}; self.market_generation+=1; self.error="Ожидание новой сети"
                    s["settings"][k]=v
                elif a=="reset":
                    self.state=paper.initial(); self.state["settings"]=s["settings"]; self.state["saved"]=s["saved"]; self.state["saved_articles"]=s.get("saved_articles",{}); self.state["news_assessments"]=s.get("news_assessments",{})
                elif a=="network_reset":
                    if s["positions"] or s["pending"] or s["bots"]: raise ValueError("Сначала закройте позиции, ордера и ботов")
                    s["settings"]["network"]="main"; self.quotes={}; self.market_generation+=1
                else: raise ValueError("Неизвестная операция")
                self.save()
            except Exception: self.state=before; raise
        return {"ok":True}
def handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def send(self,data,status=200,typ="application/json"):
            if typ=="application/json": data=json.dumps(data,ensure_ascii=False,allow_nan=False).encode("utf-8")
            elif isinstance(data,str): data=data.encode("utf-8")
            self.send_response(status); self.send_header("Content-Type",typ+"; charset=utf-8"); self.send_header("Content-Length",str(len(data)))
            self.send_header("Cache-Control","no-store"); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("X-Frame-Options","DENY"); self.send_header("Referrer-Policy","no-referrer")
            self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self' 'nonce-"+TOKEN+"'; style-src 'self'; font-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers(); self.wfile.write(data)
        def host_ok(self): return self.headers.get("Host")=="127.0.0.1:"+str(self.server.server_port)
        def authorized(self): return self.host_ok() and secrets.compare_digest(self.headers.get("X-Enaverse-Token",""),TOKEN)
        def do_GET(self):
            if not self.host_ok(): self.send({"error":"Invalid host"},403); return
            p=urllib.parse.urlsplit(self.path)
            try:
                if p.path=="/": self.send((ROOT/"web/index.html").read_text("utf-8").replace("__TOKEN__",TOKEN),typ="text/html")
                elif p.path in ("/style.css","/app.js","/plus-jakarta.woff2","/jetbrains.woff2"):
                    name=p.path[1:]; self.send((ROOT/"web"/name).read_bytes(),typ="text/css" if name.endswith(".css") else "text/javascript" if name.endswith(".js") else "font/woff2")
                elif not self.authorized(): self.send({"error":"Unauthorized"},403)
                elif p.path=="/api/state": self.send(app.snapshot())
                elif p.path=="/api/details": self.send(app.details(urllib.parse.parse_qs(p.query)["symbol"][0]))
                else: self.send({"error":"Not found"},404)
            except Exception as e: self.send({"error":str(e)[:1000]},400)
        def do_POST(self):
            if not self.authorized() or self.path!="/api/action": self.send({"error":"Unauthorized"},403); return
            try:
                size=int(self.headers.get("Content-Length",0))
                if not 0<size<=180*2**20: raise ValueError("Неверный размер запроса")
                self.send(app.action(json.loads(self.rfile.read(size))))
            except Exception as e: self.send({"error":str(e)[:1000] or type(e).__name__},400)
    return Handler
def acquire_instance(home):
    home.mkdir(parents=True,exist_ok=True); f=(home/"app.lock").open("a+b"); f.seek(0,2)
    if not f.tell(): f.write(b"0"); f.flush()
    f.seek(0)
    try:
        if os.name=="nt":
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        f.close(); raise SystemExit("enaverse уже запущен; закройте предыдущий процесс.")
    return f
def main():
    instance=acquire_instance(HOME)
    app=App(HOME); server=ThreadingHTTPServer(("127.0.0.1",0),handler(app)); server.daemon_threads=True
    for task in (app.market_loop,app.news_loop): threading.Thread(target=task,daemon=True).start()
    url="http://127.0.0.1:"+str(server.server_port); print("enaverse · "+url,flush=True)
    if "--no-browser" not in sys.argv: webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: app.stop.set(); app.core.stop(); server.server_close(); instance.close()
if __name__=="__main__": main()
