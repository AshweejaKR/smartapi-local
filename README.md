# SmartAPI Local Server

SmartAPI REST server. Use the official SDK with only the `root` changed.

## Run

```text
git clone https://github.com/AshweejaKR/smartapi-local.git
cd smartapi-local
python -m venv .venv
```

Windows: `.venv\Scripts\activate`  
Linux: `source .venv/bin/activate`

```text
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Use `--host 0.0.0.0` on EC2. Open `/health` or `/admin`. Allow TCP `8000` in the EC2 security group.

## Source config

`default.yaml` is read at server startup. Restart after changes.

| Setting | `angelone` | `yahoo` | `None` |
|---|---|---|---|
| `market_data_source` | Real LTP, quote, candles, OI | Yahoo data | LTP `100.05`, 25 candles |
| `order_data` | Real orders, books, positions, holdings | — | Local SQLite simulator |
| `account_data` | Real RMS and margin | — | Local SQLite funds/margin |

Default safe mode: Yahoo market data and local orders/funds.

Use another config file: set `SMARTAPI_CONFIG_FILE=C:\smartapi-local\real.yaml` on Windows, or `export SMARTAPI_CONFIG_FILE=/opt/smartapi-local/real.yaml` on Linux.

### Full Angel One proxy

Set all three sources to `angelone`. Use the real API key, client code, password and TOTP in the SDK. The server forwards request and broker response unchanged, including broker errors. `angelone_keys.env` is not used in this mode.

### Partial Angel One mode

Set only needed sources to `angelone`. Create `angelone_keys.env` beside the YAML file:

```text
API_KEY=your_api_key
CLIENT_ID=your_client_code
PASSWORD=your_password_or_mpin
TOTP_SECRET=your_totp_secret
```

Also accepted: `ANGELONE_*`, `CLIENT_CODE`, `CLIENTCODE`, `MPIN`, `TOTP`.

`order_data: angelone` places real orders.

## Local SDK and comparison

Local login: API key `DUMMY_API_KEY`; client `DUMMY001`; password `password`; TOTP `123456`.

Run real-versus-local comparison only as a script:

```text
python test_smartapi_local.py
```

For full proxy: `set SMARTAPI_LOCAL_MODE=angelone`.  
For dummy or partial mode: `set SMARTAPI_LOCAL_MODE=none`.

`SMARTAPI_LOCAL_MODE` changes only comparison-script login values. It does not change server configuration. Script output hides passwords, TOTP, API keys and tokens.

## Tests

```text
python -m pip install -r requirements-dev.txt
python -m pytest -q --ignore=test_smartapi_local.py
```

Expected: `160 passed, 1 skipped`. `ROUTES.md` lists supported SDK routes; `SMARTAPI_LOCAL_SERVER_PLAN.md` tracks parity work.
