"""ENAVERSE local paper exchange. No private exchange endpoints."""
import math,time,uuid
PAIRS=("BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT")
FEE=.0006
def number(v,lo=0,hi=1e12):
    if isinstance(v,bool): raise ValueError("Некорректное число")
    try: n=float(v)
    except (TypeError,ValueError): raise ValueError("Некорректное число")
    if not math.isfinite(n) or not lo<=n<=hi: raise ValueError("Число вне диапазона")
    return n
def initial():
    return dict(balance=10000.,positions=[],pending=[],history=[],bots=[],saved=[],saved_articles={},settings=dict(theme="dark",currency="USDT",timeframe="15",compact=False,network="main"))
def available(s):
    return s["balance"]-sum(p["margin"] for p in s["positions"]+s["pending"])-sum(b["deposit"] for b in s["bots"])-sum(p["margin"]*p["leverage"]*FEE for p in s["pending"])
def pnl(p,price):
    return (price-p["entry"])*p["qty"]*(1 if p["side"]=="long" else -1)
def liquidation(p):
    return max(0,p["entry"]*(1-(1/p["leverage"]-.005)*(1 if p["side"]=="long" else -1)))
def order(s,d,q):
    sym=d["symbol"]; side=d["side"]; kind=d["kind"]; margin=number(d["margin"],.01); lev=number(d["leverage"],1,10)
    if sym not in PAIRS or side not in ("long","short") or kind not in ("market","limit") or lev not in (1,3,5,10): raise ValueError("Некорректный ордер")
    if margin*(1+lev*FEE)>available(s)+1e-9: raise ValueError("Недостаточно тестового баланса")
    if not q: raise ValueError("Нет свежей котировки")
    entry=number(d["limit"],1e-8) if kind=="limit" else q["ask" if side=="long" else "bid"]
    p=dict(id=uuid.uuid4().hex,symbol=sym,side=side,margin=margin,leverage=lev,entry=entry,qty=margin*lev/entry,kind=kind,created=time.time())
    if kind=="limit": s["pending"].append(p)
    else: s["balance"]-=margin*lev*FEE; s["positions"].append(p)
    return p
def close(s,id,q,reason="manual"):
    p=next((p for p in s["positions"] if p["id"]==id),None)
    if not p or not q: raise ValueError("Нет позиции или свежей цены")
    price=q["bid" if p["side"]=="long" else "ask"]
    result=max(-p["margin"],pnl(p,price)-price*p["qty"]*FEE)
    s["balance"]+=result; s["positions"].remove(p)
    s["history"].insert(0,{**p,"exit":price,"pnl":result-p["margin"]*p["leverage"]*FEE,"closed":time.time(),"reason":reason})
    del s["history"][500:]
def new_bot(s,d):
    name=str(d["name"]).strip(); dep=number(d["deposit"],1); lo=number(d["lower"],1e-8); hi=number(d["upper"],lo); levels=number(d["levels"],2,100)
    if not 1<=len(name)<=60 or d["symbol"] not in PAIRS or d["strategy"] not in ("Grid","DCA","Breakout","Basis") or hi<=lo or levels%1 or dep>available(s): raise ValueError("Проверьте параметры и свободный баланс")
    b=dict(id=uuid.uuid4().hex,name=name,symbol=d["symbol"],strategy=d["strategy"],deposit=dep,lower=lo,upper=hi,levels=int(levels),sl=number(d["sl"],.01,100),tp=number(d["tp"],.01,1000),running=False,cash=dep,qty=0.,trades=0,pnl=0.)
    if b["strategy"]=="Basis":
        b["basis_entry"]=number(d.get("basis_entry",.5),.01,100); b["basis_exit"]=number(d.get("basis_exit",.1),0,100)
        if b["basis_exit"]>=b["basis_entry"]: raise ValueError("Basis: порог выхода должен быть ниже входа")
        b["basis_position"]=None
    s["bots"].append(b); return b
def basis_fresh(q):
    return bool(q and q.get("spot_bid",0)>0 and q.get("spot_ask",0)>=q.get("spot_bid",0) and time.time()-q.get("spot_ts",0)<=15)
def basis_value(b,q):
    p=b.get("basis_position")
    if not p: return b["cash"]-b["deposit"]
    return b["cash"]-b["deposit"]+p["qty"]*(q["spot_bid"]-p["spot"]+p["perp"]-q["ask"])-p["qty"]*(q["spot_bid"]+q["ask"])*FEE
def basis_close(b,q):
    b["cash"]=b["deposit"]+basis_value(b,q); b["basis_position"]=None; b["trades"]+=2; b["pnl"]=b["cash"]-b["deposit"]
def basis_tick(b,q):
    if not basis_fresh(q): return
    b["pnl"]=basis_value(b,q)
    if not b["running"]: return
    stop=b["pnl"]<=-b["deposit"]*b["sl"]/100 or b["pnl"]>=b["deposit"]*b["tp"]/100
    if stop:
        if b.get("basis_position"): basis_close(b,q)
        b["running"]=False; return
    if b.get("basis_position"):
        if (q["ask"]/q["spot_bid"]-1)*100<=b.get("basis_exit",.1): basis_close(b,q)
        return
    # Long spot + short perpetual, equal coin quantity, unleveraged sizing.
    # Approximate paper hedge; funding, exchange liquidation and depth slippage excluded.
    if b["lower"]<=q["spot_ask"]<=b["upper"] and (q["bid"]/q["spot_ask"]-1)*100>=b.get("basis_entry",.5) and b["cash"]>1:
        qty=min(b["cash"],b["deposit"])*.45/max(q["spot_ask"],q["bid"])
        b["cash"]-=qty*(q["spot_ask"]+q["bid"])*FEE
        b["basis_position"]={"qty":qty,"spot":q["spot_ask"],"perp":q["bid"]}; b["trades"]+=2
        b["pnl"]=basis_value(b,q)
def tick(s,quotes):
    for p in s["pending"][:]:
        q=quotes.get(p["symbol"])
        if q and ((p["side"]=="long" and q["ask"]<=p["entry"]) or (p["side"]=="short" and q["bid"]>=p["entry"])):
            s["pending"].remove(p); s["balance"]-=p["margin"]*p["leverage"]*FEE; s["positions"].append(p)
    for p in s["positions"][:]:
        q=quotes.get(p["symbol"]); liq=liquidation(p)
        if q and ((p["side"]=="long" and q["bid"]<=liq) or (p["side"]=="short" and q["ask"]>=liq)): close(s,p["id"],q,"liquidation estimate")
    for b in s["bots"]:
        q=quotes.get(b["symbol"])
        if not q: continue
        if b["strategy"]=="Basis": basis_tick(b,q); continue
        b["pnl"]=b["cash"]+b["qty"]*q["bid"]-b["deposit"]
        if not b["running"]: continue
        if b["pnl"]<=-b["deposit"]*b["sl"]/100 or b["pnl"]>=b["deposit"]*b["tp"]/100:
            if b["qty"]: b["cash"]+=b["qty"]*q["bid"]*(1-FEE); b["qty"]=0; b["trades"]+=1
            b["running"]=False; b["pnl"]=b["cash"]-b["deposit"]; continue
        px=q["last"]; prev=b.get("last"); b["last"]=px
        if prev is None: continue
        step=(b["upper"]-b["lower"])/b["levels"]; lo=b["lower"]; tranche=b["deposit"]/b["levels"]
        buy=sell=False
        if b["strategy"]=="Grid":
            buy=lo<=px<=b["upper"] and math.floor((px-lo)/step)<math.floor((prev-lo)/step)
            sell=lo<=px<=b["upper"] and math.floor((px-lo)/step)>math.floor((prev-lo)/step)
        elif b["strategy"]=="DCA": buy=lo<=px<=b["upper"] and time.time()-b.get("last_buy",0)>=3600
        elif b["strategy"]=="Breakout": buy=prev<=b["upper"]<px and b["qty"]==0; sell=prev>=lo>px
        if buy and b["cash"]>=tranche:
            b["cash"]-=tranche; b["qty"]+=tranche*(1-FEE)/q["ask"]; b["trades"]+=1; b["last_buy"]=time.time()
        if sell and b["qty"]>0:
            qty=min(b["qty"],tranche/q["bid"]) if b["strategy"]=="Grid" else b["qty"]; b["qty"]-=qty; b["cash"]+=qty*q["bid"]*(1-FEE); b["trades"]+=1
        b["pnl"]=b["cash"]+b["qty"]*q["bid"]-b["deposit"]
def bot_action(s,id,action,q):
    b=next((b for b in s["bots"] if b["id"]==id),None)
    if not b: raise ValueError("Бот не найден")
    if action=="toggle":
        if not b["running"] and not q: raise ValueError("Нет свежей котировки")
        if not b["running"] and b["strategy"]=="Basis" and not basis_fresh(q): raise ValueError("Basis: нужны свежие спотовые и фьючерсные котировки")
        b["running"]=not b["running"]; b.pop("last",None)
    elif action=="delete":
        if b.get("basis_position"):
            if not basis_fresh(q): raise ValueError("Basis: для закрытия нужны свежие котировки обоих рынков")
            basis_close(b,q)
        if b["qty"] and not q: raise ValueError("Для закрытия инвентаря нужна свежая цена")
        s["balance"]+=b["cash"]+(b["qty"]*q["bid"]*(1-FEE) if b["qty"] else 0)-b["deposit"]; s["bots"].remove(b)
    else: raise ValueError("Неизвестная операция")
