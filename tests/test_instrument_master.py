"""Daily instrument-master cache, token re-resolution and verified-only Yahoo mappings."""
import csv
import importlib.util
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

import app as app_module
from conftest import auth_headers
import instrument_master
from instrument_master import download as real_download
import market
import offline_data


pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[1]
LTP = "/rest/secure/angelbroking/order/v1/getLtpData"
SBIN = {"exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045"}
spec = importlib.util.spec_from_file_location("build_yahoo_catalog", ROOT / "scripts" / "build_yahoo_catalog.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class FakeYahoo:
    def quote(self, symbol):
        return {"timestamp": market.datetime.now(market.IST), "open": 1.0, "high": 1.0,
                "low": 1.0, "close": 1.0, "ltp": 1.25, "volume": 0}


class Downloads:
    """Counting master download; `fail` simulates an outage."""

    def __init__(self, rows=None):
        self.rows, self.fail, self.count = rows, False, 0

    def __call__(self, url, timeout=120):
        self.count += 1
        if self.fail:
            raise OSError("network unreachable")
        return offline_data.master_bytes(self.rows)


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "master.db")
    monkeypatch.setattr(market, "PROVIDER", FakeYahoo())
    fake = Downloads()
    monkeypatch.setattr(instrument_master, "download", fake)
    market.CACHE.clear()
    yield fake
    market.CACHE.clear()


def ltp(client, **values):
    if not hasattr(client, "smartapi_headers"):  # one login per client (1/second limit)
        client.smartapi_headers = auth_headers(client)
    return client.post(LTP, headers=client.smartapi_headers, json={**SBIN, **values})


def test_master_download_uses_get(monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"[]"

    def urlopen(request, timeout):
        seen.append((request.get_method(), request.full_url, timeout))
        return Response()

    monkeypatch.setattr(instrument_master.urllib.request, "urlopen", urlopen)
    assert real_download(instrument_master.MASTER_URL) == b"[]"
    assert seen == [("GET", instrument_master.MASTER_URL, 120)]


def test_master_refreshes_once_per_day_and_keeps_the_last_valid_cache(downloads, monkeypatch):
    with TestClient(app_module.app) as client:
        status = instrument_master.status()
        assert downloads.count == 1 and status["row_count"] == len(offline_data.MASTER)
        assert status["last_success_date"] == instrument_master.today() and status["last_error"] == ""
        row = instrument_master.lookup("nse", "3045")
        assert row == {"exchange": "NSE", "token": "3045", "symbol": "SBIN-EQ", "name": "SBIN",
                       "instrumenttype": "", "expiry": "", "lotsize": "1", "tick_size": "5.000000"}
    with TestClient(app_module.app):
        assert downloads.count == 1  # same calendar day: no download

    monkeypatch.setattr(instrument_master, "today", lambda: "2099-01-01")
    downloads.fail = True
    with TestClient(app_module.app) as client:
        assert downloads.count == 2
        status = instrument_master.status()
        assert status["row_count"] == len(offline_data.MASTER) and status["last_success_at"]
        assert status["last_error"] == "OSError: network unreachable"
        assert instrument_master.lookup("NSE", "3045")["symbol"] == "SBIN-EQ"
        assert ltp(client).json()["data"]["ltp"] == 1.25  # mappings survive the failed refresh
        page = client.get("/admin/instruments").text
        assert "OSError: network unreachable" in page and status["last_success_at"] in page


def test_malformed_master_never_replaces_the_cache(downloads):
    with TestClient(app_module.app):
        ok, message = instrument_master.refresh(fetch=lambda url: b'{"not": "a list"}')
        assert not ok and "keeping the last valid cache" in message
        ok, _ = instrument_master.refresh(fetch=lambda url: b'[{"symbol": "NO-TOKEN"}]')
        assert not ok and instrument_master.status()["row_count"] == len(offline_data.MASTER)
        assert instrument_master.lookup("NSE", "3045") is not None


def test_without_a_cache_yahoo_fails_normally_and_angel_one_still_works(downloads, tmp_path, monkeypatch):
    downloads.fail = True
    with TestClient(app_module.app) as client:
        assert not instrument_master.loaded()
        response = ltp(client)
        assert response.status_code == 400
        assert response.json() == {"status": False, "message": "Failed to get symbol details",
                                   "errorcode": "AB1018", "data": None}
    import angelone_proxy
    from test_data_sources import FakeBroker, write_config

    broker = FakeBroker()
    monkeypatch.setenv("SMARTAPI_CONFIG_FILE", str(write_config(tmp_path, order=None, account=None)))
    monkeypatch.setattr(angelone_proxy, "send", broker)
    with TestClient(app_module.app) as client:
        assert not instrument_master.loaded()
        assert ltp(client).json()["data"]["ltp"] == 123.45


def test_admin_refresh_status_and_lookup(downloads):
    with TestClient(app_module.app) as client:
        response = client.post("/admin/instruments/refresh", follow_redirects=False)
        assert response.status_code == 303 and "refreshed" in response.headers["location"]
        page = client.get("/admin/instruments?exchange=MCX&q=GOLDPETAL").text
        for text in (instrument_master.MASTER_URL, "GOLDPETAL30NOV26FUT", "30NOV2026", "FUTCOM",
                     f"<td>{len(offline_data.MASTER)}</td>"):
            assert text in page
        assert client.get("/admin/instruments/refresh").status_code == 405  # POST only
    with sqlite3.connect(app_module.DB_PATH) as conn:
        actions = [row[0] for row in conn.execute("SELECT action FROM audit_log")]
    assert actions.count("instruments.refreshed") == 2


def test_token_change_is_resolved_again_by_exchange_and_symbol(downloads):
    with TestClient(app_module.app) as client:
        moved = [dict(row, token="777777") if row["symbol"] == "SBIN-EQ" else row
                 for row in offline_data.MASTER]
        assert instrument_master.refresh(fetch=lambda url: offline_data.master_bytes(moved))[0]
        assert market.mapping_for("NSE", "3045") is None
        assert market.mapping_for("NSE", "777777")["yahoo_symbol"] == "SBIN.NS"
        assert ltp(client).json()["errorcode"] == "AB1018"  # old token is unknown now
        current = ltp(client, symboltoken="777777").json()["data"]
        assert current["symboltoken"] == "777777" and current["tradingsymbol"] == "SBIN-EQ"


def test_only_verified_catalog_rows_are_mapped(downloads):
    with TestClient(app_module.app) as client:
        mapped = {(row["exchange"], row["tradingsymbol"]) for row in market.list_mappings()}
        assert mapped == {(exchange, symbol) for exchange, symbol, _ in offline_data.CATALOG}
        index = ltp(client, tradingsymbol="nifty 50", symboltoken="99926000").json()["data"]
        assert index["tradingsymbol"] == "Nifty 50"  # display symbol from the master
        # In the master but not VERIFIED: the normal SmartAPI symbol-details error.
        for symbol, token, exchange in (("TCS-EQ", "11536", "NSE"), ("GOLDPETAL30NOV26FUT", "574841", "MCX")):
            assert ltp(client, exchange=exchange, tradingsymbol=symbol, symboltoken=token).json()["errorcode"] == "AB1018"


def test_catalog_builder_keeps_only_verified_cash_rows(tmp_path):
    statuses = ("VERIFIED", "MISSING", "MISMATCH", "REVIEW", "UNSUPPORTED", "UNTESTED", "ERROR")
    rows = [{"exch_seg": "NSE", "symbol": f"{status}-EQ", "yahoo_candidate": f"{status}.NS", "status": status}
            for status in statuses]
    rows += [{"exch_seg": "MCX", "symbol": "GOLDPETAL30NOV26FUT", "yahoo_candidate": "GC=F", "status": "VERIFIED"},
             {"exch_seg": "BSE", "symbol": "SBIN", "yahoo_candidate": "SBIN.BO", "status": "VERIFIED"}]
    assert builder.catalog_rows(rows) == [("BSE", "SBIN", "SBIN.BO"), ("NSE", "VERIFIED-EQ", "VERIFIED.NS")]
    audit = tmp_path / "audit.csv"
    with audit.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["exch_seg", "symbol", "yahoo_candidate", "status"])
        writer.writeheader()
        writer.writerows(rows)
    builder.main(["--audit", str(audit), "--out", str(tmp_path / "catalog.csv")])
    assert market.load_catalog(tmp_path / "catalog.csv") == builder.catalog_rows(rows)


def test_tracked_catalog_is_compact_and_cash_only():
    path = ROOT / "yahoo_catalog.csv"
    rows = market.load_catalog(path)
    with path.open(encoding="utf-8") as handle:
        assert len(rows) == sum(1 for _ in handle) - 1 > 0  # nothing filtered out
    assert {exchange for exchange, _, _ in rows} <= {"NSE", "BSE"}
    assert len({(exchange, symbol) for exchange, symbol, _ in rows}) == len(rows)
    assert len(rows) >= 7_000
    assert {("NSE", "RELIANCE-EQ", "RELIANCE.NS"),
            ("NSE", "SBIN-EQ", "SBIN.NS"),
            ("NSE", "TCS-EQ", "TCS.NS"),
            ("NSE", "NIFTYBEES-EQ", "NIFTYBEES.NS")} <= set(rows)
    assert path.stat().st_size < 1_000_000


def test_legacy_seeded_mappings_are_removed_but_manual_ones_stay(downloads, tmp_path, monkeypatch):
    with sqlite3.connect(app_module.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE symbol_mappings (exchange TEXT NOT NULL, tradingsymbol TEXT NOT NULL, "
            "symboltoken TEXT NOT NULL, yahoo_symbol TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
            "PRIMARY KEY(exchange, symboltoken), UNIQUE(exchange, tradingsymbol))")
        conn.executemany("INSERT INTO symbol_mappings(exchange, tradingsymbol, symboltoken, yahoo_symbol) "
                         "VALUES (?, ?, ?, ?)", [*market.LEGACY_SEEDS, ("NSE", "MY-EQ", "1", "MY.NS")])
    catalog = [row for row in offline_data.CATALOG if row[1] != "RELIANCE-EQ"]
    monkeypatch.setattr(market, "CATALOG_PATH", offline_data.write_catalog(tmp_path / "c.csv", catalog))
    with TestClient(app_module.app):
        assert market.mapping_for("NSE", "2885") is None  # legacy, not in the verified catalog
        assert market.mapping_for("NSE", "1")["yahoo_symbol"] == "MY.NS"
        assert market.mapping_for("NSE", "3045")["origin"] == "catalog"
