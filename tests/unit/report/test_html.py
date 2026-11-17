"""HTML report: email-safe markup, tier glyphs, P-009 labels, ref_date labels, footer.

SYNTHETIC FIXTURE: all data comes from ``brief_synth`` (made-up values, labelled there).
"""

from __future__ import annotations

import re
from html import escape
from pathlib import Path

import pytest

from brief_synth import MON, MON_0700, World, monday_rows
from deskbrief.report import html as report_html
from deskbrief.report.html import FOOTER_LINE, render_html, render_text
from deskbrief.report.model import BriefModel


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    w.observations(monday_rows())
    w.dq(MON, [("fresh", "warn", "yahoo", "ES_F"), ("valid", "pass", "stooq", "SPY")])
    w.xchecks(MON, [("sp500_gap", "warn", 7.0)])  # ref_date defaults to Friday 13 Nov
    w.fetches(MON, [("yahoo", "ok"), ("stooq", "error")])
    return w


@pytest.fixture
def model(world: World) -> BriefModel:
    return BriefModel.from_store(world.connect(), world.config, MON, MON_0700)


def row_html(html: str, label: str) -> str:
    """The ``<tr>`` that contains ``label`` (rows are one ``<tr>`` each, no nesting)."""
    needle = escape(label, quote=False)
    return next(t for t in re.findall(r"<tr[ >].*?</tr>", html, re.S) if needle in t)


def label_of(model: BriefModel, iid: str) -> str:
    return next(r for r in model.rows() if r.instrument_id == iid).label


def test_email_safe_markup(model: BriefModel) -> None:
    html = render_html(model)
    low = html.lower()
    assert "<table" in low
    assert "max-width:640px" in html.replace(" ", "")
    assert 'style="' in html
    for banned in ("<script", "<style", "<link", "onclick", "javascript:", "<iframe", "@import"):
        assert banned not in low
    assert html.count("<table") == html.count("</table>")


def test_tier_glyphs_and_proxy_labels_come_from_the_model(model: BriefModel) -> None:
    html = render_html(model)
    ten = row_html(html, label_of(model, "UST_10Y"))
    assert "◐ delayed" in ten
    assert "○ official" in row_html(html, label_of(model, "UST_2Y"))
    spy = row_html(html, label_of(model, "SPY"))
    assert "● live" in spy
    assert "proxy" in spy.lower()


def test_labels_follow_p009_never_a_hardcoded_close(model: BriefModel) -> None:
    html = render_html(model)
    cell = row_html(html, label_of(model, "EURUSD"))
    assert "since previous official value" in cell
    assert "close" not in cell.lower()
    yld = row_html(html, label_of(model, "UST_2Y"))
    assert "close" not in yld.lower()
    # an equity close row says so because the model says so, not because the template does
    template = Path(report_html.__file__).parent / "templates" / "brief.html.j2"
    assert "close" not in template.read_text(encoding="utf-8").lower()


def test_crosscheck_rows_carry_the_model_ref_date_not_the_target_date(
    model: BriefModel,
) -> None:
    html = render_html(model)
    item = next(n for n in model.notable if n.kind == "crosscheck")
    assert item.ref_label == "13 Nov"
    cell = row_html(html, "sp500_gap")
    assert "13 Nov" in cell
    assert "16 Nov" not in cell  # the run's target_date
    assert item.text.replace("&", "&amp;") in html


def test_footer_honesty_line_and_tier_legend(model: BriefModel) -> None:
    html = render_html(model)
    assert FOOTER_LINE == "Personal project built on public data. Not investment advice."
    assert FOOTER_LINE in html
    assert "● live/proxy" in html
    assert "◐ delayed" in html
    assert "○ official" in html


def test_header_headline_health_and_limits_are_shown(model: BriefModel) -> None:
    html = render_html(model)
    assert model.title in html
    assert model.health.text in html
    assert model.headline.text.replace("&", "&amp;") in html
    for limit in model.known_limits:
        assert limit.text.replace("&", "&amp;").replace("<", "&lt;") in html
    assert "fresh" in html
    assert "stooq" in html


def test_row_qualifiers_and_derived_label_are_rendered(model: BriefModel) -> None:
    html = render_html(model)
    assert "delayed quote" in row_html(html, label_of(model, "ES_F"))
    assert "derived" in row_html(html, label_of(model, "UST_2S10S"))


def test_unavailable_rows_are_shown_as_such_and_text_is_escaped(tmp_path: Path) -> None:
    empty = World(tmp_path / "empty")
    model = BriefModel.from_store(empty.connect(), empty.config, MON, MON_0700)
    html = render_html(model)
    assert "data unavailable" in html
    assert "S&amp;P" in html
    assert "S&P" not in html.replace("S&amp;P", "")


def test_plain_text_alternative(model: BriefModel) -> None:
    text = render_text(model)
    assert model.title in text
    assert model.headline.text in text
    assert FOOTER_LINE in text
    assert "<" not in text
