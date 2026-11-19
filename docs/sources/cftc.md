# CFTC Commitments of Traders (weekly positioning, tier 3)

Code: `src/deskbrief/sources/cftc.py` · dataset `cot` · card T49, ruling P-021c.

| Instrument | Market (CFTC contract market code) | Field | Symbol |
|---|---|---|---|
| `COT_GC_NET` | Gold, COMEX (`088691`) | `net_position` | `088691:net_position` |
| `COT_ES_NET` | E-mini S&P 500, CME (`13874A`) | `net_position` | `13874A:net_position` |
| `COT_ZN_NET` | 10-year Treasury note, CBOT (`043602`) | `net_position` | `043602:net_position` |
| `COT_GC_OI` | Gold, COMEX (`088691`) | `open_interest` | `088691:open_interest` |
| `COT_ES_OI` | E-mini S&P 500, CME (`13874A`) | `open_interest` | `13874A:open_interest` |
| `COT_ZN_OI` | 10-year Treasury note, CBOT (`043602`) | `open_interest` | `043602:open_interest` |

All are `asset_class: macro`, `kind: macro`, unit `contracts`, interval `1w`, tier 3. The
report and dashboard label them "net speculative position" and "open interest" (T45).

## Report variant: legacy, futures only

CFTC publishes three variants. The **legacy** report is used, futures only:

- The **disaggregated** report covers physical commodities only. It has gold but not the S&P
  e-mini or the 10-year note.
- The **Traders in Financial Futures** (TFF) report covers financial futures only. It has the
  e-mini and the note but not gold.
- The legacy report covers all three markets with one trader split. Its **non-commercial**
  category is the usual "speculative" position, which is what P-021 and the T45 label name.

Futures only rather than futures-and-options combined: it is the standard headline series, and
the combined report converts options to futures-equivalent delta, which mixes in a model.

- `net_position` = `noncomm_positions_long_all` − `noncomm_positions_short_all`. Spreading
  positions are left out, as is usual. CFTC does not publish this difference, so the rows carry
  `is_derived = true` and the brief lists them under "derived values".
- `open_interest` = `open_interest_all`, as published (`is_derived = false`).

## Endpoint

`GET https://publicreporting.cftc.gov/resource/6dca-aqww.json` is CFTC's public-reporting
Socrata (SODA) API. It is keyless. The request uses SoQL parameters:

- `$select`: the five columns above plus `cftc_contract_market_code` and
  `report_date_as_yyyy_mm_dd`.
- `$where`: `cftc_contract_market_code in('088691','13874A','043602') AND
  report_date_as_yyyy_mm_dd between '<start>T00:00:00' and '<end>T00:00:00'`.
- `$order`: report date, then code.
- `$limit=5000`.

The answer is a JSON list of objects with every value as a string. Dates look like
`2026-11-10T00:00:00.000`. User-Agent: `sources.cftc.user_agent` in `config.yaml` (the project
UA). Rate limit: 10 requests a minute; one run makes one request.

## Plan and parse

- `plan` makes one request for every configured market, covering the reports dated in the
  last 35 days (`LOOKBACK_DAYS`, four or five reports). A late or revised report is picked up on
  the next run. Nothing is planned for `intraday` or `rebuild`. A configured symbol that is not
  `<known code>:<field>` is logged (`cftc.plan.skip`) and not fetched.
- `plan_range(start, end, ctx)` (P-020) makes the same single request over `[start, end]`. Two
  years of three markets is about 312 rows, one call. End before start raises `ValueError`.
- `start` and `end` also travel in `FetchRequest.params`, and `parse` keeps only rows dated in
  that window. They are not sent to CFTC; only the `$` parameters are.
- **`as_of` = 00:00 ET of the report's position date**, normally the Tuesday. It is not the
  Friday release. This is the `1w` rule in the `store/backend.py` docstring (P-021): the
  reference date is the date the weekly value describes. So `et_date(as_of)` is the Tuesday, and
  the T45 freshness check finds it as the release's `data_day` (`lag_bdays: 3`, Friday back to
  Tuesday).

## Failure modes

Each of these raises `ParseError`:

- a non-list body: a Socrata error object `{"error": true, "message": ...}`, or HTML;
- a row that is not an object, or has no code;
- a bad or missing date;
- a count that is not a non-negative whole number, or is non-finite;
- two reports for one market and date;
- a body with 5000 or more rows, which may have been cut;
- a request without ISO `start` and `end`.

Rows for markets that are not configured are ignored. An empty list is the legitimate empty
result.

## Publication time and freshness

CFTC's release schedule page says the reports come out at 3:30 p.m. Eastern. They are usually
released on Friday and usually hold data from the previous Tuesday
(<https://www.cftc.gov/MarketReports/CommitmentsofTraders/ReleaseSchedule/index.htm>, read
2026-11-19). In 2026 a federal holiday in the week moves the release to the next Monday:

| Release (Monday) | Position date | Holiday |
|---|---|---|
| Jan 5 | Dec 30, 2025 | New Year's Day |
| Jun 22 | Jun 16 | Juneteenth |
| Jul 6 | Jun 30 | Independence Day (observed) |
| Nov 16 | Nov 10 | Veterans Day |
| Nov 30 | Nov 24 | Thanksgiving |
| Dec 28 | Dec 22 | Christmas |

The position dates in this table are inferred from the "previous Tuesday" rule; the page lists
release dates only.

The freshness entry is:

```yaml
{source: cftc, dataset: cot, expected_by_et: "15:30", lag_bdays: 3, calendar: SIFMA_US,
 grace_minutes: 180, release_weekday: 4}
```

How the fields were set:

- `release_weekday: 4` (Friday) and `expected_by_et: "15:30"` come from the schedule.
- `lag_bdays: 3` is Friday back to Tuesday.
- `SIFMA_US` holds the federal holidays (Veterans Day and Columbus Day included; XNYS has neither).
- T45 delays a release by one business day for each holiday in its week, which lands on the
  Monday in every case above. Until then a missing release is `skip` ("release delayed by
  holiday"), never `fail`.
- `grace_minutes: 180` allows for the Socrata copy lagging the 15:30 web release. The eod run
  (22:30 UTC) is after 15:30 ET, so Friday's report is normally fetched the same day.

Known limit: the schedule does not delay the release for Monday holidays such as Martin Luther
King Day or Columbus Day, and Good Friday does not delay it either. T45 still counts them, so in
those weeks freshness waits until Monday before warning. That gives a later `warn`, never a false
`fail`, and a report that arrives on Friday passes as usual.

Weekly series get no anomaly check (T45 known limit).

## Live probe (2026-11-19, T49)

Run by the implementer on Thursday 2026-11-19. The session's permission gate refused `curl` from
the shell and the in-app browser, so the SODA API was read with the agent's web-fetch tool. That
tool returns the JSON text but no status code, so HTTP 200 is inferred from the JSON being served.
Counts, min and max were computed **by the server** (SoQL `count`, `min`, `max`, `$group`), not
estimated. No bot check, CAPTCHA or block page was seen.

Newest rows (`$order=report_date DESC`):

| Market | Newest report date | Non-comm long | Non-comm short | Net | Open interest |
|---|---|---|---|---|---|
| Gold `088691` | 2026-11-10 (Tue) | 226,234 | 36,127 | +190,107 | 404,725 |
| E-mini S&P `13874A` | 2026-11-10 (Tue) | 211,412 | 350,046 | −138,634 | 2,017,687 |
| 10Y note `043602` | 2026-11-10 (Tue) | 684,024 | 1,667,940 | −983,916 | 6,095,194 |

The newest report, 10 Nov, fits the schedule. It was released on Monday 16 Nov (Veterans Day
delay), and the 17 Nov report is due Friday 20 Nov.

Two-year range, 2024-11-19 to 2026-11-19:

| Market | Rows | First | Last | Net min | Net max | OI min | OI max |
|---|---|---|---|---|---|---|---|
| Gold | 104 | 2024-11-19 | 2026-11-10 | 154,260 | 302,508 | 326,052 | 577,505 |
| E-mini S&P | 104 | 2024-11-19 | 2026-11-10 | −225,074 | 80,613 | 1,879,423 | 2,596,020 |
| 10Y note | 104 | 2024-11-19 | 2026-11-10 | −1,078,470 | −534,883 | 4,447,413 | 6,254,472 |

The exact daily query `plan` builds (the URL in the `http.done` log line) was fetched as well,
for the window 2026-10-15 to 2026-11-19. It returned 12 rows: the reports of 20 Oct, 27 Oct,
3 Nov and 10 Nov for each of the three markets. The 3 Nov and 10 Nov values match the table
above.

Every value is inside the `contracts` validity bound `[-10,000,000, 100,000,000]`. There is one
row per market and report date (104 weekly reports), so the duplicate guard does not fire on real
data.

The fixture `tests/fixtures/synthetic/cftc/cot_legacy.json` copies the column names and the date
format, with invented values. CFTC data is public, but no real capture is committed (repo rule:
synthetic fixtures only).

Owner check: this gives the exact HTTP status, row counts, newest row, min and max through the
real `plan_range`/`fetch`/`parse`. It is keyless and prints no body. Run from the repository
root:

```bash
PYTHONPATH=src .venv/bin/python -c 'from datetime import date, timedelta; from deskbrief.config import load_config; from deskbrief.models import JobKind, RunContext, Trigger; from deskbrief.sources.cftc import CftcSource; from deskbrief.sources.http import DefaultHttpClient; from deskbrief.timeutil import utc_now; c = load_config("config.yaml"); s = CftcSource(c); h = DefaultHttpClient(c.sources); e = date.today(); x = RunContext(run_id="probe", job=JobKind.HISTORY, trigger=Trigger.MANUAL, target_date=e, now_utc=utc_now(), git_sha="-", config_hash="-", config=c, store=None); r = s.plan_range(e - timedelta(days=730), e, x)[0]; w = s.fetch(r, h); print("status", w.http_status, w.error or ""); d = s.parse(w); [print(i, len(g), g["as_of"].max().tz_convert("America/New_York").date(), g.sort_values("as_of")["value"].iloc[-1], g["value"].min(), g["value"].max()) for i, g in d.groupby("instrument_id")]'
```
