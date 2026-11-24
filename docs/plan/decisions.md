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
- Decision: a table with nothing to list keeps one placeholder row. Its first cell reads `none: nothing to list` and the other cells are blank, except that in every table with a `status` column the placeholder's `status` is `empty`. That covers the five market tables, `tbl_checks` and `tbl_sources`. Macros (T33) skip rows whose `status` is `empty`. `tbl_lineage` has no `status` column, so macros recognise its placeholder by the first cell. Spec §9.2 is updated. T32B already builds this (`PLACEHOLDER_LABEL`, `PLACEHOLDER_STATUS` in `src/deskbrief/report/excel.py`), so no code changes. `tests/contract/test_excel_tables.py` pins `PLACEHOLDER_STATUS == "empty"`.
- Why: T32B put `empty` into `tbl_checks.status` and `tbl_sources.status`, which have their own status values, and spec §9.2 did not mention it. One skip rule is simpler for the macros than one per table. Raised in the [PR #47 review, finding 4](https://github.com/allenvarghese05/desk-brief/pull/47#pullrequestreview-5933580186).
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-17. Asked whether the spec should say that `empty` marks the placeholder row in any table with a status column, so the macros need one rule, he chose "Yes, one rule (Recommended)". The orchestrator wrote this record at his instruction.

## P-017 · 2026-11-17 · Every job rescores; eod compacts the last 7 days
- Decision: spec §5.4 and the small-files line in §6 match the code merged in T23 and T23B:
  - Every job ends with the serving step. It rescores `scorecard_daily` for the job's target date (an overwrite of that date's file, allowed for a `DAILY_TABLES` table under P-008), then writes the serving files.
  - The serving files (`health.json` and the 14-day, 30-day and 7-day windows) are anchored on the current ET date, not the job's target date. A backfill of an old date never replaces the latest view.
  - The scorecard counts every intraday poll. For other jobs it counts the latest run of each job per source, and a partial re-run replaces only the sources it fetched.
  - In `eod` only, compaction runs between the scorecard and the serving files. It covers `observations` and `quarantine` for today's ET date and the six before it, and skips partitions with fewer than two files. Older partitions are compacted only by a manual call.
- Why: the planner made these rulings during the T23 and T23B reviews, and the code follows them. Spec §5.4 still said the scorecard runs only at eod and compaction covers one day. Rulings raised in the [PR #45 round-1 review, findings 1–3](https://github.com/allenvarghese05/desk-brief/pull/45#pullrequestreview-5933428000), the spec gap in the [PR #45 round-2 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/45#pullrequestreview-5933525550), and the compaction window in the [PR #48 round-1 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/48#pullrequestreview-5933652099).
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-17. Asked whether to update the spec so it matches the code approved in #45 and #48, he chose "Match the code (Recommended)". The orchestrator wrote this record at his instruction.


## P-018 · 2026-11-19 · Scheduled runs are triggered by a Cloudflare Worker cron
- Decision: one Cloudflare Worker with a single every-minute Cron Trigger holds the schedule table (UTC, mirrored from the four workflows' `schedule:` crons) and calls GitHub's `workflow_dispatch` for `morning`, `intraday`, `eod` and `sla` at the matching minutes, passing two inputs: `fire`, the exact cron string, and `slot`, the scheduled minute in UTC (ISO 8601) computed from the table, not from when the Worker ran. One trigger avoids Cloudflare's per-plan trigger limits and its day-of-week numbering; the table is evaluated in code.
  - A `workflow_dispatch` run that carries `fire` and `slot` is a scheduled run: `trigger_from_env` returns `schedule`, `scheduled_for` is `slot`, and the gates are unchanged. `runs.trigger` records `schedule`; the dispatcher is named in the run log. No contract change.
  - A run whose job and `scheduled_for` match a `runs` row with status `ok` or `degraded` skips with reason `duplicate fire` (new runner logic). For a GitHub schedule event `scheduled_for` is the last fire of its cron when the run starts, so the guarantee is: a late GitHub fallback for a once-a-day pipeline fire (morning, eod, the `15 12` ECB trigger) resolves to the same slot as the Worker's run and is skipped; `sla` writes no `runs` row, so a duplicate sla run just checks again, which is harmless; a late fallback for the `*/15` intraday cron resolves to a later slot and runs as one more intraday poll, which is harmless.
  - The GitHub `schedule:` crons stay as a fallback.
  - The token is a fine-grained PAT (this repository, Actions: write), set by the owner as a Worker secret, never committed and never logged.
- Why: on 2026-11-18 GitHub created every scheduled event 5–8.5 h late and only 3 of 37 intraday fires. The ECB-fix FX cross-checks need a Yahoo tick within about a minute of the fix, which a late run cannot take (`docs/investigations/2026-11-18-runs-and-xchecks.md`, PR #70). Cloudflare is already the storage provider (R2).
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-19. Asked how scheduled runs should be triggered, he chose "Cloudflare cron (Recommended)". Mechanics (dispatch as schedule, duplicate skip, one trigger) set by the planner after the [PR #73 round-1 review, findings 3–5](https://github.com/allenvarghese05/desk-brief/pull/73). Card T43.

## P-019 · 2026-11-19 · ETF and single-stock daily bars come from Yahoo history
- Decision: the Yahoo `history` dataset (daily OHLCV, tier 2, `is_fallback = false`) is extended to every ETF and single stock in `config.yaml`; for these instruments it is the primary daily close, not a fallback for an official source. P-003 stands: no Alpaca daily bars.
  - `close_vs_official` compares the Alpaca SIP last 1-minute bar before the close with the Yahoo daily close (two independent sources) instead of the Stooq close.
  - `proxy_vs_exact` takes the proxy ETF's daily return from Yahoo history. It compares two instruments, not two sources, so it stays meaningful when both legs are Yahoo; its `details` say "same provider" in that case.
- Why: Stooq is disabled (owner decision; it serves a JS verification page to automated requests, so these checks would skip even if it were enabled), so ETFs and stocks have no daily bars, no charts and no long horizons on the dashboard. Yahoo is already a source with a `history` dataset; tier 2 is visible in hosted mode. Yahoo's terms remain part of open item H9.
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-19. Asked where ETF and stock daily history should come from, he chose "Yahoo history (Recommended)". Card T48.

## P-020 · 2026-11-19 · A one-time 2-year history load, separate from backfill
- Decision: a new manually dispatched `history` job loads two years of daily rows for every daily and weekly feed. It is not the per-day `backfill` path (that would be about 500 runs, and the sources plan one target date with short windows).
  - Contract (card T44): `JobKind.HISTORY`; an optional `plan_range(start, end, ctx)` method on the source adapter protocol (`sources/base.py`), absent meaning "no range support".
  - Each source implements `plan_range` in the card that owns its file: FRED (T46), Cboe (T47), Yahoo `history` (T48), CFTC (T49); Treasury and ECB in the history card (T53).
  - One run fetches each feed's date range in as few requests as the provider allows and writes one `observations` file per touched date partition (one run, one file per partition: P-008 holds).
  - It re-checks nothing already stored: it skips `(source, dataset, field, interval, as_of)` keys present, so an interrupted load can be re-run.
  - Its workflow has its own concurrency group (not `deskbrief-pipeline`), never runs the checks, scorecard or serving steps, and so never queues or cancels scheduled runs.
  - Backfilled rows carry their real `ingested_at`, so point-in-time reads before the load do not see them.
  - Dispatching it writes to production R2: an owner step.
  - The spec text for P-018 to P-021 is updated by the history card (T53), the last card of the phase, so one card owns the spec file; until then these rulings take precedence over the spec, as P-017 did.
- Why: 1Y/YTD horizons, 20-day volatility and 52-week ranges need history the store does not have (it started on 2026-10-29). Two years covers 1Y comparisons with a year to spare.
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-19. Asked how much history to load, he chose "2 years (Recommended)". The separate `history` job was set by the planner after the [PR #73 round-1 review, finding 7](https://github.com/allenvarghese05/desk-brief/pull/73), and the contract and per-source range support after round 2, findings 1 and 2. Cards T44, T53.

## P-021 · 2026-11-19 · Macro-desk data: FRED rates and dollar, Cboe volatility, CFTC positioning
- Decision: add
  - (a) FRED series: 10-year TIPS real yield (pct), 10-year breakeven inflation (pct), broad trade-weighted dollar (field `level`, unit index_pts), ICE BofA US high-yield option-adjusted spread (pct), Chicago Fed national financial conditions index (weekly, field `level`, new unit `index`, may be negative);
  - (b) Cboe indices: VIX9D, VIX3M, VIX6M, GVZ, VXN, SKEW (index_pts);
  - (c) a new `cftc` source for the weekly Commitments of Traders report (gold, S&P 500 e-mini, 10-year Treasury note futures): net speculative position and open interest (new unit `contracts`).
  All tier 3 (official), shown in hosted mode, using existing asset classes (`rates`, `fx`, `macro`, `vol`).
- Contract changes, approved by the planner and made first in their own `contract:` card (T44): `interval` gains `1w` (`as_of` = 00:00 America/New_York on the reference date, extending P-006, recorded in the `store/backend.py` docstring); `unit` gains `index` and `contracts` (`schemas.ENUMS` and `config.Unit`); `field` gains `net_position`, `open_interest` and `level` (an index value that is neither a price nor a rate); `KNOWN_SOURCES` gains `cftc`; `checks.validity` gets bounds for `index` and `contracts`. Code that special-cases `1d`/`1mo` learns `1w` in card T45. Adding instruments to `config.yaml` beyond P-004 is approved by this ruling for cards T46–T49.
- Why: a macro desk reads gold against real yields and the dollar, prices index options off implied volatility, and watches positioning for crowded trades. FRED and Cboe are existing sources; CFTC data is free and public.
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-19. Asked which macro-desk data to add, he chose all three groups. Contract scope set by the planner after the [PR #73 round-1 review, findings 2 and 6](https://github.com/allenvarghese05/desk-brief/pull/73), and round 2, findings 3, 4 and 7. Cards T44, T45, T46, T47, T49.

## P-022 · 2026-11-19 · Dashboard v2: refined dark, chart-first Home, TradingView charts, compact provenance
- Decision:
  - **Look.** A refined dark theme:
    - near-black page, with panels one shade lighter;
    - no borders, so cards separate by shade;
    - one blue accent, for the selected state only;
    - four type sizes and tabular figures;
    - card padding larger than the gaps between cards.

    It applies to every page.
  - **Colour.** Muted green and red mean up and down. **This supersedes spec §10.2's "red reserved for check failures".** A failed check is shown by a distinct marker that does not rely on colour alone: a ✕ icon and the word "failed" on an amber tint. A warning uses "warn" with the same amber family. Stale data still turns grey with a "stale" tag.
  - **Home layout** (replaces the §10.3 Overview sketch):
    - a slim top bar: brand and date, page tabs, a compact search and one status dot;
    - a headline strip of five instruments, each with its value, a change pill, a sparkline and a tier glyph;
    - a watchlist rail grouped by asset class, with full names;
    - a large chart of the selected instrument, with range and style controls in its header;
    - a right rail with the "Do the sources agree?" verdict and risk and range facts;
    - tabs under the chart: All values, Lineage, Checks, Corrections.

    Notable moves become a σ tag on watchlist rows, not a strip. The approved mockup is kept as a labelled synthetic fixture at `tests/fixtures/design/home-mockup-2026-11-19.html`.
  - **Charts.** Time-series and candle charts use TradingView Lightweight Charts (Apache-2.0). The standalone build is vendored at a pinned version, with a SHA-256 checksum, its `LICENSE` and `NOTICE`. It is served by a Streamlit custom component from a hand-written static folder: no npm build step, no CDN at runtime, and no change to `pyproject.toml`. Every chart shows the "Charts by TradingView" attribution link. Altair stays for small charts where it is enough.
  - **Provenance.** Every figure still carries source, tier, as-of and field in its markup and accessible label (P-009, spec §10.2).
    - On screen: a tier glyph beside the figure, with field, source and as-of in a CSS tooltip on hover, keyboard focus and tap.
    - Clicking the source opens a details panel with the full lineage.
    - A visible glyph legend on each page labels "delayed" (◐).
    - The words "proxy", "IEX", "fallback" and "HYPOTHETICAL" always stay visible as text next to the figure, never only in a tooltip.
    - The full "last · delayed · time · source" line no longer has to be printed under each figure.
    - Hosted mode still never shows or reads tier 1.
    - Generated markup uses no `title` attributes; tooltips are CSS.
  - **Navigation.** Selecting an instrument or a page happens inside the Streamlit session, through `st.navigation` and `st.switch_page` / `st.page_link` with `query_params`. It never does a full browser reload. Deep links by URL (`?id=`) keep working.
  - **Scope.** No contract files change.
- Why: on 2026-11-19 (ET) the owner judged the T36 dashboard cluttered and slow. He shared reference designs and chose this direction from four options. A study of the code found the main causes of the slowness: every click was a full page reload, connections were rebuilt every 60 s, and the table cache re-downloaded whole tables. It also found the candle chart's problems: a data gap, a calendar axis with weekend gaps, and fixed-width candles.
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-19 (ET). Asked four questions, he chose "Refined dark", "Big chart hero", "TradingView Lightweight" and "Compact icon + hover/panel". He then approved the clickable mockup with "Looks good, build it". The colour, label and markup rules were settled by the planner after the [PR #85 round-1 review, findings 1, 2 and 14](https://github.com/allenvarghese05/desk-brief/pull/85). Spec §10.2 and §10.3 are updated by card T58. Cards T56–T59.

## P-023 · 2026-11-23 · Dashboard v3: no containers, editorial layout
- Decision:
  - **No containers.** Blocks sit on the page background. A section opens with a small uppercase label over a hairline rule, and sections are separated by hairlines and whitespace. **This supersedes P-022's "panels one shade lighter; cards separate by shade".**
    - **Removed from every page:** panels, card surfaces, rounded boxes, outlines around blocks and shadows.
    - **What counts as a block container:** any element that wraps a section or a group of rows or figures. That includes keyed Streamlit containers, KPI cells, column and tab bodies, and expanders.
    - **What a block container may not have:**
      - a background other than transparent;
      - a border other than one hairline top or bottom rule;
      - a radius;
      - a shadow.
    - **Elements that may have a background different from the page:**
      - the hero change pill;
      - the selected watchlist row, and the hover highlight of a clickable row;
      - tooltips, popovers and dialogs;
      - the search field;
      - the selected segment of a segmented control;
      - inline markers and data encodings: tier glyphs, σ tags, status dots, the failed-check and warn markers, stale tags, check squares, heatmap cells, chart canvases, and the 52-week range track and its marker;
      - the selected-tab underline, and the hairline under the tab row;
      - keyboard focus indicators.
  - **Type.** Four type sizes:
    - the hero figure, 40–44 px on desktop and 36 px on phones;
    - 15 px for strip values, the verdict answer and the brand;
    - 13 px for body text, rows and tables;
    - 11.5 px for labels, qualifiers and meta.

    Hierarchy comes from weight and colour. Figures use tabular numerals.
  - **Home layout.** It replaces P-022's layout; the reference is direction B ("editorial calm"), kept as a labelled synthetic fixture at `tests/fixtures/design/home-mockup-2026-11-23.html`. Where it differs from this ruling, this ruling wins.
    - **Top bar:** brand and date, page tabs, search, and one status line.
    - **Market strip:** one line of five instruments as plain text: name, value, and the change as coloured text. It has no tiles, pills or sparklines. A basis shared by all five is said once at the start of the strip (for example "Fri close"). An item with a different basis says its own (for example "10Y delayed, vs official").
    - **Two columns:** the hero on the left, and the watchlist on the right.
    - **The hero:**
      - a label;
      - the figure;
      - one pill, holding the 1D change and its basis;
      - a provenance line;
      - range and style controls;
      - the chart;
      - one row of horizons from 1W to 1Y;
      - "Do the sources agree?" and "Risk and range" side by side under the chart, as flat label-value rows.
    - **The watchlist:**
      - It runs full length and never scrolls inside itself.
      - It is grouped by asset class, and each group's header folds and unfolds the group.
      - Rows have full names.
      - Changes are coloured text, not pills.
      - Every row has the same hover content: full name, field, tier, source and as-of. It is shown so that no scroll area, column or iframe can clip it.
      - It is wide enough that names never wrap one letter per line.
    - **Below 960 px,** the watchlist drops under the hero at full width.
    - **Below 640 px,** the sources and risk sections stack. The All values table puts the source and its tier word on a second line in the date cell, instead of in its own column.
    - **Tabs:** All values, Lineage, Checks and Corrections stay under the main area.
  - **The other pages** follow the same rules: no panels; sections with labels and hairlines; tables flat with hairline rows; the same four type sizes.
  - **Kept from P-022:**
    - the colours: one blue accent for the selected state, muted green and red for up and down, and the failed-check marker;
    - every P-022 Provenance bullet, unchanged. That covers:
      - source, tier, as-of and field in each figure's markup and accessible label;
      - the tier glyph with a CSS tooltip on hover, keyboard focus and tap;
      - the details panel;
      - the glyph legend on each page;
      - the words "proxy", "IEX", "fallback" and "HYPOTHETICAL" always visible beside their figures;
      - no `title` attributes;
      - tier 1 never shown or read in hosted mode;
    - TradingView charts and their attribution;
    - in-session navigation;
    - "no contract files change".
- Why: on 2026-11-21 (ET), after testing T58 and T59 against real data, the owner asked for the UI to be cleaner, without "AI slop" and without "containers for no reason". The owner also reported layout bugs:
  - an overflowing verdict;
  - clipped hover tooltips;
  - watchlist names breaking one letter per line at about 800 px;
  - dashes and "No checks yet" on weekends;
  - a "Not like-for-like" warning for two sources 0.8 bp apart.

  The planner built three container-free mockups at four widths:
  - A, "flat terminal";
  - B, "editorial calm";
  - C, "focus pane".

  An independent judge ranked them B, then A, then C. It recommended B with five changes taken from the others; this ruling includes them:
  - collapsible watchlist groups;
  - the 1D change in the pill;
  - a smaller hero figure;
  - the basis said once in the strip;
  - a wider watchlist, with a phone table that keeps the source.
- Status: **ratified by Allen Varghese (project owner)** in the Claude Code session on 2026-11-23 (ET). Shown A and B side by side and recommended "B with the grafts", the owner replied "yes go with this option". Cards T60 and T60B.
