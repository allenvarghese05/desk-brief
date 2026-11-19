# Desk Brief runbook

Operational procedures. Keep secrets out of this file: names only, never values.

## 1. Morning workflow (`.github/workflows/morning.yml`)

- Two crons, `15 10 * * 1-5` and `15 11 * * 1-5` UTC (P-013). The code gate proceeds only for the fire landing 06:00-07:00 ET; the other one exits as `skipped` and does not ping the healthcheck.
- Re-run by hand: Actions → morning → Run workflow. A manual run has no `GITHUB_EVENT_SCHEDULE`, so the gate treats it as a manual trigger.
- The step summary is the run manifest (counts only). The `morning-logs-<run id>` artifact holds the redacted JSON log and the same manifest; it is kept 7 days and contains no provider data beyond counts.
- Healthcheck: success pings `HEALTHCHECK_URL_MORNING`, a failed job pings `.../fail`. No ping at all means the job did not run: the dead-man's switch alerts.
- The workflow sets `DESKBRIEF_STORAGE_BACKEND=r2` itself and maps the `R2_BUCKET` secret to `DESKBRIEF_STORAGE_BUCKET`, the name `config.py` reads.

## 2. Rolling back `clean/_CURRENT` by hand

Use when a rebuilt clean version (`clean/v{N+1}`) is bad and readers must go back to `v{N}`.

1. Find the current and previous versions: read `clean/_CURRENT`, and list `clean/`.
2. Point the pointer back: write `v{N}` (the previous version, plain text, no newline needed) to `clean/_CURRENT`.
3. **Delete the flip marker `clean/_flips/from-v{N}`.** The rebuild claimed it when it flipped away from `v{N}`. While it exists, any new rebuild from `v{N}` aborts with: `another rebuild already flipped away from 'v{N}': marker clean/_flips/from-v{N} exists. After rolling back _CURRENT by hand, delete clean/_flips/from-v{N} to rebuild from v{N} again`.
4. Before deleting, open the marker. Its JSON body has `flipped_at`, `new_version` and `run_id`. If you did **not** roll back by hand, a marker can also be left by a rebuild killed between claiming and writing the pointer; check `flipped_at` first so you do not delete a live claim.
5. Leave the abandoned `clean/v{N+1}/` in place. Version numbers only go up: the next rebuild uses `v{N+2}`, because `next_clean_version` takes the highest version directory present.
6. Run `deskbrief run rebuild` (workflow_dispatch of the rebuild job) when ready.

Do this with the admin credentials (Cloudflare dashboard or an S3 client). The CI writer token can also do it; the dashboard reader cannot.

## 3. SMTP secrets (the email step, T31)

| Secret | Meaning |
|---|---|
| `SMTP_USER` | Gmail address that sends the report |
| `SMTP_APP_PASSWORD` | 16-character Google app password (needs 2-Step Verification), no spaces |
| `REPORT_RECIPIENTS` | comma-separated recipient addresses |
| `SMTP_HOST`, `SMTP_PORT` | optional; empty means `smtp.gmail.com` and `465` (implicit TLS). Any other port uses STARTTLS |

- Rotate the app password: Google Account → Security → App passwords → delete `deskbrief`, create a new one, then `gh secret set SMTP_APP_PASSWORD -R allenvarghese05/desk-brief`.
- Until T31 lands the report/email step is a no-op, so a wrong SMTP secret shows up only after that merge. After changing a secret, dispatch the morning workflow once and check the step summary.
- Setup steps: `docs/setup/keys-and-accounts.md`, section 6.

## 4. Scheduled triggers (Cloudflare Worker, P-018)

- Scheduled runs are started by the cron Worker in `infra/cron-worker/`. It calls `workflow_dispatch` for `morning`, `intraday`, `eod` and `sla` at the minutes in its UTC schedule table, passing `fire` (the cron string) and `slot` (the scheduled minute). The workflows' own `schedule:` crons stay as a fallback.
- A dispatch with `fire` and `slot` is a scheduled run (`trigger=schedule`, gated on `slot`). A dispatch you start by hand from the Actions tab has neither, so it stays `manual` and ungated, as before.
- Whichever of the Worker's run and GitHub's fallback arrives second for a once-a-day fire finds the first one's `runs` row and exits as `skipped` with reason `duplicate fire`. That is expected, not an incident. A failed first run does not count, so the fallback can repair it.
- Deploy, token, rotation and latency check: `infra/cron-worker/README.md`. The token is a Worker secret; never paste it anywhere else.
- If the Worker is down, GitHub's crons still fire, possibly hours late. The ECB-fix FX leg then skips with its reason (P-012 known limit) and the morning SLA watchdog still catches a missing report.

## 5. History load (`.github/workflows/history.yml`, P-020)

A one-time load of two years of daily and weekly history, so the dashboard has real 1Y/YTD horizons. It writes to production R2, so only the owner dispatches it.

**What it loads.** Every source with range support, through its `plan_range`: FRED (14 series, one request each), Treasury (one request per month, 25), ECB (one request), Yahoo `history` (one request, one yfinance call per symbol, 22), Cboe (one file per index, 7) and CFTC (one request). Not loaded, and listed in the plan: Alpaca and Twelve Data (no range support, including `fx_series_1m`), Yahoo `quote`, and Stooq (disabled).

**Cost** (default range, from the dry run in PR for T53):

| | |
|---|---|
| Requests | 49 (70 provider calls), all keyless except FRED's 14 |
| Daily budgets | none of these sources has one; Twelve Data, the only budgeted source, is not used |
| Pacing | the configured per-source rate limits; the slowest are Treasury (30/min, about 50 s), Yahoo (30/min, about 45 s) and Cboe (10/min, about 40 s) |
| Rows | about 25,000 observations |
| Files | about 520 `observations` files (one per ET date), one `source_log` file, raw files, one `runs` row |
| Time | about 5–10 minutes; the job's timeout is 60 |

**Dispatch.**

1. Dry run first: Actions → history → Run workflow, tick `dry_run`, leave the dates empty. The step summary prints the plan: sources, requests, date spans, estimated rows and partitions, and what is not loaded. Nothing is fetched or written.
2. Real run: Run workflow again with `dry_run` unticked. Empty `start` and `end` mean the two years up to yesterday (ET). `sources` narrows it, e.g. `fred,treasury`.

From a terminal: `gh workflow run history.yml -R allenvarghese05/desk-brief -f dry_run=true`, then the same without `-f dry_run=true`.

**Check it.**

- The step summary ends with the run manifest: per source, fetches, failures, rows; "Observations written"; and a note with the rows skipped as already stored or outside the range. Status `ok` or `degraded` exits 0; `failed` (nothing succeeded, or storage unavailable) exits 1.
- The log has one `history.request` and one `history.request_done` line per request and a `history.source_done` line per source.
- In the dashboard, ETF, stock, curve and volatility charts should now reach back two years. In DuckDB: `SELECT source, min(as_of), max(as_of), count(*) FROM obs_latest GROUP BY 1`.
- It runs no checks, scorecard or serving. The next scheduled run's checks and serving pick the history up.

**Re-run after an interruption.** Dispatch it again with the same inputs. Keys already stored are skipped, so it writes only what is missing and never writes a row twice. A source that failed (a Yahoo rate limit, say) can be re-run alone with `sources=yahoo`.

**Concurrency.** Its group is `deskbrief-history`, not `deskbrief-pipeline`, so it neither waits for nor cancels scheduled runs. Two history dispatches queue one behind the other.
