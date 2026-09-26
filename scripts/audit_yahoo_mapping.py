"""Audit Angel One instrument-master symbols against Yahoo Finance.

Standalone and read-only: it never touches the server, its database or config.

Every master row, identified by (exch_seg, token), gets one status:

  VERIFIED     Yahoo returned recent prices on the expected exchange and type.
  MISSING      Tested: Yahoo reports no such symbol, or no recent prices.
  MISMATCH     Tested: Yahoo answered with another symbol/exchange/type/currency.
  UNTESTED     Rule-derived candidate, not checked yet. Unverified, not a mapping.
  REVIEW       Plausible Yahoo equivalent, but no conservative rule; decide manually.
  UNSUPPORTED  No Yahoo equivalent assumed (derivatives, commodities, debt, ...).
  ERROR        Temporary failure (timeout, throttling, network). Retried next run;
               never evidence that a symbol is unmapped.

Examples:
  python scripts/audit_yahoo_mapping.py                      # download, check 30
  python scripts/audit_yahoo_mapping.py --master reports/yahoo_audit/OpenAPIScripMaster.json
  python scripts/audit_yahoo_mapping.py --master <file> --limit 0   # offline only
  python scripts/audit_yahoo_mapping.py --master <file> --full      # resumable

After the Yahoo checks, Angel One FULL quotes (read-only, 50 tokens per call) are
fetched for every Yahoo-tested candidate using angelone_keys.env, adding
angel_ltp and angel_prev_close. price_diff(_pct) = Yahoo latest close - Angel LTP is
filled only when both prices are from the same market date (IST); every other row
is labelled STALE or NOT_COMPARABLE in price_check. Use --no-angel to skip.
Progress is logged every 100 checks, then a final summary.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import http.client
import json
from pathlib import Path
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=1mo&interval=1d"
USER_AGENT = "Mozilla/5.0"
IST = ZoneInfo("Asia/Kolkata")
PROGRESS_EVERY = 100
STATUSES = ("VERIFIED", "MISSING", "MISMATCH", "ERROR", "UNTESTED", "REVIEW", "UNSUPPORTED")
TESTED = {"VERIFIED", "MISSING", "MISMATCH"}

# Hand-checked index names -> Yahoo; keyed by (exch_seg, master name) for AMXIDX rows.
INDEX_MAP = {
    ("NSE", "NIFTY"): "^NSEI",
    ("NSE", "BANKNIFTY"): "^NSEBANK",
    ("NSE", "FINNIFTY"): "NIFTY_FIN_SERVICE.NS",
    ("NSE", "MIDCPNIFTY"): "NIFTY_MID_SELECT.NS",
    ("NSE", "NIFTYNXT50"): "^NSMIDCP",
    ("NSE", "INDIA VIX"): "^INDIAVIX",
    ("NSE", "NIFTY IT"): "^CNXIT",
    ("BSE", "SENSEX"): "^BSESN",
}
YAHOO_EXCHANGE = {"NSE": "NSI", "BSE": "BSE"}
CASH_TYPES = {"EQUITY", "ETF"}
# NSE series that trade like shares/units; Yahoo may list them, but not by rule.
NSE_REVIEW_SERIES = {"BE", "BZ", "SM", "ST", "SZ", "IV", "RR", "MF"}
NSE_DEBT_SERIES = {"SG", "GS", "TB", "GB"}
BSE_RANGES = {"8": ("BSE:GSEC", "government securities/SDL range 8xxxxx"),
              "9": ("BSE:DEBT", "bond/NCD range 9xxxxx"),
              "7": ("BSE:7xxxxx", "commercial paper/rights entitlement range 7xxxxx")}
PRIORITY = (("NSE", "SBIN-EQ"), ("NSE", "NIFTYBEES-EQ"), ("BSE", "SBIN"),
            ("BSE", "NIFTYBEES"), ("NSE", "Nifty 50"), ("BSE", "SENSEX"))
EXAMPLE_NAMES = ("SBIN", "NIFTYBEES", "NIFTY", "SENSEX", "GOLDPETAL")
FIELDS = ("exch_seg", "token", "symbol", "name", "instrumenttype", "expiry", "group", "rule",
          "yahoo_candidate", "status", "reason", "yahoo_symbol_returned", "yahoo_exchange",
          "yahoo_type", "yahoo_currency", "yahoo_name", "yahoo_last_close", "yahoo_last_date",
          "checked_at", "angel_ltp", "angel_prev_close", "angel_time", "price_diff", "price_diff_pct",
          "price_check", "angel_note")
ANGEL_ROOT = "https://apiconnect.angelone.in"
ANGEL_LOGIN = "/rest/auth/angelbroking/user/v1/loginByPassword"
ANGEL_QUOTE = "/rest/secure/angelbroking/market/v1/quote"
ANGEL_BATCH = 50  # SmartAPI quote limit per request


class TransientError(Exception):
    """Verification could not complete; says nothing about the mapping."""


# --- master -----------------------------------------------------------------

def load_master(source, save_to=None, timeout=120):
    """Read the master from a local JSON file or download it with GET."""
    if not re.match(r"https?://", str(source)):
        return parse_master(Path(source).read_bytes())
    request = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    rows = parse_master(body)
    if save_to:
        Path(save_to).parent.mkdir(parents=True, exist_ok=True)
        Path(save_to).write_bytes(body)
    return rows


def parse_master(body):
    rows = json.loads(body)
    if not isinstance(rows, list):
        raise ValueError("Instrument master must be a JSON list")
    parsed = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("exch_seg") or not row.get("token"):
            raise ValueError(f"Instrument master row lacks exch_seg/token: {row!r}")
        parsed.append({key: str(row.get(key) or "").strip() for key in
                       ("exch_seg", "token", "symbol", "name", "instrumenttype", "expiry")})
    return parsed


# --- candidates -------------------------------------------------------------

def candidate(row):
    """Return (group, yahoo_candidate, status, reason) from master fields only."""
    exch, itype, symbol = row["exch_seg"], row["instrumenttype"], row["symbol"]
    if exch not in ("NSE", "BSE"):
        return f"{exch}:{itype or 'NONE'}", "", "UNSUPPORTED", "derivative/commodity/currency segment; no Yahoo equivalent assumed"
    if itype == "AMXIDX":
        yahoo = INDEX_MAP.get((exch, row["name"]))
        if yahoo:
            return f"{exch}:INDEX", yahoo, "UNTESTED", "curated index mapping"
        return f"{exch}:INDEX", "", "REVIEW", "index not in curated mapping"
    if itype:
        return f"{exch}:{itype}", "", "REVIEW", "unrecognised cash-segment instrument type"
    if exch == "NSE":
        base, _, series = symbol.rpartition("-")
        if not base:
            return "NSE:INDEX-ALT", "", "REVIEW", "alternate index token without series; use the AMXIDX row"
        if series == "EQ":
            return "NSE:EQ", f"{base}.NS", "UNTESTED", "rule: NSE -EQ -> base.NS"
        if series in NSE_REVIEW_SERIES:
            return f"NSE:{series}", "", "REVIEW", f"equity-like series {series}; {base}.NS plausible but not rule-derived"
        if series in NSE_DEBT_SERIES or re.fullmatch(r"[NYZ][0-9A-Z]", series):
            return "NSE:DEBT", "", "UNSUPPORTED", f"government/debt series {series}"
        return "NSE:OTHER", "", "REVIEW", f"unrecognised NSE series {series}"
    token = row["token"]
    if len(token) != 6:
        return "BSE:INDEX-ALT", "", "REVIEW", "short BSE token; alternate index id, use the AMXIDX row"
    if token.startswith("5"):
        return "BSE:EQ", f"{symbol}.BO", "UNTESTED", "rule: BSE scrip 5xxxxx -> symbol.BO"
    if token[0] in BSE_RANGES:
        group, reason = BSE_RANGES[token[0]]
        return group, "", "UNSUPPORTED", reason
    return "BSE:OTHER", "", "REVIEW", "BSE token outside known ranges"


def expected_for(exch):
    types = {"INDEX"} if exch.endswith("INDEX") else CASH_TYPES
    return YAHOO_EXCHANGE[exch.split(":")[0]], types


# --- Yahoo verification -----------------------------------------------------

def fetch_chart(yahoo_symbol, timeout):
    """GET Yahoo's chart endpoint; returns (http_status, parsed_json_or_None)."""
    url = CHART_URL.format(urllib.parse.quote(yahoo_symbol, safe=""))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
            http.client.HTTPException) as exc:
        raise TransientError(f"network: {exc}") from exc
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def classify_chart(http_status, payload, yahoo_symbol, exchange, types, now, max_age_days):
    """Turn one chart reply into (status, reason, details); raise TransientError if inconclusive."""
    error = ((payload or {}).get("chart") or {}).get("error") or {}
    if http_status == 404 and error.get("code") == "Not Found":
        return "MISSING", "Yahoo: " + (error.get("description") or "not found"), {}
    if http_status == 429:
        raise TransientError("throttled (HTTP 429)")
    if http_status != 200 or not payload:
        raise TransientError(f"unexpected HTTP {http_status}")
    try:
        result = payload["chart"]["result"][0]
        meta = result["meta"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TransientError("malformed chart response") from exc
    details = {
        "yahoo_symbol_returned": meta.get("symbol", ""), "yahoo_exchange": meta.get("exchangeName", ""),
        "yahoo_type": meta.get("instrumentType", ""), "yahoo_currency": meta.get("currency", ""),
        "yahoo_name": meta.get("longName") or meta.get("shortName") or "",
    }
    stamps = result.get("timestamp") or []
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []
    prices = [(stamp, close) for stamp, close in zip(stamps, closes) if close and close > 0]
    if prices:
        stamp, close = prices[-1]
        details["yahoo_last_close"] = round(close, 4)
        details["yahoo_last_date"] = datetime.fromtimestamp(stamp, IST).date().isoformat()
    problems = []
    if str(details["yahoo_symbol_returned"]).upper() != yahoo_symbol.upper():
        problems.append(f"returned symbol {details['yahoo_symbol_returned']!r}")
    if details["yahoo_exchange"] != exchange:
        problems.append(f"exchange {details['yahoo_exchange']!r} != {exchange!r}")
    if details["yahoo_type"] not in types:
        problems.append(f"type {details['yahoo_type']!r} not in {sorted(types)}")
    if details["yahoo_currency"] != "INR":
        problems.append(f"currency {details['yahoo_currency']!r}")
    if problems:
        return "MISMATCH", "; ".join(problems), details
    if not prices:
        return "MISSING", "Yahoo symbol exists but returned no prices", details
    age = (now - datetime.fromtimestamp(prices[-1][0], timezone.utc)).days
    if age > max_age_days:
        return "MISSING", f"stale: last price {details['yahoo_last_date']} ({age} days old)", details
    return "VERIFIED", "recent Yahoo prices on expected exchange/type", details


def verify(item, fetch, now, args, sleep=time.sleep):
    """Check one candidate with bounded retries; returns a checkpoint record."""
    exchange, types = expected_for(item["group"])
    attempt = 0
    while True:
        try:
            status, reason, details = classify_chart(
                *fetch(item["yahoo_candidate"], args.timeout), item["yahoo_candidate"],
                exchange, types, now, args.max_age_days)
            break
        except TransientError as exc:
            if attempt >= args.retries:
                status, reason, details = "ERROR", f"{exc} after {attempt + 1} attempts", {}
                break
            attempt += 1
            sleep(args.delay * 30 if "429" in str(exc) else max(2.0, args.delay) * 2 ** attempt)
    return {"exch_seg": item["exch_seg"], "token": item["token"],
            "yahoo_candidate": item["yahoo_candidate"], "status": status, "reason": reason,
            "checked_at": now.isoformat(timespec="seconds"), **details}


# --- checkpoint -------------------------------------------------------------

def load_checkpoint(path):
    results = {}
    if path and Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                for old in ("last_close", "last_date"):  # names used before the Angel comparison
                    if old in record:
                        record["yahoo_" + old] = record.pop(old)
                results[(record["exch_seg"], record["token"], record["yahoo_candidate"])] = record
    return results


def queue(items, results, seed, extra_priority=()):
    """Priority examples, earlier errors, then a seeded group-interleaved order."""
    wanted = set(PRIORITY) | set(extra_priority)
    pending = [item for item in items
               if results.get(key(item), {}).get("status") not in TESTED]
    first, retry, groups = [], [], defaultdict(list)
    for item in pending:
        if (item["exch_seg"], item["symbol"]) in wanted:
            first.append(item)
        elif key(item) in results:
            retry.append(item)
        else:
            groups[item["group"]].append(item)
    rng = random.Random(seed)
    for group in groups.values():
        rng.shuffle(group)
    rest, lists = [], [groups[name] for name in sorted(groups)]
    while any(lists):
        rest.extend(group.pop() for group in lists if group)
    return first + retry + rest


def key(item):
    return item["exch_seg"], item["token"], item["yahoo_candidate"]


# --- Angel One price comparison ---------------------------------------------

def angel_post(path, body, headers, timeout=15):
    """POST JSON to SmartAPI; errors never include the request body."""
    request = urllib.request.Request(ANGEL_ROOT + path, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise TransientError(f"Angel HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
        raise TransientError(f"Angel network: {type(exc).__name__}") from exc


def angel_login(env_path, post=angel_post):
    """Log in with the repo's credentials file; returns headers for quote calls."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from angelone_proxy import env_file, totp

    values = env_file(Path(env_path))
    pick = lambda *names: next((values[name] for name in names if values.get(name)), "")
    api_key = pick("ANGELONE_API_KEY", "API_KEY")
    client = pick("ANGELONE_CLIENT_CODE", "CLIENT_CODE", "CLIENT_ID", "CLIENTCODE")
    password = pick("ANGELONE_PASSWORD", "PASSWORD", "MPIN")
    secret = pick("ANGELONE_TOTP_SECRET", "TOTP_SECRET", "TOTP")
    if not all((api_key, client, password, secret)):
        raise TransientError("Angel One credentials file is incomplete")
    headers = {
        "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB", "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": api_key,
    }
    reply = post(ANGEL_LOGIN, {"clientcode": client, "password": password, "totp": totp(secret)}, headers)
    token = ((reply or {}).get("data") or {}).get("jwtToken")
    if not reply.get("status") or not token:
        raise TransientError(f"Angel login failed: {reply.get('errorcode')} {reply.get('message')}")
    return {**headers, "Authorization": f"Bearer {token}"}


def angel_quotes(items, headers, delay, post=angel_post, sleep=time.sleep, log=print):
    """FULL quotes for Yahoo-tested NSE/BSE candidates, 50 tokens per request."""
    wanted = defaultdict(list)
    for item in items:
        if item["yahoo_candidate"] and item["status"] in TESTED:
            wanted[item["exch_seg"]].append(item["token"])
    batches = [(exchange, tokens[start:start + ANGEL_BATCH])
               for exchange, tokens in sorted(wanted.items())
               for start in range(0, len(tokens), ANGEL_BATCH)]
    quotes = {}
    for index, (exchange, tokens) in enumerate(batches):
        if index:
            sleep(delay)
        for attempt in range(2):
            try:
                reply = post(ANGEL_QUOTE, {"mode": "FULL", "exchangeTokens": {exchange: tokens}}, headers)
                if not reply.get("status"):
                    raise TransientError(f"Angel quote failed: {reply.get('errorcode')} {reply.get('message')}")
                break
            except TransientError as exc:
                reply = exc
                sleep(max(5.0, delay * 5))
        if isinstance(reply, TransientError):
            quotes.update({(exchange, token): {"angel_note": f"error: {reply}"} for token in tokens})
            continue
        data = reply.get("data") or {}
        for row in data.get("fetched") or []:
            quotes[(exchange, str(row.get("symbolToken")))] = {
                "angel_ltp": row.get("ltp"), "angel_prev_close": row.get("close"),
                "angel_time": row.get("exchFeedTime", ""), "angel_note": ""}
        for row in data.get("unfetched") or []:
            quotes[(exchange, str(row.get("symbolToken")))] = {
                "angel_note": f"unfetched: {row.get('message') or row.get('errorCode')}"}
    log(f"Angel quotes: {len(batches)} batches, {len(quotes)} tokens")
    return quotes


def angel_date(value):
    """IST market date of Angel One's exchFeedTime, e.g. '24-Sep-2026 15:59:59'."""
    try:
        return datetime.strptime(str(value).strip(), "%d-%b-%Y %H:%M:%S").date().isoformat()
    except ValueError:
        return ""


def add_price_comparison(items, quotes):
    """Attach Angel prices; compare only observations from the same market date.

    price_diff = Yahoo latest close - Angel LTP; price_diff_pct is relative to Angel.
    """
    for item in items:
        quote = quotes.get((item["exch_seg"], item["token"]))
        if not quote:
            continue
        item.update(quote)
        for field in ("price_diff", "price_diff_pct"):
            item.pop(field, None)
        yahoo, angel = item.get("yahoo_last_close"), quote.get("angel_ltp")
        yahoo_day, angel_day = item.get("yahoo_last_date", ""), angel_date(quote.get("angel_time", ""))
        if not (yahoo and angel and yahoo_day and angel_day):
            item["price_check"] = "NOT_COMPARABLE: price or date missing"
        elif yahoo_day != angel_day:
            item["price_check"] = f"STALE: Yahoo {yahoo_day} vs Angel {angel_day}"
        else:
            item["price_check"] = "SAME_DATE"
            item["price_diff"] = round(yahoo - angel, 4)
            item["price_diff_pct"] = round((yahoo - angel) * 100 / angel, 3)

# --- audit ------------------------------------------------------------------

def audit(rows, args, fetch=fetch_chart, now_fn=lambda: datetime.now(timezone.utc),
          sleep=time.sleep, log=print):
    """Classify every row, verify a bounded (or full) set of candidates, merge results."""
    items = []
    for row in rows:
        group, yahoo, status, reason = candidate(row)
        items.append({**row, "group": group, "rule": reason, "yahoo_candidate": yahoo,
                      "status": status, "reason": reason})
    checkpoint = None if args.fresh else args.checkpoint
    results = load_checkpoint(checkpoint)
    candidates = [item for item in items if item["yahoo_candidate"]]
    todo = queue(candidates, results, args.seed, args.also)
    if not args.full:
        todo = todo[:args.limit]
    run = Counter()
    consecutive_errors = 0
    handle = open(args.checkpoint, "a", encoding="utf-8") if args.checkpoint else None
    try:
        for index, item in enumerate(todo):
            if index:
                sleep(args.delay)
            record = verify(item, fetch, now_fn(), args, sleep)
            results[key(item)] = record
            run[record["status"]] += 1
            if handle:
                handle.write(json.dumps(record) + "\n")
                handle.flush()
            if (index + 1) % PROGRESS_EVERY == 0:
                log(f"[{index + 1}/{len(todo)}] " + ", ".join(f"{k}={v}" for k, v in sorted(run.items())))
            consecutive_errors = consecutive_errors + 1 if record["status"] == "ERROR" else 0
            if consecutive_errors >= args.max_consecutive_errors:
                run["aborted"] = 1
                log(f"Stopping: {consecutive_errors} consecutive temporary errors; rerun to resume.")
                break
    except KeyboardInterrupt:
        run["aborted"] = 1
        log("Interrupted; progress is in the checkpoint.")
    finally:
        if handle:
            handle.close()
    log(f"Yahoo checks done: {sum(v for k, v in run.items() if k != 'aborted')}/{len(todo)} "
        + ", ".join(f"{k}={v}" for k, v in sorted(run.items())))
    for item in items:
        record = results.get(key(item)) if item["yahoo_candidate"] else None
        if record:
            item.update({field: record.get(field, "") for field in FIELDS if field in record})
            item["status"], item["reason"] = record["status"], record["reason"]
    return items, run, len(candidates)


# --- reports ----------------------------------------------------------------

def write_csv(items, path):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for item in sorted(items, key=lambda i: (i["exch_seg"], i["group"], i["symbol"], i["token"])):
            writer.writerow({field: item.get(field, "") for field in FIELDS})


def _line(item):
    extra = f" -> {item['yahoo_candidate']}" if item["yahoo_candidate"] else ""
    price = (f", Yahoo {item['yahoo_last_close']} on {item['yahoo_last_date']}"
             if item.get("yahoo_last_close") else "")
    if item.get("angel_ltp"):
        check = (f"diff {item['price_diff_pct']}%" if item.get("price_diff_pct") is not None
                 else item.get("price_check") or "not compared")
        price += f", Angel LTP {item['angel_ltp']} ({check})"
    expiry = f" exp {item['expiry']}" if item["expiry"] else ""
    return (f"- {item['exch_seg']} {item['token']} `{item['symbol']}` ({item['instrumenttype'] or 'cash'}"
            f"{expiry}){extra}: **{item['status']}** — {item['reason']}{price}")


def _listing(title, items, cap):
    lines = [f"\n## {title} ({len(items)})\n"]
    lines += [_line(item) for item in items[:cap]] or ["- none"]
    if len(items) > cap:
        lines.append(f"- … {len(items) - cap} more in the CSV")
    return lines


def write_summary(items, run, candidate_count, args, path, cap=20, angel_status="not requested"):
    totals = Counter(item["status"] for item in items)
    by_group, rules = defaultdict(Counter), {}
    for item in items:
        by_group[item["group"]][item["status"]] += 1
        rules.setdefault(item["group"], item["rule"])
    lines = [
        "# Yahoo mapping audit", "",
        f"- Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Master: `{args.master}` — {len(items)} rows",
        f"- Mode: {'full' if args.full else f'bounded (limit {args.limit})'}; this run checked "
        f"{sum(v for k, v in run.items() if k != 'aborted')} candidates"
        + (" and stopped early (resumable)" if run.get("aborted") else ""),
        f"- Rule-derived candidates: {candidate_count}. Only **VERIFIED** rows are confirmed by Yahoo; "
        "UNTESTED candidates are unverified guesses, and ERROR is a temporary failure, not a missing symbol.",
        "", "## Totals by status", "", "| " + " | ".join(STATUSES) + " | total |",
        "|" + "---|" * (len(STATUSES) + 1),
        "| " + " | ".join(str(totals[s]) for s in STATUSES) + f" | {len(items)} |",
        "", "## By exchange / group", "",
        "| group | rows | " + " | ".join(STATUSES) + " | rule |",
        "|" + "---|" * (len(STATUSES) + 3),
    ]
    for group in sorted(by_group):
        counts = by_group[group]
        lines.append(f"| {group} | {sum(counts.values())} | "
                     + " | ".join(str(counts[s] or "") for s in STATUSES) + f" | {rules[group]} |")
    lines += ["", "## Key examples", ""]
    for name in EXAMPLE_NAMES:
        found = [i for i in items if i["name"] == name and i["exch_seg"] in ("NSE", "BSE", "MCX")
                 and not i["instrumenttype"].startswith("OPT")]
        lines += [_line(i) for i in found[:8]] or [f"- {name}: not in master"]
    pick = lambda status: [i for i in items if i["status"] == status]
    lines += price_section(items, angel_status, cap)
    lines += _listing("Verified examples", pick("VERIFIED"), cap)
    lines += _listing("Missing (tested)", pick("MISSING"), cap)
    lines += _listing("Mismatch (tested)", pick("MISMATCH"), cap)
    lines += _listing("Temporary errors (retry)", pick("ERROR"), cap)
    lines += ["", f"## Review — ambiguous ({totals['REVIEW']})", ""]
    review = defaultdict(list)
    for item in pick("REVIEW"):
        review[item["group"]].append(item)
    lines += [f"- {group}: {len(found)} rows, e.g. " + ", ".join(f"`{i['symbol']}`" for i in found[:5])
              + f" — {found[0]['reason']}" for group, found in sorted(review.items())] or ["- none"]
    lines += ["", f"## Untested candidates ({totals['UNTESTED']})", ""]
    lines += [f"- {g}: {c['UNTESTED']}" for g, c in sorted(by_group.items()) if c["UNTESTED"]] or ["- none"]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def price_section(items, angel_status, cap):
    tested = [i for i in items if i["status"] in TESTED]
    compared = [i for i in tested if i.get("price_check") == "SAME_DATE"]
    stale = [i for i in tested if str(i.get("price_check", "")).startswith("STALE")]
    other = [i for i in tested if str(i.get("price_check", "")).startswith("NOT_COMPARABLE")]
    size = lambda i: abs(i["price_diff_pct"])
    buckets = Counter("<= 0.5%" if size(i) <= .5 else "0.5-2%" if size(i) <= 2 else "> 2%" for i in compared)
    angel_only = [i for i in tested if i.get("angel_ltp") and not i.get("yahoo_last_close")]
    lines = [
        "", "## Yahoo vs Angel One price", "",
        f"- Angel One quotes: {angel_status}",
        "- `price_diff` = Yahoo latest daily close - Angel One LTP; `angel_prev_close` is Angel's previous-day close.",
        "- Prices are compared only when Yahoo's last close and Angel One's feed time share the same "
        "market date (IST); other rows are STALE or NOT_COMPARABLE and have no diff.",
        f"- Tested rows: {len(tested)}; same-date compared: {len(compared)} — |diff| <= 0.5%: "
        f"{buckets['<= 0.5%']}, 0.5-2%: {buckets['0.5-2%']}, > 2%: {buckets['> 2%']}",
        f"- Stale (different dates): {len(stale)}; not comparable (missing price/date): {len(other)}",
        f"- Angel One has a price but Yahoo has none: {len(angel_only)}",
        f"- Angel One note set (unfetched/error): {sum(1 for i in tested if i.get('angel_note'))}",
    ]
    return lines + _listing("Largest same-date price differences", sorted(compared, key=size, reverse=True), cap)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--master", default=MASTER_URL, help="local JSON path or URL (default: Angel One URL)")
    parser.add_argument("--out-dir", default="reports/yahoo_audit", help="outputs; gitignored by default")
    parser.add_argument("--limit", type=int, default=30, help="new Yahoo checks this run (0 = offline)")
    parser.add_argument("--full", action="store_true", help="check every untested candidate (resumable)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between Yahoo requests")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--retries", type=int, default=2, help="retries per temporary error")
    parser.add_argument("--max-consecutive-errors", type=int, default=5)
    parser.add_argument("--max-age-days", type=int, default=10, help="newest Yahoo price must be this recent")
    parser.add_argument("--seed", type=int, default=0, help="sample order for bounded runs")
    parser.add_argument("--also", action="append", default=[], metavar="EXCH:SYMBOL",
                        help="check this master symbol first, e.g. NSE:TCS-EQ")
    parser.add_argument("--checkpoint", help="JSONL results file (default: <out-dir>/checkpoint.jsonl)")
    parser.add_argument("--fresh", action="store_true", help="ignore earlier checkpoint results")
    parser.add_argument("--no-angel", action="store_true", help="skip the Angel One price comparison")
    parser.add_argument("--angel-env", default=str(Path(__file__).resolve().parents[1] / "angelone_keys.env"),
                        help="Angel One credentials file (default: repo angelone_keys.env)")
    args = parser.parse_args(argv)
    args.also = [tuple(value.split(":", 1)) for value in args.also]
    args.checkpoint = args.checkpoint or str(Path(args.out_dir) / "checkpoint.jsonl")
    return args


def writable(path, write):
    """Write the report; if it is locked (e.g. open in Excel), use a timestamped name."""
    try:
        write(path)
    except PermissionError:
        path = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        print(f"Report file locked; writing {path} instead")
        write(path)
    return path


def main(argv=None):
    args = parse_args(argv)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = load_master(args.master, save_to=out / "OpenAPIScripMaster.json")
    items, run, candidate_count = audit(rows, args)
    angel_status = "skipped (--no-angel)"
    if not args.no_angel:
        try:
            quotes = angel_quotes(items, angel_login(args.angel_env), args.delay)
            add_price_comparison(items, quotes)
            angel_status = f"fetched {sum(1 for q in quotes.values() if q.get('angel_ltp'))} of {len(quotes)}"
        except Exception as exc:  # the Yahoo audit is still written
            angel_status = f"unavailable ({type(exc).__name__}: {exc})"
        print(f"Angel One quotes: {angel_status}")
    csv_path = writable(out / "yahoo_mapping_audit.csv", lambda path: write_csv(items, path))
    summary_path = writable(out / "yahoo_mapping_summary.md", lambda path: write_summary(
        items, run, candidate_count, args, path, angel_status=angel_status))
    totals = Counter(item["status"] for item in items)
    print(f"{len(items)} rows: " + ", ".join(f"{s}={totals[s]}" for s in STATUSES))
    print(f"CSV: {csv_path}\nSummary: {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
