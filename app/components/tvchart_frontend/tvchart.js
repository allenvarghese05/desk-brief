/*
 * Desk Brief price chart glue (T57B, P-022).
 *
 * Talks the Streamlit component message protocol directly (no npm, no build step):
 *   -> streamlit:componentReady   once, when this script has loaded
 *   <- streamlit:render           args.spec: theme tokens, series and their provenance
 *   -> streamlit:setFrameHeight   the iframe height
 *   -> streamlit:setComponentValue {status: "error", sig, n} only when the chart cannot be
 *                                 drawn, so Python can fall back for that payload
 *
 * Times arrive as business-day strings ("YYYY-MM-DD"). Lightweight Charts lays bars out by
 * index, so weekends and holidays leave no gaps. Every tooltip line names the field, tier,
 * source and as-of date of the point under the crosshair. The tooltip is built with
 * textContent only.
 */
(function () {
  "use strict";

  var LWC = window.LightweightCharts;
  var wrap = document.getElementById("wrap");
  var host = document.getElementById("chart");
  var tip = document.getElementById("tip");
  var note = document.getElementById("note");
  var DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  var chart = null;
  var lastSpec = "";
  var lastWidth = 0;
  var reportedSig = null;
  var reports = 0; // n: tells a new failure from the last one Streamlit keeps returning;
  // it carries the time too, because a new iframe starts counting again
  var currentSig = "";

  function send(type, data) {
    var msg = { isStreamlitMessage: true, type: type };
    if (data) {
      for (var k in data) { if (Object.prototype.hasOwnProperty.call(data, k)) { msg[k] = data[k]; } }
    }
    window.parent.postMessage(msg, "*");
  }

  function setHeight(h) { send("streamlit:setFrameHeight", { height: Math.ceil(h) }); }

  function fail(text) {
    if (chart) { chart.remove(); chart = null; }
    host.style.display = "none";
    note.style.display = "block";
    note.textContent = "Chart unavailable: " + text;
    setHeight(wrap.getBoundingClientRect().height || 60);
    // one report per payload: Python falls back for this payload only (sig), and a new range,
    // style or data set tries the chart again
    if (reportedSig !== currentSig) {
      reportedSig = currentSig;
      send("streamlit:setComponentValue", {
        value: {
          status: "error", message: String(text), sig: currentSig,
          n: Date.now() + "-" + (++reports)
        },
        dataType: "json"
      });
    }
  }

  // ---- dates ------------------------------------------------------------------------------
  function toDate(t) {
    if (typeof t === "string") {
      var p = t.split("-");
      return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
    }
    if (typeof t === "number") { return new Date(t * 1000); }
    if (t && typeof t === "object") { return new Date(Date.UTC(t.year, t.month - 1, t.day)); }
    return null;
  }
  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function key(t) {
    var d = toDate(t);
    return d ? d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) : "";
  }
  /** Crosshair and tooltip date: always with the year, e.g. "Thu 19 Nov 2026". */
  function fullDate(t) {
    var d = toDate(t);
    if (!d) { return ""; }
    return DAYS[d.getUTCDay()] + " " + d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + " " + d.getUTCFullYear();
  }
  /** Axis labels: years are spelled out, and month and day ticks carry the year when the data
   *  span more than one calendar year or lie in a past year, so no tick is an ambiguous "Mar". */
  function tickFormatter(withYear) {
    return function (t, type) {
      var d = toDate(t);
      if (!d) { return ""; }
      var yy = "'" + pad(d.getUTCFullYear() % 100);
      if (type === 0) { return "" + d.getUTCFullYear(); }               // TickMarkType.Year
      if (type === 1) { return MONTHS[d.getUTCMonth()] + (withYear ? " " + yy : ""); } // Month
      return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + (withYear ? " " + yy : "");
    };
  }

  // ---- numbers ----------------------------------------------------------------------------
  function fmt(v, precision) {
    if (v === null || v === undefined || isNaN(v)) { return "—"; }
    return Number(v).toLocaleString("en-US", { minimumFractionDigits: precision, maximumFractionDigits: precision });
  }
  function fmtVol(v) {
    if (v === null || v === undefined || isNaN(v)) { return "—"; }
    return Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 });
  }

  // ---- build ------------------------------------------------------------------------------
  function seriesFor(c, s, spec, th) {
    var common = { priceLineVisible: false, lastValueVisible: true };
    var pf = { type: "price", precision: spec.precision, minMove: Math.pow(10, -spec.precision) };
    if (s.kind === "candles") {
      return c.addSeries(LWC.CandlestickSeries, Object.assign({}, common, {
        upColor: th.up, downColor: th.down, wickUpColor: th.up, wickDownColor: th.down,
        borderVisible: false, priceFormat: pf
      }));
    }
    if (s.kind === "area") {
      return c.addSeries(LWC.AreaSeries, Object.assign({}, common, {
        lineColor: s.color || th.accent, topColor: th.accent_tint, bottomColor: "rgba(0,0,0,0)",
        lineWidth: 2, priceFormat: pf
      }));
    }
    if (s.kind === "volume") {
      return c.addSeries(LWC.HistogramSeries, {
        priceScaleId: "vol", priceFormat: { type: "volume" },
        priceLineVisible: false, lastValueVisible: false
      });
    }
    return c.addSeries(LWC.LineSeries, Object.assign({}, common, {
      color: s.color || th.accent, lineWidth: 2,
      lineStyle: s.dashed ? LWC.LineStyle.Dashed : LWC.LineStyle.Solid, priceFormat: pf
    }));
  }

  function build(spec) {
    if (!LWC || typeof LWC.createChart !== "function") {
      fail("the chart library did not load.");
      return;
    }
    var th = spec.theme;
    var first = spec.series[0] && spec.series[0].points;
    if (!first || !first.length) { fail("no data to draw."); return; }
    var firstYear = toDate(first[0].time).getUTCFullYear();
    var lastYear = toDate(first[first.length - 1].time).getUTCFullYear();
    // month and day ticks carry the year when the data span years or sit in a past year
    var needYear = firstYear !== lastYear || lastYear !== new Date().getUTCFullYear();
    var hasVolume = spec.series.some(function (s) { return s.kind === "volume"; });

    if (chart) { chart.remove(); chart = null; }
    host.style.display = "block";
    note.style.display = "none";
    host.style.height = spec.height + "px";
    host.setAttribute("aria-label", spec.label || "Price chart");
    tip.style.background = th.tooltip;
    tip.style.color = th.text;

    chart = LWC.createChart(host, {
      autoSize: true,
      layout: {
        background: { type: "solid", color: th.panel }, textColor: th.meta,
        fontFamily: th.sans, fontSize: 11,
        // the library's logo link carries a title attribute (P-022: none); the attribution is
        // the visible "Charts by TradingView" link Python draws under every chart
        attributionLogo: false
      },
      grid: { vertLines: { visible: false }, horzLines: { color: th.divider } },
      rightPriceScale: {
        borderVisible: false,
        scaleMargins: { top: 0.08, bottom: hasVolume ? 0.26 : 0.08 }
      },
      timeScale: {
        borderVisible: false, minBarSpacing: 0.05, fixLeftEdge: true,
        fixRightEdge: true, tickMarkFormatter: tickFormatter(needYear)
      },
      crosshair: {
        vertLine: { color: th.meta, labelBackgroundColor: th.raised },
        horzLine: { color: th.meta, labelBackgroundColor: th.raised }
      },
      localization: {
        locale: "en-US",
        timeFormatter: fullDate,
        priceFormatter: function (p) { return fmt(p, spec.precision); }
      }
    });

    var drawn = [];
    spec.series.forEach(function (s) {
      var api = seriesFor(chart, s, spec, th);
      var byTime = {};
      var data = s.points.map(function (p) {
        byTime[p.time] = p;
        if (s.kind === "candles") {
          var item = { time: p.time, open: p.open, high: p.high, low: p.low, close: p.close };
          if (p.color) { item.color = p.color; item.wickColor = p.color; }
          return item;
        }
        if (s.kind === "volume") {
          return { time: p.time, value: p.value, color: p.up ? th.up_tint : th.down_tint };
        }
        return { time: p.time, value: p.value };
      });
      api.setData(data);
      drawn.push({ spec: s, api: api, byTime: byTime });
    });
    if (hasVolume) {
      chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    }
    if (spec.last !== null && spec.last !== undefined && isFinite(spec.last)) {
      drawn[0].api.createPriceLine({
        price: spec.last, color: th.text, lineWidth: 1, lineStyle: LWC.LineStyle.Dashed,
        axisLabelVisible: true
      });
    } else {
      drawn[0].api.applyOptions({ priceLineVisible: true });
    }
    chart.timeScale().fitContent();
    lastWidth = host.clientWidth;

    chart.subscribeCrosshairMove(function (param) { showTip(param, drawn, spec); });
    setHeight(wrap.getBoundingClientRect().height);
  }

  // ---- tooltip ----------------------------------------------------------------------------
  function line(label, value) {
    var row = document.createElement("div");
    var k = document.createElement("span");
    k.className = "k";
    k.textContent = label;
    row.appendChild(k);
    row.appendChild(document.createTextNode(value));
    return row;
  }

  function showTip(param, drawn, spec) {
    if (!param || !param.time || !param.point || param.point.x < 0 || param.point.y < 0) {
      tip.style.display = "none";
      return;
    }
    var t = key(param.time);
    while (tip.firstChild) { tip.removeChild(tip.firstChild); }
    var head = document.createElement("div");
    head.className = "d";
    head.textContent = fullDate(param.time);
    tip.appendChild(head);
    var shown = 0;
    drawn.forEach(function (d) {
      var p = d.byTime[t];
      if (!p) { return; }
      var s = d.spec;
      var m = s.meta[p.m] || ["", "", ""];
      var name = s.name ? s.name + " " : "";
      if (s.kind === "candles" && p.ohlc) {
        tip.appendChild(line("Open", fmt(p.open, spec.precision)));
        tip.appendChild(line("High", fmt(p.high, spec.precision)));
        tip.appendChild(line("Low", fmt(p.low, spec.precision)));
        tip.appendChild(line("Close", fmt(p.close, spec.precision)));
      } else if (s.kind === "candles") {
        tip.appendChild(line("Close", fmt(p.close, spec.precision)));
      } else if (s.kind === "volume") {
        tip.appendChild(line("Volume", fmtVol(p.value)));
      } else {
        tip.appendChild(line(name || "Value", fmt(p.value, spec.precision)));
      }
      tip.appendChild(line("Field", m[0]));
      tip.appendChild(line("Tier", m[1]));
      tip.appendChild(line("Source", m[2]));
      tip.appendChild(line("As of", fullDate(p.time)));
      shown += 1;
    });
    if (!shown) { tip.style.display = "none"; return; }
    tip.style.display = "block";
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var W = host.clientWidth, H = host.clientHeight;
    var x = param.point.x + 14, y = param.point.y + 14;
    if (x + w > W - 4) { x = Math.max(4, param.point.x - w - 14); }
    if (y + h > H - 4) { y = Math.max(4, H - h - 4); }
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }

  // ---- width: refit candles when the container width changes (e.g. a phone turning) -----
  if (window.ResizeObserver) {
    new ResizeObserver(function () {
      if (chart && host.clientWidth && host.clientWidth !== lastWidth) {
        lastWidth = host.clientWidth;
        chart.timeScale().fitContent();
      }
    }).observe(host);
  }

  // ---- protocol ---------------------------------------------------------------------------
  window.addEventListener("message", function (event) {
    if (event.source !== window.parent) { return; }  // only Streamlit, the parent frame
    var data = event.data;
    if (!data || data.type !== "streamlit:render") { return; }
    var spec = data.args && data.args.spec;
    currentSig = (spec && spec.sig) || "";
    if (!spec) { fail("no chart data was sent."); return; }
    var text = JSON.stringify(spec);
    if (text === lastSpec && chart) { return; }
    lastSpec = text;
    try {
      build(spec);
    } catch (err) {
      fail(err && err.message ? err.message : String(err));
    }
  });

  send("streamlit:componentReady", { apiVersion: 1 });
})();
