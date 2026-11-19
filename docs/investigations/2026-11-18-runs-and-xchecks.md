# 2026-11-18: late scheduled runs, missing intraday runs, cross-checks that never compare

The redesigned dashboard (#71, now on `main`) showed three problems on 18 Nov 2026,
the first weekday with the pipeline crons on `main` (morning.yml landed in #64 and intraday.yml in #65,
on the evening of 17 Nov ET). The evidence below comes from GitHub run history
(`gh run list` / `gh run view --log`) and from reading the code. The R2 store was not read,
because doing so needs the owner's `.env`.

## 1. Scheduled runs start about 8 hours late: GitHub schedule delay, not our arithmetic

GitHub *created* every scheduled run 5 to 8.5 hours after its cron time (`createdAt` is when
GitHub emitted the `schedule` event). Little time was lost after that. The job-level
`started_at` (`gh api .../actions/runs/<id>/jobs`) is 3-5 s after `createdAt`, e.g. morning run
44379989415 was created at 19:23:19Z, its job was created at 19:23:20Z and started at 19:23:23Z.
`schedule_delay_s` = 29,313 s puts the process's `ctx.now_utc` at 19:23:33Z, about 14 s after
`createdAt`. That leaves no room for a runner queue or a wait on our concurrency group.
(Run-level `startedAt` equals `createdAt` for every run, so it proves nothing on its own.)

| workflow | cron (UTC) | fired at (UTC) | `createdAt` (UTC) | late by | outcome |
|---|---|---|---|---|---|
| morning | `15 10 * * 1-5` | 10:15 | 18:50:31 | 8 h 35 m | skipped (05:15 ET is outside the 06:00-07:00 window: correct) |
| morning | `15 11 * * 1-5` | 11:15 | 19:23:19 | 8 h 08 m | ran, ok, email 14:27 ET |
| intraday | `15 12 * * 1-5` | 12:15 | 20:26:52 | 8 h 12 m | ran, ok (see §2) |
| sla | `20 12 * * 1-5` | 12:20 | 20:33:00 | 8 h 13 m | success |
| eod | `30 22 * * 1-5` | 22:30 | 03:43:13 (19 Nov) | 5 h 13 m | success |

`scheduled_for` and `schedule_delay_s` are correct:

- The workflow maps `github.event.schedule` into `GITHUB_EVENT_SCHEDULE`. The logs show
  `15 10 * * 1-5` for run 44374060689 and `15 11 * * 1-5` for run 44379989415, so each run knows
  which cron it belongs to.
- `scheduled_for_from_env` → `last_cron_fire(expr, now)` (`src/deskbrief/pipeline/jobs.py:239`)
  takes the latest fire of *that* cron at or before now: 11:15Z for the morning run.
  At 19:23:33Z, that gives 29,313 s (8 h 08 m 33 s), the dashboard's figure exactly. The intraday figure,
  29,527 s, works out the same way from the `15 12` fire.
- The morning gate is judged on `scheduled_for`, by design: a late runner still produces that
  day's report instead of skipping it. So the 06:15 ET email went out at 14:27 ET.

The cause is on GitHub's side: every one of our scheduled workflows was 5 to 8.5 h late on the
same day, while our own code started within seconds of each run being created. GitHub's
[events reference](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
says: "The `schedule` event can be delayed during periods of high loads". It names "the start
of every hour" as a high-load time, warns that "some queued jobs may be dropped", and
recommends scheduling "at a different time of the hour". The docs say nothing about
quarter-hours or about newly registered schedules. A worse first day is possible (18 Nov was
the first weekday for these crons) but **inferred, not documented**. Whether the delay repeats
is the thing to watch.

Limitation (not a bug): for a `*/15` cron, `last_cron_fire` returns the latest matching
minute, so a delayed intraday run reports a delay measured from the *most recent* quarter-hour
rather than from the fire it really belongs to. GitHub does not say which fire that was.
For intraday, `schedule_delay_s` is therefore a lower bound.

**Proposed fixes (owner decision: these change the schedule or add a service):**

1. Watch 19-20 Nov before changing anything; one bad day may be GitHub's backlog.
2. Move crons off the top of the hour and the round quarters, which GitHub's advice supports,
   e.g. morning `7 10` / `7 11`, intraday `7-59/15 13-21`, sla `23 12`, eod `37 22`. The
   morning window tolerates that shift. The ECB-fix fire is different: its Yahoo tick must land
   within about 1 minute of 14:15 CET (§3), so it cannot move, and even at an off-peak minute
   GitHub gives **no guarantee** of a start within a minute. This fix reduces delays; it cannot
   make the fix-time checks reliable.
3. Trigger the workflows from an external scheduler that calls `workflow_dispatch` (a
   Cloudflare Worker cron fits the existing R2 account). This is the only option here that can
   hit the ECB fix within a minute, so it should be weighed now rather than only "if delays
   persist". It would need the runner to treat such a dispatch as a scheduled run with an
   explicit `scheduled_for` (`GITHUB_EVENT_SCHEDULE` already accepts an ISO timestamp).

## 2. Intraday mostly not running: most fires never became runs (consistent with GitHub dropping them)

On 18 Nov, `intraday.yml` should have fired 36 times (`*/15 13-21`) plus once at 12:15. GitHub
created only three intraday runs in total:

| run | `GITHUB_EVENT_SCHEDULE` | created (UTC) | result | `runs` row? |
|---|---|---|---|---|
| 44391118116 | `15 12 * * 1-5` | 20:26:52 | ok, Yahoo only | yes: the one row on the dashboard |
| 44398065379 | `*/15 13-21 * * 1-5` | 21:06:54 | skipped: "16:07 ET is outside the XNYS session" | no |
| 44445323256 | `*/15 13-21 * * 1-5` | 02:34:36 (19 Nov) | skipped: "21:34 ET is outside the XNYS session" | no |

- **Not cancelled by concurrency.** The group is `deskbrief-pipeline` with
  `cancel-in-progress: false`. With that setting, a newer *pending* run can replace an older
  pending one, but none of the three runs shows `cancelled`, and GitHub created no other
  intraday runs that a pending one could have replaced.
- **Most fires never became runs.** Only 2 runs exist for the 36 `*/15` fires, and both arrived
  after the close. This is inferred from absence: the `gh` API cannot tell a dropped fire from
  one that was never registered. It is consistent with GitHub's documented "some queued jobs may
  be dropped" under load. The intraday gate uses the wall clock for the session check (correct:
  live quotes are only useful while the session is open), so both skipped.
- **Skipped runs write no `runs` row, by design** (`runner.py`, `run_job`: "no runs row: RunStatus
  has no 'skipped'"). The dashboard therefore cannot show them. The step summary and the
  `run.skipped` log line do show them.
- The one `runs` row is the **ECB-fix trigger** (`15 12`), not a `*/15` poll. It arrived at 15:27
  ET, inside the session, so `evaluate_gate` returned the in-session branch (all sources), but
  the workflow still passed `--sources yahoo` for that cron. It fetched Yahoo only, with no Alpaca IEX
  or Twelve Data quote.

**Proposed fixes:**

- The same as §1 (off-peak minutes, or an external trigger).
- Optional, owner decision because it changes provider usage: in `intraday.yml`, pass
  `--sources yahoo` only when the gate would choose the ECB-fix branch. Simplest: drop the
  flag and rely on the gate, which already restricts the out-of-session fix run to Yahoo.
  Behaviour change to note: a `15 12` run that GitHub delays into the session (like 18 Nov's)
  would then fetch **all** sources, as an ordinary intraday poll does. That is probably what we
  want, but it is the provider-usage change this item is flagged for.
- Optional: record skipped scheduled runs somewhere the dashboard can see them, e.g. a
  `skipped` status or a small `schedule_events` table. Without that, "did not fire" and "fired
  and was gated" look the same on the Runs page.

## 3. Cross-checks: 23 of 32 never compared anything

The dashboard keys cross-checks by `(group_id, check_name)` (`app/pages/3_Cross_checks.py` on
`main`, from #71). The current config produces 35 result rows per run. `fx_vs_ecb` emits a
Twelve Data row and a Yahoo row per pair under one key, so the 35 rows collapse to 32 keys. The 9
that compare are `iex_vs_sip` ×5 and `official_pair` ×4. To get the skip reasons below, I ran
every registered check against an empty store with the real `config.yaml`. They are the
`details_json.reason` strings the store holds when that check's data is missing. Which data is
missing comes from the feed config and the run history above.

| check (keys) | skip reason in `details_json` | missing data | verdict |
|---|---|---|---|
| `close_vs_official` ×5 (sp500, nasdaq, smallcap, gold, ust_long) | `missing side: <ETF>/alpaca/sip_bars_1m, <ETF>/stooq/daily` (both sides listed when both are absent) | `stooq/daily` is the only official close. Stooq was disabled on 18 Nov because every automated request was already getting a JavaScript browser-verification page instead of the CSV (`config.yaml`, `docs/sources/stooq.md`), so this would skip even with Stooq enabled | **Permanent skip until there is another source.** Not a code bug, but the check is dead. Disabling Stooq did not cause it. |
| `proxy_vs_exact` ×7 (sp500, oil_wti, oil_brent, gold, ust_2y, ust_10y, ust_long) | `missing side (need both sessions from one source): <ETF>/close, ...` | the proxy ETF leg needs an `interval=1d` close, and for SPY, USO, BNO, GLD, SHY, IEF and TLT the only `1d` feed is `stooq/daily` (blocked by its verification page, as above) | **Permanent skip until there is another source.** The exact legs (Yahoo history, FRED/Treasury yields) are fine. |
| `yahoo_vs_primary` equities ×5 (sp500, nasdaq, smallcap, gold, ust_long) | `missing side: no yahoo quote tick on <ref>` (or `... inside the regular session ...`) | Yahoo `quote` ticks come only from in-session intraday runs (§2) | **Expected.** Waits on intraday. 18 Nov has one in-session Yahoo run (15:27 ET), so this should compare from the 19 Nov morning run, provided the 18 Nov eod run (success) wrote that day's SIP bars. This is not confirmed from the store. Unlike the FX rows, any in-session tick works here, so it is less exposed to delay. |
| `yahoo_vs_primary` FX ×3 (eurusd, gbpusd, usdjpy) | `missing side: no twelvedata/fx_series_1m bars on <ref>`, later `no Yahoo tick within ±1m of the ECB-fix bars` | `twelvedata/fx_series_1m` is planned **only by `eod`**, which had never run before 03:43Z on 19 Nov; it also needs a Yahoo tick within ±1 minute of those bars, i.e. of the fix | **At risk: a likely bug under schedule delay** (see below). It skips on every day the fix-time run is late, even if eod succeeds. |
| `fx_vs_ecb` ×3 (eurusd, gbpusd, usdjpy) | Twelve Data row: `missing side: <PAIR>/twelvedata/fx_series_1m`. Yahoo row: `no Yahoo ticks around the fix (<PAIR>)` | the same two inputs; the ECB rate side is present (the ECB source works) | **Twelve Data row: expected.** It should compare from the 19 Nov morning run, now that the 18 Nov eod has succeeded. **Yahoo row: at risk, the same as `yahoo_vs_primary` FX** (see below). Because the key merges both rows, the dashboard will show the pair as "compared" even while the Yahoo row keeps skipping. |

**The Yahoo FX rows depend on a run within a minute of the ECB fix, which §1 shows GitHub does
not deliver.** That covers the 3 `yahoo_vs_primary` FX keys and the Yahoo half of each
`fx_vs_ecb` key: 6 rows. Both need a Yahoo tick within about 1 minute of 14:15 CET. A Yahoo
tick's `as_of` is the quote's `regularMarketTime` (`sources/yahoo.py`, P-006), so a quote cannot
be backfilled: it is always the price at the moment of the fetch. A fix-time run that GitHub
starts hours late still passes the gate, because the ECB-fix window is judged on
`scheduled_for`, not the wall clock. It then fetches a quote from the actual run time. 18 Nov
shows this: the `15 12` run started at 20:27Z and stored a late-afternoon quote, nowhere near
13:15Z. (That cron is also the *summer* fix time. In winter the fix at 13:15Z falls to the
`*/15` 13:15 fire, which never became a run that day.) Off-peak cron minutes cannot fix this,
because GitHub gives no start-time guarantee (§1 fix 2). The options are an external scheduler
(§1 fix 3), or a wider tolerance or another way to get the fix-time Yahoo price, for example a
Yahoo 1m history bar that *can* be fetched later. Both are owner decisions.

The verdicts above are inferred from config and run history; they were not read from the
store. To confirm them, the owner can run this on the 7-day window:

```sql
SELECT check_name, group_id, left_source,
       json_extract_string(details_json, '$.reason') AS reason, count(*) AS n
FROM xchecks WHERE status = 'skip' AND target_date >= current_date - 7
GROUP BY ALL ORDER BY check_name, group_id;
```

### Gaps that are design limits rather than missing runs

- **Catch-up cannot recover Twelve Data bars.** Catch-up runs as `JobKind.BACKFILL`, and the Twelve
  Data source plans nothing for backfill (`sources/twelvedata.py` docstring). A day whose eod
  run is dropped never gets `fx_series_1m`, so `fx_vs_ecb` and `yahoo_vs_primary` FX skip for
  that day for good. Alpaca SIP bars *are* backfilled, which is why `iex_vs_sip` works.
- **No usable daily-close source for the ETFs.** Stooq is the only one, and it is blocked. That
  limit removes 12 of the 23 keys.
- **Fix-time quotes cannot be backfilled.** See the Yahoo FX paragraph above (6 rows).

**Proposed fixes (owner decision: provider usage or config):**

1. Give the ETFs a second daily close: either a `yahoo/history` feed for SPY, QQQ, IWM, GLD, TLT, SHY,
   IEF, USO and BNO (the Yahoo source already serves `history` for the futures), or derive the close from the
   Alpaca SIP 1m bar ending at the session close. Either one revives `proxy_vs_exact`.
   `close_vs_official` needs a source independent of Alpaca, so Yahoo history fits it
   better. Until one exists, consider disabling the 5 `close_vs_official` keys, so that "never compared"
   means something.
2. Let `backfill` plan `twelvedata/fx_series_1m` for missed TARGET days. That costs one batch call
   per day against the 750/day budget.
3. Make the skip reason say *why* a side is missing when its only feed belongs to a disabled
   source (e.g. "stooq is disabled") instead of the generic "missing side". This is small and
   safe, and was left out of this investigation's PR to keep it doc-only.

## What landed

Only this report. Every fix above changes schedules, provider usage or check semantics, so all
of them are left for the owner to decide.
