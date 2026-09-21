# Local SmartAPI-Compatible REST Server — Plan

## Goal

Local FastAPI test double for Angel One SmartAPI. Official `smartapi-python` should work by changing only `root`.

```python
SmartConnect(api_key="DUMMY_API_KEY", root="http://127.0.0.1:8000")
```

REST only. WebSocket support is optional later.

## Design

- FastAPI + Uvicorn
- SQLite persistence
- Jinja2 Admin UI
- Yahoo Finance market data with per-symbol HIJACK overrides
- YAML-selectable Yahoo, dummy or Angel One data forwarding
- one process / one port
- SDK route inventory: `ROUTES.md`

## Core behavior

Support authentication/session tokens, users/funds, LTP/OHLCV/history, MARKET/LIMIT BUY/SELL, modify/cancel, orders/trades/positions/holdings/RMS, configurable charges, rate limits, faults, audit/reset controls and restart persistence.

Rules:
- MARKET fills after configurable delay at effective LTP.
- LIMIT BUY fills at/under limit; LIMIT SELL at/over limit.
- invalid/cancelled/rejected orders never fill.
- REST and execution use the same market-data source.
- secrets/tokens must not be logged.
- Admin and `/health` remain available during simulated REST faults.

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundation/routes | DONE |
| 2 | Authentication/session | DONE |
| 3 | Admin users/funds | DONE |
| 4 | Yahoo market data | DONE |
| 5 | HIJACK market data | DONE |
| 6 | RMS/portfolio persistence | DONE |
| 7 | MARKET/LIMIT execution | DONE |
| 8 | Charges/P&L | DONE |
| 9 | Rate limiting | DONE |
| 10 | Fault simulation | DONE |
| 11 | Remaining REST routes | DONE |
| 12 | Admin monitoring/reset | DONE |
| 13 | SDK regression tests | DONE |
| 14 | Real vs local parity | IN PROGRESS |

Phase 13 acceptance covered regression tests, live Yahoo verification and restart persistence.

## Phase 14 — Real vs local parity

Use two official SDK clients:

```text
REAL  -> Angel One SmartAPI
LOCAL -> SmartConnect(..., root="http://127.0.0.1:8000")
```

Compare SDK result/exception, `status/message/errorcode/data`, nested keys/types/nulls and order/trade/position/holding/RMS structures. Normalize only dynamic tokens, IDs, timestamps and live prices.

### Safety

Real credentials stay outside the repo:

```text
REAL_ENV_FILE=D:\smartapi\angelone_keys.env
LOCAL_ROOT=http://127.0.0.1:8000
ENABLE_REAL_ORDERS=false
ENABLE_REAL_200_QTY=false
ENABLE_REAL_MCX_ORDERS=false
CLEANUP_ENABLED=true
```

Track and clean up only orders/positions created by the parity run.

### Coverage

- auth failures and successful login/profile/RMS
- NIFTYBEES NSE/BSE discovery and LTP
- safe MARKET/LIMIT BUY/SELL tests
- test-created order cancellation
- invalid order/symbol/token/product cases
- ONE_MINUTE/FIFTEEN_MINUTE/ONE_DAY history
- exact `GOLDPETAL30SEP26FUT` MCX discovery and optional controlled qty-1 flow
- invalid/missing/expired auth and logout behavior
- local-only delay/HIJACK/rate-limit/charges/funds/fault/restart regression

### Reports

```text
reports/smartapi_parity_YYYYMMDD_HHMMSS.json
reports/smartapi_parity_YYYYMMDD_HHMMSS.md
```

Each case records redacted requests/results, normalized comparison, schema/type/behavior PASS/FAIL and cleanup results.

### Acceptance

Phase 14 becomes `DONE` only after the real-vs-local run completes successfully, cleanup is verified, reports are generated, and remaining incompatibilities are recorded.

## Change rule

Keep changes small, preserve SmartAPI behavior, update tests with code changes, and do not mark a phase DONE before acceptance passes.

## Source selection

`default.yaml` selects market, order and account sources independently. Angel One mode logs in with the external credentials file and returns its response unchanged for selected routes. It never stores real orders, positions, holdings or funds in local SQLite. `None` market mode returns LTP `100.05` and 25 sample candles.
