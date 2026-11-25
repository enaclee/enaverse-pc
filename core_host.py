"""ENACORE v1 stdio adapter. Install only a trusted archive."""
import hashlib,json,os,queue,re,shutil,stat,subprocess,sys,tempfile,threading,uuid,zipfile
from pathlib import Path
class Core:
    def __init__(self,home):
        self.home=Path(home); self.root=self.home/"cores"; self.data=self.home/"core-data"
        self.root.mkdir(parents=True,exist_ok=True); self.data.mkdir(parents=True,exist_ok=True)
        self.pointer=self.root/"active.json"; self.lock=threading.RLock(); self.proc=None; self.replies=None
    def active(self):
        return json.loads(self.pointer.read_text("utf-8")) if self.pointer.exists() else None
    def write(self,obj):
        p=self.root/(".pointer-"+uuid.uuid4().hex); p.write_text(json.dumps(obj),encoding="utf-8"); os.replace(p,self.pointer)
    def check(self,d):
        m=json.loads((d/"manifest.json").read_text("utf-8"))
        if m.get("core_id")!="ENACORE" or m.get("target_platform")!="desktop" or m.get("protocol_version")!=1 or not re.fullmatch(r"\d+\.\d+\.\d+",m.get("version","")): raise ValueError("Несовместимое ядро")
        scripts=d/"scripts"; files=m["files"]; actual={p.name for p in scripts.glob("*.py")}
        if actual!=set(files) or not {"enacore_desktop.py","enacore.py","chain_bridge.py"}<=actual: raise ValueError("Неверный набор модулей")
        for name,digest in files.items():
            if hashlib.sha256((scripts/name).read_bytes()).hexdigest()!=digest: raise ValueError("SHA-256 модуля: "+name)
        return m
    def env(self): return {**os.environ,"PYTHONUTF8":"1","ENACORE_DATA_DIR":str(self.data)}
    def directory(self,a):
        name=a["directory"]
        if not re.fullmatch(r"ENACORE-\d+\.\d+\.\d+-[0-9a-f]{12}",name): raise ValueError("Неверный указатель ядра")
        return self.root/name
    def install(self,blob,digest):
        with self.lock:
            if not re.fullmatch("[a-fA-F0-9]{64}",digest or "") or hashlib.sha256(blob).hexdigest()!=digest.lower(): raise ValueError("SHA-256 не совпадает")
            if len(blob)>128*2**20: raise ValueError("Архив слишком большой")
            from io import BytesIO
            with tempfile.TemporaryDirectory(dir=self.root) as td:
                stage=Path(td)/"stage"; stage.mkdir()
                with zipfile.ZipFile(BytesIO(blob)) as z:
                    seen=set(); members=z.infolist()
                    if len(members)>2000 or sum(x.file_size for x in members)>256*2**20: raise ValueError("Превышен лимит ZIP")
                    for x in members:
                        parts=x.filename.rstrip("/").split("/"); key="/".join(parts).casefold()
                        if key in seen or any(not p or p in (".","..") or ":" in p or "\\" in p or p.endswith((" ",".")) or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?",p) for p in parts) or stat.S_ISLNK(x.external_attr>>16): raise ValueError("Небезопасный путь ZIP")
                        seen.add(key); dest=stage.joinpath(*parts)
                        if x.is_dir(): dest.mkdir(parents=True,exist_ok=True)
                        else:
                            dest.parent.mkdir(parents=True,exist_ok=True)
                            with z.open(x) as a,dest.open("xb") as b: shutil.copyfileobj(a,b)
                m=self.check(stage); old=self.active()
                if old and tuple(map(int,m["version"].split(".")))<tuple(map(int,old["version"].split("."))): raise ValueError("Для понижения используйте откат")
                with tempfile.TemporaryDirectory() as probe:
                    env={**self.env(),"ENACORE_DATA_DIR":probe}
                    for cmd,timeout in [(["enacore_desktop.py","--health","--data-dir",probe],90),(["enacore.py","--test"],180)]:
                        p=subprocess.run([sys.executable,str(stage/"scripts"/cmd[0]),*cmd[1:]],env=env,capture_output=True,encoding="utf-8",errors="replace",timeout=timeout)
                        if p.returncode: raise RuntimeError("Самотест ядра не пройден: "+(p.stderr or p.stdout)[-700:])
                        if cmd[0]=="enacore_desktop.py" and not json.loads(p.stdout).get("ok"): raise RuntimeError("Health check failed")
                dest=self.root/("ENACORE-"+m["version"]+"-"+digest.lower()[:12])
                if dest.exists(): raise ValueError("Версия уже установлена")
                self.stop(); backup=self.root/("backup-"+uuid.uuid4().hex); shutil.copytree(self.data,backup,ignore=shutil.ignore_patterns("desktop.lock"))
                os.replace(stage,dest); new={"version":m["version"],"directory":dest.name,"sha256":digest.lower(),"previous":old,"backup":backup.name}; self.write(new); return new
    def rollback(self):
        with self.lock:
            old=(self.active() or {}).get("previous")
            if not old: raise ValueError("Нет предыдущей версии")
            self.check(self.directory(old)); self.stop(); self.write(old); return old
    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None: return
            a=self.active()
            if not a: raise ValueError("Сначала установите ядро")
            d=self.directory(a); self.check(d); q=queue.Queue(); self.replies=q
            with (self.home/"core.log").open("a",encoding="utf-8") as log:
                self.proc=subprocess.Popen([sys.executable,str(d/"scripts/enacore_desktop.py"),"--stdio","--data-dir",str(self.data)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True,encoding="utf-8",bufsize=1,env=self.env())
            p=self.proc
            def read():
                try:
                    for line in p.stdout: q.put(json.loads(line))
                except Exception as e: q.put(e)
                finally: q.put(EOFError("Ядро завершилось"))
            threading.Thread(target=read,daemon=True).start()
    def rpc(self,method,args=None,timeout=45):
        allowed={"pc_capabilities","pc_contract","pc_config_get","pc_config_set","pc_runtime_start","pc_runtime_stop","pc_runtime_status","pc_forecast","pc_research_status","pc_events","enacore_health","enacore_system_monitor","enacore_explain_state","pc_shutdown"}
        if method not in allowed: raise ValueError("Метод не разрешён")
        with self.lock:
            self.start(); id=uuid.uuid4().hex
            try:
                self.proc.stdin.write(json.dumps({"id":id,"method":method,"args":args or []})+"\n"); self.proc.stdin.flush(); r=self.replies.get(timeout=timeout)
                if isinstance(r,Exception): raise r
                if r.get("id")!=id: raise RuntimeError("Неверный ID ответа")
                return r["result"]
            except Exception: self.stop(False); raise RuntimeError("Нет корректного ответа ENACORE; процесс остановлен")
    def stop(self,graceful=True):
        with self.lock:
            p=self.proc; self.proc=None
            if p:
                if p.poll() is None:
                    if graceful:
                        try: p.stdin.close(); p.wait(timeout=12)
                        except Exception: pass
                    if p.poll() is None:
                        p.terminate()
                        try: p.wait(timeout=5)
                        except subprocess.TimeoutExpired: p.kill(); p.wait()
                for f in (p.stdin,p.stdout):
                    try: f.close()
                    except Exception: pass
    def status(self): return {"installed":self.active(),"connected":bool(self.proc and self.proc.poll() is None)}
