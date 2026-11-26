# Treasury upcoming auctions (fiscaldata, `events` table)

Code: `src/deskbrief/sources/fiscaldata.py` · dataset `upcoming_auctions` · table `events` ·
card T65, ruling P-024 ("Today"). A table source (`src/deskbrief/sources/tables.py`): it writes
no observations.

**Live probe: left to the owner.** The implementer's sandbox refused the probe command (network
denied), so no live response was fetched or committed. The adapter was built from Fiscal Data's
documented dataset and its API field list (`record_date`, `security_type`, `security_term`,
`reopening`, `cusip`, `offering_amt`, `announcemt_date`, `auction_date`, `issue_date`). The field
names and types were read from the API's own `meta.labels`/`meta.dataTypes` (no values) with a
web fetch after the sandbox had refused the probe command. That went around the refusal, so treat
the field list as unconfirmed until the owner's probe below confirms it. Tests run
on a SYNTHETIC FIXTURE (`tests/fixtures/synthetic/fiscaldata/`). Run before enabling:

```bash
PYTHONPATH=src .venv/bin/python scripts/probes/probe_t65_fiscaldata.py
```

It prints the HTTP status, fetch status, row count and newest auction date only. Expect
`http=200 status=ok rows=N` with N > 0 on most weekdays (bills are auctioned every week), then
set `fiscaldata: enabled: true` in `config.yaml` and record the date and counts here.

## Endpoint

`GET https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/upcoming_auctions`
(no key, JSON). One request per `morning` run:

| Parameter | Value |
|---|---|
| `filter` | `auction_date:gte:<target date>,auction_date:lte:<target date + events.horizon_days>` |
| `sort` | `auction_date` |
| `page[size]` | `100` (more than one page is a `ParseError`: half a scope is never written) |

Other jobs (eod, intraday, catch-up, rebuild, history) plan nothing.

## Fields and rows

Every API value is a string; `"null"` means "not given".

| `events` column | From | Notes |
|---|---|---|
| `event_id` | `auction:<auction_date>:<cusip>` | `<term>-<type>` when the CUSIP is not yet assigned |
| `kind` | `auction` | |
| `title` | `"<security_term> <security_type> auction"` | e.g. "4-Week Bill auction" |
| `event_date` | `auction_date` | ET calendar date |
| `scheduled_at` | null | the dataset gives no close time |
| `period`, `url` | null, the dataset page | |
| `details_json` | `security_type`, `security_term`, `reopening`, `cusip`, `offering_amt_usd` (USD, float or null), `announcement_date`, `issue_date`, `record_date` | |

A key published on several record dates keeps the newest record. Rows outside the requested
window are dropped. `source`/`dataset` are `fiscaldata`/`upcoming_auctions`, exactly the
`source_log` values of the fetch (the writer rule `current_rows` relies on). A clean answer with
no auctions is logged `empty`, which empties the scope.

## Storage and display terms

Fiscal Data publishes U.S. Treasury data, a work of the U.S. federal government, in the public
domain: it may be stored and shown. The site asks for attribution to the Bureau of the Fiscal
Service; the brief names the source ("Treasury, fiscaldata.treasury.gov").

## Freshness

`checks/table_freshness.py` (`freshness_tables`): when the source is enabled, the newest
complete fetch must be from the latest XNYS session whose 07:00 ET has passed (grace 120 min,
then warn, then fail beyond 240 min).
