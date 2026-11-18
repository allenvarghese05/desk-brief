"""Sources scorecard (spec 10.3 page 5): per source uptime, latency, data age, error and
disagreement rate over the days that exist, with "n days of data"; a 30-day trend of one metric.

Measured numbers only: nothing is estimated or filled in. Rates are weighted by the day's fetch
count (disagreement by its cross-check count); latencies and data age are the median of the daily
values, said so in the column header. Operational metrics, not market data, so every source is
listed in both modes.
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

ctx = _kit.context("Sources")
sc = _kit.serving_frame(ctx, "scorecard_30d.parquet")
if sc.empty:
    st.markdown("No scorecard data yet. It is written at the end of each job.")
    st.stop()
# a source switched off in config.yaml (stooq, by owner decision) is history, not a live feed
disabled = sorted(k for k, v in ctx.config.sources.items() if not v.enabled)
sc = sc[~sc["source"].isin(disabled)]
if sc.empty:
    st.markdown("No scorecard data for an enabled source yet.")
    st.stop()


def pct(x: float | None) -> str:
    return "n/a" if x is None or pd.isna(x) else f"{x * 100:.1f}%"


def num(x: float | None, unit: str) -> str:
    return "n/a" if x is None or pd.isna(x) else f"{x:,.0f} {unit}"


def weighted(g: pd.DataFrame, rate: str, weight: str) -> float | None:
    g = g[g[rate].notna() & (g[weight] > 0)]
    return None if g.empty else float((g[rate] * g[weight]).sum() / g[weight].sum())


rows = []
cards = []
for source, g in sc.groupby("source"):
    uptime = weighted(g, "ok_rate", "fetches")
    errors = weighted(g, "error_rate", "fetches")
    age = g["median_data_age_s"].median()
    tone = (
        "muted"
        if uptime is None
        else "ok"
        if uptime >= 0.98
        else "warn"
        if uptime >= 0.9
        else "fail"
    )
    cards.append((
        str(source), pct(uptime), tone,
        f"uptime · data age {_kit.duration(age)} · {int(g['date'].nunique())} days",
    ))  # fmt: skip
    rows.append({
        "Source": source,
        "Days of data": int(g["date"].nunique()),
        "Fetches": int(g["fetches"].sum()),
        "Uptime": pct(uptime),
        "Error rate": pct(errors),
        "Median latency": num(g["median_latency_ms"].median(), "ms"),
        "p95 latency": num(g["p95_latency_ms"].median(), "ms"),
        "Median data age": _kit.duration(age),
        "Disagreement rate": pct(weighted(g, "disagreement_rate", "n_crosschecks")),
    })  # fmt: skip
table = pd.DataFrame(rows).sort_values("Source")
n_days = int(sc["date"].nunique())
st.markdown(_kit.kpis(cards), unsafe_allow_html=True)
_kit.section(
    "Scorecard",
    f"{n_days} days of data in the window; each source row says how many it has. Rates are "
    "weighted by fetches (disagreement by cross-checks); latency and data age are the median "
    "of the daily medians. A source with few days is a short sample, not a trend.",
)
st.dataframe(table, hide_index=True, width="stretch")
if disabled:
    st.caption(f"Switched off in config.yaml, so not listed: {', '.join(disabled)}.")

METRICS = {
    "Uptime": ("ok_rate", 100.0, "%"),
    "Error rate": ("error_rate", 100.0, "%"),
    "Median latency": ("median_latency_ms", 1.0, "ms"),
    "p95 latency": ("p95_latency_ms", 1.0, "ms"),
    "Median data age": ("median_data_age_s", 1.0, "s"),
    "Disagreement rate": ("disagreement_rate", 100.0, "%"),
}
_kit.section("Trend")
left, right = st.columns(2)
source = left.selectbox("Source", sorted(sc["source"].unique()), key="source")
metric = right.selectbox("Metric", list(METRICS), key="metric")
col, scale, unit = METRICS[metric]
g = sc[(sc["source"] == source) & sc[col].notna()].sort_values("date")
line = _kit.Line(
    metric,
    tuple((f"{d:%d %b}", float(v) * scale) for d, v in zip(g["date"], g[col], strict=True)),
    _kit.TIER_COLOUR[3],
)
st.markdown(
    f'<div class="db-n">{_kit.escape(source)}: {_kit.escape(metric.lower())} per day, '
    f"{len(g)} days of data.</div>",
    unsafe_allow_html=True,
)
_kit.show_chart(
    _kit.line_chart([line], unit),
    g.assign(**{metric: g[col] * scale})[["date", "fetches", metric]],
    "trend",
)
