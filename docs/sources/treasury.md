# Source note: US Treasury daily par yield curve (T10)

Module: `src/deskbrief/sources/treasury.py` · Dataset: `par_curve` · Auth: none · Tier: 3
(official daily close) · Calendar: SIFMA_US · Public domain data.

## Endpoint and format (from Treasury's documented download; NOT yet verified live)

- URL pattern (monthly CSV):
  `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{YYYY}/all?type=daily_treasury_yield_curve&field_tdr_date_value_month={YYYYMM}&_format=csv`
- Columns: `Date` (`MM/DD/YYYY`), then tenors `1 Mo`, `1.5 Month` (newer files), `2 Mo`, `3 Mo`,
  `4 Mo`, `6 Mo`, `1 Yr`, `2 Yr`, `3 Yr`, `5 Yr`, `7 Yr`, `10 Yr`, `20 Yr`, `30 Yr`.
- Values are percent (4.25 = 4.25%); blank cells mean "not published". Only business days appear.
- The parser matches headers to configured symbols ignoring case/whitespace, ignores unknown
  columns, skips leading `#` comment lines, returns an empty frame for an empty month and raises
  `ParseError` for HTML, a missing `Date` column, bad dates or non-numeric/non-finite values.

## Live probe status

**PENDING.** The probe was blocked in the agent environment (the request was denied by the
permission gate; no workaround was attempted). Until a real capture exists, the unit tests use the
labelled synthetic fixture `tests/fixtures/synthetic/treasury/par_curve_202611.csv`.

### Probe steps (owner, or the GHA probe job)

1. Run:
   `curl -sSL -D headers.txt -o par_curve.csv "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/2026/all?type=daily_treasury_yield_curve&field_tdr_date_value_month=202611&_format=csv"`
2. Verify: HTTP 200 (note any redirect host; add it to the sandbox allowlist), `Content-Type`
   is CSV, the first line is the header above (watch for renamed tenors, e.g. `1.5 Month`, or a
   changed date format), the row count equals the number of SIFMA business days published so far
   in the month, and values look like percent.
3. Also check one month with a holiday (e.g. `202611`, no 11/11 or 11/26 rows) and, if possible,
   a future month (expect header-only or an empty body, both handled as an empty month).
4. Save a trimmed real file (header plus ~5 rows; public domain) to
   `tests/fixtures/real/treasury/par_curve_<YYYYMM>.csv`, add a golden test against it, and
   record the endpoint, date, HTTP status and row count here and in the PR.
5. If the URL or columns differ, update `BASE_URL` / the parser and bump `parser_version`.
