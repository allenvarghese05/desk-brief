"""The morning note (P-024, card T66): recorded model replies only, never a live call.

SYNTHETIC FIXTURE: every value is made up (``brief_synth`` builders). The replies below are
written for these invented values; the API key is a fake string that is never sent anywhere.
"""
# ruff: noqa: RUF001, RUF003  (the sigma and minus signs are intentional output)

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from brief_synth import FRI, MON, MON_0700, World, daily, et, monday_rows, tick
from deskbrief.calendars import is_business_day
from deskbrief.report import note as note_mod
from deskbrief.report.model import BriefModel, Note
from deskbrief.report.note import (
    check,
    facts,
    month_spend,
    numbers,
    record_spend,
    shown_numbers,
    stored_note,
    with_note,
    write_note,
)
from deskbrief.store.backend import report_key
from deskbrief.store.local import LocalBackend

KEY = {"ANTHROPIC_API_KEY": "fake-key-for-tests"}
YH = {"source": "yahoo", "dataset": "history", "tier": 2}


def _history(iid: str, base: float, **kw: Any) -> list[dict[str, Any]]:
    rows, v, d, n = [], base, date(2026, 9, 1), 0
    while d <= FRI:
        if is_business_day("XNYS", d):
            rows.append(daily(iid, d, round(v, 6), **kw))
            v = v * (1 + (1 if n % 2 == 0 else -1) * (1, 2, 3, 4, 5)[n % 5] * 0.002)
            n += 1
        d += timedelta(days=1)
    return rows


def _price(iid: str, k: float, base: float, unit: str = "USD", **tick_kw: Any) -> list[Any]:
    rows = _history(iid, base, unit=unit, **YH)
    return [*rows, tick(iid, et(MON, "06:58"), rows[-1]["value"] * (1 + k * 0.002), unit=unit,
                        **tick_kw)]  # fmt: skip


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    base = [r for r in monday_rows() if r["instrument_id"] not in ("ES_F", "WTI")]
    w.observations([*base, *_price("ES_F", -8, 6000.0, "index_pts"), *_price("WTI", -2, 70.0)])
    return w


@pytest.fixture
def model(world: World) -> BriefModel:
    return BriefModel.from_store(world.connect(), world.config, MON, MON_0700)


class Fake:
    """A recorded reply (or a raised error) in place of ``anthropic.Anthropic``."""

    def __init__(self, reply: str = "", exc: Exception | None = None, stop: str = "end_turn"):
        self.reply, self.exc, self.stop = reply, exc, stop
        self.calls: list[dict[str, Any]] = []
        self.keys: list[str] = []

    @property
    def messages(self) -> Fake:
        return self

    def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=self.reply)],
            usage=SimpleNamespace(input_tokens=1200, output_tokens=300),
            stop_reason=self.stop,
        )  # fmt: skip

    def factory(self, key: str, timeout: float) -> Fake:
        self.keys.append(key)
        return self


def _es(model: BriefModel) -> tuple[str, str, str]:
    es = next(m for m in model.movers if m.instrument_id == "ES_F")
    return es.change_text.lstrip("+−"), es.value_text, f"{es.sigma:.1f}σ"


def _write(model: BriefModel, world: World, fake: Fake, **kw: Any) -> Note:
    return write_note(model, world.config, KEY, client_factory=fake.factory, **kw)


# ---------------------------------------------------------------------------
# the five recorded replies the card names
# ---------------------------------------------------------------------------


def test_a_fully_matching_reply_keeps_every_sentence(model: BriefModel, world: World) -> None:
    chg, value, sig = _es(model)
    reply = (
        f"S&P futures fell {chg} to {value}, a {sig} move for the 10-year-old contract series.\n"
        "WTI and Brent were the other markets in view."
    )
    fake = Fake(reply)
    n = _write(model, world, fake)
    assert n.reason is None
    assert len(n.sentences) == 2
    assert (n.n_checked, n.n_numbers) == (3, 3)  # "10-year" is a name, not a number
    assert n.model == "claude-sonnet-5-5"
    assert n.cost_usd == pytest.approx((1200 * 2.0 + 300 * 10.0) / 1e6)
    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "low"}
    assert call["max_tokens"] == 8000
    assert fake.keys == ["fake-key-for-tests"]


def test_a_reply_with_a_wrong_number_loses_that_sentence(model: BriefModel, world: World) -> None:
    chg, _, sig = _es(model)
    reply = f"S&P futures fell {chg}, a {sig} day.\nWTI fell 9.9% overnight."
    n = _write(model, world, Fake(reply))
    assert n.sentences == (f"S&P futures fell {chg}, a {sig} day.",)
    assert n.dropped == ("number 9.9 pct matches no value in the brief",)


def test_a_reply_with_a_forbidden_word_loses_that_sentence(model: BriefModel, world: World) -> None:
    chg, _, _ = _es(model)
    reply = f"S&P futures fell {chg}.\nTraders should buy the dip."
    n = _write(model, world, Fake(reply))
    assert n.sentences == (f"S&P futures fell {chg}.",)
    assert n.dropped == ("forbidden word 'buy'",)  # the first listed word it holds


def test_an_empty_reply_gives_no_note_today(model: BriefModel, world: World) -> None:
    n = _write(model, world, Fake("   "))
    assert n.sentences == ()
    assert n.reason == "the reply was empty"


def test_a_timeout_gives_no_note_today(model: BriefModel, world: World) -> None:
    import anthropic
    import httpx2

    exc = anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.example.test"))
    n = _write(model, world, Fake(exc=exc))
    assert n.sentences == ()
    assert n.reason == "the API timed out"


# ---------------------------------------------------------------------------
# the other ways there is no note
# ---------------------------------------------------------------------------


def test_no_key_never_calls_the_api(model: BriefModel, world: World) -> None:
    fake = Fake("anything")
    n = write_note(model, world.config, {}, client_factory=fake.factory)
    assert n.reason == "no API key is configured"
    assert fake.calls == []


def test_turned_off_in_config(model: BriefModel, world: World) -> None:
    brief = world.config.brief
    off = world.config.model_copy(
        update={"brief": brief.model_copy(update={"note": brief.note.model_copy(
            update={"enabled": False})})}
    )  # fmt: skip
    fake = Fake("anything")
    n = write_note(model, off, KEY, client_factory=fake.factory)
    assert n.reason == "the note is turned off in config"
    assert fake.calls == []


def test_the_spend_cap_stops_the_call(model: BriefModel, world: World) -> None:
    fake = Fake("anything")
    n = _write(model, world, fake, spent_usd=4.95)  # the worst case would pass 5 USD
    assert n.reason == "the monthly spend cap of $5.00 is reached"
    assert fake.calls == []
    assert _write(model, world, Fake("Nothing moved."), spent_usd=1.0).reason is None


def test_an_api_error_names_only_its_type(model: BriefModel, world: World) -> None:
    n = _write(model, world, Fake(exc=RuntimeError("secret detail fake-key-for-tests")))
    assert n.reason == "the API returned an error (RuntimeError)"
    assert "fake-key" not in json.dumps(n.__dict__, default=str)


def test_a_refusal_is_no_note(model: BriefModel, world: World) -> None:
    assert (
        _write(model, world, Fake("x", stop="refusal")).reason == "the model declined to write it"
    )


def test_no_surviving_sentence_is_no_note(model: BriefModel, world: World) -> None:
    n = _write(model, world, Fake("Gold will rise 12% today."))
    assert n.sentences == ()
    assert n.reason == "no sentence passed the number check"


# ---------------------------------------------------------------------------
# the check itself
# ---------------------------------------------------------------------------


def _nums(text: str) -> list[tuple[str, str, str | None]]:
    return [(str(n.value), n.unit, n.sign) for n in numbers(text)]


def test_numbers_keep_their_units_signs_and_skip_names() -> None:
    found = _nums("S&P 500 futures fell −1.6% and the 10Y rose 3bp; 2s10s at +22 bp, $71.20.")
    assert found == [("1.6", "pct", "-"), ("3", "bp", None), ("22", "bp", "+"),
                     ("71.20", "usd", None)]  # fmt: skip
    assert numbers("the 2-year and 10-year yields") == []
    assert _nums("a 3.5σ day, 1,200 contracts, +0.25pp") == [
        ("3.5", "sigma", None), ("1200", "contracts", None), ("0.25", "pp", "+"),
    ]  # fmt: skip


SHOWN = numbers("−1.6%  4.37%  −3bp  +27bp  5,976.00  2%  +0.07pp  3.8σ")


def kept(text: str, names: tuple[str, ...] = ()) -> bool:
    return bool(check(text, SHOWN, (), 5, names).kept)


def test_a_number_matches_only_as_shown_with_its_unit() -> None:
    assert kept("It fell 1.6%.")
    assert not kept("It fell 1.7%.")
    assert not kept("It fell 1.6bp.")  # a unit the brief does not use for it
    assert kept("It fell 3 basis points to 4.37%.")
    assert kept("Spreads widened 27bps.")  # bps is bp
    assert kept("The index was 5,976.00.")  # a bare level the brief shows
    assert kept("Real yields rose 0.07pp.")  # pp is read, not taken for a bare 0


@pytest.mark.parametrize(
    "sentence",
    [
        "Spreads widened fifteen basis points.",  # a number written as a word
        "Volume was double the usual.",
        "Insiders sold $40m of stock.",  # suffixes no shown value carries
        "Volume was 3x normal.",
        "Spreads widened 37bpz.",  # a digit inside a word
        "The yield fell .5%.",  # a leading dot is read, and matches nothing
        "Real yields rose 0.91pp.",
        "This is the largest move in 30 years.",  # a count, not a name
        "Gold ran for 26 days straight.",
        "Futures fell for 7 sessions.",
    ],
)
def test_numbers_the_check_cannot_read_or_match_fail_closed(sentence: str) -> None:
    assert not kept(sentence)


def test_the_note_may_not_add_precision_or_drift_within_the_rounding() -> None:
    assert not kept("Equities rose 2.4%.")  # shown "2%"
    assert not kept("Spreads widened 27.4bp.")  # shown "+27bp"
    assert not kept("Futures fell 1.64%.")  # shown "−1.6%"
    assert kept("The 10-year was 4.4%.")  # coarser than "4.37%" is allowed


def test_an_explicit_sign_or_a_direction_word_must_agree() -> None:
    assert not kept("Futures moved +1.6%.")  # shown "−1.6%"
    assert kept("Futures moved −1.6%.")
    assert not kept("Futures rose 1.6%.")
    assert kept("Futures fell 1.6%.")
    assert not kept("Spreads narrowed 27bp.")  # shown "+27bp"
    assert kept("Spreads widened 27bp, while the index fell 1.6%.")
    assert kept("Spreads widened 0.07pp to 4.37%.")  # a level carries no direction


def test_market_names_with_digits_are_not_numbers() -> None:
    assert kept("The S&P fut fell 1.6%.", names=("S&P fut",))
    assert kept("10Y real yields fell 3bp.", names=("10Y real",))
    assert not kept("COT 7 fell 1.6%.")  # an unknown name with a digit fails


def test_forbidden_words_match_stems_and_allow_market_words() -> None:
    words = ("buy", "sell", "target", "expect", "forecast", "predict", "recommend", "will rise")
    for bad in ("Funds were selling.", "Desks are buying.", "It was expected.",
                "Banks raised targets.", "One forecasts more.", "It was predicted.",
                "He recommends patience.", "Gold will likely rise."):  # fmt: skip
        assert not check(bad, (), words, 5).kept, bad
    for ok in ("A sell-off hit stocks.", "The buyback was paused.", "Sellers stepped back."):
        assert check(ok, (), words, 5).kept, ok


def test_at_most_max_sentences_are_kept() -> None:
    text = "\n".join(f"Sentence {w}." for w in ("alpha", "beta", "gamma", "delta"))
    out = check(text, [], (), 2)
    assert out.kept == ("Sentence alpha.", "Sentence beta.")
    assert out.dropped == ("over the sentence limit", "over the sentence limit")


def test_dates_and_times_in_the_facts_are_never_allowed_numbers(model: BriefModel) -> None:
    payload = facts(model, {1})
    assert "07:00" in payload["brief"]
    shown = shown_numbers(payload)
    out = check("Futures fell for 7 sessions.\nThe drop came on day 16.", shown, (), 5)
    assert out.kept == ()
    assert all(s.unit or s.decimals for s in shown if s.value in (7, 16, 2026))


def test_a_reply_cut_at_the_token_limit_is_no_note(model: BriefModel, world: World) -> None:
    chg, _, _ = _es(model)
    n = _write(model, world, Fake(f"S&P futures fell {chg} and then", stop="max_tokens"))
    assert n.sentences == ()
    assert n.reason == "the reply stopped at the token limit"
    assert n.cost_usd > 0


def test_the_four_invented_claims_of_the_review_are_all_dropped(
    model: BriefModel, world: World
) -> None:
    reply = (
        "S&P futures rose fifteen percent. Spreads widened 37bps. "
        "It was the largest drop in 40 years. Futures fell for 7 sessions."
    )
    n = _write(model, world, Fake(reply))
    assert n.sentences == ()
    assert n.reason == "no sentence passed the number check"
    assert len(n.dropped) == 4


# ---------------------------------------------------------------------------
# hosted mode, spend and the stored note
# ---------------------------------------------------------------------------


def test_hosted_mode_never_reads_tier1_values_into_the_prompt(tmp_path: Path) -> None:
    w = World(tmp_path)
    alp = {"source": "alpaca", "dataset": "iex_latest", "tier": 1}
    aapl = _history("AAPL", 200.0, unit="USD", **YH)
    t1 = aapl[-1]["value"] * 1.02
    w.observations([*monday_rows(), *aapl, tick("AAPL", et(MON, "06:58"), t1, **alp)])
    m = BriefModel.from_store(w.connect(), w.config, MON, MON_0700)  # the email's model
    assert any(x.instrument_id == "AAPL" for x in m.movers)  # tier 1 is a mover in the email
    spy_t1 = next(r for r in m.rows() if r.instrument_id == "SPY")
    assert spy_t1.tier == 1
    fake = Fake("Nothing to add.")
    write_note(m, w.config, KEY, client_factory=fake.factory)
    sent = fake.calls[0]["messages"][0]["content"]
    payload = json.loads(sent.split("\n", 1)[1])
    assert all(x["market"] != "AAPL" for x in payload["moved_more_than_usual"])
    aapl_row = next(r for r in m.rows() if r.instrument_id == "AAPL")
    for text in (aapl_row.value_text, spy_t1.value_text):
        assert text not in sent
    assert payload == facts(m, {1})


def _archive(store: LocalBackend, d: date, version: int, note: dict[str, Any] | None) -> None:
    body = json.dumps({"note": note}).encode()
    store.put_bytes(report_key(d, version, "model.json"), body)


def test_month_spend_sums_the_ledger_of_the_calls_month(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path / "data")
    for when, cost in ((et(date(2026, 11, 2), "07:00"), 0.01), (et(date(2026, 11, 2), "07:05"),
                       0.02), (et(date(2026, 10, 30), "07:00"), 9.0)):  # fmt: skip
        record_spend(store, when, Note(cost_usd=cost, model="m"))
    _archive(store, date(2026, 11, 13), 1, {"cost_usd": 4.0})  # a brief's copy is not the ledger
    assert month_spend(store, date(2026, 11, 16)) == pytest.approx(0.03)
    assert month_spend(store, et(date(2026, 11, 16), "07:00")) == pytest.approx(0.03)


def test_with_note_records_the_spend_before_anything_else_can_fail(
    model: BriefModel, world: World
) -> None:
    chg, _, _ = _es(model)
    with_note(model, world.config, KEY, world.store, client_factory=Fake(
        f"S&P futures fell {chg}.").factory, now=MON_0700)  # fmt: skip
    keys = world.store.list("reports/note_spend/month=2026-11/")
    assert len(keys) == 1
    assert month_spend(world.store, MON) == pytest.approx((1200 * 2.0 + 300 * 10.0) / 1e6)


def test_a_timeout_counts_the_worst_case_against_the_cap(model: BriefModel, world: World) -> None:
    n = _write(model, world, Fake(exc=TimeoutError("slow")))
    assert n.reason == "the API timed out"
    assert n.cost_usd >= 8000 * 10.0 / 1e6


def test_an_unexpected_error_costs_the_note_never_the_brief(
    model: BriefModel, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> object:
        raise KeyError("a bug")

    monkeypatch.setattr(note_mod, "facts", boom)
    out = with_note(model, world.config, KEY, world.store, client_factory=Fake("x").factory)
    assert out.note is not None
    assert out.note.reason == "the note could not be written (KeyError)"


def test_stored_note_reads_the_newest_version(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path / "data")
    assert stored_note(store, MON) is None
    _archive(store, MON, 1, {"sentences": ["Old."], "n_checked": 0, "n_numbers": 0})
    _archive(store, MON, 2, {"sentences": ["New 1.6%."], "n_checked": 1, "n_numbers": 1,
                             "model": "claude-sonnet-5-5", "cost_usd": 0.004})  # fmt: skip
    n = stored_note(store, MON)
    assert n is not None
    assert (n.sentences, n.n_checked, n.n_numbers) == (("New 1.6%.",), 1, 1)


def test_with_note_stores_the_note_in_the_model(model: BriefModel, world: World) -> None:
    chg, _, _ = _es(model)
    out = with_note(model, world.config, KEY, world.store, client_factory=Fake(
        f"S&P futures fell {chg}.").factory)  # fmt: skip
    assert out.note is not None
    assert out.note.sentences == (f"S&P futures fell {chg}.",)
    back = BriefModel.from_json(out.to_json())
    assert back.note == out.note


def test_the_morning_report_step_writes_the_note(monkeypatch: pytest.MonkeyPatch) -> None:
    src = Path(note_mod.__file__).parent.joinpath("notify.py").read_text(encoding="utf-8")
    assert "model = with_note(model, ctx.config, os.environ, ctx.store)" in src


def test_shown_numbers_cover_the_facts(model: BriefModel) -> None:
    from decimal import Decimal

    shown = shown_numbers(facts(model, {1}))
    chg, _, _ = _es(model)
    assert any(s.value == Decimal(chg.rstrip("%")) and s.unit == "pct" for s in shown)


def test_the_brief_pane_shows_the_stored_note() -> None:
    from app.components import brief

    pane = brief.pane(None, (), MON_0700, None, Note(("S&P futures fell 1.6%.",), 1, 1))
    assert '<p class="db-note-s">S&amp;P futures fell 1.6%.</p>' in pane
    assert "every number checked (1 of 1)" in pane
    none = brief.pane(None, (), MON_0700, None, Note(reason="no API key is configured"))
    assert "No note today: no API key is configured." in none


def test_the_dashboard_reads_the_stored_note_and_never_the_api(tmp_path: Path) -> None:
    from app.components import data

    store = LocalBackend(tmp_path / "data")
    _archive(store, MON, 1, {"sentences": ["Stored."], "n_checked": 0, "n_numbers": 0})
    config = World(tmp_path).config
    config = config.model_copy(
        update={"storage": config.storage.model_copy(update={"local_root": str(store.root)})}
    )
    assert data._stored_note(config, {}, MON).sentences == ("Stored.",)
    assert data._stored_note(config, {}, FRI).reason == "no brief has been written today yet"
    src = Path(data.__file__).read_text(encoding="utf-8")
    assert "anthropic" not in src
