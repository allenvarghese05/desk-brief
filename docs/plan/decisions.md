# Planner decisions

Rulings that change the spec or plan. Each has an id, a date, the decision, the reason and **who ratified it**. PRs that edit planner-owned docs must cite a ratified ruling here, except card-level edits covered by P-010. Agents may *propose* rulings; only the planner role, or the project owner when the planner role is held by the same agent as the implementer, may ratify them.

> **Ratification record:** P-001 to P-007 were proposed by the orchestrator agent while implementing T01 and escalated after T01 review round 3 (workflow §4). The project owner, Allen Varghese, chose "Ratify all six" (P-001…P-006) when asked in the Claude Code session on 2026-11-15, and then explicitly instructed the orchestrator to write this record on his behalf. P-007 documents the workflow-doc edit that goes with that decision.

## P-001 · 2026-11-15 · Unit name `fx_rate` replaces `ratio`
- Decision: FX observations use unit `fx_rate` (quote currency per unit of base). Spec §4 and §7.2 updated.
- Why: `ratio` is ambiguous (could be a spread ratio or a hedge ratio); `fx_rate` says what it is. Raised in T01 review round 1.

## P-002 · 2026-11-15 · Morning headline uses `ES_F`, not `SPY`
- Decision: `report.headline_instruments` = ES_F, UST_10Y, EURUSD, WTI, VIX. Spec §4 updated.
- Why: the brief is written at 07:00 ET, before SPY trades; S&P futures trade overnight, so they carry the overnight move. SPY stays in the equities table.

## P-003 · 2026-11-15 · Alpaca `daily_bars` dataset dropped
- Decision: T14 implements `iex_latest`, `iex_bars_1m`, `sip_bars_1m` only. Spec §5.2 updated.
- Why: daily closes come from Stooq (T3) and Yahoo (T2); a third daily source adds requests without a new cross-check.

## P-004 · 2026-11-15 · Source cards may edit their own feeds in `config.yaml`
- Decision: `config.yaml` values are not contracts. A source card may add/adjust feeds and dataset schedules **for its own source only**, disclosed under "Files outside Owns". Anything else in config goes through the planner.
- Why: parallel source cards otherwise all block on a config PR. Model shapes in `config.py` remain contracts.

## P-005 · 2026-11-15 · `revisions` table carries `dataset`
- Decision: `revisions` includes `dataset` (part of the observation natural key). Spec §7.2 updated.
- Why: without it two datasets for the same instrument (e.g. Yahoo quote vs history) would be conflated.

## P-006 · 2026-11-15 · Partition dates and `as_of` semantics
- Decision, partition `date=` per table: raw = UTC date of `ingested_at`; observations = ET date of `as_of`; quarantine and revisions = ET date of `detected_at`; source_log = ET date of `requested_at`; dq_results, crosscheck_results, alerts, runs = the run's `target_date`; scorecard_daily = the ET date scored.
- Decision, `as_of`: `tick` = trade/quote instant; `1m` = bar start; `1d` = 00:00 America/New_York on the reference date; `1mo` = 00:00 America/New_York on the first of the reference month. So `et_date(as_of)` is always the reference date.
- Implemented in `src/deskbrief/store/backend.py` (docstring + `raw_key`) and spec §6.
- Why: catch-up, rebuild, `daily_close` and `obs_pit` all depend on one shared definition.

## P-007 · 2026-11-15 · Planner rulings live in `docs/plan/decisions.md`
- Decision: the Planner row in `docs/process/agent-workflow.md` §1 points to this log; a ruling needs ratification as described at the top of this file.
- Why: T01 review round 3 showed a planner-owned change needs an auditable approver, not just a rationale.

## P-008 · 2026-11-15 · One append per (table, ET date) per run
- Decision: keep the storage key layout (`run={run_id}.parquet`). Within one run, each table/date partition is written exactly once; the runner (T19) buffers rows per run, splits observations by `et_date(as_of)`, and flushes once per (table, date). A second append raises `KeyExistsError` by design.
- If incremental per-source flushes are ever needed, add an optional `part` to `table_key` (`run={id}-p{n}.parquet`) via a `contract:` PR; existing globs already match it.
- Why: surfaced in T02 review; keeps files few (small-files goal) and makes double-writes loud.
- Status: **ratified by Allen Varghese (project owner)**: he answered "Ratify both" when asked in the Claude Code session on 2026-11-15 (ET); the orchestrator wrote this record at his instruction. Spec §2.4/§5.5/§7.3 and plan cards were updated to implement it (PR #6 review round 1).

## P-009 · 2026-11-15 · `daily_close` field semantics and display labels
- Decision: `daily_close` takes fields `close`, `yield` and `rate` as the day's value. Every consumer (report T30–T33, dashboard T34–T35) must label by `field`/`unit` (e.g. "ECB reference rate", "yield %"), never call an ECB fix or a Treasury yield a "close". `daily_close` is latest-version (not point-in-time); report/check code needing reproducibility uses `obs_pit` and excludes the current, unfinished ET date.
- Why: surfaced in T02 review.
- Status: **ratified by Allen Varghese (project owner)**: he answered "Ratify both" when asked in the Claude Code session on 2026-11-15 (ET); the orchestrator wrote this record at his instruction. Spec §2.4/§5.5/§7.3 and plan cards were updated to implement it (PR #6 review round 1).

## P-010 · 2026-11-15 · Which planner edits need a ruling
- Decision: changes to the **spec, contract files, process docs, the plan's Contracts list, the "Rules that apply to every card", phase gates G0–G4, and approval/merge authority** always need a ratified ruling. **Card-level edits** (adding, splitting or refining a card's deliverables or acceptance criteria) do not, provided that (a) each change appends a line to that card's `Changelog` citing its source (spec section, ruling id, or a review comment URL), and (b) the edit does not widen a card's Owns to include a contract file or another card's files.
- Why: found in PR #6 review rounds 1–2. Requiring a ruling for every acceptance-criteria tweak would block the plan, but the exemption must be bounded and traceable.
- Status: **ratified by Allen Varghese (project owner)**: he answered "Ratify P-010" in the Claude Code session on 2026-11-15 (ET); the orchestrator wrote this record at his instruction.

## P-011 · 2026-11-16 · Data checks follow each calendar's early close
- Decision:
  - On an early-close day of a dataset's calendar, the checks expect the shorter session. Minute-bar completeness counts only the minutes up to the early close.
  - For `SIFMA_US` datasets (Treasury, FRED), when the data day is a SIFMA recommended early close (for example Good Friday, when SIFMA recommends one), a missing or late value is capped at `warn` instead of `fail`, because those publishers may skip such a day.
  - Spec §8.1 is updated. This ratifies the behaviour T20 already implements and tests (`test_sifma_early_close_missing_value_is_warn_not_fail`).
- Why: without it, the checks expect data that never arrives on early-close days and raise false failures. Raised in the [PR #20 review, finding 3](https://github.com/allenvarghese05/desk-brief/pull/20#pullrequestreview-5914719908).
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-16, in two answers:
  - Asked whether the checks should treat SIFMA early-close days as ending early, he answered "yeah it should because doesnt the market close ealier on holdidays and stuff?".
  - Asked whether a Treasury or FRED value missing on an early-close day should be a warning instead of a failure, he chose "Yes, warning (Recommended)".
- The orchestrator wrote this record at his instruction.

## P-012 · 2026-11-16 · FX fetch at the ECB fix time every weekday
- Decision:
  - The FX leg of `yahoo_vs_primary` gets a Yahoo FX quote at the ECB fix (14:15 Europe/Berlin) every weekday.
  - Summer (CEST): `intraday` gets one extra cron, `15 12 * * 1-5` (12:15 UTC).
  - Winter (CET): the existing 13:15 UTC `intraday` trigger is used, so there are no extra winter runs.
  - At the fix time, these runs may fetch FX prices before the NYSE session opens. Without that, the intraday gate stops every run outside the session, and 13:15 UTC is before the open in both seasons.
  - Spec §5.4 and §5.3 are updated. T24B builds it; the details are in that card.
- Why: without this the leg skips every day of the year. Raised for summer in [PR #18 round 2, confirmation list finding 1](https://github.com/allenvarghese05/desk-brief/pull/18#pullrequestreview-5914906053). The winter half was found in [PR #23 round 1, finding 1](https://github.com/allenvarghese05/desk-brief/pull/23#pullrequestreview-5926433510).
- Known limit: GitHub cron can start late. On a day it starts after the fix window, the leg still skips, with its specific reason.
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-16, in two answers:
  - Asked whether to add one extra short run every weekday at about 12:15 UTC, he chose "Add the run (Recommended)".
  - Told that the winter 13:15 UTC run also stops before the stock market opens, and asked whether to keep the 12:15 UTC summer run and let the existing 13:15 UTC winter run fetch just the FX prices before the open (no extra winter runs), he chose "Yes, cover both (Recommended)".
- The orchestrator wrote this record at his instruction.

## P-013 · 2026-11-16 · Morning schedule uses one cron per fire time
- Decision: `morning` uses two cron entries, `15 10 * * 1-5` and `15 11 * * 1-5`, instead of `15 10,11 * * 1-5`. `github.event.schedule` then names the exact fire, and the morning gate (T19, `scheduled_for`) uses it, so a delayed run still maps to its own fire and proceeds. Spec §5.4 is updated; T24A builds it. `sla.yml` (T24B) still catches a missing report for any cause, such as a run that never starts.
- Why: with one combined expression, the gate cannot tell which fire a delayed run belongs to. Raised in [PR #16 round 2, finding 3](https://github.com/allenvarghese05/desk-brief/pull/16#pullrequestreview-5914845782) and the [T19 author's note on round-1 finding 4](https://github.com/allenvarghese05/desk-brief/pull/16#issuecomment-6906415315).
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-16. Asked whether to split the morning schedule into one cron per run time, he answered "yes that fine, morning cron work". The orchestrator wrote this record at his instruction.

## P-014 · 2026-11-16 · One agent-log file per card
- Decision: each card's agent log goes in `docs/agent-log/T{id}.md`, using the entry format in workflow §9. `docs/agent-log.md` keeps the T00–T05 entries as history and points to the per-card files. Workflow §8.2 and §9, the plan's definition of done and the spec §14 repository layout are updated.
- Why: parallel agents appending to one shared file collide. Every card since T10 already used per-card files. Raised in the [PR #11 review, finding 7](https://github.com/allenvarghese05/desk-brief/pull/11#pullrequestreview-5914386201) and the [PR #14 review, finding 3](https://github.com/allenvarghese05/desk-brief/pull/14#pullrequestreview-5914397821).
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-16. He chose "One file per card (Recommended)" when asked which agent-log rule to use. The orchestrator wrote this record at his instruction.

## P-015 · 2026-11-16 · Excel column headers are part of the §9.2 contract
- Decision:
  - The column headers of the eight Excel tables, as T32 built them (`src/deskbrief/report/excel.py`, pinned by `tests/contract/test_excel_tables.py`), are recorded in spec §9.2. They are part of the Python–VBA contract.
  - Before T33 starts, the five market tables (`tbl_rates`, `tbl_fx`, `tbl_commod`, `tbl_equity`, `tbl_global`) gain two columns, appended at the end so existing positions stay put:
    - `dq_flag`: the instrument's worst check or cross-check result on the report date, so `ShowFlaggedOnly` can filter market tables.
    - `as_of_utc`: a UTC timestamp next to the display text in `as_of`.
    - The owner approved the two columns. Their names and exact values are the planner's choice, set in card T32B.
  - Changing any header after this needs a `contract:` PR. The plan's Contracts list names Excel column headers alongside table names.
  - Card T32B builds the two columns. It is the one header change this ruling approves, so it needs no separate `contract:` PR.
- Why: T33's macros depend on exact headers. §9.2 named the tables but not their columns, and adding columns after the macros exist would break them. Raised in the [PR #33 review, findings 1 and 4](https://github.com/allenvarghese05/desk-brief/pull/33#pullrequestreview-5932128409).
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-16, in two answers:
  - Asked whether to record T32's headers in the spec as the official contract, he chose "Yes, record them (Recommended)".
  - Asked whether to add a status column for "Show flagged only" and a real timestamp column to the market tables before T33, he chose "Yes, add both (Recommended)".
- The orchestrator wrote this record at his instruction.

## P-016 · 2026-11-17 · `empty` marks the Excel placeholder row
- Decision: a table with nothing to list keeps one placeholder row. Its first cell reads `none: nothing to list` and the other cells are blank, except that in every table with a `status` column the placeholder's `status` is `empty`. That covers the five market tables, `tbl_checks` and `tbl_sources`. Macros (T33) skip rows whose `status` is `empty`. `tbl_lineage` has no `status` column, so macros recognise its placeholder by the first cell. Spec §9.2 is updated. T32B already builds this (`PLACEHOLDER_LABEL`, `PLACEHOLDER_STATUS` in `src/deskbrief/report/excel.py`), so no code changes.
- Why: T32B put `empty` into `tbl_checks.status` and `tbl_sources.status`, which have their own status values, and spec §9.2 did not mention it. One skip rule is simpler for the macros than one per table. Raised in the [PR #47 review, finding 4](https://github.com/allenvarghese05/desk-brief/pull/47#pullrequestreview-5933580186).
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-17. Asked whether the spec should say that `empty` marks the placeholder row in any table with a status column, so the macros need one rule, he chose "Yes, one rule (Recommended)". The orchestrator wrote this record at his instruction.

## P-017 · 2026-11-17 · Every job rescores; eod compacts the last 7 days
- Decision: spec §5.4 matches the code merged in T23 and T23B:
  - Every job ends with the serving step. It rescores `scorecard_daily` for the job's target date (an overwrite of that date's file, allowed for a `DAILY_TABLES` table under P-008), then writes the serving files.
  - The serving files (`health.json` and the 14-day, 30-day and 7-day windows) are anchored on the current ET date, not the job's target date. A backfill of an old date never replaces the latest view.
  - The scorecard counts every intraday poll. For other jobs it counts the latest run of each job per source, and a partial re-run replaces only the sources it fetched.
  - In `eod` only, compaction runs between the scorecard and the serving files. It covers `observations` and `quarantine` for today's ET date and the six before it, and skips partitions with fewer than two files. Older partitions are compacted only by a manual call.
- Why: the planner made these rulings during the T23 and T23B reviews, and the code follows them. Spec §5.4 still said the scorecard runs only at eod and compaction covers one day. Rulings raised in the [PR #45 round-1 review, findings 1–3](https://github.com/allenvarghese05/desk-brief/pull/45#pullrequestreview-5933428000), the spec gap in the [PR #45 round-2 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/45#pullrequestreview-5933525550), and the compaction window in the [PR #48 round-1 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/48#pullrequestreview-5933652099).
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-17. Asked whether to update the spec so it matches the code approved in #45 and #48, he chose "Match the code (Recommended)". The orchestrator wrote this record at his instruction.
