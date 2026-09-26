# SmartAPI SDK route inventory

Source: current `angel-one/smartapi-python` `SmartConnect._routes`, inspected for Phase 1.
Methods come from the corresponding public SDK method. `api.token` and `api.refresh` share one POST route, as do the two place-order helpers. The individual-order-details SDK method appends the order ID, so the local route uses `{order_id}`.

| Method | Path | SDK key(s) |
|---|---|---|
| POST | `/rest/auth/angelbroking/user/v1/loginByPassword` | api.login |
| POST | `/rest/secure/angelbroking/user/v1/logout` | api.logout |
| POST | `/rest/auth/angelbroking/jwt/v1/generateTokens` | api.token, api.refresh |
| GET | `/rest/secure/angelbroking/user/v1/getProfile` | api.user.profile |
| POST | `/rest/secure/angelbroking/order/v1/placeOrder` | api.order.place, api.order.placefullresponse |
| POST | `/rest/secure/angelbroking/order/v1/modifyOrder` | api.order.modify |
| POST | `/rest/secure/angelbroking/order/v1/cancelOrder` | api.order.cancel |
| GET | `/rest/secure/angelbroking/order/v1/getOrderBook` | api.order.book |
| POST | `/rest/secure/angelbroking/order/v1/getLtpData` | api.ltp.data |
| GET | `/rest/secure/angelbroking/order/v1/getTradeBook` | api.trade.book |
| GET | `/rest/secure/angelbroking/user/v1/getRMS` | api.rms.limit |
| GET | `/rest/secure/angelbroking/portfolio/v1/getHolding` | api.holding |
| GET | `/rest/secure/angelbroking/order/v1/getPosition` | api.position |
| POST | `/rest/secure/angelbroking/order/v1/convertPosition` | api.convert.position |
| POST | `/gtt-service/rest/secure/angelbroking/gtt/v1/createRule` | api.gtt.create |
| POST | `/gtt-service/rest/secure/angelbroking/gtt/v1/modifyRule` | api.gtt.modify |
| POST | `/gtt-service/rest/secure/angelbroking/gtt/v1/cancelRule` | api.gtt.cancel |
| POST | `/rest/secure/angelbroking/gtt/v1/ruleDetails` | api.gtt.details |
| POST | `/rest/secure/angelbroking/gtt/v1/ruleList` | api.gtt.list |
| POST | `/rest/secure/angelbroking/historical/v1/getCandleData` | api.candle.data |
| POST | `/rest/secure/angelbroking/historical/v1/getOIData` | api.oi.data |
| POST | `/rest/secure/angelbroking/market/v1/quote` | api.market.data |
| POST | `/rest/secure/angelbroking/order/v1/searchScrip` | api.search.scrip |
| GET | `/rest/secure/angelbroking/portfolio/v1/getAllHolding` | api.allholding |
| GET | `/rest/secure/angelbroking/order/v1/details/{order_id}` | api.individual.order.details |
| POST | `/rest/secure/angelbroking/margin/v1/batch` | api.margin.api |
| POST | `/rest/secure/angelbroking/brokerage/v1/estimateCharges` | api.estimateCharges |
| POST | `/rest/secure/angelbroking/edis/v1/verifyDis` | api.verifyDis |
| POST | `/rest/secure/angelbroking/edis/v1/generateTPIN` | api.generateTPIN |
| POST | `/rest/secure/angelbroking/edis/v1/getTranStatus` | api.getTranStatus |
| POST | `/rest/secure/angelbroking/marketData/v1/optionGreek` | api.optionGreek |
| POST | `/rest/secure/angelbroking/marketData/v1/gainersLosers` | api.gainersLosers |
| GET | `/rest/secure/angelbroking/marketData/v1/putCallRatio` | api.putCallRatio |
| POST | `/rest/secure/angelbroking/marketData/v1/OIBuildup` | api.oIBuildup |
| GET | `/rest/secure/angelbroking/marketData/v1/nseIntraday` | api.nseIntraday |
| GET | `/rest/secure/angelbroking/marketData/v1/bseIntraday` | api.bseIntraday |


## Local market controls

Local test controls for HIJACK prices and candles. They change only this server's SQLite state, never Angel One, Yahoo Finance, the instrument master or broker orders. All endpoints need a local session: `Authorization: Bearer <jwtToken>` from `loginByPassword` (dummy or real login mode). Admin → Market shows a read-only status; interactive docs are at `/docs`.

| Method | Path | Body / query | Action |
|---|---|---|---|
| GET | `/local/v1/market/state` | `?exchange=&symboltoken=` | Read control state |
| POST | `/local/v1/market/hijack` | `exchange, symboltoken, ltp`, optional `open, high, low, close, volume` | Enable HIJACK (replaces all values) |
| POST | `/local/v1/market/ltp/set` | `exchange, symboltoken, ltp` | Set LTP |
| POST | `/local/v1/market/ltp/step` | `exchange, symboltoken, step` | LTP += step (signed) |
| POST | `/local/v1/market/ltp/percent` | `exchange, symboltoken, percent` | LTP *= 1 + percent/100 (signed) |
| POST | `/local/v1/market/clear` | `exchange, symboltoken` | Clear HIJACK; configured provider again |
| POST | `/local/v1/market/candles` | `exchange, symboltoken, timestamp, open, high, low, close, volume` | Create/replace one candle |
| GET | `/local/v1/market/candles` | `?exchange=&symboltoken=&fromdate=&todate=` (dates optional, ISO-8601) | List saved candles |
| POST | `/local/v1/market/candles/delete` | `exchange, symboltoken, timestamp` | Delete one candle |
| POST | `/local/v1/market/candles/clear` | `exchange, symboltoken` | Delete all candles of the instrument |

Every success returns the state:

```json
{"status": true, "message": "SUCCESS", "errorcode": "", "data": {
  "exchange": "NSE", "symboltoken": "3045", "tradingsymbol": "SBIN-EQ",
  "hijack": true, "source": "HIJACK", "provider": "yahoo", "configured_provider": "yahoo",
  "override": {"ltp": 101.5, "open": null, "high": null, "low": null, "close": null, "volume": null},
  "quote": {"ltp": 101.5, "open": 101.5, "high": 101.5, "low": 101.5, "close": 101.5, "volume": 0},
  "overridden_candles": 1}}
```

`POST candles` adds `"candle": ["2026-09-08T10:00:00+05:30", 100.0, 102.0, 99.0, 101.0, 500]`; `GET candles` adds `"candles": [...]` in `getCandleData` row format; `candles/clear` adds `"deleted": 1`.

Behavior:

- `exchange` and `symboltoken` are required. The trading symbol comes from the current instrument master. Yahoo mode accepts only verified Yahoo symbols and Angel One mode only master instruments (any token when no master is cached); others return `Failed to get symbol details` (`AB1018`). Dummy mode accepts any exchange/token.
- `ltp`, `open`, `high`, `low`, `close` must be finite numbers above zero; `step` and `percent` may be negative, but a resulting LTP of zero or less is rejected. `volume` is a non-negative whole number. `ltp/set`, `ltp/step` and `ltp/percent` need HIJACK enabled and keep explicitly set OHLC/volume; unset OHLC values equal the LTP.
- Candles: `timestamp` is ISO-8601 and is stored at minute precision in Asia/Kolkata (`2026-09-08T04:31:45Z` → `2026-09-08T10:01`); `low <= open, close <= high`.
- While HIJACK is on, local `getLtpData`, `quote` and `getCandleData` use it for that instrument in every market source, including Angel One: hijacked tokens are answered locally and never sent to the broker. `getCandleData` returns saved candles in the requested range; if none are in the range, one candle is synthesized from the HIJACK quote at `fromdate`. Provider candles are never merged in. After `clear`, saved candles remain (and can be listed or deleted) but do not affect responses.
- Every change clears cached provider data for the instrument, is persisted, and is audited (instrument and values only).
- Errors use the SmartAPI envelope: `403 AG8001` without a valid session, `400 AB1004` for invalid input or HIJACK not enabled, `400 AB1018` for unknown symbols, `404 AB1004` for a missing candle.
