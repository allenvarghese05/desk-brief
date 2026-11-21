"""Cross-checks (spec 10.3 page 3, P-022 template since T59): do independent sources agree?

Four blocks: the headline counts, the results (problems first, skipped checks in their own
tab), the gap chart of one check against its tolerance band (chart and table in tabs), and the
page footer with the display mode and the glyph legend.

Squares are labelled by the date the check compared (``details.ref_date``), never by the run's
``target_date``. Hosted mode withholds checks that name a source with a tier-1 feed. A check's
latest gap is a figure, so it goes through ``value_cell`` (compact) with the sources compared,
the most live tier they serve, the date compared and what it is.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from app.components import kit as _kit  # noqa: E402
from app.components.values import Provenance, value_cell  # noqa: E402

ctx = _kit.context("Cross-checks")
RANK = {"fail": 0, "warn": 1, "pass": 2, "skip": 3}


def _gap(x: float) -> str:
    return "no gap computed" if pd.isna(x) else f"{x:+.1f} bp"


def readable(name: str) -> str:
    """``spy_vs_es`` -> ``SPY vs ES``: what the check compares, in words."""
    parts = name.split("_vs_")
    if len(parts) == 2:
        return " vs ".join(p.replace("_", " ").upper() for p in parts)
    return name.replace("_", " ").capitalize()


df = _kit.serving_frame(ctx, "crosschecks_7d.parquet")
if df.empty:
    with _kit.card("xc-none", "Results"):
        st.markdown("No cross-check results in the last 7 days.")
    _kit.page_footer(ctx)
    st.stop()
exposed = _kit.exposed_sources(ctx.config, ctx.hidden)
shown = _kit.keep_rows(df, ("left_source", "right_source"), exposed)
withheld = len(df) - len(shown)
df = shown
df = df.assign(group_id=df["group_id"].fillna(""))
df = df.sort_values(["ref_date", "at_time"], na_position="first")
keys = sorted(set(zip(df["group_id"], df["check_name"], strict=True)))


def _latest_real(group: str, name: str) -> str:
    """The latest result that actually compared something; ``skip`` only if none did."""
    c = df[(df["group_id"] == group) & (df["check_name"] == name)]
    real = c[c["status"] != "skip"]
    return str((real if not real.empty else c).iloc[-1]["status"])


def _gap_cell(last: pd.Series) -> str:
    """The latest gap: a figure with its provenance (compact), or the plain "no gap" text."""
    text = _gap(last["diff_bps"])
    tiers = [
        t
        for t in (
            _kit.feed_tier(ctx.config, str(last["left_source"]), str(last["left_instrument"])),
            _kit.feed_tier(ctx.config, str(last["right_source"]), str(last["right_instrument"])),
        )
        if t is not None
    ]
    if pd.isna(last["diff_bps"]) or not tiers or pd.isna(last["ref_date"]):
        return f'<span class="db-unavailable">{_kit.escape(text)}</span>'
    p = Provenance(
        f"{last['left_source']} vs {last['right_source']}",
        min(tiers),
        _kit.day_text(last["ref_date"]),
        f"gap between the two sources, basis points ({readable(str(last['check_name']))})",
    )
    return value_cell(text, p)


def _row(group: str, name: str) -> str:
    c = df[(df["group_id"] == group) & (df["check_name"] == name)].tail(7)
    items = [
        (
            _kit.day_text(r.ref_date),
            r.status,
            f"compared data of {_kit.day_text(r.ref_date)}: {r.status}, {_gap(r.diff_bps)} "
            f"(warn {r.warn_bps:g}, fail {r.fail_bps:g})",
        )
        for r in c.itertuples()
    ]
    real = c[c["status"] != "skip"]
    last = (real if not real.empty else c).iloc[-1]
    first_d, last_d = _kit.day_text(c["ref_date"].iloc[0]), _kit.day_text(c["ref_date"].iloc[-1])
    pair = f"{last['left_source']} vs {last['right_source']}"
    return (
        f"<tr><td><span class='db-xname'>{_kit.escape(readable(name))}</span>"
        f"<span class='db-note'> · {_kit.escape(group or 'no group')} · {_kit.escape(pair)}</span>"
        f"<br><span class='db-id'>{_kit.escape(name)}</span></td>"
        f"<td>{_kit.status_strip(items)}</td>"
        f"<td class='num'>{_gap_cell(last)}"
        f"<span class='db-tagline'>warn {last['warn_bps']:g} · fail {last['fail_bps']:g}"
        "</span></td>"
        f"<td class='db-n'>data {first_d} to {last_d}</td></tr>"
    )


latest = {k: _latest_real(*k) for k in keys}
ran = [k for k in keys if latest[k] != "skip"]
skipped = [k for k in keys if latest[k] == "skip"]
n = {s: sum(1 for k in ran if latest[k] == s) for s in ("fail", "warn", "pass")}

# --- block 1: the answer first ---
st.markdown(
    _kit.kpis(
        [
            (
                "Failing",
                str(n["fail"]),
                "fail" if n["fail"] else "ok",
                "sources disagree past fail",
            ),
            ("Warnings", str(n["warn"]), "warn" if n["warn"] else "ok", "gap above the warn line"),
            ("Agreeing", str(n["pass"]), "ok" if n["pass"] else "muted", "within tolerance"),
            ("Never compared", str(len(skipped)), "muted", "skipped every run this week"),
        ]
    ),
    unsafe_allow_html=True,
)

# --- block 2: results, problems first; skipped checks one tab away ---
head = (
    "<thead><tr><th>Check</th><th>Last 7 days</th><th class='num'>Latest gap</th>"
    "<th>Compared</th></tr></thead>"
)
order = sorted(ran, key=lambda k: (RANK.get(str(latest[k]), 9), k[1]))
with _kit.card(
    "xc-results",
    "Results, problems first",
    "Status and gap are each check's latest real comparison (a run with nothing to compare "
    "is a grey square, not a result). Squares are labelled by the date of the data compared.",
):
    st.markdown(_kit.status_legend(), unsafe_allow_html=True)
    compared, folded = st.tabs([f"Compared ({len(order)})", f"Skipped ({len(skipped)})"])
    with compared:
        st.markdown(
            _kit.scroll(
                f'<table class="db-table">{head}<tbody>{"".join(_row(*k) for k in order)}'
                "</tbody></table>"
            )
            if order
            else '<div class="db-n">No check compared anything this week.</div>',
            unsafe_allow_html=True,
        )
    with folded:
        st.markdown(
            f'<div class="db-n">{len(skipped)} skipped '
            f"{'check' if len(skipped) == 1 else 'checks'}: nothing to compare all week.</div>"
            + _kit.scroll(
                f'<table class="db-table">{head}<tbody>{"".join(_row(*k) for k in skipped)}'
                "</tbody></table>"
            )
            if skipped
            else '<div class="db-n">Every check compared something this week.</div>',
            unsafe_allow_html=True,
        )
    _kit.withheld_note(withheld, ctx.mode)

# --- block 3: one check's gap against its tolerance band ---
label = {f"{g or 'no group'} · {nm}": (g, nm) for g, nm in keys}
with _kit.card("xc-gap", "Gap chart", controls=True) as controls:
    with controls:  # type: ignore[attr-defined]
        pick = st.selectbox("Check", list(label), key="check", label_visibility="collapsed")
    group, name = label[pick]
    c = df[(df["group_id"] == group) & (df["check_name"] == name)]
    warn, fail = float(c["warn_bps"].iloc[-1]), float(c["fail_bps"].iloc[-1])
    line = _kit.Line(
        "gap",
        tuple(  # skipped checks have no gap and an unknown data date: nothing to plot
            (_kit.day_text(r.ref_date), float(r.diff_bps))
            for r in c.itertuples()
            if pd.notna(r.diff_bps) and pd.notna(r.ref_date)
        ),
        _kit.TIER_COLOUR[3],
    )
    table = pd.DataFrame(  # text, so a skipped check reads "no gap computed", never NaN
        {
            "compared": [_kit.day_text(d) for d in c["ref_date"]],
            "status": c["status"],
            "gap": [_gap(x) for x in c["diff_bps"]],
            "warn (bp)": c["warn_bps"],
            "fail (bp)": c["fail_bps"],
            "left source": c["left_source"],
            "right source": c["right_source"],
        }
    )
    svg = _kit.line_chart([line], "bps", band=(-warn, warn), guides=(-fail, fail))
    chart_tab, table_tab = st.tabs(["Chart", "Table"])
    with chart_tab:
        st.markdown(
            svg or '<span class="db-unavailable">no data in this window</span>',
            unsafe_allow_html=True,
        )
    with table_tab:
        st.dataframe(table, hide_index=True, width="stretch")
    _kit.card_footer(
        f"Gap in basis points by compared data date, {readable(name)} "
        f"({c['left_source'].iloc[-1]} vs {c['right_source'].iloc[-1]}). Shaded: within warn "
        f"(±{warn:g} bp). Dashed: fail (±{fail:g} bp)."
    )

_kit.page_footer(ctx)
