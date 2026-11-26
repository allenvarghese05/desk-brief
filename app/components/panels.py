"""Markets panels (T36 layout, T57 look, T58 Home v2, T60 Home v3): top bar status line, the
one-line market strip, the watchlist with sigma tags and row tooltips, the hero header, the
page footer and the Home stylesheet.

All text comes from ``BriefModel`` rows (labels, signed changes, as-of text); every number is
printed through ``values.value_cell`` / ``change_cell`` with compact provenance (a focusable
tier glyph with a CSS tooltip, P-022). The caveat words (proxy, IEX, fallback, HYPOTHETICAL)
are printed once beside each figure, never twice in one place.

Selection is in-session (T57): each selectable item is returned as ``(instrument id, html)``
and the page draws it under a transparent button whose callback sets ``st.query_params``
(``app/views/markets.py``). The markup itself holds no link, no ``target`` and no ``title``.

Every watchlist row carries the same hover content (P-023): full name, field, tier, source and
as-of, in a CSS tooltip (``.db-wl-tip``) that opens on hover and on keyboard focus of the row's
button and is ``position: fixed``, so no column, scroll area or iframe can clip it.

On a day that is not an XNYS session the 1D change of a price is the last session's (bug 4 of
T60): :func:`move_label` names it by that session's weekday ("Fri"), or "since Fri close" for a
market that traded on an XNYS holiday.

:data:`HOME_CSS` styles the T60 blocks (strip, hero, watchlist, verdict, risk facts, range bar,
footer) from the ``app/theme.py`` tokens, with no container backgrounds (P-023); the page
injects it after ``theme.apply()``.
"""
# ruff: noqa: RUF001  (the sigma sign is intentional output)

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape

import numpy as np
from app.components.data import Overview
from app.components.values import (
    TIER_GLYPH,
    TIER_NAME,
    Provenance,
    change_cell,
    direction,
    tier_glyph,
    value_cell,
)

from deskbrief.calendars import is_business_day
from deskbrief.query import (
    BASIS_NY_CLOSE,
    BASIS_NYSE_CLOSE,
    HOME_EXCHANGES,
    OFFICIAL_INTERVALS,
    ChangeRow,
    field_label,
    like_for_like,
    source_name,
)
from deskbrief.report.model import SHORT_LABELS, SPREAD_ID, Row, Section
from deskbrief.timeutil import to_et

NO_CHECKS = "No checks yet"

#: what a trader calls each instrument (one map, shared with the email)
SHORT = SHORT_LABELS
_TENOR_YEARS = {"UST_3M": 0.25, "UST_2Y": 2, "UST_10Y": 10, "UST_20Y": 20, "UST_30Y": 30}
NO_CHANGE = "—"
NO_DATA = 'no data<span class="db-sr"> (data unavailable)</span>'
#: the instrument selected when the URL names none
DEFAULT_ID = "ES_F"


def short(row: Row) -> str:
    """The trader's name (10Y, Gold); a ticker (SPY, EURUSD) as it is; never a raw id with an
    underscore."""
    iid = row.instrument_id
    return SHORT.get(iid, iid.replace("_", " "))


def _age(delta_seconds: float) -> str:
    minutes = max(0, int(delta_seconds // 60))
    if minutes < 1:
        return "just now"
    if minutes < 120:
        return f"{minutes}m ago"
    return f"{minutes // 60}h ago" if minutes < 2880 else f"{minutes // 1440}d ago"


def provenance_of(row: Row, now: datetime) -> Provenance:
    """Source, tier, as-of and field label of an available row (never invented)."""
    if row.tier is None:
        raise ValueError(f"{row.instrument_id} has no value, so no provenance")
    if row.change is None:  # only the derived 2s10s spread has no observed value behind it
        if row.instrument_id != SPREAD_ID:
            raise ValueError(f"{row.instrument_id} has a value but no observed change behind it")
        return Provenance("treasury (derived)", row.tier, row.as_of_text, "derived spread")
    last = row.change.last
    hover = f"{to_et(last.as_of):%d %b %H:%M} ET, {_age((now - last.as_of).total_seconds())}"
    return Provenance(last.source, row.tier, row.as_of_text, field_label(last), hover, flags(row))


def flags(row: Row) -> tuple[str, ...]:
    """The caveat words that stay visible beside the row's figures (P-022)."""
    out = ["proxy"] if row.is_proxy else []
    if any(q.startswith("IEX") for q in row.qualifiers):
        out.append("IEX")
    if any(q.startswith("fallback") for q in row.qualifiers):
        out.append("fallback")
    return tuple(out)


def _listed(text: str, p: Provenance) -> str:
    """A value in a Markets list: glyph and caveat words, as-of in the tooltip."""
    return value_cell(text, p, compact=True, show_as_of=False)


def _has_change(row: Row) -> bool:
    return row.change is not None and row.change_text not in ("n/a", "")


def _change(row: Row, p: Provenance) -> str:
    if not _has_change(row):
        return _no_change(NO_CHANGE)
    return change_cell(row.change_text, p, row.basis, compact=True, glyph=False)


def _no_change(text: str) -> str:
    return (
        f'<span class="db-flat">{text}'
        '<span class="db-sr"> (no earlier value to compare with)</span></span>'
    )


# ---------------------------------------------------------------------------
# top bar
# ---------------------------------------------------------------------------


def brand(ov: Overview) -> str:
    et = to_et(ov.as_of)
    return (
        '<div class="db-top"><span class="db-brand">Desk Brief</span>'
        f'<span class="db-when">{et:%a} {et.day} {et:%b} · {et:%H:%M} ET</span></div>'
    )


def status(ov: Overview, run: tuple[date, datetime | None] | None = None) -> str:
    """Pipeline health as one status dot and one line (P-023): "Last run Fri 07:00 · 14 checks
    ok". ``run`` is the latest run with checks (its target date and start time,
    ``data.Snapshot.last_run``); ``None`` means no run has recorded a check, the only case that
    says "No checks yet". The counts are also in the dot's CSS tooltip and for screen readers.
    The page puts a button over it that opens Data health in the same session."""
    s = ov.health.status
    text = escape(ov.health.text)
    if run is None:
        line = NO_CHECKS
    else:
        day, started = run
        when = f" {to_et(started):%H:%M}" if started and to_et(started).date() == day else ""
        summ = ov.health.summary
        total = summ.n_checks - summ.n_skip
        counts = f"{total} checks ok" if s == "ok" else text
        line = f'Last run {day:%a}{when}<span class="db-status-long"> · {counts}</span>'
    return (
        '<div class="db-status">'
        f'<span class="db-health db-health-{s} db-tipwrap">'
        f'<span class="db-dot db-dot-{s}"></span>{line}'
        f'<span class="db-sr"> · {text}</span>'
        f'<span class="db-tip" aria-hidden="true">{text}</span></span></div>'
    )


# ---------------------------------------------------------------------------
# market strip and biggest moves
# ---------------------------------------------------------------------------


Item = tuple[str, str]  # (instrument id, html of the selectable item)


def _day(ts: datetime) -> date:
    return to_et(ts).date()


def _own_close(ch: ChangeRow) -> bool:
    """A quoted (intraday) non-price value against its own previous close (T61: Yahoo ``^IRX``
    against Yahoo's ``^IRX`` close), not against an official value."""
    assert ch.previous is not None
    return ch.last.interval not in OFFICIAL_INTERVALS and like_for_like(ch.last, ch.previous)


def _exchange_word(basis: str) -> str | None:
    """ "Tokyo" for "previous Tokyo close": a foreign index's own exchange (T61)."""
    for home in HOME_EXCHANGES.values():
        if basis == home.basis:
            return basis.removeprefix("previous ").removesuffix(" close")
    return None


def move_label(row: Row) -> str:
    """The short basis of a row's 1D change, as the strip says it (P-023), from the change's own
    basis so the strip and the email always name the same baseline (T61).

    * a weekly value: "week on week";
    * an official value against the one before (a yield, a rate): the value's weekday ("Fri");
      a quote against its own previous close (a Yahoo yield quote): "vs Fri close"; a quote
      against an official baseline: "vs official";
    * a price on an XNYS session day: "vs Fri close", naming the baseline's day;
    * an FX pair against its own New York close: "vs NY close"; a foreign index against its own
      exchange's previous session: "vs Tokyo close";
    * a price on a day that is not an XNYS session (T60 bug 4; ``query.overnight_changes``
      then names the baseline "<weekday> close"): the last session's weekday ("Fri") when the
      value is from a session, else "since Fri close" (a market that traded on an XNYS
      holiday, compared with the last session's close).

    Empty when the row has no change to label."""
    ch = row.change
    if ch is None or ch.previous is None or not _has_change(row):
        return ""
    last, prev = ch.last, ch.previous
    if last.interval == "1w":
        return "week on week"
    if ch.family != "price":
        if _own_close(ch):
            return f"vs {_day(prev.as_of):%a} close"
        return f"{_day(last.as_of):%a}" if last.tier == 3 else "vs official"
    if ch.basis == BASIS_NYSE_CLOSE:
        return f"vs {_day(prev.as_of):%a} close"
    if ch.basis.startswith(BASIS_NY_CLOSE):
        return "vs NY close"
    exchange = _exchange_word(ch.basis)
    if exchange is not None:
        return f"vs {exchange} close"
    s = _day(last.as_of)
    return f"{s:%a}" if is_business_day("XNYS", s) else f"since {ch.basis}"


def basis_text(row: Row) -> str:
    """The longer basis inside the hero pill: what the 1D change spans, with dates. It names
    the same baseline as the email's basis text (T61)."""
    ch = row.change
    if ch is None or ch.previous is None or not _has_change(row):
        return ""
    last, prev = ch.last, ch.previous
    pd_ = _day(prev.as_of)
    then = f"{pd_.day} {pd_:%b}"
    if last.interval == "1w":
        return f"week on week, vs {then}"
    if ch.family != "price":
        if _own_close(ch):
            return f"vs {pd_:%a} {then} {source_name(prev.source)} close"
        return f"vs {then} official" + ("" if last.tier == 3 else " value")
    if ch.basis == BASIS_NYSE_CLOSE:
        return f"vs {pd_:%a} {then} close"
    if ch.basis.startswith(BASIS_NY_CLOSE):
        return f"vs {pd_:%a} {then} NY close ({to_et(prev.as_of):%H:%M} ET quote)"
    exchange = _exchange_word(ch.basis)
    if exchange is not None:
        return f"vs {pd_:%a} {then} {exchange} close"
    s = _day(last.as_of)
    if is_business_day("XNYS", s):
        return f"{s:%a} vs {pd_:%a} {then} close"
    return f"since {pd_:%a} {then} close"


def _is_weekday(label: str) -> bool:
    return label in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def strip_items(ov: Overview, sel: str = "") -> tuple[str, list[Item]]:
    """The market strip (P-023): one line of plain text per headline instrument, name, value
    and the change as coloured text; no tiles, pills or sparklines.

    Returns ``(lead, items)``: ``lead`` is the basis most items share, said once at the start
    of the strip ("Fri close"); an item whose basis differs says its own ("vs official")."""
    labels = {row.instrument_id: move_label(row) for _, row in ov.headline if row.available}
    sigs = sigmas(ov) if hasattr(ov, "moves") else {}
    checked = {i for c in getattr(ov, "caveats", ()) for i in c.instrument_ids}
    counts = Counter(v for v in labels.values() if v)
    common, n = counts.most_common(1)[0] if counts else ("", 0)
    common = common if n >= 2 else ""
    items = []
    for label, row in ov.headline:
        if not row.available:
            body = f'<span class="db-sx-val db-unavailable">{NO_DATA}</span>'
        else:
            p = provenance_of(row, ov.as_of)
            own = labels.get(row.instrument_id, "")
            tag = f'<em class="db-sx-basis">{escape(own)}</em>' if own and own != common else ""
            sig = sigma_tag(sigs.get(row.instrument_id))
            mark = check_mark() if row.instrument_id in checked else ""
            body = (
                f'<span class="db-sx-val">{_listed(row.value_text, p)}</span>'
                f'<span class="db-sx-chg">{tag}{_change(row, p)}</span>{sig}{mark}'
            )
        cls = "db-sx is-sel" if row.instrument_id == sel else "db-sx"
        items.append(
            (
                row.instrument_id,
                f'<a class="{cls}" data-db-id="{escape(row.instrument_id)}">'
                f'<span class="db-sx-name">{escape(label)}'
                f'<span class="db-sr"> · {escape(row.name)}</span></span>{body}</a>',
            )
        )
    lead = f"{common} close" if _is_weekday(common) else common
    return lead, items


def strip_lead(lead: str) -> str:
    """The shared basis at the start of the strip, or nothing."""
    if not lead:
        return ""
    return f'<div class="db-sx-lead"><span class="db-sr">Changes: </span>{escape(lead)}</div>'


_SIGMA = re.compile(r"is (\d+(?:\.\d+)?)σ")


def _across_gap(row: Row) -> bool:
    """The latest move spans a gap in the data (the tearsheet's rule: more than
    ``GAP_BDAYS`` business days between the two values), so no sigma is shown for it."""
    from app.components.tearsheet import GAP_BDAYS

    if row.change is None or row.change.previous is None:
        return False
    a, b = to_et(row.change.previous.as_of).date(), to_et(row.change.last.as_of).date()
    return int(np.busday_count(a, b)) > GAP_BDAYS


def sigmas(ov: Overview) -> dict[str, tuple[str, str]]:
    """The day's unusual moves as ``{instrument id: (sigma text, sentence for screen readers)}``.

    These are the report's own notable moves (at least the anomaly threshold away from each
    instrument's recent daily moves); P-022 shows them as a tag on the watchlist row. A move
    across a gap in the data gets no tag (the risk card says "no data" for it). The sentence
    names the instrument by its display name, never by its raw id."""
    rows = {r.instrument_id: r for s in ov.sections for r in s.rows}
    out: dict[str, tuple[str, str]] = {}
    for n in ov.moves:
        m = _SIGMA.search(n.text)
        iid = n.instrument_id
        if not iid or not m or iid in out:
            continue
        row = rows.get(iid)
        if row is not None and _across_gap(row):
            continue
        text = n.text.replace(iid, short(row)) if row is not None else n.text
        out[iid] = (f"{m.group(1)}σ", text)
    return out


def check_mark() -> str:
    """The "check" marker of a market named in Check before using (P-024)."""
    return (
        '<span class="db-mark db-mark-warn db-check">check<span class="db-sr"> (see Check '
        "before using)</span></span>"
    )


def sigma_tag(sig: tuple[str, str] | None) -> str:
    if sig is None:
        return ""
    return (
        f'<span class="db-sig">{escape(sig[0])}<span class="db-sr"> (unusual move: '
        f"{escape(sig[1])})</span></span>"
    )


# ---------------------------------------------------------------------------
# search and watchlist
# ---------------------------------------------------------------------------


def matches(row: Row, query: str) -> bool:
    """Every word of ``query`` appears in the row's id, short name, name or label."""
    hay = f"{row.instrument_id} {short(row)} {row.name} {row.label}".lower().replace("_", " ")
    hay += " " + row.instrument_id.lower()
    return all(word in hay for word in query.lower().split())


def search(sections: Iterable[Section], query: str) -> list[Section]:
    """Sections cut down to the rows that match, empty sections dropped."""
    out = []
    for s in sections:
        rows = tuple(r for r in s.rows if matches(r, query))
        if rows:
            out.append(Section(s.asset_class, s.title, rows))
    return out


def _row_tip(row: Row, now: datetime) -> str:
    """The hover content every watchlist row has (P-023): full name, field, tier, source and
    as-of. A CSS tooltip (``.db-wl-tip``), shown on hover and on keyboard focus of the row's
    button; ``aria-hidden`` because the row's screen-reader text says the same."""
    if not row.available:
        facts = "no data"
    else:
        p = provenance_of(row, now)
        facts = (
            f'<span class="db-field">{escape(p.field)}</span> · {TIER_GLYPH[p.tier]} '
            f'{TIER_NAME[p.tier]} · <span class="db-src">{escape(p.source)}</span> · '
            f'<span class="db-asof">{escape(p.hover or p.as_of)}</span>'
        )
    return (
        f'<span class="db-tip db-wl-tip" role="tooltip" aria-hidden="true">'
        f'<b>{escape(row.name)}</b><span class="db-wl-tip-l">{facts}</span></span>'
    )


def wl_row(
    row: Row,
    now: datetime,
    sel: str,
    spark: Sequence[float] = (),
    sig: tuple[str, str] | None = None,
    sub: str = "",
    check: bool = False,
) -> str:
    """One watchlist row (P-023): the short name (never truncated) with a sigma tag for an
    unusual move, the full name under it (truncated with an ellipsis), then the value and the
    change as coloured text. ``spark`` is accepted for callers of the T58 signature and not
    drawn (P-023: no sparklines in lists)."""
    del spark
    tag = '<span class="db-tag">proxy</span>' if row.is_proxy and not row.available else ""
    hover = "; ".join((row.label, *row.qualifiers))
    cls = "db-wl-row db-row is-sel" if row.instrument_id == sel else "db-wl-row db-row"
    mark = check_mark() if check else ""
    name = (
        f'<span class="db-wl-name"><b>{escape(short(row))}{tag}{sigma_tag(sig)}{mark}</b>'
        f"<small>{escape(sub or row.name)}</small>"
        f'<span class="db-sr"> ({escape(hover)})</span></span>'
    )
    if not row.available:
        cells = f'<span class="db-wl-gap db-unavailable">{NO_DATA}</span>'
    else:
        p = provenance_of(row, now)
        cells = (
            f'<span class="db-wl-val">{_listed(row.value_text, p)}</span>'
            f'<span class="db-wl-chg">{_change(row, p)}</span>'
        )
    current = ' aria-current="true"' if row.instrument_id == sel else ""
    return (
        f'<a class="{cls}" data-db-id="{escape(row.instrument_id)}"{current}>'
        f"{_row_tip(row, now)}{name}{cells}</a>"
    )


@dataclass(frozen=True)
class WatchGroup:
    title: str
    rows: list[Item]
    proxies: list[Item]  # folded behind one line; empty while searching
    #: instrument ids of every row, proxies included (the page keeps the selected one in view)
    ids: tuple[str, ...] = field(default=())

    @property
    def fold_label(self) -> str:
        noun = "proxy" if len(self.proxies) == 1 else "proxies"
        return f"Show {len(self.proxies)} ETF {noun}"

    @property
    def key(self) -> str:
        """A widget-key-safe name of the group."""
        return re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")

    def __len__(self) -> int:
        return len(self.rows) + len(self.proxies)


def watchlist(
    sections: Sequence[Section],
    now: datetime,
    sel: str = "",
    *,
    fold_proxies: bool = True,
    sparks: Mapping[str, Sequence[float]] | None = None,
    sigs: Mapping[str, tuple[str, str]] | None = None,
) -> list[WatchGroup]:
    """Every instrument grouped by asset class; ETF proxies fold behind one line unless
    searching. ``sigs`` are the sigma tags (:func:`sigmas`); ``sparks`` is accepted and not
    drawn (P-023)."""
    del sparks
    sigs = sigs or {}

    def item(r: Row) -> Item:
        iid = r.instrument_id
        return iid, wl_row(r, now, sel, (), sigs.get(iid))

    out = []
    for sec in sections:
        real = [r for r in sec.rows if not (fold_proxies and r.is_proxy)]
        proxies = [r for r in sec.rows if fold_proxies and r.is_proxy]
        out.append(
            WatchGroup(
                sec.title,
                [item(r) for r in real],
                [item(r) for r in proxies],
                tuple(r.instrument_id for r in sec.rows),
            )
        )
    return out


MOVERS_TITLE = "Moved more than usual"
#: the watchlist switch (P-024): unusual first by default
UNUSUAL, BY_GROUP = "Unusual first", "By group"


def mover_sub(row: Row, now: datetime) -> str:
    """The quiet line under a mover's name: source, tier and time ("Yahoo · delayed · 06:06")."""
    if not row.available or row.tier is None:
        return ""
    p = provenance_of(row, now)
    return f"{source_name(p.source)} · {TIER_NAME[p.tier]} · {row.as_of_text}"


def movers_group(ov: Overview, sel: str) -> WatchGroup:
    """The movers, largest sigma first, as the first watchlist group (P-024 "Unusual first"):
    the one ranking of ``BriefModel.movers``, each with its sigma tag and any check marker."""
    rows = {r.instrument_id: r for s in ov.sections for r in s.rows}
    sigs = sigmas(ov)
    checked = {i for c in ov.caveats for i in c.instrument_ids}
    items = []
    for m in ov.movers:
        row = rows.get(m.instrument_id)
        if row is None:
            continue
        body = wl_row(
            row, ov.as_of, sel, (), sigs.get(m.instrument_id), mover_sub(row, ov.as_of),
            m.instrument_id in checked,
        )  # fmt: skip
        items.append((m.instrument_id, body))
    return WatchGroup(MOVERS_TITLE, items, [], tuple(i for i, _ in items))


def without(sections: Sequence[Section], ids: Collection[str]) -> list[Section]:
    """Sections with the rows in ``ids`` left out, empty sections dropped ("everything else")."""
    out = []
    for s in sections:
        rows = tuple(r for r in s.rows if r.instrument_id not in ids)
        if rows:
            out.append(Section(s.asset_class, s.title, rows))
    return out


def group_title(title: str, *, folded: bool | None = None, count: int = 0) -> str:
    """A watchlist group header. ``folded`` is ``None`` for a header that does not fold (the
    T58 signature); otherwise a chevron says whether the group is open."""
    if folded is None:
        return f'<div class="db-wl-grp db-sec-title">{escape(title)}</div>'
    chev = "▸" if folded else "▾"
    state = "folded" if folded else "open"
    n = f'<span class="db-grp-n">{count}</span>' if count else ""
    return (
        f'<div class="db-wl-grp db-sec-title db-grp-h"><span aria-hidden="true" '
        f'class="db-grp-chev">{chev}</span>{escape(title)}{n}'
        f'<span class="db-sr"> ({state})</span></div>'
    )


# ---------------------------------------------------------------------------
# chart header, details and the overview grid
# ---------------------------------------------------------------------------


def _header_notes(row: Row) -> list[str]:
    """What the value is, beyond its name, without repeating a caveat word that the figure
    already shows (P-022 review: "proxy" once, not twice)."""
    out = []
    if row.is_proxy and row.proxy_label:
        out.append("tracks " + row.proxy_label.removeprefix("proxy for ").strip())
    for q in row.qualifiers:
        if q.startswith("IEX"):
            q = q.split(":", 1)[-1].strip()  # "single exchange, ..."; the IEX tag is shown
        elif q.startswith("fallback"):
            continue  # the fallback tag and the source line say it
        out.append(q)
    return out


def label(title: str, aside: str = "") -> str:
    """A section label over a hairline (P-023), with an optional quiet note on its right."""
    extra = f"<span>{escape(aside)}</span>" if aside else ""
    return f'<div class="db-label">{escape(title)}{extra}</div>'


def chart_header(row: Row, now: datetime) -> str:
    """The hero (P-023): its label (short name over a hairline, the full name on the right),
    the figure with its glyph and caveat words, one pill holding the 1D change and its basis,
    and one quiet provenance line: field, tier, as-of and source."""
    head = (
        f'<div class="db-label db-ch"><span class="db-ch-name">{escape(short(row))}</span>'
        f'<span class="db-ch-sub">{escape(row.name)}</span></div>'
    )
    if not row.available:
        return head + f'<div class="db-ch-value db-unavailable">{NO_DATA}</div>'
    p = provenance_of(row, now)
    if _has_change(row):
        tone = direction(row.change_text).removeprefix("db-")
        chg = change_cell(row.change_text, p, row.basis, compact=True, glyph=False)
        basis = basis_text(row)
        sig = f" · {row.sigma:.1f}σ" if row.sigma is not None else ""
        pill = (
            f'<span class="db-pill db-pill-{tone}">{chg}'
            + (f'<span class="db-pill-basis">1D · {escape(basis)}{sig}</span>' if basis else "")
            + "</span>"
        )
    else:
        why = row.change.note if row.change is not None and row.change.note else ""
        pill = f'<span class="db-pill-none">{_no_change("no change yet")}</span>' + (
            f'<span class="db-pill-why">{escape(why)}</span>' if why else ""
        )
    line = " · ".join(
        [
            f'<span class="db-field">{escape(p.field)}</span>',
            f"{tier_glyph(p.tier)} {TIER_NAME[p.tier]}",
            f'<span class="db-asof">{escape(p.hover or p.as_of)}</span>',
            f'<span class="db-src">{escape(p.source)}</span>',
            *(escape(n) for n in _header_notes(row)),
            *((escape(row.sigma_note),) if row.sigma_note else ()),
            *(
                (escape(f"not ranked: {row.change.unranked_reason}"),)
                if row.change is not None and row.change.unranked_reason
                else ()
            ),
        ]
    )
    return (
        head + '<div class="db-ch-price">'
        f'<span class="db-ch-value">{value_cell(row.value_text, p, compact=True, show_as_of=False)}'
        f"</span>{pill}</div>"
        f'<div class="db-ch-foot">{line}</div>'
    )


def section_title(title: str) -> str:
    return f'<div class="db-sec-title">{escape(title)}</div>'


# ---------------------------------------------------------------------------
# page footer and the Home stylesheet (T58)
# ---------------------------------------------------------------------------


def footer(ov: Overview, legend: str) -> str:
    """The quiet last line: display mode and tiers, any withheld instruments, and what the tier
    glyphs mean (``legend`` is ``kit.glyph_legend``)."""
    withheld = (
        f"<span>{ov.withheld} instrument(s) withheld in {escape(ov.mode)} mode: live (tier 1) "
        "data is not shown here until provider display terms are verified.</span>"
        if ov.withheld
        else ""
    )
    return (
        f'<div class="db-foot"><span class="db-mode">{escape(ov.mode_label)}'
        '<span class="db-sr"> (display mode and the data tiers it shows)</span></span>'
        f"{withheld}{legend}</div>"
    )


def _css() -> str:
    from app import theme as t

    pick = 'div[class*="st-key-db-pick-"]'
    wl = 'div[class*="st-key-db-pick-wl-"]'
    main = '[data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] .st-key-db-watch)'
    watch_col = '[data-testid="stColumn"]:has(.st-key-db-watch)'
    hero_col = '[data-testid="stColumn"]:has(.st-key-db-hero)'
    brief_col = '[data-testid="stColumn"]:has(.st-key-db-brief)'
    return f"""
<style>
/* ---- T60 Home v3 (P-023): no containers; labels over hairlines; four type sizes ---- */

/* top bar: the status line's tooltip opens below it, right-aligned */
.db-status .db-tip {{ left: auto; right: 0; bottom: auto; top: calc(100% + 8px);
  transform: none; }}
@media (max-width: 1280px) and (min-width: 961px) {{ .db-status-long {{ display: none; }} }}

/* market strip: one line of plain text, the shared basis said once at its start */
.st-key-db-strip {{ flex-wrap: wrap; align-items: baseline; gap: 8px 44px !important;
  padding: 14px 0 13px 0; border-bottom: 1px solid {t.DIVIDER}; margin-bottom: 8px; }}
.st-key-db-strip > div {{ flex: 0 0 auto !important; width: auto !important; min-width: 0; }}
.db-sx-lead {{ color: {t.META}; font-size: {t.SIZE_META}px; white-space: nowrap; }}
.db-sx {{ display: flex; align-items: baseline; gap: 10px; white-space: nowrap;
  color: {t.TEXT}; }}
.db-sx-name {{ color: {t.META}; font-size: {t.SIZE_BODY}px; }}
.db-sx.is-sel .db-sx-name {{ color: {t.TEXT}; }}
.db-sx-val {{ font-size: {t.SIZE_LG}px; font-weight: 500; }}
.db-sx .db-fig {{ display: inline-block; text-align: left; }}
.db-sx-chg {{ font-size: {t.SIZE_BODY}px; }}
.db-sx-basis {{ font-style: normal; color: {t.META}; font-size: {t.SIZE_META}px;
  margin-right: 5px; }}
.db-sx .db-sig {{ margin-left: 2px; }}
{pick}:hover .db-sx-name {{ color: {t.TEXT}; }}
@media (max-width: 1280px) {{ .st-key-db-strip {{ gap: 8px 28px !important; }}
  .db-sx {{ flex-direction: column; gap: 0; }} }}
@media (max-width: 640px) {{ .st-key-db-strip > div {{ flex: 1 1 28% !important; }} }}

/* three aligned panes (P-024, T62). The source order is the phone order (brief, watchlist,
   market); on a wide screen they sit watchlist (280 px), market, brief (380 px). Below 1280 px
   the brief moves above the other two as a two-column block (brief.BRIEF_CSS); below 960 px
   everything stacks in the source order, so a phone reads like the email. Each pane opens
   with a label over a hairline, so the three labels share one baseline. */
{main} {{ gap: {t.GAP}px; flex-wrap: nowrap; align-items: flex-start; }}
{watch_col} {{ flex: 0 0 280px !important; width: 280px !important; min-width: 0; order: 1; }}
{hero_col} {{ flex: 1 1 0 !important; width: auto !important; min-width: 0; order: 2; }}
{brief_col} {{ flex: 0 0 380px !important; width: 380px !important; min-width: 0; order: 3; }}
@media (max-width: 1280px) {{
  {main} {{ flex-wrap: wrap; gap: 40px; }}
  {brief_col} {{ flex: 1 1 100% !important; width: 100% !important; order: 0; }}
  {watch_col} {{ flex: 0 0 260px !important; width: 260px !important; }}
}}
@media (max-width: 960px) {{
  {watch_col}, {hero_col}, {brief_col} {{ flex: 1 1 100% !important; width: 100% !important;
    order: 0; }}
}}
/* the watchlist switch and the line under "Moved more than usual" */
.st-key-db-wl-switch {{ margin: -4px 0 6px 0; }}
.st-key-db-wl-switch [data-testid="stButtonGroup"] button {{ background: transparent; border: 0;
  color: {t.META}; min-height: 26px; padding: 0 10px; font-size: {t.SIZE_META}px; }}
.db-wl-since {{ padding: 0 10px 4px 10px; color: {t.META}; font-size: {t.SIZE_META}px; }}
.db-check {{ background: transparent; padding: 0; margin-left: 6px; }}

/* the hero: label, figure, pill, provenance line */
.db-ch .db-ch-name {{ text-transform: uppercase; letter-spacing: .09em; font-weight: 600;
  overflow: visible; flex: 0 0 auto; }}
.db-ch-price {{ display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }}
.db-ch-value {{ font-size: {t.SIZE_HERO}px; font-weight: 500; letter-spacing: -.025em;
  line-height: 1.05; color: {t.TEXT}; }}
.db-ch-value .db-fig {{ display: inline-flex; align-items: center; gap: 6px; text-align: left; }}
.db-ch-value .db-prov, .db-ch-value .db-flag {{ font-size: {t.SIZE_META}px; letter-spacing: 0; }}
.db-pill {{ display: inline-flex; align-items: baseline; gap: 8px; padding: 3px 9px;
  border-radius: {t.RADIUS}px; font-size: {t.SIZE_LG}px; font-weight: 500;
  background: {t.RAISED}; }}
.db-pill-up {{ background: {t.UP_TINT}; }}
.db-pill-down {{ background: {t.DOWN_TINT}; }}
.db-pill-down .db-val {{ color: {t.DOWN_PILL}; }}
.db-pill .db-fig {{ display: inline; }}
.db-pill-basis {{ color: {t.SECONDARY}; font-size: {t.SIZE_META}px; font-weight: 400; }}
.db-pill-none {{ font-size: {t.SIZE_BODY}px; }}
.db-pill-why {{ color: {t.META}; font-size: {t.SIZE_META}px; }}
.db-ch-foot {{ color: {t.META}; font-size: {t.SIZE_META}px; margin-top: 12px; }}
.db-ch-foot .db-tier {{ color: {t.META}; }}
@media (max-width: 640px) {{ .db-ch-value {{ font-size: {t.SIZE_HERO_PHONE}px; }} }}

/* range and style controls; Compare and CSV in "More" */
.st-key-db-ctrls {{ align-items: center; gap: 8px 20px !important; flex-wrap: wrap;
  margin-top: 18px; }}
.st-key-db-ctrls > div {{ width: auto !important; flex: 0 0 auto !important; }}
.st-key-db-ctrls > div:last-child {{ margin-left: auto; }}
.st-key-db-ctrls [data-testid="stButtonGroup"] button {{ background: transparent; border: 0;
  color: {t.SECONDARY}; min-height: 28px; padding: 0 10px; font-size: {t.SIZE_BODY}px; }}
[data-testid="stButtonGroup"] button[aria-checked="true"] {{
  background: {t.RAISED}; color: {t.TEXT}; border-radius: {t.RADIUS}px; }}
.st-key-db-ctrls [data-testid="stPopover"] button {{ background: transparent; border: 0;
  color: {t.SECONDARY}; font-size: {t.SIZE_BODY}px; }}

/* under the chart: the 1W-1Y horizons, "Charts by TradingView" at the right */
.db-under {{ display: flex; align-items: baseline; justify-content: space-between;
  gap: 10px 24px; flex-wrap: wrap; margin-top: 12px; padding-top: 12px;
  border-top: 1px solid {t.DIVIDER}; }}
.db-under .db-tv-attr {{ color: {t.META}; font-size: {t.SIZE_META}px; text-align: right;
  margin-left: auto; }}
.db-under .db-asofline {{ display: none; }}
.db-chfoot {{ color: {t.META}; font-size: {t.SIZE_META}px; margin-top: 8px; }}
@media (max-width: 640px) {{ .db-under {{ flex-direction: column; }}
  .db-under .db-tv-attr {{ margin-left: 0; text-align: left; }} }}

/* "Do the sources agree?" and "Risk and range" side by side, flat label-value rows */
.st-key-db-notes {{ margin-top: 28px; }}
.st-key-db-notes [data-testid="stHorizontalBlock"] {{ gap: {t.GAP}px; }}
.st-key-db-detail, .st-key-db-risk {{ gap: 0; min-width: 0; }}
.db-verdict {{ align-items: baseline; gap: 10px; margin: 0; }}
.db-verdict b {{ font-size: {t.SIZE_LG}px; }}
.db-verdict .db-dot {{ align-self: center; margin-right: 0; }}
.db-verdict-gap {{ color: {t.SECONDARY}; font-size: {t.SIZE_BODY}px; }}
.db-verdict-p, .db-verdict-sum {{ color: {t.META}; font-size: {t.SIZE_BODY}px;
  margin: 4px 0 0 0; overflow-wrap: anywhere; }}
.db-srcs {{ margin-top: 12px; }}
.db-src-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; column-gap: 16px;
  align-items: baseline; padding: 9px 0; border-top: 1px solid {t.DIVIDER};
  font-size: {t.SIZE_BODY}px; }}
.db-src-row > span {{ color: {t.SECONDARY}; min-width: 0; overflow-wrap: anywhere; }}
.db-src-row .db-tagline {{ white-space: normal; }}
.db-src-row > b {{ font-weight: 500; color: {t.TEXT}; text-align: right; white-space: nowrap; }}
.db-src-row .db-fig {{ text-align: right; }}
.db-facts {{ margin: 0; }}
.db-fact {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; column-gap: 16px;
  align-items: baseline; padding: 9px 0; border-top: 1px solid {t.DIVIDER}; }}
.db-fact:first-child {{ border-top: 0; padding-top: 0; }}
.db-fact > span {{ color: {t.SECONDARY}; font-size: {t.SIZE_BODY}px; min-width: 0; }}
.db-fact > b {{ font-weight: 500; font-size: {t.SIZE_BODY}px; color: {t.TEXT};
  text-align: right; }}
.db-fact .db-fig {{ text-align: right; }}
.db-gapnote {{ margin: 0 0 8px 0; }}
.db-rangebox {{ position: relative; padding: 10px 0 2px 0; border-top: 1px solid {t.DIVIDER}; }}
.db-range-head {{ display: flex; justify-content: space-between; align-items: baseline;
  gap: 12px; font-size: {t.SIZE_BODY}px; }}
.db-range-label {{ color: {t.SECONDARY}; }}
.db-range-head b {{ font-weight: 500; font-variant-numeric: tabular-nums; }}
.db-range .db-range-fill {{ top: 0; height: 2px; left: 0; border-radius: 2px;
  background: #3A4250; }}
.db-range-ends > span {{ position: static; min-width: 0; }}
.db-range-ends .db-fig {{ text-align: left; }}
.db-range-ends > span:last-child .db-fig {{ text-align: right; }}
/* the links sit in their own block under the range, never over its labels (T60 bug 1) */
.st-key-db-links {{ position: relative; clear: both; margin-top: 16px; padding-top: 12px;
  border-top: 1px solid {t.DIVIDER}; gap: 6px !important; }}
.st-key-db-links [data-testid="stButton"] button,
.st-key-db-links [data-testid="stPageLink"] a {{ color: {t.ACCENT}; font-size: {t.SIZE_BODY}px;
  padding: 0; min-height: 0; background: transparent; border: 0; justify-content: flex-start; }}
.st-key-db-links [data-testid="stPageLink"] a p, .st-key-db-links [data-testid="stButton"] p {{
  color: {t.ACCENT}; font-size: {t.SIZE_BODY}px; }}

/* the watchlist: full length, never an inner scroller */
.st-key-db-watch {{ gap: 0 !important; }}
.st-key-db-watch [data-testid="stVerticalBlock"] {{ gap: 0; }}
.db-wl-head {{ display: grid; grid-template-columns: minmax(0, 1fr) auto 64px; column-gap: 12px;
  padding: 0 10px; color: {t.META}; font-size: {t.SIZE_META}px; margin-top: -4px; }}
.db-wl-head span {{ text-align: right; }}
.db-wl-grp {{ padding: 18px 10px 6px 10px; }}
.db-grp-h {{ display: flex; align-items: baseline; gap: 6px; }}
.db-grp-chev {{ display: inline-block; width: 10px; }}
.db-grp-n {{ margin-left: auto; font-weight: 400; letter-spacing: 0; }}
{pick}:hover .db-grp-h {{ color: {t.TEXT}; }}
.db-wl-row {{ position: relative; display: grid; grid-template-columns: minmax(0, 1fr) auto 64px;
  column-gap: 12px; align-items: center; min-height: 44px; padding: 4px 10px;
  color: {t.TEXT}; font-size: {t.SIZE_BODY}px; }}
{pick}:hover .db-wl-row {{ background: {t.RAISED}; border-radius: {t.RADIUS}px; }}
.db-wl-row.is-sel {{ background: {t.RAISED}; border-radius: {t.RADIUS}px; }}
.db-wl-row.is-sel::before {{ content: ""; position: absolute; left: 0; top: 11px; bottom: 11px;
  width: 2px; border-radius: 2px; background: {t.ACCENT}; }}
/* names never break one letter per line: the short id never wraps, the full name truncates
   with an ellipsis after it (T60 bug 3) */
.db-wl-name {{ min-width: 0; display: flex; flex-direction: column; line-height: 1.3; }}
.db-wl-name b {{ display: flex; align-items: baseline; gap: 6px; font-weight: 600;
  white-space: nowrap; word-break: keep-all; }}
.db-wl-name small {{ display: block; min-width: 0; color: {t.META}; font-size: {t.SIZE_META}px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; word-break: keep-all; }}
.db-wl-val, .db-wl-chg {{ text-align: right; white-space: nowrap; min-width: 0; }}
.db-wl-val .db-fig, .db-wl-chg .db-fig {{ text-align: right; }}
.db-wl-val .db-flags {{ display: block; line-height: 1.2; }}
.db-wl-val .db-flag {{ margin-left: 4px; }}
.db-wl-gap {{ grid-column: 2 / 4; text-align: right; }}
.db-sig {{ color: {t.AMBER}; font-size: {t.SIZE_META}px; font-weight: 500; }}
/* every row's hover content: fixed, so no column, scroll area or iframe clips it; it opens
   on hover and on keyboard focus of the row's button (T60 bug 2) */
.db-tip.db-wl-tip {{ position: fixed; left: auto; right: auto; top: auto; bottom: auto;
  transform: translate(calc(-100% - 14px), -6px); z-index: 1000; }}
.db-wl-tip b, .db-wl-tip .db-wl-tip-l {{ display: block; }}
{wl}:hover .db-wl-tip, {wl}:has(button:focus-visible) .db-wl-tip {{ display: block; }}
{wl}:has(.db-prov:hover) .db-wl-tip, {wl}:has(.db-prov:focus) .db-wl-tip {{ display: none; }}
@media (max-width: 960px) {{
  .db-tip.db-wl-tip {{ transform: translateY(calc(-100% - 6px)); max-width: min(280px, 90vw); }}
}}

/* tabs under the main area */
.st-key-db-values {{ margin-top: 40px; }}
.st-key-db-values .db-table {{ max-width: 980px; }}
.db-values .db-src-m {{ display: none; }}
@media (max-width: 640px) {{
  .db-values .db-c-src {{ display: none; }}
  .db-values .db-src-m {{ display: block; white-space: normal; }}
  .db-values .db-tagline, .db-values td {{ white-space: normal; overflow-wrap: anywhere; }}
}}

/* footer */
.db-foot {{ margin-top: 40px; color: {t.META}; font-size: {t.SIZE_META}px; display: flex;
  gap: 8px 16px; flex-wrap: wrap; }}
.db-foot .db-glyphs {{ gap: 12px; }}
</style>
"""


#: the Home stylesheet (injected by ``app/views/markets.py`` after ``theme.apply()``)
HOME_CSS = _css()
