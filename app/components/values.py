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


def value_cell(text: str, p: Provenance) -> str:
    return f'<span class="db-fig"><span class="db-val">{escape(text)}</span>{_meta(p)}</span>'


def change_cell(text: str, p: Provenance, basis: str | None = None) -> str:
    """A change; ``basis`` says what it is measured against ("previous NYSE close (13 Nov)")."""
    direction = "db-down" if text.startswith(_MINUS) else "db-up" if text.startswith("+") else ""
    extra = f" · {escape(basis)}" if basis else ""
    return (
        f'<span class="db-fig"><span class="db-val {direction}">{escape(text)}</span>'
        f"{_meta(p, extra)}</span>"
    )
