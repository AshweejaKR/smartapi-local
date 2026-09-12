"""Phase 14 REAL-vs-LOCAL SmartAPI parity runner."""
from __future__ import annotations

import base64, hashlib, hmac, json, math, os, sqlite3, struct, time
from datetime import datetime, timedelta
from pathlib import Path

from SmartApi import SmartConnect
from auth import password_hash

ROOT = Path(__file__).resolve().parent
SECRET = {"password", "totp", "apikey", "api_key", "jwttoken", "refreshtoken", "feedtoken", "authorization", "email", "mobileno"}
DYNAMIC = {"jwttoken", "refreshtoken", "feedtoken", "orderid", "uniqueorderid", "exchangeorderid", "updatetime", "tradedate", "filltime", "timestamp", "ltp", "price", "fillprice", "averageprice", "open", "high", "low", "close"}
OPEN = {"OPEN", "PENDING", "TRIGGER PENDING"}
FINAL = {"COMPLETE", "FILLED", "REJECTED", "CANCELLED"}


def flag(key, default=False):
    return os.getenv(key, str(default)).lower() in {"1", "true", "yes", "on"}


def cfg(key, default):
    return os.getenv(key, str(default))


def env(path):
    out = {}
    for row in path.read_text().splitlines():
        row = row.strip()
        if row and not row.startswith("#") and "=" in row:
            key, value = row.split("=", 1)
            out[key.strip()] = value.strip().strip("\"'")
    return out


def totp(secret):
    secret = secret.upper().replace(" ", "")
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(time.time() // 30)), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000:06d}"


def clean(value):
    if isinstance(value, dict):
        return {k: "<redacted>" if k.lower() in SECRET else clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def norm(value, key=""):
    if key.lower() in DYNAMIC:
        return 0.0 if isinstance(value, (int, float)) else "<dynamic>"
    if isinstance(value, dict):
        return {k: norm(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [norm(v) for v in value]
    return value


def shape(value, types=False):
    if isinstance(value, dict):
        return {k: shape(v, types) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [] if not value else [shape(value[0], types)]
    return type(value).__name__ if types else "value"


def diff(a, b, path="", out=None):
    out = [] if out is None else out
    if len(out) >= 15:
        return out
    if type(a) != type(b):
        out.append(f"{path}: {type(a).__name__}!={type(b).__name__}")
    elif isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            item = f"{path}.{key}" if path else key
            if key not in a or key not in b:
                out.append(f"{item}: missing on {'REAL' if key not in a else 'LOCAL'}")
            else:
                diff(a[key], b[key], item, out)
    elif isinstance(a, list):
        if a and b:
            diff(a[0], b[0], f"{path}[0]", out)
        elif bool(a) != bool(b):
            out.append(f"{path}: one side has no representative list item")
    elif a != b:
        out.append(f"{path}: {a!r}!={b!r}")
    return out


def call(func):
    delay = float(cfg("PARITY_API_DELAY", 1))
    if delay:
        time.sleep(delay)
    try:
        return {"exception": None, "result": func()}
    except Exception as exc:
        return {"exception": {"type": type(exc).__name__, "message": str(exc)}, "result": None}
    finally:
        if delay:
            time.sleep(delay)


def behavior(result):
    if result["exception"]:
        return "exception", result["exception"]["type"]
    value = result["result"]
    return (value.get("status"), value.get("errorcode")) if isinstance(value, dict) else ("result", type(value).__name__)


def order(instrument, side, kind, quantity, price="0", **extra):
    product = extra.pop("producttype", None) or ("CARRYFORWARD" if instrument["exchange"].upper() in {"MCX", "NFO", "BFO"} else "DELIVERY")
    return {"variety": "NORMAL", **instrument, "transactiontype": side, "ordertype": kind,
            "producttype": product, "duration": "DAY", "price": price,
            "quantity": str(quantity), **extra}


def exact(response, symbol):
    rows = ((response.get("result") or {}).get("data") or []) if isinstance(response, dict) else []
    row = next((x for x in rows if x.get("tradingsymbol") == symbol), None)
    return None if not row else {"exchange": str(row["exchange"]).upper(), "tradingsymbol": str(row["tradingsymbol"]).upper(), "symboltoken": str(row["symboltoken"])}


class Parity:
    def __init__(self):
        self.root = cfg("LOCAL_ROOT", "http://127.0.0.1:8000")
        self.db = Path(cfg("LOCAL_DB", ROOT / "smartapi_local.db"))
        self.dir = Path(cfg("PARITY_REPORT_DIR", ROOT / "reports"))
        self.timeout = float(cfg("PARITY_POLL_TIMEOUT", 12))
        self.interval = float(cfg("PARITY_POLL_INTERVAL", .25))
        self.real_orders = flag("ENABLE_REAL_ORDERS")
        self.real200 = flag("ENABLE_REAL_200_QTY")
        self.mcx_orders = flag("ENABLE_REAL_MCX_ORDERS")
        self.cleanup_on = flag("CLEANUP_ENABLED", True)
        self.close = flag("CLOSE_CONTROLLED_POSITIONS", True)
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.cases = []
        self.orders = {"REAL": set(), "LOCAL": set()}
        self.tokens = {"REAL": set(), "LOCAL": set()}
        self.delta = {}
        self.cleanup = {"enabled": self.cleanup_on, "cancelled": [], "closed": [], "errors": []}

    def add(self, case_id, desc, real_req, local_req, real_func, local_func, real_order=False, notes=""):
        real, local = call(real_func), call(local_func)
        nr, nl = norm(clean(real)), norm(clean(local))
        self.cases.append({"case_id": case_id, "description": desc, "real_request": clean(real_req),
                           "local_request": clean(local_req), "real_sdk_result": clean(real),
                           "local_sdk_result": clean(local), "normalized_real_result": nr,
                           "normalized_local_result": nl,
                           "schema_match": "PASS" if shape(nr) == shape(nl) else "FAIL",
                           "type_match": "PASS" if shape(clean(real), True) == shape(clean(local), True) else "FAIL",
                           "behavior_match": "PASS" if behavior(real) == behavior(local) else "FAIL",
                           "differences": diff(nr, nl), "notes": notes,
                           "real_order_used": real_order, "cleanup_result": None})
        return real, local

    def local_only(self, case_id, desc, request, func, notes="REAL skipped by safety configuration."):
        result = call(func)
        self.cases.append({"case_id": case_id, "description": desc, "status": "LOCAL_ONLY",
                           "local_request": clean(request), "local_sdk_result": clean(result),
                           "notes": notes, "real_order_used": False, "cleanup_result": None})
        return result

    def skip(self, case_id, desc, note):
        self.cases.append({"case_id": case_id, "description": desc, "status": "SKIPPED",
                           "notes": note, "real_order_used": False, "cleanup_result": None})

    def local_user(self, balance):
        self.user = "PARITY14" + self.stamp[-6:]
        self.password = "parity-local-password"
        self.key = "PARITY_LOCAL_" + self.stamp
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO users(client_code,password_hash,api_key,totp,name,email,mobile,enabled) VALUES (?,?,?,?,?,?,?,1)",
                         (self.user, password_hash(self.password), self.key, "123456", "Parity Test User", "parity@local.invalid", "0000000000"))
            conn.execute("INSERT INTO accounts(client_code,available_balance) VALUES (?,?)", (self.user, balance))

    def mapping(self, instrument, yahoo):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO symbol_mappings(exchange,tradingsymbol,symboltoken,yahoo_symbol,enabled) VALUES (?,?,?,?,1) ON CONFLICT(exchange,tradingsymbol) DO UPDATE SET symboltoken=excluded.symboltoken,yahoo_symbol=excluded.yahoo_symbol,enabled=1",
                         (instrument["exchange"], instrument["tradingsymbol"], instrument["symboltoken"], yahoo))

    def wait(self, target, order_id):
        client = self.real if target == "REAL" else self.local
        end, last = time.monotonic() + self.timeout, "TIMEOUT"
        while time.monotonic() < end:
            rows = ((call(client.orderBook).get("result") or {}).get("data") or [])
            row = next((x for x in rows if str(x.get("orderid")) == order_id), None)
            if row:
                last = str(row.get("status", "")).upper()
                if last in FINAL:
                    return last
            time.sleep(self.interval)
        return last

    def track(self, target, response, instrument, request):
        data = ((response.get("result") or {}).get("data") or {})
        order_id = data.get("orderid")
        if not order_id:
            return
        self.orders[target].add(str(order_id))
        self.tokens[target].add(str(instrument["symboltoken"]))
        if request["ordertype"] != "MARKET" or self.wait(target, str(order_id)) not in {"COMPLETE", "FILLED"}:
            return
        quantity = int(request["quantity"])
        key = (target, instrument["exchange"], instrument["tradingsymbol"], instrument["symboltoken"], request["producttype"])
        self.delta[key] = self.delta.get(key, 0) + (quantity if request["transactiontype"] == "BUY" else -quantity)

    def filtered(self, target, method):
        client = self.real if target == "REAL" else self.local
        result = getattr(client, method)()
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            return result
        rows = result["data"]
        if method in {"orderBook", "tradeBook"} and self.orders[target]:
            rows = [x for x in rows if str(x.get("orderid")) in self.orders[target]]
        elif method in {"position", "holding"} and self.tokens[target]:
            rows = [x for x in rows if str(x.get("symboltoken")) in self.tokens[target]]
        return {**result, "data": rows}

    def snapshots(self, prefix, compare=True):
        for method in ("orderBook", "tradeBook", "position", "holding", "rmsLimit"):
            if compare:
                self.add(f"{prefix}.{method}", f"{method} after {prefix}", {}, {},
                         lambda m=method: self.filtered("REAL", m),
                         lambda m=method: self.filtered("LOCAL", m))
            else:
                self.local_only(f"{prefix}.{method}", f"{method} after {prefix} local-only", {},
                                lambda m=method: self.filtered("LOCAL", m))

    def place(self, case_id, desc, real_i, local_i, real_req, local_req, allow):
        if not allow:
            self.skip(case_id, desc, "Real order disabled by safety configuration.")
            local = self.local_only(case_id + ".LOCAL", desc + " local-only", local_req,
                                    lambda: self.local.placeOrderFullResponse(local_req))
            self.track("LOCAL", local, local_i, local_req)
            return
        real, local = self.add(case_id, desc, real_req, local_req,
                               lambda: self.real.placeOrderFullResponse(real_req),
                               lambda: self.local.placeOrderFullResponse(local_req), True)
        self.track("REAL", real, real_i, real_req)
        self.track("LOCAL", local, local_i, local_req)

    def cancel_open(self, target):
        client = self.real if target == "REAL" else self.local
        rows = (client.orderBook() or {}).get("data") or []
        results = []
        for row in rows:
            order_id = str(row.get("orderid"))
            if order_id in self.orders[target] and str(row.get("status", "")).upper() in OPEN:
                result = client.cancelOrder(order_id, row.get("variety", "NORMAL"))
                results.append(result)
                self.cleanup["cancelled"].append({"target": target, "orderid": order_id, "result": clean(result)})
        return results

    def cleanup_all(self):
        if not self.cleanup_on:
            return
        for target, client in (("REAL", self.real), ("LOCAL", self.local)):
            try:
                self.cancel_open(target)
                if self.close:
                    for (owner, exchange, symbol, token, product), quantity in list(self.delta.items()):
                        if owner != target or not quantity:
                            continue
                        request = order({"exchange": exchange, "tradingsymbol": symbol, "symboltoken": token},
                                        "SELL" if quantity > 0 else "BUY", "MARKET", abs(quantity), producttype=product)
                        result = call(lambda r=request: client.placeOrderFullResponse(r))
                        self.cleanup["closed"].append({"target": target, "result": clean(result)})
            except Exception as exc:
                self.cleanup["errors"].append(f"{target}: {type(exc).__name__}: {exc}")

    def reports(self):
        for case in self.cases:
            case["cleanup_result"] = self.cleanup
        counts = {"PASS": 0, "FAIL": 0, "SKIPPED": 0, "LOCAL_ONLY": 0}
        for case in self.cases:
            status = case.get("status")
            if status in {"SKIPPED", "LOCAL_ONLY"}:
                counts[status] += 1
            elif all(case.get(k) == "PASS" for k in ("schema_match", "type_match", "behavior_match")):
                counts["PASS"] += 1
            else:
                counts["FAIL"] += 1
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {"phase": 14, "created": datetime.now().astimezone().isoformat(), "counts": counts,
                "cleanup": self.cleanup, "cases": self.cases}
        json_path = self.dir / f"smartapi_parity_{self.stamp}.json"
        md_path = self.dir / f"smartapi_parity_{self.stamp}.md"
        json_path.write_text(json.dumps(data, indent=2, default=str))
        lines = ["# SmartAPI Phase 14 parity report", "", f"PASS {counts['PASS']} | FAIL {counts['FAIL']} | SKIPPED {counts['SKIPPED']} | LOCAL_ONLY {counts['LOCAL_ONLY']}", "", "| Case | Schema | Type | Behavior | Notes |", "|---|---|---|---|---|"]
        for case in self.cases:
            notes = "; ".join(case.get("differences") or [case.get("notes", "")]).replace("|", "\\|")
            lines.append(f"| {case['case_id']} | {case.get('schema_match', case.get('status', 'SKIP'))} | {case.get('type_match', case.get('status', 'SKIP'))} | {case.get('behavior_match', case.get('status', 'SKIP'))} | {notes} |")
        md_path.write_text("\n".join(lines) + "\n")
        return json_path, md_path


def run():
    p = Parity()
    keys = env(Path(cfg("REAL_ENV_FILE", ROOT / "angelone_keys.env")))
    required = {"API_KEY", "CLIENT_ID", "PASSWORD", "TOTP_SECRET"}
    if required - keys.keys():
        raise RuntimeError("credential file has required fields missing")
    p.real = SmartConnect(api_key=keys["API_KEY"])
    code = totp(keys["TOTP_SECRET"])
    try:
        login = call(lambda: p.real.generateSession(keys["CLIENT_ID"], keys["PASSWORD"], code))
        if not ((login.get("result") or {}).get("status")):
            raise RuntimeError("REAL login failed")
        rms = call(p.real.rmsLimit)
        p.local_user(float((((rms.get("result") or {}).get("data") or {}).get("availablecash", 0))))
        p.local = SmartConnect(api_key=p.key, root=p.root)

        bad = [("A1", "wrong API key", "BAD_KEY", keys["CLIENT_ID"], keys["PASSWORD"], code),
               ("A2", "wrong client code", keys["API_KEY"], "BAD_CLIENT", keys["PASSWORD"], code),
               ("A3", "wrong password", keys["API_KEY"], keys["CLIENT_ID"], "BAD_PASSWORD", code),
               ("A4", "wrong TOTP", keys["API_KEY"], keys["CLIENT_ID"], keys["PASSWORD"], "000000"),
               ("A5", "all credentials wrong", "BAD_KEY", "BAD_CLIENT", "BAD_PASSWORD", "000000")]
        for case_id, desc, api_key, user, password, otp in bad:
            p.add(case_id, desc, {"clientcode": user}, {"clientcode": p.user},
                  lambda k=api_key, u=user, w=password, t=otp: SmartConnect(api_key=k).generateSession(u, w, t),
                  lambda k=api_key, u=user, w=password, t=otp: SmartConnect(api_key=k if k == "BAD_KEY" else p.key, root=p.root).generateSession(u if u != keys["CLIENT_ID"] else p.user, w if w != keys["PASSWORD"] else p.password, t if t != code else "123456"))

        local_login = call(lambda: p.local.generateSession(p.user, p.password, "123456"))
        p.add("B1", "successful login", {"clientcode": keys["CLIENT_ID"]}, {"clientcode": p.user},
              lambda: login["result"], lambda: local_login["result"])
        if not ((local_login.get("result") or {}).get("status")):
            raise RuntimeError("LOCAL login failed")
        p.add("B2", "profile", {}, {}, lambda: p.real.getProfile(p.real.refresh_token), lambda: p.local.getProfile(p.local.refresh_token))
        p.add("B3", "RMS after local balance alignment", {}, {}, p.real.rmsLimit, p.local.rmsLimit)

        real_search = call(lambda: p.real.searchScrip("NSE", "NIFTYBEES"))
        nse = exact(real_search, "NIFTYBEES-EQ")
        if not nse:
            p.skip("C1", "NSE NIFTYBEES discovery", "REAL exact symbol unavailable")
            raise RuntimeError("NSE NIFTYBEES unavailable")
        p.mapping(nse, "NIFTYBEES.NS")
        _, local_search = p.add("C1", "exact NSE NIFTYBEES-EQ", {"exchange": "NSE", "searchscrip": "NIFTYBEES"}, {"exchange": "NSE", "searchscrip": "NIFTYBEES"}, lambda: real_search["result"], lambda: p.local.searchScrip("NSE", "NIFTYBEES"))
        local_nse = exact(local_search, "NIFTYBEES-EQ")
        if not local_nse:
            raise RuntimeError("LOCAL exact NSE symbol unavailable")

        real_bse = call(lambda: p.real.searchScrip("BSE", "NIFTYBEES"))
        rows = ((real_bse.get("result") or {}).get("data") or [])
        row = next((x for x in rows if "NIFTYBEES" in x.get("tradingsymbol", "")), None)
        bse = None if not row else {"exchange": str(row["exchange"]).upper(), "tradingsymbol": str(row["tradingsymbol"]).upper(), "symboltoken": str(row["symboltoken"])}
        local_bse = None
        if bse:
            p.mapping(bse, "NIFTYBEES.BO")
            _, local_result = p.add("C2", "BSE NIFTYBEES discovery", {"exchange": "BSE", "searchscrip": "NIFTYBEES"}, {"exchange": "BSE", "searchscrip": "NIFTYBEES"}, lambda: real_bse["result"], lambda: p.local.searchScrip("BSE", "NIFTYBEES"))
            local_bse = exact(local_result, bse["tradingsymbol"])
        else:
            p.skip("C2", "BSE NIFTYBEES discovery", "SKIPPED_NOT_AVAILABLE on REAL")

        real_ltp, _ = p.add("C3", "NSE LTP", nse, local_nse, lambda: p.real.ltpData(**nse), lambda: p.local.ltpData(**local_nse))
        ltp = float((((real_ltp.get("result") or {}).get("data") or {}).get("ltp", 0)))
        if bse and local_bse:
            p.add("C4", "BSE LTP", bse, local_bse, lambda: p.real.ltpData(**bse), lambda: p.local.ltpData(**local_bse))
        if ltp <= 0:
            raise RuntimeError("REAL LTP unavailable")
        low = f"{math.floor(ltp * .97 * 100) / 100:.2f}"
        high = f"{math.ceil(ltp * 1.03 * 100) / 100:.2f}"

        r, l = order(nse, "BUY", "MARKET", 200), order(local_nse, "BUY", "MARKET", 200)
        p.place("D1", "MARKET BUY 200", nse, local_nse, r, l, p.real_orders and p.real200); p.snapshots("D1", p.real_orders and p.real200)
        r, l = order(nse, "BUY", "LIMIT", 200, low), order(local_nse, "BUY", "LIMIT", 200, low)
        p.place("D2", "LIMIT BUY 200 below LTP", nse, local_nse, r, l, p.real_orders and p.real200); p.snapshots("D2", p.real_orders and p.real200)
        r, l = order(nse, "BUY", "MARKET", 1), order(local_nse, "BUY", "MARKET", 1)
        p.place("D3", "MARKET BUY 1", nse, local_nse, r, l, p.real_orders); p.snapshots("D3", p.real_orders)
        r, l = order(nse, "BUY", "LIMIT", 1, low), order(local_nse, "BUY", "LIMIT", 1, low)
        p.place("D4", "LIMIT BUY 1 below LTP", nse, local_nse, r, l, p.real_orders); p.snapshots("D4", p.real_orders)

        r, l = order(nse, "SELL", "MARKET", 1), order(local_nse, "SELL", "MARKET", 1)
        p.place("E1", "MARKET SELL 1 controlled quantity", nse, local_nse, r, l, p.real_orders); p.snapshots("E1", p.real_orders)
        r, l = order(nse, "SELL", "LIMIT", 1, high), order(local_nse, "SELL", "LIMIT", 1, high)
        p.place("E2", "LIMIT SELL 1 above LTP", nse, local_nse, r, l, p.real_orders); p.snapshots("E2", p.real_orders)
        r, l = order(nse, "SELL", "MARKET", 200), order(local_nse, "SELL", "MARKET", 200)
        p.place("E3", "MARKET SELL 200", nse, local_nse, r, l, p.real_orders and p.real200); p.snapshots("E3", p.real_orders and p.real200)

        if p.real_orders:
            p.add("F1", "cancel test-created open orders", {}, {}, lambda: p.cancel_open("REAL"), lambda: p.cancel_open("LOCAL"), True)
            p.snapshots("F2", True)
        else:
            p.local_only("F1", "cancel test-created open orders local-only", {}, lambda: p.cancel_open("LOCAL"))
            p.snapshots("F2", False)

        invalid = [("G1a", {"tradingsymbol": "__PARITY_INVALID__"}),
                   ("G1b", {"tradingsymbol": "__PARITY_INVALID__", "transactiontype": "SELL"}),
                   ("G2a", {"exchange": "MCX"}), ("G2b", {"exchange": "BSE"}),
                   ("G3", {"quantity": "0"}), ("G4", {"quantity": "-1"}),
                   ("G5", {"symboltoken": "000000000000"}),
                   ("G6", {"tradingsymbol": "__PARITY_MISMATCH__"}),
                   ("G7a", {"ordertype": "__INVALID__"}), ("G7b", {"producttype": "__INVALID__"})]
        for case_id, changes in invalid:
            real_req = {**order(nse, "BUY", "MARKET", 1), **changes}
            local_req = {**order(local_nse, "BUY", "MARKET", 1), **changes}
            p.add(case_id, "invalid NIFTYBEES order", real_req, local_req,
                  lambda a=real_req: p.real.placeOrderFullResponse(a),
                  lambda a=local_req: p.local.placeOrderFullResponse(a))
            p.snapshots(case_id)

        end = datetime.now().replace(hour=15, minute=25, second=0, microsecond=0) - timedelta(days=1)
        while end.weekday() >= 5:
            end -= timedelta(days=1)
        for case_id, interval, span in [("H1", "ONE_MINUTE", timedelta(hours=4)),
                                        ("H2", "FIFTEEN_MINUTE", timedelta(days=5)),
                                        ("H3", "ONE_DAY", timedelta(days=30))]:
            common = {"interval": interval, "fromdate": (end - span).strftime("%Y-%m-%d %H:%M"), "todate": end.strftime("%Y-%m-%d %H:%M")}
            real_req = {"exchange": nse["exchange"], "symboltoken": nse["symboltoken"], **common}
            local_req = {"exchange": local_nse["exchange"], "symboltoken": local_nse["symboltoken"], **common}
            p.add(case_id, f"{interval} candles", real_req, local_req,
                  lambda a=real_req: p.real.getCandleData(a), lambda a=local_req: p.local.getCandleData(a))

        real_mcx = call(lambda: p.real.searchScrip("MCX", "GOLDPETAL30SEP26FUT"))
        mcx = exact(real_mcx, "GOLDPETAL30SEP26FUT")
        if not mcx:
            p.skip("I1-I6", "MCX GOLDPETAL30SEP26FUT", "SKIPPED_NOT_AVAILABLE on REAL")
        else:
            p.mapping(mcx, "GC=F")
            _, local_mcx_search = p.add("I1", "exact MCX GOLDPETAL30SEP26FUT discovery", {"exchange": "MCX", "searchscrip": mcx["tradingsymbol"]}, {"exchange": "MCX", "searchscrip": mcx["tradingsymbol"]}, lambda: real_mcx["result"], lambda: p.local.searchScrip("MCX", mcx["tradingsymbol"]))
            local_mcx = exact(local_mcx_search, mcx["tradingsymbol"])
            if not local_mcx:
                p.skip("I2-I6", "MCX local comparison", "LOCAL exact mapping did not resolve")
            else:
                p.add("I2", "MCX LTP", mcx, local_mcx, lambda: p.real.ltpData(**mcx), lambda: p.local.ltpData(**local_mcx))
                if p.real_orders and p.mcx_orders:
                    r, l = order(mcx, "BUY", "MARKET", 1, producttype="CARRYFORWARD"), order(local_mcx, "BUY", "MARKET", 1, producttype="CARRYFORWARD")
                    p.place("I3", "MCX MARKET BUY 1", mcx, local_mcx, r, l, True); p.snapshots("I3")
                    r, l = order(mcx, "SELL", "MARKET", 1, producttype="CARRYFORWARD"), order(local_mcx, "SELL", "MARKET", 1, producttype="CARRYFORWARD")
                    p.place("I5", "MCX MARKET SELL 1", mcx, local_mcx, r, l, True); p.snapshots("I5")
                else:
                    p.skip("I3-I6", "MCX BUY/SELL and position/holding comparison", "ENABLE_REAL_ORDERS and ENABLE_REAL_MCX_ORDERS required")

        real_auth, local_auth = SmartConnect(api_key=keys["API_KEY"]), SmartConnect(api_key=p.key, root=p.root)
        real_auth.setAccessToken("invalid-bearer"); local_auth.setAccessToken("invalid-bearer")
        p.add("J1", "invalid bearer", {}, {}, real_auth.rmsLimit, local_auth.rmsLimit)
        real_auth, local_auth = SmartConnect(api_key=keys["API_KEY"]), SmartConnect(api_key=p.key, root=p.root)
        p.add("J2", "missing Authorization", {}, {}, real_auth.rmsLimit, local_auth.rmsLimit)
        real_auth, local_auth = SmartConnect(api_key="WRONG_API_KEY"), SmartConnect(api_key="WRONG_API_KEY", root=p.root)
        real_auth.setAccessToken(p.real.access_token); local_auth.setAccessToken(p.local.access_token)
        p.add("J3", "wrong API key authenticated request", {}, {}, real_auth.rmsLimit, local_auth.rmsLimit)
        p.add("J4", "expired-token observable behavior", {}, {},
              lambda: SmartConnect(api_key=keys["API_KEY"], access_token="expired").rmsLimit(),
              lambda: SmartConnect(api_key=p.key, access_token="expired", root=p.root).rmsLimit(),
              notes="SDK has no deterministic real-session expiry control; invalid expired bearer compared.")
    except Exception as exc:
        p.cases.append({"case_id": "RUNNER", "description": "runner failure", "status": "FAIL",
                        "notes": f"{type(exc).__name__}: {exc}", "real_order_used": False,
                        "cleanup_result": None})
    finally:
        p.cleanup_all()
        if getattr(p.real, "access_token", None) and getattr(p, "local", None) and getattr(p.local, "access_token", None):
            p.add("J5", "logout", {"clientcode": keys["CLIENT_ID"]}, {"clientcode": p.user},
                  lambda: p.real.terminateSession(keys["CLIENT_ID"]), lambda: p.local.terminateSession(p.user))
            p.add("J6", "request after logout", {}, {}, p.real.rmsLimit, p.local.rmsLimit)
        reports = p.reports()
    return reports


if __name__ == "__main__":
    a, b = run()
    print(f"Reports written: {a} and {b}")
