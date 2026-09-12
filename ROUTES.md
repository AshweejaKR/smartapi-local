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
