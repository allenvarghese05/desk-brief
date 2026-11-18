# FRED source (`sources/fred.py`, card T11)

Endpoint: `https://api.stlouisfed.org/fred/series/observations` (JSON). Auth: `FRED_API_KEY`
(env), sent as the `api_key` query parameter through `HttpClient.get(secret_params=...)` only.

**Live probe status: pending: owner keys.** No key was available when this was built, so the
parser was written against FRED's documented response format and tested on SYNTHETIC FIXTURES
(`tests/fixtures/synthetic/fred/`, invented values). Nothing here has been verified against the
live service.

## Behaviour

| Item | Value |
|---|---|
| Series (from `config.yaml`, dataset `series`) | DGS2, DGS10, DGS20, DGS30, DFF, DCOILWTICO, DCOILBRENTEU, CPIAUCSL, UNRATE |
| Request | one per series: `series_id`, `file_type=json`, `sort_order=desc`, `limit=10` (backfill adds `observation_end=<target_date>`) |
| Why 10 | the latest observations are re-read every run so a later revision shows up in the revision check |
| Jobs | morning, eod, backfill fetch; intraday and rebuild plan nothing |
| Missing value | `"."` is skipped (no NaN row). All-missing or empty `observations` gives `empty_frame` |
| Malformed payload | `ParseError` (not JSON, no `observations` list, bad date, non-numeric/non-finite value, non-string fields, failed fetch) |
| Unconfigured series in a response | `empty_frame` (rows only for configured instruments) |
| Tier / unit / scale | from config (`Feed.tier`, `Instrument.unit`, `Feed.scale`) |

| Series | field | interval | unit |
|---|---|---|---|
| DGS2/10/20/30 | `yield` | `1d` | `pct` |
| DFF | `rate` | `1d` | `pct` |
| UNRATE | `rate` | `1mo` | `pct` |
| DCOILWTICO, DCOILBRENTEU | `close` | `1d` | `USD` |
| CPIAUCSL | `close` | `1mo` | `index_pts` |

`as_of` (P-006): `1d` = 00:00 America/New_York on the observation date; `1mo` = 00:00 ET on the
first of the month (the observation date is snapped to day 1).

## Missing `FRED_API_KEY`

Decision: **return an error `RawResponse`, do not skip the plan.** `fetch` makes no HTTP call and
returns `http_status=None`, `status=error`, `error="FRED_API_KEY is not set; no request made"`,
and logs `fred.fetch.no_api_key` (the name of the variable, never a value). The failure is then
recorded in `source_log` and surfaces in freshness/`source_down` checks like any other source
failure, instead of a quiet gap. A blank or whitespace-only value counts as missing; the value is
stripped before use.

## Key handling

The key never appears in `FetchRequest.params` (the model rejects secret-looking names anyway),
`RawResponse.url_redacted`, `RawResponse.error`, the stored raw body or logs. Covered by
`test_fetch_sends_key_on_the_wire_only`, `test_fetch_http_error_does_not_leak_key` and
`test_fetch_transport_error_does_not_leak_key`.

## Live probe checklist (owner, with a real key)

Run once locally (never commit the responses beyond what FRED's terms allow; keep real captures
out of the repo unless confirmed redistributable):

1. `GET series/observations?series_id=DGS10&file_type=json&sort_order=desc&limit=10&api_key=...`
   returns HTTP 200 and `Content-Type: application/json`.
2. The top-level object has an `observations` list; each item has string `date` (`YYYY-MM-DD`),
   string `value`, `realtime_start`, `realtime_end`. Confirm the response is ordered newest first
   and contains exactly 10 items (`count`/`limit`).
3. A US holiday (for example a recent Veterans Day or Columbus Day) appears as `"value": "."`,
   not as an absent row, and is skipped by the parser.
4. For all nine series: the date of the newest row, its cadence (daily vs the first of the month
   for CPIAUCSL and UNRATE), and that the values are in the expected units (percent for DGS*,
   DFF, UNRATE; USD per barrel for DCOIL*; index points for CPIAUCSL). Confirm `units=lin`
   is the default so no transformation is applied.
5. Run `FredSource.parse` over each captured body: it validates against the
   `parsed_observations` schema, `instrument_id`, `tier` and `unit` match `config.yaml`.
6. Revisions: re-fetch a monthly series (CPIAUCSL) after a release and note whether recent values
   changed; decide whether `realtime_start`/`realtime_end` vintages are needed (spec §5.2 mentions
   them). The current plan reads the current vintage only.
7. Error shapes: a request with a bad key returns HTTP 400 with a JSON `error_message`
   (the client reports it as `HTTP 400`); the response and `url_redacted` must not contain the key.
8. Lag: when each daily series (DGS*, DFF, DCOIL*) is published, to check the `freshness`
   entry for FRED in `config.yaml` (`expected_by_et: 16:30`, `lag_bdays: 1`, SIFMA_US).
   T25c: DCOIL* come from EIA's weekly spot-price release, so completeness judges them on
   `SeriesSpec.lag_bdays` (`OIL_LAG_BDAYS = 8`), not the dataset's 1 day; live run 3 failed
   both on the 1-day lag. Confirm the release day and tighten the value here.
9. Rate: 9 requests per run stay far below the configured `rate_per_min: 100`.

Record the date, HTTP statuses and any surprises in the PR and update this file.
