"""Runs (spec 10.3 page 6, P-022 template since T59): did the jobs run, and what went wrong?

Four blocks: the headline counts, the runs of the last 14 days (a job filter is the card's
control), the incidents list from ``INCIDENTS.md``, and the page footer with the display mode
and the glyph legend. Operational facts only: no market figure is shown.
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


ALL_JOBS = "All jobs"
runs = _runs()
if runs.empty:
    with _kit.card("ru-runs", "Runs, newest first"):
        st.markdown("No runs in the last 14 days.")
else:
    delay = (runs["started_at"] - runs["scheduled_for"]).dt.total_seconds()
    runs = runs.assign(
        _delay=delay.where(delay.notna(), runs["schedule_delay_s"]),
        _took=(runs["finished_at"] - runs["started_at"]).dt.total_seconds(),
    )
    last = runs.iloc[0]
    ok_share = float((runs["status"] == "ok").mean())
    bad = int((runs["status"] != "ok").sum())
    tone = {"ok": "ok", "degraded": "warn"}.get(str(last["status"]), "fail")
    # --- block 1: the answer first ---
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
                ("Typical run time", _kit.duration(runs["_took"].median()), "muted",
                 "median, 14 days"),
            ]
        ),
        unsafe_allow_html=True,
    )  # fmt: skip
    # --- block 2: the runs, filtered by job ---
    with _kit.card("ru-runs", "Runs, newest first", controls=True) as controls:
        jobs = [ALL_JOBS, *sorted(str(j) for j in runs["job"].dropna().unique())]
        with controls:  # type: ignore[attr-defined]
            job = (
                st.segmented_control(
                    "Job", jobs, default=ALL_JOBS, key="job", label_visibility="collapsed"
                )
                or ALL_JOBS
            )
        view = runs if job == ALL_JOBS else runs[runs["job"] == job]
        table = pd.DataFrame({
            "Started (ET)": [_kit.et_text(t) for t in view["started_at"]],
            "Job": view["job"],
            "Status": [STATUS_MARK.get(str(x), str(x)) for x in view["status"]],
            "For date": [_kit.day_text(d) for d in view["target_date"]],
            "Start delay": [_kit.duration(d) for d in view["_delay"]],
            "Run time": [_kit.duration(t) for t in view["_took"]],
            "Fetches ok / failed": [
                f"{int(n) - int(f)} / {int(f)}" if pd.notna(n) and pd.notna(f) else "n/a"
                for n, f in zip(view["n_fetch"], view["n_fetch_failed"], strict=True)
            ],
            "Observations": view["n_obs"],
            "DQ fail / warn": [
                f"{int(a)} / {int(b)}" if pd.notna(a) and pd.notna(b) else "n/a"
                for a, b in zip(view["n_dq_fail"], view["n_dq_warn"], strict=True)
            ],
            "Trigger": view["trigger"],
        })  # fmt: skip
        st.dataframe(table, hide_index=True, width="stretch")
        st.markdown(f"GitHub: [workflow runs]({ACTIONS})")

# --- block 3: incidents ---
path = ROOT / "INCIDENTS.md"
incidents = _incidents.parse(path.read_text(encoding="utf-8")) if path.exists() else []
with _kit.card("ru-incidents", "Incidents", f"{len(incidents)} recorded, newest first"):
    if not incidents:
        st.markdown("No incidents recorded. Only real incidents from scheduled runs are listed.")
    for inc in incidents:
        with st.expander(f"{inc.date} · {inc.title}"):
            st.markdown(inc.body)

_kit.page_footer(ctx)
