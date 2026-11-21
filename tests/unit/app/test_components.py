"""Dashboard components: provenance is mandatory, mode fails closed, R2 is read-only.

SYNTHETIC FIXTURE: credentials below are made-up strings; nothing opens a socket.
"""
# ruff: noqa: RUF001  (true minus sign is intentional output)

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from app.components import data
from app.components.values import Provenance, change_cell, tier_glyph, value_cell

from deskbrief.config import load_config
from deskbrief.store.backend import StorageError

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _close_new_connections() -> Iterator[None]:
    """Close the shared DuckDB connections a test's stores opened (T56 keeps one per store for
    the process), so the suite does not run out of native threads."""
    from app.components import data

    before = set(data._SLOTS)
    yield
    for key in set(data._SLOTS) - before:
        slot = data._SLOTS.pop(key)
        if slot.current is not None and not slot.busy:
            slot.current.conn.close()


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


# --- T57: theme, compact provenance, markers, caches ---------------------------------------

import re  # noqa: E402
import tomllib  # noqa: E402
from collections.abc import Callable  # noqa: E402
from html import unescape  # noqa: E402

from app import theme  # noqa: E402
from app.components import kit  # noqa: E402
from app.components.values import (  # noqa: E402
    TIER_GLYPH,
    TIER_NAME,
    VISIBLE_FLAGS,
    status_marker,
)
from streamlit.testing.v1 import AppTest  # noqa: E402

#: the files T57 owns that generate markup; the others still carry title= until T58/T59
T57_FILES = (
    "app/Home.py",
    "app/views/markets.py",
    "app/theme.py",
    "app/components/values.py",
    "app/components/kit.py",
    "app/components/panels.py",
)


def visible(html: str) -> str:
    """Text a sighted reader sees: screen-reader-only spans, tooltips and tags removed."""
    html = re.sub(r'<span class="db-sr(?: [^"]*)?"[^>]*>.*?</span></span>', "", html, flags=re.S)
    html = re.sub(r'<span class="db-tip"[^>]*>.*?</span></span>', "", html, flags=re.S)
    return unescape(re.sub(r"<[^>]+>", "", html))


def test_theme_tokens_are_the_p022_palette_and_match_config() -> None:
    assert (theme.PAGE, theme.PANEL, theme.RAISED, theme.DIVIDER) == (
        "#0B0D10", "#13161B", "#181C22", "#22262E",
    )  # fmt: skip
    assert (theme.TEXT, theme.SECONDARY, theme.META) == ("#E8EAED", "#9AA0A6", "#6B7078")
    assert (theme.UP, theme.DOWN, theme.ACCENT) == ("#2EBD85", "#F6465D", "#5B9DFF")
    assert (theme.RADIUS, theme.PAD, theme.GAP) == (12, 24, 20)
    assert theme.SANS.startswith("Inter")
    assert (theme.SIZE_HERO, theme.SIZE_FIGURE, theme.SIZE_BODY, theme.SIZE_META) == (
        40,
        26,
        14,
        12,
    )
    cfg = tomllib.loads((REPO / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    t = cfg["theme"]
    assert t["backgroundColor"] == theme.PAGE
    assert t["secondaryBackgroundColor"] == theme.PANEL
    assert t["textColor"] == theme.TEXT
    assert t["primaryColor"] == theme.ACCENT
    assert t["font"].startswith("Inter")
    assert cfg["client"]["showSidebarNavigation"] is False


def test_cards_have_no_borders_and_tabular_figures() -> None:
    cards = re.search(r"\.st-key-db-watch[^{]*\{([^}]*)\}", theme.CSS)
    assert cards is not None
    assert "border:" not in cards.group(1)
    assert f"border-radius: {theme.RADIUS}px" in cards.group(1)
    assert f"padding: {theme.PAD}px" in cards.group(1)
    assert "tabular-nums" in theme.CSS
    assert "#0b0e13" not in theme.CSS.lower()  # the T36 palette is gone


@pytest.mark.parametrize("tier", [1, 2, 3])
def test_every_tier_has_glyph_tooltip_focus_and_label(tier: int) -> None:
    p = Provenance("treasury", tier, "17 Nov", "yield", "17 Nov 00:00 ET, 2d ago")
    html = value_cell("4.31%", p, compact=True)
    prov = re.search(r'<span class="db-prov"([^>]*)>(.*)</span></span>$', html, flags=re.S)
    assert prov is not None
    attrs, body = prov.groups()
    assert 'tabindex="0"' in attrs  # focusable: the tooltip shows on focus and on tap
    assert f'data-tier="{tier}"' in attrs
    label = re.search(r'aria-label="([^"]*)"', attrs)
    assert label is not None
    for part in ("yield", TIER_NAME[tier], f"tier {tier}", "17 Nov 00:00 ET", "treasury"):
        assert part in label.group(1)
    assert f'<span class="db-glyph" aria-hidden="true">{TIER_GLYPH[tier]}</span>' in body
    tip = re.search(r'<span class="db-tip" role="tooltip"[^>]*>(.*)', body, flags=re.S)
    assert tip is not None
    for part in (
        'db-field">yield<',
        TIER_NAME[tier],
        'db-asof">17 Nov 00:00 ET, 2d ago<',
        "treasury",
    ):
        assert part in tip.group(1)
    assert visible(html).strip() == f"4.31%{TIER_GLYPH[tier]}"  # the figure and its glyph


def test_tooltip_shows_on_hover_focus_and_tap() -> None:
    for state in (":hover .db-tip", ":focus .db-tip", ":focus-within .db-tip"):
        assert f".db-prov{state}" in theme.CSS
    assert re.search(r"\.db-tip \{\{?[^}]*display: none", theme.CSS)


def test_flag_words_stay_visible_in_compact_mode() -> None:
    p = Provenance("alpaca", 2, "10:27", "last", flags=VISIBLE_FLAGS)
    for html in (value_cell("$600.00", p, compact=True), value_cell("$600.00", p)):
        shown = visible(html)
        for word in ("proxy", "IEX", "fallback", "HYPOTHETICAL"):
            assert word in shown
    with pytest.raises(ValueError, match="unknown provenance flag"):
        Provenance("s", 2, "10:27", "close", flags=("rumour",))


def test_row_caveats_become_visible_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.components import panels

    monkeypatch.setattr(panels, "field_label", lambda last: "last")
    now = datetime(2026, 11, 16, 12, tzinfo=UTC)
    row = SimpleNamespace(
        instrument_id="SPY", tier=2, as_of_text="10:27", is_proxy=True,
        change=SimpleNamespace(last=SimpleNamespace(source="alpaca", as_of=now)),
        qualifiers=("fallback source: alpaca", "IEX: single exchange, not the consolidated tape"),
    )  # fmt: skip
    p = panels.provenance_of(row, now)  # type: ignore[arg-type]
    assert p.flags == ("proxy", "IEX", "fallback")
    shown = visible(value_cell("$600.00", p, compact=True))
    for word in ("proxy", "IEX", "fallback"):
        assert word in shown


def test_compact_change_keeps_or_hides_the_glyph() -> None:
    with_glyph = change_cell("+3bp", GOOD, "previous close", compact=True)
    assert 'class="db-prov db-of-chg"' in with_glyph
    assert "vs previous close" in with_glyph
    quiet = change_cell("+3bp", GOOD, "previous close", compact=True, glyph=False)
    assert "db-prov" not in quiet
    assert 'db-src">treasury<' in quiet
    assert "vs previous close" in quiet
    assert visible(quiet).strip() == "+3bp"


def test_failure_marker_is_amber_with_a_cross_and_differs_from_down() -> None:
    fail, warn, stale = status_marker("fail"), status_marker("warn"), status_marker("stale")
    assert "✕ failed" in fail
    assert "db-mark-fail" in fail
    assert "warn" in warn
    assert "db-mark-warn" in warn
    assert "stale" in stale
    assert theme.FAIL == theme.WARN == theme.AMBER  # one amber family for failed and warn
    assert theme.FAIL.lower() != theme.DOWN.lower()
    assert kit.STATUS_COLOUR["fail"] != theme.DOWN
    css = theme.CSS + kit.PAGE_CSS
    rules = re.findall(r"([^{}]*\b(?:fail|warn)[^{}]*)\{\{?([^}]*)\}", css)
    assert rules  # every failed / warn rule ...
    for selector, body in rules:
        assert theme.DOWN.lower() not in body.lower(), selector  # ... never uses red
    down = re.search(r"\.db-down \{\{?([^}]*)\}", theme.CSS)
    mark = re.search(r"\.db-mark-fail \{\{?([^}]*)\}", theme.CSS)
    assert down is not None
    assert mark is not None
    assert down.group(1).strip() != mark.group(1).strip()
    assert theme.AMBER in mark.group(1)
    assert theme.AMBER_TINT in mark.group(1)
    with pytest.raises(ValueError, match="unknown check status"):
        status_marker("red")


def test_glyph_legend_labels_delayed_and_hides_live_when_hosted() -> None:
    local = kit.glyph_legend(frozenset())
    hosted = kit.glyph_legend(frozenset({1}))
    assert ">delayed<" in local
    assert ">official<" in local
    assert ">live<" in local
    assert ">delayed<" in hosted
    assert ">live<" not in hosted
    assert "●" not in hosted


def test_no_title_attributes_or_self_targets_in_t57_files() -> None:
    for rel in T57_FILES:
        src = (REPO / rel).read_text(encoding="utf-8")
        assert 'target="_self"' not in src, rel
        assert not re.search(r"""[\s'"]title=["'{\\]""", src), rel


def test_rendered_t57_markup_has_no_title_or_link(run_home: Callable[..., AppTest]) -> None:
    """Everything panels, values and kit render for a real (synthetic) overview."""
    from app.components import data, panels

    run_home("local")
    cfg = load_config(__import__("os").environ["DESKBRIEF_CONFIG"])
    ov = data.overview(data.load_snapshot(cfg, frozenset()), data.LOCAL, frozenset())
    parts = [panels.brand(ov), panels.status(ov), panels.yield_curve(ov), panels.NO_MOVES]
    parts += [h for _, h in panels.tile_items(ov, "UST_10Y") + panels.move_items(ov)]
    for g in panels.watchlist(ov.sections, ov.as_of, "SPY"):
        parts += [panels.group_title(g.title)] + [h for _, h in g.rows + g.proxies]
    for title, cards in panels.overview(ov, "UST_10Y"):
        parts += [panels.section_title(title)] + [h for _, h in cards]
    rows = {r.instrument_id: r for s in ov.sections for r in s.rows}
    parts += [panels.chart_header(r, ov.as_of) for r in rows.values()]
    parts += [kit.glyph_legend(), kit.kpis([("Checks", "3", "fail", "x")]), kit.status_legend()]
    parts += [kit.status_strip([("16 Nov", "fail", "1 failed"), ("13 Nov", "pass", "ok")])]
    html = "".join(parts)
    assert len(parts) > 40
    assert "title=" not in html
    assert "href=" not in html
    assert "target=" not in html
    assert 'data-tier="1"' in html  # local mode: the tier-1 SPY tick is there


def test_kit_caches_are_keyed_on_the_data_version(monkeypatch: pytest.MonkeyPatch) -> None:
    import streamlit as st

    st.cache_data.clear()
    reads: list[str] = []

    class Store:
        def get_bytes(self, key: str) -> bytes:
            reads.append(key)
            return b'{"a": 1}'

    cfg = load_config(REPO / "config.yaml")
    ctx = kit.Ctx(cfg, "hosted", frozenset({1}), Store(), None)  # type: ignore[arg-type]
    version = iter(["v1", "v1", "v2"])
    monkeypatch.setattr(kit.data, "data_version", lambda config: next(version))
    assert kit.serving_json(ctx, "x.json") == {"a": 1}
    kit.serving_json(ctx, "x.json")  # same version: cached
    kit.serving_json(ctx, "x.json")  # new version: read again
    assert len(reads) == 2


def test_kit_version_survives_an_unreadable_store(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(config: object) -> str:
        raise StorageError("R2_READER_ACCESS_KEY_ID is not set")

    monkeypatch.setattr(kit.data, "data_version", boom)
    assert kit.version(load_config(REPO / "config.yaml")) == ""
