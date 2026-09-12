"""Editable simulator charges; rates are percentages, flat fees are per fill."""
from decimal import Decimal, ROUND_HALF_UP


# Simulator defaults only, deliberately not current broker or statutory rates.
DEFAULTS = (
    ("brokerage", "Brokerage", "FLAT", "BOTH", "TURNOVER", 1),
    ("stt_ctt", "STT/CTT", "PERCENTAGE", "SELL", "TURNOVER", 1),
    ("exchange_charge", "Exchange charges", "PERCENTAGE", "BOTH", "TURNOVER", 1),
    ("sebi_charge", "SEBI charges", "PERCENTAGE", "BOTH", "TURNOVER", 1),
    ("stamp_duty", "Stamp duty", "PERCENTAGE", "BUY", "TURNOVER", 1),
    ("other_charge", "Custom charge", "FLAT", "BOTH", "TURNOVER", 0),
    ("gst", "GST", "PERCENTAGE", "BOTH", "SERVICE_FEES", 1),
)
CHARGE_FIELDS = tuple(row[0] for row in DEFAULTS)
TRADE_FIELDS = ("gross_trade_value", *CHARGE_FIELDS, "total_charges", "gross_pnl", "net_pnl")


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def init_charges(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS charge_config ("
        "name TEXT PRIMARY KEY, label TEXT NOT NULL, "
        "calculation TEXT NOT NULL CHECK(calculation IN ('FLAT', 'PERCENTAGE')), "
        "rate REAL NOT NULL DEFAULT 0 CHECK(rate >= 0), "
        "side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL', 'BOTH')), "
        "basis TEXT NOT NULL CHECK(basis IN ('TURNOVER', 'SERVICE_FEES')), "
        "enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)))"
    )
    conn.executemany(
        "INSERT OR IGNORE INTO charge_config "
        "(name, label, calculation, side, basis, enabled) VALUES (?, ?, ?, ?, ?, ?)",
        DEFAULTS,
    )
    for table, fields in (("trades", TRADE_FIELDS), ("accounts", ("total_charges",)),
                          ("positions", ("total_charges",))):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name in fields:
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} REAL NOT NULL DEFAULT 0")
                if table == "trades" and name == "gross_trade_value":
                    conn.execute("UPDATE trades SET gross_trade_value=ROUND(price * quantity, 2)")


def calculate_charges(conn, price, quantity, side):
    if side not in {"BUY", "SELL"}:
        raise ValueError("Invalid transaction side")
    price, quantity = Decimal(str(price)), Decimal(str(quantity))
    if not price.is_finite() or not quantity.is_finite() or price <= 0 or quantity <= 0:
        raise ValueError("Fill price and quantity must be positive and finite")
    turnover = money(price * quantity)
    amounts = {name: money(0) for name in CHARGE_FIELDS}
    rules = {row["name"]: row for row in conn.execute("SELECT * FROM charge_config")}
    # GST is last so its optional service-fee basis uses rounded component amounts.
    for name in CHARGE_FIELDS:
        rule = rules[name]
        if not rule["enabled"] or rule["side"] not in {side, "BOTH"}:
            continue
        base = turnover
        if name == "gst" and rule["basis"] == "SERVICE_FEES":
            base = sum(amounts[key] for key in ("brokerage", "exchange_charge", "sebi_charge"))
        rate = Decimal(str(rule["rate"]))
        amounts[name] = money(rate if rule["calculation"] == "FLAT" else base * rate / 100)
    return {"gross_trade_value": float(turnover),
            **{name: float(amount) for name, amount in amounts.items()},
            "total_charges": float(sum(amounts.values()))}


def pnl_values(realized, unrealized, charges):
    gross = money(realized) + money(unrealized)
    return {"gross_pnl": float(gross), "total_charges": float(money(charges)),
            "net_pnl": float(gross - money(charges))}

