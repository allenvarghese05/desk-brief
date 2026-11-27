# SEC EDGAR filings, Form 4 and 13F (`filings` table)

Code: `src/deskbrief/sources/edgar.py` · datasets `company` and `holders` · table `filings` ·
card T65 (absorbs T51), ruling P-024 (Company section). A table source
(`src/deskbrief/sources/tables.py`): it writes no observations.

**Live probe: left to the owner.** The contact User-Agent is a secret the implementer could not
read, and live SEC calls were out of scope, so nothing was fetched. The adapter follows SEC's
documented formats (submissions JSON, Section 16 ownership XML, 13F information table and cover
page) and is tested on INVENTED documents for the fictional "Example Corp (EXMP)"
(`tests/fixtures/synthetic/edgar/`). Before enabling:

1. Add the repository secret `DESKBRIEF_SEC_USER_AGENT` (a contact such as `Name email@host`,
   per SEC's fair-access policy). `morning.yml` passes it to the step that runs the job only.
2. Run the probe locally with the variable set (it is never printed):

   ```bash
   DESKBRIEF_SEC_USER_AGENT="Your Name you@example.com" PYTHONPATH=src \
     .venv/bin/python scripts/probes/probe_t65_edgar.py AAPL <issuer CIK> [<CUSIP> <13F filer CIK> ...]
   ```

   It prints, per fetch, the HTTP status, fetch status, row count and newest filed date.
3. Add `filings.companies` (and optionally `filings.holders`) to `config.yaml`, set
   `edgar: enabled: true`, and record the probe date and counts here. (The T64 contract tests
   pin `filings` to its defaults in `config.yaml`, so that change also updates
   `tests/contract/test_t64_events_filings_contract.py::test_filings_settings`.)

## Requests

All under the source's rate limiter (`rate_per_min: 300` = 5/s, half SEC's 10/s; config refuses
more than 600). Planned in the `morning` job only, and fetched only when the run's target date
is today (ET): a manual run for a past date leaves the stored filings alone. User-Agent:
`secret_headers` only.

| Dataset | One fetch per | HTTP requests inside the fetch |
|---|---|---|
| `company` | `filings.companies` entry | `data.sec.gov/submissions/CIK##########.json`, then each Form 4 / 4/A XML filed in the last `filings.insider_days` (`www.sec.gov/Archives/edgar/data/<cik>/<accession>/<doc>.xml`, the raw XML behind the `xslF345...` view) |
| `holders` | `filings.holders` entry (only when a company has a `cusip`) | the holder's submissions, then for its latest 13F period and the one before: each 13F-HR / 13F-HR/A `index.json`, its information table, and for an amendment the cover page `primary_doc.xml` |

A fetch is all or nothing: if a request inside it fails, the fetch is logged with that
request's status and error and writes no rows. One exception: a 404 on a document of one filing
(a Form 4 XML, a 13F `index.json`, information table or cover page) skips that filing instead
(logged `edgar.document_skipped`, listed under `skipped` in the bundle), so one missing
document does not hold back a company every day. Each accession is still written whole or not
at all: a skipped Form 4 gets no rows, and a holder with a skipped filing in its latest or
previous 13F period gets no rows that run. A Form 4/A whose possible original was skipped that
run is skipped too (logged `edgar.amendment_skipped`). A possible original is any Form 4 filed no
later than the amendment, on its `dateOfOriginalSubmission` or with the same `reportDate` (the
Form 4's period): the same rule that matches an original, without the owner, who is named in the
missing document. Written now, the amendment could match another candidate or none, the stored row
naming the right one would be replaced, and the reader would count both. The reader also keeps
only the latest of several unmatched amendments by one owner with the same candidates, and the
Company section's "check" flag counts every one of them. A 404 on the submissions JSON,
and any 5xx or 429,
still fail the whole fetch. The body stored in `raw/` is a JSON bundle of
every document read (`bundle: edgar-bundle/1`), parsed purely by `parse_table`. 13F information
tables are kept only for the configured CUSIPs, with the full document's byte size and sha256
alongside (large filers' tables are tens of MB).

**Missing User-Agent:** no request is made; the fetch is an error naming the variable (never its
value), the run continues degraded, and the freshness check flags the datasets.

## Rows (`filings`)

| Row | seq | Fields |
|---|---|---|
| latest 10-K, latest 10-Q | 0 | form, filed_date, period (`reportDate`), url (primary document) |
| each Form 4 / 4/A | 0 | form, filed_date, period (`periodOfReport`), url (filing index), party (reporting owner), party_cik, party_role ("Director", "Officer: <title>", "10% Owner", "Other: <text>") |
| its non-derivative transactions | 1.. | trade_date, trade_code, side (P buy, S sell, else other), shares, price (USD/share), value_usd (= shares × price; null without a price), plan_10b5_1, details (security title, A/D code, D/I ownership, shares after) |
| a holder's 13F for the latest period | 0 | form, filed_date, period (quarter end), url, party (configured holder name), party_cik |
| its position in the company | 1 | shares (at period end), shares_change (minus the previous period; 0 shares if absent then), value_usd (13F market value, USD), details (CUSIP, class, previous period and shares) |

Only share positions count (`sshPrnamtType = SH`, no put/call). 13F values are whole dollars
(the format since 2023). A company the holder held in neither quarter gets no rows.

**Amendments** (`amends_accession`, seq 0 only, only for a full restatement):

- Form 4/A is a complete amended form: it names the Form 4 of the same reporting owner filed on
  its `dateOfOriginalSubmission`, when exactly one such filing is in the fetch; failing that,
  the one Form 4 of that owner filed no later than the amendment, on that date or with the same
  `periodOfReport`. When that is still not exactly one, `amends_accession` stays null and the
  seq 0 `details_json` has `amendment_unmatched: true` and `amends_candidates`; the reader drops
  those candidates, so only the amendment is counted, never both.
- 13F-HR/A with cover `amendmentType = RESTATEMENT` carries the period and names the period's
  original 13F-HR. A `NEW HOLDINGS` amendment adds positions, amends nothing: its positions are
  added to the period's holdings (on the original's accession) and it gets a filing row only
  (`details_json.merged_into`).
- 10-K/A and 10-Q/A are not fetched (often partial); the latest original is shown.
- Readers drop an amended filing (`schemas.current_rows`) and keep only the latest amendment of
  each original (`query.latest_amendments_only`), so two amendments never double-count.

**Holders in the Company section** (`query.company_filings`): each holder is shown at its own
latest 13F period, so during the 45-day filing window a holder that has not filed the new
quarter yet keeps its previous one (`HolderChange.period`, shown "as of" per holder when the
periods differ). A holder whose latest period has missed two later filing deadlines
(`HOLDER_STALE_QUARTERS = 2`, deadline = quarter end + 45 days) is left out as stale.

**10b5-1 flag:** true when a transaction's footnotes mention Rule 10b5-1, or when the form's
10b5-1 checkbox (`aff10b5One`, on forms since 2023) is ticked and the transaction is an
open-market trade (code P or S).

## Storage and display terms

EDGAR filings are public records published by the U.S. Securities and Exchange Commission; SEC
content is in the public domain and may be stored and shown. Automated access must follow SEC's
fair-access policy: a declared User-Agent with contact details and at most 10 requests per
second (https://www.sec.gov/os/accessing-edgar-data). The brief links each filing to EDGAR.

## Freshness

`checks/table_freshness.py` (`freshness_tables`) covers `edgar/company` and `edgar/holders` when
the source is enabled and configured: the newest run in which every fetch of the dataset was
complete must be from the latest XNYS session whose 07:00 ET has passed.
