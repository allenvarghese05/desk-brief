# Alpaca source (`sources/alpaca.py`, card T14)

Base URL: `https://data.alpaca.markets/v2/stocks`. Auth: `ALPACA_KEY_ID` and `ALPACA_SECRET_KEY`
(env), sent as the `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` headers through
`HttpClient.get(secret_headers=...)` only.

**Live probe status: PENDING (owner keys).** No keys were available and live requests to provider
websites are blocked in the build environment, so the parser was written against Alpaca's documented
Market Data v2 format and tested on SYNTHETIC FIXTURES (`tests/fixtures/synthetic/alpaca/`, invented
values). Nothing here has been verified against the live service.

## Datasets

| Dataset | Endpoint | Fields | interval | tier | unit |
|---|---|---|---|---|---|
| `iex_latest` | `/trades/latest` and `/quotes/latest` (`feed=iex`, all symbols in one request each) | `last`; `bid`, `ask` | `tick` | 1 | instrument unit (USD) |
| `iex_bars_1m` | `/bars` (`timeframe=1Min`, `feed=iex`) | `open`, `high`, `low`, `close`, `volume` | `1m` | 1 | USD; volume in `shares` |
| `sip_bars_1m` | `/bars` (`timeframe=1Min`, `feed=sip`) | same | `1m` | 2 | same |

There is no `daily_bars` dataset (ruling P-003). Tier, unit and `scale` come from config; `scale`
applies to prices, never to volume. `as_of` (P-006): `tick` = trade/quote instant (nanoseconds are
truncated to microseconds); `1m` = bar start.

`iex_latest` needs two HTTP calls per plan (trades, quotes). They are told apart by the public
request param `part=trades|quotes`, which `fetch` removes before calling the API. A quote side of
`0` (Alpaca's "no quote") is skipped rather than stored as a price.

## Plans

| Job | Requests |
|---|---|
| morning | `iex_latest` trades + quotes (pre-market latest) |
| intraday | `iex_latest` trades + quotes |
| eod | `iex_bars_1m` and `sip_bars_1m` for `crosschecks.iex_vs_sip.symbols` over the XNYS session of `target_date` |
| backfill | the same bars for the target date |
| rebuild | nothing (replays raw files) |

The session window is `session_bounds_utc(target_date)`, so DST and early closes are honoured
(2026-11-27 closes 13:00 ET = 18:00 UTC). A day with no XNYS session plans nothing.

## SIP 15-minute rule

The free plan serves SIP data only once it is older than 15 minutes. `plan` clamps the SIP window
end to `ctx.now_utc - 16 minutes` (`SIP_DELAY`, one minute of margin) and drops the SIP request if
nothing is left (`end <= start`, for example an `eod` run minutes after the open). IEX is real time
and is not clamped. A violation would come back as HTTP 403 and is recorded as an ordinary fetch
error. Tests: `test_sip_window_always_ends_at_least_16_minutes_before_now` (sweep),
`test_plan_sip_window_exactly_16_minutes_old_is_allowed_unclamped`,
`test_plan_sip_is_skipped_when_the_whole_window_is_too_recent`.

## Pagination

Bars follow `next_page_token` inside `fetch` (`limit=10000` per page), bounded two ways: a hard cap
of `MAX_PAGES = 20` pages and a repeated-token check, so it can never loop forever. Either failure,
a failing HTTP page, or a non-JSON later page returns an error `RawResponse` (the whole fetch
fails; never a partial frame). When the failure is the page cap, a repeated token or a non-JSON
later page, the error `RawResponse` body still holds the pages fetched so far (same `pages` shape),
so raw keeps them even though `parse` refuses the response. A single page is stored byte-for-byte; several pages are stored as
`{"pages":[<page 1 bytes>,<page 2 bytes>,...]}` so each page stays verbatim, and `parse` accepts both
shapes.

## IEX covers one exchange only

On the free plan `feed=iex` returns trades, quotes and bars from the IEX exchange alone. IEX prices
can differ from the consolidated tape, and its volume is a small fraction of consolidated volume
(IEX volume is never comparable to SIP or to a full-market figure). The `tier` 1 / tier 2 split
and the `iex_vs_sip` cross-check exist because of this; do not show IEX volume as market volume.

## Deferred (T08)

* A SIP window guard in `fetch` (as well as `plan`) was not added: `fetch` would have to read the
  wall clock, which makes the recorded-fixture fetch tests depend on the date they run. `plan`
  stays the single guard (clamped at `now - 16 min`).
* Per-symbol quarantine (one bad symbol rejecting the whole payload) waits for the live probe,
  which shows whether real payloads ever carry one bad symbol among good ones.

## Errors and empty responses

* Non-2xx (401 bad keys, 403 SIP-too-recent, 422, 429 ...): the HttpClient returns an error
  `RawResponse`; `parse` raises `ParseError`.
* A provider error body (`{"code": ..., "message": ...}`), even inside a 200: `ParseError`.
* Malformed payloads (not JSON, missing `bars`/`trades`/`quotes`, bad timestamp, non-numeric or
  non-finite value, missing field): `ParseError`; one bad row rejects the whole payload.
* Empty `bars` (`{}` or `null`) or empty `trades`/`quotes`: `empty_frame`, and `fetch` marks the
  response `FetchStatus.EMPTY`. On a business day the T20 completeness check is what flags it.
* Symbols in a response that are not configured for that dataset are ignored.

## Missing keys

Decision (same as FRED): **return an error `RawResponse`, do not skip the plan.** If either
`ALPACA_KEY_ID` or `ALPACA_SECRET_KEY` is unset or blank, `fetch` makes no HTTP call and returns
`http_status=None`, `status=error`, `error="<NAME(S)> is not set; no request made"`, and logs
`alpaca.fetch.no_api_key` (variable names only). Values are stripped before use.

## Key handling

Keys never appear in `FetchRequest` (the model rejects secret-looking param names anyway),
`RawResponse.url_redacted`, `RawResponse.error`, the stored raw body or sidecar, or logs. Tests
(respx): `test_fetch_sends_keys_in_headers_only`, `test_keys_never_reach_the_raw_body_or_sidecar`,
`test_http_error_does_not_leak_keys`, `test_transport_error_does_not_leak_keys`.

## Live probe checklist (owner, with real keys)

Run once locally; do not commit the responses (commercial provider data):

1. `GET /v2/stocks/trades/latest?symbols=SPY,QQQ&feed=iex` and `/quotes/latest`: HTTP 200, objects
   keyed by symbol under `trades` / `quotes`, `t` an RFC 3339 timestamp with nanoseconds,
   `p` / `bp` / `ap` numbers. Note what a symbol with no quote looks like (`ap: 0`?) and what
   happens for a symbol IEX never traded.
2. `GET /v2/stocks/bars?symbols=SPY,QQQ&timeframe=1Min&start=...&end=...&feed=iex&adjustment=raw&sort=asc&limit=10000`:
   `bars` is an object of per-symbol arrays with `t,o,h,l,c,v,n,vw`; `t` is the bar start (UTC).
   Confirm what an empty result looks like (`{}` vs `null`) and what `next_page_token` is on the
   last page (`null`).
3. Pagination: repeat with `limit=100` to force several pages; confirm the token is accepted as
   `page_token` and the combined data has no duplicates or gaps.
4. SIP: `feed=sip` with `end` older than 15 minutes returns HTTP 200; with `end` inside the window,
   record the exact status and body (assumed 403 with a `message`). Confirm the margin of 16
   minutes is enough and not needlessly large.
5. Early close (for example 2026-11-27): bars stop at 12:59 ET; the last bar start is 17:59 UTC.
6. Errors: wrong key returns 401 (or 403) with a JSON `message`; the response and
   `url_redacted` must not contain the key.
7. Run `AlpacaSource.parse` over each capture: it validates against `parsed_observations`; tiers
   and units match `config.yaml`; volume rows have unit `shares`.
8. Rate: eod issues about 2 bars requests, intraday 2 latest requests, far below
   `rate_per_min: 180`.

Record the date, HTTP statuses and any surprises in the PR and update this file.
