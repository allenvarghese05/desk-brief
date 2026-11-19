"""The only way a market number reaches the page (spec 10.2, P-009, licence rule of H9).

``value_cell`` and ``change_cell`` take a :class:`Provenance` that *requires* source, tier,
as-of and the field label; building one without them raises, so a bare number cannot be
rendered through these helpers. Next to every figure they print the field label (``yield``,
``ECB reference rate``, ``close``), the tier glyph and name, the as-of time and the source.
"""
# ruff: noqa: RUF001  (true minus sign is intentional output)

from __future__ import annotations

from dataclasses import dataclass
from html import escape

TIER_GLYPH = {1: "●", 2: "◐", 3: "○"}
TIER_NAME = {1: "live", 2: "delayed", 3: "official"}
_MINUS = "−"


@dataclass(frozen=True)
class Provenance:
    """Where a figure comes from. Every field is required and must be non-empty."""

    source: str
    tier: int
    as_of: str  # ET text as shown, e.g. "17 Nov" or "10:27"
    field: str  # label of what the value is: "yield", "ECB reference rate", "close"
    hover: str = ""  # full ET time and relative age, shown on hover

    def __post_init__(self) -> None:
        if self.tier not in TIER_GLYPH:
            raise ValueError(f"tier must be 1, 2 or 3, got {self.tier!r}")
        for name in ("source", "as_of", "field"):
            if not getattr(self, name).strip():
                raise ValueError(f"provenance needs a non-empty {name}")


def tier_glyph(tier: int) -> str:
    """``● ◐ ○`` with the tier name for screen readers and hover."""
    name = TIER_NAME[tier]
    return f'<span class="db-tier" title="{name}" aria-label="{name}">{TIER_GLYPH[tier]}</span>'


def _meta(p: Provenance, extra: str = "") -> str:
    return (
        f'<span class="db-meta" title="{escape(p.hover)}">'
        f'<span class="db-field">{escape(p.field)}</span> · {tier_glyph(p.tier)} '
        f"{TIER_NAME[p.tier]} · "
        f'<span class="db-asof">{escape(p.as_of)}</span> · '
        f'<span class="db-src">{escape(p.source)}</span>{extra}</span>'
    )


def _hover(p: Provenance, extra: str = "") -> str:
    """Full provenance as one line of plain text: the compact tag's tooltip."""
    parts = [p.field, TIER_NAME[p.tier], p.hover or p.as_of, p.source]
    return " · ".join(x for x in parts if x) + (f" · {extra}" if extra else "")


def compact_meta(p: Provenance, extra: str = "", *, visible: bool = True) -> str:
    """Tier glyph and as-of on screen; field, source (and ``extra``) for screen readers and hover.

    The same four facts as :func:`_meta` are in the markup (``db-field``, ``db-src``,
    ``db-asof``, the glyph), so a figure never loses its provenance (spec 10.2, P-009, H9).
    ``visible=False`` hides the whole tag, for a change printed right next to its value.
    """
    cls = "db-tagline" if visible else "db-sr"
    tail = f'<span class="db-sr"> · {escape(extra)}</span>' if extra else ""
    return (
        f'<span class="{cls}" title="{escape(_hover(p, extra))}">'
        f'<span class="db-sr"><span class="db-field">{escape(p.field)}</span> · </span>'
        f"{tier_glyph(p.tier)} "
        f'<span class="db-asof">{escape(p.as_of)}</span>'
        f'<span class="db-sr"> · {TIER_NAME[p.tier]} · '
        f'<span class="db-src">{escape(p.source)}</span></span>{tail}</span>'
    )


def value_cell(text: str, p: Provenance, *, compact: bool = False) -> str:
    """A value with its provenance: full line by default, the compact tag when ``compact``."""
    meta = compact_meta(p) if compact else _meta(p)
    return f'<span class="db-fig"><span class="db-val">{escape(text)}</span>{meta}</span>'


def direction(text: str) -> str:
    """CSS class for a signed change text: up, down, or flat."""
    return "db-down" if text.startswith(_MINUS) else "db-up" if text.startswith("+") else "db-flat"


def change_cell(
    text: str, p: Provenance, basis: str | None = None, *, compact: bool = False
) -> str:
    """A change; ``basis`` says what it is measured against ("previous NYSE close (13 Nov)").

    ``compact`` keeps the provenance in the markup but off screen: the value next to it already
    shows the same tag, so printing it twice is noise.
    """
    cls = direction(text)
    cls = "" if cls == "db-flat" and not compact else cls
    if compact:
        meta = compact_meta(p, f"vs {basis}" if basis else "", visible=False)
    else:
        meta = _meta(p, f" · {escape(basis)}" if basis else "")
    return f'<span class="db-fig"><span class="db-val {cls}">{escape(text)}</span>{meta}</span>'
