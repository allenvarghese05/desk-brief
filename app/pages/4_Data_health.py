"""Data health (spec 10.3 page 4, P-022 template since T59): is anything wrong with the data?

Five blocks: the headline counts, what needs attention in today's latest run (each check type
one tab away), freshness over the last 7 days, corrections and quarantine (one tab each), and
the page footer with the display mode and the glyph legend.

Hosted mode withholds rows naming a source with a tier-1 feed (a validity or revision row can
carry the value itself). A corrected value is a market figure, so the old and new values go
through ``value_cell`` (compact) with source, tier, as-of and field. The DQ query runs on the
shared dashboard connection and is cached on ``data.data_version()`` (T56, T59).
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from app.components import data  # noqa: E402
from app.components import kit as _kit  # noqa: E402
from app.components.values import Provenance, value_cell  # noqa: E402

from deskbrief.config import Config  # noqa: E402

ctx = _kit.context("Data health")
WINDOW = 7
RANK = {"fail": 0, "warn": 1, "pass": 2, "skip": 3}
exposed = _kit.exposed_sources(ctx.config, ctx.hidden)


@st.cache_data(ttl=data.DATA_CACHE_TTL_SECONDS, show_spinner=False)
def _dq(_config: Config, where: str, since: date, ver: str) -> pd.DataFrame:
    """DQ results since ``since`` on the shared connection; ``where`` and ``ver`` (the data
    version) only key the cache."""
    with data.shared_connection(_config) as conn:
        return conn.execute(
            "SELECT run_id, check_type, check_name, source, instrument_id, target_date, "
            "evaluated_at, status, observed, threshold FROM dq WHERE target_date > ? "
            "ORDER BY target_date, check_type, check_name",
            [since],
        ).fetchdf()


def _withhold(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if df.empty:
        return df, 0
    kept = _kit.keep_rows(df, ("source",), exposed)
    return kept, len(df) - len(kept)


dq, withheld = _withhold(
    _dq(ctx.config, _kit.where_key(ctx.config), ctx.today - timedelta(days=WINDOW),
        _kit.version(ctx.config))
)  # fmt: skip
rev, rev_w = _withhold(_kit.recent_table(ctx, "revisions", 14))
quar, q_w = _withhold(_kit.recent_table(ctx, "quarantine", 14))

today = dq[dq["target_date"] == pd.Timestamp(ctx.today)] if not dq.empty else dq
latest_run = ""
if not today.empty:
    latest_run = str(today.sort_values("evaluated_at")["run_id"].iloc[-1])
    today = today[today["run_id"] == latest_run]
counts = today["status"].value_counts() if not today.empty else pd.Series(dtype=int)
n_fail, n_warn = int(counts.get("fail", 0)), int(counts.get("warn", 0))
n_pass = int(counts.get("pass", 0))

# --- block 1: the answer first ---
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


def _attention(bad: pd.DataFrame) -> str:
    """One line per check, source and status: which instruments, and the observed range."""
    bad = bad.assign(_r=bad["status"].map(RANK)).sort_values(["_r", "check_type", "check_name"])
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
        lines.append(
            f"<tr><td>{_kit.status_marker(str(status))}</td>"
            f"<td><b>{_kit.escape(str(what))}</b><span class='db-note'> · "
            f"{_kit.escape(str(source))}</span><br><span class='db-n'>"
            f"{len(g)} {'result' if len(g) == 1 else 'results'}"
            f"{': ' + _kit.escape(shown) if shown else ''}</span></td>"
            f"<td class='db-n'>{_kit.escape(seen + limit)}</td></tr>"
        )
    return _kit.scroll(
        '<table class="db-table"><thead><tr><th>Status</th><th>Check, source and what it '
        "hit</th><th>Observed vs limit</th></tr></thead>"
        f"<tbody>{''.join(lines)}</tbody></table>"
    )


# --- block 2: what needs attention today, each check type one tab away ---
with _kit.card("dh-attention", "Needs attention", f"Latest run {latest_run}" if latest_run else ""):
    if today.empty:
        st.markdown("No DQ results for today yet.")
    else:
        bad = today[today["status"].isin(["fail", "warn"])].copy()
        types = sorted(str(t) for t in today["check_type"].unique())
        tabs = st.tabs(["Problems", *(f"All {t}" for t in types)])
        with tabs[0]:
            if bad.empty:
                st.markdown(
                    '<div class="db-n">Nothing failing or warning in the latest run.</div>',
                    unsafe_allow_html=True,
                )
            else:
                by_type = bad.groupby("check_type")["status"].value_counts().unstack(fill_value=0)
                summary = " · ".join(
                    f"{t}: {', '.join(f'{int(n)} {s}' for s, n in row.items() if n)}"
                    for t, row in by_type.iterrows()
                )
                st.markdown(
                    f'<div class="db-n">{_kit.escape(summary)}</div>{_attention(bad)}',
                    unsafe_allow_html=True,
                )
        for tab, ctype in zip(tabs[1:], types, strict=True):
            group = today[today["check_type"] == ctype]
            c = group["status"].value_counts()
            with tab:
                st.markdown(
                    f'<div class="db-n">{_kit.escape(ctype)} results · '
                    f"{_kit.escape(', '.join(f'{n} {s}' for s, n in c.items()))}</div>",
                    unsafe_allow_html=True,
                )
                st.dataframe(
                    group[["status", "check_name", "source", "instrument_id", "observed",
                           "threshold"]],
                    hide_index=True,
                    width="stretch",
                )  # fmt: skip
    _kit.withheld_note(withheld, ctx.mode)

# --- block 3: freshness timeline ---
with _kit.card(
    "dh-fresh", "Freshness, last 7 days", "One square per day: did each dataset arrive on time?"
):
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
            _kit.status_legend()
            + _kit.scroll(
                '<table class="db-table"><thead><tr><th>Dataset check</th><th>Arrival by day'
                "</th><th>Expected vs actual</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table>"
            ),
            unsafe_allow_html=True,
        )


def _revised(r: pd.Series, which: str) -> str:
    """A revised value with its provenance; plain text when config lists no such feed."""
    v = r[f"{which}_value"]
    text = "n/a" if pd.isna(v) else f"{float(v):,.6g}"
    tier = _kit.feed_tier(ctx.config, str(r["source"]), str(r["instrument_id"]), str(r["dataset"]))
    if pd.isna(v) or tier is None:
        return f'<span class="db-unavailable">{_kit.escape(text)}</span>'
    p = Provenance(
        str(r["source"]),
        tier,
        _kit.et_text(r["as_of"]),
        f"{r['field']} as {'first published' if which == 'old' else 'revised'}",
    )
    return value_cell(text, p)


def _corrections(rev: pd.DataFrame) -> str:
    rows = "".join(
        f"<tr><td>{_kit.escape(_kit.et_text(r['detected_at']))}</td>"
        f"<td><b>{_kit.escape(str(r['instrument_id']))}</b><span class='db-note'> · "
        f"{_kit.escape(str(r['field']))} · {_kit.escape(str(r['source']))}</span></td>"
        f"<td class='num'>{_revised(r, 'old')}</td><td class='num'>{_revised(r, 'new')}</td></tr>"
        for _, r in rev.sort_values("detected_at", ascending=False).iterrows()
    )
    return _kit.scroll(
        '<table class="db-table"><thead><tr><th>Detected</th><th>Instrument, field and source'
        '</th><th class="num">Was</th><th class="num">Now</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


# --- block 4: corrections and quarantine ---
with _kit.card("dh-fixes", "Corrections and quarantine, last 14 days"):
    t_rev, t_quar = st.tabs([f"Corrections ({len(rev)})", f"Quarantine ({len(quar)})"])
    with t_rev:
        st.markdown(
            '<div class="db-n">A source changed a value it had already published.</div>'
            + (_corrections(rev) if not rev.empty else "<p>No revisions detected.</p>"),
            unsafe_allow_html=True,
        )
        _kit.withheld_note(rev_w, ctx.mode)
    with t_quar:
        st.markdown(
            '<div class="db-n">Fetches held back because they failed a sanity rule.</div>',
            unsafe_allow_html=True,
        )
        if quar.empty:
            st.markdown("Nothing quarantined.")
        else:
            st.dataframe(
                quar[["detected_at", "source", "dataset", "reason", "detail",
                      "raw_key"]].sort_values("detected_at", ascending=False),
                hide_index=True,
                width="stretch",
            )  # fmt: skip
        _kit.withheld_note(q_w, ctx.mode)

_kit.page_footer(ctx)
