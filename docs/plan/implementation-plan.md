# Desk Brief — Implementation Plan

Status: v1, 2026-11-15; review follow-up cards added 2026-11-16 and 2026-11-17. Source of truth for task order. Spec: `docs/specs/2026-11-15-desk-brief-design.md` (cited as **§n**). Process: `docs/process/agent-workflow.md`.

Rules that apply to every card:
- One card → one branch (`t{id}-{slug}`) → one PR. Target ≤ ~400 changed lines excluding fixtures/lockfile.
- A card may only touch files in its **Owns** list (+ its own tests and fixtures). Anything else → stop, ask the planner.
- `config.yaml` *values* are not contracts: a source card may add or adjust feeds/datasets **for its own source** (disclose in the PR). Other config edits go through the planner (ruling P-004 in `docs/plan/decisions.md`).
- **Contracts** (frozen after Phase 0): `models.py`, `schemas.py`, `sources/base.py`, `store/backend.py` public API, `config.py` models, storage key layout (§6), table schemas (§7.2), Excel table names and column headers (§9.2, P-015). Changing one needs a planner-approved `contract:` PR merged first.
- TDD: failing test first. No network in unit tests. Synthetic data only under `tests/`, labeled.
- Definition of done for every card: acceptance criteria met, `make ci` green locally and in CI, reviewer APPROVE, `docs/agent-log/T{id}.md` entry written (P-014).

---

## 1. Phase overview and dependency graph

```mermaid
flowchart TD
  H[Human setup H1–H5] -.-> T24
  T00[T00 Repo + CI scaffold] --> T01[T01 Contracts: config, models, schemas, logging, time]
  T01 --> T02[T02 Storage: backends, raw writer, datasets, DuckDB views]
  T01 --> T03[T03 HttpClient: rate limit, retry, redaction]
  T01 --> T04[T04 Calendars]
  T03 --> T10[T10 Treasury] & T11[T11 FRED] & T12[T12 ECB] & T13[T13 Stooq + Cboe] & T14[T14 Alpaca] & T15[T15 Twelve Data] & T16[T16 Yahoo]
  T02 --> T19[T19 Runner + clean + CLI]
  T03 --> T19
  T10 & T11 & T12 & T13 & T16 --> T24A[T24A morning.yml v0 - official + Yahoo]
  T19 --> T24A
  T02 --> T20[T20 DQ framework + single-source checks]
  T04 --> T20
  T02 --> T21[T21 Cross-check framework + official/yahoo/close checks]
  T21 --> T22[T22 IEX vs SIP, proxy vs exact, FX vs ECB]
  T20 & T21 --> T23[T23 Scorecard + serving snapshot + compaction]
  T14 & T15 & T19 --> T24B[T24B intraday, eod, sla, backfill, rebuild workflows]
  T02 & T04 & T03 --> T05[T05 Hardening follow-ups]
  T05 --> T24A
  T02 --> T30[T30 BriefModel]
  T30 --> T31[T31 HTML + email]
  T30 --> T32[T32 Excel xlsx]
  T32 --> T33[T33 VBA xlsm]
  T02 --> T34[T34 query layer + dashboard shell + hosting]
  T34 --> T35[T35 Dashboard pages]
  T23 --> T35
  T20 --> T40[T40 Alerts]
  T02 --> T41[T41 Paper portfolio + reconciliation]
  T35 & T31 --> T42[T42 README, docs, screenshots]
  T19 & T20 & T21 --> T06[T06 Wire checks into runner + runner safety]
  T30 --> T09[T09 Brief-model follow-ups]
  T09 --> T06
  T06 --> T23
  T20 --> T07[T07 Data-check follow-ups]
  T08A[T08A contract: HttpClient cost argument] --> T08B[T08B Budget units in HTTP client]
  T03 --> T08A
  T05 & T08B --> T08[T08 Source + logging follow-ups]
  T07 -.->|anomaly bullet only| T09
  T21 --> T17[T17 Cross-check follow-ups]
  T06 & T07 & T08 & T09 & T17 --> T18[T18 Sandbox-runnable tests]
```

### Parallel lanes (max concurrent implementers per window)

| Window | Lane A | Lane B | Lane C | Lane D | Lane E | Lane F |
|---|---|---|---|---|---|---|
| W0 Mon AM | T00 → T01 (sequential, single agent) | | | | | |
| W1 Mon PM | T02 | T03 | T04 | | | |
| W2 Tue | T19 | T10 → T13 | T11 → T12 | T14 | T15 | T16 |
| W3 Tue PM/Wed | T05 → T24A (**ship asap**) | T20 | T21 → T22 | T30 | T34 | |
| W4 Wed/Thu | T24B | T23 | T31 | T32 → T33 | T35 | T40 |
| W5 Fri | T41 | T42 | fixes from run history | | | |

Review follow-ups (T06–T09, T08A, T08B, T17, T18) fill free lanes from W3 on, within the ordering rules in their section. T09 lands before or with T06, and T06 before T23.

Critical path: **T00 → T01 → T02/T03 → T05 + T19 + official sources → T24A** (scheduled runs start) → T20/T21 → T30 → T31 (email live). Everything else hangs off this.

### Timeline

| Date | Target |
|---|---|
| Mon 16 Nov | W0 + W1 merged. Human tasks H1–H5 done (accounts, keys, bucket, repo, branch protection) |
| Tue 17 Nov | W2 merged. T24A merged by evening → **first scheduled morning run Wed 18 11:15 UTC** (2 days earlier than brief) |
| Wed 18 Nov | T20, T21, T22, T30, T34 merged; T24B live (intraday + eod begin history) |
| Thu 19 Nov | T23, T31 (email live), T32, T35 merged |
| Fri 20 Nov | T33 (needs H6), T40, T41, T42. Screenshots |
| Sat–Sun 21–22 | Polish from run history; real incidents written up; user feedback (H8) |
| 23–28 Nov | Buffer; stretch (T50 narrative) only if all DoD met |

Cut order if behind: T41 → T40 → T33 → dashboard pages 5–6. Never cut: T24A, T20–T22, T31, scorecard.

---

## 2. Human tasks (Allen — cannot be done by agents)

| ID | Task | Needed by |
|---|---|---|
| H1 | Accounts + keys: Alpaca (paper account, data API keys), Twelve Data, FRED API key | T24A (Tue) |
| H2 | Cloudflare: create R2 bucket `deskbrief-data` (private); 3 API tokens: `ci-writer` (RW, this bucket), `dashboard-reader` (RO), `admin` | T02 integration, T24A |
| H3 | GitHub repo `desk-brief` (public). Branch protection on `main` per workflow doc §6 (single account, `review/approved` status gate). Secrets per spec §11 | T00 merge |
| H4 | healthchecks.io: two checks (morning, eod) with schedules; email alerts to you | T24A |
| H5 | Gmail app password (or Resend + domain), `REPORT_RECIPIENTS` secret | T31 |
| H6 | In desktop Excel: import `vba/*.bas` into blank `.xlsm`, add buttons, save; run `uv run python -m xlsxwriter.vba_extract` (or the `vba_extract.py` script) → commit `vbaProject.bin` | T33 |
| H7 | Streamlit Community Cloud: deploy `app/Home.py` as private app, add viewer emails, paste `dashboard-reader` secrets | T34 |
| H8 | Send dashboard + report to feedback users; capture feedback verbatim for README | Sat 21 |
| H9 | Verify provider display terms (Alpaca, Twelve Data, Yahoo) before hosted mode shows any T1/T2 market data; record outcome in `docs/terms.md` | T34 |

---

## 3. Task cards

Card format: **Depends** · **Parallel with** · **Owns** · **Spec** · Deliverables · Acceptance criteria · Out of scope.

### Phase 0 — Foundations (sequential)

#### T00 — Repo scaffold and strict CI
- **Depends:** none · **Parallel with:** none
- **Owns:** `pyproject.toml`, `uv.lock`, `Makefile`, `.github/workflows/ci.yml`, `.github/pull_request_template.md`, `.github/dependabot.yml`, `.gitleaks.toml`, `.gitignore`, `.env.example`, `README.md` (stub), `INCIDENTS.md` (template), `docs/agent-log.md`, `docs/glossary.md` (from brief §18), `src/deskbrief/__init__.py`, `tests/conftest.py`
- **Spec:** §13, §14, workflow doc §5–6
- Deliverables: uv project (Python 3.12); deps: pandas, duckdb, pyarrow, httpx, pydantic, pyyaml, structlog, pandera, tenacity, pandas-market-calendars, yfinance, xlsxwriter, jinja2, streamlit, python-ulid; dev: pytest, pytest-cov, pytest-socket, respx, ruff, mypy, pandas-stubs, types-PyYAML. `make ci` = `uv lock --check && ruff check && ruff format --check && mypy --strict src && pytest -m "not integration" --cov=deskbrief --cov-fail-under=85 && gitleaks`. CI jobs: `lint`, `typecheck`, `test`, `security` (gitleaks + `uv` audit if available) as separate required checks; pinned action SHAs; `permissions: contents: read`. Also implement the `contracts` and `pr-hygiene` CI checks from workflow doc §5 (small scripts in `scripts/ci/`, owned by this card), and a `config` job that is a no-op until T01 lands `python -m deskbrief.config`.
- Acceptance: CI green on an empty package with one trivial test; network blocked in tests by default (`--disable-socket` in `pytest.ini` options); PR template contains sections from workflow doc §7.
- Out of scope: any business code.

#### T01 — Contracts
- **Depends:** T00 · **Parallel with:** none
- **Owns:** `config.yaml`, `src/deskbrief/{config.py,models.py,schemas.py,logging.py,timeutil.py,ids.py}`, `src/deskbrief/sources/base.py`, `src/deskbrief/store/backend.py` (Protocol only), `tests/unit/test_config.py`, `tests/unit/test_schemas.py`, `tests/unit/test_logging.py`, `tests/unit/test_timeutil.py`
- **Spec:** §3, §4, §5.1, §6, §7.2
- Deliverables: `python -m deskbrief.config` (validate config, exit non-zero on error; used by CI `config` job); full `config.yaml` with the universe in §3 (all feeds, groups, datasets, thresholds); pydantic models + `load_config()` with validation (every group references existing instruments; every feed references a known source; tiers ∈ {1,2,3}); dataclasses `FetchRequest`, `RawResponse`, `RunContext`, `JobKind`, `DQResult`, `XResult`; pandera schemas for every table in §7.2 exposed as `OBS_SCHEMA`, `SOURCE_LOG_SCHEMA`, …; structlog JSON setup with redaction processor; `timeutil` (utc_now injectable clock, ET conversions, ULID ids); `Source` and `StorageBackend` protocols.
- Acceptance: config loads; invalid configs (dangling group ref, bad tier, unknown source) raise clear errors; redaction test proves `apikey=SECRET` never appears in output; schemas reject wrong dtypes/naive timestamps.
- Out of scope: implementations.

### Phase 1 — Infrastructure + ingestion (parallel)

#### T02 — Storage layer
- **Depends:** T01 · **Parallel with:** T03, T04
- **Owns:** `src/deskbrief/store/{local.py,r2.py,raw.py,datasets.py,duck.py}`, `tests/unit/store/*`, `tests/integration/test_r2.py`
- **Spec:** §2.2, §2.3, §6, §7.3
- Deliverables: `LocalBackend`, `R2Backend` (boto3 or s3fs against R2 endpoint); `put_bytes(overwrite=False)` raises on existing key; `RawWriter.write(raw, ctx) -> key` with sidecar; `Datasets.append(name, df, ctx)` validates with schema and writes `…/date=/run=.parquet`; `duck.connect(store, config)` returns DuckDB connection with httpfs secret (R2) or local paths, registers all §7.3 views incl. `obs_pit` macro and dim views from config; `clean/_CURRENT` pointer read.
- Acceptance: round-trip tests on LocalBackend; write-once enforced; `obs_latest` and `obs_pit` correct on a synthetic multi-version dataset (table-driven); `daily_close` tier preference correct; integration test (marked) writes/reads one object in R2.

#### T03 — HttpClient
- **Depends:** T01 · **Parallel with:** T02, T04
- **Owns:** `src/deskbrief/sources/http.py`, `src/deskbrief/sources/registry.py`, `tests/unit/sources/test_http.py`
- **Spec:** §5.1
- Deliverables: httpx client wrapper; per-source token bucket from config; tenacity retry (timeout, 5xx, 429 honoring Retry-After; no retry 401/403/404); returns `RawResponse` with `error` instead of raising; Twelve Data daily budget tracker (backed by Store via an injected callable, so no T02 dependency); source registry `get_sources(config)`.
- Acceptance: respx tests for each retry path, rate limit timing (fake clock), redaction of query secrets in logged request, budget exhaustion returns `status=budget_exhausted` without HTTP call.

#### T04 — Market calendars
- **Depends:** T01 · **Parallel with:** T02, T03
- **Owns:** `src/deskbrief/calendars.py`, `tests/unit/test_calendars.py`
- Deliverables: `is_business_day(cal, date)`, `prev_business_day`, `session_bounds_utc(date)` for XNYS (incl. early closes), SIFMA_US, TARGET (fixed rules: Jan 1, Good Friday, Easter Monday, May 1, Dec 25, 26).
- Acceptance: Thanksgiving 2026-11-26 closed, 2026-11-27 XNYS early close 13:00 ET, TARGET Good Friday, DST boundary sessions correct.

#### T10 — Treasury par yield curve source
- **Depends:** T03 · **Parallel with:** T11–T16
- **Owns:** `src/deskbrief/sources/treasury.py`, `tests/unit/sources/test_treasury.py`, `tests/fixtures/real/treasury/*`
- Deliverables: plan (morning: D-1; eod: today; backfill: month ranges), fetch, parse → one obs per tenor (`field=yield`, `unit=pct`, `interval=1d`, `as_of` = date 00:00 ET → UTC), instrument mapping via config symbols ("2 Yr", "10 Yr", …).
- Acceptance: golden parse test on a real captured file; empty-month and holiday handled; `docs/sources/treasury.md` probe note (endpoint verified, date, sample size) — included in PR description.

#### T11 — FRED source
- Same shape as T10. **Owns:** `sources/fred.py` + tests/fixtures. Series from config (DGS2, DGS10, DGS20, DGS30, DFF, DCOILWTICO, DCOILBRENTEU, CPIAUCSL, UNRATE). `.` → skip row (not NaN). Request last 10 observations per series per run so revisions are visible. Acceptance includes redaction of `api_key`.

#### T12 — ECB reference rates source
- **Owns:** `sources/ecb.py` + tests. SDMX CSV for USD, GBP, JPY vs EUR; emits raw ECB rates plus derived EURUSD/GBPUSD/USDJPY with `is_derived=true`. Acceptance: cross-rate math test; TARGET holiday empty response → `empty`, not error.

#### T13 — Stooq + Cboe sources
- **Owns:** `sources/stooq.py`, `sources/cboe.py` + tests. Day-1 probe decides: if Stooq is blocked/needs key, implement with `enabled: false` in config and document; Yahoo daily close becomes T3 fallback with `is_fallback=true` (implemented in T16; T13 only sets config `enabled: false` — no code dependency on T16). Cboe VIX history CSV → `VIX` close.

#### T14 — Alpaca source
- **Owns:** `sources/alpaca.py` + tests (synthetic fixtures). Datasets: `iex_latest` (latest trade + quote, multi-symbol), `iex_bars_1m`, `sip_bars_1m` (window must end ≥16 min before now — enforce and test). (`daily_bars` dropped, ruling P-003.) Pagination via `next_page_token`. Acceptance: SIP window guard; pagination; headers never logged.

- Changelog: T01 review: `daily_bars` dropped (P-003).
#### T15 — Twelve Data source
- **Owns:** `sources/twelvedata.py` + tests (synthetic fixtures). `fx_quote` batch for 3 pairs; `fx_series_1m` for a window (ECB fix ±10 min). Budget enforced via T03 tracker. Error payloads with HTTP 200 (`{"status":"error"}`) map to `status=error`.

#### T16 — Yahoo source
- **Owns:** `sources/yahoo.py` + tests (synthetic fixtures). yfinance wrapped: `quote` (fast_info / last price with timestamp) and `history` (daily + 1m). Serializes returned frame + yfinance version as raw JSON. Applies `scale` from feed config (^TNX). Implements `daily_fallback` dataset for T13 fallback. Acceptance: scale applied; empty frame → `empty`; exception → `error` with message.

#### T19 — Pipeline runner, clean step, CLI
- **Depends:** T02, T03 · **Parallel with:** T10–T16 (uses a `FakeSource` in tests)
- **Owns:** `src/deskbrief/pipeline/{runner.py,jobs.py,clean.py,catchup.py,manifest.py}`, `src/deskbrief/cli.py`, `tests/unit/pipeline/*`, `tests/fault_injection/inj_runner_*.py`
- **Spec:** §2.4, §5.4, §5.5
- Deliverables: `deskbrief run {morning|intraday|eod|backfill|rebuild} [--date] [--dry-run] [--sources]`; runner loop per sequence diagram; failure isolation; quarantine; `runs` row with `schedule_delay_s` (from `GITHUB_EVENT_SCHEDULE` / scheduled time); catch-up for missed business days; exit code 0 for ok/degraded, 1 for failed (only when *no* source succeeded or storage unavailable); GHA step-summary markdown writer; `rebuild` writes `clean/v{N+1}` then flips `_CURRENT`.
- Storage rule (P-008, spec §2.4 item 5, §5.5): buffer rows per run and flush once per (table, ET date) in a `finally`, also when degraded; split observations by `et_date(as_of)`. The `runs` row is written **last**, so a run that died mid-flush has no `runs` row and catch-up treats it as missed. Backfill flushes per month chunk so it never buffers more than ~31 days in memory.
- Acceptance: a source raising mid-run still leaves every other source's rows flushed (fault-injection test); a simulated crash before flush leaves raw/ intact and `rebuild` recovers the clean rows; a rerun uses a new `run_id` and causes no `KeyExistsError`.
- Acceptance: fault-injection tests: one source raising, one returning corrupted body, one 429 storm → run `degraded`, other sources' data present, quarantine row + DQ fail written; rerun same date → no duplicate rows in `obs_latest`; catch-up picks up 2 missed days.

- Changelog: PR #6: storage rule per P-008; flush/crash/retry acceptance from the PR #6 round-1 review; runs-row-last and backfill chunking from the round-2 review.
#### T05 — Hardening follow-ups from T01/T02/T04 reviews
- **Depends:** T02, T04 merged; **merge after T03** (T03 imports `redact`/`MASK` from `logging.py`; T05 rebases on it) · **Parallel with:** Phase 1 sources (no shared files) · **Blocks:** T24A (carries the R2 checksum config)
- **Owns:** `src/deskbrief/logging.py`, `src/deskbrief/calendars.py`, `src/deskbrief/store/{local.py,r2.py,raw.py,datasets.py,duck.py}`, `pyproject.toml` + `uv.lock` (only the `pandas-market-calendars` floor), and their tests
- Deliverables (each with a test):
  - logging: add strong names `secret[_-]?key`, `(session|private|auth)[_-]?(token|key)`, and treat `x-auth-token` and `sessionid` both as header/key names and as inline `name=value` tokens (T01 confirmation review #1); structured-key masking covers `private_key`, `auth` (T01 confirmation #4); reduce strong-tier over-masking of placeholder values like `required`, `missing`, `None`, `ok` (T01 confirmation #3); weak-tier keeps `cache_key=SPY:1m:2026-11-14`-style diagnostics readable (T01 confirmation #2). Every probe from all rounds goes into the MUST_MASK / MUST_KEEP tables
  - calendars: `_check_date` rejects non-`date` types with `TypeError`; validate `cal` before empty-range shortcuts; make `cal` keyword-only in `is_early_close(d, *, cal="XNYS")`; add `add_business_days(cal, d, n)` (used by freshness `lag_bdays`); return read-only cached schedules; raise `pandas-market-calendars` floor to the locked major (T04 review; verified by `uv lock --check` in CI, no unit test)
  - storage: backends refuse `overwrite=True` and `delete()` for `raw/` keys; `RawWriter.write` is idempotent on retry (if body exists with the same sha256, continue to sidecar); `Datasets.append` asserts rows belong to `part_date` (P-006); `overwrite` only for `DAILY_TABLES`; R2 boto config `request_checksum_calculation`/`response_checksum_validation` = `when_required`; cache `_CURRENT` per run; escape glob chars in local root (T02 review)
- Acceptance: all of the above tested (except the dependency floor, see above); no contract file changes. The implementer may split the work by area (logging / calendars / storage) if the diff exceeds ~600 lines. If `part` is adopted later (P-008), compaction must accept `run=*-p{n}.parquet`.

- Changelog: PR #6 rounds 1–2: card created from review findings on PRs #2, #3 and #5 (links in the PR #6 body).

### Review follow-ups (from PRs #9–#21)

These cards collect the non-blocking findings that reviewers deferred on PRs #9–#21. Each bullet links its source review. Finding numbers refer to the numbered list in the linked review; "confirmation list" means the second list in a round-2 review. As with T05, a follow-up card may own files of a card that has already merged.

Order and shared files:
- **T09 merges before T06, or in the same window.** Wiring checks into the runner (T06) makes `health_summary` double-count cross-checks until T09 fixes it.
- **T09's anomaly-reuse bullet waits for T07's anomaly change.** The rest of T09 does not wait.
- `pipeline/jobs.py` is shared. T06 edits only the checks hook, T23 only the serving hook (`steps_for`), T31 only the morning row of `steps_for`, and T24B only the intraday branch of `evaluate_gate`. T24B may also need a small `runner.py` edit; it starts that only after T06 has merged. Whichever merges later rebases on the earlier one.
- T18 runs last, after T06–T09 and T17, because it touches test files those cards own.
- T08's Twelve Data budget bullet waits for T08B, which waits for the T08A `contract:` PR. The rest of T08 does not wait.
- Otherwise T07, T08, T08A/T08B and T17 are independent.

#### T06 — Wire checks and cross-checks into the runner; runner safety
- **Depends:** T19, T20, T21 merged; T09 merged first or in the same window · **Parallel with:** T07, T08, T17 · **Blocks:** T23 (scorecard reads `dq_results`/`crosscheck_results` written by real runs)
- **Owns:** `src/deskbrief/pipeline/{runner.py,clean.py,catchup.py,manifest.py}`, the checks hook in `src/deskbrief/pipeline/jobs.py` (shared file, see above), `src/deskbrief/cli.py`, `tests/unit/pipeline/*`, `tests/fault_injection/inj_runner_*.py`
- **Spec:** §2.4, §5.5, §7.2, §8; P-008
- Deliverables:
  - Call `checks.run_checks` from the runner hook in `jobs.py`, after the observations flush, with a single `dq_results` append per (table, ET date) per run (P-008). Source: [PR #16 merge confirmation](https://github.com/allenvarghese05/desk-brief/pull/16#issuecomment-6906878775).
  - Design choice for this card: run the T21 cross-checks in the same hook, so the run makes one `dq_results` append that includes the mirrored `consistency` rows. Write `revisions` and `crosscheck_results` under their §7.2 partition rules. Basis: the T20 card ("runner hook `run_checks(ctx)` writes `dq_results` and `revisions`"), the T21 card ("writes `crosscheck_results` + mirrored `consistency` dq rows") and P-008. The T21 card names no hook, so a separate cross-check hook is acceptable if it keeps a single `dq_results` append per run.
  - Count the check-written DQ rows in the `runs` row (`n_dq_fail`, `n_dq_warn`), not only the runner's own rows. Source: [PR #16 round 1, finding 8](https://github.com/allenvarghese05/desk-brief/pull/16#pullrequestreview-5914480147).
  - Rebuild flip guard: never flip `_CURRENT` to a version with no partitions written; re-check that the pointer has not moved immediately before the flip; two concurrent rebuilds must not both flip. Source: [PR #16 round 2, finding 1](https://github.com/allenvarghese05/desk-brief/pull/16#pullrequestreview-5914845782).
  - Chunk rebuild by month with a single final flip, so memory stays bounded as `raw/` grows. Source: [PR #16 round 1, finding 10](https://github.com/allenvarghese05/desk-brief/pull/16#pullrequestreview-5914480147) and the [author's deferral](https://github.com/allenvarghese05/desk-brief/pull/16#issuecomment-6906415315).
  - An `EMPTY` fetch with no calendar reason writes a DQ `warn` row instead of passing silently. Use the per-dataset expectations from T20. Source: [author's deferral of round-1 finding 9](https://github.com/allenvarghese05/desk-brief/pull/16#issuecomment-6906415315).
- Acceptance:
  - A runner test proves a single `dq_results` append per date per run with checks and cross-checks enabled.
  - A rerun of the same date adds no duplicate check rows.
  - A `source_log` flush failure and a `quarantine` flush failure each give `failed`; a `dq_results`-only loss gives `degraded`. Source: [PR #16 round 2, finding 2](https://github.com/allenvarghese05/desk-brief/pull/16#pullrequestreview-5914845782).
  - Rebuild with zero partitions leaves `_CURRENT` unchanged; a pointer move between write and flip aborts the flip.
- Out of scope: month-chunked backfill (needs a `sources/base.py` contract change; separate `contract:` PR if ever wanted).

- Changelog: card created from the PR #16 reviews linked above and the T20/T21 card text.

#### T07 — Data-check follow-ups
- **Depends:** T20 merged · **Parallel with:** T06, T08, T17 · **Blocks:** T09's anomaly-reuse bullet
- **Owns:** `src/deskbrief/checks/*`, `tests/unit/checks/*`, `tests/fault_injection/*inj_checks*.py`
- **Spec:** §8.1; P-009
- Deliverables (source: [PR #20 review](https://github.com/allenvarghese05/desk-brief/pull/20#pullrequestreview-5914719908) unless noted):
  - Make freshness and completeness agree for a dataset that is due but still within grace: both `skip` with reason "waiting" (finding 1).
  - Intraday freshness: age of the latest tick against the open session (spec §8.1) (finding 2). It needs a per-dataset intraday schedule. A new `config.yaml` value goes through the planner (P-004). A new field on a `config.py` model is a contract change: land a `contract:` PR first.
  - Completeness for Yahoo and Twelve Data datasets, and intraday tick-age freshness. Source: [PR #16 merge confirmation](https://github.com/allenvarghese05/desk-brief/pull/16#issuecomment-6906878775).
  - FRED daily oil (DCOILWTICO, DCOILBRENTEU): do not fail completeness on days SIFMA is open but the oil market is closed (finding 4).
  - Anomaly: score only a move that belongs to the expected data day. A series that stopped updating must not keep reporting its old move (finding 5).
  - Docstring for the eod minute-completeness rule: the 22:30 UTC eod run judges the previous session's bars, per P-009 (finding 6).
  - Rename the fault-injection test file to match the Owns glob, or record why it cannot (finding 7).
- Acceptance: one table-driven test per bullet above; no change to the `dq_results` schema.
- Early closes (P-011, now in spec §8.1): keep T20's early-close behaviour, and make every new check in this card follow it. Minute completeness counts only minutes up to the early close. A missing or late `SIFMA_US` value on a SIFMA recommended early close is at most `warn` (finding 3).

- Changelog: card created from the PR #20 review and the PR #16 merge confirmation; early-close line from P-011.

#### T08A — `contract:` HttpClient cost argument
- **Depends:** T03 merged · **Blocks:** T08B
- **Owns:** `src/deskbrief/sources/base.py` (the `HttpClient` protocol only), as a planner-approved `contract:` PR under the plan's Contracts rule (PR title `contract: <summary>`), plus the signature of `DefaultHttpClient.get` in `src/deskbrief/sources/http.py`. T08B owns `http.py` after T08A merges.
- Deliverables: add an optional keyword argument to `HttpClient.get` for the number of budget units a request costs, default 1. Add the same defaulted keyword to `DefaultHttpClient.get`, accepted and ignored until T08B, so the runner's `http: HttpClient` typing still checks. No behaviour change.
- Acceptance: the contracts CI check passes with the change declared; mypy passes on every source unchanged.

- Changelog: card created so the contract change T08B needs is tracked, from [PR #22 round 3, finding 3](https://github.com/allenvarghese05/desk-brief/pull/22#pullrequestreview-5925742514); `DefaultHttpClient.get` signature added from [PR #23 round 1, finding 6](https://github.com/allenvarghese05/desk-brief/pull/23#pullrequestreview-5926433510).

#### T08B — Budget units in the shared HTTP client
- **Depends:** T03, T08A merged · **Blocks:** T08's Twelve Data budget bullet
- **Owns:** `src/deskbrief/sources/http.py`, `tests/unit/sources/test_http.py`
- Contract first: the `HttpClient` protocol that `Source.fetch` receives is defined in `src/deskbrief/sources/base.py`, a contract file. Adding a cost argument to `get` is a contract change. It lands first as card T08A, a planner-approved `contract:` PR. T08B does not own `sources/base.py`.
- Deliverables: add a `units` argument (default 1) to `BudgetTracker.try_consume` and implement the new `HttpClient.get` cost argument in `sources/http.py`. Existing callers do not change.
- Acceptance: a test that a cost-5 call consumes 5 units and is refused when fewer remain; all existing source tests pass unchanged.
- Why its own card: every source uses this API, so it lands and is reviewed alone. The source review itself says it "needs a contract change". Source: [PR #17 round 2, finding 2](https://github.com/allenvarghese05/desk-brief/pull/17#pullrequestreview-5914845514).

- Changelog: card created from the PR #17 round-2 review.

#### T08 — Source and logging follow-ups
- **Depends:** T05, T11, T13–T16 merged; T08B for the Twelve Data budget bullet · **Parallel with:** T06, T07, T17
- **Owns:** `src/deskbrief/sources/{alpaca.py,twelvedata.py,cboe.py,stooq.py,yahoo.py,fred.py}`, `src/deskbrief/logging.py`, `pyproject.toml` + `uv.lock` (only the `yfinance` floor), `docs/sources/*`, their tests
- The implementer may split this card by area (one PR per source, plus one for logging) if the diff exceeds ~400 lines, as T05 allowed.
- Deliverables:
  - Twelve Data batch budget: after T08B, count a batch quote as one unit per symbol. Source: [PR #17 round 2, finding 2](https://github.com/allenvarghese05/desk-brief/pull/17#pullrequestreview-5914845514).
  - Twelve Data: test one symbol missing from an otherwise good batch; inline `_new_fetch_id`. Source: [PR #17 round 2, confirmation list findings 1–2](https://github.com/allenvarghese05/desk-brief/pull/17#pullrequestreview-5914845514).
  - Alpaca: when pagination hits the page cap or a repeated token, keep the pages already fetched in raw. Say in the docs that free-plan IEX prices and volume cover one exchange only. Optionally guard SIP windows in `fetch` as well as `plan`. Consider per-symbol quarantine after the live probe. Source: [PR #19 review, findings 1–4](https://github.com/allenvarghese05/desk-brief/pull/19#pullrequestreview-5914847211).
  - Cboe: validate only rows inside the requested window, so one bad historical row does not break every daily run; detect a truncated download that ends mid-number; mark the unverified URL `# UNVERIFIED` in code. Source: [PR #11 review, findings 2–4](https://github.com/allenvarghese05/desk-brief/pull/11#pullrequestreview-5914386201).
  - Convert `csv.Error` in Stooq and `OverflowError` in Cboe to `ParseError`. Source: [PR #11 review, finding 5](https://github.com/allenvarghese05/desk-brief/pull/11#pullrequestreview-5914386201).
  - Yahoo: raise the `yfinance` floor to the 1.x API the code uses; document (or fix) that futures, indices and yields drop today's bar even after the session ends; make the "empty on a US business day means error" rule depend on the symbol set, not only the date. Source: [PR #13 round 2, findings 1–3](https://github.com/allenvarghese05/desk-brief/pull/13#pullrequestreview-5914461338).
  - FRED: add a test that every configured FRED series has an entry in `SERIES_SPECS`, so a new config series fails CI; assert the warning in the skip test; consider logging the skip at error level. Source: [PR #14 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/14#pullrequestreview-5914397821).
  - Logging: decide each of two known gaps: all-uppercase short-segment credential values kept by the weak tier, and short numeric values kept for strong names (passwords, secrets, API keys) by the placeholder exemption. For each, either tighten the rule and add the probes from the source review as MUST_MASK cases, or record the cases as known-unmasked in the `logging.py` docstring and as MUST_KEEP cases with a comment. The review offers both options. Source: [PR #15 review, findings 1–2](https://github.com/allenvarghese05/desk-brief/pull/15#pullrequestreview-5914412921).
- Acceptance: a test per bullet. T08 makes no change to `sources/base.py`; the protocol change belongs to T08A.
- Out of scope: live probes (owner task; see "Live-probe gates" below).

- Changelog: card created from the PR #11, #13, #14, #15, #17 and #19 reviews linked above.

#### T09 — Brief-model follow-ups
- **Depends:** T30 merged; T07 for the anomaly-reuse bullet · **Parallel with:** T08, T17 · **Blocks:** T06 (or merges in the same window)
- **Owns:** `src/deskbrief/report/model.py`, `src/deskbrief/query.py`, `tests/unit/report/*`, `tests/unit/test_query.py`, `tests/brief_synth.py`
- Deliverables:
  - `health_summary`: do not count a cross-check twice. T21 mirrors each cross-check as a `consistency` row in `dq_results`. Exclude those rows from the DQ count, or count only the mirror. Source: [PR #18 merge confirmation](https://github.com/allenvarghese05/desk-brief/pull/18#issuecomment-6907020326).
  - Where the model shows cross-check rows, label them with `details.ref_date`, not the run's `target_date`. Source: [PR #18 round 2, finding 7](https://github.com/allenvarghese05/desk-brief/pull/18#pullrequestreview-5914906053).
  - Per-row qualifiers (fallback, IEX single exchange, delayed by N minutes) on each `Row`, not only as global known limits. Source: [PR #21 round 1, finding 2](https://github.com/allenvarghese05/desk-brief/pull/21#pullrequestreview-5914850524).
  - After T07: reuse T20's anomaly logic for notable moves instead of a separate z-score. Source: [PR #21 round 1, finding 6](https://github.com/allenvarghese05/desk-brief/pull/21#pullrequestreview-5914850524).
  - Cross-source note when a tick value (for example `UST_10Y` from Yahoo `^TNX`) is compared with an official baseline. Wording for a zero baseline that says the baseline was found and is zero. Source: [PR #21 round 2, confirmation list findings 1–2](https://github.com/allenvarghese05/desk-brief/pull/21#pullrequestreview-5914901602).
  - The golden test must not break on unrelated `config.yaml` edits: compare with `config_hash` masked, or build from a fixture config. Source: [PR #21 round 2, confirmation list finding 3](https://github.com/allenvarghese05/desk-brief/pull/21#pullrequestreview-5914901602).
  - Add baseline rows to the lineage table, so T33 can trace a change to its baseline fetch (the reviewer flagged it "for T33"). Source: [PR #21 round 1, finding 9](https://github.com/allenvarghese05/desk-brief/pull/21#pullrequestreview-5914850524).
  - Add a 2s10s spread row (spec §9.1 sample). It must be derived from two official yields of the same ET date (never a `^TNX` tick minus an official 2Y), shown in bp, and labelled as derived. Source: [PR #21 round 1, deferred items](https://github.com/allenvarghese05/desk-brief/pull/21#pullrequestreview-5914850524).
- Acceptance: a test with mirrored consistency rows proves `health_summary` counts each cross-check once; the golden test passes after a config-only edit; a 2s10s test rejects a tick leg; a test per remaining bullet.

- Changelog: card created from the PR #18 and #21 reviews linked above; `tests/unit/test_query.py` added to Owns from [PR #22 round 3, finding 2](https://github.com/allenvarghese05/desk-brief/pull/22#pullrequestreview-5925742514).

#### T17 — Cross-check follow-ups
- **Depends:** T21 merged · **Parallel with:** T06–T09 (T22 owns different files)
- **Owns:** `src/deskbrief/crosschecks/{base.py,official_pair.py,yahoo_primary.py,close_official.py}`, their tests
- Deliverables (source: [PR #18 round 2](https://github.com/allenvarghese05/desk-brief/pull/18#pullrequestreview-5914906053), confirmation list):
  - Equity leg: exclude a Yahoo tick stamped exactly at the session close (filter `as_of < close`, not `<=`), so it cannot pair with the 15:59 bar and cause a spurious warn. Alternatively, keep it and document the effect in the module docstring (finding 2).
  - Ignore ticks before the session open, and give a pre-market-specific skip reason when no in-session tick exists (finding 3).
  - Add a test with the summer (CEST) fix-window layout: with a Yahoo FX tick from the P-012 fix-time run (12:15 UTC in summer, 13:15 UTC in winter), the FX leg pairs it and passes in both seasons; with no tick in the window, it skips with its specific reason (finding 1).
- Acceptance: tests for a tick at exactly the close, an early-close day, a pre-market-only day and the summer layout.

- Changelog: card created from the PR #18 round-2 review; summer test updated for P-012.

#### T18 — Make the test suite runnable in a sandbox
- **Depends:** T06, T07, T08, T09, T17, T07C, T09C merged (they own most of the test directories below; T07C owns `tests/unit/checks/*` and T09C owns `tests/brief_synth.py`) · **Runs last** among the open follow-up cards
- **Owns:** `tests/conftest.py` (add one repo-root `config.yaml` fixture or constant), the `[tool.pytest.ini_options]` table in `pyproject.toml`. It may make the path-only change to switch these files to that fixture: `tests/unit/test_config.py`, `tests/unit/test_models.py`, `tests/unit/pipeline/{pipeline_fakes.py,test_pipeline_catchup_rebuild.py}`, `tests/unit/{checks,crosschecks,store}/conftest.py`, `tests/unit/sources/test_{fred,twelvedata,alpaca,cboe,stooq}.py`, `tests/fault_injection/test_inj_checks_faults.py`, `tests/brief_synth.py`.
- Deliverables: find why pytest collection touches `.env.example` and stop it; resolve `config.yaml` from the repo root instead of the working directory; keep tests that build an `httpx` client from loading the system CA store when they use respx.
- Acceptance: `pytest -m "not integration"` passes from the repo root under a sandbox that denies `.env*` and the system keychain; CI unchanged.
- Why: reviewers on PRs #16–#21 could not run the suite locally and relied on CI alone. Source: the [PR #16 author response](https://github.com/allenvarghese05/desk-brief/pull/16#issuecomment-6906415315) ("I could not run pytest locally").

- Changelog: card created from the PR #16 author response; T07C and T09C added to Depends, and "Parallel with" replaced, because both own files this card path-edits (wave-3 cards, [PR #49 round-1 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/49)).

#### Live-probe gates (owner, before T24A's first scheduled run)
Every source card merged with its live probe PENDING and synthetic fixtures only. These are the per-source probes that phase gate G1 calls for, listed so none is missed. Before T24A ships, the owner runs each with real keys and records the result in `docs/sources/<source>.md`:
- Treasury: capture a real file and add the golden parse test the T10 card asks for. Source: [PR #10 round 2, finding 5](https://github.com/allenvarghese05/desk-brief/pull/10#pullrequestreview-5914408475).
- ECB: verify the URL, series key and no-results behaviour, and record the real 404 body and content type. Source: [PR #9 round 2, nit 2](https://github.com/allenvarghese05/desk-brief/pull/9#pullrequestreview-5914469527).
- Stooq: decide `enabled` from the probe. If blocked, set `enabled: false` (T13 card rule). Source: [PR #11 review, finding 1](https://github.com/allenvarghese05/desk-brief/pull/11#pullrequestreview-5914386201).
- Cboe: verify the URL. Source: [PR #11 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/11#pullrequestreview-5914386201).
- FRED, Alpaca, Twelve Data, Yahoo: one probe each. Sources: [PR #14](https://github.com/allenvarghese05/desk-brief/pull/14#pullrequestreview-5914397821), [PR #19, nit 5](https://github.com/allenvarghese05/desk-brief/pull/19#pullrequestreview-5914847211), [PR #17, nit 6](https://github.com/allenvarghese05/desk-brief/pull/17#pullrequestreview-5914845514), [PR #13](https://github.com/allenvarghese05/desk-brief/pull/13#pullrequestreview-5914461338).

- Changelog: list created from the reviews linked above; it adds no new gate beyond G1.

#### Owner decisions (ruled 2026-11-16)
The four open questions from the follow-up reviews are now ratified rulings in `docs/plan/decisions.md`:

| Topic | Ruling | Where it lands |
|---|---|---|
| Early-close rule | P-011: checks follow each calendar's early close | spec §8.1; T07 |
| FX at the ECB fix | P-012: Yahoo FX quote at the 14:15 Berlin fix every weekday; extra 12:15 UTC cron in summer, existing 13:15 UTC trigger in winter | spec §5.3, §5.4; T24B; T17 test |
| Morning cron form | P-013: two morning crons, one per fire time | spec §5.4; T24A |
| Agent-log location | P-014: one file per card, `docs/agent-log/T{id}.md` | workflow §8.2, §9; definition of done |


### Review follow-ups, wave 2 (from PRs #24–#35)

Non-blocking findings deferred in the second wave. Each bullet links its source. As before, a follow-up card may own files of a merged card, and no two open cards own the same file.

Order:
- T07A (contract) before T07B.
- T09B before T32B, and T32B before T33.
- T22B, T26 and T06D are independent of each other and of the rest.

#### T07A — `contract:` per-dataset intraday schedule
- **Depends:** T07 merged · **Blocks:** T07B
- **Owns:** `src/deskbrief/config.py` (one new model and one `Config` field only), as a planner-approved `contract:` PR (Contracts rule); `config.yaml` values via the planner (P-004); config tests
- Deliverables: `IntradaySchedule(source, dataset, calendar="XNYS", grace_minutes=30)` and `Config.intraday: tuple[IntradaySchedule, ...] = ()`, cross-referenced against feeds like `datasets`. Values: `alpaca/iex_latest`, `yahoo/quote` and `twelvedata/fx_quote`, each with 30 minutes grace (two missed 15-minute polls).
- Source: the T07 implementer's proposal in the [PR #27 description](https://github.com/allenvarghese05/desk-brief/pull/27) and the [PR #27 round-2 review, finding 1](https://github.com/allenvarghese05/desk-brief/pull/27#pullrequestreview-5932068771).

- Changelog: card created from PR #27.

#### T07B — Intraday tick-age freshness
- **Depends:** T07A · **Owns:** `src/deskbrief/checks/*`, `tests/unit/checks/*`
- Deliverables: age of the newest tick against `grace_minutes` for each `Config.intraday` entry. It runs only inside the session (early-close aware, P-011) and at least one grace period after the open, and gives `skip` otherwise. It uses the existing warn/fail ladder.
- Acceptance: tests for in-session fresh and stale ticks, pre-open, an early close and a holiday.

- Changelog: card created from PR #27 (the deferred T07 bullet).

#### T09B — Brief-model follow-ups 2
- **Depends:** T07, T09 merged · **Blocks:** T32B · **Owns:** `src/deskbrief/report/model.py`, `src/deskbrief/query.py`, `tests/unit/report/test_model.py`, `tests/unit/report/golden/*`, `tests/unit/test_query.py`, `tests/brief_synth.py`
- Deliverables:
  - Reuse T20/T07's anomaly logic for notable moves (the deferred T09 bullet).
  - `health_summary` and the cross-check items read only the latest run for each `target_date`. Re-runs keep both runs' rows under P-008. Source: [PR #32 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/32#pullrequestreview-5932131226).
  - `Row.dq_flag`: the worst check or cross-check status for the instrument on the report date, for T32B (P-015).
  - Typed numeric `value` and `value_unit` on derived rows (the 2s10s spread in bp), so writers stop parsing text. Source: [PR #33 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/33#pullrequestreview-5932128409).
  - `exclude_tiers=` on `latest_snapshot` and `overnight_changes`, so hosted mode falls back to a tier 2/3 value instead of hiding the instrument. Source: the T34 implementer's note in the [PR #37 description](https://github.com/allenvarghese05/desk-brief/pull/37).
  - A test that the 2s10s spread ignores a tier-2 `1d` yield. A future-dated quote shows a warning instead of "0 min old". A docstring on the spread's shared date. Source: [PR #28 review, findings 1–3](https://github.com/allenvarghese05/desk-brief/pull/28#pullrequestreview-5931993002).
- Acceptance: a test per bullet; `SCHEMA_VERSION` stays 1 if every new field has a default.

- Changelog: card created from the PR #28, #32, #33 and #37 sources linked above.

#### T32B — Excel columns before T33
- **Depends:** T09B · **Blocks:** T33 · **Owns:** `src/deskbrief/report/excel.py`, `tests/contract/test_excel_tables.py`, `tests/unit/report/test_excel.py`, `tests/xlsx_read.py`
- Deliverables:
  - Append `dq_flag` and `as_of_utc` to the five market tables exactly as spec §9.2 lists them (P-015).
  - Values for `dq_flag`: `fail`, then `warn`, then `pass`, taking the worst over the instrument's checks and cross-checks on the report date. `skip` counts as no result, and an instrument with no result gets `none`.
  - `as_of_utc`: ISO 8601 UTC text, for example `2026-11-16T11:58:00Z`, stored as text so VBA reads it the same everywhere. Blank when the value is unavailable.
  - Write typed derived values from T09B, and remove `_parse_bp`.
  - Export the placeholder label as a constant and set the placeholder's `status` to `empty`. Note in the T33 card handoff that macros skip the placeholder row.
  - Replace the FX-format magic number with a named constant.
  - Cover Notable, the IEX and fallback qualifiers, and lineage `is_fallback` in tests. Source: [PR #33 review, findings 2, 3, 5 and 6](https://github.com/allenvarghese05/desk-brief/pull/33#pullrequestreview-5932128409).
- Acceptance: the contract test pins the new headers; a negative-spread test.

- Changelog: card created from the PR #33 review and P-015.

#### T22B — Cross-check follow-ups 2
- **Depends:** T22 merged · **Owns:** `src/deskbrief/crosschecks/{fx_ecb.py,iex_sip.py}`, their tests
- Deliverables:
  - fx_vs_ecb: the Twelve Data price comes from the bar starting at 14:15, which closes about a minute after the fix. Use the bar ending at the fix, or document the offset. Give a specific Yahoo skip reason ("no ticks" vs "no tick within ±1m"). Pin the ±1-minute tolerance with ticks at +60 s, +61 s and −60 s. Add tests that rows ingested after `now_utc` are ignored and that the latest version wins. Compute `_is_derived` once per pair. Source: [PR #30 review, findings 1–5](https://github.com/allenvarghese05/desk-brief/pull/30#pullrequestreview-5932010935).
  - iex_vs_sip: add `left_label`/`right_label` saying IEX is a single venue and SIP is consolidated. Add a p95 nearest-rank test with 95 clean and 5 bad minutes. Clamp or comment `n_missing_both`. Source: [PR #29 review, findings 1–3](https://github.com/allenvarghese05/desk-brief/pull/29#pullrequestreview-5932008561).
- Acceptance: a test per bullet.

- Changelog: card created from the PR #29 and #30 reviews.

#### T26 — Futures roll calendar
- **Depends:** T22 merged · **Owns:** `src/deskbrief/crosschecks/proxy_exact.py`, its tests, a `crosschecks.roll_calendar` block in `config.yaml` via the planner (P-004)
- Deliverables: replace the ES_F and BRENT roll-window approximations with rules taken from the exchange calendars (CME for ES and CL, ICE for Brent, COMEX for GC), and model WTI and GOLD_F too. Record the source URL and date in `docs/sources/` (VERIFY by the owner).
- Acceptance: a window-length test per contract over a year, and each roll date checked against the cited calendar.
- Source: [PR #31 round 1, finding 1](https://github.com/allenvarghese05/desk-brief/pull/31#pullrequestreview-5932027859) and [round 2, nit 3](https://github.com/allenvarghese05/desk-brief/pull/31#pullrequestreview-5932099384).

- Changelog: card created from the PR #31 reviews.

#### T06D — Rebuild follow-ups
- **Depends:** T06 merged · **Owns:** `src/deskbrief/pipeline/clean.py`, `tests/unit/pipeline/test_pipeline_rebuild_safety.py`, `tests/fault_injection/inj_runner_rebuild.py`
- Deliverables:
  - The abort reason names the `clean/_flips/from-{previous}` marker key and the operator step, and the marker stores the flip time.
  - A test that a non-`KeyExistsError` claim-write failure gives `failed`, no flush and an unchanged pointer.
  - Optional cleanup of abandoned version directories.
- Source: [PR #35 review, findings 1–3](https://github.com/allenvarghese05/desk-brief/pull/35#pullrequestreview-5932290937).

- Changelog: card created from the PR #35 review.

### Review follow-ups, wave 3 (from PRs #37 and #40–#48)

Non-blocking findings deferred in the third wave. Each bullet links its source. As before, a follow-up card may own files of a merged card, and no two open cards own the same file.

Order:
- T09C before T34B (T34B uses T09C's `exclude_tiers` pass-through).
- T07C, T22C, T23C and T06E are independent of each other and of the other open cards.
- T18 runs after T07C and T09C, which own test files that T18 path-edits (see T18's Depends).
- T09C does not block T33. Until it merges, macros must not rely on `as_of_utc` for `UST_2S10S` (see the T33 card).

Not carried: the two PR #41 nits. The stale PR-body line needs no action, and the duplicate-entry message in `config.py` matches the `datasets` message and would need a `contract:` PR to change. Source: [PR #41 review](https://github.com/allenvarghese05/desk-brief/pull/41#pullrequestreview-5933282565).

#### T07C — Check follow-ups 3
- **Depends:** T07B merged · **Owns:** `src/deskbrief/checks/{feed_completeness.py,freshness.py,base.py}`, `tests/unit/checks/*`
- Deliverables:
  - `feed_completeness` covers `alpaca/iex_latest`: at least one tick per configured Alpaca instrument inside the XNYS session, with the same session, early-close and holiday handling as the Yahoo and Twelve Data tick rules. Alpaca's `as_of` is the last-trade time, so the rule asks only that the instrument appears, not that its tick is recent: an illiquid ETF must not fail. `freshness_intraday` keeps judging the feed as a whole. Source: [PR #46 review, finding 1](https://github.com/allenvarghese05/desk-brief/pull/46#pullrequestreview-5933420789).
  - A test for the `as_of <= now` guard in `newest_tick`: a tick with `as_of = now + 5 min` and `ingested_at <= now` is ignored, giving `no_ticks` or an older tick's age. Source: [PR #46 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/46#pullrequestreview-5933420789).
  - The `freshness_intraday` row of the docstring table in `checks/base.py` fits on one line. Source: [PR #46 review, finding 3](https://github.com/allenvarghese05/desk-brief/pull/46#pullrequestreview-5933420789).
  - This card's own agent log, `docs/agent-log/T07C.md`, records that `docs/agent-log/T07B.md` overstated `feed_completeness`'s coverage until this card. T07B.md itself is not edited. Source: [PR #46 review, finding 1](https://github.com/allenvarghese05/desk-brief/pull/46#pullrequestreview-5933420789).
- Acceptance: tests for an Alpaca instrument missing in session (fail), present but old (pass), pre-open, an early close and a holiday (skip); the guard test fails if the guard is removed.

- Changelog: card created from the PR #46 review.

#### T22C — Cross-check follow-ups 3
- **Depends:** T22B merged · **Owns:** `src/deskbrief/crosschecks/fx_ecb.py`, `tests/unit/crosschecks/test_xc_fx_ecb.py`
- Deliverables:
  - The docstring and the `BAR_LENGTH` comment say the bar-start convention is unverified until the Twelve Data live probe, and that `BAR_LENGTH` becomes 0 if `datetime` turns out to be the bar end.
  - The `_is_derived` test adds an ECB row for one derived cross and asserts that both source rows carry the same `right_is_derived`.
  - The Yahoo skip reason formats the tolerance from seconds (or from `TICK_TOLERANCE` directly), so a non-whole-minute tolerance prints correctly.
  - A comment that `n_yahoo_ticks` counts ticks in the ±10-minute window here, unlike `yahoo_vs_primary`.
- Source: [PR #42 review, findings 1–4](https://github.com/allenvarghese05/desk-brief/pull/42#pullrequestreview-5933261782).
- Acceptance: a test per behaviour bullet.

- Changelog: card created from the PR #42 review.

#### T09C — Report follow-ups
- **Depends:** T09B, T32B, T31B merged · **Blocks:** T34B · **Owns:** `src/deskbrief/report/{model.py,html.py,excel.py,notify.py}`, `src/deskbrief/query.py` (docstring only), `tests/unit/report/*`, `tests/unit/test_query.py`, `tests/brief_synth.py`, `tests/xlsx_read.py`
- Deliverables:
  - `Row.as_of` for the 2s10s spread: the shared date stamp of the two yields, so Excel `as_of_utc` is no longer blank for `UST_2S10S`. The Excel test asserts the new value. Source: [PR #47 review, findings 3 and 5](https://github.com/allenvarghese05/desk-brief/pull/47#pullrequestreview-5933580186).
  - The spread's tail says "change unavailable (no earlier common date)" when there is only one common date, so the HTML caption stops repeating the value. A test builds an n/a row through the real model path, not a hand-built description, and asserts the caption. Source: [PR #43 review, findings 1 and 2](https://github.com/allenvarghese05/desk-brief/pull/43#pullrequestreview-5933289273).
  - Excel: a test pins `as_of_utc` for one official daily row (the stored midnight-ET stamp), and the `AS_OF_UTC_FORMAT` comment says official values carry a date stamp, not a publish time. A test pins the FX four-decimal boundary with a rate near 150 next to a major pair. Source: [PR #47 review, findings 1 and 2](https://github.com/allenvarghese05/desk-brief/pull/47#pullrequestreview-5933580186).
  - `BriefModel.from_store` takes a keyword-only `exclude_tiers=()` and passes it to every `query.py` read that already accepts it, so the dashboard can build a hosted model with tier 1 excluded. Default `()` keeps every existing caller unchanged. Source: [PR #37 review, finding 1](https://github.com/allenvarghese05/desk-brief/pull/37#pullrequestreview-5932793791) (T09B added the parameter to `query.py` only).
  - The `health_summary` docstring and the Known-limits text say that a partial re-run supersedes both check tables for its date. Source: [PR #44 round-2 review, finding 1](https://github.com/allenvarghese05/desk-brief/pull/44#pullrequestreview-5933450119).
  - notify: the scrubbed `NotifyError` also has no `__context__` holding the raw exception. The concurrent-archive test flags `overwrite=True` instead of checking for a default value. Source: [PR #43 review, findings 3 and 4](https://github.com/allenvarghese05/desk-brief/pull/43#pullrequestreview-5933289273).
  - Optional: a `_COMPLETE` marker written last in each archive version, so a partial version is visible. Source: [PR #43 review, finding 5](https://github.com/allenvarghese05/desk-brief/pull/43#pullrequestreview-5933289273).
- Acceptance: a test per bullet; the Excel contract test is unchanged (no header changes); `SCHEMA_VERSION` stays 1.

- Changelog: card created from the PR #43, #44 and #47 reviews; `from_store` pass-through moved here from T34B, because T34B does not own `model.py` ([PR #49 round-1 review, finding 3](https://github.com/allenvarghese05/desk-brief/pull/49)).

#### T23C — Scorecard, serving and compaction follow-ups
- **Depends:** T23, T23B merged · **Owns:** `src/deskbrief/scorecard.py`, `src/deskbrief/pipeline/serving.py`, `src/deskbrief/store/compaction.py`, `tests/unit/test_scorecard.py`, `tests/unit/test_serving.py`, `tests/unit/test_compaction.py`, `tests/unit/t23_synth.py`, `docs/agent-log/T23B.md` (the stale "Decisions" line only)
- Deliverables:
  - The scorecard and the serving build share one DuckDB connection per job, re-reading the scorecard after its write. Measure the intraday serving step before and after and put both numbers in the PR. Source: [PR #45 round-2 review, finding 1](https://github.com/allenvarghese05/desk-brief/pull/45#pullrequestreview-5933525550).
  - The backfill test also asserts `scorecard_30d.parquet`. `parts` is a list literal. Source: [PR #45 round-2 review, findings 3 and 4](https://github.com/allenvarghese05/desk-brief/pull/45#pullrequestreview-5933525550).
  - The compaction window test checks day 6, the oldest included date, so `RECENT_DAYS = 6` fails it. A serving test with `now_utc` late in the UTC day (after 20:00 ET in winter) asserts the window counts back from the ET date. Source: [PR #48 round-2 review, findings 1 and 2](https://github.com/allenvarghese05/desk-brief/pull/48#pullrequestreview-5933765825).
  - `_compact_all` resolves the clean version once and passes `clean_version=` to every partition. Re-wrap the `serving.py` module docstring. Source: [PR #48 round-2 review, findings 4 and 5](https://github.com/allenvarghese05/desk-brief/pull/48#pullrequestreview-5933765825).
  - `docs/agent-log/T23B.md` says the last 7 ET days are compacted, and older partitions only by a manual call. Source: [PR #48 round-2 review, finding 3](https://github.com/allenvarghese05/desk-brief/pull/48#pullrequestreview-5933765825).
- Acceptance: a test per behaviour bullet; each mutation named above fails a test.

- Changelog: card created from the PR #45 and #48 reviews.

#### T06E — Rebuild follow-ups 2
- **Depends:** T06D merged · **Owns:** `src/deskbrief/pipeline/clean.py`, `tests/unit/pipeline/test_pipeline_rebuild_safety.py`, `tests/fault_injection/inj_runner_rebuild.py`
- Deliverables:
  - The marker test asserts that `flipped_at` equals the rebuild's injected clock.
  - The abort test asserts the full "delete clean/_flips/from-v1" phrase.
  - The abort reason adds: "if you did not roll back by hand, check `flipped_at` in the marker body first", since a rebuild killed between the claim and the pointer write leaves the same marker.
  - A comment on the parametrized claim-failure test says why its two cases assert different things.
  - Optional, carried from T06D: delete abandoned version directories. Abandoned means a `clean/v{N}/` that is not the version in `_CURRENT`, has no `clean/_flips/from-*` marker naming it as `new_version`, and was last written more than 7 days ago. If done, tests show that `_CURRENT`'s version, a flipped-to version and a recent in-progress version are never deleted. Source: the deferred-cleanup item in the [PR #40 review](https://github.com/allenvarghese05/desk-brief/pull/40#pullrequestreview-5933274186) acceptance list.
- Source: [PR #40 review, findings 1–4](https://github.com/allenvarghese05/desk-brief/pull/40#pullrequestreview-5933274186).
- Acceptance: a test per behaviour bullet.

- Changelog: card created from the PR #40 review.

#### T34B — Dashboard follow-ups
- **Depends:** T34, T09C merged · **Owns:** `app/Home.py`, `app/theme.py`, `app/components/*`, `tests/unit/app/{test_home.py,test_components.py,conftest.py}`
- Deliverables:
  - Hosted mode passes its hidden tiers to `BriefModel.from_store(..., exclude_tiers=...)` (T09C) in `build_snapshot`, so an instrument whose latest value is tier 1 falls back to tier 2 or 3 instead of disappearing. The `series_all` history keeps its current per-mode filter. The cached reader's key includes the hidden tiers, so hosted and local snapshots never share a cache entry. Source: [PR #37 review, finding 1](https://github.com/allenvarghese05/desk-brief/pull/37#pullrequestreview-5932793791).
  - `Home.py` also catches `duckdb.Error` and `OSError` as "Cannot read the data store", without echoing the message. A test covers an unreachable store, not just missing credentials. Source: [PR #37 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/37#pullrequestreview-5932793791).
  - The `theme.py` docstring stops promising stale greying. `overview()` in `app/components/data.py` appends dashboard limits to the model's `known_limits` for the Known limits expander: no stale greying, no z column, no curve panel, no live polling, and a 30-day sparkline instead of the spec's 1-day one. Source: [PR #37 review, finding 3](https://github.com/allenvarghese05/desk-brief/pull/37#pullrequestreview-5932793791).
  - `panels.py` raises `ValueError` instead of `assert`, and tests `row.change is not None` instead of the `"n/a"` text. Source: [PR #37 review, finding 4](https://github.com/allenvarghese05/desk-brief/pull/37#pullrequestreview-5932793791).
- Acceptance: AppTest cases for the hosted fallback and the unreachable store; a test per remaining behaviour bullet.

- Changelog: card created from the PR #37 review (the `query.py` half shipped in T09B); depends on T09C for the `from_store` pass-through, cache key and Known-limits location made explicit, from the [PR #49 round-1 review, findings 3 and 5](https://github.com/allenvarghese05/desk-brief/pull/49).

### Phase 2 — Scheduling, quality, cross-checks

#### T24A — `morning.yml` v0 (ship first)
- **Depends:** T19, T10, T11, T12, T13, T16, T05 · **Parallel with:** T20–T22, T30, T34
- **Owns:** `.github/workflows/morning.yml`, `docs/runbook.md`
- Deliverables: two crons, `15 10 * * 1-5` and `15 11 * * 1-5` (P-013; DST-safe, code gates) + `workflow_dispatch`; concurrency group; uv cache; secrets env; runs `deskbrief run morning`; pings healthcheck on success, `/fail` on failure; uploads logs as artifact (7-day retention, contains no provider data beyond counts).
- The job sets `GITHUB_EVENT_SCHEDULE: ${{ github.event.schedule }}` in its environment, so the morning gate's `scheduled_for` sees the exact fire (P-013). Test or dispatch evidence: a scheduled run's step summary shows the fire time it gated on.
- `docs/runbook.md` covers manual rollback of `clean/_CURRENT`: delete `clean/_flips/from-{previous}` before rebuilding from a rolled-back version. Source: [PR #35 review, finding 1](https://github.com/allenvarghese05/desk-brief/pull/35#pullrequestreview-5932290937).
- The job passes the T31 SMTP settings (`SMTP_USER`, `SMTP_APP_PASSWORD`, `REPORT_RECIPIENTS`, optional `SMTP_HOST`/`SMTP_PORT`) as secrets. Source: the [PR #36 description](https://github.com/allenvarghese05/desk-brief/pull/36).
- **Depends also on T05** (R2 checksum config). Gate: the owner runs `tests/integration/test_r2.py` with real keys **on `main` at or after the T05 merge**, and the commit SHA plus the pass output are pasted into the T24A PR description.
- Acceptance: workflow sets `DESKBRIEF_STORAGE_BACKEND=r2` (config default is `local` for dev); manual dispatch run green against real sources writes raw/clean/runs to R2 (screenshot/log excerpt in PR); job shows step summary. Report generation is a no-op until T31.

- Changelog: PR #6: depends on T05; R2 gate from the T02 review (PR #5) finding 5; gate wording from the PR #6 round-1 review; two morning crons from P-013; runbook and SMTP lines from PRs #35 and #36; `GITHUB_EVENT_SCHEDULE` mapping from [PR #23 round 2, finding 3](https://github.com/allenvarghese05/desk-brief/pull/23#pullrequestreview-5926563588).
#### T20 — DQ framework + single-source checks
- P-009: checks needing reproducible daily values read `obs_pit`, never `daily_close`, and exclude the current unfinished ET date.
- **Depends:** T02, T04 · **Parallel with:** T21, T30, T34
- **Owns:** `src/deskbrief/checks/*`, `tests/unit/checks/*`, `tests/fault_injection/inj_checks_*.py`
- Spec §8.1. Seven types: freshness, completeness, calendar, validity, uniqueness, anomaly, revision (consistency comes from T21). Each `Check.evaluate(ctx, duck) -> list[DQResult]`; registry; runner hook `run_checks(ctx)` writes `dq_results` and `revisions`.
- Acceptance: table-driven tests per check incl. holiday → `skip`; injected stale price → freshness fail; injected ×10 unit flip → validity/anomaly fail; FRED revision → revisions row + warn.

- Changelog: PR #6: P-009 line.
#### T21 — Cross-check framework + official_pair, yahoo_vs_primary, close_vs_official
- P-009: daily comparisons read `obs_pit` and exclude the current unfinished ET date; result details carry each side's `field`/`unit`.
- **Depends:** T02 · **Parallel with:** T20
- **Owns:** `src/deskbrief/crosschecks/{base.py,official_pair.py,yahoo_primary.py,close_official.py}`, tests
- Deliverables: `CrossCheck` protocol, bps math helpers (level diff for yields in bp; relative diff for prices), writes `crosscheck_results` + mirrored `consistency` dq rows.
- Acceptance: synthetic cases for pass/warn/fail at boundaries; missing side → `skip` with reason.

- Changelog: PR #6: P-009 line.
#### T22 — iex_vs_sip, proxy_vs_exact, fx_vs_ecb
- P-009: `fx_vs_ecb` labels the ECB side as "ECB reference rate" (`field=rate`), not a close; the comparison reads `obs_pit`.
- **Depends:** T21 · **Owns:** `crosschecks/{iex_sip.py,proxy_exact.py,fx_ecb.py}`, tests
- Acceptance: minute alignment (missing minutes excluded, counted); inverse-duration expected move math tested (TLT D=16.5, Δy=+10bp → −1.65%); direction-disagreement rule; 14:15 CET fix time across DST.

- Changelog: PR #6 round 2: P-009 line.
#### T23 — Scorecard, serving snapshot, compaction
- **Depends:** T20, T21, T06 · **Owns:** `src/deskbrief/{scorecard.py}`, `pipeline/serving.py`, `store/compaction.py`, tests; edits the `pipeline/jobs.py` serving step (allowed; shared file, see Review follow-ups)
- Deliverables: `scorecard_daily` from source_log + crosscheck disagreements; serving files (§6) written at end of every job; compaction with row-count verification before delete.
- Acceptance: scorecard numbers correct on synthetic log; compaction preserves all distinct versions; serving snapshot contains tier/as-of/source for every headline instrument.
- Cross-check rows carry the run's `target_date`; label them with `details.ref_date`, the date actually compared.

- Compaction must accept rebuild files named `run={run_id}-{YYYY-MM}.parquet` (T06 part 2; rows keep the real `run_id`). The scorecard reads only the latest run for each `target_date`. Source: [PR #35 review, finding 4](https://github.com/allenvarghese05/desk-brief/pull/35#pullrequestreview-5932290937) and [PR #32 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/32#pullrequestreview-5932131226).

- Changelog: depends on T06 (real runs write the check tables it reads), and the `jobs.py` serving-step edit made explicit, from [PR #22 round 2](https://github.com/allenvarghese05/desk-brief/pull/22#pullrequestreview-5925693019); `ref_date` labelling from [PR #18 round 2, finding 7](https://github.com/allenvarghese05/desk-brief/pull/18#pullrequestreview-5914906053) and [author response](https://github.com/allenvarghese05/desk-brief/pull/18#issuecomment-6906900231); rebuild file names and latest-run reading from the PR #35 and #32 reviews.

#### T24B — Remaining workflows
- **Depends:** T14, T15, T19 · **Owns:** `.github/workflows/{intraday,eod,sla,backfill,rebuild}.yml`; edits the intraday branch of `evaluate_gate` in `pipeline/jobs.py` (allowed; shared file, see Review follow-ups); only if needed for Yahoo-only enforcement, a small edit in `pipeline/runner.py` after T06 merges
- Acceptance: each dispatches green; intraday exits early outside session; sla fails if report missing; backfill accepts inputs.
- `sla.yml` catches a missing report for any cause, such as a morning run that never starts.
- From the [PR #24 review](https://github.com/allenvarghese05/desk-brief/pull/24#pullrequestreview-5931869851), findings 1–2:
  - `Gate` carries a source restriction that `run_job` applies, so the `--sources yahoo` flag is needed only on the 12:15 cron.
  - The intraday gate uses `scheduled_for`, and `intraday.yml` maps `github.event.schedule` into `GITHUB_EVENT_SCHEDULE`, with a delayed-run test (scheduled 12:15, now 12:40 CEST, proceeds).
- ECB-fix FX fetch (P-012):
  - `intraday.yml` adds cron `15 12 * * 1-5`.
  - The fix-window rule lives in one place, the intraday branch of `evaluate_gate`: a run proceeds outside the NYSE session when its `scheduled_for` time (wall clock if unknown, as for the morning gate) is within ±10 min of 14:15 Europe/Berlin on a TARGET business day. This check comes before the "not an XNYS business day" check, so it also covers US holidays when TARGET is open (Thanksgiving, July 4 and similar).
  - An exempted run fetches Yahoo only. The workflow passes `--sources yahoo` on the 12:15 trigger and on any trigger the gate exempts. If the gate also has to enforce Yahoo-only (it takes no `sources` argument today), the small `runner.py` change goes in this card with a note in the PR, merged after T06 or rebased on it. Yahoo's intraday `quote` dataset includes `EURUSD=X`.
  - Any other trigger outside the session exits `skipped` as today, including the 12:15 trigger in winter and on TARGET holidays.
  - No Twelve Data request is added: the leg reads the `fx_series_1m` bars from eod.
  - The exempted run passes through the T06 checks hook and the T23 serving hook. It must not raise a false freshness failure for the sources it did not fetch, and must not replace the latest serving view with a Yahoo-only one.
  - Tests:
    - a CEST date at 12:15 UTC and a CET date at 13:15 UTC each proceed and plan Yahoo requests only;
    - Thanksgiving (XNYS closed, TARGET open) at 13:15 UTC proceeds Yahoo-only;
    - a CET date at 12:15 UTC and a TARGET holiday are `skipped`;
    - a 13:45 UTC CET run outside the session is still `skipped`;
    - an exempted run leaves freshness results for other sources unchanged and the serving view intact.

- Changelog: late-cron safety net from the [PR #16 author's note on finding 4](https://github.com/allenvarghese05/desk-brief/pull/16#issuecomment-6906415315); summer FX run from [PR #18 round 2, confirmation list finding 1](https://github.com/allenvarghese05/desk-brief/pull/18#pullrequestreview-5914906053), ruled in P-012 (winter half from [PR #23 round 1, finding 1](https://github.com/allenvarghese05/desk-brief/pull/23#pullrequestreview-5926433510)); `evaluate_gate` edit for it; single window rule, US-holiday order, `scheduled_for` gating, Yahoo-only planning and hook safety from [PR #23 round 2, findings 1, 2, 4 and 6](https://github.com/allenvarghese05/desk-brief/pull/23#pullrequestreview-5926563588); `sla.yml` wording from [PR #23 round 1, finding 5](https://github.com/allenvarghese05/desk-brief/pull/23#pullrequestreview-5926433510); Gate source restriction and intraday `scheduled_for` from the PR #24 review.

### Phase 3 — Outputs

#### T30 — BriefModel
- P-009: the model labels each value by `field`/`unit` ("10Y yield", "ECB reference rate", "close"); "change since previous NYSE close" applies only to `field=close` instruments, others say "since previous official value"; values come from `obs_pit(report_time)`.
- **Depends:** T02 (uses §7.2 schemas; can test on fixture store before T20/T21 merge) · **Owns:** `src/deskbrief/report/model.py`, `src/deskbrief/query.py` (shared read API: `latest_snapshot`, `overnight_changes`, `health_summary`, `series`, `lineage`), tests
- Acceptance: for `field=close` instruments the overnight change uses the previous NYSE close (Monday → Friday); yields and ECB rates use the previous official value and are labelled per P-009 (test case for a yield and for an ECB rate); yields in bp, prices in %; known-limits list built from config labels + today's failures; model JSON-serializable; deterministic snapshot test.

- Changelog: PR #6: P-009 line and reworded overnight-change acceptance (round-1 and round-2 reviews).
#### T31 — HTML report + email
- P-009: labels rendered from the model's field/unit labels, never a hard-coded "close".
- **Depends:** T30 · **Owns:** `report/html.py`, `report/notify.py`, `report/templates/*`, tests; edits `pipeline/jobs.py` morning step (allowed)
- Acceptance: email-safe HTML (inline CSS, table layout, no JS); tier glyphs + proxy labels present; footer honesty line; `Notifier` protocol with `SmtpNotifier` and `NullNotifier` (dry-run); email failure → run `degraded` + alert, report still archived.
- Cross-check rows carry the run's `target_date`; render the model's `details.ref_date` label (T09 decides it), never `target_date`.

- Changelog: PR #6: P-009 line; `ref_date` labelling from [PR #18 round 2, finding 7](https://github.com/allenvarghese05/desk-brief/pull/18#pullrequestreview-5914906053) and [author response](https://github.com/allenvarghese05/desk-brief/pull/18#issuecomment-6906900231).
#### T32 — Excel `.xlsx`
- P-009: a `field`/`label` column in every data table; the About sheet explains close vs yield vs reference rate.
- **Depends:** T30 · **Owns:** `report/excel.py`, `tests/contract/test_excel_tables.py`
- Acceptance: sheet and table names/headers exactly as §9.2 (contract test — VBA depends on it); number formats; About sheet labels.

- Changelog: PR #6: P-009 line.
#### T33 — VBA `.xlsm`
- P-009: macros never relabel values; the `label`/`field` column from T32 is kept visible.
- **Depends:** T32, T32B, H6 · **Owns:** `vba/*`, `report/vba.py`, `tests/contract/test_vba_manifest.py`, CI step in `ci.yml` (allowed edit: add `vba-manifest` job)
- Handoff from T32B: macros skip rows whose `status` is `empty` (P-016). Until T09C merges, `as_of_utc` is blank for `UST_2S10S`, so macros must not rely on it for that row.
- Acceptance: `.bas` modules reference only §9.2 names; `.xlsm` produced when `vbaProject.bin` exists, skipped with warning otherwise; manifest check fails on drift; README macro-unblock note.

- Changelog: PR #6 round 2: P-009 line; depends on T32B and uses the spec §9.2 headers per P-015 ([PR #33 review, findings 1 and 4](https://github.com/allenvarghese05/desk-brief/pull/33#pullrequestreview-5932128409)); T32B handoff line from [PR #47 review, finding 3](https://github.com/allenvarghese05/desk-brief/pull/47#pullrequestreview-5933580186) and P-016.
#### T34 — Dashboard shell, query wiring, hosting
- P-009: the value component shows the field label next to tier and as-of.
- **Depends:** T02, T30 (`query.py`) — may start on `query.py` stub interface agreed in T30 PR · **Owns:** `app/Home.py`, `app/theme.py`, `app/components/*`, `.streamlit/config.toml`, `tests/unit/app/test_home.py`
- Deliverables: display-mode flag; read-only R2 connection with `st.cache_data(ttl=60)`; top bar (as-of, health badge, mode); Overview per §10.3 using serving files; tier glyph component; provenance drawer component; deployment notes for H7.
- Acceptance: AppTest smoke; hosted mode never renders tier-1 rows; every number rendered via the component that requires source/tier/as_of (no bare numbers).

- Changelog: PR #6: P-009 line.
#### T35 — Dashboard pages
- P-009: tooltips and drill-downs say "close", "yield" or "reference rate" per `field`.
- **Depends:** T34, T23 · **Owns:** `app/pages/*`, tests
- Acceptance: pages 1–6 per §10.3; AppTest smoke per page; follows `dataviz` + `minimalist-ui` skill guidance; screenshots attached to PR.
- Cross-check rows carry the run's `target_date`; render the model's `details.ref_date` label (T09 decides it), never `target_date`.

- Changelog: PR #6: P-009 line; `ref_date` labelling from [PR #18 round 2, finding 7](https://github.com/allenvarghese05/desk-brief/pull/18#pullrequestreview-5914906053) and [author response](https://github.com/allenvarghese05/desk-brief/pull/18#issuecomment-6906900231).
### Phase 4 — Next

#### T40 — Alerts
- **Depends:** T20 · **Owns:** `src/deskbrief/alerts.py`, tests. z-score moves, volume spikes, crosscheck fails, source down, stale → `alerts` table; surfaced in report "Notable" and dashboard.

#### T41 — Paper portfolio + reconciliation
- **Depends:** T02 · **Owns:** `src/deskbrief/portfolio.py`, `holdings.csv`, `broker_statement.csv`, tests. HYPOTHETICAL label everywhere; valuation with `daily_close`; reconciliation check type #9.

#### T42 — README, architecture doc, screenshots
- **Depends:** T31, T35 · **Owns:** `README.md`, `docs/architecture.md`, `docs/terms.md` (from H9). Only measured numbers.

### Phase 4B — Macro-desk data (rulings P-018 to P-021, 2026-11-19)

Order (never two open PRs owning the same file, §3):
- **Track A, in parallel from the start:** T43. It owns no file of track B.
- **Track B, one after another:** T44 (contract) → T45 (weekly cadence) → T46 → T47 → T48 → T49. T46–T49 each edit `config.yaml`.
- **Last:** T53 (history load, after T43 and T49), then T54 (dashboard views).

Rules for the phase:
- **Live probes:** keyless sources (Yahoo, Cboe, CFTC) are probed by the implementer. FRED needs `FRED_API_KEY`, which only the owner holds, so the PR gives the exact command and the owner runs it.
- **Spec:** the spec text for P-018 to P-021 is written by T53 alone. Until then the rulings take precedence (P-020).
- **Range support:** each source card implements `plan_range` for its own source (P-020), with a test over a multi-month range.

Changelog: T45's Owns widened to the value readers, and T43 names `scheduled_for_from_env`, after the [PR #73 confirmation review, findings 1–3](https://github.com/allenvarghese05/desk-brief/pull/73).

#### T43 — Cloudflare Worker cron trigger (P-018)
- **Depends:** none · **Owns:** `infra/cron-worker/` (Worker script, `wrangler.toml` without secrets, README with the deploy, secret and rotation commands), the `fire` and `slot` `workflow_dispatch` inputs and their mapping in `.github/workflows/{morning,intraday,eod,sla}.yml`, `trigger_from_env` and `scheduled_for_from_env` (which prefers `DESKBRIEF_SLOT`) in `src/deskbrief/pipeline/jobs.py`, the `slot` → `scheduled_for` mapping and the `duplicate fire` skip in `src/deskbrief/pipeline/runner.py`, tests.
- Acceptance:
  - **One trigger:** the Worker has a single Cron Trigger (`* * * * *`) and a UTC schedule table in code. A test parses the four workflows' `schedule:` crons and fails if the table differs.
  - **Inputs:** each dispatch passes `fire` (the exact cron string) and `slot` (the scheduled minute in UTC, computed from the table, not from when the Worker ran). The workflows read both through `env:`, never `${{ }}` inside `run:`. `GITHUB_EVENT_SCHEDULE` gets `fire` (so `intraday.yml`'s literal `15 12 * * 1-5` branch still works) and a separate `DESKBRIEF_SLOT` gets `slot`.
  - **Human dispatch unchanged:** a `workflow_dispatch` without `fire` and `slot` stays `Trigger.MANUAL` and ungated. Test covers it.
  - **Same as a schedule event:** a dispatch with `fire` and `slot` gets `Trigger.SCHEDULE`, `scheduled_for = slot`, and the same gate result as a schedule event for that slot. Tests cover a dispatched ECB-fix run that starts late.
  - **Duplicates (P-018's guarantee):** a pipeline run (`run_job`) whose job and `scheduled_for` match an `ok` or `degraded` `runs` row skips with reason `duplicate fire`. `sla.yml` is a read-only check that writes no `runs` row, so a duplicate sla run just checks again, which is harmless. Tests:
    - a GitHub morning event arriving hours after the Worker's run is skipped;
    - a late GitHub `*/15` intraday event resolves to a later slot and runs.
  - **Failures and secrets:** the Worker retries a non-2xx dispatch once, logs the HTTP status and never logs the token.
  - **Owner steps:** deploying the Worker and `wrangler secret put` are owner steps. After deployment the owner records one ECB-fix run's slot, dispatch and first-fetch times; the target is dispatch within 30 s of the slot.

#### T44 — Contract: history job, range planning, weekly cadence, new units and fields (P-020, P-021)
- **Depends:** none · **Owns:**
  - `src/deskbrief/models.py`: `JobKind.HISTORY`, so `ENUMS["job"]` follows.
  - `src/deskbrief/schemas.py`: `ENUMS` gains `interval` `1w`; `unit` `index` and `contracts`; `field` `net_position`, `open_interest` and `level`.
  - `src/deskbrief/config.py`: `Unit` gains `index` and `contracts`; `KNOWN_SOURCES` gains `cftc`; `DatasetSchedule` gains optional `release_weekday` (0 = Monday to 6), used by `1w` datasets and absent for the rest.
  - `src/deskbrief/cli.py`: `run` rejects `history` with "history is its own subcommand (card T53)" until T53 adds it.
  - `src/deskbrief/sources/base.py`: an optional `plan_range(start: date, end: date, ctx: RunContext) -> list[FetchRequest]` on the adapter protocol, with a helper reporting whether an adapter has it.
  - The `as_of` rule for `1w` in the `store/backend.py` docstring.
  - `config.yaml` `checks.validity`: `index: [-100, 100000]`, `contracts: [-10000000, 100000000]`.
  - `tests/contract/*`.
  - PR title `contract: …`; the body names card T44.
- Acceptance:
  - Contract tests pin the new values.
  - A `1w` row validates with `as_of` at 00:00 ET of its reference date.
  - An adapter without `plan_range` is reported as having no range support.
  - A `DatasetSchedule` with `release_weekday` validates; existing schedules are unchanged.
  - `deskbrief run history` exits with the message above and runs no pipeline step.
  - No source or check behaviour changes, and existing data still validates.

#### T45 — Weekly cadence and the new fields, wherever code reads values (P-021)
- **Depends:** T44 · **Owns:**
  - Checks: `src/deskbrief/checks/freshness.py`, `src/deskbrief/checks/completeness.py`, `src/deskbrief/checks/feed_completeness.py`.
  - The value readers: `VALUE_FIELDS`, `VALUE_INTERVALS`, `OFFICIAL_INTERVALS` and `field_label` in `src/deskbrief/query.py`; `DAILY_CLOSE_FIELDS` in `src/deskbrief/store/duck.py`.
  - The `("1d", "1mo")` special cases in `src/deskbrief/report/model.py`.
  - In the dashboard: `_MAIN_RANK` and the `build_bars` field list in `app/components/data.py`, and the interval cases and `_FIELD_TEXT` in `app/components/tearsheet.py`.
  - Tests.
- Acceptance:
  - A `1w` dataset's freshness expects a release on its `DatasetSchedule.release_weekday` (T44); a holiday-delayed release is `skip`, not `fail`.
  - Completeness counts weeks.
  - The report and the dashboard show a weekly value's date without a time and label its change as week-on-week.
  - End to end: a synthetic store with a `level` row (daily) and a `1w` row each yields a value, a baseline change and a P-009 label in `BriefModel`, and bars in `build_bars`. `net_position` and `open_interest` read as values with labels "net speculative position" and "open interest".
  - Weekly series get no anomaly check (`checks/anomaly.py` reads `1d` only); this is stated in the known limits.
  - Tests use synthetic rows only.

#### T46 — FRED rates, dollar, credit and conditions (P-021a)
- **Depends:** T44, T45 · **Owns:** `src/deskbrief/sources/fred.py` (`SERIES_SPECS` entries and `plan_range`), the FRED feeds and new instruments in `config.yaml`, `docs/sources/fred.md`, tests.
- Series:

  | FRED id | Field | Unit | Interval | Asset class |
  |---|---|---|---|---|
  | `DFII10` (10Y TIPS real yield) | `yield` | `pct` | `1d` | rates |
  | `T10YIE` (10Y breakeven) | `yield` | `pct` | `1d` | rates |
  | `DTWEXBGS` (broad dollar) | `level` | `index_pts` | `1d` | fx |
  | `BAMLH0A0HYM2` (HY OAS) | `rate` | `pct` | `1d` | macro |
  | `NFCI` | `level` | `index` | `1w` | macro |

  The daily series use dataset `series` and its existing freshness entry (`lag_bdays: 1`). NFCI uses a new dataset, `series_weekly`, which `fred.py` plans for weekly series. Freshness is keyed by source and dataset, so NFCI gets its own entry with `release_weekday` set from FRED's release calendar (confirmed at the probe), using T45.
- Acceptance:
  - Each series is planned, parsed and labelled (P-009), and appears in the report model and the dashboard.
  - A negative NFCI value passes validity, and so does a TIPS yield down to −2%.
  - `plan_range` covers 2 years in one request per series.
  - The owner-run live probe is recorded.

#### T47 — Cboe volatility indices (P-021b)
- **Depends:** T46 (`config.yaml`) · **Owns:** `src/deskbrief/sources/cboe.py` (generalize from VIX to a per-symbol URL and column mapping; `plan_range`), the Cboe feeds and new instruments in `config.yaml`, `docs/sources/cboe.md`, tests.
- Acceptance:
  - VIX9D, VIX3M, VIX6M, GVZ, VXN and SKEW are fetched daily (tier 3, `asset_class` `vol`, unit `index_pts`, field `close`).
  - Each symbol's CSV layout, SKEW's included, is confirmed by a live probe and pinned by a synthetic fixture.
  - VIX behaviour is unchanged.

#### T48 — Yahoo daily history for ETFs and single stocks (P-019)
- **Depends:** T47 (`config.yaml`) · **Owns:** `src/deskbrief/sources/yahoo.py` (`plan_range` for the `history` dataset; symbol mapping if needed), the Yahoo `history` feeds in `config.yaml`, `src/deskbrief/crosschecks/close_official.py`, `src/deskbrief/crosschecks/proxy_exact.py`, `docs/sources/yahoo.md`, tests.
- Acceptance:
  - Every ETF and single stock has a Yahoo `history` feed (tier 2, `is_fallback = false`). Live probe recorded.
  - `close_vs_official` compares the Alpaca SIP last 1m bar before the close with the Yahoo daily close.
  - `proxy_vs_exact` uses the proxy ETF's Yahoo daily return, with `details` saying "same provider" when both legs are Yahoo.
  - A synthetic-store test shows SPY and MSFT getting bars through `app/components/data.build_bars`.
  - The Yahoo `history` freshness entry covers the new feeds (checked, extended if needed). The existing `daily_fallback` behaviour is untouched.
  - Alpaca datasets are unchanged (P-003).

#### T49 — CFTC Commitments of Traders source (P-021c)
- **Depends:** T44, T45, T48 (`config.yaml`) · **Owns:**
  - `src/deskbrief/sources/cftc.py`, including `plan_range`.
  - Its entries in `sources/registry.py` and `sources/catalog.py`.
  - In `config.yaml`: the `cftc` source settings, freshness entry, feeds and instruments. Ids `COT_GC_NET`, `COT_ES_NET`, `COT_ZN_NET` take field `net_position`; `COT_GC_OI`, `COT_ES_OI`, `COT_ZN_OI` take field `open_interest`. All use unit `contracts`, `asset_class` `macro` and `kind` `macro`.
  - `docs/sources/cftc.md`.
  - Tests with a synthetic fixture.
- Acceptance:
  - Weekly rows are stored with `interval` `1w` and `as_of` = 00:00 ET of the reference Tuesday. The PR names the report variant (legacy or disaggregated) and gives the reason.
  - Its freshness entry sets `release_weekday: 4` (Friday), using T45.
  - User-Agent and rate limit are set, the live probe is recorded, and unit tests use no network.

#### T53 — Two-year history load, and the phase's spec text (P-020)
- **Depends:** T43, T49 · **Owns:** `src/deskbrief/pipeline/history.py` (new), a `history` subcommand in `src/deskbrief/cli.py`, `.github/workflows/history.yml` (new; own concurrency group), `plan_range` in `src/deskbrief/sources/treasury.py` and `src/deskbrief/sources/ecb.py`, `docs/runbook.md` (trigger and history sections), `docs/specs/2026-11-15-desk-brief-design.md` (D3, §5.2, §5.4, §7.2, §8.2 and the P-012 known limit, following P-018 to P-021), tests.
- Acceptance:
  - **Coverage:** one `history` run loads 2 years through each adapter's `plan_range`, within rate limits and budgets. Feeds without range support (Twelve Data `fx_series_1m`, Alpaca) are listed and skipped.
  - **Files:** one `observations` file per touched date partition. A test over N dates asserts it.
  - **Re-runs:** keys already stored are skipped, so a re-run writes nothing twice. Test covers it.
  - **Isolation:** no checks, scorecard or serving, and the concurrency group is not `deskbrief-pipeline`.
  - **Point in time:** rows keep their real `ingested_at`, so `obs_pit(t)` before the load does not see them.
  - **Owner step:** dispatch writes to production R2, so the owner runs it. The PR includes a dry-run plan with requests per source and estimated rows.

#### T54 — Dashboard views for the new data (follow-up)
- **Depends:** T53 · **Owns:** `app/**`, `tests/unit/app/**`.
- Acceptance:
  - A VIX term-structure panel (9D, 30D, 3M, 6M).
  - The yield curve's change by tenor against 1W and 1M, plus a 2s10s history line.
  - Gold against the 10Y real yield.
  - A 90-day correlation heatmap.
  - CFTC net positioning with its weekly date.
  - Every figure goes through `value_cell`, and hosted mode is unchanged.

### Review follow-ups, Phase 4B (from PRs #74–#82)

Non-blocking findings deferred from the Phase 4B reviews, in one card (T55). Each bullet links its source.

Not carried:
- **CFTC holiday over-count.** T45's holiday rule over-counts Monday holidays and Good Friday, so CFTC freshness warns a day late in those weeks. It never gives a false fail, and `docs/sources/cftc.md` documents it. Source: [PR #80 review, finding 3](https://github.com/allenvarghese05/desk-brief/pull/80#pullrequestreview-5984358141).
- **Unflushed DQ row in the history job.** `_unexpected_empty_day` buffers a DQ row with today's date that is never flushed. It is harmless. Source: [PR #81 review, nit 4](https://github.com/allenvarghese05/desk-brief/pull/81#pullrequestreview-5984647185).
- **Per-source flush in the history job.** A killed load (timeout or SIGTERM) loses its unflushed rows. Flushing per source would write a partition twice in one run, which P-008 forbids without a `contract:` change adding a `part` to `table_key`. The load takes about 5–10 minutes against a 60-minute timeout, and a re-run refetches only what is missing, so the risk does not justify a contract change. Source: [PR #81 review, finding 1](https://github.com/allenvarghese05/desk-brief/pull/81#pullrequestreview-5984647185).
- **CFTC owner probe.** The owner may run the documented keyless one-liner in `docs/sources/cftc.md` to record the HTTP status. The server-computed counts are already exact, and the first scheduled CFTC run (Friday 15:30 ET) shows the live status in `source_log`. Source: [PR #80 review, finding 1](https://github.com/allenvarghese05/desk-brief/pull/80#pullrequestreview-5984358141).
- **Cboe 20:00 ET freshness.** It rests on one observation. The `cboe/vix_history` freshness results from the first scheduled runs confirm it or show the time to move, and that needs no card until they do. Source: [PR #78 review, nit 4](https://github.com/allenvarghese05/desk-brief/pull/78#pullrequestreview-5982833275).
- **Price returns on ex-dividend days.** The unadjusted close gives price returns, not total returns. That is by design, because `close_vs_official` compares traded prices, and the T48 PR lists it as a risk. Source: [PR #79 review, nit 4](https://github.com/allenvarghese05/desk-brief/pull/79#pullrequestreview-5983287678).
- **Tooltip test scope.** The provenance test checks only tooltips that show a numeric value, which is the rule as written. Source: [PR #82 round 2, nit 1](https://github.com/allenvarghese05/desk-brief/pull/82#pullrequestreview-5985302516).
- **Size and model-name nits** in the agent logs and PR bodies: [PR #74](https://github.com/allenvarghese05/desk-brief/pull/74#pullrequestreview-5971101281), [PR #77](https://github.com/allenvarghese05/desk-brief/pull/77#pullrequestreview-5981837291), [PR #80](https://github.com/allenvarghese05/desk-brief/pull/80#pullrequestreview-5984358141), [PR #82](https://github.com/allenvarghese05/desk-brief/pull/82#pullrequestreview-5985302516). No action.

#### T55 — Phase 4B report, test and doc follow-ups
- **Depends:** T54 merged · **Owns:**
  - `src/deskbrief/report/excel.py` and `tests/unit/report/*`;
  - `tests/unit/crosschecks/test_xc_t48_yahoo_history.py`;
  - `tests/unit/sources/{test_cftc_t49.py,test_yahoo_t48.py}`;
  - a new `tests/unit/test_parser_version_dedup.py`;
  - `docs/sources/{cboe.md,fred.md}`;
  - `config.yaml`, for the Yahoo quote feeds below only;
  - `.github/workflows/history.yml`, `tests/unit/test_history_workflow_t53.py`, and `docs/runbook.md` §5 History load only;
  - `tests/contract/test_t44_macro_contract.py`, for the rename below only.
- Deliverables:
  - **Excel contracts change.** The Excel workbook shows a weekly position change as whole contracts with thousands separators ("−10,000"), not "−10000.0" in a `pts` format. Choose the number format from the row's value unit. The `change_unit` column keeps reading "pts" for these rows, because changing its value is a contract change (P-015). Pin that text in the test, and list a `contract:` follow-up in the PR. If the format fix itself turns out to need a new `change_unit` value, stop and ask the planner. Source: [PR #76 round 2, nit 1](https://github.com/allenvarghese05/desk-brief/pull/76#pullrequestreview-5981261285).
  - **Excel About sheet.** It explains:
    - the new fields `level`, `net_position` and `open_interest`;
    - the units `index` and `contracts`;
    - that a weekly (`1w`) value is dated to its reference day, for example a CFTC Tuesday.

    The About sheet on main explains only close, last, yield, rate, spread and the tiers. Source: [PR #76 description, Risks and follow-ups](https://github.com/allenvarghese05/desk-brief/pull/76).
  - **Hosted-mode test.** `close_vs_official` stays withheld in hosted mode now that it has real results: a test over `exposed_sources` plus `keep_rows`. Source: [PR #79 review, finding 1](https://github.com/allenvarghese05/desk-brief/pull/79#pullrequestreview-5983287678).
  - **In-progress bar test.** A Yahoo history test fetched during the session drops the in-progress ETF bar over a multi-day range. Source: [PR #79 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/79#pullrequestreview-5983287678).
  - **Re-parsed VIX row.** A test writes a VIX row at `parser_version` 1 and a re-parsed row at version 2 for the same key. It asserts one value in `obs_latest`, the query snapshot, `BriefModel` and `build_bars`. Source: [PR #78 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/78#pullrequestreview-5982833275).
  - **CFTC change text.** The CFTC report-model test asserts `change_text`, for example "+150 contracts", plus a negative grouped case such as "−1,234 contracts". Source: [PR #80 review, finding 2](https://github.com/allenvarghese05/desk-brief/pull/80#pullrequestreview-5984358141).
  - **Probe tables.**
    - The Cboe probe table in `docs/sources/cboe.md` takes the owner's exact run from [the PR #78 comment](https://github.com/allenvarghese05/desk-brief/pull/78#issuecomment-7021366889): status, rows, newest, min and max.
    - The FRED table in `docs/sources/fred.md` takes the owner's FRED probe output if the planner has it by then. Otherwise it stays marked pending.

    Sources: [PR #78 review, finding 1](https://github.com/allenvarghese05/desk-brief/pull/78#pullrequestreview-5982833275) and [PR #77 round 2, note 1](https://github.com/allenvarghese05/desk-brief/pull/77#pullrequestreview-5981984243).
  - **History workflow secrets.** `history.yml` moves the R2 and FRED secrets from job-level `env` to the "Run history" step only, so the validate and `uv sync` steps don't see them. The `IN_*` input variables stay at job level. Rewrite the job-level assertion in `test_conventions_secrets_and_pins` to match. Source: [PR #81 review, nit 3](https://github.com/allenvarghese05/desk-brief/pull/81#pullrequestreview-5984647185).
  - **Runbook §5 wording.** It says a re-run completes a degraded run or a handled crash. A killed process (timeout or SIGTERM) loses its unflushed rows, and the next run refetches them. Source: [PR #81 review, nit 2](https://github.com/allenvarghese05/desk-brief/pull/81#pullrequestreview-5984647185).
  - **Test name.** Rename `test_weekly_row_validates_at_midnight_et_of_reference_date`, which checks only that the schema accepts the row, so its name says what it checks. Source: [PR #74 review, nit 1](https://github.com/allenvarghese05/desk-brief/pull/74#pullrequestreview-5971101281).
  - **Overnight moves for the new vol indices.** Yahoo `quote` feeds (tier 2) for VIX9D, VIX3M, VIX6M, VXN, GVZ and SKEW, like VIX's `^VIX`, so they get an overnight move. Add only the symbols the implementer's live probe confirms Yahoo serves. List the rest in the PR. Source: [PR #78 review, nit 3](https://github.com/allenvarghese05/desk-brief/pull/78#pullrequestreview-5982833275).
- Acceptance:
  - An Excel test pins a contracts change cell's number format and value, and its `change_unit` text.
  - The About sheet test names each new field and unit.
  - Each new test fails if the behaviour it guards is removed.
  - The probe tables hold exact values, not estimates.
  - Config validates.
  - The PR lists which vol symbols were added and which were not, with the probe evidence.
  - The history workflow has no `secrets.` reference outside its run step, tested in `test_history_workflow_t53.py`.
- Changelog: card created from the PR #74–#82 reviews linked above. Citations, the `change_unit` note, the secrets-test note and the test rename were added after the [PR #83 round-1 review](https://github.com/allenvarghese05/desk-brief/pull/83).

### Phase 4C — Dashboard v2 (ruling P-022, 2026-11-19)

The owner approved a clickable mockup of the new Home page. It is the visual reference, kept as a labelled synthetic fixture at `tests/fixtures/design/home-mockup-2026-11-19.html` (open it in a browser). The cards describe the same design in words, and the cards win where the two differ.

Order: T56 → T57 → T57B → T58 → T59, one after another, so no two of these cards are open at once.
- T56 provides the cache key and the synthetic store.
- T57 builds the theme, provenance and navigation.
- T57B builds the chart component.
- T58 lays out Home.
- T59 applies the design to the other pages.

Rules for the phase:
- **Provenance:**
  - Every figure keeps source, tier, as-of and field in its markup (P-009, spec §10.2, P-022).
  - Hosted mode never shows or reads tier 1, and every new reader is tested hosted against local on the same store.
  - Generated markup has no `title` attributes.
- **Synthetic store and screenshots:**
  - T56 adds `tests/bench/synth_store.py`. It builds a synthetic store with about 2,000 observations files under a given directory, with one documented command.
  - Each later card's PR includes screenshots at about 1500 px and about 390 px wide, taken against that store (never real provider data), alongside the mockup fixture for comparison.
- **Timings:**
  - Timings are measured with the T56 benchmark on the implementer's machine, and the PR states the machine.
  - Targets are relative to the pre-T56 baseline measured the same way.
- **Tests across cards:** a card may update tests outside its Owns only where its own change breaks them mechanically: markup, signatures, fakes. It lists each such file under "Files outside Owns".
- **No regressions:** every piece of information the T36/T54 pages show stays reachable. T58's inventory table names where each item goes.

#### T56 — Dashboard data layer: one connection, rebuild only on change, incremental cache
- **Depends:** T55 merged · **Owns:**
  - `app/components/data.py`;
  - `src/deskbrief/store/table_cache.py`;
  - `src/deskbrief/store/duck.py` (dashboard connection options only);
  - `tests/unit/store/{test_table_cache.py,test_duck_views.py}`;
  - `tests/unit/app/test_data_layer.py` (new);
  - `tests/bench/` (new).
- Deliverables:
  - **One connection.** `build_snapshot` uses the shared connection. There is no private second connection. The public signatures of `build_snapshot` and `open_connection` stay compatible.
  - **Rebuild only on change.** The shared connection is rebuilt only when the store's listing has changed. The listing is checked at most once every 60 s, with one LIST per table. A rebuild never blocks queries on the current connection: the old connection serves until the new one is ready, then is swapped in and closed.
  - **Incremental, compaction-safe table cache.**
    - New keys are downloaded and appended.
    - When any key of an ET-date partition disappears (eod compaction deletes the partition's files and writes a compacted file, P-017), that whole partition is evicted and re-read.
    - Loads are single-flight per table.
  - **No unused views.** The dashboard connection skips the scorecard `read_parquet` view and its LIST and httpfs setup.
  - **A data version.** `data.data_version()` returns a fingerprint of the listing. `data.py`'s caches are keyed on it with a 15-minute TTL.
  - **A synthetic store and a benchmark.**
    - `tests/bench/synth_store.py` builds the synthetic store (see the phase rules).
    - `tests/bench/bench_dashboard.py` measures cold build, warm query, and rebuild after one new file, on that store.
    - The PR gives the before and after numbers.
- Acceptance:
  - One new file means exactly one file downloaded (test).
  - A compacted partition is evicted and re-read, with no duplicate or stale rows, and row counts equal a fresh load (test).
  - Two concurrent loads download each key once (test).
  - No listing change means no rebuild (test).
  - Queries during a rebuild use the old connection (test).
  - The warm query and the rebuild-after-one-file are each at least 5× faster than the pre-T56 baseline on the benchmark.
  - The existing hosted-mode tests pass.
- Changelog: card created from P-022 and the 2026-11-19 performance study. Compaction handling, test Owns and the benchmark were added after the [PR #85 round-1 review, findings 3, 4 and 9](https://github.com/allenvarghese05/desk-brief/pull/85).

#### T57 — Dashboard v2 foundation: theme, compact provenance, in-session navigation
- **Depends:** T56 merged · **Owns:**
  - `app/Home.py`, which becomes the `st.navigation` router;
  - `app/views/markets.py` (new; today's Home content moved here unchanged except for links);
  - `app/theme.py` and `.streamlit/config.toml`;
  - `app/components/{values.py,kit.py,panels.py}`;
  - the `_link` helper in `app/components/macro.py`;
  - `tests/unit/app/{test_components.py,test_navigation.py}` (the second is new).
- Deliverables:
  - **Theme.** Design tokens in `theme.py` and `config.toml`:
    - page `#0B0D10`, panel `#13161B`, raised panel `#181C22`, divider `#22262E`;
    - text `#E8EAED`, secondary `#9AA0A6`, meta `#6B7078`;
    - up `#2EBD85`, down `#F6465D` (tints at about 13%), accent `#5B9DFF`;
    - failure and warning markers in amber with ✕ / "failed" and "warn" (P-022);
    - Inter with tabular figures;
    - type sizes of about 40, 26, 14 and 12 px;
    - card radius 12 px, card padding 24 px, gaps 20 px.

    No card borders, no boxes inside boxes, and the old CSS is removed.
  - **Compact provenance.**
    - `value_cell` and `change_cell` gain `compact=True`, opt-in until T59 converts the remaining pages. It renders a tier glyph with a CSS tooltip giving field, tier, source and as-of. The tooltip shows on hover, focus and tap, and the element is focusable.
    - The words proxy, IEX, fallback and HYPOTHETICAL stay visible.
    - The accessible label carries the full provenance.
    - A details panel (`st.dialog`) shows the full lineage when a source is clicked.
    - `kit` provides a glyph-legend helper.
  - **Navigation.**
    - `app/Home.py` becomes the router: `st.navigation([...], position="top")` registers `app/views/markets.py` and each `app/pages/*.py` as `st.Page`.
    - In-app selection (tiles, watchlist, moves, overview cards, nav, health badge, and Macro's `_link`) uses buttons or callbacks that set `st.query_params`, or `st.switch_page` / `st.page_link` with `query_params`. There is no `<a target="_self">`.
    - `?id=` deep links work on every page.
    - The default sidebar page list and its toggle are hidden.
  - **Caches.** The caches in `kit.py` are keyed on `data.data_version()`.
- Acceptance:
  - An AppTest navigation test: clicking an instrument sets `query_params["id"]` without a new session, so session state persists, and a deep link `?id=X` opens X.
  - No `<a target="_self">` and no `title` attribute appear in the generated markup (test).
  - Every tier has its glyph, its tooltip text, a focusable element and its accessible label (test).
  - Hosted-versus-local tests pass.
  - Component screenshots are in the PR.
- Changelog: card created from P-022. The router split, the link emitters, deep links, opt-in compact mode and the AppTest test were added after the [PR #85 round-1 review, findings 5, 11 and 13](https://github.com/allenvarghese05/desk-brief/pull/85).

#### T57B — TradingView chart component
- **Depends:** T57 merged · **Owns:**
  - `app/components/tvchart.py` (new);
  - `app/components/tvchart_frontend/`: a hand-written `index.html` and a glue JS file using the Streamlit component message protocol (ready, render, frame height), plus the vendored `lightweight-charts.standalone.production.js` at a pinned version with its `LICENSE`, `NOTICE` and a `SHA256SUMS` file;
  - `app/components/chart.py`;
  - `tests/unit/app/test_tvchart.py` (new).
- Deliverables:
  - **The component.** `components.declare_component(path=…)` serves the static folder. There is no npm build, no CDN and no `pyproject.toml` change. The JS is fetched once per browser, not inlined per rerun.
  - **The chart.** `tvchart.chart(...)` renders:
    - candles, a line or area, and an optional compare series;
    - a time scale with no weekend gaps;
    - crosshair, last-price line, and zoom and pan;
    - green up and red down;
    - a container-fit width.

    Its tooltip shows field, tier, source and as-of. The chart sits inside `st.fragment`, so range and style changes rerun only the chart.
  - **Attribution.** `tvchart.attribution()` renders the "Charts by TradingView" link, and every page that draws a tvchart calls it.
  - **Gitleaks.** If gitleaks flags the minified bundle, an allowlist entry is added for that path only, with a reason.
- Acceptance:
  - The checksum of the vendored file matches `SHA256SUMS`, and `LICENSE` and `NOTICE` are present (test).
  - The series and options payload is unit-tested for candles, line and compare.
  - Attribution is present wherever `tvchart.chart` is used (test).
  - Screenshots are in the PR.
  - The PR states that Dependabot does not track the vendored file, and how to upgrade it.
- Changelog: card split out of T57 after the [PR #85 round-1 review, findings 7 and 8](https://github.com/allenvarghese05/desk-brief/pull/85).

#### T58 — Home page v2 (the approved mockup), stats that respect data gaps
- **Depends:** T57B merged · **Owns:**
  - `app/views/markets.py`;
  - `app/components/{panels.py,tearsheet.py,instrument.py}`;
  - `tests/unit/app/{test_home.py,test_tearsheet.py,conftest.py}`;
  - `docs/specs/2026-11-15-desk-brief-design.md` §10.2 and §10.3 only.
- Deliverables:
  - **Layout,** as in P-022 and the mockup fixture:
    - the top bar, then the headline strip (value, change pill, sparkline, glyph; the selected one outlined);
    - a watchlist rail grouped by asset class, with full names, no raw ids, no truncation at about 1500 px, and σ tags for notable moves;
    - the tvchart, about two-thirds of the centre, with name, price, change pill and provenance in its header, range pills (1M 3M 6M 1Y All), and a Candles/Line toggle. Compare and CSV go in a small menu. One row of 1D to 1Y changes sits under the chart.
    - a right rail with a single "Do the sources agree?" verdict and its sources, and risk and range as label-value rows with a labelled 52-week bar;
    - tabs for All values, Lineage, Checks and Corrections;
    - all figures with `compact=True`.
  - **Gap-aware stats.**
    - Horizons refuse a base close older than the horizon allows and show "no data".
    - Realised vol, sigma and drawdown skip moves across gaps longer than 5 business days and say so.
    - Date spans include the year when they cross a year.
  - **Caches.** The caches in `instrument.py` and `tearsheet.py` are keyed on `data.data_version()`.
  - **An inventory table** in the PR, listing every item on today's Home and where it now lives:
    - three-tier ladder and "Do the sources agree?" → right-rail verdict;
    - 14-day cross-check strip and "Known limits" → Checks tab;
    - withheld-instruments caption, status and mode chips → top-bar status dot and the page footer;
    - chart and compare captions (sources, tiers, date span) → a chart footer line;
    - "Every value we hold" → All values tab;
    - lineage, checks and corrections → their tabs;
    - biggest moves → σ tags;
    - yield curve and the 30-day small multiples → Macro (link from Home).

    Anything else is listed too.
  - **Spec.** §10.2 and §10.3 are rewritten to match P-022.
- Acceptance:
  - Tests pin every layout block and every inventory row at its new location.
  - A data-gap fixture shows horizons, vol and drawdown do not cross the gap.
  - No `title` attribute appears (test).
  - Hosted-versus-local tests pass.
  - Desktop and phone screenshots sit beside the mockup fixture in the PR.
  - A warm-cache first render is at least 3× faster than the pre-T56 baseline on the T56 benchmark store.
- Changelog: card created from P-022 and the 2026-11-19 study (the candle chart, the stuck tooltip, stats across gaps). Cache keys, the inventory and relative timings were added after the [PR #85 round-1 review, findings 6, 9 and 10](https://github.com/allenvarghese05/desk-brief/pull/85).

#### T59 — Other pages in the v2 design
- **Depends:** T58 merged · **Owns:** `app/pages/*.py`, `app/components/{macro.py,incidents.py}`, `tests/unit/app/{test_pages.py,test_macro.py}`.
- Deliverables:
  - Macro, Cross-checks, Data health, Sources and Runs use the theme, `compact=True` provenance, the glyph legend and in-session navigation.
  - Each page follows the identical card template: title on the left, controls on the right, content, then an optional footer.
  - Each page shows 4–6 blocks, with depth in tabs.
  - Macro's time series use `tvchart` with attribution. The heatmap may stay Altair.
  - Every cache, including `4_Data_health.py`'s, is keyed on `data.data_version()`.
  - Once every page uses it, `compact=True` becomes the default.
- Acceptance:
  - The page tests pass, updated for the new layout.
  - Hosted-versus-local tests hold.
  - Each page has desktop and phone screenshots in the PR.
  - No `<a target="_self">` and no `title` attributes appear (test).
- Changelog: card created from P-022.

### Phase 4D — Dashboard v3, no containers (ruling P-023, 2026-11-23)

The owner chose mockup direction B with five changes. The reference is the labelled synthetic fixture `tests/fixtures/design/home-mockup-2026-11-23.html`. Its numbers are invented, and its `title` attributes are for viewing only. The cards and P-023 describe the design in words and win where they differ from the fixture.

Order: T60 → T60B, one after the other.
- T60 does the shared theme, Home, and the bugs the owner reported.
- T60B applies the look to the other five pages.

The Phase 4C rules still apply:
- provenance;
- the synthetic store for screenshots;
- tests outside Owns;
- no regressions.

Screenshots are taken at about 1500, 1100, 800 and 390 px wide, against the T56 synthetic store, never real provider data.

#### T60 — Home without containers, shared theme, reported bugs
- **Depends:** T59 merged · **Owns:**
  - `app/theme.py`;
  - `app/views/markets.py`;
  - `app/components/{kit.py,values.py,panels.py,tearsheet.py,instrument.py}`;
  - `tests/unit/app/{test_home.py,test_tearsheet.py,test_components.py,test_navigation.py,conftest.py}`;
  - `docs/specs/2026-11-15-desk-brief-design.md` §10.2 and §10.3 only.
- Deliverables:
  - **Theme (shared by every page).** It follows P-023:
    - no card or panel surfaces, borders around blocks, radii on blocks, or shadows;
    - section labels over hairlines;
    - the four type sizes;
    - backgrounds only on the elements P-023 lists.

    Page-specific panel styles left for T60B may remain until T60B removes them, and the PR lists them.
  - **Home layout,** as in P-023 and the fixture:
    - **Top bar,** then the one-line market strip, with the basis said once.
    - **The hero.** The figure is 40–44 px. The pill holds the 1D change and its basis. Below it come:
      - the provenance line;
      - range and Line/Candles controls, with Compare and CSV in "More";
      - the tvchart;
      - the 1W–1Y horizon row with "Charts by TradingView";
      - "Do the sources agree?" and "Risk and range" side by side, as flat rows.
    - **The watchlist** sits on the right:
      - It is about 340–360 px wide at 1500 px, full length, with no inner scroll.
      - Its group headers fold the group. Fold state is kept in session state, the selected instrument's group never folds away, and groups start open.
      - Rows keep the σ tag for notable moves.
    - **Below 960 px,** the watchlist goes under the hero. **Below 640 px,** sources and risk stack, and the All values source moves into the date cell.
    - **The tabs** keep their current content.
  - **Bugs reported by the owner on 2026-11-21:**
    1. The verdict section no longer overflows its column. The Lineage and Macro links no longer overlap the 52-week labels at any of the four widths.
    2. Watchlist hover content is never clipped. It is not inside a scroll area, and it renders where neither a column nor an iframe can clip it. It has no `title` attribute.
    3. Watchlist names never break one letter per line at any width, including about 800 px. They truncate with an ellipsis only after the short id.
    4. On weekends and holidays, the strip and watchlist show the last session's change, labelled with its day (for example "Fri"), not a dash.
    5. The run status on weekends shows the latest run and its check counts, not "No checks yet". "No checks yet" appears only when no run has been stored.
    6. **The verdict** (`instrument.verdict` and `_gap_text`):
       - Within `AGREE_BP`: "Agree", tone ok, with the gap. If the values are more than `LIKE_FOR_LIKE_S` apart in time, a neutral note says so. This is the 0.8 bp, 41/41 case.
       - Beyond `AGREE_BP` and more than `LIKE_FOR_LIKE_S` apart: "Not comparable", tone warn, with the time apart.
       - Beyond `AGREE_BP` at the same moment: "No", tone fail, as today.
       - The warn mark appears only in the warn case.
  - **Review follow-up** from [PR #90 round 2](https://github.com/allenvarghese05/desk-brief/pull/90): `kit.feed_tier`'s docstring says when it falls back to the source's most live tier for the instrument, or for any instrument, and when it returns `None`.
  - **Spec.** §10.2 and §10.3 are updated to match P-023.
- Acceptance:
  - **Theme and layout tests:**
    - A test fails if the theme CSS gives a block container a background, border, radius or shadow outside P-023's list.
    - Tests pin every Home block in its new place, including the strip's single basis, the pill's 1D change and a 1W-first horizon row.
  - **Bug tests:** each fails if its fix is removed.
    - A weekend fixture shows Friday's change and the latest run's checks.
    - Verdict tests cover agree-but-apart, not comparable and disagree.
    - A watchlist test shows that a folded group keeps the selected row reachable.
  - **Provenance:**
    - Hosted-versus-local tests pass.
    - No `title` attribute and no `<a target="_self">` appear (test).
    - "proxy", "IEX", "fallback" and "HYPOTHETICAL" stay visible beside their figures, once each.
  - **Screenshots:** Home at all four widths beside the fixture's. The PR states for each bug the width where it was fixed and checked.
  - **Timing:** a warm-cache first render is no slower than at T59 on the T56 benchmark (the PR gives both times).
- Changelog: card created from P-023 and the owner's 2026-11-21 test of T58 and T59.

#### T60B — Other pages without containers
- **Depends:** T60 merged · **Owns:**
  - `app/pages/*.py`;
  - `app/components/{macro.py,incidents.py}`;
  - `tests/unit/app/{test_pages.py,test_macro.py}`.
- Deliverables:
  - **The other pages.** Macro, Cross-checks, Data health, Sources and Runs use T60's theme with no panels:
    - section labels over hairlines;
    - flat tables with hairline rows;
    - the four type sizes.

    Each page keeps its 4–6 blocks and its tabs, and the page-specific panel styles T60 listed are removed.
  - **Fit.** At the four widths, nothing overflows, overlaps or wraps one letter per line. The Macro heatmap and tvcharts keep their attribution.
  - **Review follow-up** from [PR #90 round 2](https://github.com/allenvarghese05/desk-brief/pull/90): the gold vs real-yield panel's "Gold vs 1M" label follows the gold series in use. It reads "GLD vs 1M", with "proxy" visible, when gold comes from GLD.
- Acceptance:
  - The page tests pass, updated for the new layout.
  - A test pins the GLD label.
  - The T60 theme test still passes with every page loaded.
  - Hosted-versus-local tests hold.
  - No `title` attributes and no `<a target="_self">` appear (test).
  - Each page has screenshots at about 1500 and 390 px in the PR.
- Changelog: card created from P-023.

### Phase 5 — Stretch (only after DoD)

- **T50** Claude-written morning narrative (`claude-sonnet-5-5`), every number in the text verified against `model.json` before inclusion; unverifiable sentence → dropped and logged.
- **T51** SEC 13F changes for 3 managers (User-Agent, rate limit).
- **T52** Alpaca WebSocket streaming in local mode.

---

## 4. Phase gates (planner runs after each phase merges)

| Gate | Check |
|---|---|
| G0 (after T01) | Contracts reviewed by planner; tag `contracts-v1` |
| G1 (after W2) | `deskbrief run morning --date <yesterday>` locally against real sources, all 8 sources probed; findings → issues |
| G2 (after T24A) | First scheduled run observed; schedule delay recorded |
| G3 (after T31) | Real email received; report numbers spot-checked against provider websites by Allen |
| G4 (Fri) | DoD checklist (brief §14) walked; gaps → cards |
