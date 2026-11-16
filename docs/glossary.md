# Glossary

- **Tier**: trust level of a number. Tier 1 = fast but approximate (often a proxy). Tier 2 = the exact quoted price, delayed ~15 minutes. Tier 3 = official end-of-day value from a primary source (Treasury, ECB, FRED).
- **Proxy**: real data about something related, used as a stand-in (e.g. USO for oil, TLT for long-term yields). Approximate, always labeled.
- **ETF**: a basket of investments trading on an exchange with a live price (SPY, QQQ, GLD, USO, TLT).
- **Futures price**: today's agreed price for delivery on a future date; the "oil price" in the news is usually the front-month contract (CL=F).
- **IEX vs SIP**: IEX is one US exchange; SIP is the consolidated feed from all exchanges. Alpaca's free plan gives real-time IEX and 15-minute-delayed SIP.
- **Basis point (bp)**: 0.01 percentage point.
- **Yield curve**: the interest rate the US government pays at each maturity. 2s10s = 10-year minus 2-year yield. Bond prices move opposite to yields.
- **OHLCV**: open, high, low, close, volume. Return = today's close / yesterday's close − 1.
- **Z-score**: how many standard deviations today's move is from the recent (30-day) average move.
- **FX quote**: EUR/USD 1.08 means one euro costs 1.08 dollars.
- **As-of vs ingested-at**: when the market says a value applies vs when we fetched it. Keeping both makes the data point-in-time.
