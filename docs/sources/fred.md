# FRED source (`sources/fred.py`, cards T11 and T46)

Endpoint: `https://api.stlouisfed.org/fred/series/observations` (JSON). Auth: `FRED_API_KEY`
(env), sent as the `api_key` query parameter through `HttpClient.get(secret_params=...)` only.

**Live probe status: pending: owner keys** (T11 series and, since T46, the five P-021a
series: checklist item 10). No key was available when this was built, so the
parser was written against FRED's documented response format and tested on SYNTHETIC FIXTURES
(`tests/fixtures/synthetic/fred/`, invented values). Nothing here has been verified against the
live service.

## Behaviour

| Item | Value |
|---|---|
| Series, dataset `series` | DGS2, DGS10, DGS20, DGS30, DFF, DCOILWTICO, DCOILBRENTEU, CPIAUCSL, UNRATE; T46: DFII10, T10YIE, DTWEXBGS, BAMLH0A0HYM2 |
| Series, dataset `series_weekly` (T46) | NFCI (every `1w` series goes here; a spec on the wrong dataset is logged at error level and not fetched) |
| Request | one per series: `series_id`, `file_type=json`, `sort_order=desc`, `limit=10` (backfill adds `observation_end=<target_date>`) |
| Range request (`plan_range`, P-020, T46) | one per series: `observation_start`, `observation_end`, `sort_order=asc`, `limit=100000` (FRED's maximum), so 2 years of daily data (~520 rows) is one call per series |
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
| DFII10 (10-year TIPS real yield), T10YIE (10-year breakeven) | `yield` | `1d` | `pct` |
| DTWEXBGS (nominal broad dollar index, Jan 2006 = 100) | `level` | `1d` | `index_pts` |
| BAMLH0A0HYM2 (ICE BofA US high-yield OAS) | `rate` | `1d` | `pct` |
| NFCI (Chicago Fed National Financial Conditions Index) | `level` | `1w` | `index` |

`as_of` (P-006): `1d` = 00:00 America/New_York on the observation date; `1mo` = 00:00 ET on the
first of the month (the observation date is snapped to day 1); `1w` = 00:00 ET on the date FRED
gives, which for NFCI is the Friday that ends the week (P-021).

## T46 series (P-021a)

Instruments in `config.yaml`, all tier 3:

| Instrument | FRED id | asset_class | kind | Report label (P-009) |
|---|---|---|---|---|
| `UST_10Y_REAL` | DFII10 | rates | yield | US 10-year TIPS real yield |
| `BEI_10Y` | T10YIE | rates | yield | US 10-year breakeven inflation (nominal minus real yield) |
| `USD_BROAD` | DTWEXBGS | fx | macro | Nominal broad US dollar index (level) |
| `HY_OAS` | BAMLH0A0HYM2 | macro | macro | ICE BofA US high-yield option-adjusted spread (rate) |
| `NFCI` | NFCI | macro | macro | Chicago Fed National Financial Conditions Index (level) |

- `USD_BROAD` has kind `macro`, not `fx`: config ties kind `fx` to unit `fx_rate`, and an index
  of the dollar against a basket is not a currency pair. Its changes read in points.
- Validity: a real yield may be negative; `pct` is `[-2, 25]` inclusive, so −2.00 passes and
  −2.01 fails (`test_tips_yield_down_to_minus_two_passes_validity`). NFCI is negative when
  conditions are looser than average; `index` is `[-100, 100000]`.
- Changes: the two yields move in bp, the HY OAS in pp (like DFF, kind `macro`), the dollar index
  and NFCI in points; NFCI week-on-week (T45).

### Publication timing and freshness

Sources, read on 2026-11-19 (public pages, no key): the Chicago Fed NFCI "current data" page and
FRED's series pages for NFCI, DTWEXBGS, DFII10 and BAMLH0A0HYM2 (their "Updated" stamps). To be
confirmed by the owner's probe (checklist item 10 below).

| Series | Published | Config |
|---|---|---|
| NFCI | Chicago Fed: Wednesdays 08:30 ET, covering the period through the previous Friday; when a federal holiday falls on or before Wednesday, Thursday instead. FRED posted the 18 Nov 2026 release at 07:35 CST (08:35 ET). | `series_weekly`: `release_weekday: 2`, `lag_bdays: 3` (Wed → Fri), `expected_by_et: "09:30"`, `grace_minutes: 90` |
| DFII10, T10YIE | H.15: the previous business day's value, posted around 16:15 ET (DFII10 updated 18 Nov 15:16 CST with 17 Nov) | dataset `series` (`16:30`, `lag_bdays: 1`), same as DGS10 |
| BAMLH0A0HYM2 | ICE BofA: the previous business day's close, posted in the morning (updated 19 Nov 09:28 CST with 18 Nov) | dataset `series` |
| DTWEXBGS | H.10, **weekly**: Monday about 16:15 ET (updated 16 Nov 15:17 CST with values through Fri 13 Nov; next release 23 Nov) | dataset `series`, plus `SeriesSpec.lag_bdays = 6` (`DOLLAR_LAG_BDAYS`) for completeness |

How the checks treat these:
- NFCI freshness and completeness (T45 weekly logic): a run on Wednesday before 09:30 ET judges
  the previous week's release; after 09:30 + 90 min without the new Friday value it warns, after
  3 h it fails. T45's holiday rule delays the release one business day per SIFMA holiday in the
  seven days ending on the Wednesday. This matches the Chicago Fed rule for a Monday–Wednesday
  holiday; for a holiday on the previous Thursday or Friday it delays a release that is not
  actually delayed, so a missing release is a `skip` until Thursday instead of a fail
  (lenient, never a false alarm).
- DTWEXBGS completeness requires the value for 6 SIFMA business days before the publication
  day: 1 day old right after the Monday release, 5 just before the next one, and a Monday eod
  run before FRED has posted the release still passes. Dataset-level freshness for `series` is
  unaffected (the other daily series are fresh).
- Anomaly (daily `level` rows since T45): DTWEXBGS moves are relative returns (base is always
  near 100–130, so the T45 base ≤ 0 guard never fires). The check uses the dataset's 1-day lag,
  so the Friday value is scored on the Monday eod and Tuesday runs after the release and is a
  harmless `stale series` skip later in the week
  (`test_dollar_anomaly_is_stale_series_skip_late_in_the_week`). NFCI is weekly and gets no
  anomaly check (T45 known limit).

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
9. Rate: 14 requests per run (T46) stay far below the configured `rate_per_min: 100`.
10. T46 series. Run from the repository root after loading `.env` into the shell
    (`set -a; . ./.env; set +a`). The one-liner prints no key, no URL and no body; it runs the
    real `FredSource.plan_range` (2 years), `fetch` and `parse` through the pipeline's HTTP
    client, whose log lines carry only the redacted URL:

    ```bash
    PYTHONPATH=src .venv/bin/python -c 'from datetime import date, timedelta; from deskbrief.config import load_config; from deskbrief.models import JobKind, RunContext, Trigger; from deskbrief.sources.fred import FredSource; from deskbrief.sources.http import DefaultHttpClient; from deskbrief.timeutil import utc_now; from deskbrief.schemas import validate; from deskbrief.checks.validity import find_violations; c = load_config("config.yaml"); s = FredSource(c); h = DefaultHttpClient(c.sources); e = date.today(); x = RunContext(run_id="probe", job=JobKind.HISTORY, trigger=Trigger.MANUAL, target_date=e, now_utc=utc_now(), git_sha="-", config_hash="-", config=c, store=None); [print(r.symbols[0], r.dataset, w.http_status, w.error or "", *((len(d), d["as_of"].max().tz_convert("America/New_York").strftime("%a %Y-%m-%d"), d.sort_values("as_of")["value"].iloc[-1], d["value"].min(), d["value"].max(), sorted(set(d["field"])), sorted(set(d["interval"])), sorted(set(d["unit"])), "schema ok" if validate("parsed_observations", d) is not None else "", "out_of_range=%d" % len(find_violations(d, c.checks.validity))) if (d := (s.parse(w) if w.ok else None)) is not None else ())) for r in s.plan_range(e - timedelta(days=730), e, x) if r.symbols[0] in ("DFII10", "T10YIE", "DTWEXBGS", "BAMLH0A0HYM2", "NFCI") for w in [s.fetch(r, h)]]'
    ```

    For each series it prints the HTTP status, the number of observations, the newest
    non-missing date and value, min and max, the parsed field/interval/unit, `schema ok` once
    the frame validates against `parsed_observations`, and `out_of_range=N`, the number of rows
    outside `checks.validity` (expect 0). Check: NFCI's newest date is a Friday and the release was on a Wednesday
    (FRED's "Updated" stamp); DTWEXBGS's newest date is the Friday before the latest Monday;
    DFII10/T10YIE/BAMLH0A0HYM2 are one business day old; values are in the expected ranges
    (DFII10 can be negative; NFCI usually is). A 2-year `plan_range` request per series
    returns all observations in one response (the count is the second number).

Record the date, HTTP statuses and any surprises in the PR and update this file.

## Release dates (card T65, P-024 "Today")

Dataset `release_dates`, table `events` (not observations). In the `morning` job only, one
request per `config.events.fred_releases` entry:
`GET https://api.stlouisfed.org/fred/release/dates` with `release_id`, `file_type=json`,
`realtime_start=<target date>`, `realtime_end=<target date + events.horizon_days>`,
`include_release_dates_with_no_data=true` (otherwise future dates are left out) and
`sort_order=asc`. The key is the same `FRED_API_KEY`, sent as a secret param.

Each date in the window becomes one `release` event: `event_id = release:<id>:<date>`, title from
the config entry, `scheduled_at` = the date at the entry's `time_et` in New York (FRED gives no
time), `details_json` = release id and FRED's release name, `url` =
`https://fred.stlouisfed.org/release?rid=<id>`. All release fetches share one scope
`(fred, release_dates)`: if one fails or is quarantined, that run writes no release events and
the previous run's stay current. Tests use a SYNTHETIC FIXTURE
(`tests/fixtures/synthetic/fred_releases/`).

**`events.fred_releases` is still empty** in `config.yaml`. The T64 contract test
`test_events_settings` pins it to `()` until the ids are confirmed, so filling it also updates
that test. The release ids were not verified by the implementer (no key, no live call). Candidates
to confirm with the probe's `--list` (FRED's own release ids and names), then by fetching them:

| Release (as FRED names it) | Candidate id | time_et |
|---|---|---|
| Consumer Price Index | 10 | 08:30 |
| Employment Situation | 50 | 08:30 |
| Gross Domestic Product | 53 | 08:30 |
| Personal Income and Outlays (PCE) | 54 | 08:30 |
| Advance durable goods (Manufacturers' Shipments, Inventories, and Orders) | confirm with `--list` | 08:30 |
| Consumer confidence | FRED may not carry the Conference Board release; University of Michigan Surveys of Consumers is the usual FRED alternative (confirm with `--list`) | 10:00 |

```bash
FRED_API_KEY=... PYTHONPATH=src .venv/bin/python scripts/probes/probe_t65_fred_releases.py --list
FRED_API_KEY=... PYTHONPATH=src .venv/bin/python scripts/probes/probe_t65_fred_releases.py 10 50 53 54
```

The second prints, per release, the HTTP status, fetch status, row count and newest date in the
horizon. **Probe: left to the owner.**
