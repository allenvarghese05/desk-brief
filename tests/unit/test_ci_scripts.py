"""Tests for the CI gate scripts in scripts/ci/ (contracts and pr-hygiene)."""

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    path = ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


contracts = _load("check_contracts")
hygiene = _load("check_pr_hygiene")

GOOD_BODY = """## Card
T01 — Contracts

## What changed
- added models

## Spec sections implemented
§4, §5.1

## Acceptance criteria
- [x] config loads (test_config.py::test_loads)

## Tests
- tests/unit/test_config.py

## Live probe (source cards only)
n/a

## Honesty / labeling
- [x] No synthetic data outside tests/

## Files outside "Owns"
None

## Risks and follow-ups
None
"""


class TestContracts:
    def test_creating_contract_file_is_allowed(self) -> None:
        assert contracts.violations([("A", "src/deskbrief/models.py")], "t01: x", []) == []

    def test_modifying_contract_file_needs_label_and_prefix(self) -> None:
        changes = [("M", "src/deskbrief/models.py"), ("M", "src/deskbrief/other.py")]
        assert contracts.violations(changes, "t14: alpaca", []) == ["src/deskbrief/models.py"]
        assert contracts.violations(changes, "contract: widen", ["contract"]) == []
        # label without prefix is not enough
        assert contracts.violations(changes, "t14: alpaca", ["contract"]) == [
            "src/deskbrief/models.py"
        ]

    def test_deleting_or_renaming_contract_file_is_flagged(self) -> None:
        assert contracts.violations([("D", "src/deskbrief/schemas.py")], "t02: x", []) == [
            "src/deskbrief/schemas.py"
        ]
        assert contracts.violations([("R100", "src/deskbrief/config.py")], "t02: x", []) == [
            "src/deskbrief/config.py"
        ]


class TestHygiene:
    def test_good_pr_passes(self) -> None:
        assert hygiene.problems("t01: contracts", GOOD_BODY) == []

    def test_contract_title_allowed(self) -> None:
        assert hygiene.problems("contract: widen obs schema", GOOD_BODY) == []

    def test_bad_title(self) -> None:
        errs = hygiene.problems("Add stuff", GOOD_BODY)
        assert any("title" in e for e in errs)

    def test_missing_card_and_sections(self) -> None:
        errs = hygiene.problems("t01: x", "## Card\n\n## Tests\nsome")
        assert any("reference a card" in e for e in errs)
        assert any("'## Card' is empty" in e for e in errs)
        assert any("missing section '## What changed'" in e for e in errs)

    def test_placeholders_rejected(self) -> None:
        body = GOOD_BODY.replace("T01 — Contracts", "T__ — <title>")
        errs = hygiene.problems("t01: x", body)
        assert any("T__" in e for e in errs)

    def test_none_body(self) -> None:
        assert hygiene.problems("t01: x", None)
