# ECB euro foreign exchange reference rates

- Source key / dataset: `ecb` / `eurofxref` (tier 3, `field=rate`, `unit=fx_rate`; a reference rate, not a close, P-009).
- Code: `src/deskbrief/sources/ecb.py`; tests: `tests/unit/sources/test_ecb.py`.
- Endpoint: `https://data-api.ecb.europa.eu/service/data/EXR/D.USD+GBP+JPY.EUR.SP00.A` with `startPeriod`, `endPeriod` (same day) and `format=csvdata`. No key. Published around 16:00 CET on TARGET business days.
- Attribution: data from the European Central Bank (ECB euro foreign exchange reference rates). ECB statistics may be reused with source attribution; check the ECB terms of use before redistributing.

## What the source emits

| Instrument | Value | `is_derived` |
|---|---|---|
| EURUSD | ECB USD per EUR | false |
| GBPUSD | EURUSD / EURGBP | true |
| USDJPY | EURJPY / EURUSD | true |

`as_of` = 00:00 America/New_York on the reference date (P-006), `interval=1d`. The EUR/GBP and EUR/JPY rates stay in the raw file only.

## Decisions

- **Missing currency -> `ParseError`**, not a skip. If USD, GBP or JPY is absent for a date, the crosses cannot be built; emitting a partial set would be partial garbage (base.py rule). Duplicate, non-numeric, non-positive or NaN rates also raise.
- **No data rows -> empty frame.** `fetch` also maps HTTP 404 (the SDMX "no results" answer) to status `empty`, not `error`. Other HTTP errors stay errors.
- **TARGET holidays and weekends:** `plan` returns no request. Morning plans the previous TARGET business day; eod/backfill/rebuild plan `target_date`; intraday plans nothing.
- The parser ignores leading `#` comment lines (used by the synthetic fixture header).

## Live probe: PENDING

The probe was denied by the permission gate in the build environment, and the 404-on-holiday behaviour above is taken from ECB's documentation, not observed. The committed fixture `tests/fixtures/synthetic/ecb/exr_d_usd_gbp_jpy.csv` is a SYNTHETIC FIXTURE modeled on the documented SDMX CSV format. To close this out:

1. Run (no key needed):
   `curl -sS -D headers.txt -o exr.csv "https://data-api.ecb.europa.eu/service/data/EXR/D.USD+GBP+JPY.EUR.SP00.A?lastNObservations=3&format=csvdata"`
2. Check: HTTP 200; columns include `CURRENCY`, `CURRENCY_DENOM` (= `EUR`), `TIME_PERIOD` (`YYYY-MM-DD`), `OBS_VALUE`; three currencies per date; `Content-Type`.
3. Also check a TARGET holiday (for example `startPeriod=endPeriod=2026-04-06`): expect HTTP 404 or an empty body.
4. Save the real CSV as `tests/fixtures/real/ecb/exr_d_usd_gbp_jpy.csv` (small, 3 days), point the golden test at it, update expected values, delete the synthetic fixture, and record the date, status and sample rows here and in the PR.
