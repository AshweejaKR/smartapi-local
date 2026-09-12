# Local SmartAPI-Compatible REST Server — Implementation Plan

## 1. Goal
Build a local Python REST server that behaves like Angel One SmartAPI closely enough that the official `smartapi-python` SDK can talk to it with only the base URL changed.

Primary usage:

```text
SmartConnect(api_key="DUMMY_API_KEY", root="http://127.0.0.1:8000")
```

The local server should support the same REST URL paths exposed by the SmartAPI Python SDK, use SmartAPI-compatible request/response structures, provide simulated trading/account behavior, use Yahoo Finance for default market data, and expose a simple local Admin UI for changing simulator state.

Version 1 scope is REST only. SmartAPI WebSocket compatibility is a separate later phase if AutoTick needs it.

---

## 2. Core Design

Use:

- Python
- FastAPI
- Uvicorn
- Jinja2 templates
- SQLite
- SQLAlchemy or Python sqlite3; prefer the simpler option during implementation
- yfinance for Yahoo Finance data
- pytest for automated tests

Single process and single port:

```text
http://127.0.0.1:8000
```

Examples:

```text
SmartAPI REST API:
http://127.0.0.1:8000/rest/...

Admin UI:
http://127.0.0.1:8000/admin
```

The Admin UI is optional. The REST server must work normally even if nobody opens the Admin UI.

---

## 3. Main Requirements

### 3.1 SmartAPI SDK compatibility

Implement every REST route listed in the current `SmartConnect._routes` dictionary from the official SmartAPI Python SDK.

The server should match, as closely as practical:

- HTTP method
- URL path
- request JSON fields
- headers
- authentication behavior
- HTTP status codes
- success response schema
- failure response schema
- field names
- null behavior
- order/position/holding structures
- rate-limit failures

The official SDK should work by changing only its `root` URL.

### 3.2 Authentication

Use dummy users configured from Admin UI.

Each user contains at minimum:

- client code
- password
- API key/private key
- fixed TOTP
- name
- email
- mobile number
- exchanges/products if required by SmartAPI profile response
- enabled/disabled status

No rotating TOTP implementation is required.

Login validation:

```text
clientCode + password + API key + fixed TOTP
```

All must match a configured enabled dummy account.

Correct credentials:

- return SmartAPI-compatible login success
- generate dummy JWT token
- generate refresh token
- generate feed token
- persist active session/token metadata

Wrong credentials:

- return SmartAPI-style authentication failure
- preserve expected response fields such as status/message/errorcode/data

Support token expiry and forced expiry for testing.

### 3.3 Market data

Default data source:

```text
Yahoo Finance
```

Support:

- LTP
- volume
- OHLCV
- historical candles where Yahoo supports requested timeframe

Use a symbol mapping layer because SmartAPI symbols/tokens and Yahoo symbols differ.

Examples:

```text
NSE symbol -> Yahoo symbol
RELIANCE-EQ -> RELIANCE.NS
```

Admin can configure mappings.

### 3.4 HIJACK market-data mode

Admin can override selected symbols.

Per symbol, support:

```text
YAHOO mode
HIJACK mode
```

YAHOO mode:

- return Yahoo-fetched data

HIJACK mode:

- return admin-controlled values
- manual LTP
- manual volume
- manual OHLCV candles

Switching HIJACK off should immediately return to Yahoo data.

The API layer should not know whether data came from Yahoo or HIJACK. Use one market-data service interface.

### 3.5 Orders

Support at least:

- MARKET
- LIMIT
- BUY
- SELL
- cancel
- modify
- order book
- trade book

MARKET order behavior:

1. order accepted
2. order appears OPEN/PENDING
3. wait configurable delay
4. default delay = 1000 ms
5. fetch latest effective LTP from Yahoo or HIJACK source
6. fill order
7. update funds, trades, positions and P&L

LIMIT BUY behavior:

```text
remain OPEN until LTP <= limit price
```

LIMIT SELL behavior:

```text
remain OPEN until LTP >= limit price
```

Version 1 fill price for a triggered limit order:

```text
limit price
```

Cancelled/rejected orders must never fill.

Order-monitoring logic must run on the server side and must not depend on Admin UI being open.

### 3.6 Account and funds

Admin UI should allow:

- add funds
- remove funds
- set account balance if needed

Trading should update:

- available cash
- used margin/funds
- realized P&L
- unrealized P&L
- total charges
- net P&L

Insufficient funds should produce SmartAPI-compatible order rejection behavior.

### 3.7 Positions and holdings

Persist state in SQLite.

Store:

- orders
- trades
- open positions
- closed positions
- holdings
- account balance
- charges

At end of day or on server restart, positional delivery holdings must remain available.

Intraday and positional behavior can stay simple in v1, but persisted holdings must survive process restart.

### 3.8 Brokerage and taxes

Create a configurable charges engine.

Support configurable values for:

- brokerage
- STT/CTT
- exchange transaction charges
- GST
- SEBI charges
- stamp duty
- other/custom charges

Allow:

- flat fee
- percentage fee
- buy-only charge
- sell-only charge
- both sides
- enable/disable each charge

Do not hardcode current Angel One tax/brokerage values into core logic.

Admin UI can increase/decrease/change each configured charge.

Per trade store:

```text
gross trade value
brokerage
STT/CTT
exchange charge
GST
SEBI charge
stamp duty
other charge
total charges
```

P&L presentation:

```text
Gross P&L
- Total Charges
= Net P&L
```

### 3.9 Rate limits

Implement configurable rate limiting.

Rate limits should be configurable per endpoint or route group.

Support limits such as:

- requests/second
- requests/minute
- requests/hour

Admin UI can modify limits at runtime.

Rate-limit failures should use SmartAPI-compatible response structure/status behavior.

Persist rate-limit configuration in SQLite.

### 3.10 Fault simulation

Admin UI should allow temporary REST failure simulation.

Important design:

- Admin UI stays reachable
- SmartAPI REST paths become unavailable/failing

Configurable fault duration:

```text
seconds
```

Default UI can allow values such as 1–60 seconds, but server configuration should not hardcode that range unnecessarily.

Fault types:

- API unavailable
- timeout/slow response
- HTTP 500
- HTTP 503
- token/authentication failure

Minimum required v1 feature:

```text
make SmartAPI REST paths unavailable for N seconds
```

Do not block `/admin` during fault simulation.

Log fault start/end times.

### 3.11 Admin UI

Use simple Python/Jinja2 HTML.

No React.
No separate frontend build system.
No separate admin process.
No separate port.

Recommended pages:

```text
/admin
/admin/account
/admin/users
/admin/market
/admin/candles
/admin/orders
/admin/positions
/admin/holdings
/admin/rate-limits
/admin/charges
/admin/faults
/admin/logs
/admin/reset
```

The UI may use normal HTML forms and POST handlers.

It does not need to expose a separate public JSON admin API unless implementation becomes cleaner with internal form endpoints.

### 3.12 Logging and audit

Log:

- REST requests
- response status
- login attempts
- orders
- fills
- rejections
- rate-limit failures
- admin configuration changes
- fund changes
- HIJACK changes
- charge changes
- outages/fault simulation
- resets

Do not log plain-text passwords or authorization tokens.

### 3.13 Reset controls

Admin UI should support:

- reset today's orders/trades
- clear open orders
- reset positions
- clear HIJACK overrides
- restore default rate limits
- restore default charges
- full simulator reset

Protect destructive reset using a simple confirmation page/button.

---

## 4. Suggested Project Structure

```text
smartapi-local/
├── app.py
├── config.py
├── requirements.txt
├── README.md
├── PLAN.md
│
├── api/
│   ├── __init__.py
│   ├── auth.py
│   ├── account.py
│   ├── orders.py
│   ├── market.py
│   ├── portfolio.py
│   ├── historical.py
│   ├── gtt.py
│   └── misc.py
│
├── admin/
│   ├── __init__.py
│   ├── routes.py
│   └── templates/
│       ├── base.html
│       ├── index.html
│       ├── account.html
│       ├── users.html
│       ├── market.html
│       ├── candles.html
│       ├── orders.html
│       ├── positions.html
│       ├── holdings.html
│       ├── rate_limits.html
│       ├── charges.html
│       ├── faults.html
│       └── reset.html
│
├── services/
│   ├── auth_service.py
│   ├── account_service.py
│   ├── order_service.py
│   ├── position_service.py
│   ├── execution_service.py
│   ├── charges_service.py
│   └── fault_service.py
│
├── market/
│   ├── provider.py
│   ├── yahoo_provider.py
│   ├── hijack_provider.py
│   ├── market_service.py
│   └── symbol_mapper.py
│
├── rate_limit/
│   └── limiter.py
│
├── db/
│   ├── database.py
│   ├── schema.py
│   └── repository.py
│
├── smartapi/
│   ├── routes.py
│   ├── responses.py
│   ├── errors.py
│   └── schemas.py
│
└── tests/
    ├── test_auth.py
    ├── test_market.py
    ├── test_orders.py
    ├── test_positions.py
    ├── test_rate_limits.py
    ├── test_faults.py
    └── test_smartapi_sdk.py
```

Keep modules smaller if practical. Do not create abstractions that are not needed.

---

# 5. Phase Status Tracking

Use this table as the single progress tracker. Codex must update it only after the phase tests pass.

| Phase | Name | Status | Completed Date | Commit/PR | Notes |
|---|---|---|---|---|---|
| 1 | Project foundation and SmartAPI route inventory | DONE | 2026-09-08 | Completed before Phase 2 | Foundation/routes completed and covered by later SDK regression. |
| 2 | Dummy authentication and session handling | DONE | 2026-09-08 | Implement Phase 2 local authentication | Fixed-TOTP local authentication completed. |
| 3 | Admin user and account controls | DONE | 2026-09-08 | Implement Phase 3 admin user and account controls | Jinja user/fund controls; credential changes revoke sessions. |
| 4 | Yahoo Finance market-data provider | DONE | 2026-09-08 | Implement Phase 4 Yahoo Finance market data provider | SQLite mappings and bounded last-known cache. |
| 5 | HIJACK market-data override | DONE | 2026-09-08 | Implement Phase 5 HIJACK market-data override | SQLite overrides, shared market service and Admin UI. |
| 6 | Account, RMS and core portfolio models | DONE | 2026-09-08 | Implement Phase 6 account and portfolio persistence | SQLite-backed RMS, order, trade, position and holding views. |
| 7 | MARKET and LIMIT execution engine | DONE | 2026-09-08 | Implement Phase 7 MARKET and LIMIT execution engine | Persistent lifecycle events and server-side fill checker. |
| 8 | Brokerage, taxes and net P&L | DONE | 2026-09-08 | Local file edits | SQLite charge rules, fill breakup, fund deductions and gross/net P&L; later regression coverage completed. |
| 9 | Configurable rate limiting | DONE | 2026-09-09 | Implement Phase 9 configurable rate limiting | SQLite-backed in-process endpoint/group limits with runtime Admin UI. |
| 10 | Fault and outage simulation | DONE | 2026-09-09 | Implement Phase 10 fault simulation | SQLite fault windows, Admin UI modes, automatic expiry and SDK recovery test. |
| 11 | Remaining SmartAPI REST route compatibility | DONE | 2026-09-09 | Local file edits | Current SmartConnect REST routes wired and covered by compatibility regression. |
| 12 | Admin monitoring, audit and resets | DONE | 2026-09-09 | Implement Phases 12-13 admin monitoring and SDK regression coverage | Dashboard, monitoring pages, audit log and confirmed resets. |
| 13 | Full SDK compatibility and regression tests | DONE | 2026-09-09 | Implement Phases 12-13 admin monitoring and SDK regression coverage | Official SDK regression suite: 136 passed; Yahoo and restart persistence verification completed. |
| 14 | Real SmartAPI vs Local SmartAPI parity testing | IN PROGRESS | - | - | Controlled REAL-vs-LOCAL parity runner being implemented. |

Allowed status values:

```text
TODO
IN PROGRESS
BLOCKED
DONE
```

Completion rule:

- Mark `IN PROGRESS` when implementation starts.
- Mark `BLOCKED` only when a real external dependency prevents completion.
- Mark `DONE` only after phase acceptance checks and tests pass.
- When marking `DONE`, fill `Completed Date` and `Commit/PR`.
- Add one short note if behavior changed from the original plan.

---

# 6. Implementation Phases


## Phase 1 — Project foundation and SmartAPI route inventory

**Status:** `DONE`
**Completed:** `2026-09-08`  
**Commit/PR:** `Completed before Phase 2`


### Goal
Create the FastAPI project, SQLite initialization, SmartAPI response helpers, and exact route inventory from the official Python SDK.

### Tasks

- create minimal project structure
- add FastAPI/Uvicorn startup
- configure SQLite
- create `/admin` placeholder page
- inspect official current `SmartConnect._routes`
- create one local route mapping document/table
- register placeholder handlers for every SDK REST route
- create common SmartAPI success/error response functions
- add `/health` endpoint outside SmartAPI compatibility paths

### Acceptance

- server starts
- `/admin` loads
- `/health` returns OK
- every SDK REST URL resolves to a handler instead of 404
- placeholder responses use SmartAPI-like response envelope

### Codex prompt

> Implement Phase 1 of the local SmartAPI-compatible server. Read PLAN.md first. Use Python FastAPI, Jinja2 and SQLite. Keep code simple and short. Inspect the current official `angel-one/smartapi-python` `SmartConnect._routes` and create handlers/placeholders for every REST route using the same paths and HTTP methods. Add common SmartAPI-compatible success/error response helpers, SQLite initialization, `/health`, and a basic `/admin` page. Do not implement trading logic yet. Add tests proving every SDK route is registered and does not return 404. Do not overengineer.

---

## Phase 2 — Dummy authentication and session handling

**Status:** `DONE`  
**Completed:** `2026-09-08`
**Commit/PR:** `Implement Phase 2 local authentication`


### Goal
Make official SDK login/session calls work against local server.

### Tasks

- dummy-user SQLite table
- seed one default test user
- fixed TOTP
- API key validation
- password validation
- user enabled/disabled flag
- loginByPassword
- generateTokens
- getProfile
- logout
- dummy JWT/refresh/feed tokens
- token expiry
- forced token expiry support
- SmartAPI-style login failures

### Acceptance

Official SDK can run:

```text
generateSession()
getProfile()
generateToken()
terminateSession()
```

Correct dummy credentials succeed.
Wrong client code/password/API key/TOTP fail.
Disabled user fails.
Expired token fails.

### Codex prompt

> Implement Phase 2 from PLAN.md. Add SQLite-backed dummy users and SmartAPI-compatible authentication/session behavior. Use fixed TOTP only; no rotating TOTP algorithm. Validate clientCode, password, X-PrivateKey/API key and fixed TOTP. Implement loginByPassword, generateTokens, getProfile and logout using SmartAPI-compatible response structures. Generate local dummy JWT, refresh and feed tokens with configurable expiry. Wrong credentials, disabled users and expired tokens must return SmartAPI-style failures. Seed one documented dummy user for tests. Add official `smartapi-python` SDK integration tests using only `root=http://127.0.0.1:<test-port>` change. Keep implementation small.

---

## Phase 3 — Admin user and account controls

**Status:** `DONE`
**Completed:** `2026-09-08`
**Commit/PR:** `Implement Phase 3 admin user and account controls`


### Goal
Create basic Admin UI for dummy credentials and funds.

### Tasks

Admin pages for:

- list users
- add user
- edit user
- delete/disable user
- set fixed TOTP
- set API key
- add funds
- remove funds
- show available balance

Do not show stored password hashes.

### Acceptance

Admin can create a user and immediately use it through SDK login.
Admin can add/remove funds and value persists after restart.

### Codex prompt

> Implement Phase 3 from PLAN.md. Build a simple Jinja2 Admin UI under `/admin` on the same FastAPI server and port. Add pages/forms to list/add/edit/disable/delete dummy users and configure client code, password, API key and fixed TOTP. Add account controls to add/remove funds and show balances. Persist everything in SQLite. UI should use plain HTML/Jinja forms; no React, no separate admin API/server/port. Changes must take effect immediately for SmartAPI authentication. Add basic validation and tests.

---

## Phase 4 — Yahoo Finance market-data provider

**Status:** `DONE`
**Completed:** `2026-09-08`
**Commit/PR:** `Implement Phase 4 Yahoo Finance market data provider`


### Goal
Provide default real market data without Admin UI dependency.

### Tasks

- Yahoo/yfinance provider
- symbol mapping table
- LTP
- volume
- OHLCV
- historical candles
- caching/last-known value
- SmartAPI market-data response conversion

### Acceptance

If Admin UI is never opened, server still fetches Yahoo data and serves SmartAPI market endpoints.
Server restart does not require UI interaction.

### Codex prompt

> Implement Phase 4 from PLAN.md. Add a simple Yahoo Finance market-data provider using `yfinance`. Add configurable SmartAPI-symbol/token to Yahoo-symbol mapping persisted in SQLite. Implement LTP, volume, OHLCV and historical-candle retrieval needed by SmartAPI REST market endpoints, converting Yahoo data into SmartAPI-compatible response fields. Add a small last-known cache so a temporary Yahoo failure can return cached data when appropriate or a SmartAPI-style error when no cache exists. The server must operate fully without Admin UI interaction. Keep provider logic simple and testable.

---

## Phase 5 — HIJACK market-data override

**Status:** `DONE`
**Completed:** `2026-09-08`
**Commit/PR:** `Implement Phase 5 HIJACK market-data override`


### Goal
Allow controlled market movement from Admin UI.

### Tasks

Per symbol:

- YAHOO/HIJACK mode
- override LTP
- override volume
- override OHLCV
- manually edit/add candle
- display active data source

Create one market service selecting effective data source.

### Acceptance

HIJACK ON → SmartAPI endpoint returns configured values.
HIJACK OFF → immediately returns Yahoo values.
Orders use same effective market data.

### Codex prompt

> Implement Phase 5 from PLAN.md. Add per-symbol `YAHOO` and `HIJACK` modes. In Admin UI, allow selecting a symbol and overriding LTP, volume and OHLCV/candle data. Persist overrides in SQLite. Create one market-data service used by all REST and later execution logic: if hijack is enabled use override data, otherwise Yahoo provider. Switching mode must take effect immediately. Show the current source in Admin UI. Do not duplicate market selection logic across endpoints.

---

## Phase 6 — Account, RMS and core portfolio models

**Status:** `DONE`  
**Completed:** `2026-09-08`  
**Commit/PR:** `Implement Phase 6 account and portfolio persistence`


### Goal
Implement persistent account, holdings, positions, orders and trades data.

### Tasks

- account/RMS response
- order table
- trade table
- position table
- holding table
- available funds
- used funds
- realized/unrealized P&L basics
- restart persistence

### Acceptance

SDK account/RMS/position/holding/order/trade calls return SmartAPI-compatible structures.
State survives restart.

### Codex prompt

> Implement Phase 6 from PLAN.md. Add minimal SQLite models/repositories for accounts, orders, trades, positions and holdings. Implement SmartAPI-compatible RMS/account, order book, trade book, positions and holdings responses. Track available funds, used funds, realized P&L and unrealized P&L. Persist state across restart. Keep schemas limited to fields required by SmartAPI compatibility and tests; avoid unnecessary accounting complexity.

---

## Phase 7 — MARKET and LIMIT execution engine

**Status:** `DONE`
**Completed:** `2026-09-08`
**Commit/PR:** `Implement Phase 7 MARKET and LIMIT execution engine`


### Goal
Implement realistic simple order lifecycle.

### Tasks

MARKET:

- accept order
- OPEN/PENDING
- default 1000 ms delay
- latest effective LTP
- FILLED

LIMIT BUY:

```text
fill when LTP <= limit price
```

LIMIT SELL:

```text
fill when LTP >= limit price
```

Also:

- modify
- cancel
- insufficient-funds rejection
- background order checker
- configurable market fill delay

### Acceptance

MARKET order does not fill immediately.
It fills after about configured delay.
LIMIT stays open until crossing condition.
Cancelled order never fills.
Works with both Yahoo and HIJACK price changes.

### Codex prompt

> Implement Phase 7 from PLAN.md. Add the minimal order execution engine for MARKET and LIMIT BUY/SELL orders. MARKET orders must be accepted as OPEN/PENDING, wait a configurable delay defaulting to 1000 ms, then fill at the latest effective LTP from the shared market service. LIMIT BUY stays OPEN until LTP <= limit price; LIMIT SELL stays OPEN until LTP >= limit price; v1 fills at the limit price. Add cancel, modify, insufficient-funds rejection and a lightweight server-side background checker. The checker must run independently of the Admin UI. Persist all order state transitions and add deterministic tests using HIJACK prices.

---

## Phase 8 — Brokerage, taxes and net P&L

**Status:** `DONE`  
**Completed:** `2026-09-08`  
**Commit/PR:** `Local file edits`

Implemented configurable SQLite charges, `/admin/charges`, atomic fill deductions and gross/net P&L. Later Phase 13 regression coverage completed.


### Goal
Apply configurable transaction charges.

### Tasks

Configurable charges:

- brokerage
- STT/CTT
- exchange transaction charges
- GST
- SEBI charges
- stamp duty
- other/custom

Each supports suitable:

- flat/percentage
- buy/sell/both
- enabled/disabled

Admin page to edit values.

### Acceptance

Every fill stores charge breakup.
Funds reflect charges.
Gross P&L, charges and net P&L are available.
Charge configuration survives restart.

### Codex prompt

> Implement Phase 8 from PLAN.md. Create a small configurable transaction-charges engine. Support brokerage, STT/CTT, exchange charges, GST, SEBI charges, stamp duty and an optional custom charge. Allow flat or percentage calculation where appropriate, buy/sell/both applicability, and enabled/disabled status. Store configuration in SQLite and add a simple `/admin/charges` page for editing values. On every fill, store the full charge breakup, deduct charges from funds and calculate Gross P&L, Total Charges and Net P&L. Do not hardcode current real broker rates into core logic; provide editable defaults only.

---

## Phase 9 — Configurable rate limiting

**Status:** `DONE`  
**Completed:** `2026-09-09`  
**Commit/PR:** `Implement Phase 9 configurable rate limiting`


### Goal
Simulate SmartAPI rate limits.

### Tasks

- per route/route-group configuration
- per-second limits
- per-minute limits
- per-hour limits
- Admin UI editing
- SmartAPI-style failure response
- reset counters safely

### Acceptance

Changing rate limits from UI takes effect without restart.
Exceeded calls fail in SmartAPI-compatible format.

### Codex prompt

> Implement Phase 9 from PLAN.md. Add a simple configurable rate limiter for SmartAPI REST routes. Support per-second, per-minute and per-hour limits per endpoint or route group. Persist configuration in SQLite and add `/admin/rate-limits` for editing limits at runtime. When a limit is exceeded, return the closest SmartAPI-compatible HTTP status and response envelope/error fields based on current documented behavior. Add tests for limits and runtime configuration changes. Keep implementation local-process only; no Redis.

---

## Phase 10 — Fault and outage simulation

**Status:** `DONE`    
**Completed:** `2026-09-09`    
**Commit/PR:** `Implement Phase 10 fault simulation`


### Goal
Test AutoTick recovery/reconnect behavior.

### Tasks

Admin fault controls:

- REST unavailable for N seconds
- slow response/timeout
- HTTP 500
- HTTP 503
- forced auth/token failure

Critical behavior:

```text
/admin remains reachable
SmartAPI REST paths are affected
```

### Acceptance

Admin can trigger a 5-second REST outage.
SDK calls fail during window.
Admin remains usable.
REST automatically recovers when duration expires.

### Codex prompt

> Implement Phase 10 from PLAN.md. Add Admin UI fault simulation. Minimum feature: make SmartAPI REST paths unavailable/failing for a configurable number of seconds while `/admin` and `/health` remain reachable. Also add simple selectable slow/timeout, HTTP 500, HTTP 503 and forced token/auth failure modes if they stay small. Implement with middleware/service state, persist useful configuration if needed, and log fault start/end. Faults must automatically clear after configured duration. Add integration tests proving SDK failure and automatic recovery.

---

## Phase 11 — Remaining SmartAPI REST route compatibility

**Status:** `DONE`  
**Completed:** `2026-09-09`  
**Commit/PR:** `Local file edits`


### Goal
Complete every REST route exposed by official SmartAPI Python SDK.

### Areas

Depending on current SDK route inventory:

- GTT
- historical OI
- search scrip
- all holdings
- individual order details
- margin APIs
- brokerage/charge estimation
- eDIS-related routes
- option Greeks
- gainers/losers
- put/call ratio
- OI buildup
- NSE/BSE intraday routes
- any other routes currently in `SmartConnect._routes`

For features not naturally available from Yahoo, use deterministic simulated data/configuration while matching SmartAPI response structures.

### Acceptance

Every current SDK REST method has a compatible local response.
No SDK REST route returns unimplemented/404.

### Codex prompt

> Implement Phase 11 from PLAN.md. Re-read the current official `smartapi-python` repository and compare every entry in `SmartConnect._routes` and the SDK methods using them against our local server. Implement all remaining REST routes with the same paths, methods, request fields and closest practical SmartAPI response structures. Use deterministic simulated data for features Yahoo cannot provide. Do not add WebSocket support in this phase. Add a route/method compatibility test matrix and ensure no current SDK REST method hits a 404 or an intentionally unimplemented endpoint.

---

## Phase 12 — Admin monitoring, audit and resets

**Status:** `DONE`
**Completed:** `2026-09-09`
**Commit/PR:** `Implement Phases 12-13 admin monitoring and SDK regression coverage`


### Goal
Make simulator convenient for long AutoTick testing.

### Tasks

Admin pages:

- dashboard
- current funds
- market source per symbol
- orders
- open limit orders
- positions
- holdings
- trades
- charges
- rate-limit status
- active faults
- audit log

Reset operations:

- clear today's orders/trades
- clear open orders
- clear positions
- clear HIJACK values
- defaults for charges
- defaults for limits
- full simulator reset

### Acceptance

Useful state visible without database inspection.
Destructive reset requires confirmation.

### Codex prompt

> Implement Phase 12 from PLAN.md. Finish the simple Jinja2 Admin UI with a compact dashboard for funds, market source, orders, open limit orders, positions, holdings, trades, charges, rate-limit configuration/status, active faults and recent audit entries. Add reset controls for today's activity, open orders, positions, hijack overrides, rate-limit defaults, charge defaults and full simulator reset. Require a simple confirmation step for destructive resets. Keep HTML basic and Python-driven; no frontend framework.

---

## Phase 13 — Full SDK compatibility and regression tests

**Status:** `DONE`
**Completed:** `2026-09-09`
**Commit/PR:** `Implement Phases 12-13 admin monitoring and SDK regression coverage`


### Goal
Prove server behaves as a drop-in REST test double.

### Tests

Use official `smartapi-python` package directly.

Test:

- correct login
- incorrect login
- fixed TOTP failure
- API-key failure
- expired token
- profile
- refresh token
- logout
- RMS/funds
- Yahoo LTP
- HIJACK LTP
- candles
- market order 1-second fill
- limit order waiting
- limit order triggered by HIJACK
- cancel
- modify
- order book
- trade book
- positions
- holdings
- brokerage/taxes
- insufficient funds
- rate limit
- temporary outage
- restart persistence
- every SDK REST route

### Acceptance

Main usage should be approximately:

```text
from SmartApi import SmartConnect

api = SmartConnect(
    api_key="configured-dummy-key",
    root="http://127.0.0.1:8000",
)
```

No SDK source modification should be required.

### Codex prompt

> Implement Phase 13 from PLAN.md. Build a comprehensive regression/integration test suite using the official `smartapi-python` SDK without modifying the SDK. The only server-selection change should be passing `root` to `SmartConnect`. Cover authentication success/failures, token expiry, profile, funds/RMS, Yahoo and HIJACK data, candles, MARKET 1000 ms fill delay, LIMIT trigger behavior, cancel/modify, trades/positions/holdings, charges, insufficient funds, rate limits, temporary REST outage/recovery, restart persistence and every current REST route in the SDK. Fix compatibility gaps found by tests while keeping the server simple.

---

---

## Phase 14 — Real SmartAPI vs Local SmartAPI parity testing

**Status:** `IN PROGRESS`  
**Completed:** `-`  
**Commit/PR:** `-`

### Goal

Compare the official Angel One SmartAPI server and the local SmartAPI-compatible server using the same official `smartapi-python` SDK and equivalent parameters.

Create two SDK objects:

```text
REAL  -> official Angel One SmartAPI server
LOCAL -> SmartConnect(..., root="http://127.0.0.1:8000")
```

The main target is response-format and behavior compatibility.

Compare:

- SDK return value
- HTTP success/failure behavior where observable
- `status`
- `message`
- `errorcode`
- `data`
- top-level keys
- nested keys
- field types
- list/object structure
- null behavior
- exception type/message
- order/position/holding structures

Do not require dynamic values to be identical.

Normalize only dynamic values such as:

- JWT/refresh/feed tokens
- order IDs
- unique/exchange order IDs
- timestamps
- LTP
- execution price
- live market-generated values

Do not normalize missing keys, wrong field names, wrong types, success/failure mismatches or materially different error structures.

### Real broker secrets

Read real SmartAPI secrets only from:

```text
D:\smartapi\angelone_keys.env
```

Do not copy credentials into source code.

Never print or save:

- password
- API key
- TOTP
- JWT
- refresh token
- feed token

### Safety controls

Add test configuration such as:

```text
REAL_ENV_FILE=D:\smartapi\angelone_keys.env
LOCAL_ROOT=http://127.0.0.1:8000
ENABLE_REAL_ORDERS=true/false
ENABLE_REAL_200_QTY=false
ENABLE_REAL_MCX_ORDERS=true/false
CLEANUP_ENABLED=true
```

Default large/dangerous real-order tests to disabled.

Track only orders created by the parity-test run.

Never cancel or modify unrelated user orders/positions.

Always perform cleanup in a final/failure-safe path.

### Test Group A — Login failures

Run each separately while keeping all other credential fields correct:

1. wrong API key only
2. wrong client code only
3. wrong password only
4. wrong TOTP only
5. all credentials wrong

Compare REAL vs LOCAL:

- SDK return/exception
- `status`
- `message`
- `errorcode`
- `data`
- field names/types

### Test Group B — Correct login and account

1. login successfully to REAL and LOCAL
2. get profile/account details
3. get RMS/balance
4. set LOCAL balance equal to the REAL usable balance
5. call RMS again and compare structure/value semantics

### Test Group C — NIFTYBEES discovery and LTP

For NSE:

```text
searchScrip("NSE", "NIFTYBEES")
```

Require exact `NIFTYBEES-EQ`.

Store discovered symbol/token.

For BSE:

```text
searchScrip("BSE", "NIFTYBEES")
```

Do not assume symbol/token equals NSE.

Then compare LTP responses for valid NSE and BSE instruments.

If REAL has no valid BSE result, record the REAL behavior and make LOCAL compatible with that behavior.

### Test Group D — NIFTYBEES BUY tests

After every order action fetch:

- order book
- trade book
- positions
- holdings
- RMS/funds

Cases:

1. LOCAL MARKET BUY 200 qty
   - verify OPEN/PENDING first
   - verify fill after configured 1-second delay
   - REAL 200 qty disabled by default

2. LIMIT BUY 200 qty about 3% below current LTP
   - LOCAL always
   - REAL only when explicitly enabled
   - should remain OPEN while price is above the limit

3. MARKET BUY 1 qty on REAL and LOCAL

4. LIMIT BUY 1 qty about 3% below current LTP on REAL and LOCAL

Use valid tick-size rounding.

### Test Group E — NIFTYBEES SELL tests

Repeat equivalent safe SELL cases.

Prefer selling the quantity created by the controlled BUY test.

Cases:

1. MARKET SELL 1 qty
2. LIMIT SELL 1 qty about 3% above current LTP
3. optional LOCAL 200 qty SELL
4. REAL 200 qty SELL only when explicitly enabled and valid

### Test Group F — Cancel open orders

Collect only test-created OPEN LIMIT orders.

Cancel them on REAL and LOCAL.

Then fetch:

- order book
- trade book
- positions
- holdings
- RMS

Verify canceled orders remain canceled.

### Test Group G — Invalid NIFTYBEES orders

Test REAL and LOCAL where safe:

1. wrong trading symbol BUY
2. wrong trading symbol SELL
3. intentionally invalid MCX exchange combination
4. intentionally invalid BSE symbol/token combination
5. quantity = 0
6. quantity < 0
7. invalid symbol token
8. mismatched symbol/token
9. invalid order type
10. invalid product type

After each case fetch order/position/holding state where useful.

Record exact REAL error behavior and compare LOCAL.

### Test Group H — Historical NIFTYBEES data

Using discovered NSE token, call:

```text
ONE_MINUTE
FIFTEEN_MINUTE
ONE_DAY
```

Use identical fixed date ranges within SmartAPI limits.

Compare candle row structure:

```text
timestamp
open
high
low
close
volume
```

Compare schema/types, not exact live OHLCV values unless intentionally mirrored.

### Test Group I — MCX GOLDPETAL30SEP26FUT

First discover:

```text
searchScrip("MCX", "GOLDPETAL30SEP26FUT")
```

Require an exact tradingsymbol match.

Never hardcode or guess token.

If REAL does not return the exact contract:

```text
SKIPPED_NOT_AVAILABLE
```

and do not place MCX orders.

If available:

1. get MCX LTP on REAL and LOCAL
2. MARKET BUY 1 qty on REAL and LOCAL when `ENABLE_REAL_MCX_ORDERS=true`
3. wait for final status
4. fetch and compare:
   - order book
   - trade book
   - positions
   - holdings
   - RMS/funds
   - individual order details where supported
   - charge estimate where supported
5. verify post-BUY position vs holding behavior
6. MARKET SELL 1 qty to close/reduce the controlled position
7. again compare order/trade/position/holding/RMS/P&L/charges
8. confirm no test-created MCX open orders remain

Do not assume MCX futures appears in holdings. Match REAL SmartAPI behavior.

### Test Group J — Auth/session edge cases

Compare:

- invalid bearer token
- expired token
- missing Authorization where possible
- wrong API key on authenticated request
- logout
- request after logout

### Test Group K — Local-only regression

Keep local tests for:

- MARKET 1000 ms fill delay
- HIJACK LTP
- HIJACK volume
- HIJACK OHLCV
- configurable rate limits
- configurable brokerage/taxes
- insufficient funds
- temporary REST outage
- restart persistence
- admin-setting persistence

These tests do not require REAL parity.

### Reports

Generate:

```text
reports/smartapi_parity_YYYYMMDD_HHMMSS.json
reports/smartapi_parity_YYYYMMDD_HHMMSS.md
```

Each test case should contain:

- case ID
- description
- REAL request summary with secrets redacted
- LOCAL request summary with secrets redacted
- REAL SDK result
- LOCAL SDK result
- normalized REAL result
- normalized LOCAL result
- schema match PASS/FAIL
- type match PASS/FAIL
- behavior match PASS/FAIL
- differences
- notes
- whether a real order was placed
- cleanup result

### Cleanup

Always:

- track REAL/LOCAL test-created order IDs
- cancel only test-created OPEN orders
- close only controlled test-created positions when configured and safe
- never touch unrelated account activity
- logout sessions
- write reports even if cleanup partially fails

### Acceptance

Phase 14 is complete when:

- two official SDK clients are created
- REAL and LOCAL authentication failure matrix is compared
- successful login/profile/RMS comparison works
- LOCAL balance can be aligned to REAL balance
- NIFTYBEES NSE/BSE search and LTP comparison works
- safe MARKET/LIMIT order parity works
- invalid-order cases are captured
- 1m/15m/1D candle structures are compared
- `GOLDPETAL30SEP26FUT` is dynamically discovered or safely skipped
- controlled MCX 1-qty BUY/SELL flow is implemented behind explicit enable flag
- order/trade/position/holding/RMS comparisons are reported
- cleanup handles all test-created open orders
- JSON and Markdown parity reports are generated
- all discovered LOCAL incompatibilities are clearly listed

### Codex prompt

> Read `README.md` and `PLAN.md` completely. Implement Phase 14 only: REAL Angel One SmartAPI vs LOCAL SmartAPI parity testing. Use the official `smartapi-python` SDK for both clients. Read REAL credentials only from `D:\smartapi\angelone_keys.env`; never print or store secrets in reports. Create one REAL client and one LOCAL client using `root=http://127.0.0.1:8000`. Implement the full Phase 14 test matrix from PLAN.md: login failure matrix, successful login/profile/RMS, align local balance to real, NIFTYBEES NSE/BSE search and LTP, safe MARKET/LIMIT BUY/SELL tests, order/trade/position/holding/RMS checks, cancel open test orders, invalid-symbol/exchange/qty/token/order/product cases, 1m/15m/1D historical candles, auth/session edge cases, and MCX `GOLDPETAL30SEP26FUT` exact-symbol discovery plus controlled MARKET BUY 1 qty and MARKET SELL 1 qty when explicitly enabled. Never hardcode symbol tokens. Track only test-created orders and clean them up in a failure-safe final block. Normalize only dynamic fields such as tokens/order IDs/timestamps/live prices. Generate JSON and Markdown parity reports showing schema/type/behavior PASS/FAIL and differences. Keep code simple. Do not modify unrelated server functionality unless a small compatibility fix is directly required by a failing parity case. Mark Phase 14 DONE only after acceptance checks pass.

## Completed follow-up — LTP control panel

**Status:** `DONE`
**Completed:** `2026-09-09`
**Commit/PR:** `Add HIJACK-gated LTP control panel`

Added `ltp_control_panel.py`, a small Tkinter tool for selected stocks. LTP editing is locked until HIJACK is enabled; the tool supports direct updates plus value- or percentage-based increases and decreases, and can return a symbol to YAHOO mode.

---

# 7. Optional Later Phase — WebSocket compatibility

Do not implement during REST v1 unless AutoTick requires it.

Possible later scope:

- SmartWebSocketV2-compatible connection
- subscription messages
- token/symbol subscriptions
- tick streaming
- HIJACK tick streaming
- Yahoo polling-to-tick conversion
- order update WebSocket
- WebSocket disconnect simulation

This should remain separate because SmartAPI REST and SmartAPI WebSocket protocols are different.

---

# 8. Recommended Execution Order

```text
Phase 1   Foundation/routes
Phase 2   Authentication
Phase 3   Admin users/funds
Phase 4   Yahoo market data
Phase 5   HIJACK data
Phase 6   Account/portfolio persistence
Phase 7   Order execution
Phase 8   Brokerage/taxes
Phase 9   Rate limits
Phase 10  Fault simulation
Phase 11  Remaining SDK routes
Phase 12  Admin monitoring/reset
Phase 13  Full compatibility testing
Phase 14  Real-vs-local parity testing
```

Do not attempt all features in one Codex session.

Complete and test one phase before starting the next.

---

# 9. Important Implementation Rules for Codex

For every phase:

1. Read `README.md` and `PLAN.md` first.
2. Inspect current code before making changes.
3. Keep code short and simple.
4. Do not overengineer.
5. Avoid abstractions unless currently needed.
6. Preserve SmartAPI field names exactly where compatibility matters.
7. Add/update tests for the phase.
8. Run tests before completing the phase.
9. At phase start, change both the phase header status and tracking-table status to `IN PROGRESS`.
10. After all acceptance checks and tests pass, change both statuses to `DONE`.
11. When marking `DONE`, record completion date and commit/PR in both places.
12. If blocked, mark `BLOCKED` and add one short reason.
13. Do not implement future phases early unless required for current phase.
14. Never mark a phase `DONE` only because code was written; tests and acceptance checks must pass first.

---

# 9. Definition of Done for REST v1

REST v1 is complete when:

- official SmartAPI Python SDK points to local server using `root`
- all SDK REST routes exist
- authentication uses configurable dummy users
- fixed TOTP works
- invalid credentials fail
- Yahoo provides default market data
- HIJACK overrides LTP/volume/OHLCV
- MARKET fills after configurable 1-second default delay
- LIMIT waits for crossing condition
- funds/orders/trades/positions/holdings persist
- brokerage/taxes are configurable
- rate limits are configurable
- temporary API outages can be simulated
- Admin UI controls simulator state
- Admin UI and REST share one server/port
- server works without opening Admin UI
- automated compatibility tests pass
