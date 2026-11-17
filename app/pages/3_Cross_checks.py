"""Cross-checks (spec 10.3 page 3): one row per check and group with the last 7 days as a status
strip; pick one for its gap chart against the tolerance band.

Squares are labelled by the date the check compared (``details.ref_date``), never by the run's
``target_date``. Hosted mode withholds checks that name a source with a tier-1 feed.
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
from app.pages import _kit  # noqa: E402

ctx = _kit.context("Cross-checks")


def _gap(x: float) -> str:
    return "no gap computed" if pd.isna(x) else f"{x:+.1f} bp"


df = _kit.serving_frame(ctx, "crosschecks_7d.parquet")
if df.empty:
    st.markdown("No cross-check results in the last 7 days.")
    st.stop()
exposed = _kit.exposed_sources(ctx.config, ctx.hidden)
shown = _kit.keep_rows(df, ("left_source", "right_source"), exposed)
withheld = len(df) - len(shown)
df = shown
df = df.assign(group_id=df["group_id"].fillna(""))
df = df.sort_values(["ref_date", "at_time"], na_position="first")
keys = sorted(set(zip(df["group_id"], df["check_name"], strict=True)))

st.markdown(_kit.status_legend(), unsafe_allow_html=True)
rows = []
for group, name in keys:
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
    first, last = _kit.day_text(c["ref_date"].iloc[0]), _kit.day_text(c["ref_date"].iloc[-1])
    rows.append(
        f"<tr><td>{_kit.escape(name)}<span class='db-note'> · {_kit.escape(group or 'no group')}"
        f"</span></td><td>{_kit.status_strip(items)}</td>"
        f"<td class='db-n'>data {first} to {last}</td></tr>"
    )
st.markdown(
    '<table class="db-table"><thead><tr><th>Check</th><th>Last 7 days</th><th>Compared</th>'
    f"</tr></thead><tbody>{''.join(rows)}</tbody></table>",
    unsafe_allow_html=True,
)
_kit.withheld_note(withheld, ctx.mode)

label = {f"{g or 'no group'} · {n}": (g, n) for g, n in keys}
pick = st.selectbox("Gap chart", list(label), key="check")
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
st.markdown(
    f'<div class="db-n">Gap in basis points by compared data date. Shaded: within warn '
    f"(±{warn:g} bp). Dashed: fail (±{fail:g} bp).</div>",
    unsafe_allow_html=True,
)
table = c.assign(compared=c["ref_date"])[
    ["compared", "status", "diff_bps", "warn_bps", "fail_bps", "left_source", "right_source"]
]
_kit.show_chart(
    _kit.line_chart([line], "bps", band=(-warn, warn), guides=(-fail, fail)), table, "gap"
)
