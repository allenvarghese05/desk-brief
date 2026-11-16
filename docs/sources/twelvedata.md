# Twelve Data source (`sources/twelvedata.py`, card T15)

Endpoints: `https://api.twelvedata.com/quote` (batch, `fx_quote`) and
`https://api.twelvedata.com/time_series` (batch, `fx_series_1m`). Auth: `TWELVEDATA_API_KEY`
(env), sent as the `apikey` query parameter through `HttpClient.get(secret_params=...)` only.

- Live probe: PENDING (owner keys)

The parser was written against Twelve Data's documented response shapes and tested on SYNTHETIC
FIXTURES (`tests/fixtures/synthetic/twelvedata/`, invented values). Nothing here has been
verified against the live service.

## Behaviour

| Item | Value |
|---|---|
| Pairs (from `config.yaml`) | EUR/USD, GBP/USD, USD/JPY (datasets `fx_quote`, `fx_series_1m`, tier 1, unit `fx_rate`) |
| `fx_quote` | one batch request; `last` = quote `close`; `bid`/`ask` only if present; `interval=tick`; `as_of` = quote `timestamp` (unix, UTC) |
| `fx_series_1m` | one batch request, `interval=1min`, `timezone=UTC`, `order=asc`, `outputsize=60`; field `close`; `interval=1m`; `as_of` = bar start |
| Window | `crosschecks.fx_vs_ecb.fix_time_cet` (14:15) +/- 10 min, via `timeutil.local_to_utc(..., CET)`: 13:05-13:25 UTC in winter, 12:05-12:25 UTC in summer (21 bars; `FetchRequest.window` is `[start, end)` so it ends at 13:26) |
| Plans | morning, intraday: `fx_quote`. eod: `fx_series_1m` on TARGET business days only (holiday or weekend plans nothing). backfill, rebuild: nothing |
| Bars outside the window | dropped by `parse` |
| No `mid` field | none is produced: bars give `close`, quotes give `last` (and `bid`/`ask` when present). The `fx_vs_ecb` cross-check (spec §8.2 says "1m mid") must use the bar `close` at the fix bar, or compute a mid itself |

## Error handling decisions

1. **HTTP 200 with `{"status":"error",...}`**: detected in `fetch` (not only in `parse`). The
   `RawResponse` keeps the body byte-for-byte but carries `status=ERROR` (`RATE_LIMITED` if the
   payload code is 429) and `error="Twelve Data error <code>: <message>"` with the key masked, so
   `source_log` shows the real outcome. `parse` also raises `ParseError` on such a body so replaying a
   stored raw file fails the same way. Why `fetch` and not only `ParseError`: the model supports
   `status` for "provider error inside a 200", and the failure is then visible as a fetch failure
   rather than only as a quarantine.
2. **Batch where one symbol errors**: `fetch` leaves the response `ok`. `parse` emits the good
   symbols and logs `twelvedata.parse.symbol_error` (symbol and reason) for each bad one: provider
   error entry, non-numeric or non-positive value, missing timestamp, malformed bar, or a requested
   symbol absent from the response (counted as a failure, so a 200 object with none of the
   requested symbols, such as `{}` or an unrelated object, is a `ParseError`). If no symbol is
   usable, `ParseError`. Zero rows with valid
   entries (empty `values`) is a legitimate empty frame. The per-symbol flag is a log line only;
   there is no schema column for it. A DQ completeness check (T20) will see the missing pair.
3. **Missing key**: error `RawResponse` (`http_status=None`, `error` names `TWELVEDATA_API_KEY`),
   no HTTP call, logged as `twelvedata.fetch.no_api_key`. Blank or whitespace counts as missing.

## Budget accounting (known gap, for the runner/planner)

Twelve Data bills one credit per symbol: a 3-pair batch costs 3 credits (`credits_for(req)`
returns that number). The T03 `BudgetTracker.try_consume(source, day)` reserves exactly one unit
per HTTP attempt, and `HttpClient.get` (a contract in `base.py`) has no weight parameter. The
source cannot reach the tracker, so **a batch is tracked as 1 unit and the persisted daily count
under-reports real use by up to a factor of `len(symbols)`** (3 here). Pinned by
`test_budget_known_gap_batch_counts_one_tracked_unit`.

Why this is tolerable for now: real use is about 3 credits per intraday run (about 36 runs/day) plus
3 at morning and 3 at eod, roughly 120 of the provider's 800/day (8/min) limit, so the tracked
count would have to be 6x low before the plan breaks, and `daily_budget: 750` is below the
provider's cap. The per-minute limit (`rate_per_min: 7`) is also per request, so a batch is
one token.

Proposed follow-up (needs a `contract:` PR, so out of scope for T15): add `units: int = 1` to
`BudgetTracker.try_consume` and a `cost: int = 1` keyword to `HttpClient.get`; the source then
passes `cost=credits_for(req)`. The budget-exhausted test should then assert that a batch of 3
is refused when fewer than 3 units remain.

Budget exhausted: the client returns `BUDGET_EXHAUSTED` with no HTTP call; covered by
`test_budget_exhausted_makes_no_http_call` and `test_budget_last_unit_then_exhausted`.

## Key handling

The key never appears in `FetchRequest.params` (the model rejects secret-looking names),
`RawResponse.url_redacted`, `RawResponse.error`, the stored raw body or logs. Covered by the
`test_fetch_*_does_not_leak_key` tests and `test_fetch_200_error_message_echoing_key_is_masked`.
Tests build the fake key at runtime.

## Live probe checklist (owner, with a real key)

Run locally once; do not commit raw responses from this commercial provider.

1. `GET /quote?symbol=EUR/USD,GBP/USD,USD/JPY&apikey=...`: HTTP 200, object keyed by symbol,
   each with `close` and unix `timestamp`. Note whether `bid`/`ask` exist for FX (the parser
   emits them only if present) and whether `timestamp` is the quote time or the last-bar time.
2. `GET /time_series?symbol=...&interval=1min&start_date=...&end_date=...&timezone=UTC&order=asc`:
   confirm `datetime` strings are UTC when `timezone=UTC` is set, that `end_date` is inclusive of
   the 14:25 CET bar, that 21 bars come back, and that `datetime` is the bar start (the parser
   assumes all three).
3. Error shapes: a bad key gives `{"status":"error","code":401,...}` and with which HTTP status
   (the parser handles 200 and non-200). Out of credits: code 429. One unknown symbol in a batch:
   an error object under that symbol while the others succeed.
4. Credits: confirm in the dashboard that a 3-symbol batch costs 3 credits per call and what the
   per-minute limit counts.
5. Weekend or TARGET-closed day: what `/time_series` returns for an empty window (error 400 "no
   data" vs empty `values`); the planner already skips TARGET holidays.
6. Run `TwelveDataSource.parse` over each capture; it must validate against `parsed_observations`
   with `instrument_id`, `tier` and `unit` matching `config.yaml`.

Record the date, HTTP statuses and surprises in the PR and update this file.
