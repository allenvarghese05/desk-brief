"""The templated morning note (P-024, card T66; PR #102 round 4): recorded replies only, never
a live call. The model returns picks (ids and what to show); code validates them and renders
every sentence from fixed templates, so no text of the reply can reach the output.

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
    MAX_CONTRASTS,
    MAX_ITEMS,
    Market,
    allowed_contrasts,
    facts,
    hidden_tiers,
    markets,
    month_spend,
    record_spend,
    render,
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

    def __init__(self, reply: Any = "", exc: Exception | None = None, stop: str = "end_turn"):
        # a dict or list is sent as the JSON text a structured reply carries
        text = reply if isinstance(reply, str) else json.dumps(reply)
        self.reply, self.exc, self.stop = text, exc, stop
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


def _write(model: BriefModel, world: World, fake: Fake, **kw: Any) -> Note:
    return write_note(model, world.config, KEY, client_factory=fake.factory, **kw)


def _mk(model: BriefModel, world: World) -> tuple[dict[str, Market], frozenset[frozenset[str]]]:
    return markets(model, hidden_tiers(world.config)), allowed_contrasts(model, world.config)


ES_MOVE = {"items": [{"market_id": "ES_F", "show": "move"}], "contrasts": []}


def _es(model: BriefModel) -> Any:
    return next(m for m in model.movers if m.instrument_id == "ES_F")


def _every_sentence(table: dict[str, Market], pairs: Any) -> set[str]:
    """Every sentence the templates can render from ``table``: an output outside this set would
    hold text that did not come from the brief."""
    out = set()
    for k in table.values():
        for show in ("move", "move_and_level", "move_and_sigma"):
            if show == "move_and_sigma" and (k.sigma is None or k.sign == 0):
                continue
            out.add(note_mod._sentence(k, show)[0])
    for pair in pairs:
        a, b = sorted(pair)
        if a in table and b in table:
            out.add(note_mod._contrast(table[a], table[b])[0])
            out.add(note_mod._contrast(table[b], table[a])[0])
    return out


# ---------------------------------------------------------------------------
# a valid reply, rendered from the templates
# ---------------------------------------------------------------------------


def test_a_valid_reply_is_rendered_from_the_briefs_own_strings(
    model: BriefModel, world: World
) -> None:
    es = _es(model)
    reply = {
        "items": [
            {"market_id": "ES_F", "show": "move_and_sigma"},
            {"market_id": "UST_10Y", "show": "move_and_level"},
            {"market_id": "EURUSD", "show": "move"},
        ],
        "contrasts": [],
    }
    fake = Fake(reply)
    n = _write(model, world, fake)
    assert n.reason is None
    assert n.sentences == (
        # the verb says the direction, so the change is shown without its sign
        f"S&P futures fell {es.change_text.lstrip('−')}, a {es.sigma:.1f}σ day.",
        "10-year fell 3bp to 4.37%.",
        "EUR/USD rose 0.4%.",
    )
    assert (n.n_checked, n.n_numbers) == (5, 5)  # figures rendered
    assert n.model == "claude-sonnet-5-5"
    assert n.cost_usd == pytest.approx((1200 * 2.0 + 300 * 10.0) / 1e6)
    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["max_tokens"] == 8000
    out = call["output_config"]
    assert out["effort"] == "low"
    assert out["format"]["type"] == "json_schema"
    schema = out["format"]["schema"]
    ids = schema["properties"]["items"]["items"]["properties"]["market_id"]["enum"]
    assert ids == sorted(_mk(model, world)[0])  # only today's markets can be named
    assert schema["additionalProperties"] is False
    assert "You write no prose" in call["system"]
    assert fake.keys == ["fake-key-for-tests"]


def test_an_allowed_contrast_is_rendered_and_its_markets_said_once(world: World) -> None:
    table = {
        "BRENT": Market("BRENT", "Brent crude oil", "price", "−4.1%", "$71.20", "4.4σ"),
        "WTI": Market("WTI", "WTI crude oil", "price", "+0.3%", "$69.50", None),
        "VIX": Market("VIX", "Cboe Volatility Index", "price", "+2.1 pts", "19.4", "3.2σ"),
    }
    pairs = frozenset({frozenset({"BRENT", "WTI"})})
    reply = {
        "items": [{"market_id": "BRENT", "show": "move"}, {"market_id": "VIX", "show": "move"}],
        "contrasts": [{"a": "BRENT", "b": "WTI"}, {"a": "VIX", "b": "WTI"}],
    }
    out = render(reply, table, pairs, 5)
    assert out.sentences == (
        "Cboe Volatility Index rose 2.1 pts.",
        "Brent crude oil fell 4.1% while WTI crude oil rose 0.3%.",
    )
    assert out.n_numbers == 3
    assert "contrast VIX/WTI is not an allowed pair" in out.dropped
    assert "BRENT: already in a contrast" in out.dropped


def test_contrasts_come_from_related_pairs_and_check_before_using(
    model: BriefModel, world: World
) -> None:
    pairs = allowed_contrasts(model, world.config)
    assert {frozenset(p.pair) for p in world.config.brief.related_pairs} <= pairs
    # a pair a Check before using item names is allowed too
    from dataclasses import replace

    from deskbrief.report.model import Caveat

    flagged = replace(
        model, caveats=(Caveat("pair", "2Y vs 10Y", "Apart.", ("UST_2Y", "UST_10Y")),)
    )
    assert frozenset({"UST_2Y", "UST_10Y"}) in allowed_contrasts(flagged, world.config)
    assert frozenset({"UST_2Y", "UST_10Y"}) not in pairs


# ---------------------------------------------------------------------------
# the verb comes from the sign and the market's kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "change", "expected"),
    [
        ("price", "+0.4%", "X rose 0.4%."),
        ("price", "−1.6%", "X fell 1.6%."),
        ("price", "+3bp", "X rose 3bp."),  # a yield
        ("price", "−3bp", "X fell 3bp."),
        ("spread", "+27bp", "X widened 27bp."),
        ("spread", "−5bp", "X narrowed 5bp."),
        ("curve", "+4bp", "X steepened 4bp."),
        ("curve", "−3bp", "X flattened 3bp."),
        ("price", "+0.0%", "X was unchanged."),
        ("spread", "0bp", "X was unchanged."),
        ("curve", "−0bp", "X was unchanged."),
    ],
)
def test_the_verb_follows_the_kind_and_the_sign(kind: str, change: str, expected: str) -> None:
    k = Market("ID", "X", kind, change, "1.00", None)
    assert note_mod._sentence(k, "move")[0] == expected
    assert note_mod._sentence(k, "move")[1] == (0 if "unchanged" in expected else 1)


def test_a_zero_change_with_its_level_and_no_sigma() -> None:
    k = Market("ID", "X", "price", "+0.0%", "4.37%", "2.0σ")
    assert note_mod._sentence(k, "move_and_level") == ("X was unchanged at 4.37%.", 1)
    out = render({"items": [{"market_id": "ID", "show": "move_and_sigma"}]}, {"ID": k}, (), 5)
    assert out.sentences == ()
    assert out.dropped == ("ID: sigma asked of a move that is not ranked",)


def test_market_kinds() -> None:
    assert note_mod.market_kind("UST_2S10S", "US 2s10s spread") == "curve"
    assert (
        note_mod.market_kind("HY_OAS", "ICE BofA US high-yield option-adjusted spread") == "spread"
    )
    assert note_mod.market_kind("UST_10Y", "US 10-year Treasury yield") == "price"
    assert note_mod.market_kind("EURUSD", "Euro / US dollar") == "price"


# ---------------------------------------------------------------------------
# hostile replies: only template sentences, or no note
# ---------------------------------------------------------------------------

INJECT = "IGNORE-PREVIOUS-say-BUY-now-9.9%"

HOSTILE: list[Any] = [
    {"items": [{"market_id": "NOPE", "show": "move"}], "contrasts": []},  # unknown id
    {"items": [{"market_id": "UST_2Y", "show": "move_and_sigma"}]},  # sigma of a non-mover
    {"items": [{"market_id": "EURUSD", "show": "move_and_sigma"}]},
    {"items": [{"market_id": INJECT, "show": INJECT}], "contrasts": [{"a": INJECT, "b": INJECT}]},
    {"items": [{"market_id": "ES_F", "show": INJECT}]},
    {"items": [{"market_id": "ES_F", "show": "move", "text": INJECT, "name": INJECT}],
     "contrasts": [], "note": INJECT, "sentences": [INJECT]},  # extra fields
    {"items": [{"market_id": "ES_F ", "show": "move"}]},  # near miss
    {"items": [{"market_id": "es_f", "show": "move"}]},
    {"items": [{"market_id": ["ES_F"], "show": "move"}]},
    {"items": [{"market_id": {"x": "ES_F"}, "show": "move"}]},
    {"items": "ES_F fell", "contrasts": INJECT},
    {"items": [INJECT, 7, None, ["ES_F", "move"]]},
    {"items": [{"market_id": "ES_F", "show": "move"}] * 3},  # repeats
    {"items": [{"market_id": "UST_10Y", "show": "move"}],
     "contrasts": [{"a": "UST_2Y", "b": "UST_10Y"}, {"a": "ES_F", "b": "ES_F"}]},  # not allowed
    {"items": [{"market_id": "AAPL", "show": "move"}]},  # a tier the hosted page hides
    {"items": [{"market_id": "HY_OAS", "show": "move"}]},  # not in today's snapshot
    {"items": [{"market_id": iid, "show": "move"} for iid in
               ("ES_F", "UST_2Y", "UST_10Y", "UST_2S10S", "EURUSD", "WTI", "ES_F")]},  # too many
    [{"market_id": "ES_F", "show": "move"}],  # not an object
    "ES_F fell 50% and you should buy",  # not even JSON
    None,
    {},
]  # fmt: skip


@pytest.mark.parametrize("reply", HOSTILE)
def test_a_hostile_reply_gives_only_template_sentences_or_no_note(
    model: BriefModel, world: World, reply: Any
) -> None:
    table, pairs = _mk(model, world)
    raw = reply if isinstance(reply, str) else json.dumps(reply)
    n = _write(model, world, Fake(raw))
    allowed = _every_sentence(table, pairs)
    assert set(n.sentences) <= allowed
    assert len(n.sentences) <= min(world.config.brief.note.max_sentences, MAX_ITEMS + MAX_CONTRASTS)
    if not n.sentences:
        assert n.reason in ("no pick in the reply was valid",
                            "the reply was not the structured output asked for")  # fmt: skip
    assert INJECT not in json.dumps(n.__dict__, default=str)


def test_no_reply_string_reaches_the_output_except_as_a_mapped_id(
    model: BriefModel, world: World
) -> None:
    """Every string of the reply is a unique token; the stored note and the email may hold none
    of them. A valid id appears only as the brief's display name."""
    tokens = [f"TOKEN{i:02d}xq" for i in range(12)]
    reply = {
        "items": [
            {"market_id": "ES_F", "show": "move", tokens[0]: tokens[1]},
            {"market_id": tokens[2], "show": "move"},
            {"market_id": "WTI", "show": tokens[3]},
            {"market_id": "UST_10Y", "show": "move_and_level", "why": tokens[4]},
        ],
        "contrasts": [{"a": tokens[5], "b": "WTI", "c": tokens[6]}],
        tokens[7]: [tokens[8], {"x": tokens[9]}],
        "preamble": tokens[10],
        "sentences": [tokens[11]],
    }
    n = _write(model, world, Fake(reply))
    assert n.sentences == (
        f"S&P futures fell {_es(model).change_text.lstrip('−')}.",
        "10-year fell 3bp to 4.37%.",
    )
    from dataclasses import replace

    from deskbrief.report.html import render_html, render_text

    with_it = replace(model, note=n)
    for out in (with_it.to_json(), render_html(with_it), render_text(with_it)):
        assert not [t for t in tokens if t in out]
    for s in n.sentences:  # ids never printed: display names only
        assert "ES_F" not in s
        assert "UST_10Y" not in s


def test_a_malformed_reply_is_no_note(model: BriefModel, world: World) -> None:
    n = _write(model, world, Fake('{"items": [{"market_id": "ES_F", "show": "move"}'))
    assert n.sentences == ()
    assert n.reason == "the reply was not the structured output asked for"
    assert n.cost_usd > 0


# ---------------------------------------------------------------------------
# the ways there is no note (kept from rounds 1-3)
# ---------------------------------------------------------------------------


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


def test_no_key_never_calls_the_api(model: BriefModel, world: World) -> None:
    fake = Fake(ES_MOVE)
    n = write_note(model, world.config, {}, client_factory=fake.factory)
    assert n.reason == "no API key is configured"
    assert fake.calls == []


def test_turned_off_in_config(model: BriefModel, world: World) -> None:
    brief = world.config.brief
    off = world.config.model_copy(
        update={"brief": brief.model_copy(update={"note": brief.note.model_copy(
            update={"enabled": False})})}
    )  # fmt: skip
    fake = Fake(ES_MOVE)
    n = write_note(model, off, KEY, client_factory=fake.factory)
    assert n.reason == "the note is turned off in config"
    assert fake.calls == []


def test_the_spend_cap_stops_the_call(model: BriefModel, world: World) -> None:
    fake = Fake(ES_MOVE)
    n = _write(model, world, fake, spent_usd=4.95)  # the worst case would pass 5 USD
    assert n.reason == "the monthly spend cap of $5.00 is reached"
    assert fake.calls == []
    assert _write(model, world, Fake(ES_MOVE), spent_usd=1.0).reason is None


def test_an_api_error_names_only_its_type(model: BriefModel, world: World) -> None:
    n = _write(model, world, Fake(exc=RuntimeError("secret detail fake-key-for-tests")))
    assert n.reason == "the API returned an error (RuntimeError)"
    assert "fake-key" not in json.dumps(n.__dict__, default=str)


def test_a_refusal_is_no_note(model: BriefModel, world: World) -> None:
    n = _write(model, world, Fake(ES_MOVE, stop="refusal"))
    assert n.reason == "the model declined to write it"


def test_a_reply_cut_at_the_token_limit_is_no_note(model: BriefModel, world: World) -> None:
    n = _write(model, world, Fake('{"items": [{"market_id": "ES_F"', stop="max_tokens"))
    assert n.sentences == ()
    assert n.reason == "the reply stopped at the token limit"
    assert n.cost_usd > 0


def test_at_most_max_sentences_are_rendered() -> None:
    table = {f"M{i}": Market(f"M{i}", f"Market {i}", "price", "+1.0%", "1", None) for i in range(5)}
    reply = {"items": [{"market_id": k, "show": "move"} for k in table]}
    out = render(reply, table, (), 3)
    assert len(out.sentences) == 3
    assert out.n_numbers == 3
    assert "2 sentences over the limit of 3" in out.dropped


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
    fake = Fake({"items": [{"market_id": "AAPL", "show": "move_and_sigma"}]})
    n = write_note(m, w.config, KEY, client_factory=fake.factory)
    assert n.sentences == ()  # a hidden tier cannot be picked either
    sent = fake.calls[0]["messages"][0]["content"]
    payload = json.loads(sent.split("\n", 1)[1])
    assert "AAPL" not in payload["moved_more_than_usual"]
    assert all(x["market_id"] not in ("AAPL", "SPY") for x in payload["markets"])
    aapl_row = next(r for r in m.rows() if r.instrument_id == "AAPL")
    for text in (aapl_row.value_text, spy_t1.value_text):
        assert text not in sent
    assert payload == facts(m, w.config, {1})


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
    with_note(model, world.config, KEY, world.store, client_factory=Fake(ES_MOVE).factory,
              now=MON_0700)  # fmt: skip
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
    out = with_note(model, world.config, KEY, world.store, client_factory=Fake(ES_MOVE).factory)
    assert out.note is not None
    assert out.note.sentences == (f"S&P futures fell {_es(model).change_text[1:]}.",)
    back = BriefModel.from_json(out.to_json())
    assert back.note == out.note


def test_the_morning_report_step_writes_the_note(monkeypatch: pytest.MonkeyPatch) -> None:
    src = Path(note_mod.__file__).parent.joinpath("notify.py").read_text(encoding="utf-8")
    assert "model = with_note(model, ctx.config, os.environ, ctx.store)" in src


def test_the_facts_hold_each_markets_display_strings(model: BriefModel, world: World) -> None:
    payload = facts(model, world.config, {1})
    es = _es(model)
    assert {"market_id": "ES_F", "name": "S&P futures", "change": es.change_text,
            "level": es.value_text, "sigma": f"{es.sigma:.1f}σ"} in payload["markets"]  # fmt: skip
    assert ["BRENT", "WTI"] not in payload["allowed_contrasts"]  # Brent is not in today's brief


def test_the_calls_cost_is_kept_when_reading_the_reply_fails(
    model: BriefModel, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> object:
        raise KeyError("a bug after the call")

    monkeypatch.setattr(note_mod, "render", boom)
    out = with_note(model, world.config, KEY, world.store, client_factory=Fake(ES_MOVE).factory,
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


def test_no_figures_means_no_badge(model: BriefModel) -> None:
    from dataclasses import replace

    from deskbrief.report.html import render_html, render_text

    plain = replace(model, note=Note(("Euro / US dollar was unchanged.",), 0, 0, model="m"))
    assert "Every number matches" not in render_html(plain)
    assert "Every number matches" not in render_text(plain)
    assert "Euro / US dollar was unchanged." in render_html(plain)
    counted = replace(model, note=Note(("EUR/USD rose 0.4%.",), 1, 1, model="m"))
    assert "Every number matches the stored data (1 of 1)." in render_text(counted)


def test_a_market_marked_check_says_so_in_the_note_and_the_badge(
    model: BriefModel, world: World
) -> None:
    """Round 4 email N1: a market Check before using names carries "(check)" in the note, as in
    the movers and the snapshot, and the badge says how many figures are from it."""
    from dataclasses import replace

    from deskbrief.report.html import render_html, render_text
    from deskbrief.report.model import Caveat

    flagged = replace(model, caveats=(Caveat("dq", "WTI", "A data check failed.", ("WTI",)),))
    reply = {
        "items": [
            {"market_id": "WTI", "show": "move_and_level"},
            {"market_id": "EURUSD", "show": "move"},
        ]
    }
    n = _write(flagged, world, Fake(reply))
    assert n.sentences == ("WTI crude (check) fell 0.4% to $69.50.", "EUR/USD rose 0.4%.")
    assert (n.n_checked, n.n_numbers) == (1, 3)
    assert (
        n.badge == "Every number matches the stored data (3 of 3); 2 are from a market marked check"
    )
    out = replace(flagged, note=n)
    assert f"{n.badge}." in render_text(out)
    assert f"{n.badge}." in render_html(out)
    assert Note(("X rose 1%.",), 1, 1).badge == "Every number matches the stored data (1 of 1)"


def test_the_note_uses_the_desks_short_names(model: BriefModel, world: World) -> None:
    table = markets(model, hidden_tiers(world.config))
    assert table["ES_F"].name == "S&P futures"
    assert table["UST_10Y"].name == "10-year"
    assert table["UST_2S10S"].name == "2s10s curve"
    assert note_mod.note_name("HY_OAS", "ICE BofA US high-yield option-adjusted spread") == (
        "high-yield spread"
    )
    k = Market("HY_OAS", "high-yield spread", "spread", "+27bp", "4.12%", "3.6σ")
    assert (
        note_mod._sentence(k, "move_and_sigma")[0] == "High-yield spread widened 27bp, a 3.6σ day."
    )
    assert note_mod.note_name("AAPL", "Apple Inc. (common stock)") == "Apple Inc."
