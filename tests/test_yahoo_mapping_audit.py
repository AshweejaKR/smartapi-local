"""Offline checks for scripts/audit_yahoo_mapping.py; Yahoo is never contacted."""
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_yahoo_mapping.py"
spec = importlib.util.spec_from_file_location("audit_yahoo_mapping", SCRIPT)
audit_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_mod)

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
MASTER = [
    {"token": "3045", "symbol": "SBIN-EQ", "name": "SBIN", "expiry": "", "instrumenttype": "", "exch_seg": "NSE", "strike": "-1.000000"},
    {"token": "10576", "symbol": "NIFTYBEES-EQ", "name": "NIFTYBEES", "expiry": "", "instrumenttype": "", "exch_seg": "NSE"},
    {"token": "10866", "symbol": "GVPTECH-BE", "name": "GVPTECH", "expiry": "", "instrumenttype": "", "exch_seg": "NSE"},
    {"token": "1004", "symbol": "679AP34-SG", "name": "679AP34", "expiry": "", "instrumenttype": "", "exch_seg": "NSE"},
    {"token": "26000", "symbol": "NIFTY", "name": "NIFTY", "expiry": "", "instrumenttype": "", "exch_seg": "NSE"},
    {"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY", "expiry": "", "instrumenttype": "AMXIDX", "exch_seg": "NSE"},
    {"token": "99926004", "symbol": "Nifty 500", "name": "NIFTY 500", "expiry": "", "instrumenttype": "AMXIDX", "exch_seg": "NSE"},
    {"token": "500112", "symbol": "SBIN", "name": "SBIN", "expiry": "", "instrumenttype": "", "exch_seg": "BSE"},
    {"token": "958219", "symbol": "775IGT28", "name": "775IGT28", "expiry": "", "instrumenttype": "", "exch_seg": "BSE"},
    {"token": "574841", "symbol": "GOLDPETAL30NOV26FUT", "name": "GOLDPETAL", "expiry": "30NOV2026", "instrumenttype": "FUTCOM", "exch_seg": "MCX"},
    {"token": "35001", "symbol": "SBIN29SEP26FUT", "name": "SBIN", "expiry": "29SEP2026", "instrumenttype": "FUTSTK", "exch_seg": "NFO"},
]


def chart(symbol, exchange="NSI", itype="EQUITY", currency="INR", age_days=1, close=100.5):
    stamp = int((NOW - timedelta(days=age_days)).timestamp())
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "exchangeName": exchange, "instrumentType": itype,
                 "currency": currency, "longName": "Example"},
        "timestamp": [stamp - 86400, stamp],
        "indicators": {"quote": [{"close": [close and 99.0, close]}]},
    }]}}


NOT_FOUND = {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found"}}}


def args(tmp_path, *extra):
    return audit_mod.parse_args(["--out-dir", str(tmp_path), "--delay", "0", *extra])


def test_parse_master_reads_local_json_and_normalises(tmp_path):
    path = tmp_path / "master.json"
    path.write_text(json.dumps(MASTER), encoding="utf-8")
    rows = audit_mod.load_master(str(path))
    assert len(rows) == len(MASTER)
    assert rows[0] == {"exch_seg": "NSE", "token": "3045", "symbol": "SBIN-EQ", "name": "SBIN",
                       "instrumenttype": "", "expiry": ""}
    with pytest.raises(ValueError):
        audit_mod.parse_master(b'{"not": "a list"}')
    with pytest.raises(ValueError):
        audit_mod.parse_master(b'[{"symbol": "NO-TOKEN"}]')


@pytest.mark.parametrize("index, expected", [
    (0, ("NSE:EQ", "SBIN.NS", "UNTESTED")),
    (1, ("NSE:EQ", "NIFTYBEES.NS", "UNTESTED")),
    (2, ("NSE:BE", "", "REVIEW")),
    (3, ("NSE:DEBT", "", "UNSUPPORTED")),
    (4, ("NSE:INDEX-ALT", "", "REVIEW")),
    (5, ("NSE:INDEX", "^NSEI", "UNTESTED")),
    (6, ("NSE:INDEX", "", "REVIEW")),
    (7, ("BSE:EQ", "SBIN.BO", "UNTESTED")),
    (8, ("BSE:DEBT", "", "UNSUPPORTED")),
    (9, ("MCX:FUTCOM", "", "UNSUPPORTED")),
    (10, ("NFO:FUTSTK", "", "UNSUPPORTED")),
])
def test_candidate_rules_are_conservative(index, expected):
    row = audit_mod.parse_master(json.dumps([MASTER[index]]).encode())[0]
    assert audit_mod.candidate(row)[:3] == expected


@pytest.mark.parametrize("reply, symbol, status", [
    ((200, chart("SBIN.NS")), "SBIN.NS", "VERIFIED"),
    ((200, chart("^NSEI", itype="INDEX")), "^NSEI", "MISMATCH"),  # cash expectations
    ((200, chart("SBIN.NS", exchange="BSE")), "SBIN.NS", "MISMATCH"),
    ((200, chart("SBIN.NS", currency="USD")), "SBIN.NS", "MISMATCH"),
    ((200, chart("OTHER.NS")), "SBIN.NS", "MISMATCH"),
    ((200, chart("SBIN.NS", age_days=40)), "SBIN.NS", "MISSING"),
    ((200, chart("SBIN.NS", close=None)), "SBIN.NS", "MISSING"),
    ((404, NOT_FOUND), "SBIN.NS", "MISSING"),
])
def test_classify_chart_outcomes(reply, symbol, status):
    result = audit_mod.classify_chart(*reply, symbol, "NSI", audit_mod.CASH_TYPES, NOW, 10)
    assert result[0] == status


@pytest.mark.parametrize("reply", [(429, None), (500, None), (401, {"chart": {}}), (404, None), (200, {"chart": {"result": []}})])
def test_inconclusive_replies_are_transient_not_missing(reply):
    with pytest.raises(audit_mod.TransientError):
        audit_mod.classify_chart(*reply, "SBIN.NS", "NSI", audit_mod.CASH_TYPES, NOW, 10)


def run(tmp_path, fetch, *extra):
    rows = audit_mod.parse_master(json.dumps(MASTER).encode())
    return audit_mod.audit(rows, args(tmp_path, *extra), fetch=fetch, now_fn=lambda: NOW,
                           sleep=lambda _: None, log=lambda _: None)


def test_bounded_run_covers_every_row_and_resumes_from_checkpoint(tmp_path):
    calls = []

    def fetch(symbol, timeout):
        calls.append(symbol)
        if symbol == "^NSEI":
            return 200, chart(symbol, itype="INDEX")
        return 200, chart(symbol, exchange="BSE" if symbol.endswith(".BO") else "NSI")

    items, _, candidates = run(tmp_path, fetch, "--limit", "2")
    assert candidates == 4 and calls == ["SBIN.NS", "NIFTYBEES.NS"]  # priority examples first
    statuses = {(i["exch_seg"], i["token"]): i["status"] for i in items}
    assert len(statuses) == len(MASTER)
    assert statuses[("NSE", "3045")] == "VERIFIED" and statuses[("BSE", "500112")] == "UNTESTED"

    calls.clear()
    items, _, _ = run(tmp_path, fetch, "--full")
    assert sorted(calls) == ["SBIN.BO", "^NSEI"]  # nothing re-tested
    assert all(i["status"] == "VERIFIED" for i in items if i["yahoo_candidate"])

    audit_mod.write_csv(items, tmp_path / "out.csv")
    audit_mod.write_summary(items, Counter(), candidates, args(tmp_path), tmp_path / "summary.md")
    with open(tmp_path / "out.csv", newline="", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == len(MASTER)
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "GOLDPETAL30NOV26FUT" in summary and "UNSUPPORTED" in summary


def test_timeouts_become_errors_are_retried_and_stop_the_run(tmp_path):
    def down(symbol, timeout):
        raise audit_mod.TransientError("network: timed out")

    items, run_counts, _ = run(tmp_path, down, "--full", "--retries", "1", "--max-consecutive-errors", "2")
    assert run_counts["ERROR"] == 2 and run_counts["aborted"] == 1
    errors = [i for i in items if i["status"] == "ERROR"]
    assert len(errors) == 2 and not any(i["status"] == "MISSING" for i in items)

    items, _, _ = run(tmp_path, lambda s, t: (200, chart(s, exchange="BSE" if s.endswith(".BO") else "NSI",
                                                          itype="INDEX" if s.startswith("^") else "EQUITY")), "--full")
    assert not any(i["status"] in {"ERROR", "UNTESTED"} for i in items)


def test_angel_login_uses_credentials_file_and_rejects_failures(tmp_path):
    env = tmp_path / "keys.env"
    env.write_text("API_KEY=k\nCLIENT_CODE=c\nPASSWORD=p\nTOTP_SECRET=123456\n", encoding="utf-8")
    sent = []

    def post(path, body, headers):
        sent.append((path, body, headers["X-PrivateKey"]))
        return {"status": True, "data": {"jwtToken": "jwt"}}

    headers = audit_mod.angel_login(env, post=post)
    assert headers["Authorization"] == "Bearer jwt"
    assert sent == [(audit_mod.ANGEL_LOGIN, {"clientcode": "c", "password": "p", "totp": "123456"}, "k")]
    with pytest.raises(audit_mod.TransientError, match="AB1007"):
        audit_mod.angel_login(env, post=lambda *a: {"status": False, "errorcode": "AB1007", "message": "bad"})


def test_angel_quotes_batch_and_price_difference():
    items = [{"exch_seg": "NSE", "token": str(n), "yahoo_candidate": f"S{n}.NS", "status": "VERIFIED",
              "yahoo_last_close": 101.0} for n in range(60)]
    items += [{"exch_seg": "BSE", "token": "500112", "yahoo_candidate": "SBIN.BO", "status": "MISSING"},
              {"exch_seg": "BSE", "token": "1", "yahoo_candidate": "X.BO", "status": "UNTESTED"}]
    calls = []

    def post(path, body, headers):
        (exchange, tokens), = body["exchangeTokens"].items()
        calls.append((exchange, len(tokens)))
        if exchange == "BSE":
            return {"status": True, "data": {"fetched": [], "unfetched": [
                {"symbolToken": "500112", "message": "Symbol not found"}]}}
        if tokens[0] == "50":
            return {"status": False, "errorcode": "AB2001", "message": "rate"}
        return {"status": True, "data": {"fetched": [
            {"symbolToken": t, "ltp": 100.0, "close": 99.0, "exchFeedTime": "24-Sep-2026 15:59:59"} for t in tokens]}}

    quotes = audit_mod.angel_quotes(items, {}, 0, post=post, sleep=lambda _: None, log=lambda _: None)
    assert calls == [("BSE", 1), ("NSE", 50), ("NSE", 10), ("NSE", 10)]  # failed batch retried once
    audit_mod.add_price_comparison(items, quotes)
    assert items[0]["price_diff"] == 1.0 and items[0]["price_diff_pct"] == 1.0
    assert items[0]["angel_prev_close"] == 99.0 and items[0]["angel_note"] == ""
    assert items[55]["angel_note"].startswith("error:") and "price_diff" not in items[55]
    assert items[60]["angel_note"] == "unfetched: Symbol not found"
    assert "angel_note" not in items[61]  # untested candidates are not quoted


def test_old_checkpoint_price_fields_are_renamed(tmp_path):
    path = tmp_path / "checkpoint.jsonl"
    path.write_text(json.dumps({"exch_seg": "NSE", "token": "3045", "yahoo_candidate": "SBIN.NS",
                                "status": "VERIFIED", "last_close": 978.5, "last_date": "2026-09-24"}) + "\n")
    record = audit_mod.load_checkpoint(path)[("NSE", "3045", "SBIN.NS")]
    assert record["yahoo_last_close"] == 978.5 and "last_close" not in record
