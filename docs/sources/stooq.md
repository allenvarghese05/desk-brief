# Stooq (daily closes, tier 3)

Code: `src/deskbrief/sources/stooq.py` · dataset `daily` · symbols are `*.us` (config `instruments[].feeds`).

## Endpoint

`GET https://stooq.com/q/d/l/?s=spy.us&i=d&d1=YYYYMMDD&d2=YYYYMMDD` returns a CSV
`Date,Open,High,Low,Close,Volume` (one request per symbol). The plan asks for the 10 calendar days
ending on the run's target date and is skipped for `intraday`.

## Output

`open/high/low/close` (instrument unit, `Feed.scale` applied) and `volume` (unit `shares`),
interval `1d`, tier 3, `as_of` = 00:00 ET of the row date (P-006).

## Live probe status: PENDING

The day-1 probe has **not been run**: the card was built in an environment where outbound requests
to provider sites were denied by the permission gate. Nothing in this repo has been verified
against the live site. The parser is built from the documented CSV shape and a synthetic fixture
(`tests/fixtures/synthetic/stooq/`, header "SYNTHETIC FIXTURE"). Stooq's redistribution terms were
not checked, so no real capture is committed.

Known risk (from public reports, unverified here): Stooq may now answer automated CSV requests with
a captcha/apikey page, "Exceeded the daily hits limit", or "No data". All of these arrive as HTTP
200 and make `parse` raise `ParseError` (the runner quarantines the fetch and records a DQ failure).

## What the probe must decide

Run once from a normal machine (and once from a GitHub Actions runner):

    curl -sS -i -A "desk-brief/0.1 (+https://github.com/allenvarghese05/desk-brief)" \
      "https://stooq.com/q/d/l/?s=spy.us&i=d&d1=20261101&d2=20261113"

* Header line `Date,Open,High,Low,Close,Volume` and rows: Stooq works. Replace the synthetic fixture with
  the capture if the terms allow redistribution; otherwise keep the synthetic one.
* HTML, captcha, apikey prompt, or "Exceeded the daily hits limit": Stooq is blocked.

## Switching it off

In `config.yaml` set `sources.stooq.enabled: false`. The registry then never builds it and the HTTP
client refuses to send for it. Yahoo daily close (T16) provides the labelled fallback.

## Behaviour notes

* A window with no rows (holiday week) is answered "No data" by Stooq, indistinguishable from an
  unknown symbol, so it is a `ParseError`, not an empty result. The 10-day window makes this rare.
* Lines starting with `#` are ignored (fixture headers).
