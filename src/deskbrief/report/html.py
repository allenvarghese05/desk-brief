"""Email-safe HTML and plain-text renderers for the morning brief (spec §9.1, P-024 card T63).

Both renderers are dumb: they print what the ``BriefModel`` already decided (the read, the
movers, Check before using, the snapshot rows, the stored note and Today's events). Nothing
here says what a value *is* (P-009), so a yield or an ECB reference rate is never called a
"close".

Order (the approved email mockup, P-024): the read; the note; Moved more than usual (the top
five of ``BriefModel.movers`` with sigma, source and time); Today; the snapshot (one row per
market, the underlying instead of its proxy, quiet and missing rows folded into grey lines,
weekly and monthly series dated); Check before using; a dashboard link; a three-line footer
with the health count. The full limits and warnings list is on Data health, as the footer says.

Email safety: tables for rows, inline CSS only (no ``<style>`` block, no script, no external
resources), one 640 px column. The template is ``templates/brief.html.j2``; autoescape is on.
The dashboard link is shown only for an ``https://`` URL (``DESKBRIEF_DASHBOARD_URL``, read by
the caller); without one the email names the dashboard without a link.
"""
# ruff: noqa: RUF001  (the sigma sign is intentional output)

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import jinja2

from deskbrief.report.model import NO_TRADE_TEXT, TIER_GLYPH, TIER_NAME, BriefModel, Event, Read
from deskbrief.timeutil import to_et

FOOTER_LINE = "Personal project built on public data. Not investment advice."
TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "brief.html.j2"
#: how many movers the email lists (P-024: the top five)
TOP_MOVERS = 5
#: the environment variable naming the dashboard's public URL (optional)
DASHBOARD_URL_ENV = "DESKBRIEF_DASHBOARD_URL"
_TONE = {"up": "#0e8a5f", "down": "#c8283c", "flat": "#5f6670", "none": "#5f6670"}
_READ = {"up": "#0e8a5f", "down": "#c8283c", "flat": "#111418"}


def _legend() -> str:
    """``● live · ◐ delayed · ○ official``, from the model's constants."""
    return " · ".join(f"{TIER_GLYPH[t]} {TIER_NAME[t]}" for t in (1, 2, 3))


def dashboard_url(env: Mapping[str, str]) -> str | None:
    """The dashboard's URL when it is a plain ``https://`` address, else None."""
    url = (env.get(DASHBOARD_URL_ENV) or "").strip()
    if not url.startswith("https://") or any(c in url for c in " \"<>'`"):
        return None
    if any(_control(c) for c in url):  # no newline, tab or other control, C0 or C1
        return None
    return url


def _control(c: str) -> bool:
    """A C0 or C1 control, DEL, or a Unicode line or paragraph separator (U+2028, U+2029)."""
    n = ord(c)
    return n < 32 or 127 <= n <= 159 or n in (0x2028, 0x2029)


def _event_line(e: Event) -> str:
    """ "Durable goods orders, Oct · prior +1.4%": the period and prior value only when the
    source gives them (one line: the model has already collapsed external whitespace)."""
    text = e.title + (f", {e.period}" if e.period else "")
    return text + (f" · prior {e.prior}" if e.prior else "")


def _since(m: BriefModel) -> str:
    if m.previous_brief is None:
        return ""
    et = to_et(m.previous_brief)
    return f"{et:%a} {et:%H:%M}"


def _why(read: Read | None) -> str:
    """One line naming the rules behind the read: "Risk-off: 2 of 4 rules met (...)"."""
    if read is None or read.kind == "quiet":
        return ""
    met = read.met
    if read.kind == "regime":
        total = sum(1 for r in read.rules if r.regime == read.regime)
        mine = [r.label for r in met if r.regime == read.regime]
        return f"{read.label}: {len(mine)} of {total} rules met ({', '.join(mine)})."
    if read.kind == "mixed":
        return "Rules met: " + "; ".join(f"{r.label} ({r.regime})" for r in met) + "."
    if met:
        return "Rules met: " + ", ".join(r.label for r in met) + "."
    return "No regime rule met."


def _movers_note(m: BriefModel) -> str:
    windows = sorted({x.n_moves for x in m.movers[:TOP_MOVERS]})  # the movers shown
    if not windows:
        return ""
    if len(windows) == 1:
        text = f"σ compares the move with that market's last {windows[0]} daily moves"
    else:
        text = (
            "σ compares each move with that market's own recent daily moves "
            f"({windows[0]} to {windows[-1]} of them)"
        )
    since = _since(m)
    if since:
        text += f", for values new since the {since} brief"
    text += "."
    extra = len(m.movers) - TOP_MOVERS
    if extra > 0:
        text += f" {extra} more on the dashboard."
    return text


def footer_lines(m: BriefModel) -> tuple[str, str, str]:
    """The three footer lines: the health count, the legend and proxies, the disclaimer."""
    first = f"{m.health.text}. Details and the full list of limits are on the Data health page."
    second = f"{_legend()}. Proxies (ETFs) are shown only when the underlying market has no value."
    return first, second, FOOTER_LINE


def _read_parts(m: BriefModel) -> tuple[str, str, str]:
    """(label with its full stop, the rest of the read, the label colour)."""
    if m.read is None:
        return "No read.", "", _READ["flat"]
    label = m.read.label
    rest = m.read.text.removeprefix(f"{label}.").strip()
    return f"{label}.", rest, _READ.get(m.read.tone, _READ["flat"])


def _when(m: BriefModel) -> str:
    et = to_et(m.report_time)
    return f"Desk Brief · {et:%a} {et.day} {et:%b} · {et:%H:%M} ET"


def _env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        autoescape=jinja2.select_autoescape(["html", "j2"], default=True),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(model: BriefModel, *, dashboard: str | None = None) -> str:
    """The brief as one self-contained HTML document. ``dashboard`` is the dashboard's URL
    (:func:`dashboard_url`); without it the email names the dashboard without a link."""
    label, rest, colour = _read_parts(model)
    return (
        _env()
        .get_template(TEMPLATE_NAME)
        .render(
            m=model,
            when=_when(model),
            read_label=label,
            read_rest=rest,
            read_color=colour,
            note=model.note if model.note is not None and model.note.shown else None,
            why=_why(model.read),
            movers=model.movers[:TOP_MOVERS],
            movers_note=_movers_note(model),
            since=_since(model),
            events=model.today,
            tone=_TONE,
            tier_name=TIER_NAME,
            dashboard_url=dashboard,
            footer_lines=footer_lines(model),
            no_trade=NO_TRADE_TEXT,
        )
    )


def render_text(model: BriefModel, *, dashboard: str | None = None) -> str:
    """Plain-text alternative (the ``text/plain`` part of the email), in the same order."""
    label, rest, _ = _read_parts(model)
    out = [_when(model), f"{label} {rest}".strip(), ""]
    note = model.note if model.note is not None and model.note.shown else None
    if note is not None and note.sentences:
        out += [" ".join(note.sentences), f"Every number checked ({note.n_checked} of "
                f"{note.n_numbers}).", ""]  # fmt: skip
    elif note is not None:
        out += [f"No note today: {note.reason}.", ""]
    why = _why(model.read)
    if why:
        out += [why, ""]
    out.append("MOVED MORE THAN USUAL")
    if model.movers:
        for x in model.movers[:TOP_MOVERS]:
            check = " (check)" if x.check else ""
            out.append(
                f"  {x.short}{check}: {x.value_text} {x.change_text}, {x.sigma:.1f}σ "
                f"({x.source}, {TIER_NAME[x.tier]}, {x.as_of_text})"
            )
        out.append(f"  {_movers_note(model)}")
    else:
        since = _since(model)
        out.append(
            "  Nothing moved more than usual" + (f" since the {since} brief." if since else ".")
        )
    out += ["", "TODAY (ET)"]
    if model.today:
        out += [f"  {e.time_text or 'all day'}  {_event_line(e)} ({e.source})" for e in model.today]
    else:
        out.append("  No releases or auctions stored for today.")
    out += ["", "SNAPSHOT"]
    for g in model.snapshot:
        out.append(f"  {g.title.upper()}")
        for r in g.rows:
            meta = f" ({r.meta})" if r.meta else ""
            check = " (check)" if r.check else ""
            out.append(f"    {r.label}{meta}{check}: {r.value_text} {r.change_text or '–'}")
        if g.no_trade:
            out.append(f"    {', '.join(g.no_trade)}: {NO_TRADE_TEXT}")
        if g.unavailable:
            out.append(f"    {', '.join(g.unavailable)}: data unavailable")
    out += ["", "CHECK BEFORE USING"]
    if model.caveats:
        out += [f"  - {c.title}. {c.text}" for c in model.caveats]
    else:
        out.append("  Nothing to check in today's read.")
    out.append("")
    out.append(
        f"Open the dashboard: {dashboard}"
        if dashboard
        else "Charts, sources and Data health are on the Desk Brief dashboard."
    )
    out += ["", *footer_lines(model), ""]
    return "\n".join(out)


__all__ = ["DASHBOARD_URL_ENV", "FOOTER_LINE", "dashboard_url", "render_html", "render_text"]
