# Planner decisions

Rulings that change the spec or plan. Each has an id, a date, the decision, the reason and **who ratified it**. PRs that edit planner-owned docs must cite a ratified ruling here. Agents may *propose* rulings; only the planner role, or the project owner when the planner role is held by the same agent as the implementer, may ratify them.

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

## P-008 · 2026-11-16 · One append per (table, ET date) per run
- Decision: keep the storage key layout (`run={run_id}.parquet`). Within one run, each table/date partition is written exactly once; the runner (T19) buffers rows per run, splits observations by `et_date(as_of)`, and flushes once per (table, date). A second append raises `KeyExistsError` by design.
- If incremental per-source flushes are ever needed, add an optional `part` to `table_key` (`run={id}-p{n}.parquet`) via a `contract:` PR; existing globs already match it.
- Why: surfaced in T02 review; keeps files few (small-files goal) and makes double-writes loud.
- Status: proposed by the planner role; **pending owner ratification** (does not change spec or contracts).

## P-009 · 2026-11-16 · `daily_close` field semantics and display labels
- Decision: `daily_close` takes fields `close`, `yield` and `rate` as the day's value. Every consumer (report T30–T33, dashboard T34–T35) must label by `field`/`unit` (e.g. "ECB reference rate", "yield %"), never call an ECB fix or a Treasury yield a "close". `daily_close` is latest-version (not point-in-time); report/check code needing reproducibility uses `obs_pit` and excludes the current, unfinished ET date.
- Why: surfaced in T02 review.
- Status: proposed by the planner role; **pending owner ratification**.
