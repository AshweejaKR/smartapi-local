# Local SmartAPI server

Local FastAPI simulator for Angel One SmartAPI REST. The official `smartapi-python` SDK should work by changing only `root`.

```text
SmartConnect(api_key="DUMMY_API_KEY", root="http://127.0.0.1:8000")
```

## Run

```text
pip install -r requirements.txt
uvicorn app:app --reload

# development/tests
pip install -r requirements-dev.txt
pytest
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

Server:
- `SMARTAPI_HOST` (default `127.0.0.1`)
- `SMARTAPI_PORT` (default `8000`)
- `SMARTAPI_PUBLIC_HOST` for the displayed public/Elastic IP
- `SMARTAPI_STARTUP_BANNER=0` to hide the startup banner
- `SMARTAPI_ROOT` for the LTP control panel

Simulator:
- `SMARTAPI_ACCESS_TOKEN_TTL_SECONDS`, `SMARTAPI_REFRESH_TOKEN_TTL_SECONDS`
- `SMARTAPI_FORCE_TOKEN_EXPIRY=1`, `SMARTAPI_DISABLE_TOTP=1`
- `SMARTAPI_MARKET_FILL_DELAY_MS=1000`, `SMARTAPI_ORDER_CHECK_INTERVAL_MS=100`
- `SMARTAPI_MARKET_CACHE_TTL_SECONDS=5`
- `SMARTAPI_SHORT_MARGIN_PERCENT=20`, `SMARTAPI_SLOW_DELAY_MS=250`

Parity:
- `REAL_ENV_FILE`, `LOCAL_ROOT`, `LOCAL_DB`, `PARITY_REPORT_DIR`
- `PARITY_API_DELAY`, `PARITY_POLL_TIMEOUT`, `PARITY_POLL_INTERVAL`
- `ENABLE_REAL_ORDERS`, `ENABLE_REAL_200_QTY`, `ENABLE_REAL_MCX_ORDERS`
- `CLEANUP_ENABLED`, `CLOSE_CONTROLLED_POSITIONS`

Rate limiting is a local test feature. Before authentication, requests may be bucketed by the client code supplied in the request body; do not treat that value as authenticated identity.

## Controls

Use `/admin` for users, funds, market overrides, candles, charges, rate limits, faults, monitoring and resets.

For quick LTP control:

```text
python ltp_control_panel.py
```

See `ROUTES.md` for SDK routes and `SMARTAPI_LOCAL_SERVER_PLAN.md` for status and Phase 14 tests.
