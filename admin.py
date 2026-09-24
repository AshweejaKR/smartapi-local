"""Plain Jinja simulator controls, monitoring and confirmed resets."""
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import invalidate_sessions, password_hash
from charges import CHARGE_FIELDS, init_charges, pnl_values
from common import OWNED_TABLES, connect, delete_client
from fault import LOCK as FAULT_LOCK, active_fault, clear_fault, start_fault
from orders import free_cash
from market import CACHE, DEFAULT_MAPPINGS, delete_candle, list_mappings, mapping_for, override_candles, save_candle, save_override
from rate_limit import DEFAULT_LIMITS, WINDOWS, limiter, list_limits, save_limit


router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
CLIENT_CODE_RE = re.compile(r"[A-Za-z0-9_-]{3,32}")
MOBILE_RE = re.compile(r"\+?[0-9]{7,15}")


def init_admin():
    with connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS audit_log ("
            "id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, action TEXT NOT NULL, "
            "detail TEXT NOT NULL, client_code TEXT NOT NULL DEFAULT '')"
        )


async def form_data(request):
    values = parse_qs((await request.body()).decode(), keep_blank_values=True)
    return {key: items[-1].strip() for key, items in values.items()}


def validate_user(data, require_password):
    errors = []
    if not CLIENT_CODE_RE.fullmatch(data.get("client_code", "")):
        errors.append("Client code must be 3-32 letters, numbers, underscores or dashes.")
    password = data.get("password", "")
    if require_password and len(password) < 4:
        errors.append("Password must be at least 4 characters.")
    elif password and len(password) < 4:
        errors.append("New password must be at least 4 characters.")
    if not data.get("api_key") or len(data["api_key"]) > 128:
        errors.append("API key is required and must be at most 128 characters.")
    if not re.fullmatch(r"[0-9]{6}", data.get("totp", "")):
        errors.append("Fixed TOTP must contain exactly 6 digits.")
    if not data.get("name"):
        errors.append("Name is required.")
    if "@" not in data.get("email", ""):
        errors.append("Enter a valid email address.")
    if not MOBILE_RE.fullmatch(data.get("mobile", "")):
        errors.append("Mobile must contain 7-15 digits, optionally starting with +.")
    return errors


def redirect(path, message):
    separator = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{separator}message={quote(message)}", status_code=303)


def record_audit(conn, action, detail, client_code=""):
    conn.execute(
        "INSERT INTO audit_log(created_at, action, detail, client_code) VALUES (?, ?, ?, ?)",
        (datetime.now().isoformat(timespec="seconds"), action, detail, client_code),
    )


def audit(action, detail, client_code=""):
    with connect() as conn:
        record_audit(conn, action, detail, client_code)


def recent_audit(conn, limit=100):
    rows = [dict(row) for row in conn.execute(
        "SELECT created_at, action, detail, client_code FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    )]
    for row in conn.execute(
        "SELECT e.*, COALESCE(o.client_code, '') AS client_code FROM order_events e "
        "LEFT JOIN orders o ON o.order_id=e.order_id ORDER BY e.id DESC LIMIT ?", (limit,)
    ):
        rows.append({"created_at": datetime.strptime(row["created_at"], "%d-%b-%Y %H:%M:%S").isoformat(),
                     "action": "order." + row["status"].lower(), "client_code": row["client_code"],
                     "detail": row["order_id"] + (": " + row["text"] if row["text"] else "")})
    for row in conn.execute("SELECT * FROM fault_events ORDER BY id DESC LIMIT ?", (limit,)):
        for field, action in (("started_at", "fault.started"), ("ended_at", "fault.ended")):
            if row[field] is not None:
                rows.append({"created_at": datetime.fromtimestamp(row[field]).isoformat(timespec="seconds"),
                             "action": action, "detail": row["mode"], "client_code": ""})
    return sorted(rows, key=lambda row: row["created_at"], reverse=True)[:limit]


def limit_status():
    """Read live counters without consuming a request or exposing tokens."""
    now = time.monotonic()
    seconds = dict(WINDOWS)
    with limiter.lock:
        return [{"scope": scope, "target": target, "client_code": client_code,
                 "window": field.removeprefix("per_"), "used": used}
                for (scope, target, client_code, field), events in sorted(limiter.events.items())
                if (used := sum(event > now - seconds[field] for event in events))]


MONITORS = {
    "orders": ("Orders", "orders", "", "id DESC",
               ("client_code", "order_id", "tradingsymbol", "transaction_type", "order_type", "quantity", "price", "status", "reserved_funds", "updated_at")),
    "open-orders": ("Open limit orders", "orders", "WHERE order_type='LIMIT' AND status IN ('OPEN', 'PENDING')", "id DESC",
                    ("client_code", "order_id", "tradingsymbol", "transaction_type", "quantity", "price", "reserved_funds", "created_at")),
    "positions": ("Positions", "positions", "", "client_code, exchange, symboltoken",
                  ("client_code", "tradingsymbol", "product_type", "net_qty", "avg_price", "last_price", "realized_pnl", "total_charges")),
    "holdings": ("Holdings", "holdings", "", "client_code, exchange, symboltoken",
                 ("client_code", "tradingsymbol", "quantity", "average_price", "last_price")),
    "trades": ("Trades", "trades", "", "id DESC",
               ("client_code", "trade_id", "order_id", "tradingsymbol", "transaction_type", "quantity", "price", *CHARGE_FIELDS, "total_charges", "gross_pnl", "net_pnl", "trade_time")),
}


def monitor_rows(conn, kind, limit=100, offset=0):
    title, table, where, ordering, columns = MONITORS[kind]
    count = conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]
    rows = conn.execute(f"SELECT * FROM {table} {where} ORDER BY {ordering} LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return {"kind": kind, "title": title, "columns": columns, "rows": rows, "count": count}


@router.get("")
async def dashboard(request: Request):
    fault = active_fault()
    with connect() as conn:
        summary = conn.execute(
            "SELECT COUNT(*) AS users, COALESCE(SUM(enabled), 0) AS enabled FROM users"
        ).fetchone()
        accounts = [{**dict(row), "available_cash": free_cash(conn, row["client_code"])}
                    for row in conn.execute("SELECT * FROM accounts ORDER BY client_code")]
        sections = [monitor_rows(conn, kind, 5) for kind in MONITORS]
        charges = conn.execute("SELECT * FROM charge_config ORDER BY rowid").fetchall()
        entries = recent_audit(conn, 10)
    return templates.TemplateResponse(
        request, "admin.html", {"summary": summary, "accounts": accounts,
                               "total": sum(row["available_cash"] for row in accounts),
                               "sections": sections, "symbols": list_mappings(), "charges": charges,
                               "limits": list_limits(), "counters": limit_status(),
                               "active": fault, "entries": entries}
    )


@router.get("/monitor/{kind}")
async def monitor_page(request: Request, kind: str):
    if kind not in MONITORS:
        return templates.TemplateResponse(request, "monitor.html", {"title": "Unknown monitor", "columns": [], "rows": [], "count": 0, "page": 1}, status_code=404)
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    with connect() as conn:
        values = monitor_rows(conn, kind, 100, (page - 1) * 100)
    return templates.TemplateResponse(request, "monitor.html", {**values, "page": page})


@router.get("/audit")
async def audit_page(request: Request):
    active_fault()
    with connect() as conn:
        entries = recent_audit(conn)
    return templates.TemplateResponse(request, "audit.html", {"entries": entries})


@router.get("/logs")
async def logs_page(request: Request):
    return await audit_page(request)


def market_redirect(exchange, symboltoken, message):
    return redirect(
        f"/admin/market?symbol={quote(str(exchange) + ':' + str(symboltoken))}",
        message,
    )


def optional_float(value):
    if value is None or not value.strip():
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError
    return result


def optional_int(value):
    if value is None or not value.strip():
        return None
    result = int(value)
    if result < 0:
        raise ValueError
    return result


@router.get("/market")
async def market_page(request: Request):
    rows = list_mappings()
    selected_value = request.query_params.get("symbol", "")
    exchange, _, symboltoken = selected_value.partition(":")
    selected = next(
        (row for row in rows if row["exchange"] == exchange and row["symboltoken"] == symboltoken),
        rows[0] if rows else None,
    )
    candles = []
    if selected:
        candles = override_candles(
            selected["exchange"], selected["symboltoken"], datetime.min, datetime.max
        ) or []
    return templates.TemplateResponse(
        request, "market.html", {"symbols": rows, "selected": selected, "candles": candles}
    )


@router.post("/market")
async def save_market(request: Request):
    data = await form_data(request)
    exchange, symboltoken = data.get("exchange"), data.get("symboltoken")
    if mapping_for(exchange, symboltoken) is None:
        return redirect("/admin/market", "Select a valid symbol.")
    try:
        if data.get("action") == "delete_candle":
            delete_candle(exchange, symboltoken, data.get("timestamp", ""))
            audit("market.candle_deleted", f"{exchange}:{symboltoken}")
            return market_redirect(exchange, symboltoken, "Candle deleted.")
        if data.get("action") == "save_candle":
            stamp = datetime.fromisoformat(data.get("timestamp", "")).replace(second=0, microsecond=0)
            values = {
                "open": optional_float(data.get("candle_open")),
                "high": optional_float(data.get("candle_high")),
                "low": optional_float(data.get("candle_low")),
                "close": optional_float(data.get("candle_close")),
                "volume": optional_int(data.get("candle_volume")),
            }
            if any(values[key] is None for key in ("open", "high", "low", "close", "volume")):
                raise ValueError
            save_candle(exchange, symboltoken, stamp.isoformat(), values)
            audit("market.candle_saved", f"{exchange}:{symboltoken} {stamp.isoformat()}")
            return market_redirect(exchange, symboltoken, "Candle saved.")
        values = {
            "ltp": optional_float(data.get("ltp")),
            "volume": optional_int(data.get("volume")),
            "open": optional_float(data.get("open")),
            "high": optional_float(data.get("high")),
            "low": optional_float(data.get("low")),
            "close": optional_float(data.get("close")),
        }
        save_override(exchange, symboltoken, data.get("mode", "YAHOO"), values)
    except (TypeError, ValueError):
        return market_redirect(exchange, symboltoken, "Enter valid numeric market values.")
    audit("market.updated", f"{exchange}:{symboltoken} {data.get('mode', 'YAHOO')}")
    return market_redirect(exchange, symboltoken, "Market source updated.")


@router.get("/charges")
async def charges_page(request: Request):
    with connect() as conn:
        rows = conn.execute("SELECT * FROM charge_config ORDER BY rowid").fetchall()
    return templates.TemplateResponse(request, "charges.html", {"charges": rows})


@router.post("/charges")
async def save_charges(request: Request):
    data = await form_data(request)
    updates = []
    try:
        for name in CHARGE_FIELDS:
            rate = Decimal(data.get(f"{name}_rate", ""))
            calculation = data.get(f"{name}_calculation")
            side = data.get(f"{name}_side")
            basis = data.get(f"{name}_basis", "TURNOVER")
            if not rate.is_finite() or rate < 0 or rate > Decimal("1000000000"):
                raise ValueError
            if calculation not in {"FLAT", "PERCENTAGE"} or side not in {"BUY", "SELL", "BOTH"}:
                raise ValueError
            if basis not in ({"TURNOVER", "SERVICE_FEES"} if name == "gst" else {"TURNOVER"}):
                raise ValueError
            updates.append((float(rate), calculation, side, basis,
                            int(data.get(f"{name}_enabled") == "on"), name))
    except (InvalidOperation, ValueError):
        return redirect("/admin/charges", "Enter valid charge settings and rates from 0 to 1,000,000,000.")
    with connect() as conn:
        conn.executemany(
            "UPDATE charge_config SET rate=?, calculation=?, side=?, basis=?, enabled=? WHERE name=?",
            updates,
        )
        record_audit(conn, "charges.updated", "Charge configuration saved")
    return redirect("/admin/charges", "Charges saved. New fills use these settings.")


@router.get("/users")
async def users(request: Request):
    with connect() as conn:
        rows = conn.execute(
            "SELECT client_code, api_key, totp, name, email, mobile, enabled "
            "FROM users ORDER BY client_code"
        ).fetchall()
    return templates.TemplateResponse(request, "users.html", {"users": rows})


@router.get("/users/add")
async def add_user_form(request: Request):
    return templates.TemplateResponse(
        request, "user_form.html", {"user": {}, "editing": False, "errors": []}
    )


@router.post("/users/add")
async def add_user(request: Request):
    data = await form_data(request)
    errors = validate_user(data, True)
    if not errors:
        try:
            with connect() as conn:
                conn.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        data["client_code"], password_hash(data["password"]),
                        data["api_key"], data["totp"], data["name"],
                        data["email"], data["mobile"],
                    ),
                )
                conn.execute(
                    "INSERT INTO accounts(client_code) VALUES (?)",
                    (data["client_code"],),
                )
                record_audit(conn, "user.added", "User created", data["client_code"])
        except sqlite3.IntegrityError:
            errors.append("That client code already exists.")
    if errors:
        return templates.TemplateResponse(
            request, "user_form.html",
            {"user": data, "editing": False, "errors": errors}, status_code=400,
        )
    return redirect("/admin/users", "User added.")


@router.get("/users/{client_code}/edit")
async def edit_user_form(request: Request, client_code: str):
    with connect() as conn:
        user = conn.execute(
            "SELECT client_code, api_key, totp, name, email, mobile, enabled "
            "FROM users WHERE client_code = ?", (client_code,),
        ).fetchone()
    if user is None:
        return redirect("/admin/users", "User not found.")
    return templates.TemplateResponse(
        request, "user_form.html", {"user": user, "editing": True, "errors": []}
    )


@router.post("/users/{old_client_code}/edit")
async def edit_user(request: Request, old_client_code: str):
    data = await form_data(request)
    errors = validate_user(data, False)
    with connect() as conn:
        current = conn.execute(
            "SELECT * FROM users WHERE client_code = ?", (old_client_code,),
        ).fetchone()
    if current is None:
        return redirect("/admin/users", "User not found.")
    if not errors:
        new_code = data["client_code"]
        stored_password = (
            password_hash(data["password"]) if data.get("password")
            else current["password_hash"]
        )
        try:
            with connect() as conn:
                conn.execute(
                    "UPDATE users SET client_code=?, password_hash=?, api_key=?, "
                    "totp=?, name=?, email=?, mobile=? WHERE client_code=?",
                    (
                        new_code, stored_password, data["api_key"], data["totp"],
                        data["name"], data["email"], data["mobile"], old_client_code,
                    ),
                )
                conn.execute(
                    "UPDATE accounts SET client_code=? WHERE client_code=?",
                    (new_code, old_client_code),
                )
                conn.execute(
                    "UPDATE sessions SET client_code=? WHERE client_code=?",
                    (new_code, old_client_code),
                )
                for table in OWNED_TABLES:
                    conn.execute(f"UPDATE {table} SET client_code=? WHERE client_code=?", (new_code, old_client_code))
                record_audit(conn, "user.updated", f"Previous client code: {old_client_code}", new_code)
        except sqlite3.IntegrityError:
            errors.append("That client code already exists.")
        else:
            invalidate_sessions(new_code)
    if errors:
        return templates.TemplateResponse(
            request, "user_form.html",
            {"user": data, "editing": True, "errors": errors,
             "old_client_code": old_client_code}, status_code=400,
        )
    return redirect("/admin/users", "User updated; existing sessions were revoked.")


@router.post("/users/{client_code}/toggle")
async def toggle_user(client_code: str):
    with connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM users WHERE client_code = ?", (client_code,),
        ).fetchone()
        if row is None:
            return redirect("/admin/users", "User not found.")
        enabled = 0 if row["enabled"] else 1
        conn.execute(
            "UPDATE users SET enabled = ? WHERE client_code = ?", (enabled, client_code)
        )
        record_audit(conn, "user.enabled" if enabled else "user.disabled", "User status changed", client_code)
    if not enabled:
        invalidate_sessions(client_code)
    return redirect("/admin/users", "User enabled." if enabled else "User disabled.")


@router.post("/users/{client_code}/delete")
async def delete_user(client_code: str):
    with connect() as conn:
        found = conn.execute(
            "SELECT 1 FROM users WHERE client_code = ?", (client_code,),
        ).fetchone()
        if found is None:
            return redirect("/admin/users", "User not found.")
        delete_client(conn, client_code)
        record_audit(conn, "user.deleted", "User and owned activity deleted", client_code)
    return redirect("/admin/users", "User deleted.")


@router.get("/account")
async def accounts(request: Request):
    with connect() as conn:
        rows = conn.execute(
            "SELECT users.client_code, users.name, users.enabled, "
            "accounts.* FROM users JOIN accounts USING(client_code) "
            "ORDER BY users.client_code"
        ).fetchall()
        accounts = []
        for row in rows:
            positions = conn.execute(
                "SELECT net_qty, avg_price, last_price FROM positions WHERE client_code=?",
                (row["client_code"],),
            ).fetchall()
            unrealized = round(sum(
                (position["last_price"] - position["avg_price"]) * position["net_qty"]
                for position in positions
            ), 2)
            accounts.append({
                **dict(row), "available_cash": free_cash(conn, row["client_code"]),
                **pnl_values(row["realized_pnl"], unrealized, row["total_charges"]),
            })
    return templates.TemplateResponse(request, "account.html", {"accounts": accounts})


@router.post("/account/{client_code}/funds")
async def change_funds(request: Request, client_code: str):
    data = await form_data(request)
    action = data.get("action")
    try:
        amount = Decimal(data.get("amount", "")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000000"):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return redirect("/admin/account", "Enter a positive amount up to 1,000,000,000.")
    if action not in {"add", "remove"}:
        return redirect("/admin/account", "Invalid fund action.")
    with connect() as conn:
        row = conn.execute(
            "SELECT available_balance FROM accounts WHERE client_code = ?", (client_code,),
        ).fetchone()
        if row is None:
            return redirect("/admin/account", "Account not found.")
        balance = Decimal(str(row["available_balance"])).quantize(Decimal("0.01"))
        new_balance = balance + amount if action == "add" else balance - amount
        if new_balance < 0 or (action == "remove" and amount > Decimal(str(free_cash(conn, client_code)))):
            return redirect("/admin/account", "Cannot remove more than the available balance.")
        conn.execute(
            "UPDATE accounts SET available_balance = ? WHERE client_code = ?",
            (float(new_balance), client_code),
        )
        record_audit(conn, "funds." + action, str(amount), client_code)
    return redirect("/admin/account", "Funds updated.")


@router.get("/rate-limits")
async def rate_limits_page(request: Request):
    return templates.TemplateResponse(request, "rate_limits.html", {"limits": list_limits(), "counters": limit_status()})


@router.post("/rate-limits")
async def update_rate_limit(request: Request):
    data = await form_data(request)
    try:
        save_limit(
            data.get("scope", "endpoint"), data.get("target", ""),
            int(data.get("per_second", "0")), int(data.get("per_minute", "0")),
            int(data.get("per_hour", "0")), data.get("enabled") == "on",
        )
    except (TypeError, ValueError):
        return redirect("/admin/rate-limits", "Enter a valid endpoint/group and non-negative limits.")
    audit("rate_limit.updated", f"{data.get('scope', 'endpoint')} {data.get('target', '')}")
    return redirect("/admin/rate-limits", "Rate limit saved and counters cleared.")


@router.get("/faults")
async def faults_page(request: Request):
    return templates.TemplateResponse(request, "faults.html", {"active": active_fault()})


@router.post("/faults")
async def update_fault(request: Request):
    data = await form_data(request)
    try:
        start_fault(data.get("mode", data.get("fault_type", "unavailable")), data.get("duration", ""))
    except (TypeError, ValueError):
        return redirect("/admin/faults", "Enter a valid fault mode and positive duration.")
    return redirect("/admin/faults", "Fault started.")


@router.post("/faults/clear")
async def stop_fault():
    clear_fault()
    return redirect("/admin/faults", "Fault cleared.")

RESETS = {
    "today": ("Clear today's activity", "Delete orders created today and today's trades, including trades and events belonging to those orders. Release their open reservations. Keep cash, charges, positions and holdings. Dates use the server's local clock."),
    "open-orders": ("Clear open orders", "Delete all OPEN/PENDING orders and their events, releasing reserved funds. Keep filled orders and trades."),
    "positions": ("Clear positions", "Delete all positions and holdings; clear used funds and realized P&L. Keep funded balances, charged fees, order/trade history and open-order reservations. No closing trades are created."),
    "hijack": ("Clear HIJACK overrides", "Delete every manual quote and candle override. All symbols return to Yahoo mode."),
    "rate-limits": ("Restore rate-limit defaults", "Replace all rate-limit rules with simulator defaults and clear every request counter."),
    "charges": ("Restore charge defaults", "Restore zero-rate simulator charge defaults. Historical charges on accounts and trades remain unchanged."),
    "full": ("Full simulator reset", "Delete all users, sessions, balances, orders, trades, positions, holdings, GTT rules, audit/fault history, market overrides and cached market data. Restore the DUMMY001 user with password password, API key DUMMY_API_KEY, TOTP 123456 and zero funds; restore default symbols, charges and rate limits. Clear any active fault and record this reset."),
}


def reset_state(conn, action):
    if action in {"today", "open-orders"}:
        where, params = "status IN ('OPEN', 'PENDING')", ()
        if action == "today":
            today = datetime.now()
            where = "(substr(created_at, 1, 11)=? OR substr(created_at, 1, 10)=?)"
            params = (today.strftime("%d-%b-%Y"), today.date().isoformat())
            conn.execute("DELETE FROM trades WHERE substr(trade_time, 1, 11)=? OR substr(trade_time, 1, 10)=?", params)
        conn.execute(f"DELETE FROM trades WHERE order_id IN (SELECT order_id FROM orders WHERE {where})", params)
        conn.execute(f"DELETE FROM order_events WHERE order_id IN (SELECT order_id FROM orders WHERE {where})", params)
        conn.execute(f"DELETE FROM orders WHERE {where}", params)
    if action in {"positions", "full"}:
        conn.execute("DELETE FROM positions")
        conn.execute("DELETE FROM holdings")
        conn.execute("UPDATE accounts SET used_funds=0")
    if action in {"hijack", "full"}:
        conn.execute("DELETE FROM market_overrides")
        conn.execute("DELETE FROM market_override_candles")
    if action in {"rate-limits", "full"}:
        conn.execute("DELETE FROM rate_limit_config")
        conn.executemany(
            "INSERT INTO rate_limit_config(scope, target, per_second, per_minute, per_hour) VALUES (?, ?, ?, ?, ?)",
            DEFAULT_LIMITS,
        )
    if action in {"charges", "full"}:
        conn.execute("DELETE FROM charge_config")
        init_charges(conn)
    if action == "full":
        for table in ("order_events", "trades", "orders", "gtt_rules", "sessions", "accounts", "users", "fault_events", "audit_log", "symbol_mappings"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE fault_state SET mode='', started_at=NULL, ends_at=NULL WHERE id=1")
        conn.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                     ("DUMMY001", password_hash("password"), "DUMMY_API_KEY", "123456", "Local Test User", "dummy@example.test", "9000000000"))
        conn.execute("INSERT INTO accounts(client_code) VALUES ('DUMMY001')")
        conn.executemany("INSERT INTO symbol_mappings(exchange, tradingsymbol, symboltoken, yahoo_symbol) VALUES (?, ?, ?, ?)", DEFAULT_MAPPINGS)
    record_audit(conn, "reset." + action, RESETS[action][0])


@router.get("/resets")
async def resets_page(request: Request):
    return templates.TemplateResponse(request, "resets.html", {"resets": RESETS})


@router.get("/resets/{action}")
async def reset_confirmation(request: Request, action: str):
    return templates.TemplateResponse(request, "reset_confirm.html", {"action": action, "reset": RESETS.get(action)}, status_code=200 if action in RESETS else 404)


@router.post("/resets/{action}")
async def reset_simulator(request: Request, action: str):
    if action not in RESETS:
        return templates.TemplateResponse(request, "reset_confirm.html", {"action": action, "reset": None}, status_code=404)
    if (await form_data(request)).get("confirm") != "RESET":
        return templates.TemplateResponse(request, "reset_confirm.html", {"action": action, "reset": RESETS[action], "error": "Type RESET to confirm this operation."}, status_code=400)
    with FAULT_LOCK, connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        reset_state(conn, action)
    if action in {"rate-limits", "full"}:
        limiter.clear()
    if action == "full":
        clear_fault()
        CACHE.clear()
    return redirect("/admin", RESETS[action][0] + " completed.")
