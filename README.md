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

`default.yaml` is used unless `SMARTAPI_CONFIG_FILE` names another YAML file. Edit it, or use **Admin → Settings**, which validates the values and saves them atomically to the active file. Restart the server to apply saved settings.

| Setting | Values | Default |
|---|---|---|
| market_data_source | `angelone` broker quotes · `yahoo` verified Yahoo symbols · `null` LTP 100.05, 25 candles | `angelone` |
| order_data | `angelone` real broker orders · `null` local SQLite orders | `null` |
| account_data | `angelone` broker RMS/margin · `null` local SQLite funds | `null` |
| client_auth | `dummy` local users · `real` Angel One credentials | `dummy` |
| credentials_file | Angel One keys used by `dummy` mode (relative to the YAML file) | `angelone_keys.env` |

Example settings, both with `client_auth: dummy`:

| | market_data_source | order_data | account_data |
|---|---|---|---|
| A | angelone | angelone | angelone |
| B | angelone | null | angelone |

The shipped default uses Angel One for market data and keeps orders and account data local. If the internal Angel One login fails, only market data falls back to Yahoo for that process.

### Client login

- `dummy`: clients log in with a local user. Default: client code `DUMMY001`, password `password`, API key `DUMMY_API_KEY`, TOTP `123456`. Sources set to `angelone` use one server-side broker session from `credentials_file` (`API_KEY`, `CLIENT_CODE`/`CLIENT_ID`, `PASSWORD`, `TOTP_SECRET`).
- `real`: clients log in with their own Angel One API key, client code, password and TOTP. The server validates them with Angel One and returns local tokens. Each local session keeps its own broker session in memory, used for the `angelone` routes; `order_data: null` still keeps orders local. A broker token that expires is refreshed once; if that fails, the local session ends and the client logs in again. Real sessions end when the server restarts.

Broker API keys, passwords, TOTP seeds and tokens are never stored in SQLite and never appear in local tokens, admin pages, logs, audit rows or errors.

### Fallback

In `dummy` mode the server logs in to Angel One once at startup. If that fails, market data switches to Yahoo for this process only: the saved setting is unchanged, Admin → Settings shows the reason, and an audit row is recorded. Order and account routes set to `angelone` return `Angel One request is unavailable` (`AB2001`, HTTP 503); they never fall back to local orders. The failed login is not retried until restart.

## Instruments and Yahoo symbols

The Angel One instrument master is downloaded with GET once per calendar day (IST) at startup and cached in `smartapi_local.db` (not in Git). If the download fails, the last valid cache is kept. Admin → Instruments shows the last successful refresh, source URL, row count and last failure, has a lookup, and **Refresh now** downloads it on demand.

Requests identify instruments by exchange and token; display symbols come from the current master. Yahoo market data covers only symbols in `yahoo_catalog.csv`, the VERIFIED rows of the Yahoo mapping audit, keyed by exchange and Angel trading symbol. After each refresh the current token is looked up again for every catalog symbol. Other instruments (derivatives, commodities, currencies, debt, funds, unreviewed series) return `Failed to get symbol details` (`AB1018`) with Yahoo. The `angelone` source works for every broker instrument. With no cached master, Yahoo lookups return `AB1018`.

Rebuild the catalog after a new audit run:

```text
python scripts/audit_yahoo_mapping.py --master reports/yahoo_audit/OpenAPIScripMaster.json --full
python scripts/build_yahoo_catalog.py
```

The audit compares Yahoo and Angel One prices only when both are from the same market date; other rows are labelled `STALE` or `NOT_COMPARABLE`. It logs progress every 100 checks.

## Market controls (HIJACK)

REST-only local test controls under `/local/v1/market` set an instrument's LTP and candles. While HIJACK is on, local `getLtpData`, `quote` and `getCandleData` use it in every market source (Angel One, Yahoo, dummy); nothing is sent to the broker or Yahoo for that instrument. Endpoints, bodies and validation are in [ROUTES.md](ROUTES.md#local-market-controls); Admin → Market shows a read-only status.

```bash
R=http://127.0.0.1:8000
T=$(curl -s $R/rest/auth/angelbroking/user/v1/loginByPassword -H 'X-PrivateKey: DUMMY_API_KEY' \
  -H 'Content-Type: application/json' -d '{"clientcode":"DUMMY001","password":"password","totp":"123456"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["data"]["jwtToken"])')
c() { curl -s "$R$1" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' ${2:+-d "$2"}; echo; }
I='"exchange":"NSE","symboltoken":"3045"'
c /local/v1/market/hijack      "{$I,\"ltp\":100}"
c /local/v1/market/ltp/set     "{$I,\"ltp\":101}"
c /local/v1/market/ltp/step    "{$I,\"step\":-0.5}"
c /local/v1/market/ltp/percent "{$I,\"percent\":2}"
c /local/v1/market/candles     "{$I,\"timestamp\":\"2026-09-08T10:00\",\"open\":100,\"high\":102,\"low\":99,\"close\":101,\"volume\":500}"
c /rest/secure/angelbroking/order/v1/getLtpData "{$I,\"tradingsymbol\":\"SBIN-EQ\"}"
c /rest/secure/angelbroking/historical/v1/getCandleData "{$I,\"interval\":\"ONE_MINUTE\",\"fromdate\":\"2026-09-08 09:15\",\"todate\":\"2026-09-08 15:30\"}"
c /local/v1/market/clear       "{$I}"
```

## Test

```text
python -m pip install -r requirements-dev.txt
python -m pytest -m smoke -q   # quick: 55 passed, ~10s
python -m pytest -q            # full offline: 223 passed, 1 skipped, ~100s
```

Tests are offline: the master download, Yahoo and Angel One are mocked. The skipped test needs `SMARTAPI_LIVE_YAHOO=1`. `smartapi_parity.py` calls the real broker; run it only on purpose.
