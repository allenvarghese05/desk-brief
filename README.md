# Desk Brief

A markets data pipeline, morning report and near-live dashboard that checks its own numbers.

> Personal project built on public data. Not investment advice. No trading, signals or predictions.

Every number carries its **source**, its **tier** (● fast proxy, ◐ exact but delayed, ○ official daily) and its **as-of time**, and the same quantity is cross-checked across independent sources.

Status: under construction. See:
- Design: [`docs/specs/2026-11-15-desk-brief-design.md`](docs/specs/2026-11-15-desk-brief-design.md)
- Plan: [`docs/plan/implementation-plan.md`](docs/plan/implementation-plan.md)
- How it is built (planner / implementer / reviewer agents): [`docs/process/agent-workflow.md`](docs/process/agent-workflow.md)

## Development

```bash
uv sync --all-groups
make ci
```
