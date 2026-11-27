"""The Company section of Home's middle pane, single stocks only (P-024, card T67).

Filing-based facts from SEC EDGAR (stored by T65 in the ``filings`` table and read through
``query.company_filings``, which applies ``schemas.current_rows`` and keeps one amendment per
original):

* the latest 10-Q or 10-K, with its filed date, period and a link to the filing on EDGAR;
* insiders in the last ``filings.insider_days``: sells, buys, the net value and how many trades
  were under a pre-set Rule 10b5-1 plan (Form 4);
* big-holder changes from 13F for the latest quarter, with that as-of quarter and the note that
  13F runs about 45 days behind.

Every fact names its source and date, like any other figure. Instruments of any other kind
(ETFs, indices, futures) never show the section. No earnings date and no outbound link to a
third-party research tool until the owner decides (P-024 open questions). Flat like every Home
block (P-023): a label over a hairline, label-value rows, no panel; no ``title`` attributes.
"""

from __future__ import annotations

from datetime import date
from html import escape

from deskbrief.query import CompanyFilings, HolderChange

EMPTY = "No filings stored yet"
UNREADABLE = "Filings could not be read"
HOLDER_LAG = "13F runs about 45 days behind"
#: holder changes listed by name under the summary (largest change first)
HOLDERS_LISTED = 3


def _d(d: date | None) -> str:
    return f"{d.day} {d:%b %Y}" if d is not None else "date not given"


def _usd(x: float) -> str:
    sign = "−" if x < 0 else "+" if x > 0 else ""  # noqa: RUF001 (a true minus sign)
    v = abs(x)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"{sign}${v / div:,.1f}{suffix}"
    return f"{sign}${v:,.0f}"


def _shares(x: float) -> str:
    sign = "−" if x < 0 else "+" if x > 0 else ""  # noqa: RUF001 (a true minus sign)
    return f"{sign}{abs(x):,.0f}"


def _count(n: int, noun: str, tone: str) -> str:
    """ "4 sells", "1 buy", "0 buys" (coloured only when non-zero)."""
    text = f"{n} {noun}{'' if n == 1 else 's'}"
    return f'<span class="{tone}">{text}</span>' if n else text


def _row(label: str, value: str, note: str) -> str:
    return (
        f'<div class="db-co-row"><dt>{escape(label)}</dt><dd>{value}</dd>'
        f'<small class="db-co-note">{note}</small></div>'
    )


def _holder(h: HolderChange, own_period: bool = False) -> str:
    tone = "db-up" if h.shares_change > 0 else "db-down" if h.shares_change < 0 else "db-flat"
    when = f" · as of {_d(h.period)}" if own_period else ""
    return (
        f'<li><span class="db-co-party">{escape(h.party)}{when}</span>'
        f'<span class="{tone}">{_shares(h.shares_change)} sh</span></li>'
    )


def section_html(co: CompanyFilings | None, insider_days: int, *, listed: bool = True) -> str:
    """The Company section's markup. ``co`` None means the store could not be read (said so,
    never shown as "nothing stored"); ``listed`` False means the stock is not under
    ``filings.companies`` in ``config.yaml``, so nothing is fetched for it."""
    head = '<div class="db-label db-co-label">Company<span>SEC EDGAR filings</span></div>'
    if co is None:
        return (
            f'<section class="db-co">{head}<p class="db-empty">{UNREADABLE} from the store just '
            "now; the rest of the page is unaffected.</p></section>"
        )
    if (
        co.latest_filing is None
        and not co.insiders.trades
        and not co.holders
        and not co.insiders.n_ambiguous  # the "check" flag is never hidden by the empty state
    ):
        why = (
            "Filings come from SEC EDGAR when the source is turned on."
            if listed
            else "This stock is not listed under filings.companies in config.yaml, so no "
            "filings are fetched for it."
        )
        return f'<section class="db-co">{head}<p class="db-empty">{EMPTY}. {why}</p></section>'
    rows = []
    f = co.latest_filing
    if f is not None:
        period = f" · period {_d(f.period)}" if f.period else ""
        link = (
            f'<a class="db-co-link" href="{escape(f.url)}" target="_blank" '
            f'rel="noopener noreferrer">open filing on EDGAR</a>'
        )
        rows.append(
            _row(
                "Latest filing",
                f"{escape(f.form)}, {_d(f.filed_date)}",
                f"SEC EDGAR · filed {_d(f.filed_date)}{period} · {link}",
            )
        )
    else:
        rows.append(_row("Latest filing", "none stored", "SEC EDGAR · 10-Q and 10-K"))
    ins = co.insiders
    sells = _count(ins.n_sells, "sell", "db-down")
    buys = _count(ins.n_buys, "buy", "db-up")
    n_trades = ins.n_buys + ins.n_sells
    plans = f" · {ins.n_10b5_1} of {n_trades} under 10b5-1 plans" if n_trades else ""
    unpriced = f" · {ins.n_unpriced} without a price" if ins.n_unpriced else ""
    check, unsure = "", ""
    if ins.n_ambiguous or ins.n_held_back:
        # PR #102 review rounds 3-4: an amendment left out or not refreshed; the trades shown
        # may miss or repeat one, so the line says "check" and why
        check = (
            ' <span class="db-mark db-mark-warn db-check">check<span class="db-sr"> (some '
            "amendments are not counted as filed)</span></span>"
        )
        unsure = "".join(_amendment_notes(ins.n_ambiguous, ins.n_held_back))
    rows.append(
        _row(
            f"Insiders, {insider_days} days",
            f"{sells} · {buys}{check}",
            f"Form 4 · net {_usd(ins.net_usd)}{plans}{unpriced}{unsure} · filed since "
            f"{_d(ins.since)}",
        )
    )
    if co.holders:
        added = sum(1 for h in co.holders if h.shares_change > 0)
        cut = sum(1 for h in co.holders if h.shares_change < 0)
        differ = co.holders_periods_differ  # mid filing window: each holder's own quarter
        names = "".join(_holder(h, differ) for h in co.holders[:HOLDERS_LISTED])
        as_of = "each holder's latest 13F" if differ else f"as of {_d(co.holders_period)}"
        rows.append(
            _row(
                "Big holders, last quarter",
                f"{added} added · {cut} cut",
                f'13F · {as_of}, {HOLDER_LAG}<ul class="db-co-holders">{names}</ul>',
            )
        )
    else:
        rows.append(_row("Big holders, last quarter", "none stored", f"13F · {HOLDER_LAG}"))
    return f'<section class="db-co">{head}<dl class="db-co-grid">{"".join(rows)}</dl></section>'


def marker(co: CompanyFilings | None) -> tuple[date, str] | None:
    """The chart marker of the latest 10-Q or 10-K: its filed date and "10-Q filed"."""
    if co is None or co.latest_filing is None:
        return None
    return co.latest_filing.filed_date, f"{co.latest_filing.form} filed"


def _css() -> str:
    from app import theme as t

    return f"""
<style>
/* ---- T67 the Company section (P-024): flat label-value rows, two columns ---- */
.db-co {{ margin-top: 30px; font-size: {t.SIZE_BODY}px; }}
.db-co li {{ font-size: inherit; }}
.db-co-grid {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  column-gap: 32px; margin: 0; }}
.db-co-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; column-gap: 16px;
  padding: 9px 0; border-top: 1px solid {t.DIVIDER}; min-width: 0; }}
.db-co-row:nth-child(-n+2) {{ border-top: 0; padding-top: 0; }}
.db-co-row dt {{ color: {t.SECONDARY}; font-size: {t.SIZE_BODY}px; min-width: 0; }}
.db-co-row dd {{ margin: 0; text-align: right; font-weight: 500; white-space: nowrap;
  font-size: {t.SIZE_BODY}px; font-variant-numeric: tabular-nums; }}
.db-co-note {{ grid-column: 1 / -1; color: {t.META}; font-size: {t.SIZE_META}px; }}
.db-co-note a.db-co-link {{ color: {t.ACCENT} !important; }}
.db-co-holders {{ list-style: none; margin: 4px 0 0 0; padding: 0; }}
.db-co-holders li {{ display: flex; justify-content: space-between; gap: 12px; margin: 0; }}
.db-co-party {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
@media (max-width: 640px) {{
  .db-co-grid {{ grid-template-columns: minmax(0, 1fr); }}
  .db-co-row:nth-child(2) {{ border-top: 1px solid {t.DIVIDER}; padding-top: 9px; }}
}}
</style>
"""


#: the Company section's stylesheet (injected by ``app/views/markets.py``)
COMPANY_CSS = _css()


def _amendment_notes(n_ambiguous: int, n_held_back: int) -> list[str]:
    """Why the insider line says "check": Form 4/As left out of, or not refreshed in, the
    counts (PR #102 review rounds 3-4, N3)."""
    out = []
    if n_ambiguous:
        s = "s" if n_ambiguous != 1 else ""
        out.append(
            f" · {n_ambiguous} amendment{s} could not be matched to one original or "
            "restate one a later amendment covers, so the possible originals or the earlier "
            "amendment are left out"
        )
    if n_held_back:
        s = "s" if n_held_back != 1 else ""
        out.append(
            f" · {n_held_back} amendment{s} held back because a possible original could not "
            "be read, so the trade shown may be the one before the amendment, or missing"
        )
    return out
