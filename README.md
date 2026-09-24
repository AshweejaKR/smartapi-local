# SmartAPI Local Server

SmartAPI REST server for local testing. Use the official SDK with its `root` set to this server.

## Run

```text
git clone https://github.com/AshweejaKR/smartapi-local.git
cd smartapi-local
python -m venv .venv
```

Activate: Windows `.venv\\Scripts\\activate`; Linux `source .venv/bin/activate`.

```text
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

On EC2, use `--host 0.0.0.0` and allow TCP 8000. Check `/health` and `/admin`.

## Sources

Restart after changing `default.yaml`.

| Setting | angelone | yahoo | None |
|---|---|---|---|
| market_data_source | Broker data | Yahoo data | LTP 100.05, 25 candles |
| order_data | Real broker orders | — | Local SQLite orders |
| account_data | Broker RMS and margin | — | Local SQLite funds |

Default: Yahoo market data with local orders and funds.

Set `SMARTAPI_CONFIG_FILE` to use another YAML file.

## Angel One

- All three sources as `angelone`: transparent proxy. SDK sends real credentials; broker request and response pass through unchanged.
- Any selected source as `angelone`: partial proxy. Put `API_KEY`, `CLIENT_ID`, `PASSWORD`, and `TOTP_SECRET` in `angelone_keys.env` beside the YAML.
- `order_data: angelone` places real orders.

## Test

```text
python -m pip install -r requirements-dev.txt
python -m pytest -q --ignore=test_smartapi_local.py
```

Expected: 160 passed, 1 skipped.

Use `python test_smartapi_local.py` only for real-versus-local comparison. Set `SMARTAPI_LOCAL_MODE=angelone` for full proxy or `none` for local/partial mode. The script hides secrets.
