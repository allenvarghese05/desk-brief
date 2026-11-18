"""Dashboard components: provenance is mandatory, mode fails closed, R2 is read-only.

SYNTHETIC FIXTURE: credentials below are made-up strings; nothing opens a socket.
"""
# ruff: noqa: RUF001  (true minus sign is intentional output)

from __future__ import annotations

from pathlib import Path

import pytest
from app.components import data
from app.components.values import Provenance, change_cell, tier_glyph, value_cell

from deskbrief.config import load_config
from deskbrief.store.backend import StorageError

REPO = Path(__file__).resolve().parents[3]

GOOD = Provenance("treasury", 3, "17 Nov", "yield", "17 Nov 00:00 ET, 2d ago")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": ""},
        {"as_of": " "},
        {"field": ""},
        {"tier": 0},
        {"tier": 4},
    ],
)
def test_provenance_requires_source_tier_asof_and_field(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {"source": "s", "tier": 2, "as_of": "10:27", "field": "close"}
    with pytest.raises(ValueError, match=r"tier|provenance"):
        Provenance(**{**base, **kwargs})  # type: ignore[arg-type]


def test_cells_show_label_tier_asof_source_and_escape_html() -> None:
    html = value_cell("4.31%", GOOD)
    for part in ("4.31%", "yield", "○", "official", "17 Nov", "treasury"):
        assert part in html
    evil = Provenance("<b>x</b>", 2, "10:27", "close")
    assert "<b>" not in change_cell("+1.0%", evil, "<i>basis</i>")
    assert tier_glyph(1).count("●") == 1


def test_change_direction_class() -> None:
    assert "db-down" in change_cell("−3bp", GOOD)
    assert "db-up" in change_cell("+3bp", GOOD)
    assert "db-up" not in change_cell("0bp", GOOD)
    assert "db-down" not in change_cell("0bp", GOOD)


def test_mode_fails_closed_and_hosted_hides_tier1_whatever_config_says() -> None:
    cfg = load_config(REPO / "config.yaml")
    assert data.resolve_mode("local") == data.LOCAL
    assert {data.resolve_mode(v) for v in (None, "", "Hosted", "x")} == {data.HOSTED}
    hosted = cfg.model_copy(
        update={"dashboard": cfg.dashboard.model_copy(update={"hosted_hidden_tiers": ()})}
    )
    assert 1 in data.hidden_tiers(data.HOSTED, hosted)
    assert data.hidden_tiers(data.LOCAL, cfg) == frozenset()


def test_reader_env_ignores_writer_keys_and_names_gaps_without_values() -> None:
    env = {
        "R2_ACCOUNT_ID": "acct",
        "R2_ACCESS_KEY_ID": "writer-id",
        "R2_SECRET_ACCESS_KEY": "writer-secret",
        "R2_READER_ACCESS_KEY_ID": "reader-id",
        "R2_READER_SECRET_ACCESS_KEY": "reader-secret",
    }
    assert set(data.reader_env(env)) == set(data.READER_ENV)
    assert "writer-id" not in data.reader_env(env).values()
    with pytest.raises(StorageError) as err:
        data.reader_env({"R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "writer-id"})
    assert "R2_READER_SECRET_ACCESS_KEY" in str(err.value)
    assert "writer-id" not in str(err.value)


def test_cached_reader_key_includes_hidden_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    import streamlit as st

    st.cache_data.clear()
    calls: list[frozenset[int]] = []

    def fake(config: object, env: object, now: object, exclude_tiers: frozenset[int]) -> object:
        calls.append(exclude_tiers)
        return object()

    monkeypatch.setattr(data, "build_snapshot", fake)
    cfg = load_config(Path(__file__).resolve().parents[3] / "config.yaml")
    data.load_snapshot(cfg, frozenset({1}))
    data.load_snapshot(cfg, frozenset())
    data.load_snapshot(cfg, frozenset({1}))  # cached
    assert calls == [frozenset({1}), frozenset()]


def test_provenance_of_raises_value_error_not_assert() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.components.panels import provenance_of

    now = datetime(2026, 11, 16, 12, tzinfo=UTC)
    orphan = SimpleNamespace(instrument_id="UST_10Y", tier=3, change=None, as_of_text="x")
    with pytest.raises(ValueError, match="UST_10Y"):
        provenance_of(orphan, now)  # type: ignore[arg-type]
    spread = SimpleNamespace(instrument_id="UST_2S10S", tier=3, change=None, as_of_text="x")
    assert provenance_of(spread, now).field == "derived spread"  # type: ignore[arg-type]
