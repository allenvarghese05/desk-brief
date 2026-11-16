# Cboe VIX history (daily close, tier 3)

Code: `src/deskbrief/sources/cboe.py` · dataset `vix_history` · symbol `VIX`.

## Endpoint

`GET https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv` (no auth, no
params): the whole history since 1990, columns `DATE,OPEN,HIGH,LOW,CLOSE`, dates `MM/DD/YYYY`.
The URL is from memory of the public Cboe page and is **unverified** (see below).

## Plan and parse

`plan` emits one request carrying `lookback_days=10` and `target_date`. The whole file is
downloaded (and stored raw, so any date can be replayed); `parse` emits **only** rows dated within
`[target_date - lookback_days, target_date]`. A request without `lookback_days` (e.g. a backfill)
yields every row. Only `close` is emitted: unit `index_pts`, interval `1d`, tier 3, `as_of` =
00:00 ET of the row date (P-006). Not planned for `intraday`.

## Failure modes

HTML/error pages served with status 200, a missing header, a bad date or number raise `ParseError`.
An empty or header-only body, or a window with no rows, is a legitimate empty result. Up to four
title lines before the header and lines starting with `#` are tolerated.

## Live probe status: PENDING

Not run: outbound requests to provider sites were denied by the permission gate in the
environment this card was built in. The parser rests on the documented format and a synthetic
fixture (`tests/fixtures/synthetic/cboe/`, header "SYNTHETIC FIXTURE"). Cboe's redistribution terms
were not checked, so no real capture is committed. To verify:

    curl -sS -I https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv
    curl -sS https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv | head -3

Expect `200` and the header above. If the URL has moved, change `URL` in `cboe.py`; if the file
format changed, `parse` fails loudly. Disable with `sources.cboe.enabled: false`.
