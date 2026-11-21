"""The workspace price chart's data: the TradingView payload (T57B) and its helpers.

:func:`price_panel` draws range and style controls and a
:func:`app.components.tvchart.chart` in one ``st.fragment``. :func:`tv_series` and
:func:`tv_compare_series` turn bars into its payload. The T36 Altair ``price_chart`` and
``compare_chart`` were retired by T59 once no page drew them; the Altair fallback of a
TradingView chart is :func:`app.components.tvchart.fallback_chart`.

Data are completed daily bars from :func:`app.components.data.load_bars` (hidden tiers never
read). Candles need a real open, high and low on most days; otherwise the close is drawn as a
line, never a made-up candle. The latest (possibly intraday) value is a dashed price line, so
the chart and the figure above it always agree. Every point's tooltip names its tier and
source; the caption under the chart carries the same facts for the series as a whole.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st
from app import theme
from app.components import tvchart
from app.components.values import TIER_GLYPH, TIER_NAME

#: range buttons and how many calendar days each shows (None: everything the store has)
RANGES: dict[str, int | None] = {"1M": 31, "3M": 92, "6M": 183, "1Y": 366, "All": None}
DEFAULT_RANGE = "3M"
CANDLES, LINE = "Candles", "Line"
_PCT_UNITS = {"pct"}


def window(bars: pd.DataFrame, rng: str) -> pd.DataFrame:
    """The bars inside the chosen range, counted back from the last bar."""
    days = RANGES.get(rng)
    if bars.empty or days is None:
        return bars
    end = bars["d"].max()
    out: pd.DataFrame = bars[bars["d"] > end - timedelta(days=days)].reset_index(drop=True)
    return out


def candle_ok(bars: pd.DataFrame) -> pd.Series:
    """Days whose open, high and low exist and agree with the close (low <= body <= high)."""
    o, h, lo, c = bars["open"], bars["high"], bars["low"], bars["close"]
    body_lo = pd.concat([o, c], axis=1).min(axis=1)
    body_hi = pd.concat([o, c], axis=1).max(axis=1)
    return o.notna() & h.notna() & lo.notna() & (lo <= body_lo) & (h >= body_hi)


def has_ohlc(bars: pd.DataFrame) -> bool:
    """Candles only when at least 80% of days carry a consistent open, high and low."""
    if bars.empty:
        return False
    return bool(candle_ok(bars).mean() >= 0.8)


def has_volume(bars: pd.DataFrame) -> bool:
    return bool(not bars.empty and bars["volume"].notna().mean() >= 0.8 and bars["volume"].sum())


def tier_text(tier: object) -> str:
    t = int(tier)  # type: ignore[call-overload]
    return f"{TIER_GLYPH[t]} {TIER_NAME[t]}"


def _label(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.copy()
    df["up"] = (df["close"] >= df["open"].fillna(df["close"])).map({True: "up", False: "down"})
    df["ok"] = candle_ok(df)
    df["tier_text"] = df["tier"].map(tier_text)
    df["field_text"] = df["field"].astype(str)
    df["volume_text"] = "volume (" + df["field_text"] + " bar)"
    return df


def rebased(a: pd.DataFrame, b: pd.DataFrame, name_a: str, name_b: str) -> pd.DataFrame:
    """Both series on their common dates, each set to 100 on the first of them (long format)."""
    cols = ["d", "close", "tier", "source", "field"]
    m = a[cols].merge(b[cols], on="d", suffixes=("_a", "_b"))
    if len(m) < 2:
        return pd.DataFrame(columns=["d", "series", "index", "field_text", "tier_text", "source"])
    m = m.assign(
        a=m["close_a"] / m["close_a"].iloc[0] * 100, b=m["close_b"] / m["close_b"].iloc[0] * 100
    )
    first = f"{m['d'].iloc[0]:%d %b %Y}"

    def leg(name: str, key: str) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "d": m["d"],
                "series": name,
                "index": m[key],
                "field_text": m[f"field_{key}"].astype(str) + f" rebased to 100 on {first}",
                "tier_text": m[f"tier_{key}"].map(tier_text),
                "source": m[f"source_{key}"],
            }
        )

    return pd.concat([leg(name_a, "a"), leg(name_b, "b")], ignore_index=True)


def caption(bars: pd.DataFrame, *, latest_line: bool = True) -> str:
    """One line naming what the chart shows: field, tiers, sources and the date span."""
    if bars.empty:
        return ""
    field = str(bars["field"].iloc[-1])
    tiers = " ".join(
        f"{TIER_GLYPH[int(t)]} {TIER_NAME[int(t)]}" for t in sorted(bars["tier"].unique())
    )
    sources = ", ".join(sorted(bars["source"].unique()))
    a, b = bars["d"].min(), bars["d"].max()
    return (
        f"Daily {field}s · {tiers} · {sources} · {a.day} {a:%b %Y} to {b.day} {b:%b %Y} "
        f"({len(bars)} days)." + (" Dashed line: latest value." if latest_line else "")
    )


# ---- TradingView chart (T57B, P-022) ------------------------------------------------------
#: chart styles of the TradingView chart (candles only when the bars carry real OHLC)
AREA = "Area"
TV_STYLES = (CANDLES, LINE, AREA)


def tv_precision(bars: pd.DataFrame) -> int:
    """Decimals on the price axis: 2 for percentages and prices of 10 or more, else 4."""
    if bars.empty:
        return 2
    if str(bars["unit"].iloc[-1]) in _PCT_UNITS:
        return 2
    level = bars["close"].abs().median()
    return 2 if level == level and level >= 10 else 4


def tv_series(bars: pd.DataFrame, *, style: str) -> list[tvchart.Series]:
    """The TradingView payload for one instrument.

    Candles only when :func:`has_ohlc`. A day without a consistent open, high and low is drawn
    as a flat grey mark at the close (``ohlc`` false), never a made-up candle. Otherwise the
    close is a line or an area. Volume is added as a bottom histogram when :func:`has_volume`.
    Every point names its field, tier and source."""
    if bars.empty:
        return []
    df = _label(bars)
    out: list[tvchart.Series] = []
    if style == CANDLES and has_ohlc(bars):
        ok = df["ok"]
        c = df.assign(
            open=df["open"].where(ok, df["close"]),
            high=df["high"].where(ok, df["close"]),
            low=df["low"].where(ok, df["close"]),
            ohlc=ok,
        )
        pts, meta = tvchart.points(c, "candles", meta=("field_text", "tier_text", "source"),
                                   extra=("ohlc",))  # fmt: skip
        for p in pts:
            if not p["ohlc"]:
                p["color"] = theme.STALE
        out.append({"kind": "candles", "name": "", "points": pts, "meta": meta})
    else:
        line = df.assign(value=df["close"])
        pts, meta = tvchart.points(line, "line", meta=("field_text", "tier_text", "source"))
        kind: tvchart.Kind = "area" if style == AREA else "line"
        out.append({"kind": kind, "name": "", "points": pts, "meta": meta})
    if has_volume(bars):
        vol = df.assign(value=df["volume"], up=df["up"] == "up")
        pts, meta = tvchart.points(vol, "volume", meta=("volume_text", "tier_text", "source"),
                                   extra=("up",))  # fmt: skip
        out.append({"kind": "volume", "name": "Volume", "points": pts, "meta": meta})
    return out


def tv_compare_series(
    a: pd.DataFrame, b: pd.DataFrame, name_a: str, name_b: str
) -> list[tvchart.Series]:
    """Two series rebased to 100 on their first common date (second one dashed, amber)."""
    long = rebased(a, b, name_a, name_b)
    out: list[tvchart.Series] = []
    for name, colour, dashed in ((name_a, theme.ACCENT, False), (name_b, theme.AMBER, True)):
        leg = long[long["series"] == name].assign(value=lambda f: f["index"])
        pts, meta = tvchart.points(leg, "line", meta=("field_text", "tier_text", "source"))
        out.append({"kind": "line", "name": name, "color": colour, "dashed": dashed,
                    "points": pts, "meta": meta})  # fmt: skip
    return out


@st.fragment
def price_panel(
    bars: pd.DataFrame,
    *,
    name: str,
    last: float | None,
    key: str = "tv",
    compare: tuple[str, pd.DataFrame] | None = None,
    height: int = 380,
) -> None:
    """Range and style controls with the TradingView chart under them, as one fragment: a
    range or style change reruns only this panel."""
    c1, c2 = st.columns([3, 2])
    rng = (
        c1.segmented_control(
            "Range", list(RANGES), key=f"{key}-rng", default=DEFAULT_RANGE,
            label_visibility="collapsed",
        )
        or DEFAULT_RANGE
    )  # fmt: skip
    styles = list(TV_STYLES) if has_ohlc(bars) else [LINE, AREA]
    style = styles[0]
    if compare is None:  # a comparison is always two rebased lines
        style = (
            c2.segmented_control(
                "Chart style", styles, key=f"{key}-style", default=styles[0],
                label_visibility="collapsed",
            )
            or styles[0]
        )  # fmt: skip
    view = window(bars, rng)
    if view.empty:
        st.caption("No data in this range.")
        return
    if compare is not None:
        other_name, other = compare
        series = tv_compare_series(view, window(other, rng), name, other_name)
        if series and series[0]["points"]:
            tvchart.chart(series, key=f"{key}-chart", height=height, precision=2,
                          label=f"{name} and {other_name} rebased to 100")  # fmt: skip
            st.caption(f"{caption(view, latest_line=False)} {other_name}: "
                       f"{caption(window(other, rng), latest_line=False)}")  # fmt: skip
            return
        st.caption(f"No common dates with {other_name}; showing {name} alone.")
    drawn = tvchart.chart(
        tv_series(view, style=style), key=f"{key}-chart", height=height,
        precision=tv_precision(view), last=last, label=f"{name} price chart",
    )  # fmt: skip
    # the Altair fallback draws no latest-value line, so its caption does not mention one
    st.caption(caption(view, latest_line=drawn and last is not None))
