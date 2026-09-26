"""Build the tracked Yahoo mapping catalog from a Phase 1 audit CSV.

Only VERIFIED NSE/BSE rows are kept: MISSING, MISMATCH, REVIEW, UNSUPPORTED,
UNTESTED and ERROR rows are never mapped. The catalog is keyed by exchange and
Angel trading symbol; the server resolves the current token from the instrument
master, so it survives token changes.

  python scripts/build_yahoo_catalog.py
  python scripts/build_yahoo_catalog.py --audit reports/yahoo_audit/yahoo_mapping_audit.csv
"""
import argparse
import csv
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("exchange", "tradingsymbol", "yahoo_symbol")
EXCHANGES = {"NSE", "BSE"}


def catalog_rows(audit_rows):
    """Sorted unique (exchange, tradingsymbol, yahoo_symbol) for VERIFIED rows."""
    rows = {}
    for row in audit_rows:
        exchange = row.get("exch_seg", "").strip().upper()
        symbol, yahoo = row.get("symbol", "").strip(), row.get("yahoo_candidate", "").strip()
        if row.get("status") == "VERIFIED" and exchange in EXCHANGES and symbol and yahoo:
            rows[(exchange, symbol)] = yahoo
    return [(exchange, symbol, yahoo) for (exchange, symbol), yahoo in sorted(rows.items())]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audit", default=str(ROOT / "reports/yahoo_audit/yahoo_mapping_audit.csv"))
    parser.add_argument("--out", default=str(ROOT / "yahoo_catalog.csv"))
    args = parser.parse_args(argv)
    with open(args.audit, newline="", encoding="utf-8") as handle:
        rows = catalog_rows(csv.DictReader(handle))
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(FIELDS)
        writer.writerows(rows)
    print(f"{len(rows)} VERIFIED mappings written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
