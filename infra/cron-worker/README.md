# Desk Brief cron Worker (P-018, card T43)

GitHub created scheduled events 5 to 8.5 hours late on 2026-11-18 and dropped most intraday fires
(`docs/investigations/2026-11-18-runs-and-xchecks.md`). This Cloudflare Worker now starts the
scheduled runs. The GitHub `schedule:` crons stay in place as a fallback.

## How it works

- One Cron Trigger, `* * * * *` (`wrangler.toml`). On each tick the Worker checks the schedule
  table in `src/index.js` (UTC, the same crons as the `schedule:` blocks of `morning.yml`,
  `intraday.yml`, `eod.yml` and `sla.yml`).
- For every entry that matches the tick's scheduled minute, it calls
  `POST /repos/allenvarghese05/desk-brief/actions/workflows/<workflow>/dispatches` on `main` with
  two inputs:
  - `fire`: the exact cron string, for example `15 12 * * 1-5`;
  - `slot`: the scheduled minute in UTC, for example `2026-11-19T12:15:00Z`. It comes from the
    tick's `scheduledTime` and the table, not from when the code ran.
- The workflows map `fire` into `GITHUB_EVENT_SCHEDULE` and `slot` into `DESKBRIEF_SLOT`. The
  pipeline treats such a dispatch as a scheduled run (`runs.trigger = schedule`,
  `scheduled_for = slot`, the same gates). The run log line `run.trigger` names the dispatcher
  (`cron-worker` or `github-schedule`).
- When both the Worker and GitHub start the same fire, whichever runs second finds an `ok` or
  `degraded` `runs` row for the same job and `scheduled_for` and skips with `duplicate fire`.
  A late GitHub `*/15` event resolves to a later slot and runs as one more intraday poll. `sla`
  writes no `runs` row, so a second sla run just checks again.
- A non-2xx answer (or a network error) is retried once. Each attempt logs the workflow, fire,
  slot, attempt number and HTTP status. The token is never logged.

The drift test `tests/unit/test_cron_worker.py` fails if the table and the workflows' crons
differ. When you change a cron, change both, then redeploy the Worker.

Local tests (no network): `node --test test/index.test.mjs` from this directory.

## Owner steps

These need the owner's Cloudflare and GitHub accounts, so an agent cannot run them.

### 1. Create the token

Create a fine-grained personal access token in GitHub: Settings → Developer settings →
Fine-grained tokens → Generate new token.

- Resource owner: `allenvarghese05`; Repository access: only `allenvarghese05/desk-brief`.
- Repository permissions: **Actions: Read and write** (Metadata: Read is added automatically).
  Nothing else.
- Expiry: the longest you are comfortable with; put the expiry date in your calendar.

Copy the token once. Do not paste it into a file, a commit, an issue or a chat.

### 2. Deploy the Worker and set the secret

From the repository root:

```bash
cd infra/cron-worker
npx wrangler login
npx wrangler deploy
npx wrangler secret put GITHUB_TOKEN
```

`wrangler secret put` prompts for the value; paste the token there. `wrangler deploy` prints the
`* * * * *` schedule. The Worker has no public route (`workers_dev = false`).

### 3. Check it

```bash
npx wrangler tail desk-brief-cron
```

At the next fire (for example `:00`, `:15`, `:30` or `:45` between 13:00 and 21:45 UTC on a
weekday) a `dispatch.ok` line with `"status":204` should appear, and a new `workflow_dispatch`
run appears in GitHub:

```bash
gh run list --workflow intraday.yml --event workflow_dispatch --limit 5
```

A `dispatch.failed` line with status 401 or 403 means the token is wrong, expired or lacks
Actions: write; 404 means the repository or workflow name is wrong; 422 means the workflow on
`main` does not declare the `fire` and `slot` inputs yet (merge T43 first).

### 4. Record the latency of one ECB-fix run

After deployment, record one ECB-fix run (14:15 Berlin: 12:15 UTC in summer, 13:15 UTC in
winter, on a TARGET business day). The target is a dispatch within 30 s of the slot.

- Slot: the `slot` in the Worker's `dispatch.ok` line.
- Dispatch time: `dispatched_at` in the same line.
- First-fetch time: the earliest `requested_at` in that run's `source_log` rows, or the first
  `fetch` line of the run's log artifact. `gh run view <run-id> --log` also shows when the job
  started.

Write the three times in the T43 PR or `INCIDENTS.md` if the target is missed.

### Rotate the token

1. Create a new token as in step 1.
2. `npx wrangler secret put GITHUB_TOKEN` from `infra/cron-worker`, and paste the new token.
3. Watch `npx wrangler tail desk-brief-cron` for one `dispatch.ok`, then revoke the old token in
   GitHub.

If the token leaks, revoke it in GitHub first (the GitHub `schedule:` crons keep running), then
create and set a new one.

### Remove

`npx wrangler delete desk-brief-cron` stops the dispatches. The GitHub crons keep the pipeline
running, late. To bring it back, run step 2 again.
