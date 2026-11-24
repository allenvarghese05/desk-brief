"""Data health (spec 10.3 page 4, P-022 template since T59): is anything wrong with the data?

Five blocks: the headline counts, what needs attention in the latest run (each check type one
tab away), freshness over the last 7 days, corrections and quarantine (one tab each), and
the page footer with the display mode and the glyph legend.

Hosted mode withholds rows naming a source with a tier-1 feed (a validity or revision row can
carry the value itself). A corrected value is a market figure, so the old and new values go
through ``value_cell`` (compact) with source, tier, as-of and field. The DQ query runs on the
shared dashboard connection and is cached on ``data.data_version()`` (T56, T59).

The run shown is T60's latest run with checks (``query.latest_check_run``: the greatest target
date on or before today with check rows evaluated by now), the same run as Markets' status line.
On a weekend, a holiday or a morning before the run it is the last run, named by its day ("Fri
07:00 run"), never "no run today yet" (T60B, P-023). Flat sections, no panels (P-023).
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
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
from deskbrief.query import latest_check_run  # noqa: E402
from deskbrief.timeutil import to_et  # noqa: E402

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


@st.cache_data(ttl=data.DATA_CACHE_TTL_SECONDS, show_spinner=False)
def _last_run(
    _config: Config, where: str, today: date, ver: str, _now: datetime
) -> tuple[date, str, datetime | None] | None:
    """T60's latest run with checks at ``_now`` (target date, run id, start time), or None when
    no run has recorded a check. ``where``, ``today`` and ``ver`` only key the cache; a new run
    changes the data version."""
    with data.shared_connection(_config) as conn:
        ref = latest_check_run(conn, _now)
    return None if ref is None else (ref.target_date, ref.run_id, ref.started_at)


def start_time(day: date, started: datetime | None) -> str:
    """The start time, " 07:00", when the run started on its own target date (as on Markets)."""
    return (
        f" {to_et(started):%H:%M}" if started is not None and to_et(started).date() == day else ""
    )


def run_name(day: date, started: datetime | None, today: date) -> str:
    """The run in words: "the Fri 07:00 run", or "today's 07:00 run" when the run is today's."""
    when = start_time(day, started)
    return f"today's{when} run" if day == today else f"the {day:%a}{when} run"


def _withhold(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if df.empty:
        return df, 0
    kept = _kit.keep_rows(df, ("source",), exposed)
    return kept, len(df) - len(kept)


last = _last_run(ctx.config, _kit.where_key(ctx.config), ctx.today, _kit.version(ctx.config),
                 ctx.now)  # fmt: skip
since = ctx.today - timedelta(days=WINDOW)
if last is not None:  # an older latest run (a long holiday) is still read
    since = min(since, last[0] - timedelta(days=1))
dq, withheld = _withhold(
    _dq(ctx.config, _kit.where_key(ctx.config), since, _kit.version(ctx.config))
)  # fmt: skip
rev, rev_w = _withhold(_kit.recent_table(ctx, "revisions", 14))
quar, q_w = _withhold(_kit.recent_table(ctx, "quarantine", 14))

# the latest run's DQ rows: T60's run when it wrote DQ rows, else that day's newest DQ run
latest_run, run_label, attention_note = "", "no checks yet", ""
today = dq.iloc[0:0]
if last is not None and not dq.empty:
    day, ref_run, started = last
    seen = pd.to_datetime(dq["evaluated_at"], utc=True) <= pd.Timestamp(ctx.now)
    on_day = dq[(dq["target_date"] == pd.Timestamp(day)) & seen]
    if not on_day.empty:
        runs_on_day = set(on_day["run_id"])
        latest_run = (
            ref_run
            if ref_run in runs_on_day
            else str(on_day.sort_values("evaluated_at")["run_id"].iloc[-1])
        )
        today = on_day[on_day["run_id"] == latest_run]
        started = started if latest_run == ref_run else None
        run_label = run_name(day, started, ctx.today)
        attention_note = f"Latest run {day:%a %d %b}{start_time(day, started)} · {latest_run}"
counts = today["status"].value_counts() if not today.empty else pd.Series(dtype=int)
n_fail, n_warn = int(counts.get("fail", 0)), int(counts.get("warn", 0))
n_pass = int(counts.get("pass", 0))
ran = not today.empty

# --- block 1: the answer first ---
st.markdown(
    _kit.kpis(
        [
            (
                "Failing",
                str(n_fail),
                ("fail" if n_fail else "ok") if ran else "muted",
                f"checks in {run_label}" if ran else run_label,
            ),
            (
                "Warnings",
                str(n_warn),
                ("warn" if n_warn else "ok") if ran else "muted",
                "need a look, not action",
            ),
            (
                "Passing",
                str(n_pass),
                "ok" if n_pass else "muted",
                f"of {len(today)} results" if ran else "no results yet",
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


# --- block 2: what needs attention in the latest run, each check type one tab away ---
with _kit.card("dh-attention", "Needs attention", attention_note):
    if today.empty:
        st.markdown("No DQ results recorded yet.")
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
    fresh = (
        dq[(dq["check_type"] == "freshness")
           & (dq["target_date"] > pd.Timestamp(ctx.today - timedelta(days=WINDOW)))]
        if not dq.empty
        else dq
    )  # fmt: skip
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


@st.cache_data(ttl=data.DATA_CACHE_TTL_SECONDS, show_spinner=False)
def _fallback_fetches(_config: Config, where: str, ids: tuple[str, ...], ver: str) -> set[str]:
    """Which of the revisions' fetches stored their observations from a fallback source (the
    revisions table does not carry ``is_fallback``; the observations behind it do)."""
    if not ids:
        return set()
    marks = ", ".join("?" for _ in ids)
    with data.shared_connection(_config) as conn:
        rows = conn.execute(
            f"SELECT DISTINCT fetch_id FROM obs_all WHERE is_fallback AND fetch_id IN ({marks})",  # noqa: S608 (placeholders only)
            list(ids),
        ).fetchall()
    return {str(r[0]) for r in rows}


PROXIES = frozenset(i.id for i in ctx.config.instruments if i.is_proxy)


def caveats(r: pd.Series, fallback: set[str]) -> tuple[str, ...]:
    """The words that stay visible beside a corrected value, by the same rule as the tear
    sheet's corrections (``tearsheet.revisions_html``): a proxy instrument, an IEX dataset, a
    fallback source (either version's fetch)."""
    fetches = {str(r.get("old_fetch_id")), str(r.get("new_fetch_id"))}
    return (
        (("proxy",) if str(r["instrument_id"]) in PROXIES else ())
        + (("IEX",) if str(r["dataset"]).startswith("iex") else ())
        + (("fallback",) if fetches & fallback else ())
    )


def _revised(r: pd.Series, which: str, fallback: set[str]) -> str:
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
        flags=caveats(r, fallback),
    )
    return value_cell(text, p)


def _corrections(rev: pd.DataFrame) -> str:
    ids = sorted({str(i) for c in ("old_fetch_id", "new_fetch_id") for i in rev[c].dropna()})
    fallback = _fallback_fetches(
        ctx.config, _kit.where_key(ctx.config), tuple(ids), _kit.version(ctx.config)
    )
    rows = "".join(
        f"<tr><td>{_kit.escape(_kit.et_text(r['detected_at']))}</td>"
        f"<td><b>{_kit.escape(str(r['instrument_id']))}</b><span class='db-note'> · "
        f"{_kit.escape(str(r['field']))} · {_kit.escape(str(r['source']))}</span></td>"
        f"<td class='num'>{_revised(r, 'old', fallback)}</td>"
        f"<td class='num'>{_revised(r, 'new', fallback)}</td></tr>"
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
