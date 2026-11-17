# Yahoo Finance source (T16)

Code: `src/deskbrief/sources/yahoo.py`. Tier 2 "second opinion" source, unofficial, wraps the
`yfinance` library. Registered with `register("yahoo", YahooSource)`.

## Datasets

| Dataset | Rows | Notes |
|---|---|---|
| `quote` | `field=last`, `interval=tick`, `as_of` = Yahoo `regularMarketTime` (the quote instant, P-006) | One `Ticker.history(period="1d")` call per symbol, then `history_metadata` (`regularMarketPrice`, `regularMarketTime`). |
| `history` | `field` in open/high/low/close, `interval=1d`, `as_of` = 00:00 ET on the bar's exchange-local date | Window `[target_date - 10d, target_date + 1d)`, `auto_adjust=False`. Only final trading-day bars are kept (see below). A null field is skipped. |
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

## Errors are made visible (hide_exceptions)

yfinance 1.8 defaults to `config.debug.hide_exceptions = True`: rate limits and blocks are logged
and come back as empty data. `fetch` sets it to `False` around its calls (restored in `finally`)
so they raise and become per-symbol errors. Because Yahoo can still return nothing for a bad
symbol, "every requested symbol returned no data" is an `error` when at least one requested symbol
normally has data on that day (ET date of the fetch), and `empty` otherwise. The symbol set decides
(`YahooSource._expects_data`): `etf`/`stock` symbols expect data on XNYS business days only;
every other kind (futures, indices, yields, FX, unconfigured symbols) expects data on any weekday,
so a NYSE holiday does not excuse an empty answer for `ES=F` or `^TNX`. A weekend is `empty`.

## Which daily bars are kept

`parse` judges each bar at the fetch time (`RawResponse.responded_at`):

- `etf`/`stock` instruments (the `daily_fallback` symbols): XNYS calendar. The bar's date must be a
  session and the session (early closes honoured) must have closed.
- Everything else (futures, indices, yields): no exchange calendar is modelled. Saturday and
  Sunday bars are dropped (e.g. the `ES=F` Sunday-evening session bar), and so is any bar dated
  today or later in the exchange's own timezone (taken from the history index, ET if unknown),
  **even after that session has ended** (known limit: without a calendar the bar is emitted from
  the next day's run; the test `test_non_xnys_bar_dated_today_is_dropped_even_after_its_session_ends`
  pins it),
  because that session may still be running. Holidays on other exchanges are not filtered; Yahoo
  normally returns no bar for them.

## Scope: daily bars only, volume only in raw

`history` is daily only. The card text says "daily + 1m", but nothing in the spec or the
cross-checks consumes Yahoo 1m bars yet, and a 1m window multiplies requests against the 30/min
limit; add it with the check that needs it. Volume is not emitted as an observation (shares vs
contracts vs zero) but is kept in the raw body so it can be recovered on replay.

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

Yahoo may have quoted `^TNX` and `^IRX` as yield x 10 at some point (unverified, no source
checked); if a live value is ~10x the official yield the feed needs `scale: 0.1`. `config.yaml` currently sets no scale (1.0) and still carries
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
