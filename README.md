# Local SmartAPI server

Phase 8 adds configurable transaction charges and gross/net P&L to persistent MARKET and LIMIT execution.

Run with uvicorn app:app --reload and point the SDK at root="http://127.0.0.1:8000".

Default seeded dummy user:

| Field | Value |
|---|---|
| Client code | DUMMY001 |
| Password | password |
| API key / X-PrivateKey | DUMMY_API_KEY |
| Fixed TOTP | 123456 |

Set SMARTAPI_ACCESS_TOKEN_TTL_SECONDS and SMARTAPI_REFRESH_TOKEN_TTL_SECONDS to configure expiry.
Set SMARTAPI_FORCE_TOKEN_EXPIRY=1 before startup to issue already-expired access tokens.
Set SMARTAPI_MARKET_FILL_DELAY_MS to configure MARKET fill delay (default 1000) and SMARTAPI_ORDER_CHECK_INTERVAL_MS to configure checker frequency (default 100).
Set SMARTAPI_DISABLE_TOTP=1 to accept any TOTP during local testing; TOTP verification remains enabled by default.

Open `/admin/charges` to edit brokerage, STT/CTT, exchange, GST, SEBI, stamp duty and custom charges. Rates start at zero; custom charges start disabled. These are editable simulator defaults, not live broker rates. Flat fees apply once per fill; percentages use rounded fill turnover (1 means 1%). GST can instead use the sum of brokerage, exchange and SEBI charges. Each component rounds half up to two decimals before summing. Settings persist in SQLite and apply to future fills only.

Every new trade stores `gross_trade_value`, each charge component, `total_charges`, `gross_pnl` and `net_pnl`. Trade gross P&L is the realized amount from quantities closed by that fill; opening fills have zero gross P&L and negative net P&L when charged. Account and position gross P&L includes realized plus unrealized P&L, with all accumulated charges subtracted for net P&L. These totals appear in RMS/position responses and `/admin/account`. Charges are deducted from cash atomically with each fill; buys reserve estimated charges and fills recheck funds using current settings. Sell proceeds can cover sell charges. Existing trades receive zero charge/P&L fields without retrospective billing; their turnover is backfilled. No tests were created or run for Phase 8, as requested.

Open `/admin` on the same server to add, edit, enable, disable or delete users and to add/remove funds. Credential and status changes take effect immediately; editing or disabling a user revokes its active sessions.

For quick LTP-only control, start the server and run `python ltp_control_panel.py`. Select a stock, click `Enable HIJACK`, then enter an LTP or use Value/Percentage `Increase` and `Decrease`; the selected symbol is switched to HIJACK mode when an update is saved. Edit `SELECTED_STOCKS` in the script to change the available stock list. Set `SMARTAPI_ROOT` if the server is not at `http://127.0.0.1:8000`.

Default market mappings include SBIN, RELIANCE and NIFTY. Add or update mappings in code with `market.upsert_mapping(exchange, tradingsymbol, symboltoken, yahoo_symbol)`; values are stored in SQLite and do not require opening the Admin UI.
