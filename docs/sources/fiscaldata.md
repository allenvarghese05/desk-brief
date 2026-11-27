# Treasury upcoming auctions (fiscaldata, `events` table)

Code: `src/deskbrief/sources/fiscaldata.py` · dataset `upcoming_auctions` · table `events` ·
card T65, ruling P-024 ("Today"). A table source (`src/deskbrief/sources/tables.py`): it writes
no observations.

**Live probe: left to the owner.** No live response was fetched or committed. The adapter was
built from Fiscal Data's documented dataset and its API field list (`record_date`,
`security_type`, `security_term`, `reopening`, `cusip`, `offering_amt`, `announcemt_date`,
`auction_date`, `issue_date`). The field names were taken from the API's published metadata and
are unconfirmed until the owner runs the probe. Tests run on a SYNTHETIC FIXTURE
(`tests/fixtures/synthetic/fiscaldata/`). Run before enabling:

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
| `event_id` | `auction:<auction_date>:<cusip>` | `<term>-<type>` when the CUSIP is not yet assigned; `-1`, `-2` ... when two such auctions share a date, term and type |
| `kind` | `auction` | |
| `title` | `"<security_term> <security_type> auction"` | e.g. "4-Week Bill auction" |
| `event_date` | `auction_date` | ET calendar date |
| `scheduled_at` | null | the dataset gives no close time |
| `period`, `url` | null, the dataset page | |
| `details_json` | `security_type`, `security_term`, `reopening`, `cusip`, `offering_amt_usd` (USD, float or null), `announcement_date`, `issue_date`, `record_date` | |

An auction is identified by its slot `(auction_date, security_term, security_type)`. When the
response holds several `record_date` snapshots of a slot, only the newest snapshot is kept, so an
auction first published without a CUSIP and later with one is one event. In that snapshot, one
CUSIP on one date is one auction; a reopening has a new auction date and so is a new event.
Event ids are unique within a fetch. (Whether the API returns more than one `record_date` per
auction is for the owner's probe to confirm.) Rows outside the requested window are dropped.
`source`/`dataset` are `fiscaldata`/`upcoming_auctions`, exactly the `source_log` values of the
fetch (the writer rule `current_rows` relies on).

Only a well-formed answer with no auctions (`{"data": [], ...}`) is logged `empty`, which empties
the scope. A 200 with an empty or malformed body is quarantined and logged `error`, and the
previous run's auctions stay current. A run for a past target date does not fetch auctions.

## Storage and display terms

Fiscal Data publishes U.S. Treasury data, a work of the U.S. federal government, in the public
domain: it may be stored and shown. The site asks for attribution to the Bureau of the Fiscal
Service; the brief names the source ("Treasury, fiscaldata.treasury.gov").

## Freshness

`checks/table_freshness.py` (`freshness_tables`): when the source is enabled, the newest
complete fetch must be from the latest XNYS session whose 07:00 ET has passed (grace 120 min,
then warn, then fail beyond 240 min).
