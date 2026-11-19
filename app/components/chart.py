"""The workspace price chart (T36): candles or a line, a volume pane, a crosshair tooltip.

Data are completed daily bars from :func:`app.components.data.load_bars` (hidden tiers never
read). Candles need a real open, high and low on most days; otherwise the close is drawn as a
line, never a made-up candle. The latest (possibly intraday) value is a dashed price line, so
the chart and the figure above it always agree. Every point's tooltip names its tier and
source; the caption under the chart carries the same facts for the series as a whole.
"""

from __future__ import annotations

import math
from datetime import timedelta

import altair as alt
import pandas as pd
from app import theme
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


def _fmt(unit: str) -> str:
    return ".2f" if unit in _PCT_UNITS else ",.4~f"


def _label(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.copy()
    df["up"] = (df["close"] >= df["open"].fillna(df["close"])).map({True: "up", False: "down"})
    df["ok"] = candle_ok(df)
    df["tier_text"] = df["tier"].map(lambda t: f"{TIER_GLYPH[int(t)]} {TIER_NAME[int(t)]}")
    return df


def price_chart(
    bars: pd.DataFrame, *, style: str, last: float | None, height: int = 380
) -> alt.TopLevelMixin:
    """The main chart; ``style`` is :data:`CANDLES` or :data:`LINE` (line when no OHLC)."""
    df = _label(bars)
    unit = str(df["unit"].iloc[-1])
    fmt = _fmt(unit)
    candles = style == CANDLES and has_ohlc(bars)
    lo_col, hi_col = ("low", "high") if candles else ("close", "close")
    extra = df["close"].tolist() + ([last] if last is not None else [])
    lows = [v for v in df[lo_col].tolist() + extra if v == v]
    highs = [v for v in df[hi_col].tolist() + extra if v == v]
    lo, hi = min(lows), max(highs)
    pad = (hi - lo) * 0.06 or abs(hi) * 0.01 or 1.0
    y_scale = alt.Scale(domain=[lo - pad, hi + pad], zero=False, nice=False)
    x = alt.X(
        "d:T",
        title=None,
        axis=alt.Axis(format="%d %b", labelAngle=0, grid=False, tickCount=6),
    )
    base = alt.Chart(df).encode(x=x)
    colour = alt.condition("datum.up == 'up'", alt.value(theme.UP), alt.value(theme.DOWN))
    y_axis = alt.Axis(orient="right", format=fmt, tickCount=6, title=None)
    if candles:
        width = max(1.5, min(9.0, 640 / max(len(df), 1) * 0.6))
        # a candle only when open, high and low exist and agree with the close
        full = "datum.ok"
        wick = (
            base.transform_filter(full)
            .mark_rule(strokeWidth=1)
            .encode(y=alt.Y("low:Q", scale=y_scale, axis=y_axis), y2="high:Q", color=colour)
        )
        body = (
            base.transform_filter(full)
            .mark_bar(size=width)
            .encode(y="open:Q", y2="close:Q", color=colour)
        )
        # any other day is a tick at the close, never an invented candle
        bare = (
            base.transform_filter(f"!({full})")
            .mark_tick(color=theme.MUTED, thickness=2, size=width + 4)
            .encode(y=alt.Y("close:Q", scale=y_scale))
        )
        marks: alt.TopLevelMixin = wick + body + bare
    else:
        area = base.mark_area(color=theme.ACCENT, opacity=0.08).encode(
            y=alt.Y("close:Q", scale=y_scale, axis=y_axis)
        )
        line = base.mark_line(color=theme.ACCENT, strokeWidth=1.8).encode(y="close:Q")
        marks = area + line
    hover = alt.selection_point(
        fields=["d"], nearest=True, on="pointerover", clear="pointerout", empty=False
    )
    tips = [
        alt.Tooltip("d:T", title="Date", format="%a %d %b %Y"),
        *(
            [
                alt.Tooltip("open:Q", title="Open", format=fmt),
                alt.Tooltip("high:Q", title="High", format=fmt),
                alt.Tooltip("low:Q", title="Low", format=fmt),
            ]
            if candles
            else []
        ),
        alt.Tooltip("close:Q", title=str(df["field"].iloc[-1]).capitalize(), format=fmt),
        alt.Tooltip("tier_text:N", title="Tier"),
        alt.Tooltip("source:N", title="Source"),
    ]
    catcher = base.mark_rule(strokeWidth=12, opacity=0).encode(tooltip=tips).add_params(hover)
    cross = base.mark_rule(color=theme.MUTED, strokeDash=[3, 3]).encode(
        opacity=alt.condition(hover, alt.value(0.9), alt.value(0.0))
    )
    layers = [marks, cross, catcher]
    if last is not None and not math.isnan(last):
        ref = pd.DataFrame({"y": [last]})
        layers.append(
            alt.Chart(ref)
            .mark_rule(color=theme.INK, strokeDash=[2, 3], opacity=0.55)
            .encode(y=alt.Y("y:Q", scale=y_scale))
        )
    price = alt.layer(*layers).properties(height=height)
    chart: alt.TopLevelMixin = price
    if has_volume(bars):
        vol = (
            base.mark_bar(opacity=0.45, size=max(1.5, min(9.0, 640 / max(len(df), 1) * 0.6)))
            .encode(
                y=alt.Y(
                    "volume:Q",
                    axis=alt.Axis(orient="right", format="~s", tickCount=2, title=None),
                ),
                color=colour,
                tooltip=[alt.Tooltip("d:T", format="%d %b"), alt.Tooltip("volume:Q", format=",")],
            )
            .properties(height=70)
        )
        chart = alt.vconcat(price, vol, spacing=4).resolve_scale(x="shared")
    styled: alt.TopLevelMixin = (
        chart.configure(background="transparent", font=theme.SANS)
        .configure_view(stroke=None)
        .configure_axis(
            labelColor=theme.MUTED,
            labelFontSize=11,
            gridColor=theme.GRID,
            domainColor=theme.RULE,
            tickColor=theme.RULE,
            labelPadding=6,
        )
    )
    return styled


def rebased(a: pd.DataFrame, b: pd.DataFrame, name_a: str, name_b: str) -> pd.DataFrame:
    """Both series on their common dates, each set to 100 on the first of them (long format)."""
    m = a[["d", "close"]].merge(b[["d", "close"]], on="d", suffixes=("_a", "_b"))
    if len(m) < 2:
        return pd.DataFrame(columns=["d", "series", "index"])
    m = m.assign(
        a=m["close_a"] / m["close_a"].iloc[0] * 100, b=m["close_b"] / m["close_b"].iloc[0] * 100
    )
    long = pd.concat(
        [
            pd.DataFrame({"d": m["d"], "series": name_a, "index": m["a"]}),
            pd.DataFrame({"d": m["d"], "series": name_b, "index": m["b"]}),
        ],
        ignore_index=True,
    )
    return long


def compare_chart(
    long: pd.DataFrame, name_a: str, name_b: str, height: int = 380
) -> alt.TopLevelMixin:
    """Two series rebased to 100: relative performance at a glance (second series dashed)."""
    hover = alt.selection_point(
        fields=["d"], nearest=True, on="pointerover", clear="pointerout", empty=False
    )
    base = alt.Chart(long).encode(
        x=alt.X(
            "d:T",
            title=None,
            axis=alt.Axis(format="%d %b", labelAngle=0, grid=False, tickCount=6),
        ),
    )
    lines = base.mark_line(strokeWidth=1.8).encode(
        y=alt.Y(
            "index:Q",
            scale=alt.Scale(zero=False),
            title=None,
            axis=alt.Axis(orient="right", format=".1f"),
        ),
        color=alt.Color(
            "series:N",
            scale=alt.Scale(domain=[name_a, name_b], range=[theme.ACCENT, theme.WARN]),
            legend=alt.Legend(orient="top-left", title=None, labelColor=theme.MUTED),
        ),
        strokeDash=alt.StrokeDash(
            "series:N",
            scale=alt.Scale(domain=[name_a, name_b], range=[[1, 0], [5, 3]]),
            legend=None,
        ),
    )
    catcher = (
        base.mark_rule(strokeWidth=12, opacity=0)
        .encode(
            tooltip=[
                alt.Tooltip("d:T", format="%a %d %b %Y"),
                "series:N",
                alt.Tooltip("index:Q", title="Rebased (start = 100)", format=".2f"),
            ]
        )
        .add_params(hover)
    )
    cross = base.mark_rule(color=theme.MUTED, strokeDash=[3, 3]).encode(
        opacity=alt.condition(hover, alt.value(0.9), alt.value(0.0))
    )
    ref = alt.Chart(pd.DataFrame({"y": [100.0]})).mark_rule(color=theme.RULE).encode(y="y:Q")
    styled: alt.TopLevelMixin = (
        alt.layer(ref, lines, cross, catcher)
        .properties(height=height)
        .configure(background="transparent", font=theme.SANS)
        .configure_view(stroke=None)
        .configure_axis(
            labelColor=theme.MUTED,
            labelFontSize=11,
            gridColor=theme.GRID,
            domainColor=theme.RULE,
            tickColor=theme.RULE,
            labelPadding=6,
        )
    )
    return styled


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
