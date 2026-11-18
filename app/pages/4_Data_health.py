"""Data health (spec 10.3 page 4): today's DQ results by type, freshness over the last 7 days,
the revisions log and the quarantine list. Hosted mode withholds rows naming a source with a
tier-1 feed (a validity or revision row can carry the value itself).
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from app.components import data  # noqa: E402
from app.components import kit as _kit  # noqa: E402

ctx = _kit.context("Data health")
WINDOW = 7
exposed = _kit.exposed_sources(ctx.config, ctx.hidden)


@st.cache_data(ttl=60, show_spinner=False)
def _dq(_ctx: _kit.Ctx, where: str) -> pd.DataFrame:  # `where` only keys the cache
    conn = data.open_connection(_ctx.config, os.environ)
    try:
        return conn.execute(
            "SELECT run_id, check_type, check_name, source, instrument_id, target_date, "
            "evaluated_at, "
            "status, observed, threshold FROM dq WHERE target_date > ? "
            "ORDER BY target_date, check_type, check_name",
            [_ctx.today - timedelta(days=WINDOW)],
        ).fetchdf()
    finally:
        conn.close()


where = _kit.where_key(ctx.config)
dq = _dq(ctx, where)
withheld = 0
if not dq.empty:
    kept = _kit.keep_rows(dq, ("source",), exposed)
    withheld = len(dq) - len(kept)
    dq = kept

# --- today ---
st.subheader("Today")
st.markdown(_kit.status_legend(), unsafe_allow_html=True)
today = dq[dq["target_date"] == pd.Timestamp(ctx.today)] if not dq.empty else dq
if today.empty:
    st.markdown("No DQ results for today yet.")
else:
    latest_run = today.sort_values("evaluated_at")["run_id"].iloc[-1]
    today = today[today["run_id"] == latest_run]
    st.markdown(
        f'<div class="db-n">Latest run {_kit.escape(str(latest_run))}, {len(today)} results.</div>',
        unsafe_allow_html=True,
    )
    for ctype, group in today.groupby("check_type"):
        counts = group["status"].value_counts()
        worst = next((s for s in ("fail", "warn", "pass", "skip") if s in counts), "skip")
        summary = ", ".join(f"{n} {s}" for s, n in counts.items())
        with st.expander(f"{ctype} · {summary}", expanded=worst in ("fail", "warn")):
            st.dataframe(
                group[["status", "check_name", "source", "instrument_id", "observed", "threshold"]],
                hide_index=True,
                width="stretch",
            )

# --- freshness timeline ---
st.subheader("Freshness, last 7 days")
fresh = dq[dq["check_type"] == "freshness"] if not dq.empty else dq
if fresh.empty:
    st.markdown("No freshness results in the last 7 days.")
else:
    fresh = fresh.sort_values("evaluated_at").drop_duplicates(
        ["check_name", "target_date"], keep="last"
    )
    rows = []
    for name, g in fresh.groupby("check_name"):
        items = [
            (
                f"{r.target_date:%d %b}",
                r.status,
                f"{r.target_date:%d %b}: {r.status}, {r.observed:g} minutes late "
                f"(grace {r.threshold:g})"
                if pd.notna(r.observed) and pd.notna(r.threshold)
                else f"{r.target_date:%d %b}: {r.status}",
            )
            for r in g.itertuples()
        ]
        late = g["observed"].dropna()
        grace = g["threshold"].dropna()
        latest = (
            f"latest {late.iloc[-1]:g} minutes late (grace {grace.iloc[-1]:g})"
            if len(late) and len(grace)
            else "minutes late n/a"
        )
        rows.append(
            f"<tr><td>{_kit.escape(str(name))}</td><td>{_kit.status_strip(items)}</td>"
            f"<td class='db-n num'>{latest}</td></tr>"
        )
    st.markdown(
        '<table class="db-table"><thead><tr><th>Dataset check</th><th>Arrival by day</th>'
        f"<th>Expected vs actual</th></tr></thead><tbody>{''.join(rows)}</tbody></table>",
        unsafe_allow_html=True,
    )
_kit.withheld_note(withheld, ctx.mode)

# --- revisions and quarantine ---
st.subheader("Revisions, last 14 days")
rev = _kit.recent_table(ctx, "revisions", 14)
rev_w = 0
if not rev.empty:
    kept = _kit.keep_rows(rev, ("source",), exposed)
    rev_w, rev = len(rev) - len(kept), kept
if rev.empty:
    st.markdown("No revisions detected.")
else:
    st.dataframe(
        rev[
            ["detected_at", "instrument_id", "source", "field", "as_of", "old_value", "new_value"]
        ].sort_values("detected_at", ascending=False),
        hide_index=True,
        width="stretch",
    )
_kit.withheld_note(rev_w, ctx.mode)

st.subheader("Quarantine, last 14 days")
quar = _kit.recent_table(ctx, "quarantine", 14)
q_w = 0
if not quar.empty:
    kept = _kit.keep_rows(quar, ("source",), exposed)
    q_w, quar = len(quar) - len(kept), kept
if quar.empty:
    st.markdown("Nothing quarantined.")
else:
    st.dataframe(
        quar[["detected_at", "source", "dataset", "reason", "detail", "raw_key"]].sort_values(
            "detected_at", ascending=False
        ),
        hide_index=True,
        width="stretch",
    )
_kit.withheld_note(q_w, ctx.mode)
