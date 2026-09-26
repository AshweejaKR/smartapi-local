"""Offline instrument master, Yahoo catalog and broker guard shared by all tests."""
import csv
import json

import angelone_proxy


def instrument(exchange, token, symbol, name, itype="", lotsize="1", tick="5.000000", expiry=""):
    return {"token": token, "symbol": symbol, "name": name, "expiry": expiry, "strike": "-1.000000",
            "lotsize": lotsize, "instrumenttype": itype, "exch_seg": exchange, "tick_size": tick}


MASTER = [
    instrument("NSE", "3045", "SBIN-EQ", "SBIN"),
    instrument("NSE", "2885", "RELIANCE-EQ", "RELIANCE"),
    instrument("NSE", "11536", "TCS-EQ", "TCS"),
    instrument("NSE", "10576", "NIFTYBEES-EQ", "NIFTYBEES", tick="1.000000"),
    instrument("BSE", "590103", "NIFTYBEES", "NIFTYBEES", tick="1.000000"),
    instrument("NSE", "99926000", "Nifty 50", "NIFTY", "AMXIDX", tick="0.000000"),
    instrument("MCX", "574841", "GOLDPETAL30NOV26FUT", "GOLDPETAL", "FUTCOM", expiry="30NOV2026"),
]
# Test-only catalog. TCS-EQ and the MCX future are deliberately absent (not VERIFIED).
CATALOG = [
    ("NSE", "SBIN-EQ", "SBIN.NS"),
    ("NSE", "RELIANCE-EQ", "RELIANCE.NS"),
    ("NSE", "NIFTYBEES-EQ", "NIFTYBEES.NS"),
    ("BSE", "NIFTYBEES", "NIFTYBEES.BO"),
    ("NSE", "Nifty 50", "^NSEI"),
]


def master_bytes(rows=None):
    return json.dumps(MASTER if rows is None else rows).encode()


def download(url, timeout=120):
    return master_bytes()


def write_catalog(path, rows=None):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("exchange", "tradingsymbol", "yahoo_symbol"))
        writer.writerows(CATALOG if rows is None else rows)
    return path


def no_network(method, path, body=None, headers=None):
    raise angelone_proxy.AngelOneError("Angel One network disabled in tests")
