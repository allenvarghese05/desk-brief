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

# --- the answer first: is anything wrong today? ---
today = dq[dq["target_date"] == pd.Timestamp(ctx.today)] if not dq.empty else dq
rev = _kit.recent_table(ctx, "revisions", 14)
quar = _kit.recent_table(ctx, "quarantine", 14)
rev_w = q_w = 0
if not rev.empty:
    kept = _kit.keep_rows(rev, ("source",), exposed)
    rev_w, rev = len(rev) - len(kept), kept
if not quar.empty:
    kept = _kit.keep_rows(quar, ("source",), exposed)
    q_w, quar = len(quar) - len(kept), kept
latest_run = ""
if not today.empty:
    latest_run = str(today.sort_values("evaluated_at")["run_id"].iloc[-1])
    today = today[today["run_id"] == latest_run]
counts = today["status"].value_counts() if not today.empty else pd.Series(dtype=int)
n_fail, n_warn = int(counts.get("fail", 0)), int(counts.get("warn", 0))
n_pass = int(counts.get("pass", 0))
st.markdown(
    _kit.kpis(
        [
            (
                "Failing",
                str(n_fail),
                "fail" if n_fail else "ok",
                "checks in today's latest run" if latest_run else "no run today yet",
            ),
            ("Warnings", str(n_warn), "warn" if n_warn else "ok", "need a look, not action"),
            (
                "Passing",
                str(n_pass),
                "ok" if n_pass else "muted",
                f"of {len(today)} results" if len(today) else "no results yet",
            ),
            ("Corrections, 14 days", str(len(rev)), "muted", "values a source revised"),
            (
                "Quarantined, 14 days",
                str(len(quar)),
                "warn" if len(quar) else "ok",
                "fetches held back as bad",
            ),
        ]
    ),
    unsafe_allow_html=True,
)

_kit.section("Needs attention", f"Latest run {latest_run}" if latest_run else "")
if today.empty:
    st.markdown("No DQ results for today yet.")
else:
    rank = {"fail": 0, "warn": 1, "pass": 2, "skip": 3}
    bad = today[today["status"].isin(["fail", "warn"])].copy()
    if bad.empty:
        st.markdown(
            '<div class="db-n">Nothing failing or warning in the latest run.</div>',
            unsafe_allow_html=True,
        )
    else:
        bad = bad.assign(_r=bad["status"].map(rank)).sort_values(["_r", "check_type", "check_name"])
        by_type = bad.groupby("check_type")["status"].value_counts().unstack(fill_value=0)
        summary = " · ".join(
            f"{t}: {', '.join(f'{int(n)} {s}' for s, n in row.items() if n)}"
            for t, row in by_type.iterrows()
        )
        st.markdown(f'<div class="db-n">{_kit.escape(summary)}</div>', unsafe_allow_html=True)
        # one line per check, source and status: which instruments, and the observed range
        lines = []
        keys = ["_r", "status", "check_type", "check_name", "source"]
        for (_, status, ctype, name, source), g in bad.fillna({"source": "no source"}).groupby(
            keys, sort=True, dropna=False
        ):
            ids = sorted({str(i) for i in g["instrument_id"].dropna()})
            shown = ", ".join(ids[:8]) + (f" and {len(ids) - 8} more" if len(ids) > 8 else "")
            obs = g["observed"].dropna()
            thr = g["threshold"].dropna()
            seen = (
                f"observed {obs.min():g}" + (f" to {obs.max():g}" if obs.max() != obs.min() else "")
                if len(obs)
                else ""
            )
            limit = f" · limit {thr.iloc[0]:g}" if len(thr) else ""
            what = name if name == ctype else f"{ctype} · {name}"
            mark = {"fail": "✕", "warn": "!"}.get(str(status), "")
            lines.append(
                f'<tr><td><span class="db-st-{_kit.escape(str(status))}">{mark} '
                f"{_kit.escape(str(status))}</span></td>"
                f"<td><b>{_kit.escape(str(what))}</b><span class='db-note'> · "
                f"{_kit.escape(str(source))}</span><br><span class='db-n'>"
                f"{len(g)} {'result' if len(g) == 1 else 'results'}"
                f"{': ' + _kit.escape(shown) if shown else ''}</span></td>"
                f"<td class='db-n'>{_kit.escape(seen + limit)}</td></tr>"
            )
        st.markdown(
            '<table class="db-table"><thead><tr><th>Status</th><th>Check, source and what it '
            "hit</th><th>Observed vs limit</th></tr></thead>"
            f"<tbody>{''.join(lines)}</tbody></table>",
            unsafe_allow_html=True,
        )
    for ctype, group in today.groupby("check_type"):
        c = group["status"].value_counts()
        summary = ", ".join(f"{n} {s}" for s, n in c.items())
        with st.expander(f"All {ctype} results · {summary}"):
            st.dataframe(
                group[["status", "check_name", "source", "instrument_id", "observed", "threshold"]],
                hide_index=True,
                width="stretch",
            )

# --- freshness timeline ---
_kit.section("Freshness, last 7 days", "One square per day: did each dataset arrive on time?")
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

# --- corrections and quarantine ---
left, right = st.columns(2, gap="large")
with left:
    _kit.section("Corrections, last 14 days", "A source changed a value it had already published.")
    if rev.empty:
        st.markdown("No revisions detected.")
    else:
        st.dataframe(
            rev[["detected_at", "instrument_id", "source", "field", "as_of", "old_value",
                 "new_value"]].sort_values("detected_at", ascending=False),
            hide_index=True,
            width="stretch",
        )  # fmt: skip
    _kit.withheld_note(rev_w, ctx.mode)
with right:
    _kit.section("Quarantine, last 14 days", "Fetches held back because they failed a sanity rule.")
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
