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
