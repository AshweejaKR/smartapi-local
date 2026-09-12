"""Phase 14 REAL-vs-LOCAL SmartAPI parity runner.  Set ENABLE_REAL_ORDERS=true to trade live."""
from __future__ import annotations
import base64, hashlib, hmac, json, math, os, sqlite3, struct, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from SmartApi import SmartConnect
from auth import password_hash

ROOT=Path(__file__).resolve().parent
SECRET={"password","totp","apikey","api_key","jwttoken","refreshtoken","feedtoken","authorization","email","mobileno"}
DYNAMIC={"jwttoken","refreshtoken","feedtoken","orderid","uniqueorderid","exchangeorderid","updatetime","tradeDate","filltime","timestamp","ltp","price","fillprice","averageprice","open","high","low","close"}
OPEN={"OPEN","PENDING","TRIGGER PENDING"}; FINAL={"COMPLETE","FILLED","REJECTED","CANCELLED"}
def flag(k,d=False): return os.getenv(k,str(d)).lower() in {"1","true","yes","on"}
def cfg(k,d): return os.getenv(k,str(d))
def env(p):
    out={}
    for row in p.read_text().splitlines():
        row=row.strip()
        if row and not row.startswith("#") and "=" in row:
            key,value=row.split("=",1);out[key.strip()]=value.strip().strip("\"'")
    return out
def totp(s):
    s=s.upper().replace(" ",""); key=base64.b32decode(s+"="*(-len(s)%8)); d=hmac.new(key,struct.pack(">Q",int(time.time()//30)),hashlib.sha1).digest(); o=d[-1]&15
    return f"{(struct.unpack('>I',d[o:o+4])[0]&0x7fffffff)%1000000:06d}"
def clean(v):
    if isinstance(v,dict): return {k:"<redacted>" if k.lower() in SECRET else clean(x) for k,x in v.items()}
    if isinstance(v,list): return [clean(x) for x in v]
    return v
def norm(v,k=""):
    if k.lower() in DYNAMIC: return 0.0 if isinstance(v,(float,int)) else "<dynamic>"
    if isinstance(v,dict): return {a:norm(b,a) for a,b in v.items()}
    if isinstance(v,list): return [norm(x) for x in v]
    return v
def shape(v,types=False):
    if isinstance(v,dict): return {k:shape(x,types) for k,x in sorted(v.items())}
    if isinstance(v,list): return [shape(x,types) for x in v]
    return type(v).__name__ if types else "value"
def diff(a,b,p="",out=None):
    out=[] if out is None else out
    if len(out)>=15:return out
    if type(a)!=type(b):out.append(f"{p}: {type(a).__name__}!={type(b).__name__}")
    elif isinstance(a,dict):
        for k in sorted(set(a)|set(b)):
            q=f"{p}.{k}" if p else k
            if k not in a or k not in b:out.append(f"{q}: missing on {'REAL' if k not in a else 'LOCAL'}")
            else:diff(a[k],b[k],q,out)
    elif isinstance(a,list):
        if len(a)!=len(b):out.append(f"{p}: list length {len(a)}!={len(b)}")
        for i,(x,y) in enumerate(zip(a,b)):diff(x,y,f"{p}[{i}]",out)
    elif a!=b:out.append(f"{p}: {a!r}!={b!r}")
    return out
def call(f):
    """Run one API request independently, with the requested pacing on both targets."""
    time.sleep(1)
    try:
        return {"exception":None,"result":f()}
    except Exception as e:
        return {"exception":{"type":type(e).__name__,"message":str(e)},"result":None}
    finally:
        time.sleep(1)
def behavior(x):
    if x["exception"]:return ("exception",x["exception"]["type"])
    r=x["result"]; return (r.get("status"),r.get("errorcode")) if isinstance(r,dict) else ("result",type(r).__name__)
def order(i,side,kind,q,price="0",**x):return {"variety":"NORMAL",**i,"transactiontype":side,"ordertype":kind,"producttype":"DELIVERY","duration":"DAY","price":price,"quantity":str(q),**x}
def exact(r,s):
    rows=((r.get("result") or {}).get("data") or []) if isinstance(r,dict) else []
    v=next((x for x in rows if x.get("tradingsymbol")==s),None)
    return {"exchange":str(v["exchange"]).upper(),"tradingsymbol":str(v["tradingsymbol"]).upper(),"symboltoken":str(v["symboltoken"])} if v else None

class Parity:
 def __init__(self):
    self.root=cfg("LOCAL_ROOT","http://127.0.0.1:8000");self.db=Path(cfg("LOCAL_DB",ROOT/"smartapi_local.db"));self.dir=Path(cfg("PARITY_REPORT_DIR",ROOT/"reports"));self.timeout=float(cfg("PARITY_POLL_TIMEOUT",12));self.interval=float(cfg("PARITY_POLL_INTERVAL",.25));self.real_orders=flag("ENABLE_REAL_ORDERS");self.real200=flag("ENABLE_REAL_200_QTY");self.mcx_orders=flag("ENABLE_REAL_MCX_ORDERS");self.cleanup_on=flag("CLEANUP_ENABLED",True);self.close=flag("CLOSE_CONTROLLED_POSITIONS",True);self.stamp=datetime.now().strftime("%Y%m%d_%H%M%S");self.cases=[];self.orders={"REAL":set(),"LOCAL":set()};self.delta={};self.cleanup={"enabled":self.cleanup_on,"cancelled":[],"closed":[],"errors":[]}
 def add(self,id,desc,rr,lr,rf,lf,real_order=False,notes=""):
    a,b=call(rf),call(lf);na,nb=norm(clean(a)),norm(clean(b)); self.cases.append({"case_id":id,"description":desc,"real_request":clean(rr),"local_request":clean(lr),"real_sdk_result":clean(a),"local_sdk_result":clean(b),"normalized_real_result":na,"normalized_local_result":nb,"schema_match":"PASS" if shape(na)==shape(nb) else "FAIL","type_match":"PASS" if shape(clean(a),True)==shape(clean(b),True) else "FAIL","behavior_match":"PASS" if behavior(a)==behavior(b) else "FAIL","differences":diff(na,nb),"notes":notes,"real_order_used":real_order,"cleanup_result":None});return a,b
 def skip(self,id,desc,note):self.cases.append({"case_id":id,"description":desc,"status":"SKIPPED","notes":note,"real_order_used":False,"cleanup_result":None})
 def local_user(self,balance):
    self.user="PARITY14"+self.stamp[-6:];self.password="parity-local-password";self.key="PARITY_LOCAL_"+self.stamp
    with sqlite3.connect(self.db) as c:
      c.execute("INSERT INTO users(client_code,password_hash,api_key,totp,name,email,mobile,enabled) VALUES (?,?,?,?,?,?,?,1)",(self.user,password_hash(self.password),self.key,"123456","Parity Test User","parity@local.invalid","0000000000"));c.execute("INSERT INTO accounts(client_code,available_balance) VALUES (?,?)",(self.user,balance))
 def mapping(self,i,yahoo):
    with sqlite3.connect(self.db) as c:c.execute("INSERT INTO symbol_mappings(exchange,tradingsymbol,symboltoken,yahoo_symbol,enabled) VALUES (?,?,?,?,1) ON CONFLICT(exchange,tradingsymbol) DO UPDATE SET symboltoken=excluded.symboltoken,yahoo_symbol=excluded.yahoo_symbol,enabled=1",(i["exchange"],i["tradingsymbol"],i["symboltoken"],yahoo))
 def track(self,target,response,i,side):
    d=((response.get("result") or {}).get("data") or {});oid=d.get("orderid")
    if oid:
      self.orders[target].add(str(oid))
      if d.get("ordertype")=="MARKET" and self.wait(target,str(oid)) in {"COMPLETE","FILLED"}:
       k=(target,i["exchange"],i["tradingsymbol"],i["symboltoken"]);self.delta[k]=self.delta.get(k,0)+(1 if side=="BUY" else -1)*int(d.get("quantity",0)or 0)
 def wait(self,target,oid):
    client=self.real if target=="REAL" else self.local;end=time.monotonic()+self.timeout;last="TIMEOUT"
    while time.monotonic()<end:
      rows=((call(client.orderBook).get("result")or{}).get("data")or[]);row=next((x for x in rows if str(x.get("orderid"))==oid),None)
      if row:
       last=str(row.get("status","")).upper()
       if last in FINAL:return last
      time.sleep(self.interval)
    return last
 def snapshots(self,p):
    for m,n in (("orderBook","orderBook"),("tradeBook","tradeBook"),("position","position"),("holding","holding"),("rmsLimit","rmsLimit")):self.add(f"{p}.{n}",f"{n} after {p}",{}, {},getattr(self.real,m),getattr(self.local,m))
 def place(self,id,desc,ri,li,rq,lq,side,allow):
    if not allow:
      self.skip(id,desc,"Real order disabled by safety configuration.")
      z=call(lambda:self.local.placeOrderFullResponse(lq));self.track("LOCAL",z,li,side);self.cases.append({"case_id":id+".LOCAL","description":desc+" local-only","local_request":clean(lq),"local_sdk_result":clean(z),"notes":"REAL skipped","real_order_used":False,"cleanup_result":None});return
    a,b=self.add(id,desc,rq,lq,lambda:self.real.placeOrderFullResponse(rq),lambda:self.local.placeOrderFullResponse(lq),True);self.track("REAL",a,ri,side);self.track("LOCAL",b,li,side)
 def cleanup_all(self):
    if not self.cleanup_on:return
    for target,client in (("REAL",self.real),("LOCAL",self.local)):
      try:
       rows=((call(client.orderBook).get("result") or {}).get("data") or []);by={str(x.get("orderid")):x for x in rows}
       for oid in self.orders[target]:
        r=by.get(oid)
        if r and str(r.get("status","")).upper() in OPEN:self.cleanup["cancelled"].append({"target":target,"result":clean(call(lambda o=oid,v=r.get("variety","NORMAL"):client.cancelOrder(o,v)))})
       if self.close:
        for (owner,e,s,t),q in list(self.delta.items()):
         if owner==target and q:self.cleanup["closed"].append({"target":target,"result":clean(call(lambda r=order({"exchange":e,"tradingsymbol":s,"symboltoken":t},"SELL" if q>0 else "BUY","MARKET",abs(q)):client.placeOrderFullResponse(r)))})
      except Exception as x:self.cleanup["errors"].append(f"{target}: {type(x).__name__}: {x}")
 def reports(self):
    for x in self.cases:x["cleanup_result"]=self.cleanup
    counts={"PASS":0,"FAIL":0,"SKIPPED":0}
    for x in self.cases:counts["SKIPPED" if x.get("status")=="SKIPPED" else "PASS" if all(x.get(k)=="PASS" for k in("schema_match","type_match","behavior_match")) else "FAIL"]+=1
    self.dir.mkdir(parents=True,exist_ok=True);data={"phase":14,"created":datetime.now().astimezone().isoformat(),"counts":counts,"cleanup":self.cleanup,"cases":self.cases};j=self.dir/f"smartapi_parity_{self.stamp}.json";m=self.dir/f"smartapi_parity_{self.stamp}.md";j.write_text(json.dumps(data,indent=2,default=str));lines=["# SmartAPI Phase 14 parity report","",f"PASS {counts['PASS']} | FAIL {counts['FAIL']} | SKIPPED {counts['SKIPPED']}","","| Case | Schema | Type | Behavior | Notes |","|---|---|---|---|---|"]
    for x in self.cases:lines.append(f"| {x['case_id']} | {x.get('schema_match','SKIP')} | {x.get('type_match','SKIP')} | {x.get('behavior_match','SKIP')} | {('; '.join(x.get('differences') or [x.get('notes','')])).replace('|','\\|')} |")
    m.write_text("\n".join(lines)+"\n");return j,m

def run():
 p=Parity();keys=env(Path(cfg("REAL_ENV_FILE",ROOT/"angelone_keys.env")));need={"API_KEY","CLIENT_ID","PASSWORD","TOTP_SECRET"}
 if need-keys.keys():raise RuntimeError("credential file has required fields missing")
 p.real=SmartConnect(api_key=keys["API_KEY"]);code=totp(keys["TOTP_SECRET"])
 try:
  login=call(lambda:p.real.generateSession(keys["CLIENT_ID"],keys["PASSWORD"],code))
  if not ((login.get("result")or{}).get("status")):raise RuntimeError("REAL login failed")
  rms=call(p.real.rmsLimit);p.local_user(float((((rms.get("result")or{}).get("data")or{}).get("availablecash",0))));p.local=SmartConnect(api_key=p.key,root=p.root)
  # A: each negative case changes only the named field.
  bad=[("A1","wrong API key","BAD_KEY",keys["CLIENT_ID"],keys["PASSWORD"],code),("A2","wrong client code",keys["API_KEY"],"BAD_CLIENT",keys["PASSWORD"],code),("A3","wrong password",keys["API_KEY"],keys["CLIENT_ID"],"BAD_PASSWORD",code),("A4","wrong TOTP",keys["API_KEY"],keys["CLIENT_ID"],keys["PASSWORD"],"000000"),("A5","all credentials wrong","BAD_KEY","BAD_CLIENT","BAD_PASSWORD","000000")]
  for id,d,k,u,w,t in bad:p.add(id,d,{"clientcode":u},{"clientcode":p.user},lambda k=k,u=u,w=w,t=t:SmartConnect(api_key=k).generateSession(u,w,t),lambda k=k,u=u,w=w,t=t:SmartConnect(api_key=k if k=="BAD_KEY" else p.key,root=p.root).generateSession(u if u!=keys["CLIENT_ID"] else p.user,w if w!=keys["PASSWORD"] else p.password,t if t!=code else "123456"))
  local_login=call(lambda:p.local.generateSession(p.user,p.password,"123456"));p.add("B1","successful login",{"clientcode":keys["CLIENT_ID"]},{"clientcode":p.user},lambda:login["result"],lambda:local_login["result"])
  if not ((local_login.get("result")or{}).get("status")):raise RuntimeError("LOCAL login failed")
  p.add("B2","profile",{}, {},lambda:p.real.getProfile(p.real.refresh_token),lambda:p.local.getProfile(p.local.refresh_token));p.add("B3","RMS after local balance alignment",{}, {},p.real.rmsLimit,p.local.rmsLimit)
  # C: REAL discovery first; the runner never hardcodes a token.
  rn=call(lambda:p.real.searchScrip("NSE","NIFTYBEES"));n=exact(rn,"NIFTYBEES-EQ")
  if not n:p.skip("C1","NSE NIFTYBEES discovery","REAL exact symbol unavailable");raise RuntimeError("NSE NIFTYBEES unavailable")
  p.mapping(n,"NIFTYBEES.NS");_,ln=p.add("C1","exact NSE NIFTYBEES-EQ",{"exchange":"NSE","searchscrip":"NIFTYBEES"},{"exchange":"NSE","searchscrip":"NIFTYBEES"},lambda:rn["result"],lambda:p.local.searchScrip("NSE","NIFTYBEES"));nl=exact(ln,"NIFTYBEES-EQ")
  if not nl:raise RuntimeError("LOCAL exact NSE symbol unavailable")
  rb=call(lambda:p.real.searchScrip("BSE","NIFTYBEES"));br=next((x for x in ((rb.get("result")or{}).get("data")or[]) if "NIFTYBEES" in x.get("tradingsymbol","") ),None);b=({"exchange":str(br["exchange"]).upper(),"tradingsymbol":str(br["tradingsymbol"]).upper(),"symboltoken":str(br["symboltoken"])} if br else None)
  if b:p.mapping(b,"NIFTYBEES.BO");_,lb=p.add("C2","BSE NIFTYBEES discovery",{"exchange":"BSE","searchscrip":"NIFTYBEES"},{"exchange":"BSE","searchscrip":"NIFTYBEES"},lambda:rb["result"],lambda:p.local.searchScrip("BSE","NIFTYBEES"));bl=exact(lb,b["tradingsymbol"])
  else:p.skip("C2","BSE NIFTYBEES discovery","SKIPPED_NOT_AVAILABLE on REAL");bl=None
  rl,_=p.add("C3","NSE LTP",n,nl,lambda:p.real.ltpData(**n),lambda:p.local.ltpData(**nl));ltp=float((((rl.get("result")or{}).get("data")or{}).get("ltp",0)))
  if b and bl:p.add("C4","BSE LTP",b,bl,lambda:p.real.ltpData(**b),lambda:p.local.ltpData(**bl))
  if ltp<=0:raise RuntimeError("REAL LTP unavailable")
  low=f"{math.floor(ltp*.97*100)/100:.2f}";high=f"{math.ceil(ltp*1.03*100)/100:.2f}"
  # D/E. Local 200 is always controlled and cleaned; REAL is behind two explicit flags.
  p.place("D1","MARKET BUY 200",n,nl,None,order(nl,"BUY","MARKET",200),"BUY",False);p.snapshots("D1")
  p.place("D2","LIMIT BUY 200 below LTP",n,nl,order(n,"BUY","LIMIT",200,low),order(nl,"BUY","LIMIT",200,low),"BUY",p.real_orders and p.real200);p.snapshots("D2")
  p.place("D3","MARKET BUY 1",n,nl,order(n,"BUY","MARKET",1),order(nl,"BUY","MARKET",1),"BUY",p.real_orders);p.snapshots("D3")
  p.place("D4","LIMIT BUY 1 below LTP",n,nl,order(n,"BUY","LIMIT",1,low),order(nl,"BUY","LIMIT",1,low),"BUY",p.real_orders);p.snapshots("D4")
  p.place("E1","MARKET SELL 1 controlled quantity",n,nl,order(n,"SELL","MARKET",1),order(nl,"SELL","MARKET",1),"SELL",p.real_orders);p.snapshots("E1")
  p.place("E2","LIMIT SELL 1 above LTP",n,nl,order(n,"SELL","LIMIT",1,high),order(nl,"SELL","LIMIT",1,high),"SELL",p.real_orders);p.snapshots("E2")
  # G: impossible combinations only, so no valid real trade can be created.
  for id,x in [("G1a",{"tradingsymbol":"__PARITY_INVALID__"}),("G1b",{"tradingsymbol":"__PARITY_INVALID__","transactiontype":"SELL"}),("G2a",{"exchange":"MCX"}),("G2b",{"exchange":"BSE"}),("G3",{"quantity":"0"}),("G4",{"quantity":"-1"}),("G5",{"symboltoken":"000000000000"}),("G6",{"tradingsymbol":"__PARITY_MISMATCH__"}),("G7a",{"ordertype":"__INVALID__"}),("G7b",{"producttype":"__INVALID__"})]:
   a={**order(n,"BUY","MARKET",1),**x};z={**order(nl,"BUY","MARKET",1),**x};p.add(id,"invalid NIFTYBEES order",a,z,lambda a=a:p.real.placeOrderFullResponse(a),lambda z=z:p.local.placeOrderFullResponse(z));p.snapshots(id)
  # H: one shared, bounded date range per run.
  end=(datetime.now()-timedelta(days=1)).replace(hour=15,minute=25,second=0,microsecond=0)
  for id,iv,span in [("H1","ONE_MINUTE",timedelta(hours=4)),("H2","FIFTEEN_MINUTE",timedelta(days=5)),("H3","ONE_DAY",timedelta(days=30))]:
   q={"interval":iv,"fromdate":(end-span).strftime("%Y-%m-%d %H:%M"),"todate":end.strftime("%Y-%m-%d %H:%M")};a={"exchange":n["exchange"],"symboltoken":n["symboltoken"],**q};z={"exchange":nl["exchange"],"symboltoken":nl["symboltoken"],**q};p.add(id,f"{iv} candles",a,z,lambda a=a:p.real.getCandleData(a),lambda z=z:p.local.getCandleData(z))
  # I: exact MCX discovery only. A missing contract is an explicit skip, never a guessed token.
  rm=call(lambda:p.real.searchScrip("MCX","GOLDPETAL30SEP26FUT"));mi=exact(rm,"GOLDPETAL30SEP26FUT")
  if not mi:p.skip("I1-I6","MCX GOLDPETAL30SEP26FUT","SKIPPED_NOT_AVAILABLE on REAL")
  else:
   p.mapping(mi,"GC=F");_,lm=p.add("I1","exact MCX GOLDPETAL30SEP26FUT discovery",{"exchange":"MCX","searchscrip":mi["tradingsymbol"]},{"exchange":"MCX","searchscrip":mi["tradingsymbol"]},lambda:rm["result"],lambda:p.local.searchScrip("MCX",mi["tradingsymbol"]));ml=exact(lm,mi["tradingsymbol"])
   if ml:
    p.add("I2","MCX LTP",mi,ml,lambda:p.real.ltpData(**mi),lambda:p.local.ltpData(**ml))
    if p.real_orders and p.mcx_orders:
     p.place("I3","MCX MARKET BUY 1",mi,ml,order(mi,"BUY","MARKET",1),order(ml,"BUY","MARKET",1),"BUY",True);p.snapshots("I3")
     p.place("I5","MCX MARKET SELL 1",mi,ml,order(mi,"SELL","MARKET",1),order(ml,"SELL","MARKET",1),"SELL",True);p.snapshots("I5")
    else:p.skip("I3-I6","MCX BUY/SELL and position/holding comparison","ENABLE_REAL_ORDERS and ENABLE_REAL_MCX_ORDERS required")
   else:p.skip("I2-I6","MCX local comparison","LOCAL exact mapping did not resolve")
  # J: observable authentication/session behaviors.
  ar=SmartConnect(api_key=keys["API_KEY"]);al=SmartConnect(api_key=p.key,root=p.root);ar.setAccessToken("invalid-bearer");al.setAccessToken("invalid-bearer");p.add("J1","invalid bearer",{}, {},ar.rmsLimit,al.rmsLimit)
  ar=SmartConnect(api_key=keys["API_KEY"]);al=SmartConnect(api_key=p.key,root=p.root);p.add("J2","missing Authorization",{}, {},ar.rmsLimit,al.rmsLimit)
  ar=SmartConnect(api_key="WRONG_API_KEY");al=SmartConnect(api_key="WRONG_API_KEY",root=p.root);ar.setAccessToken(p.real.access_token);al.setAccessToken(p.local.access_token);p.add("J3","wrong API key authenticated request",{}, {},ar.rmsLimit,al.rmsLimit)
  p.add("J4","expired-token observable behavior",{}, {},lambda:SmartConnect(api_key=keys["API_KEY"],access_token="expired").rmsLimit(),lambda:SmartConnect(api_key=p.key,access_token="expired",root=p.root).rmsLimit(),notes="SDK has no deterministic real-session expiry control; an invalid expired bearer is compared.")
 except Exception as e:p.cases.append({"case_id":"RUNNER","description":"runner failure","status":"FAIL","notes":f"{type(e).__name__}: {e}","real_order_used":False,"cleanup_result":None})
 finally:
  p.cleanup_all()
  if getattr(p.real,"access_token",None) and getattr(p.local,"access_token",None):
   p.add("J5","logout",{"clientcode":keys["CLIENT_ID"]},{"clientcode":p.user},lambda:p.real.terminateSession(keys["CLIENT_ID"]),lambda:p.local.terminateSession(p.user));p.add("J6","request after logout",{}, {},p.real.rmsLimit,p.local.rmsLimit)
  reports=p.reports()
 return reports
if __name__=="__main__":
 a,b=run();print(f"Reports written: {a} and {b}")
