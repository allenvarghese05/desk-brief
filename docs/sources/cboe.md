# Cboe daily index history (daily close, tier 3)

Code: `src/deskbrief/sources/cboe.py` · dataset `vix_history` · symbols VIX (T13), and VIX9D,
VIX3M, VIX6M, VXN, GVZ and SKEW (T47, ruling P-021b).

| Symbol | Instrument | What it is | Layout |
|---|---|---|---|
| `VIX` | `VIX` | S&P 500 30-day implied vol | OHLC |
| `VIX9D` | `VIX9D` | S&P 500 9-day implied vol | OHLC |
| `VIX3M` | `VIX3M` | S&P 500 3-month implied vol | OHLC |
| `VIX6M` | `VIX6M` | S&P 500 6-month implied vol | OHLC |
| `VXN` | `VXN` | Nasdaq-100 30-day implied vol | OHLC |
| `GVZ` | `GVZ` | Gold ETF (GLD) 30-day implied vol | single |
| `SKEW` | `SKEW` | S&P 500 tail-risk (skew) index | single |

All are `asset_class: vol`, `kind: index`, unit `index_pts`, field `close`, interval `1d`, tier 3.
The dataset keeps its T13 name `vix_history` for every symbol, so the one freshness entry
(`expected_by_et: "20:00"`, `lag_bdays: 0`, XNYS) covers all seven.

## Endpoint

`GET https://cdn-api.cboe.com/api/global/us_indices/daily_prices/{SYMBOL}_History.csv` (no auth,
no params): each file is the symbol's whole history. Dates are `MM/DD/YYYY`. Two layouts exist
(`SYMBOL_SPECS` in the code):

- **OHLC:** `DATE,OPEN,HIGH,LOW,CLOSE`. `CLOSE` is emitted.
- **single:** `DATE,<SYMBOL>` (`DATE,GVZ`, `DATE,SKEW`). The one value column is emitted.

The old host `cdn.cboe.com` answers 307 to `cdn-api.cboe.com` (probe 2026-11-18, below).

## Plan and parse

- `plan` emits one request per configured symbol, each carrying `lookback_days=10` and
  `target_date`. Not planned for `intraday`. A configured symbol with no layout in
  `SYMBOL_SPECS` is logged (`cboe.plan.skip`) and not fetched.
- `plan_range(start, end, ctx)` (P-020) emits one request per symbol carrying `start` and `end`
  (ISO dates) and `target_date = end`. Each file holds the whole history, so two years is one
  download per symbol. End before start raises `ValueError`.
- The whole file is downloaded (and stored raw, so any date can be replayed). `parse` emits
  **only** rows dated within `[target_date - lookback_days, target_date]` or `[start, end]`. A
  request with neither (e.g. a backfill) yields every row.
- `as_of` = 00:00 ET of the row date (P-006). `parser_version` is `2` since T47; VIX rows are
  the same values as before.

## Failure modes

- HTML/error pages served with status 200, a header that is not the requested symbol's layout
  (for example a SKEW request answered with an OHLC file), a bad date or a bad number raise
  `ParseError`. So do a request with no known symbol, and a range request missing `start` or
  `end` or with a bad date.
- Only rows inside the requested window are validated, so one bad historical row (bad number,
  wrong column count) is skipped instead of failing every daily run. A bad row inside the window,
  or an unparsable date, still raises (a request without a window validates every row).
- A body that does not end in a newline, when its last row is inside the window, is treated as a
  truncated download (a cut such as `16.` would otherwise parse as 16.0) and raises `ParseError`.
- `csv.Error` and an out-of-range `lookback_days` (`OverflowError`) also become `ParseError`.
- An empty or header-only body, or a window with no rows, is a legitimate empty result. Up to
  four title lines before the header and lines starting with `#` are tolerated.

## Publication time and freshness

Each file is updated once a day after the close. At the T47 probe (2026-11-19 20:08 UTC,
15:08 ET on a trading day) every file ended at 11/18/2026, so the current day's row is not there
during the session. The existing entry (`expected_by_et: "20:00"`, `lag_bdays: 0`) expects the
day's row by 20:00 ET; before that freshness is `skip`. The exact posting time was not measured;
if live runs show it later than 20:00 ET, the freshness entry should move.

In the morning brief a Cboe close dated the previous NYSE day shows its value with "no move yet"
(`query.overnight_changes`, price family). VIX gets its overnight move from the Yahoo quote
(`^VIX`). Since T55 the six other indices have one too: a tier-2 Yahoo `quote` feed each (`^VIX9D`,
`^VIX3M`, `^VIX6M`, `^VXN`, `^GVZ`, `^SKEW`). The implementer's live probe on 2026-11-20 at
00:28 UTC, through `YahooSource._quote`, got a USD price for all six (19 Nov regular-market times
21:02 to 22:00 UTC). The dashboard bars show the full daily series.

## Live probe (2026-11-19, T47)

Run by the implementer on 2026-11-19 at about 20:10 UTC. The sandboxed shell's `curl` was not
permitted in that session, so each file was read with the agent's web-fetch tool. That tool
returns the text (no status code, so HTTP 200 is inferred from the CSV being served) and pages
through it; it does not compute statistics. Row counts below are estimated from the reported
text length and the row width; min and max were not computed (owner command below). No bot check,
CAPTCHA or block page was seen.

| Symbol | Header | First row | Newest row (date, close) | Rows (est.) |
|---|---|---|---|---|
| VIX9D | `DATE,OPEN,HIGH,LOW,CLOSE` | 01/04/2011 | 11/18/2026, 23.95 | ≈3,980 (202,835 chars) |
| VIX3M | `DATE,OPEN,HIGH,LOW,CLOSE` | 09/18/2009 | 11/18/2026, 24.29 | ≈4,320 (220,294 chars) |
| VIX6M | `DATE,OPEN,HIGH,LOW,CLOSE` | 01/02/2008 | 11/18/2026, 24.75 | ≈4,750 (242,326 chars) |
| VXN | `DATE,OPEN,HIGH,LOW,CLOSE` | 09/14/2009 | 11/18/2026, 33.12 | ≈4,330 (220,593 chars) |
| GVZ | `DATE,GVZ` | 09/18/2009 | 11/18/2026, 28.04 | ≈4,300 (by trading days) |
| SKEW | `DATE,SKEW` | 01/02/1990 | 11/18/2026, 142.84 | ≈9,270 (204,016 chars) |

Values are printed with six decimals (`23.950000`). The fixtures under
`tests/fixtures/synthetic/cboe/` copy each header and layout with invented values (header line
"SYNTHETIC FIXTURE"). Cboe's redistribution terms were not checked, so no real capture is
committed.

Owner check (exact counts, min and max; keyless, prints no body). Run from the repository root:

```bash
PYTHONPATH=src .venv/bin/python -c 'from datetime import date; from deskbrief.config import load_config; from deskbrief.models import JobKind, RunContext, Trigger; from deskbrief.sources.cboe import CboeSource; from deskbrief.sources.http import DefaultHttpClient; from deskbrief.timeutil import utc_now; c = load_config("config.yaml"); s = CboeSource(c); h = DefaultHttpClient(c.sources); e = date.today(); x = RunContext(run_id="probe", job=JobKind.HISTORY, trigger=Trigger.MANUAL, target_date=e, now_utc=utc_now(), git_sha="-", config_hash="-", config=c, store=None); [print(r.symbols[0], w.http_status, w.error or "", *((len(d), d.sort_values("as_of")["as_of"].iloc[-1].tz_convert("America/New_York").date(), d.sort_values("as_of")["value"].iloc[-1], d["value"].min(), d["value"].max()) if (d := (s.parse(w) if w.ok else None)) is not None else ())) for r in s.plan_range(date(1990, 1, 1), e, x) for w in [s.fetch(r, h)]]'
```

### Owner run of that check (2026-11-19 15:29 ET, exact)

Run by the owner at the T47 head and posted in
[the PR #78 comment](https://github.com/allenvarghese05/desk-brief/pull/78#issuecomment-7021366889).
These values replace the estimates above. Every file came from `cdn-api.cboe.com` with no bot
check. At 15:29 ET each file still ended at the prior trading day, which fits an after-close
update; the 20:00 ET freshness time is still to be confirmed by the first scheduled runs. Every
value is within the `index_pts` validity bounds.

| Symbol | HTTP | Rows | Newest (ET date, close) | Min | Max |
|---|---|---|---|---|---|
| VIX | 200 | 9,319 | 2026-11-18, 24.56 | 9.14 | 82.69 |
| VIX9D | 200 | 3,993 | 2026-11-18, 23.95 | 7.10 | 106.66 |
| VIX3M | 200 | 4,319 | 2026-11-18, 24.29 | 11.85 | 72.98 |
| VIX6M | 200 | 4,751 | 2026-11-18, 24.75 | 13.75 | 61.47 |
| VXN | 200 | 4,325 | 2026-11-18, 33.12 | 10.31 | 80.08 |
| GVZ | 200 | 4,317 | 2026-11-18, 28.04 | 8.88 | 48.98 |
| SKEW | 200 | 9,273 | 2026-11-18, 142.84 | 101.23 | 183.12 |

## Live probe (2026-11-18, T25)

The first live run (2026-11-17) got HTTP 307 from the original URL. The owner probed it on
2026-11-18 01:20 UTC (server `cloudflare`):

    curl -sI https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv
    HTTP/2 307
    location: https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv

The target is the same official Cboe file on a new host, so `URL` in `cboe.py` points at
`cdn-api.cboe.com` (pinned by `test_url_is_the_cdn_api_host_the_old_host_redirects_to`). The
shared HTTP client's redirect policy is unchanged. If the file format changes, `parse` fails
loudly. Disable with `sources.cboe.enabled: false`.
