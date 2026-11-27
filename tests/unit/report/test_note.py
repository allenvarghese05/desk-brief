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
    Slot,
    check,
    facts,
    month_spend,
    record_spend,
    slots,
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


def _es(model: BriefModel) -> str:
    """The S&P future's change as a sentence that says "fell" prints it: without its sign."""
    return next(m for m in model.movers if m.instrument_id == "ES_F").change_text.lstrip("−")


def _write(model: BriefModel, world: World, fake: Fake, **kw: Any) -> Note:
    return write_note(model, world.config, KEY, client_factory=fake.factory, **kw)


# ---------------------------------------------------------------------------
# the five recorded replies the card names (the model writes slots, never numbers)
# ---------------------------------------------------------------------------


def test_a_fully_matching_reply_keeps_every_sentence(model: BriefModel, world: World) -> None:
    es = next(m for m in model.movers if m.instrument_id == "ES_F")
    reply = (
        "{ES_F.name} fell {ES_F.chg_1d} to {ES_F.level}, a {ES_F.sigma} move.\n"
        "{WTI.name} slipped {WTI.chg_1d}."
    )
    fake = Fake(reply)
    n = _write(model, world, fake)
    assert n.reason is None
    assert n.sentences[0] == (
        # the verb states the direction (checked), so the change is shown without its sign
        f"E-mini S&P 500 futures fell {es.change_text.lstrip(chr(0x2212))} to {es.value_text}, "
        f"a {es.sigma:.1f}σ move."
    )
    assert len(n.sentences) == 2
    assert (n.n_checked, n.n_numbers) == (4, 4)  # value slots filled
    assert n.model == "claude-sonnet-5-5"
    assert n.cost_usd == pytest.approx((1200 * 2.0 + 300 * 10.0) / 1e6)
    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "low"}
    assert call["max_tokens"] == 8000
    assert "{ES_F.chg_1d}" in call["messages"][0]["content"]  # the slots are in the facts
    assert "Never write a number" in call["system"]
    assert fake.keys == ["fake-key-for-tests"]


def test_a_reply_with_a_wrong_number_loses_that_sentence(model: BriefModel, world: World) -> None:
    reply = "{ES_F.name} fell {ES_F.chg_1d}.\n{WTI.name} fell 9.9% overnight."
    n = _write(model, world, Fake(reply))
    assert n.sentences == (f"E-mini S&P 500 futures fell {_es(model)}.",)
    assert n.dropped == ("it writes a number itself instead of a slot",)


def test_a_reply_with_a_forbidden_word_loses_that_sentence(model: BriefModel, world: World) -> None:
    reply = "{ES_F.name} fell {ES_F.chg_1d}.\nTraders should buy the dip."
    n = _write(model, world, Fake(reply))
    assert n.sentences == (f"E-mini S&P 500 futures fell {_es(model)}.",)
    assert n.dropped == ("forbidden word 'buy'",)


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
    n = _write(model, world, Fake("x", stop="refusal"))
    assert n.reason == "the model declined to write it"


def test_no_surviving_sentence_is_no_note(model: BriefModel, world: World) -> None:
    n = _write(model, world, Fake("Gold will rise twelve percent today."))
    assert n.sentences == ()
    assert n.reason == "no sentence passed the number check"


def test_a_reply_cut_at_the_token_limit_is_no_note(model: BriefModel, world: World) -> None:
    n = _write(model, world, Fake("{ES_F.name} fell {ES_F.chg_1d} and then", stop="max_tokens"))
    assert n.sentences == ()
    assert n.reason == "the reply stopped at the token limit"
    assert n.cost_usd > 0


# ---------------------------------------------------------------------------
# the slot rules, on a small invented table
# ---------------------------------------------------------------------------

TABLE = {
    sl.key: sl
    for sl in (
        Slot("{ES_F.name}", "ES_F", "name", "S&P futures"),
        Slot("{ES_F.level}", "ES_F", "level", "5,976.00"),
        Slot("{ES_F.chg_1d}", "ES_F", "chg_1d", "−1.6%", "-"),
        Slot("{ES_F.sigma}", "ES_F", "sigma", "3.5σ"),
        Slot("{HY_OAS.name}", "HY_OAS", "name", "High-yield OAS"),
        Slot("{HY_OAS.chg_1d}", "HY_OAS", "chg_1d", "+27bp", "+"),
        Slot("{UST_2S10S.name}", "UST_2S10S", "name", "US 2s10s spread"),
        Slot("{UST_2S10S.chg_1d}", "UST_2S10S", "chg_1d", "−3bp", "-"),
        Slot("{UST_20Y.name}", "UST_20Y", "name", "US 20-year Treasury yield"),
        Slot("{UST_20Y.chg_1d}", "UST_20Y", "chg_1d", "0bp", None),
        Slot("{EURUSD.chg_1d}", "EURUSD", "chg_1d", "+0.4%", "+"),
        Slot("{WTI.name}", "WTI", "name", "WTI crude"),
        Slot("{WTI.chg_1d}", "WTI", "chg_1d", "−0.4%", "-"),
    )
}
ALIASES = {
    "ES_F": ("S&P futures", "S&P"),
    "HY_OAS": ("high-yield spreads",),
    "WTI": ("WTI",),
    "EURUSD": ("euro", "EURUSD"),
    "UST_2S10S": ("2s10s",),
}
WORDS = ("buy", "sell", "will rise", "should", "expect", "likely", "could", "may", "might",
         "poised", "set to", "on track", "probabl")  # fmt: skip


def kept(text: str) -> tuple[str, ...]:
    return check(text, TABLE, WORDS, 5, ALIASES).kept


def test_slots_are_filled_with_the_briefs_own_strings() -> None:
    out = check("{ES_F.name} fell {ES_F.chg_1d}, a {ES_F.sigma} move.", TABLE, WORDS, 5, ALIASES)
    assert out.kept == ("S&P futures fell 1.6%, a 3.5σ move.",)  # "fell" says the sign
    plain = check("{ES_F.name} was at {ES_F.chg_1d}.", TABLE, WORDS, 5, ALIASES)
    assert plain.kept == ("S&P futures was at −1.6%.",)  # no direction word: the sign stays
    assert out.n_numbers == 2  # value slots, not name slots


def test_an_unknown_slot_drops_the_sentence() -> None:
    out = check("{ES_F.name} fell {ES_F.chg_5d}.", TABLE, WORDS, 5, ALIASES)
    assert out.kept == ()
    assert out.dropped[0].startswith("unknown slot")


@pytest.mark.parametrize(
    "sentence",
    [
        # round 1: numbers as words, unknown suffixes, a leading dot, counts, invented claims
        "Spreads widened fifteen basis points.",
        "Spreads widened 37bps.",
        "Insiders sold $40m of stock.",
        "Volume was 3x normal.",
        "The yield fell .5%.",
        "Real yields rose 0.91pp.",
        "This is the largest move in 30 years.",
        "Gold ran for 26 days straight.",
        "Futures fell for 7 sessions.",
        "S&P futures rose fifteen percent.",
        "It was the largest drop in 40 years.",
        "Equities rose 2.4%.",
        "Futures moved +1.6%.",
        "Futures rose 1.6%.",
        "The drop came on day 16.",
        # round 2: masked names, fractions and periods, dashes, direction, rounding, markets
        "The 10Y hit a 30-year high.",
        "S&P futures hit a 52-week low.",
        "S&P futures fell, the most in 10Y.",
        "Insiders sold $40M of stock.",
        "Yields fell a quarter point.",
        "A third of the rally faded.",
        "A fifth of the gain went.",
        "It was the worst day in a decade.",
        "Spreads widened ½ a point.",
        "2s10s at –22bp.",
        "S&P futures rallied 1.6%.",
        "S&P futures ended 1.6% higher.",
        "S&P futures posted a 1.6% gain.",
        "S&P futures rose sharply, 1.6%.",
        "2s10s steepened 3bp.",
        "It was a 4σ move.",
        "The euro rose 0%.",
        "The euro was flat in 1 year.",
        "WTI rose 0.4%.",
        # round 3 probes: spans and dates with no digit at all
        "Futures fell for VII sessions.",
        "Futures fell for a week.",
        "Futures fell for a fortnight.",
        "Futures saw their biggest drop since March.",
    ],
)
def test_every_earlier_attack_drops_because_the_model_wrote_a_number(sentence: str) -> None:
    assert kept(sentence) == ()


@pytest.mark.parametrize(
    "sentence",
    [
        "{ES_F.name} rose {ES_F.chg_1d}.",  # a fall written as a rise
        "{ES_F.name} rallied {ES_F.chg_1d}.",
        "{ES_F.name} ended {ES_F.chg_1d} higher.",  # the word after the slot counts too
        "{ES_F.name} posted a {ES_F.chg_1d} gain.",
        "{ES_F.name} rose sharply, {ES_F.chg_1d}.",  # the word one clause before counts too
        "{HY_OAS.name} narrowed {HY_OAS.chg_1d}.",  # a rise written as a fall
        "{UST_2S10S.name} steepened {UST_2S10S.chg_1d}.",  # −3bp is a flattening
        "{UST_20Y.name} rose {UST_20Y.chg_1d}.",  # zero has no direction
    ],
)
def test_a_change_slot_must_agree_with_its_direction_words(sentence: str) -> None:
    assert kept(sentence) == ()


def test_matching_directions_are_kept() -> None:
    assert kept("{ES_F.name} fell {ES_F.chg_1d}, while {HY_OAS.name} widened {HY_OAS.chg_1d}.")
    assert kept("{UST_2S10S.name} flattened {UST_2S10S.chg_1d}.")
    assert kept("{UST_20Y.name} was unchanged at {UST_20Y.chg_1d}.")


def test_a_value_slot_belongs_to_the_market_its_clause_names() -> None:
    assert kept("WTI fell {ES_F.chg_1d}.") == ()  # S&P's change quoted for WTI
    assert kept("WTI rose {EURUSD.chg_1d}.") == ()  # the round-2 case, written with a slot
    assert kept("Prices fell {ES_F.chg_1d}.") == ()  # its market never named
    assert kept("{ES_F.name} fell {ES_F.chg_1d}, more than WTI.")  # WTI's clause has no slot
    assert kept("S&P futures fell {ES_F.chg_1d}.")  # a plain alias names it


def test_hedged_forecasts_are_forbidden() -> None:
    for sentence in ("S&P futures are likely to rise.", "Yields could fall further.",
                     "Gold may rise.", "Oil is poised to rally.", "Spreads are set to widen.",
                     "Futures will probably keep falling."):  # fmt: skip
        assert kept(sentence) == (), sentence


def test_forbidden_words_match_stems_and_allow_market_words() -> None:
    words = ("buy", "sell", "target", "expect", "forecast", "predict", "recommend", "will rise")
    for bad in ("Funds were selling.", "Desks are buying.", "It was expected.",
                "Banks raised targets.", "One forecasts more.", "It was predicted.",
                "He recommends patience.", "Gold will likely rise."):  # fmt: skip
        assert not check(bad, {}, words, 5).kept, bad
    for ok in ("A sell-off hit stocks.", "The buyback was paused.", "Sellers stepped back."):
        assert check(ok, {}, words, 5).kept, ok


def test_market_names_come_from_the_brief_not_the_models_digits() -> None:
    name = Slot("{UST_2Y.name}", "UST_2Y", "name", "US 2-year Treasury yield")
    chg = Slot("{UST_2Y.chg_1d}", "UST_2Y", "chg_1d", "−2bp", "-")
    table = {name.key: name, chg.key: chg}
    out = check("{UST_2Y.name} fell {UST_2Y.chg_1d}.", table, (), 5)
    assert out.kept == ("US 2-year Treasury yield fell 2bp.",)  # the digits are the brief's
    assert check("Two-year yields fell {UST_2Y.chg_1d}.", table, (), 5).kept == ()


def test_at_most_max_sentences_are_kept() -> None:
    text = "\n".join(f"Sentence {w}." for w in ("alpha", "beta", "gamma", "delta"))
    out = check(text, {}, (), 2)
    assert out.kept == ("Sentence alpha.", "Sentence beta.")
    assert out.dropped == ("over the sentence limit", "over the sentence limit")


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
    assert "AAPL" not in payload["moved_more_than_usual"]
    assert all(x["market"] not in ("AAPL", "SPY") for x in payload["slots"])
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
    with_note(model, world.config, KEY, world.store, client_factory=Fake(
        "{ES_F.name} fell {ES_F.chg_1d}.").factory, now=MON_0700)  # fmt: skip
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
    out = with_note(model, world.config, KEY, world.store, client_factory=Fake(
        "{ES_F.name} fell {ES_F.chg_1d}.").factory)  # fmt: skip
    assert out.note is not None
    assert out.note.sentences == (f"E-mini S&P 500 futures fell {_es(model)}.",)
    back = BriefModel.from_json(out.to_json())
    assert back.note == out.note


def test_the_morning_report_step_writes_the_note(monkeypatch: pytest.MonkeyPatch) -> None:
    src = Path(note_mod.__file__).parent.joinpath("notify.py").read_text(encoding="utf-8")
    assert "model = with_note(model, ctx.config, os.environ, ctx.store)" in src


def test_slots_cover_the_facts_and_say_what_they_show(model: BriefModel) -> None:
    table = slots(model, {1})
    es = table["{ES_F.chg_1d}"]
    assert (es.shows.lstrip("−"), es.sign, es.kind) == (_es(model), "-", "chg_1d")
    payload = facts(model, {1})
    assert {"slot": "{ES_F.chg_1d}", "market": "ES_F", "shows": es.shows,
            "what": es.what} in payload["slots"]  # fmt: skip


def test_the_calls_cost_is_kept_when_reading_the_reply_fails(
    model: BriefModel, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> object:
        raise KeyError("a bug after the call")

    monkeypatch.setattr(note_mod, "check", boom)
    out = with_note(model, world.config, KEY, world.store, client_factory=Fake("x").factory,
                    now=MON_0700)  # fmt: skip
    assert out.note is not None
    assert out.note.reason == "the note could not be written (KeyError)"
    assert out.note.cost_usd == pytest.approx((1200 * 2.0 + 300 * 10.0) / 1e6)
    assert month_spend(world.store, MON) == pytest.approx(out.note.cost_usd)


def test_no_client_is_built_under_tests_even_with_a_key(
    model: BriefModel, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-a-shell-might-export")
    out = with_note(model, world.config, dict(__import__("os").environ), world.store)
    assert out.note is not None
    assert out.note.reason == "the API returned an error (NoAnthropicInTestsError)"


def test_configuration_states_are_silent_to_the_reader() -> None:
    assert not Note(reason="no API key is configured").shown
    assert not Note(reason="the note is turned off in config").shown
    assert Note(reason="the API timed out").shown
    assert Note(("Kept.",), 0, 0).shown


def test_a_direction_claim_about_a_market_must_match_its_change() -> None:
    """Round 3: direction words are tied to the market they speak of, slot or no slot."""
    assert kept("Brent rose overnight.") == ()  # no change for Brent in this table
    assert kept("{WTI.name} fell {WTI.chg_1d} and S&P futures rose.") == ()  # S&P fell
    assert kept("S&P futures rose overnight.") == ()
    assert kept("S&P futures fell overnight.")
    assert kept("{ES_F.name} fell {ES_F.chg_1d}, with high-yield spreads wider.")
    assert kept("The market rose.") == ()  # a direction about no market the brief names
    assert kept("A sell-off hit stocks.")  # a noun, not a direction claim


def test_no_figures_means_no_checked_line(model: BriefModel) -> None:
    from dataclasses import replace

    from deskbrief.report.html import render_html, render_text

    worded = replace(model, note=Note(("S&P futures fell overnight.",), 0, 0, model="m"))
    assert "Every number checked" not in render_html(worded)
    assert "Every number checked" not in render_text(worded)
    assert "S&amp;P futures fell overnight." in render_html(worded)


@pytest.mark.parametrize(
    "sentence",
    [
        "{ES_F.name} was at {ES_F.level}k.",  # a letter glued on: a thousandfold claim
        "{ES_F.name} was at -{ES_F.level}.",  # a sign glued on
        "{ES_F.name} was at ${ES_F.level}.",  # a currency symbol glued on
        "{ES_F.name} fell {ES_F.chg_1d}{ES_F.chg_1d}.",  # two slots glued together
        "{ES_F.name} fell x{ES_F.chg_1d}.",
    ],
)
def test_text_glued_to_a_slot_drops_the_sentence(sentence: str) -> None:
    assert kept(sentence) == ()


def test_plain_forecasts_are_forbidden(world: World) -> None:
    words = world.config.brief.note.forbidden_words
    for sentence in ("S&P futures will fall.", "Yields are headed lower.",
                     "Oil is on course for a gain.", "Gold is set for a rise.",
                     "The euro is poised to rally."):  # fmt: skip
        assert check(sentence, TABLE, words, 5, ALIASES).kept == (), sentence
