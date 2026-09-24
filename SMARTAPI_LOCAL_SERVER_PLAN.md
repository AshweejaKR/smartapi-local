# SmartAPI Local Server — Plan

## Goal

Local SmartAPI REST server. The official `smartapi-python` SDK works when only `root` changes.

## Scope

- FastAPI server, SQLite state and Admin UI.
- Local Yahoo or dummy market data.
- Local simulated order/account flow.
- Angel One partial forwarding or transparent full proxy.
- REST routes listed in `ROUTES.md`. WebSocket can come later.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1–12 | Server, auth, market, orders, portfolio, admin, limits and faults | DONE |
| 13 | SDK regression, Yahoo check and restart persistence | DONE |
| 14 | Real-versus-local parity | IN PROGRESS |

## Source selection

`default.yaml` selects market, order and account sources independently.

- All three `angelone`: transparent proxy. Client request and broker response pass through unchanged.
- Selected `angelone` source only: server uses `angelone_keys.env` and forwards that route.
- `None` market source: LTP `100.05` and 25 sample candles.
- Real broker orders, positions, holdings and funds are never stored in local SQLite.

## Phase 14

Compare real and local SDK results for auth, profile, RMS, market data, orders, portfolio, history and invalid requests. Ignore only dynamic values: tokens, IDs, timestamps and live prices.

Keep real credentials outside Git. Real order tests stay disabled unless explicitly enabled. Generate redacted JSON and Markdown reports. Clean up only parity-run orders and positions.

Phase 14 is DONE after a successful real run, verified cleanup, generated reports and recorded remaining differences.

## Rules

Keep changes small. Preserve SmartAPI responses. Add or update tests with behavior changes. Never log secrets or tokens.
