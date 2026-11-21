"""TradingView Lightweight Charts as a Streamlit custom component (T57B, P-022).

The static folder ``tvchart_frontend/`` is served by ``components.declare_component(path=…)``:
a hand-written ``index.html``, the glue script ``tvchart.js`` (Streamlit component message
protocol: ready, render, frame height), and the vendored standalone production bundle of
Lightweight Charts at a pinned version. There is no npm build, no CDN and no
``pyproject.toml`` change. The browser fetches the bundle once from the component's static
route and caches it. It is never inlined into a rerun.

Vendored library: see ``tvchart_frontend/NOTICE`` for the version, source URL, npm integrity
value and SHA-256. Dependabot does not track it. To upgrade:

1. pick the version on registry.npmjs.org and note its ``dist.integrity`` (sha512);
2. download ``lightweight-charts-<v>.tgz`` into a new empty directory under ``$TMPDIR``,
   check it against that integrity value, then extract it;
3. copy ``package/dist/lightweight-charts.standalone.production.js`` and the package's
   ``LICENSE`` over the vendored copies (5.3.0 ships no NOTICE file; ours records the
   bundle's own copyright header);
4. update ``NOTICE`` and ``SHA256SUMS`` (``shasum -a 256``), and :data:`LIBRARY_VERSION`;
5. run ``tests/unit/app/test_tvchart.py`` and check the chart in a browser.

Data contract (:func:`points`): one point per business day, as a ``"YYYY-MM-DD"`` string,
sorted, without duplicates, NaN or infinities. Lightweight Charts places bars by index, so
weekends and holidays leave no gaps. Every point carries a provenance index into its series'
``meta`` table (field, tier, source). The chart's tooltip shows those facts with the as-of
date. Every chart is followed by :func:`attribution`: a visible "Charts by TradingView"
link and the library's attribution notice (the library's own logo is off: it carries a
``title`` attribute).

If the component cannot be used (the bundle is missing or fails its checksum, or the browser
reports that it could not draw), :func:`chart` draws an Altair line of the same data with a
visible note instead of leaving a blank space.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Sequence
from functools import cache
from pathlib import Path
from typing import Any, Literal, TypedDict

import altair as alt
import pandas as pd
import streamlit as st
from app import theme

FRONTEND = Path(__file__).with_name("tvchart_frontend")
LIBRARY = "lightweight-charts.standalone.production.js"
LIBRARY_VERSION = "5.3.0"
ATTRIBUTION_URL = "https://www.tradingview.com/"
ATTRIBUTION_TEXT = "Charts by TradingView"
#: the attribution notice of the library's NOTICE file (v5.3.0), which its README asks every
#: page using the library to show, with a link to tradingview.com
ATTRIBUTION_NOTICE = "TradingView Lightweight Charts™ © 2025 TradingView, Inc."
NOTE_FALLBACK = "Interactive chart unavailable; showing a simple line of the same data."

Kind = Literal["candles", "line", "area", "volume"]
_VALUE_COLS: dict[str, tuple[str, ...]] = {
    "candles": ("open", "high", "low", "close"),
    "line": ("value",),
    "area": ("value",),
    "volume": ("value",),
}


class Series(TypedDict, total=False):
    """One series of the payload. ``points`` come from :func:`points`; ``meta`` holds
    ``[field, tier, source]`` rows that each point's ``m`` indexes."""

    kind: Kind
    name: str
    color: str
    dashed: bool
    points: list[dict[str, Any]]
    meta: list[list[str]]


# ---- the vendored bundle -----------------------------------------------------------------
def expected_sha256() -> str | None:
    """The bundle's checksum as recorded in ``SHA256SUMS`` (``<hex>  <name>`` lines)."""
    sums = FRONTEND / "SHA256SUMS"
    if not sums.is_file():
        return None
    for row in sums.read_text(encoding="utf-8").splitlines():
        parts = row.split()
        if len(parts) == 2 and parts[1].lstrip("*") == LIBRARY:
            return parts[0].lower()
    return None


def actual_sha256() -> str | None:
    path = FRONTEND / LIBRARY
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


@cache
def library_ok() -> bool:
    """The vendored bundle exists and matches ``SHA256SUMS`` (checked once per process)."""
    want = expected_sha256()
    return want is not None and actual_sha256() == want


@cache
def _component() -> Callable[..., Any] | None:
    if not library_ok():
        return None
    try:
        import streamlit.components.v1 as components

        return components.declare_component("deskbrief_tvchart", path=str(FRONTEND))
    except Exception:  # pragma: no cover - a broken Streamlit install
        return None


# ---- payload -----------------------------------------------------------------------------
def business_day(value: object) -> str:
    """A date as a Lightweight Charts business-day string, ``"YYYY-MM-DD"``."""
    return f"{pd.Timestamp(value):%Y-%m-%d}"  # type: ignore[arg-type]


def _finite(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def points(
    frame: pd.DataFrame,
    kind: Kind,
    *,
    meta: Sequence[str] = ("field", "tier_text", "source"),
    extra: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[list[str]]]:
    """Points and their provenance table for one series.

    ``frame`` has a date column ``d``, the value columns of ``kind`` (``open``, ``high``,
    ``low`` and ``close`` for candles, else ``value``) and the ``meta`` columns. Rows whose
    values are missing or not finite are dropped, then rows are sorted by date and a repeated
    date keeps its last row. ``extra`` columns are copied as they are (booleans or strings)."""
    cols = _VALUE_COLS[kind]
    if frame.empty:
        return [], []
    df = frame.copy()
    df["time"] = [business_day(d) for d in df["d"]]
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    keep = pd.Series(True, index=df.index)
    for c in cols:
        keep &= df[c].map(_finite)
    df = df[keep].sort_values("time", kind="stable").drop_duplicates("time", keep="last")
    table: list[list[str]] = []
    index: dict[tuple[str, ...], int] = {}
    out: list[dict[str, Any]] = []
    extras = tuple(extra)
    for rec in df.to_dict("records"):
        prov = tuple(str(rec[m]) for m in meta)
        if prov not in index:
            index[prov] = len(table)
            table.append(list(prov))
        p: dict[str, Any] = {"time": rec["time"], "m": index[prov]}
        for c in cols:
            p[c] = float(rec[c])
        for c in extras:
            v = rec[c]
            p[c] = v.item() if hasattr(v, "item") else v
        out.append(p)
    return out, table


def theme_tokens() -> dict[str, str]:
    """The P-022 tokens the frontend draws with (it hard-codes no colour)."""
    return {
        "page": theme.PAGE,
        "panel": theme.PANEL,
        "raised": theme.RAISED,
        "divider": theme.DIVIDER,
        "text": theme.TEXT,
        "secondary": theme.SECONDARY,
        "meta": theme.META,
        "up": theme.UP,
        "down": theme.DOWN,
        "up_tint": theme.UP_TINT,
        "down_tint": theme.DOWN_TINT,
        "accent": theme.ACCENT,
        "accent_tint": theme.ACCENT_TINT,
        "amber": theme.AMBER,
        "tooltip": theme.TOOLTIP,
        "sans": theme.SANS,
    }


def signature(payload: dict[str, Any]) -> str:
    """A short fingerprint of a payload (without its ``sig``). The frontend echoes it in an
    error report, so a failure disables only the payload that failed: a new range, style or
    data set tries the chart again."""
    body = {k: v for k, v in payload.items() if k != "sig"}
    text = json.dumps(body, sort_keys=True, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def spec(
    series: Sequence[Series],
    *,
    height: int = 380,
    precision: int = 2,
    last: float | None = None,
    label: str = "Price chart",
) -> dict[str, Any]:
    """The JSON the frontend receives: theme tokens, series, options and a ``sig``."""
    out: dict[str, Any] = {
        "height": int(height),
        "precision": int(precision),
        "last": float(last) if last is not None and _finite(float(last)) else None,
        "label": label,
        "theme": theme_tokens(),
        "series": [dict(s) for s in series],
    }
    out["sig"] = signature(out)
    return out


# ---- rendering ---------------------------------------------------------------------------
def attribution_html() -> str:
    """The attribution line: a visible, keyboard-focusable "Charts by TradingView" link to
    tradingview.com and the attribution notice of the library's NOTICE file, in the meta
    colour. The library's own logo is switched off (``attributionLogo: false`` in
    ``tvchart.js``) because it carries a ``title`` attribute (P-022: no ``title``)."""
    # the link takes the line's colour through ``theme.apply()``'s ``.stMarkdown a { color:
    # inherit }``: Streamlit's sanitiser strips an inline ``color: … !important`` on the link
    return (
        f'<div class="db-tv-attr" style="font-size:{theme.SIZE_META}px;color:{theme.META};'
        f'margin-top:-6px">Charts by <a href="{ATTRIBUTION_URL}" target="_blank" '
        f'rel="noopener noreferrer" style="text-decoration:underline" '
        f'aria-label="{ATTRIBUTION_TEXT} (opens tradingview.com)">'
        f"TradingView</a> · {ATTRIBUTION_NOTICE}</div>"
    )


def attribution() -> None:
    """The attribution that every TradingView chart carries (P-022, library README)."""
    st.markdown(attribution_html(), unsafe_allow_html=True)


def fallback_chart(series: Sequence[Series], *, height: int = 380) -> alt.Chart:
    """Plain Altair lines of every price series (close for candles; volume left out), with
    the same provenance in the tooltip. A comparison keeps both lines and their colours."""
    rows: list[dict[str, Any]] = []
    names: list[str] = []
    colours: list[str] = []
    for i, s in enumerate(x for x in series if x.get("kind") != "volume"):
        name = s.get("name") or ("Close" if s.get("kind") == "candles" else f"Series {i + 1}")
        names.append(name)
        colours.append(s.get("color") or (theme.ACCENT if i == 0 else theme.AMBER))
        col = "close" if s.get("kind") == "candles" else "value"
        meta = s.get("meta", [])
        for p in s.get("points", []):
            m = meta[p["m"]] if p["m"] < len(meta) else ["", "", ""]
            rows.append(
                {"d": p["time"], "y": p[col], "series": name, "field": m[0], "tier": m[1],
                 "source": m[2]}
            )  # fmt: skip
    df = pd.DataFrame(rows, columns=["d", "y", "series", "field", "tier", "source"])
    out: alt.Chart = (
        alt.Chart(df)
        .mark_line(strokeWidth=1.6)
        .encode(
            x=alt.X("d:T", title=None, axis=alt.Axis(format="%d %b %Y", labelAngle=0)),
            y=alt.Y("y:Q", title=None, scale=alt.Scale(zero=False)),
            color=alt.Color(
                "series:N",
                scale=alt.Scale(domain=names, range=colours),
                legend=alt.Legend(orient="top-left", title=None, labelColor=theme.META)
                if len(names) > 1
                else None,
            ),
            tooltip=[
                alt.Tooltip("d:T", title="As of", format="%a %d %b %Y"),
                alt.Tooltip("series:N", title="Series"),
                alt.Tooltip("y:Q", title="Value", format=",.4~f"),
                alt.Tooltip("field:N", title="Field"),
                alt.Tooltip("tier:N", title="Tier"),
                alt.Tooltip("source:N", title="Source"),
            ],
        )
        .properties(height=height)
        .configure(background="transparent", font=theme.SANS)
        .configure_view(stroke=None)
        .configure_axis(labelColor=theme.META, gridColor=theme.DIVIDER, domainColor=theme.DIVIDER)
    )
    return out


def _fail_key(key: str) -> str:
    return f"{key}__tvchart_failed"


def _seen_key(key: str) -> str:
    return f"{key}__tvchart_seen"


def chart(
    series: Sequence[Series],
    *,
    key: str,
    height: int = 380,
    precision: int = 2,
    last: float | None = None,
    label: str = "Price chart",
) -> bool:
    """Draw one TradingView chart, then its attribution. Returns True when the TradingView
    chart was drawn, False when the Altair fallback was (it has no ``last`` price line).

    The first series is the main one (``last`` draws a dashed price line on it). Width fits
    the container. Falls back to Altair lines with a visible note when the component cannot
    be used, or when the browser reported that it could not draw this same payload (a new
    payload, e.g. another range or style, tries the chart again). Call it inside
    ``st.fragment`` so range and style changes rerun only the chart."""
    payload = spec(series, height=height, precision=precision, last=last, label=label)
    state = st.session_state.get(key)
    # the component keeps returning its last value on every rerun, so an error report arms the
    # fallback only the first time it is seen; otherwise a stale report would re-arm it when
    # the user comes back to that payload, and the chart would never be retried (T58)
    if isinstance(state, dict) and state.get("status") == "error" and state.get("sig"):
        report = (state["sig"], str(state.get("message", "")))
        if st.session_state.get(_seen_key(key)) != report:
            st.session_state[_seen_key(key)] = report
            st.session_state[_fail_key(key)] = state["sig"]
    component = _component()
    if component is None or st.session_state.get(_fail_key(key)) == payload["sig"]:
        st.caption(NOTE_FALLBACK)
        st.altair_chart(fallback_chart(series, height=height), theme=None)
        return False
    st.session_state.pop(_fail_key(key), None)  # drawn: a later failure is a new report
    component(spec=payload, key=key, default=None)
    attribution()
    return True
