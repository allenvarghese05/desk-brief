SYNTHETIC FIXTURE. These files imitate the documented Alpaca Market Data v2 responses
(`/v2/stocks/trades/latest`, `/v2/stocks/quotes/latest`, `/v2/stocks/bars`, and the JSON error
body). Every value is invented; nothing here was fetched from Alpaca. The top-level `_synthetic`
key is not part of the real payload (the parser ignores unknown keys). Replace or complement
them with a live capture after the owner runs the probe (see `docs/sources/alpaca.md`).

| File | Imitates |
|---|---|
| `trades_latest.json` | latest trades, 3 symbols, nanosecond timestamps |
| `quotes_latest.json` | latest quotes; one symbol has `ap: 0` (no ask) |
| `bars_iex_page1.json`, `bars_iex_page2.json`, `bars_iex_page3.json` | one paginated `bars` query (`next_page_token`) |
| `bars_sip.json` | single-page SIP bars |
| `bars_empty.json` | no bars (holiday or no data) |
| `error_unauthorized.json` | invalid key / secret |
| `error_sip_recent.json` | SIP query ending inside the 15-minute window |
