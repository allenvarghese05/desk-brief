"""The only way a market number reaches the page (spec 10.2, P-009, P-022, licence rule of H9).

``value_cell`` and ``change_cell`` take a :class:`Provenance` that *requires* source, tier,
as-of and the field label; building one without them raises, so a bare number cannot be
rendered through these helpers. Two renderings:

* the full line (default): field label, tier glyph and name, as-of and source printed under
  the figure;
* ``compact=True`` (P-022, opt-in until T59): a focusable tier glyph beside the figure whose
  CSS tooltip (hover, keyboard focus, tap) and accessible label give field, tier, source and
  as-of. The four facts stay in the markup (``db-field``, ``db-src``, ``db-asof``,
  ``data-tier``).

Either way the words proxy, IEX, fallback and HYPOTHETICAL (``Provenance.flags``) are printed
as visible text next to the figure, never only in a tooltip. No markup here uses a ``title``
attribute: tooltips are CSS (``app/theme.py``).
"""
# ruff: noqa: RUF001  (true minus sign is intentional output)

from __future__ import annotations

from dataclasses import dataclass
from html import escape

TIER_GLYPH = {1: "●", 2: "◐", 3: "○"}
TIER_NAME = {1: "live", 2: "delayed", 3: "official"}
#: caveats that must always be readable on screen beside a figure (P-022)
VISIBLE_FLAGS = ("proxy", "IEX", "fallback", "HYPOTHETICAL")
_MINUS = "−"


@dataclass(frozen=True)
class Provenance:
    """Where a figure comes from. Every field but ``hover`` and ``flags`` is required."""

    source: str
    tier: int
    as_of: str  # ET text as shown, e.g. "17 Nov" or "10:27"
    field: str  # label of what the value is: "yield", "ECB reference rate", "close"
    hover: str = ""  # full ET time and relative age, for the tooltip and the accessible label
    flags: tuple[str, ...] = ()  # words from VISIBLE_FLAGS printed beside the figure

    def __post_init__(self) -> None:
        if self.tier not in TIER_GLYPH:
            raise ValueError(f"tier must be 1, 2 or 3, got {self.tier!r}")
        for name in ("source", "as_of", "field"):
            if not getattr(self, name).strip():
                raise ValueError(f"provenance needs a non-empty {name}")
        unknown = [f for f in self.flags if f not in VISIBLE_FLAGS]
        if unknown:
            raise ValueError(f"unknown provenance flag(s) {unknown!r}; use {VISIBLE_FLAGS}")


def tier_glyph(tier: int) -> str:
    """``● ◐ ○`` with the tier name as its accessible label."""
    name = TIER_NAME[tier]
    return (
        f'<span class="db-tier" role="img" aria-label="{name}" data-tier="{tier}">'
        f"{TIER_GLYPH[tier]}</span>"
    )


def flags_html(p: Provenance) -> str:
    """The visible caveat words (proxy, IEX, fallback, HYPOTHETICAL), or nothing."""
    if not p.flags:
        return ""
    tags = "".join(f'<span class="db-flag">{escape(f)}</span>' for f in p.flags)
    return f'<span class="db-flags">{tags}</span>'


def label(p: Provenance, extra: str = "") -> str:
    """Full provenance as one line of plain text: the accessible label of a compact figure."""
    parts = [
        p.field,
        f"{TIER_NAME[p.tier]} (tier {p.tier})",
        f"as of {p.hover or p.as_of}",
        f"source {p.source}",
        *p.flags,
    ]
    return " · ".join(parts) + (f" · {extra}" if extra else "")


def _meta(p: Provenance, extra: str = "") -> str:
    when = f'<span class="db-sr"> ({escape(p.hover)})</span>' if p.hover else ""
    return (
        f'<span class="db-meta">'
        f'<span class="db-field">{escape(p.field)}</span> · {tier_glyph(p.tier)} '
        f"{TIER_NAME[p.tier]} · "
        f'<span class="db-asof">{escape(p.as_of)}</span>{when} · '
        f'<span class="db-src">{escape(p.source)}</span>{extra}</span>'
    )


def provenance_glyph(p: Provenance, extra: str = "", *, cls: str = "db-prov") -> str:
    """The compact tag: a focusable glyph, its CSS tooltip and its accessible label.

    The tooltip is ``aria-hidden`` because the label already says the same; it shows on hover,
    on keyboard focus and on tap (a tap focuses the element)."""
    tail = f" · {escape(extra)}" if extra else ""
    when = escape(p.hover or p.as_of)
    return (
        f'<span class="{cls}" tabindex="0" role="img" data-tier="{p.tier}" '
        f'aria-label="{escape(label(p, extra))}">'
        f'<span class="db-glyph" aria-hidden="true">{TIER_GLYPH[p.tier]}</span>'
        f'<span class="db-tip" role="tooltip" aria-hidden="true">'
        f'<span class="db-field">{escape(p.field)}</span> · {TIER_GLYPH[p.tier]} '
        f"{TIER_NAME[p.tier]} · "
        f'<span class="db-asof">{when}</span> · '
        f'<span class="db-src">{escape(p.source)}</span>{tail}</span></span>'
    )


def _hidden(p: Provenance, extra: str = "", *, cls: str = "db-sr") -> str:
    """The four facts for screen readers only (a change printed right next to its value)."""
    tail = f" · {escape(extra)}" if extra else ""
    return (
        f'<span class="{cls}" data-tier="{p.tier}">'
        f'<span class="db-field">{escape(p.field)}</span> · '
        f'<span aria-hidden="true">{TIER_GLYPH[p.tier]} </span>{TIER_NAME[p.tier]} · '
        f'<span class="db-asof">{escape(p.hover or p.as_of)}</span> · '
        f'<span class="db-src">{escape(p.source)}</span>{tail}</span>'
    )


def value_cell(text: str, p: Provenance, *, compact: bool = False, show_as_of: bool = True) -> str:
    """A value with its provenance: the full line by default, the glyph when ``compact``.

    A compact value also prints its as-of under the figure, as the T36 compact tag did, so a
    table (Macro, the tear sheet) still shows how old each figure is. ``show_as_of=False``
    leaves it to the tooltip, for the Markets lists, where every row is the same day."""
    if compact:
        when = (
            f'<span class="db-asofline" aria-hidden="true">{escape(p.as_of)}</span>'
            if show_as_of
            else ""
        )
        return (
            f'<span class="db-fig"><span class="db-val">{escape(text)}</span>'
            f"{provenance_glyph(p)}{flags_html(p)}{when}</span>"
        )
    return (
        f'<span class="db-fig"><span class="db-val">{escape(text)}{flags_html(p)}</span>'
        f"{_meta(p)}</span>"
    )


def direction(text: str) -> str:
    """CSS class for a signed change text: up, down, or flat."""
    return "db-down" if text.startswith(_MINUS) else "db-up" if text.startswith("+") else "db-flat"


def change_cell(
    text: str,
    p: Provenance,
    basis: str | None = None,
    *,
    compact: bool = False,
    glyph: bool = True,
) -> str:
    """A change; ``basis`` says what it is measured against ("previous NYSE close (13 Nov)").

    ``compact`` draws the change as a tinted pill with the glyph beside it. ``glyph=False``
    keeps the provenance in the markup for screen readers but off screen: for a change printed
    right next to its value, which already shows the glyph and the caveat words. Otherwise the
    caveat words (proxy, IEX, fallback, HYPOTHETICAL) are printed beside the change.
    """
    cls = direction(text)
    if compact:
        vs = f"vs {basis}" if basis else ""
        tag = (
            provenance_glyph(p, vs, cls="db-prov db-of-chg") + flags_html(p)
            if glyph
            else _hidden(p, vs, cls="db-sr db-of-chg")
        )
        return f'<span class="db-fig"><span class="db-val {cls}">{escape(text)}</span>{tag}</span>'
    cls = "" if cls == "db-flat" else cls
    meta = _meta(p, f" · {escape(basis)}" if basis else "")
    return (
        f'<span class="db-fig"><span class="db-val {cls}">{escape(text)}{flags_html(p)}</span>'
        f"{meta}</span>"
    )


def status_marker(status: str) -> str:
    """A check result that does not rely on colour (P-022): ✕ failed and ! warn on an amber
    tint, ✓ pass, stale and skip in grey. Red is never used: it means "down"."""
    text = {
        "fail": "✕ failed",
        "warn": "! warn",
        "pass": "✓ pass",
        "stale": "stale",
        "skip": "– skip",
    }
    if status not in text:
        raise ValueError(f"unknown check status {status!r}")
    return f'<span class="db-mark db-mark-{status}">{text[status]}</span>'
