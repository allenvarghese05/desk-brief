"""The morning note (P-024, card T66): recorded model replies only, never a live call.

SYNTHETIC FIXTURE: every value is made up (``brief_synth`` builders). The replies below are
written for these invented values; the API key is a fake string that is never sent anywhere.
"""
# ruff: noqa: RUF001  (the sigma and minus signs are intentional output)

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


def test_numbers_keep_their_units_and_skip_names() -> None:
    found = numbers("S&P 500 futures fell −1.6% and the 10Y rose 3bp; 2s10s at +22 bp, $71.20.")
    assert [(n.value, n.unit) for n in found] == [(1.6, "pct"), (3.0, "bp"), (22.0, "bp"),
                                                  (71.2, "usd")]  # fmt: skip
    assert numbers("the 2-year and 10-year yields") == []
    assert [(n.value, n.unit) for n in numbers("a 3.5σ day, 1,200 contracts")] == [
        (3.5, "sigma"), (1200.0, "contracts"),
    ]  # fmt: skip


def test_a_number_matches_only_within_the_shown_rounding_and_unit() -> None:
    shown = numbers("−1.6%  4.37%  −3bp  5,976.00")
    assert check("It fell 1.6%.", shown, (), 5).kept
    assert not check("It fell 1.7%.", shown, (), 5).kept
    assert not check("It fell 1.6bp.", shown, (), 5).kept  # the unit the model does not use
    assert check("It fell 3 basis points to 4.37%.", shown, (), 5).kept
    assert not check("It was 16 Nov.", shown, (), 5).kept  # a bare number the model never shows
    assert check("The index was 5,976.00.", shown, (), 5).kept  # a bare level it does show


def test_at_most_max_sentences_are_kept() -> None:
    text = "\n".join(f"Sentence {w}." for w in ("one", "two", "three", "four"))
    out = check(text, [], (), 2)
    assert out.kept == ("Sentence one.", "Sentence two.")
    assert out.dropped == ("over the sentence limit", "over the sentence limit")


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


def test_month_spend_sums_every_archived_note_of_the_month(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path / "data")
    _archive(store, date(2026, 11, 2), 1, {"cost_usd": 0.01})
    _archive(store, date(2026, 11, 2), 2, {"cost_usd": 0.02})  # a re-run is spend too
    _archive(store, date(2026, 11, 13), 1, None)
    _archive(store, date(2026, 10, 30), 1, {"cost_usd": 9.0})  # last month
    assert month_spend(store, date(2026, 11, 16)) == pytest.approx(0.03)


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
    shown = shown_numbers(facts(model, {1}))
    chg, _, _ = _es(model)
    assert any(abs(s.value - float(chg.rstrip("%"))) < 1e-9 and s.unit == "pct" for s in shown)


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
