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
    html = value_cell("4.31%", GOOD, compact=False)  # T59: the full form is now opt-in
    for part in ("4.31%", "yield", "○", "official", "17 Nov", "treasury"):
        assert part in html
    evil = Provenance("<b>x</b>", 2, "10:27", "close")
    assert "<b>" not in change_cell("+1.0%", evil, "<i>basis</i>", compact=False)
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
from html.parser import HTMLParser  # noqa: E402

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


class _Visible(HTMLParser):
    """Collects the text a sighted reader sees: no attributes (so no aria-label), nothing inside
    a screen-reader-only (``db-sr``) element and nothing inside a tooltip (``db-tip``)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0  # > 0 while inside a hidden element
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if self.depth or "db-sr" in classes or "db-tip" in classes:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.parts.append(data)


def visible(html: str) -> str:
    """Text a sighted reader sees (see :class:`_Visible`)."""
    parser = _Visible()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


def test_visible_drops_hidden_text_and_attributes() -> None:
    html = (
        '<span aria-label="proxy"><span class="db-sr x">IEX</span>'
        '<span class="db-tip"><b>fallback</b></span>shown</span>'
    )
    assert visible(html) == "shown"


def test_theme_tokens_are_the_p022_palette_and_match_config() -> None:
    """P-022 colours, P-023 type and geometry (T60), mirrored in config.toml."""
    assert (theme.PAGE, theme.PANEL, theme.RAISED, theme.DIVIDER) == (
        "#0B0D10", "#13161B", "#181C22", "#22262E",
    )  # fmt: skip
    assert (theme.TEXT, theme.SECONDARY, theme.META) == ("#E8EAED", "#9AA0A6", "#8A9099")
    assert (theme.UP, theme.DOWN, theme.ACCENT) == ("#2EBD85", "#F6465D", "#5B9DFF")
    assert theme.RADIUS == 6
    assert theme.SANS.startswith("Inter")
    # P-023: four type sizes (the hero has a phone size of its own)
    assert (theme.SIZE_HERO, theme.SIZE_LG, theme.SIZE_BODY, theme.SIZE_META) == (42, 15, 13, 11.5)
    assert theme.SIZE_HERO_PHONE == 36
    cfg = tomllib.loads((REPO / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    t = cfg["theme"]
    assert t["backgroundColor"] == theme.PAGE
    # T60: native widgets match the flat theme: the raised shade P-023 allows on the search
    # field and the selected segment, the exempt elements' radius, flat dataframes
    assert t["secondaryBackgroundColor"] == theme.RAISED
    assert t["baseRadius"] == f"{theme.RADIUS}px"
    assert t["dataframeHeaderBackgroundColor"] == theme.PAGE
    assert t["dataframeBorderColor"] == theme.DIVIDER
    assert t["textColor"] == theme.TEXT
    assert t["primaryColor"] == theme.ACCENT
    assert t["font"].startswith("Inter")
    assert cfg["client"]["showSidebarNavigation"] is False


def test_blocks_have_no_panels_and_figures_are_tabular() -> None:
    """P-023 (T60): the containers T58 drew as cards are flat; figures use tabular numerals."""
    from app.components import kit, panels

    for css in (theme.CSS, kit.PAGE_CSS, panels.HOME_CSS):
        for key in ("db-watch", "db-chart", "db-detail", "db-risk", "db-values", "db-tiles"):
            for rule in re.findall(rf"\.st-key-{key}\b[^{{]*\{{([^}}]*)\}}", css):
                assert "background: #" not in rule, (key, rule)
                assert "border-radius" not in rule, (key, rule)
    assert "tabular-nums" in theme.CSS
    assert "#0b0e13" not in theme.CSS.lower()  # the T36 palette is gone
    assert "db-tile" not in theme.CSS  # the T58 tiles became a one-line strip


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
    assert visible(html).strip() == f"4.31%{TIER_GLYPH[tier]}17 Nov"  # figure, glyph, as-of
    listed = value_cell("4.31%", p, compact=True, show_as_of=False)  # Markets lists
    assert visible(listed).strip() == f"4.31%{TIER_GLYPH[tier]}"
    assert 'db-asof">17 Nov 00:00 ET, 2d ago<' in listed  # still in the tooltip


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


def test_flag_words_stay_visible_beside_a_change() -> None:
    """A change printed on its own (biggest moves, the chart header) shows the caveat words."""
    p = Provenance("alpaca", 2, "10:27", "last", flags=("proxy", "IEX", "fallback"))
    for html in (
        change_cell("+1.2%", p, "previous close"),  # compact, the default since T59
        change_cell("+1.2%", p, "previous close", compact=False),
    ):
        shown = visible(html)
        for word in ("proxy", "IEX", "fallback"):
            assert word in shown, html
    # beside its value the change stays quiet: the value already shows the words
    quiet = visible(change_cell("+1.2%", p, "previous close", compact=True, glyph=False))
    assert quiet.strip() == "+1.2%"


def test_biggest_moves_show_proxy_and_caveats_visibly(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.components import panels

    monkeypatch.setattr(panels, "field_label", lambda last: "last")
    now = datetime(2026, 11, 16, 12, tzinfo=UTC)
    last = SimpleNamespace(source="alpaca", as_of=now)
    spy = SimpleNamespace(
        instrument_id="SPY", name="SPDR S&P 500 ETF", tier=2, as_of_text="10:27",
        is_proxy=True, available=True, change=SimpleNamespace(last=last, previous=None),
        change_text="+1.2%", basis="previous close",
        qualifiers=("fallback source: alpaca", "IEX: single exchange, not the consolidated tape"),
        label="SPDR S&P 500 ETF — proxy for the S&P 500", value_text="$600.00",  # T58 row
    )  # fmt: skip
    ov = SimpleNamespace(
        sections=[SimpleNamespace(rows=(spy,))],
        moves=[SimpleNamespace(instrument_id="SPY", text="SPY +1.2% is 3.1σ its usual move")],
        as_of=now,
    )
    # T58: biggest moves became a sigma tag on the watchlist row (P-022)
    sig = panels.sigmas(ov)["SPY"]  # type: ignore[arg-type]
    shown = visible(panels.wl_row(spy, now, "", (), sig))  # type: ignore[arg-type]
    assert "3.1σ" in shown
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
    parts = [panels.brand(ov), panels.status(ov), panels.footer(ov, kit.glyph_legend())]
    parts += [panels.status(ov, (ov.as_of.date(), ov.as_of))]
    lead, strip = panels.strip_items(ov, "UST_10Y")
    parts += [panels.strip_lead(lead)] + [h for _, h in strip]
    sigs = {"SPY": ("3.1σ", "SPY moved 3.1σ")}  # T58: moves are sigma tags on the rows
    for g in panels.watchlist(ov.sections, ov.as_of, "SPY", sparks=ov.sparks, sigs=sigs):
        parts += [panels.group_title(g.title, folded=False, count=len(g))]
        parts += [h for _, h in g.rows + g.proxies]
    rows = {r.instrument_id: r for s in ov.sections for r in s.rows}
    parts += [panels.chart_header(r, ov.as_of) for r in rows.values()]
    parts += [kit.glyph_legend(), kit.kpis([("Checks", "3", "fail", "x")]), kit.status_legend()]
    parts += [kit.status_strip([("16 Nov", "fail", "1 failed"), ("13 Nov", "pass", "ok")])]
    html = "".join(parts)
    assert len(parts) > 30
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


def _rgb(colour: str) -> tuple[float, float, float]:
    h = colour.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _tint(rgba: str, under: str) -> tuple[float, float, float]:
    """An ``rgba(r, g, b, a)`` tint composited over an opaque colour."""
    *rgb, alpha = (float(x) for x in rgba[rgba.index("(") + 1 : -1].split(","))
    base = _rgb(under)
    return (
        rgb[0] * alpha + base[0] * (1 - alpha),
        rgb[1] * alpha + base[1] * (1 - alpha),
        rgb[2] * alpha + base[2] * (1 - alpha),
    )


def _contrast(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    def lum(c: tuple[float, float, float]) -> float:
        def ch(v: float) -> float:
            v /= 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

        return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])

    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_text_colours_reach_wcag_aa_on_every_surface_they_sit_on() -> None:
    """4.5:1 for every text token on the shades it is drawn on (the 12 px meta text too)."""
    t = theme
    surfaces = (t.PAGE, t.PANEL, t.RAISED, t.HOVER)
    pairs: list[tuple[str, tuple[float, float, float], tuple[float, float, float]]] = []
    for name, fg in (("text", t.TEXT), ("secondary", t.SECONDARY), ("meta", t.META),
                     ("up", t.UP), ("down", t.DOWN)):  # fmt: skip
        pairs += [(f"{name} on {bg}", _rgb(fg), _rgb(bg)) for bg in surfaces]
    pairs.append(("meta on divider", _rgb(t.META), _rgb(t.DIVIDER)))
    pairs += [(f"tooltip {n}", _rgb(c), _rgb(t.TOOLTIP)) for n, c in
              (("text", t.TEXT), ("secondary", t.SECONDARY))]  # fmt: skip
    for bg in surfaces:  # pills and markers: text on its tint over the shade under it
        pairs.append((f"up pill on {bg}", _rgb(t.UP), _tint(t.UP_TINT, bg)))
        pairs.append((f"down pill on {bg}", _rgb(t.DOWN_PILL), _tint(t.DOWN_TINT, bg)))
        pairs.append((f"amber mark on {bg}", _rgb(t.AMBER), _tint(t.AMBER_TINT, bg)))
        pairs.append((f"flat pill on {bg}", _rgb(t.SECONDARY), _rgb(t.RAISED)))
    for status, colour in kit.STATUS_COLOUR.items():  # the check squares' glyphs
        pairs.append((f"{status} square", _rgb(kit._STATUS_INK[status]), _rgb(colour)))
    low = {name: round(_contrast(fg, bg), 2) for name, fg, bg in pairs if _contrast(fg, bg) < 4.5}
    assert low == {}
    from app.components import panels

    assert f"color: {theme.DOWN_PILL}" in panels.HOME_CSS  # the hero's down pill uses it


def test_status_strip_gives_each_day_its_detail_for_screen_readers() -> None:
    html = kit.status_strip([("16 Nov", "fail", "SPY vs ES 31 bps"), ("13 Nov", "pass", "")])
    assert 'aria-label="16 Nov fail: SPY vs ES 31 bps"' in html
    assert 'aria-label="13 Nov pass"' in html
    assert "title=" not in html


# --- T60 bug 6: the verdict -----------------------------------------------------------------


def _quote(value: float, hours: float, tier: int) -> object:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    at = datetime(2026, 11, 20, 20, tzinfo=UTC) - timedelta(hours=hours)
    return SimpleNamespace(
        value=value, unit="pct", as_of=at, tier=tier, source=f"s{tier}", field="yield",
        kind="yield", is_proxy=False, dataset="d", is_fallback=False,
    )  # fmt: skip


def test_verdict_agrees_when_close_even_if_apart_in_time() -> None:
    """The owner's case: two sources 0.8 bp apart, 15 h apart: "Agree", with a neutral note."""
    from app.components import instrument

    v = instrument.verdict([_quote(4.000, 0, 3), _quote(4.008, 15, 2)])  # type: ignore[list-item]
    assert v is not None
    assert (v.tone, v.headline) == ("ok", "Agree")
    assert v.gap == "0.8 bp apart"
    assert "15.0 h apart" in v.note
    best = _quote(4.000, 0, 3)
    gap = instrument._gap_text(_quote(4.008, 15, 2), best)  # type: ignore[arg-type]
    assert "not comparable" not in gap
    assert "15.0 h apart" in gap


def test_verdict_agrees_at_the_same_moment_without_a_note() -> None:
    from app.components import instrument

    v = instrument.verdict([_quote(4.000, 0, 3), _quote(4.002, 0.1, 2)])  # type: ignore[list-item]
    assert v is not None
    assert (v.tone, v.headline, v.note) == ("ok", "Agree", "")


def test_verdict_far_apart_in_value_and_time_is_not_comparable() -> None:
    from app.components import instrument

    v = instrument.verdict([_quote(4.00, 0, 3), _quote(4.20, 15, 2)])  # type: ignore[list-item]
    assert v is not None
    assert (v.tone, v.headline) == ("warn", "Not comparable")
    assert v.gap == "15.0 h apart"
    html = instrument.verdict_card_html([_quote(4.00, 0, 3), _quote(4.20, 15, 2)], "")  # type: ignore[list-item]
    assert "db-mark-warn" in html  # the warn mark, only in the warn case


def test_verdict_disagrees_at_the_same_moment() -> None:
    from app.components import instrument

    rows = [_quote(4.00, 0, 3), _quote(4.20, 0.1, 2)]
    v = instrument.verdict(rows)  # type: ignore[arg-type]
    assert v is not None
    assert (v.tone, v.headline, v.gap) == ("fail", "No", "20.0 bp apart")
    html = instrument.verdict_card_html(rows, "")  # type: ignore[arg-type]
    assert "db-mark-warn" not in html
    ok = instrument.verdict_card_html([_quote(4.000, 0, 3), _quote(4.008, 15, 2)], "")  # type: ignore[list-item]
    assert "db-mark-warn" not in ok
    assert '<b>Agree</b><span class="db-verdict-gap">0.8 bp apart</span>' in ok
