# Agent log

How the project was built: one entry per card / review round. Real run incidents go to INCIDENTS.md.

## 2026-11-15 · T00 Repo scaffold and strict CI · orchestrator (opus-5.5)
- Did: uv project, Makefile `ci`, CI workflow with the eight required checks (lint, typecheck, test, lockfile, security, config, contracts, pr-hygiene), contract and PR-hygiene gate scripts with tests, PR template, dependabot, gitleaks config, docs stubs.
- Went wrong: the implementer session could not start (repository not yet trusted in Claude Desktop), so the orchestrator built T00 directly; it still goes through CI + a fresh reviewer agent.
- Decisions: dependency audit is report-only (continue-on-error) so a transitive CVE in an upstream package cannot block all merges overnight; gitleaks is blocking. Coverage omits `cli.py` per spec §13.
