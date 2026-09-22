# SmartAPI Local Server

FastAPI server compatible with Angel One SmartAPI REST. Use it with the official `smartapi-python` SDK by changing only `root`.

It supports three data choices:

- **Angel One**: forward selected requests to the real broker.
- **Yahoo**: use Yahoo Finance for local market data.
- **Dummy**: fixed offline LTP and candles.

## Quick start

```text
git clone https://github.com/AshweejaKR/smartapi-local.git
cd smartapi-local
python -m venv .venv
```

Windows:

```text
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Linux / AWS EC2:

```text
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

Open:

| Page | Address |
|---|---|
| Health | `http://127.0.0.1:8000/health` |
| Admin | `http://127.0.0.1:8000/admin` |
| Market controls | `http://127.0.0.1:8000/admin/market` |

For an EC2 public address, allow TCP port `8000` in the security group, then open `http://<public-ip>:8000/admin`.

## SDK client

```python
from SmartApi import SmartConnect

api = SmartConnect(api_key="DUMMY_API_KEY", root="http://127.0.0.1:8000")
session = api.generateSession("DUMMY001", "password", "123456")
print(api.ltpData("NSE", "SBIN-EQ", "3045"))
```

Default local login:

| Field | Value |
|---|---|
| Client code | `DUMMY001` |
| Password | `password` |
| API key | `DUMMY_API_KEY` |
| Fixed TOTP | `123456` |

## Source configuration

The server reads `default.yaml` at startup. Set `SMARTAPI_CONFIG_FILE` to use another YAML file.

```yaml
market_data_source: yahoo # angelone/yahoo/None
order_data: None          # angelone/None
account_data: None        # angelone/None
credentials_file: "angelone_keys.env"
```

The default is safe: Yahoo market data with local orders and local funds.

| Setting | `angelone` | `yahoo` | `None` |
|---|---|---|---|
| `market_data_source` | Real Angel LTP, quote, candles and OI | Yahoo market data; Admin HIJACK still works | LTP always `100.05`; 25 sample candles |
| `order_data` | Real order placement, order/trade books, positions, holdings and conversion | Not allowed | Local SQLite order simulator |
| `account_data` | Real Angel RMS/funds and batch margin | Not allowed | Local SQLite RMS/funds and batch margin |

`None`, `null`, empty value and `dummy` all select dummy market data. Restart the server after changing this file.

### Use a separate config file

Windows:

```text
set SMARTAPI_CONFIG_FILE=C:\smartapi-local\real.yaml
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Linux:

```text
export SMARTAPI_CONFIG_FILE=/opt/smartapi-local/real.yaml
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

## Real Angel One mode

Set only the required sources to `angelone`. Example: real broker data with local simulated orders:

```yaml
market_data_source: angelone
order_data: None
account_data: None
credentials_file: "angelone_keys.env"
```

Example: full transparent Angel One proxy:

```yaml
market_data_source: angelone
order_data: angelone
account_data: angelone
credentials_file: "angelone_keys.env"
```

Create `angelone_keys.env` beside the selected YAML file. This file is ignored by Git.

```text
ANGELONE_API_KEY=your_api_key
ANGELONE_CLIENT_CODE=your_client_code
ANGELONE_PASSWORD=your_password_or_mpin
ANGELONE_TOTP_SECRET=your_totp_secret
```

Short names also work: `API_KEY`, `CLIENT_CODE`, `PASSWORD`, `TOTP_SECRET`.

When all three source values are `angelone`, the server is a transparent Angel One proxy. Give the SDK the real API key, client code, password and TOTP; the server forwards the exact request body, query and authentication headers to Angel One. Its HTTP status and JSON body, including login and broker errors, return unchanged. The credentials file is not used in this full-proxy mode.

When only some sources are `angelone`, the server uses the credentials file for those selected routes. If Angel One cannot be reached and no broker response exists, the server returns local HTTP `503`.

> `order_data: angelone` places real orders. Use only after checking quantity, symbol and funds.

## Local market controls

With `market_data_source: yahoo`, `/admin/market` can set per-symbol `HIJACK` LTP, OHLCV and candles. Local MARKET/LIMIT execution uses the same effective price.

For quick LTP control:

```text
python ltp_control_panel.py
```

Local data stored in `smartapi_local.db`:

- users and funds
- orders, trades, positions and holdings
- market mappings, overrides and candles
- charges, rate limits, faults and audit records

Use `/admin` to manage this data. Real Angel One orders, positions, holdings and funds are never copied into local SQLite.

## Run options

| Option | Use |
|---|---|
| `SMARTAPI_HOST` | Bind host; default `127.0.0.1` |
| `SMARTAPI_PORT` | Server port; default `8000` |
| `SMARTAPI_PUBLIC_HOST` | Public/Elastic IP shown in startup output |
| `SMARTAPI_CONFIG_FILE` | YAML configuration path |
| `SMARTAPI_STARTUP_BANNER=0` | Hide startup addresses |
| `SMARTAPI_MARKET_FILL_DELAY_MS` | Local MARKET fill delay; default `1000` |
| `SMARTAPI_ORDER_CHECK_INTERVAL_MS` | Local fill checker interval; default `100` |
| `SMARTAPI_MARKET_CACHE_TTL_SECONDS` | Yahoo cache duration; default `5` |
| `SMARTAPI_DISABLE_TOTP=1` | Disable TOTP only for local testing |
| `SMARTAPI_FORCE_TOKEN_EXPIRY=1` | Force local token-expiry tests |
| `SMARTAPI_SHORT_MARGIN_PERCENT` | Local short margin; default `20` |
| `SMARTAPI_SLOW_DELAY_MS` | Admin fault-mode delay; default `250` |

## Tests

```text
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Expected current result: `159 passed, 1 skipped`. The skipped Yahoo smoke test runs only when `SMARTAPI_LIVE_YAHOO=1`.

## More information

- `ROUTES.md`: SDK route inventory.
- `SMARTAPI_LOCAL_SERVER_PLAN.md`: project status and real-vs-local parity plan.
