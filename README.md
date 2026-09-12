# Local SmartAPI server

Local FastAPI simulator for Angel One SmartAPI REST. The official `smartapi-python` SDK should work by changing only `root`.

```text
SmartConnect(api_key="DUMMY_API_KEY", root="http://127.0.0.1:8000")
```

## Run

```text
pip install -r requirements.txt
uvicorn app:app --reload
```

Admin UI: `http://127.0.0.1:8000/admin`

## Main features

- SmartAPI-compatible REST routes and response shapes
- dummy users, fixed TOTP, JWT/refresh/feed tokens
- Yahoo Finance market data plus per-symbol HIJACK LTP/volume/OHLCV
- MARKET orders with configurable 1000 ms default fill delay
- LIMIT orders stay open until price reaches the limit
- persistent funds, orders, trades, positions and holdings in SQLite
- configurable brokerage/taxes, rate limits and fault simulation
- Jinja admin UI and LTP control panel
- real-vs-local parity runner for Phase 14

## Default local user

| Field | Value |
|---|---|
| Client code | `DUMMY001` |
| Password | `password` |
| API key | `DUMMY_API_KEY` |
| Fixed TOTP | `123456` |

## Useful environment variables

- `SMARTAPI_ACCESS_TOKEN_TTL_SECONDS`
- `SMARTAPI_REFRESH_TOKEN_TTL_SECONDS`
- `SMARTAPI_FORCE_TOKEN_EXPIRY=1`
- `SMARTAPI_MARKET_FILL_DELAY_MS` (default `1000`)
- `SMARTAPI_ORDER_CHECK_INTERVAL_MS` (default `100`)
- `SMARTAPI_DISABLE_TOTP=1` for local-only testing

## Controls

Use `/admin` for users, funds, market overrides, candles, charges, rate limits, faults, monitoring and resets.

For quick LTP control:

```text
python ltp_control_panel.py
```

See `ROUTES.md` for SDK routes and `SMARTAPI_LOCAL_SERVER_PLAN.md` for status and Phase 14 tests.
