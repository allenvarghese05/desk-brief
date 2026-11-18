"""Runs (spec 10.3 page 6): the last 14 days of runs and the incidents list from INCIDENTS.md."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from app.components import incidents as _incidents  # noqa: E402
from app.components import kit as _kit  # noqa: E402

ctx = _kit.context("Runs")
ACTIONS = "https://github.com/allenvarghese05/desk-brief/actions"
STATUS_MARK = {"ok": "✓ ok", "degraded": "! degraded", "failed": "✕ failed"}


def _runs() -> pd.DataFrame:
    """The ``runs`` table itself (has the newest run) plus the serving file, newest first.

    ``runs_14d.parquet`` is written before the run that writes it finishes, so it always
    lacks the latest run; the table partitions do not."""
    frames = [f for f in (_kit.recent_table(ctx, "runs", 14),
                          _kit.serving_frame(ctx, "runs_14d.parquet")) if not f.empty]  # fmt: skip
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).drop_duplicates("run_id", keep="first")
    return df.sort_values("started_at", ascending=False).reset_index(drop=True)


runs = _runs()
if runs.empty:
    st.markdown("No runs in the last 14 days.")
else:
    delay = (runs["started_at"] - runs["scheduled_for"]).dt.total_seconds()
    delay = delay.where(delay.notna(), runs["schedule_delay_s"])
    took = (runs["finished_at"] - runs["started_at"]).dt.total_seconds()
    last = runs.iloc[0]
    ok_share = float((runs["status"] == "ok").mean())
    bad = int((runs["status"] != "ok").sum())
    tone = {"ok": "ok", "degraded": "warn"}.get(str(last["status"]), "fail")
    st.markdown(
        _kit.kpis(
            [
                (
                    "Latest run",
                    str(last["status"]),
                    tone,
                    f"{last['job']} · {_kit.et_text(last['started_at'])}",
                ),
                ("Runs, 14 days", str(len(runs)), "muted", f"{ok_share:.0%} finished ok"),
                ("Not ok", str(bad), "warn" if bad else "ok", "degraded or failed"),
                ("Typical run time", _kit.duration(took.median()), "muted", "median, 14 days"),
            ]
        ),
        unsafe_allow_html=True,
    )
    _kit.section("Runs, newest first")
    table = pd.DataFrame({
        "Started (ET)": [_kit.et_text(t) for t in runs["started_at"]],
        "Job": runs["job"],
        "Status": [STATUS_MARK.get(str(x), str(x)) for x in runs["status"]],
        "For date": [_kit.day_text(d) for d in runs["target_date"]],
        "Start delay": [("n/a" if pd.isna(d) else f"{d:,.0f} s") for d in delay],
        "Run time": [_kit.duration(t) for t in took],
        "Fetches ok / failed": [
            f"{int(n) - int(f)} / {int(f)}" if pd.notna(n) and pd.notna(f) else "n/a"
            for n, f in zip(runs["n_fetch"], runs["n_fetch_failed"], strict=True)
        ],
        "Observations": runs["n_obs"],
        "DQ fail / warn": [
            f"{int(a)} / {int(b)}" if pd.notna(a) and pd.notna(b) else "n/a"
            for a, b in zip(runs["n_dq_fail"], runs["n_dq_warn"], strict=True)
        ],
        "Trigger": runs["trigger"],
    })  # fmt: skip
    st.dataframe(table, hide_index=True, width="stretch")
    st.markdown(f"GitHub: [workflow runs]({ACTIONS})")

_kit.section("Incidents")
path = ROOT / "INCIDENTS.md"
incidents = _incidents.parse(path.read_text(encoding="utf-8")) if path.exists() else []
if not incidents:
    st.markdown("No incidents recorded. Only real incidents from scheduled runs are listed.")
for inc in incidents:
    with st.expander(f"{inc.date} · {inc.title}"):
        st.markdown(inc.body)
