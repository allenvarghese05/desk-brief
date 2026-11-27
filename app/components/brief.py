"""The brief pane of Home v4 (P-024, card T62): the read, the note, why, Check before using and
Today.

One block of markup, flat like every Home block (P-023): labels over hairlines, no panel. Its
two halves (``.db-brief-main``: the read and the note; ``.db-brief-side``: why, Check before
using, Today) sit one under the other in the right-hand pane, side by side when the brief moves
above the other two panes below 1280 px, and stack again below 960 px.

Every word comes from ``BriefModel`` (the read, the rules, the caveats) or the stored note and
events; nothing is computed here. The note is shown only when it is stored with the brief
(T66); otherwise the pane says "No note today" with the reason. Today lists the scheduled
releases and auctions stored by T65 for the brief's ET date, with their time in ET.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from html import escape
from typing import Protocol

from deskbrief.report.model import Caveat, Note, Read
from deskbrief.timeutil import to_et


class EventLike(Protocol):
    """What Today needs of a stored event (``report.model.Event``, card T65)."""

    time_text: str  # "08:30", or "" when the source gives a date only
    title: str
    source: str
    period: str | None
    prior: str | None


def when(prev: datetime | None) -> str:
    """ "Fri 07:00": the previous brief's day and clock time in ET (T61 review)."""
    if prev is None:
        return ""
    et = to_et(prev)
    return f"{et:%a} {et:%H:%M}"


def _read_line(read: Read | None) -> str:
    if read is None:
        return '<p class="db-read">No read yet: the brief has not been built.</p>'
    label, _, rest = read.text.partition(". ")
    tone = f" db-{read.tone}" if read.tone in ("up", "down") else ""
    tail = f" {escape(rest)}" if rest else ""
    return f'<p class="db-read"><span class="db-regime{tone}">{escape(label)}.</span>{tail}</p>'


def _note(note: Note | None) -> str:
    """The note one sentence per line, its time and "every number matches the stored data (n of
    n)"; or "No note today" with a reason that is news; or nothing (no note stored, or a
    configuration state such as no API key, which goes to the log and Data health)."""
    if note is None or not note.shown:
        return ""
    if not note.sentences:
        return f'<p class="db-nonote">No note today: {escape(note.reason or "")}.</p>'
    lines = "".join(f'<p class="db-note-s">{escape(s)}</p>' for s in note.sentences)
    at = f"Note at {to_et(note.written_at):%H:%M} ET" if note.written_at else ""
    badge = note.badge  # "" when the note shows no figure: nothing to vouch for
    checked = (
        f'<span class="db-ok" aria-hidden="true">✓</span> {escape(badge[:1].lower() + badge[1:])}'
        if badge
        else ""
    )
    line = " · ".join(p for p in (at, checked) if p)
    return f"{lines}" + (f'<p class="db-checked">{line}</p>' if line else "")


def _why(read: Read | None, since: str) -> str:
    if read is None:
        return ""
    met = read.met
    if read.kind == "quiet":
        aside = f"since {since} brief" if since else ""
        body = '<p class="db-quiet">Nothing moved more than usual.</p>'
        return _section(f"Why “{read.label.lower()}”", aside, body)
    if read.kind == "regime":
        total = sum(1 for r in read.rules if r.regime == read.regime)
        items = [r.label for r in met if r.regime == read.regime]
        aside = f"{len(items)} of {total} rules"
    elif read.kind == "mixed":
        items = [f"{r.label} ({r.regime})" for r in met]
        aside = "rules split"
    else:
        items = [r.label for r in met]
        aside = f"{len(items)} met" if items else "no rule met"
    body = (
        '<ul class="db-rules">' + "".join(f"<li>{escape(i)}</li>" for i in items) + "</ul>"
        if items
        else '<p class="db-quiet">No rule met.</p>'
    )
    return _section(f"Why “{read.label.lower()}”", aside, body)


def _caveats(caveats: Sequence[Caveat]) -> str:
    if not caveats:
        body = '<p class="db-quiet">Nothing to check in today\'s read.</p>'
        return _section("Check before using", "0", body)
    rows = "".join(
        f'<p class="db-cav"><b>{escape(c.title)}</b>{escape(c.text)}</p>' for c in caveats
    )
    return _section("Check before using", str(len(caveats)), rows)


def _today(events: Sequence[EventLike] | None) -> str:
    if not events:
        body = '<p class="db-quiet">No releases or auctions stored for today.</p>'
        return _section("Today", "ET", body)
    rows = "".join(
        f'<span class="db-cal-t">{escape(e.time_text or "all day")}</span>'
        f"<div>{escape(e.title)}{escape(', ' + e.period) if e.period else ''}"
        + (f'<span class="db-cal-src"> · prior {escape(e.prior)}</span>' if e.prior else "")
        + f'<span class="db-cal-src"> · {escape(e.source)}</span></div>'
        for e in events
    )
    return _section("Today", "ET", f'<div class="db-cal">{rows}</div>')


def _section(title: str, aside: str, body: str) -> str:
    extra = f"<span>{escape(aside)}</span>" if aside else ""
    head = f'<div class="db-label">{escape(title)}{extra}</div>'
    return f'<section class="db-bsec">{head}{body}</section>'


def pane(
    read: Read | None,
    caveats: Sequence[Caveat],
    as_of: datetime,
    previous_brief: datetime | None,
    note: Note | None = None,
    events: Sequence[EventLike] | None = None,
    *,
    now: datetime | None = None,
    archived: datetime | None = None,
) -> str:
    """The whole brief pane as one block of markup. ``as_of`` is the brief the page follows
    (``data.brief_time_of``). With ``now`` (the page's time), the heading names what the reader
    sees: the read, rules and checks are recomputed at ``now``, so after the brief they are
    a "Live read" beside the brief that actually ran (``archived``), never under that brief's
    own heading (PR #102 review round 2)."""
    since = when(previous_brief)
    tail = f" · new since {since}" if since else ""
    title, aside = _heading(as_of, now, archived)
    main = (
        f'<div class="db-label">{escape(title)}<span>{escape(aside + tail)}</span></div>'
        f"{_read_line(read)}{_note(note)}"
    )
    side = _why(read, since) + _caveats(caveats) + _today(events)
    return (
        '<div class="db-brief">'
        f'<div class="db-brief-main">{main}</div>'
        f'<div class="db-brief-side">{side}</div></div>'
    )


def _heading(as_of: datetime, now: datetime | None, archived: datetime | None) -> tuple[str, str]:
    """("The brief", "Mon 06:15 ET") when the page shows the brief's own minute; else
    ("Live read", "15:02 ET · brief at 06:15"), naming the latest archived brief ("last brief
    Fri 06:15" on another day) or saying none is archived."""
    if now is None:  # a caller that shows the brief itself
        et = to_et(as_of)
        return "The brief", f"{et:%a} {et:%H:%M} ET"
    page = to_et(now)
    if archived is not None and abs((now - archived).total_seconds()) < 60:
        a = to_et(archived)
        return "The brief", f"{a:%a} {a:%H:%M} ET"
    if archived is None:
        return "Live read", f"{page:%a} {page:%H:%M} ET · no brief archived yet"
    a = to_et(archived)
    if a.date() == page.date():
        return "Live read", f"{page:%H:%M} ET · brief at {a:%H:%M}"
    return "Live read", f"{page:%a} {page:%H:%M} ET · last brief {a:%a} {a:%H:%M}"


def _css() -> str:
    from app import theme as t

    return f"""
<style>
/* ---- T62 the brief pane (P-024): flat, labels over hairlines ---- */
.db-brief {{ display: block; font-size: {t.SIZE_BODY}px; line-height: 1.5;
  color: {t.TEXT}; }}
.db-brief li, .db-brief p, .db-brief div {{ font-size: inherit; }}
.db-read {{ font-size: {t.SIZE_LG}px; font-weight: 600; line-height: 1.45; margin: 0 0 10px 0;
  color: {t.TEXT}; }}
.db-regime.db-down {{ color: {t.DOWN}; }}
.db-regime.db-up {{ color: {t.UP}; }}
.db-note-s {{ color: {t.SECONDARY}; line-height: 1.6; margin: 0; padding: 3px 0; }}
.db-checked, .db-nonote, .db-quiet {{ color: {t.META}; font-size: {t.SIZE_META}px;
  margin: 10px 0 0 0; }}
.db-quiet {{ margin: 0; }}
.db-ok {{ color: {t.UP}; }}
.db-bsec {{ margin-top: 26px; }}
.db-rules {{ list-style: none; margin: 0; padding: 0; display: grid;
  grid-template-columns: 1fr 1fr; gap: 4px 16px; color: {t.SECONDARY}; }}
.db-rules li {{ margin: 0; }}
.db-rules li::before {{ content: "✓ "; color: {t.UP}; }}
.db-cav {{ margin: 0; padding: 9px 0; border-top: 1px solid {t.DIVIDER}; color: {t.SECONDARY}; }}
.db-cav:first-of-type {{ border-top: 0; padding-top: 0; }}
.db-cav b {{ color: {t.AMBER}; font-weight: 500; margin-right: 6px; }}
.db-cal {{ display: grid; grid-template-columns: 62px minmax(0, 1fr); row-gap: 7px;
  column-gap: 12px; }}
.db-cal-t {{ color: {t.META}; font-variant-numeric: tabular-nums; }}
.db-cal-src {{ color: {t.META}; font-size: {t.SIZE_META}px; }}
@media (max-width: 1280px) and (min-width: 961px) {{
  .db-brief {{ display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
    column-gap: {t.GAP}px; align-items: start; }}
  .db-brief-side .db-bsec:first-child {{ margin-top: 0; }}
}}
@media (max-width: 640px) {{ .db-rules {{ grid-template-columns: 1fr; }} }}
</style>
"""


#: the brief pane's stylesheet (injected by ``app/views/markets.py`` with ``panels.HOME_CSS``)
BRIEF_CSS = _css()
