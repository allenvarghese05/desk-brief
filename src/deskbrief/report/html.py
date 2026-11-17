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

from deskbrief.report.model import TIER_GLYPH, TIER_NAME, BriefModel

FOOTER_LINE = "Personal project built on public data. Not investment advice."
TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "brief.html.j2"
_HEALTH_COLOR = {"ok": "#1a7f37", "warn": "#9a6700", "fail": "#cf222e", "unknown": "#5b6470"}


def _legend() -> str:
    """``● live/proxy (T1) · ◐ delayed (T2) · ○ official (T3)``, from the model's constants."""
    names = {1: "live/proxy", 2: TIER_NAME[2], 3: TIER_NAME[3]}
    return " · ".join(f"{TIER_GLYPH[t]} {names[t]} (T{t})" for t in (1, 2, 3))


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
            since = f" since {r.basis}" if r.basis else ""
            note = f" [{'; '.join(r.qualifiers)}]" if r.qualifiers else ""
            out.append(
                f"  {r.label}: {r.value_text} ({r.change_text}{since}), "
                f"as of {r.as_of_text} ET, {r.tier_glyph} {r.tier_name}{note}"
            )
        out.append("")
    if model.notable:
        out += ["NOTABLE", *(f"  - {n.text}" for n in model.notable), ""]
    out += ["KNOWN LIMITS TODAY", *(f"  - {k.text}" for k in model.known_limits), ""]
    out += [_legend(), FOOTER_LINE, ""]
    return "\n".join(out)


__all__ = ["FOOTER_LINE", "render_html", "render_text"]
