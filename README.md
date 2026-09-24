# SmartAPI Local Server

Local SmartAPI REST server. Use the official SDK with this server as `root`.

## Run

```text
git clone https://github.com/AshweejaKR/smartapi-local.git
cd smartapi-local
python -m venv .venv
```

Activate: Windows `.venv\Scripts\activate`; Linux `source .venv/bin/activate`.

```text
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

EC2: use `--host 0.0.0.0`, allow TCP 8000, then open `/health` or `/admin`.

## Config

Restart after editing `default.yaml`.

| Setting | angelone | yahoo | None |
|---|---|---|---|
| market_data_source | Broker | Yahoo | LTP 100.05, 25 candles |
| order_data | Real orders | — | Local SQLite |
| account_data | Broker RMS | — | Local SQLite |

Default: Yahoo market data, local orders and funds. Set `SMARTAPI_CONFIG_FILE` for another YAML file.

All three `angelone` sources enable transparent proxy. A partial Angel One setup uses `angelone_keys.env`; `order_data: angelone` places real orders.

## Test

```text
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Expected: 160 passed, 1 skipped.
