# SmartAPI Local Server — Plan

## Goal

Local SmartAPI REST server. Official SDK works when only `root` changes.

## Status

| Phase | Result | Status |
|---|---|---|
| 1–12 | Server, simulation, admin, limits and faults | DONE |
| 13 | SDK regression and restart check | DONE |
| 14 | Real-versus-local parity | IN PROGRESS |

## Sources

`default.yaml` selects each source.

- All `angelone`: transparent broker proxy.
- Selected `angelone`: server uses `angelone_keys.env`.
- `None` market: LTP `100.05` and 25 candles.
- Real broker state stays outside local SQLite.

## Phase 14

Compare real and local SDK results for auth, market, orders, portfolio, RMS, history and errors. Ignore dynamic tokens, IDs, timestamps and prices only.

Keep credentials outside Git. Real orders stay disabled unless enabled. Produce redacted reports and clean up parity-run orders.

Done after successful real run, verified cleanup, reports and recorded differences.

## Rules

Keep changes small. Preserve SmartAPI response behavior. Never log secrets.
