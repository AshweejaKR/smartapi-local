# Local SmartAPI-Compatible REST Server — Plan

## Goal

Build a local REST test double for Angel One SmartAPI. Official `smartapi-python` should work with only the base URL changed.

```text
SmartConnect(api_key="DUMMY_API_KEY", root="http://127.0.0.1:8000")
```

REST v1 only. WebSocket support is optional later.

## Design

- Python + FastAPI + Uvicorn
- SQLite persistence
- Jinja2 Admin UI
- Yahoo Finance (`yfinance`) default market data
- one process / one port: `127.0.0.1:8000`
- Admin UI: `/admin`
- SDK route list: `ROUTES.md`

## Required behavior

### SmartAPI compatibility

Match current `SmartConnect._routes` as closely as practical:

- method/path
- request fields and headers
- authentication
- HTTP status
- success/error JSON shape
- field names/types/null behavior
- orders/trades/positions/holdings/RMS
- rate-limit errors

### Authentication

Dummy user fields: client code, password, API key, fixed TOTP, profile data and enabled state.

Login requires all four:

```text
clientCode + password + API key + fixed TOTP
```

Support JWT, refresh/feed token, expiry, forced expiry, logout and SmartAPI-style failures.

### Market data

Default = Yahoo Finance. Store SmartAPI symbol/token -> Yahoo symbol mapping.

Support:

- LTP and volume
- OHLCV/history
- `YAHOO` or `HIJACK` per symbol
- admin override for LTP/volume/OHLCV
- last-known cache

REST and execution must use the same effective market-data service.

### Orders

Support MARKET/LIMIT BUY/SELL, modify, cancel, order book and trade book.

```text
MARKET: OPEN/PENDING -> wait configurable delay (default 1000 ms) -> fill at latest effective LTP
LIMIT BUY: fill when LTP <= limit
LIMIT SELL: fill when LTP >= limit
```

Triggered LIMIT v1 fill price = limit price. Cancelled/rejected orders never fill. Background checking must not depend on Admin UI.

### Account/portfolio

Persist in SQLite:

- balance/funds
- orders/trades
- positions/holdings
- realized/unrealized P&L
- charges

Insufficient funds must reject the order. State survives restart.

### Charges

Configurable brokerage, STT/CTT, exchange charge, GST, SEBI, stamp duty and custom charge. Support flat/percentage, BUY/SELL/BOTH, enable/disable.

Store charge breakup per fill and expose:

```text
Gross P&L - Charges = Net P&L
```

### Rate limits and faults

Rate limits: configurable per route/group for second/minute/hour.

Fault simulation: API unavailable, slow/timeout, HTTP 500/503 and auth failure for N seconds. `/admin` and `/health` stay reachable.

### Admin UI

Simple Jinja/HTML only. Control users, funds, market/HIJACK, candles, orders, positions, holdings, charges, limits, faults, audit and resets.

### Security/logging

Never log plaintext passwords, TOTP or auth tokens. Destructive reset requires confirmation.

## Phase status

| Phase | Scope | Status | Date |
|---|---|---|---|
| 1 | Foundation/routes | DONE | 2026-09-08 |
| 2 | Authentication/session | DONE | 2026-09-08 |
| 3 | Admin users/funds | DONE | 2026-09-08 |
| 4 | Yahoo market data | DONE | 2026-09-08 |
| 5 | HIJACK market data | DONE | 2026-09-08 |
| 6 | RMS/portfolio persistence | DONE | 2026-09-08 |
| 7 | MARKET/LIMIT execution | DONE | 2026-09-08 |
| 8 | Brokerage/taxes/P&L | DONE | 2026-09-08 |
| 9 | Rate limiting | DONE | 2026-09-09 |
| 10 | Fault simulation | DONE | 2026-09-09 |
| 11 | Remaining REST routes | DONE | 2026-09-09 |
| 12 | Admin monitoring/reset | DONE | 2026-09-09 |
| 13 | SDK regression tests | DONE | 2026-09-09 |
| 14 | Real vs local parity | IN PROGRESS | - |

Phase 13 verification: 136 regression tests passed, live Yahoo test passed, restart persistence verified.

Status values: `TODO`, `IN PROGRESS`, `BLOCKED`, `DONE`. Mark `DONE` only after acceptance tests pass.

# Phase 14 — Real SmartAPI vs Local parity

## Goal

Create two official SDK clients and call equivalent APIs:

```text
REAL  -> Angel One SmartAPI
LOCAL -> SmartConnect(..., root="http://127.0.0.1:8000")
```

Compare format/behavior, not changing live values.

Compare:

- SDK result/exception
- `status`, `message`, `errorcode`, `data`
- keys, nested shape, types and null behavior
- order/trade/position/holding/RMS structures

Normalize only dynamic values: tokens, IDs, timestamps and live prices. Missing keys, wrong types or different success/failure behavior must remain failures. Lists compare representative element schema, not account-history length.

## Secrets and safety

Real secrets file:

```text
D:\smartapi\angelone_keys.env
```

Never print/store password, API key, TOTP, JWT, refresh token or feed token.

Controls:

```text
LOCAL_ROOT=http://127.0.0.1:8000
ENABLE_REAL_ORDERS=false
ENABLE_REAL_200_QTY=false
ENABLE_REAL_MCX_ORDERS=false
CLEANUP_ENABLED=true
```

Track only orders created by this run. Never cancel/modify unrelated orders or positions.

## Test matrix

### A — Login failures

Run separately:

- wrong API key
- wrong client code
- wrong password
- wrong TOTP
- all wrong

Only the target credential should be wrong in the first four cases.

### B — Login/account

- correct login
- profile/account
- RMS/balance
- set LOCAL starting balance equal to REAL usable balance

### C — NIFTYBEES discovery/LTP

Discover tokens; never hardcode.

```text
searchScrip("NSE", "NIFTYBEES") -> exact NIFTYBEES-EQ
searchScrip("BSE", "NIFTYBEES") -> use REAL result if available
```

Compare NSE/BSE LTP response structures.

### D/E — NIFTYBEES orders

After parity orders compare order book, trade book, positions, holdings and RMS.

BUY:

- LOCAL MARKET 200; REAL disabled by default
- LIMIT BUY 200 at ~3% below LTP; REAL only with 200-qty flag
- MARKET BUY 1 REAL+LOCAL
- LIMIT BUY 1 at ~3% below LTP REAL+LOCAL

SELL:

- MARKET SELL 1 controlled quantity
- LIMIT SELL 1 at ~3% above LTP
- optional LOCAL 200 SELL; REAL only with flag

Local-only 200-qty scenarios are reported as `LOCAL_ONLY`; do not compare their later account state with REAL.

### F — Cancel

Cancel only test-created open LIMIT orders. Recheck order/trade/position/holding/RMS state.

### G — Invalid orders

Compare REAL and LOCAL for:

- wrong symbol BUY/SELL
- invalid MCX/BSE combination
- quantity `0` and negative
- invalid token
- mismatched symbol/token
- invalid order type/product type

### H — Historical NIFTYBEES

Compare candle structure for:

```text
ONE_MINUTE
FIFTEEN_MINUTE
ONE_DAY
```

Use same bounded date ranges. Compare row schema, not exact Yahoo-vs-broker values.

### I — MCX GOLDPETAL30SEP26FUT

First:

```text
searchScrip("MCX", "GOLDPETAL30SEP26FUT")
```

Require exact symbol. Never guess token. If unavailable on REAL -> `SKIPPED_NOT_AVAILABLE`.

If available and real-MCX flag enabled:

- compare LTP
- MARKET BUY 1
- compare order/trade/position/holding/RMS
- MARKET SELL 1 to close/reduce controlled position
- compare state again
- confirm no test-created MCX orders remain open

Use `CARRYFORWARD` for MCX futures. Do not assume futures appear in holdings; copy REAL structural behavior.

### J — Session errors

Compare invalid bearer, missing authorization, wrong API key on authenticated call, expired/invalid bearer, logout and request after logout.

### K — Local-only regression

Keep regression tests for 1000 ms MARKET delay, HIJACK values/candles, rate limits, charges, insufficient funds, outages and restart persistence.

## Reports

Generate:

```text
reports/smartapi_parity_YYYYMMDD_HHMMSS.json
reports/smartapi_parity_YYYYMMDD_HHMMSS.md
```

Each case records request summaries with redaction, REAL/LOCAL SDK results, normalized results, schema/type/behavior PASS/FAIL, differences, real-order flag and cleanup result.

## Cleanup

Always use failure-safe cleanup:

- cancel only test-created open orders
- close only controlled positions when enabled/safe
- logout sessions
- write report even if cleanup partly fails

## Phase 14 acceptance

Phase 14 is `DONE` when login/account, NIFTYBEES LTP/orders/errors/history, MCX discovery/controlled flow, session errors, cleanup and JSON/Markdown reporting complete successfully and all LOCAL incompatibilities are recorded.

## Codex rule

For future changes: read README/PLAN first, inspect existing code, keep changes small, preserve SmartAPI names/behavior, add/update tests, run tests, and do not mark a phase `DONE` before acceptance passes.

## Later optional work

WebSocket compatibility only if AutoTick needs it: SmartWebSocketV2 subscriptions/ticks, HIJACK ticks, order updates and disconnect simulation.
