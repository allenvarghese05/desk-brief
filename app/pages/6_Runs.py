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
from app.pages import _incidents, _kit  # noqa: E402

ctx = _kit.context("Runs")
ACTIONS = "https://github.com/allenvarghese05/desk-brief/actions"
runs = _kit.serving_frame(ctx, "runs_14d.parquet")

if runs.empty:
    st.markdown("No runs in the last 14 days.")
else:
    runs = runs.sort_values("started_at", ascending=False)
    delay = (runs["started_at"] - runs["scheduled_for"]).dt.total_seconds()
    delay = delay.where(delay.notna(), runs["schedule_delay_s"])
    table = pd.DataFrame({
        "Started (ET)": [_kit.et_text(t) for t in runs["started_at"]],
        "Job": runs["job"],
        "Trigger": runs["trigger"],
        "For date": runs["target_date"],
        "Start delay": [("n/a" if pd.isna(d) else f"{d:,.0f} s") for d in delay],
        "Status": runs["status"],
        "Fetches": runs["n_fetch"],
        "Failed fetches": runs["n_fetch_failed"],
        "Observations": runs["n_obs"],
        "DQ fail": runs["n_dq_fail"],
        "DQ warn": runs["n_dq_warn"],
    })  # fmt: skip
    st.dataframe(table, hide_index=True, width="stretch")
    st.markdown(f"GitHub: [workflow runs]({ACTIONS})")

st.subheader("Incidents")
path = ROOT / "INCIDENTS.md"
incidents = _incidents.parse(path.read_text(encoding="utf-8")) if path.exists() else []
if not incidents:
    st.markdown("No incidents recorded. Only real incidents from scheduled runs are listed.")
for inc in incidents:
    with st.expander(f"{inc.date} · {inc.title}"):
        st.markdown(inc.body)
