# Planner decisions

Rulings that change the spec or plan. Each has an id, a date, the decision and the reason. PRs that edit planner-owned docs must cite a ruling here.

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
- Decision: as documented in `src/deskbrief/store/backend.py` and spec §6.
- Why: catch-up, rebuild, `daily_close` and `obs_pit` all depend on one shared definition.
