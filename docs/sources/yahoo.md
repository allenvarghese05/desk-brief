# Yahoo Finance source (T16)

Code: `src/deskbrief/sources/yahoo.py`. Tier 2 "second opinion" source, unofficial, wraps the
`yfinance` library. Registered with `register("yahoo", YahooSource)`.

## Datasets

| Dataset | Rows | Notes |
|---|---|---|
| `quote` | `field=last`, `interval=tick`, `as_of` = Yahoo `regularMarketTime` (the quote instant, P-006) | One `Ticker.history(period="1d")` call per symbol, then `history_metadata` (`regularMarketPrice`, `regularMarketTime`). |
| `history` | `field` in open/high/low/close, `interval=1d`, `as_of` = 00:00 ET on the bar's exchange-local date | Window `[target_date - 10d, target_date + 1d)`, `auto_adjust=False`. A null field (partial bar) is skipped. |
| `daily_fallback` | `field=close`, `interval=1d`, `is_fallback=true` | T3 fallback for symbols Stooq normally serves. See below. |

Planned per job: morning and eod = quote + history + daily_fallback; intraday = quote only;
backfill/rebuild = history + daily_fallback. A dataset with no configured feeds is not planned.
`tier`, `unit` and `scale` always come from the `config.yaml` feed (`scale` multiplies every
price field; volume is never emitted because it means shares, contracts or zero depending on
the instrument).

## Raw is a serialization, not the provider's bytes (limitation)

yfinance does its own HTTP (curl_cffi, cookies, crumb), so the shared `HttpClient` is not used and
the exact bytes Yahoo sent are not available. `RawResponse.body` is a JSON serialization of what
the library returned, plus `yfinance.__version__` and the fetch time:

```json
{"dataset": "quote", "yfinance_version": "...", "fetched_at": "...",
 "symbols": {"^TNX": {"price": 4.25, "time": "2026-11-13T19:59:54+00:00", "currency": "USD"}}}
```

Consequences: replaying raw reproduces the parse, not Yahoo's response; a yfinance upgrade can
change what lands in raw (the version is recorded for that reason). `url_redacted` is the
pseudo-URL `yfinance://<dataset>`. Per-symbol failures are stored in the body as
`{"error": ...}` and skipped by `parse` (completeness checks see the missing row); if every symbol
fails the response carries `error` and no `http_status`. If no symbol returned data the fetch
status is `empty`.

## Rate limiting

Token bucket from `sources/http.py` (`TokenBucket`), `rate_per_min` from `config.sources.yahoo`
(30), burst 1, one token per symbol call. There is no retry: a failed symbol is reported, and the
next scheduled run tries again.

## Enabling `daily_fallback` (when Stooq is disabled, T13)

Add one feed per equity/ETF symbol to its instrument in `config.yaml` (tier 3, same tier as
Stooq, which then stays `enabled: false`):

```yaml
      - {source: yahoo, dataset: daily_fallback, symbol: SPY, tier: 3}
```

Yahoo symbols are plain tickers (`SPY`, not Stooq's `spy.us`). Rows carry `is_fallback=true`
and must be labeled as fallback wherever shown. `config.yaml` has no such feeds today because
T13 has not disabled Stooq.

## `^TNX` / `^IRX` scale: VERIFY at live probe

Historically Yahoo quoted `^TNX` and `^IRX` as yield x 10 (42.5 meant 4.25%), which needs
`scale: 0.1` on the feed. `config.yaml` currently sets no scale (1.0) and still carries
`# VERIFY scale`. The live probe has not been recorded for this PR (status: PENDING). Check the
live value against the 10-year yield from FRED/Treasury before the first scheduled run and set
`scale` on the `^TNX`/`^IRX` quote and history feeds if the value is ~10x. The validity and anomaly
checks (T20) will also flag a 10x error.

## Informal probe (local machine, 2026-11-15)

Not a verified or recorded probe (see below); yfinance 1.8.0 from a local machine, not GitHub
Actions IPs, about 10 calls, no rate limiting or blocking seen.

- `^TNX` last about 5.154 and `^IRX` about 4.122: Yahoo now returns yields already in percent, so
  scale 1.0 looks right. Pending a cross-check against FRED/Treasury.
- `ES=F` history includes a Sunday-session bar (2026-11-15).
- Index timezones differ per exchange (`^TNX` America/Chicago, `ES=F` America/New_York), so bars
  are dated by their exchange-local date.

## Live probe

PENDING (formal). Record here: date, yfinance version, one quote and one history call, observed
`^TNX`/`^IRX` magnitude, rate-limit or blocking behaviour, whether GitHub Actions IPs are blocked.
Allowed hosts when probing: `query1.finance.yahoo.com`, `query2.finance.yahoo.com`,
`fc.yahoo.com`, `guce.yahoo.com`.

## Terms

Do not commit real Yahoo responses (their terms). Fixtures in `tests/fixtures/synthetic/yahoo/`
are synthetic and labeled. Display terms for hosted mode are tracked as H9.
