"""Email-safe HTML and plain-text renderers for the morning brief (spec §9.1).

Both renderers are dumb: they print what the ``BriefModel`` already decided (labels, signed
changes, tier glyphs, qualifiers, cross-check ``ref_date`` labels, known limits). Nothing here
says what a value *is* (P-009), so a yield or an ECB reference rate is never called a "close".

Email safety: tables only, inline CSS only (no ``<style>`` block, no script, no external
resources), a 640 px column. The template is ``templates/brief.html.j2``; Jinja autoescape is on.
"""

from __future__ import annotations

from pathlib import Path

import jinja2

from deskbrief.query import SourceHealth
from deskbrief.report.model import TIER_GLYPH, TIER_NAME, BriefModel, Row

FOOTER_LINE = "Personal project built on public data. Not investment advice."
TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "brief.html.j2"
_HEALTH_COLOR = {"ok": "#1a7f37", "warn": "#9a6700", "fail": "#cf222e", "unknown": "#5b6470"}


def _legend() -> str:
    """``● live/proxy (T1) · ◐ delayed (T2) · ○ official (T3)``, from the model's constants."""
    names = {1: "live/proxy", 2: TIER_NAME[2], 3: TIER_NAME[3]}
    return " · ".join(f"{TIER_GLYPH[t]} {names[t]} (T{t})" for t in (1, 2, 3))


def _health_line(h: SourceHealth) -> str:
    """``stooq: failed (1 of 2 fetches failed)``; a source with no fetches says so."""
    if h.n_fetches == 0:
        return f"{h.source}: {h.status} (no fetches recorded)"
    return f"{h.source}: {h.status} ({h.n_failed} of {h.n_fetches} fetches failed)"


def _change_note(r: Row) -> str | None:
    """The caption under a row label: ``chg since <basis>``, or the model's own wording when
    there is no change to report (``n/a`` is never printed as a change)."""
    if not r.available:
        return None
    if r.change_text == "n/a":
        return r.description.removeprefix(f"{r.label}, ")
    return f"chg since {r.basis}" if r.basis else None


def _env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        autoescape=jinja2.select_autoescape(["html", "j2"], default=True),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(model: BriefModel) -> str:
    """The brief as one self-contained HTML document."""
    return (
        _env()
        .get_template(TEMPLATE_NAME)
        .render(
            m=model,
            health_line=_health_line,
            change_note=_change_note,
            health_color=_HEALTH_COLOR.get(model.health.status, _HEALTH_COLOR["unknown"]),
            legend=_legend(),
            footer=FOOTER_LINE,
        )
    )


def render_text(model: BriefModel) -> str:
    """Plain-text alternative (the ``text/plain`` part of the email)."""
    out = [model.title, model.health.text, "", model.headline.text, ""]
    for s in model.sections:
        out.append(s.title.upper())
        for r in s.rows:
            if not r.available:
                out.append(f"  {r.label}: {r.value_text}")
                continue
            change = f"{r.change_text} since {r.basis}" if r.basis else r.change_text
            if r.change_text == "n/a":
                change = _change_note(r) or r.change_text
            note = f" [{'; '.join(r.qualifiers)}]" if r.qualifiers else ""
            out.append(
                f"  {r.label}: {r.value_text} ({change}), "
                f"as of {r.as_of_text} ET, {r.tier_glyph} {r.tier_name}{note}"
            )
        out.append("")
    health = [_health_line(h) for h in model.health.summary.sources if h.status != "ok"]
    if health:
        out += ["DATA HEALTH", *(f"  - {h}" for h in health), ""]
    if model.notable:
        out += ["NOTABLE", *(f"  - {n.text}" for n in model.notable), ""]
    out += ["KNOWN LIMITS TODAY", *(f"  - {k.text}" for k in model.known_limits), ""]
    out += [_legend(), FOOTER_LINE, ""]
    return "\n".join(out)


__all__ = ["FOOTER_LINE", "render_html", "render_text"]
