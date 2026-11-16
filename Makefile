.PHONY: ci lock lint fmt typecheck test secrets sync config

sync:
	uv sync --all-groups

lock:
	uv lock --check

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy --strict src

test:
	uv run pytest tests -m "not integration" --cov=deskbrief --cov-report=term --cov-fail-under=85

secrets:
	@if command -v gitleaks >/dev/null 2>&1; then gitleaks detect --no-banner --redact; \
	else echo "gitleaks not installed locally; CI runs it (brew install gitleaks)"; fi

config:
	@if [ -f src/deskbrief/config.py ]; then uv run python -m deskbrief.config; \
	else echo "config: deskbrief.config not present yet"; fi

# Same gates as CI (pr-hygiene and contracts need a PR event and run in CI only).
ci: lock lint typecheck test config secrets
