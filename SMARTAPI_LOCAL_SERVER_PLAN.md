# SmartAPI Local Server — Plan

## Goal

Local SmartAPI REST server. Official SDK works when only `root` changes.

## Status

| Phase | Result | Status |
|---|---|---|
| 1–12 | Server, simulation, admin, limits and faults | DONE |
| 13 | SDK regression and restart check | DONE |
| 14 | Real-versus-local parity | IN PROGRESS |
| Mapping 1 | Yahoo mapping audit of the instrument master | DONE |
| Mapping 2 | Instrument refresh, verified Yahoo catalog, settings, dummy/real login | DONE |
| Mapping 3 | REST HIJACK controls: LTP set/step/percent, candle set/list/delete | DONE |
| Admin PIN | Simple browser PIN authentication | PLANNED |

## Sources

The active YAML file (`SMARTAPI_CONFIG_FILE`, else `default.yaml`) selects each source and the client login mode. Admin → Settings edits it atomically; changes apply on restart.

- `market_data_source`: `angelone` (default), `yahoo` or `null` (LTP `100.05`, 25 candles).
- `order_data`, `account_data`: `angelone` or `null` (local SQLite).
- `client_auth`: `dummy` (default) or `real`.
- Real broker state stays outside local SQLite.

The shipped default is market `angelone`, orders `null`, account `null`, and client authentication `dummy`.

Supported with `client_auth: dummy`: A = all `angelone`; B = market and account `angelone`, orders `null`.

## Login modes

- `dummy`: local users (`DUMMY001` / `password` / `DUMMY_API_KEY` / `123456`). `angelone` routes use one server session from `credentials_file`.
- `real`: the client's Angel One credentials are validated with the broker; the client gets local tokens only. One in-memory broker session per local session; expired broker tokens are refreshed once, otherwise the local session ends. Real sessions end on restart.

Secrets (API key, password, TOTP seed, broker tokens) never reach SQLite, local tokens, admin pages, logs, audit rows or errors.

## Fallback

If the internal (`dummy`) broker login fails, effective market data is Yahoo for this process. The saved setting is unchanged, the reason is shown in Admin → Settings and audited, and the login is not retried until restart. Order and account routes selected for `angelone` return the broker-unavailable response; they never become local orders.

## Instruments

- Master: GET `https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json` once per IST calendar day at startup, cached in SQLite; failures keep the last valid cache. Admin → Instruments shows status and offers a manual refresh and lookup.
- Identity is exchange + token; the display symbol comes from the current master.
- Yahoo symbols come only from `yahoo_catalog.csv` (VERIFIED audit rows, keyed by exchange + trading symbol), re-resolved to current tokens after each refresh. Rebuild it with `scripts/build_yahoo_catalog.py`.
- The audit compares prices only for the same market date and labels other rows `STALE`/`NOT_COMPARABLE`.

## Market controls

- REST-only under `/local/v1/market`, authenticated with a local session; documented in `ROUTES.md`. Admin → Market is read-only.
- HIJACK overrides local `getLtpData`, `quote` and `getCandleData` for one exchange/token in every market source, including Angel One (hijacked tokens are never forwarded). Nothing is written to the broker, Yahoo or the instrument master.
- LTP set/step/percent keep explicit OHLC/volume; results must stay above zero. Candles are minute-precision IST rows; with HIJACK on, `getCandleData` returns saved candles in range or one synthesized candle, never provider candles. Clearing HIJACK keeps saved candles but ignores them.
- State persists in SQLite; every change clears that instrument's cache and is audited without secrets.

## Planned Admin PIN

Implement the simple browser PIN authentication in a later branch. The approved design and acceptance criteria are in [SMARTAPI_ADMIN_PIN_PLAN.md](SMARTAPI_ADMIN_PIN_PLAN.md).

## Phase 14

Compare real and local SDK results for auth, market, orders, portfolio, RMS, history and errors. Ignore dynamic tokens, IDs, timestamps and prices only.

Keep credentials outside Git. Real orders stay disabled unless enabled. Produce redacted reports and clean up parity-run orders.

Done after successful real run, verified cleanup, reports and recorded differences.

## Rules

Keep changes small. Preserve SmartAPI response behavior. Never log secrets.
