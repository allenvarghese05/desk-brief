# Agent log

How the project was built: one entry per card / review round. Real run incidents go to INCIDENTS.md.

## 2026-11-15 · T00 Repo scaffold and strict CI · orchestrator (opus-5.5)
- Did: uv project, Makefile `ci`, CI workflow with the eight required checks (lint, typecheck, test, lockfile, security, config, contracts, pr-hygiene), contract and PR-hygiene gate scripts with tests, PR template, dependabot, gitleaks config, docs stubs.
- Went wrong: the implementer session could not start (repository not yet trusted in Claude Desktop), so the orchestrator built T00 directly; it still goes through CI + a fresh reviewer agent.
- Decisions: dependency audit is report-only (continue-on-error) so a transitive CVE in an upstream package cannot block all merges overnight; gitleaks is blocking. Coverage omits `cli.py` per spec §13.

## 2026-11-15 · T01 Contracts · orchestrator (opus-5.5)
- Did: `config.yaml` with the full 31-instrument universe and 14 cross-check groups (user's revised pairings); pydantic config models with cross-reference validation and `python -m deskbrief.config`; core types (`FetchRequest`, `RawResponse`, `RunContext`, `DQResult`, `XResult`, enums); pandera schemas for all 9 tables; `Source`/`HttpClient`/`StorageBackend` protocols; storage key builders (spec §6) in `store/backend.py` so the layout is a contract in code; structlog JSON logging with secret redaction; time helpers.
- Went wrong: pandas resolved to 3.x (new default string dtype and datetime resolution), so schemas check "tz-aware UTC datetime of any unit" and "string values in str/object columns" instead of pinning dtypes.
- Decisions: secrets never live in `FetchRequest`; they are passed to `HttpClient.get(secret_params=..., secret_headers=...)` so they can't reach raw/ or logs. Added `Instrument.is_proxy` + `label` so proxy labeling is data-driven.
- Review round 1 (REQUEST_CHANGES, 7 major): the fresh reviewer probed redaction and found real bypasses (Bearer tokens, cookies, URL userinfo, URL-encoding, JSON-in-string, exceptions/bytes via repr, stdlib httpx logs); schemas rejected empty and all-null frames; partition-date and `as_of` semantics were undefined; config lacked feeds the source cards need. All fixed with tests; schemas are now generated from one declaration with `empty_frame`/`to_frame` helpers. Lesson: contracts need a reviewer that actively probes, not just reads.
