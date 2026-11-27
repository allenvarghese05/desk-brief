"""The morning email v4 (P-024, card T63): email-safe markup, the order of the approved email
mockup, the movers, the snapshot rules (proxy, no trade, weekly dates), Check before using, the
subject line, the dashboard link, the footer and the golden files.

SYNTHETIC FIXTURE: all data comes from ``brief_synth`` (made-up values, labelled there).
Histories move by a repeating 1-5 step cycle, so a final move of 8 steps is a mover.
"""

from __future__ import annotations

import itertools
import os
import re
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

import brief_synth
from brief_synth import FRI, MON, MON_0700, THU, World, daily, et, fixture_config, monday_rows, tick
from deskbrief.calendars import is_business_day
from deskbrief.report import html as report_html
from deskbrief.report.html import FOOTER_LINE, dashboard_url, render_html, render_text
from deskbrief.report.model import BriefModel, Event, Note, Read, _subject

GOLDEN = Path(__file__).parent / "golden"
YH = {"source": "yahoo", "dataset": "history", "tier": 2}


def _history(iid: str, base: float, end: date, **kw: Any) -> list[dict[str, Any]]:
    rows, v, d, n = [], base, date(2026, 9, 1), 0
    while d <= end:
        if is_business_day("XNYS", d):
            rows.append(daily(iid, d, round(v, 6), **kw))
            v = v * (1 + (1 if n % 2 == 0 else -1) * (1, 2, 3, 4, 5)[n % 5] * 0.002)
            n += 1
        d += timedelta(days=1)
    return rows


def price(iid: str, k: float, base: float, unit: str = "USD") -> list[dict[str, Any]]:
    rows = _history(iid, base, FRI, unit=unit, **YH)
    return [*rows, tick(iid, et(MON, "06:58"), rows[-1]["value"] * (1 + k * 0.002), unit=unit)]


def desk_rows() -> list[dict[str, Any]]:
    base = [r for r in monday_rows() if r["instrument_id"] not in ("ES_F", "WTI")]
    return [
        *base,
        *price("ES_F", -8, 6000.0, "index_pts"),
        *price("BRENT", -15, 75.0),
        *price("WTI", -2, 70.0),
        daily("QQQ", FRI, 510.0, **YH, unit="USD"),  # its last close only: no trade since
        daily("COT_ES_NET", date(2026, 11, 3), -70000.0, source="cftc", dataset="cot",
              field="net_position", interval="1w", unit="contracts"),
        daily("COT_ES_NET", date(2026, 11, 10), -50000.0, source="cftc", dataset="cot",
              field="net_position", interval="1w", unit="contracts"),
    ]  # fmt: skip


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    w.observations(desk_rows())
    w.dq(MON, [("fresh", "warn", "yahoo", "ES_F"), ("valid", "pass", "stooq", "SPY")])
    w.fetches(MON, [("yahoo", "ok"), ("stooq", "error")])
    return w


@pytest.fixture
def model(world: World) -> BriefModel:
    return BriefModel.from_store(world.connect(), world.config, MON, MON_0700)


def section(html: str, title: str) -> str:
    """The markup from a section heading to the next one."""
    after = html.split(f">{title}</div>", 1)[1]
    return re.split(r"<div style=\"font-size:11\.5px;letter-spacing:\.09em", after, maxsplit=1)[0]


def test_email_safe_markup(model: BriefModel) -> None:
    html = render_html(model)
    low = html.lower()
    assert "<table" in low
    assert "max-width:640px" in html.replace(" ", "")
    assert 'style="' in html
    for banned in ("<script", "<style", "<link", "onclick", "javascript:", "<iframe", "@import"):
        assert banned not in low
    assert html.count("<table") == html.count("</table>")
    # no external resources: nothing the mail client would fetch (no dashboard URL configured)
    assert not re.search(r"""=\s*["'][^"']*https?://""", low)
    for banned in ("http://", "https://", "<img", "url(", "<form", "src=", "<base"):
        assert banned not in low


def test_sections_follow_the_email_mockup_order(model: BriefModel) -> None:
    html = render_html(model)
    order = [
        "<h1 ",
        ">Moved more than usual</div>",
        ">Today (ET)</div>",
        ">Snapshot</div>",
        ">Check before using</div>",
        "Desk Brief dashboard",
        FOOTER_LINE,
    ]
    at = [html.find(s) for s in order]
    assert -1 not in at, [s for s, i in zip(order, at, strict=True) if i < 0]
    assert at == sorted(at)
    text = render_text(model)
    headings = ["MOVED MORE THAN USUAL", "TODAY (ET)", "SNAPSHOT", "CHECK BEFORE USING"]
    assert [text.find(h) for h in headings] == sorted(text.find(h) for h in headings)


def test_the_read_opens_the_email(model: BriefModel) -> None:
    html = render_html(model)
    assert model.read is not None
    label = model.read.text.split(". ", 1)[0]
    assert re.search(rf"<h1 [^>]*><span style=\"color:#[0-9a-f]+;\">{label}\.</span>", html)
    assert "Desk Brief · Mon 16 Nov · 07:00 ET" in html


def test_movers_are_the_top_five_with_sigma_source_and_time(model: BriefModel) -> None:
    movers = section(render_html(model), "Moved more than usual")
    first = model.movers[0]
    assert first.short.replace("&", "&amp;") in movers
    assert f"{first.sigma:.1f}&sigma;" in movers
    assert "Yahoo &middot; delayed &middot; 06:58" in movers
    assert "last 30 daily moves" in movers or "daily moves" in movers
    assert "new since the Fri 07:00 brief" in movers  # the previous brief's clock time
    many = replace(model, movers=model.movers * 4)
    assert section(render_html(many), "Moved more than usual").count("&sigma;</td>") == 5


def test_no_mover_says_so_with_the_previous_brief_time(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(monday_rows())
    m = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    assert m.movers == ()
    assert "Nothing moved more than usual since the Fri 07:00 brief." in render_html(m)


def test_proxy_row_leaves_when_its_underlying_has_a_value_and_returns_when_not(
    tmp_path: Path, model: BriefModel
) -> None:
    def snap_ids(m: BriefModel) -> set[str]:
        return {r.instrument_id for g in m.snapshot for r in g.rows}

    assert "ES_F" in snap_ids(model)
    assert "SPY" not in snap_ids(model)  # the S&P future has a value: SPY (its proxy) leaves
    w = World(tmp_path / "noes")
    w.observations([r for r in desk_rows() if r["instrument_id"] != "ES_F"])
    alone = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    assert "SPY" in snap_ids(alone)  # no S&P future: the proxy comes back, labelled
    spy = next(r for g in alone.snapshot for r in g.rows if r.instrument_id == "SPY")
    assert spy.meta.startswith("proxy, ")


def test_rows_with_no_trade_since_their_last_close_fold_into_one_grey_line(
    model: BriefModel,
) -> None:
    eq = next(g for g in model.snapshot if g.title == "Equities")
    assert "QQQ" in eq.no_trade
    assert "QQQ" not in {r.instrument_id for r in eq.rows}
    snap = section(render_html(model), "Snapshot")
    assert re.search(r"QQQ[^<]*</td><td colspan=\"2\"[^>]*>no trade since last close<", snap)


def test_weekly_and_monthly_series_are_dated(model: BriefModel) -> None:
    cot = next(r for g in model.snapshot for r in g.rows if r.instrument_id == "COT_ES_NET")
    assert cot.meta == "weekly, 10 Nov"
    assert "ES COT net <span" in section(render_html(model), "Snapshot")


def test_unavailable_markets_fold_into_one_grey_line(model: BriefModel) -> None:
    assert "data unavailable" in section(render_html(model), "Snapshot")
    assert any(g.unavailable for g in model.snapshot)


def test_a_failed_check_keeps_its_marker_in_the_snapshot(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.observations(desk_rows())
    w.dq(MON, [("range", "fail", "stooq", "WTI")])
    m = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    wti = next(r for g in m.snapshot for r in g.rows if r.instrument_id == "WTI")
    assert wti.check
    # PR #102 review round 2: the marker has its own line in Check before using
    dq = [c for c in m.caveats if c.kind == "dq"]
    assert [c.instrument_ids for c in dq] == [("WTI",)]
    assert "A data check failed on this value" in section(render_html(m), "Check before using")


def test_a_mover_whose_check_failed_stays_ranked_and_is_marked(tmp_path: Path) -> None:
    # PR #102 review round 2 N1: one set drives every marker, so the mover, its snapshot row
    # and Check before using agree; the move still ranks
    w = World(tmp_path)
    w.observations([*(r for r in desk_rows() if r["instrument_id"] != "WTI"),
                    *price("WTI", -8, 70.0)])  # fmt: skip
    w.dq(MON, [("range", "fail", "stooq", "WTI")])
    m = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    wti = next(x for x in m.movers if x.instrument_id == "WTI")
    assert wti.check
    snap = next(r for g in m.snapshot for r in g.rows if r.instrument_id == "WTI")
    assert snap.check
    assert any(c.kind == "dq" and c.instrument_ids == ("WTI",) for c in m.caveats)
    text = render_text(m)
    assert "Nothing to check" not in text
    assert "WTI. A data check failed on this value; see Data health." in text


def test_an_old_quote_carries_its_time_in_the_snapshot(tmp_path: Path) -> None:
    w = World(tmp_path)
    # Thursday's evening quote is the latest value; Friday's 07:00 brief already had it
    w.observations([daily("WTI", THU, 72.0, **YH, unit="USD"),
                    tick("WTI", et(THU, "18:30"), 71.0, unit="USD")])  # fmt: skip
    m = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    wti = next(r for g in m.snapshot for r in g.rows if r.instrument_id == "WTI")
    assert wti.meta == "delayed, Thu 18:30"


def test_a_quote_older_than_its_markets_latest_close_carries_its_time(tmp_path: Path) -> None:
    # PR #102 review round 2 N2: Friday 15:00 is after Friday's 07:00 brief but before Friday's
    # close, so on Monday it is stale and shows its day and time
    w = World(tmp_path)
    w.observations([daily("WTI", THU, 72.0, **YH, unit="USD"),
                    tick("WTI", et(FRI, "15:00"), 71.0, unit="USD"),
                    tick("BRENT", et(MON, "06:58"), 75.0, unit="USD")])  # fmt: skip
    m = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    snap = {r.instrument_id: r for g in m.snapshot for r in g.rows}
    assert snap["WTI"].meta == "delayed, Fri 15:00"
    assert snap["BRENT"].meta == "delayed"  # after Friday's close: current, no time


def test_check_before_using_and_the_check_marker(model: BriefModel) -> None:
    html = render_html(model)
    checks = section(html, "Check before using")
    assert '<b style="color:#9a6700;">Brent vs WTI.</b>' in checks
    assert "&middot; check" in section(html, "Moved more than usual")


def test_footer_is_three_lines_with_the_health_count_and_no_limits_list(
    model: BriefModel,
) -> None:
    html = render_html(model)
    foot = html.split('<div style="margin-top:24px;padding-top:12px;', 1)[1].split("</div>")[0]
    assert foot.count("<br>") == 2
    assert model.health.text in foot
    assert "Data health page" in foot
    assert "● live · ◐ delayed · ○ official" in foot
    assert FOOTER_LINE in foot
    for limit in model.known_limits:  # the full limits list stays on Data health
        assert limit.text.replace("&", "&amp;") not in html


def test_the_movers_note_counts_only_the_movers_shown(model: BriefModel) -> None:
    # PR #102 review round 2 N4: a sixth mover (not listed) never widens the window range
    one = model.movers[0]
    six = (*(replace(one, n_moves=30) for _ in range(5)), replace(one, n_moves=12))
    note = report_html._movers_note(replace(model, movers=six))
    assert "last 30 daily moves" in note
    assert "12" not in note
    assert note.endswith(" 1 more on the dashboard.")
    mixed = (replace(one, n_moves=12), *(replace(one, n_moves=30) for _ in range(4)))
    assert "(12 to 30 of them)" in report_html._movers_note(replace(model, movers=mixed))


def test_dashboard_link_only_for_an_https_url(model: BriefModel) -> None:
    assert dashboard_url({}) is None
    assert dashboard_url({"DESKBRIEF_DASHBOARD_URL": "http://example.test"}) is None
    assert dashboard_url({"DESKBRIEF_DASHBOARD_URL": 'https://x.test/"><b'}) is None
    for bad in ("\n", "\t", "\x7f", "\x85", "\x9f", "\u2028", "\u2029"):
        assert dashboard_url({"DESKBRIEF_DASHBOARD_URL": f"https://x.test/a{bad}b"}) is None
    url = dashboard_url({"DESKBRIEF_DASHBOARD_URL": " https://desk.example.test/ "})
    assert url == "https://desk.example.test/"
    html = render_html(model, dashboard=url)
    assert '<a href="https://desk.example.test/"' in html
    assert "Open the dashboard: https://desk.example.test/" in render_text(model, dashboard=url)


def test_note_or_no_note_today(model: BriefModel) -> None:
    kept = replace(model, note=Note(("S&P futures fell 1.6%.",), 1, 1, model="m"))
    html = render_html(kept)
    assert "S&amp;P futures fell 1.6%." in html
    assert "Every number checked (1 of 1)." in html
    gone = replace(model, note=Note(reason="the API timed out"))
    assert "No note today: the API timed out." in render_html(gone)
    assert "No note today: the API timed out." in render_text(gone)
    assert "No note today" not in render_html(model)  # no note stored: nothing in its place
    for quiet in ("no API key is configured", "the note is turned off in config"):
        silent = replace(model, note=Note(reason=quiet))
        assert "No note today" not in render_html(silent)  # the log and Data health say why
        assert quiet not in render_text(silent)


def test_event_titles_from_sources_are_escaped_and_kept_on_one_line(tmp_path: Path) -> None:
    from deskbrief.query import EventItem
    from deskbrief.report.model import today_events

    hostile = EventItem(MON, et(MON, "08:30"), "release", '<script>x</script>\nInjected  line',
                        "fred", "release_dates", "Oct\n", None, {"prior": "+1.4%"})  # fmt: skip
    events = today_events([hostile], MON)
    w = World(tmp_path)
    m = replace(BriefModel.from_store(w.connect(), w.config, MON, MON_0700), today=events)
    html = render_html(m)
    assert "<script>x" not in html
    assert "&lt;script&gt;x&lt;/script&gt; Injected line, Oct" in html
    assert "&middot; prior +1.4%" in html
    text = render_text(m)
    assert "  08:30  <script>x</script> Injected line, Oct · prior +1.4% (FRED)" in text
    assert "Injected line" in text.split("TODAY (ET)", 1)[1].split("\n")[1]  # one line


def _read(label: str, kind: str) -> Read:
    return Read(label, kind, "flat", None, (), 0, f"{label}.")


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("Risk-off", "regime"),
        ("Risk-on", "regime"),
        ("Mixed", "mixed"),
        ("Quiet", "quiet"),
        ("No clear read", "unclear"),
    ],
)
def test_subject_line_is_the_read_then_desk_brief_and_the_date(label: str, kind: str) -> None:
    assert _subject(_read(label, kind), MON_0700) == f"{label} · Desk Brief · Mon 16 Nov"


def test_notify_sends_the_models_subject_for_a_mixed_read(tmp_path: Path) -> None:
    """Behaviour, not source: the morning report step sends the model's read as the subject."""
    from deskbrief.models import JobKind, RunContext, Trigger
    from deskbrief.report.notify import NullNotifier, report_steps

    cboe = {"source": "cboe", "dataset": "vix_history"}
    vix = _history("VIX", 16.0, FRI, unit="index_pts", **cboe)
    down = tick("VIX", et(MON, "06:58"), vix[-1]["value"] * (1 - 5 * 0.002), unit="index_pts")
    rows = [*price("ES_F", -8, 6000.0, "index_pts"), *vix, down]
    w = World(tmp_path)
    w.observations(rows)
    ctx = RunContext(
        run_id="RUNSUBJ", job=JobKind.MORNING, trigger=Trigger.MANUAL, target_date=MON,
        now_utc=MON_0700, git_sha="x", config_hash="x", config=w.config, store=w.store,
    )  # fmt: skip
    null = NullNotifier()
    build, notify = report_steps(null)
    build(ctx)
    notify(ctx)
    (msg,) = null.messages
    assert msg.subject == "Mixed · Desk Brief · Mon 16 Nov"


def test_template_never_says_what_a_value_is() -> None:
    template = Path(report_html.__file__).parent / "templates" / "brief.html.j2"
    assert "close" not in template.read_text(encoding="utf-8").lower()


def test_text_is_escaped(tmp_path: Path) -> None:
    empty = World(tmp_path / "empty")
    m = BriefModel.from_store(empty.connect(), empty.config, MON, MON_0700)
    html = render_html(m)
    assert "S&amp;P" in html
    assert "S&P" not in html.replace("S&amp;P", "")


# ---------------------------------------------------------------------------
# golden files (invented data; regenerate with UPDATE_GOLDEN=1 and review line by line)
# ---------------------------------------------------------------------------


def _golden_model(tmp_path: Path) -> BriefModel:
    brief_synth._ids = itertools.count(1)
    w = World(tmp_path, fixture_config())
    rows = [r for r in monday_rows() if r["instrument_id"] != "ES_F"]
    w.observations([*rows, *price("ES_F", -8, 6000.0, "index_pts")])  # a mover
    w.dq(MON, [("fresh", "pass", "yahoo", None), ("range", "fail", "stooq", "WTI")])
    w.xchecks(MON, [("sp500_spy_es", "warn", 7.0)], mirror=True)
    w.fetches(MON, [("yahoo", "ok")])
    m = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    return replace(m, config_hash="MASKED")


def _golden_with_note(tmp_path: Path) -> BriefModel:
    """The golden model with a note made by the real slot pipeline (a recorded reply in slots,
    filled and checked by ``note.check``), not a hand-written sentence (review round 3)."""
    from deskbrief.report.note import check, hidden_tiers, market_aliases, slots

    m = _golden_model(tmp_path)
    config = fixture_config()
    hidden = hidden_tiers(config)
    reply = "{ES_F.name} fell {ES_F.chg_1d}, a {ES_F.sigma} move.\nThe euro barely moved."
    out = check(reply, slots(m, hidden), config.brief.note.forbidden_words, 5,
                market_aliases(m, hidden))  # fmt: skip
    note = Note(out.kept, out.n_numbers, out.n_numbers, model="synthetic", written_at=MON_0700)
    events = (
        Event(MON, "08:30", "Durable goods orders", "FRED", "release", "Oct", "+1.4%"),
        Event(MON, "", "4-Week Bill auction", "Treasury", "auction"),
    )
    return replace(m, note=note, today=events)


@pytest.mark.parametrize(
    ("name", "render", "build"),
    [
        ("brief_email.html", render_html, _golden_model),
        ("brief_email.txt", render_text, _golden_model),
        ("brief_email_note.html", render_html, _golden_with_note),
        ("brief_email_note.txt", render_text, _golden_with_note),
    ],
)
def test_golden_email(tmp_path: Path, name: str, render: Any, build: Any) -> None:
    text = render(build(tmp_path))
    if os.environ.get("UPDATE_GOLDEN"):
        (GOLDEN / name).write_text(text, encoding="utf-8")
    assert text == (GOLDEN / name).read_text(encoding="utf-8")


def test_excel_about_sheet_adds_the_read_line(model: BriefModel, tmp_path: Path) -> None:
    import xlsx_read
    from deskbrief.report.excel import write_workbook

    book = xlsx_read.Workbook(write_workbook(model, tmp_path / "brief.xlsx"))
    cells = book.cells("About")
    head = next(ref for ref, c in cells.items() if c.value == "read" and ref.startswith("A"))
    assert model.read is not None
    assert str(cells["B" + head[1:]].value).startswith(model.read.text)


def test_a_future_quote_after_the_nyse_close_but_before_its_own_is_dated(tmp_path: Path) -> None:
    """Round 3 N2: near-24-hour futures close at the 17:00 ET break, not at 16:00, so a WTI
    quote from Friday 16:30 is older than its market's latest close on Monday."""
    w = World(tmp_path)
    w.observations([daily("WTI", THU, 72.0, **YH, unit="USD"),
                    tick("WTI", et(FRI, "16:30"), 71.0, unit="USD")])  # fmt: skip
    m = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)
    wti = next(r for g in m.snapshot for r in g.rows if r.instrument_id == "WTI")
    assert wti.meta == "delayed, Fri 16:30"
