"""Contract tests for card T64 (ruling P-024), widened for T62, T65 and T66: the ``events`` and
``filings`` tables, the ``fiscaldata`` and ``edgar`` sources, and the ``brief``, ``events`` and
``filings`` config sections.

Every frame and config below is a SYNTHETIC FIXTURE built in code. The company is the invented
"Example Corp (EXMP)"; its CIK, accession numbers, insiders, holders and figures are made up.
"""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, get_args
from zoneinfo import ZoneInfo

import pytest
import yaml
from pandera.errors import SchemaErrors
from pydantic import ValidationError

from deskbrief.config import (
    KNOWN_SOURCES,
    SEC_MAX_RATE_PER_MIN,
    BriefSettings,
    ChangeUnit,
    Config,
    EventsSettings,
    FilingsSettings,
    FredRelease,
    NoteSettings,
    RegimeRule,
    RegimeRuleSet,
    RelatedPair,
    SourceSettings,
    load_config,
)
from deskbrief.models import (
    EventKind,
    FilingForm,
    FilingRowKind,
    JobKind,
    RunContext,
    TradeSide,
    Trigger,
)
from deskbrief.schemas import (
    ENUMS,
    EVENTS_NATURAL_KEY,
    EVENTS_SCHEMA,
    FILINGS_NATURAL_KEY,
    FILINGS_SCHEMA,
    OBS_NATURAL_KEY,
    TABLE_SCHEMAS,
    TABLES,
    columns,
    empty_frame,
    to_frame,
    validate,
)
from deskbrief.store import backend
from deskbrief.store.backend import DERIVED_PREFIX, TABLE_LAYER, table_key, table_prefix
from deskbrief.store.datasets import PARTITION_RULES, Datasets, check_partition
from deskbrief.store.duck import DAILY_CLOSE_FIELDS, TABLE_VIEWS
from deskbrief.store.local import LocalBackend

ET = ZoneInfo("America/New_York")
CONFIG_PATH = Path("config.yaml")

#: The fetch instant of the synthetic rows: 06:05 ET on Tue 2026-03-10 (UTC-4 after the DST
#: change), so the ET partition date is 2026-03-10.
INGESTED = datetime(2026, 3, 10, 10, 5, tzinfo=UTC)
PART = date(2026, 3, 10)


# ---------------------------------------------------------------------------------------------
# enums pinned
# ---------------------------------------------------------------------------------------------


def test_event_kind_enum() -> None:
    assert [k.value for k in EventKind] == ["release", "auction"]
    assert ENUMS["event_kind"] == ("release", "auction")


def test_filing_form_enum() -> None:
    expected = ("10-K", "10-K/A", "10-Q", "10-Q/A", "4", "4/A", "13F-HR", "13F-HR/A")
    assert tuple(f.value for f in FilingForm) == expected
    assert ENUMS["filing_form"] == expected


def test_filing_row_and_trade_side_enums() -> None:
    assert ENUMS["filing_row"] == ("filing", "insider_trade", "holder_change")
    assert tuple(r.value for r in FilingRowKind) == ENUMS["filing_row"]
    assert ENUMS["trade_side"] == ("buy", "sell", "other")
    assert tuple(s.value for s in TradeSide) == ENUMS["trade_side"]


def test_existing_enums_unchanged() -> None:
    assert ENUMS["unit"] == ("USD", "pct", "fx_rate", "index_pts", "shares", "index", "contracts")
    assert ENUMS["interval"] == ("tick", "1m", "1d", "1w", "1mo")
    assert ENUMS["job"] == ("morning", "intraday", "eod", "backfill", "rebuild", "history")


def test_known_sources_gain_fiscaldata_and_edgar() -> None:
    assert {"fiscaldata", "edgar"} <= KNOWN_SOURCES
    assert {
        "treasury",
        "fred",
        "ecb",
        "stooq",
        "cboe",
        "alpaca",
        "twelvedata",
        "yahoo",
        "cftc",
    } <= KNOWN_SOURCES
    assert SEC_MAX_RATE_PER_MIN == 600.0  # SEC fair access: 10 requests per second


def test_change_unit_literal_matches_change_rows() -> None:
    assert get_args(ChangeUnit) == ("pct", "bp", "pts", "pp")


# ---------------------------------------------------------------------------------------------
# events table
# ---------------------------------------------------------------------------------------------

EVENTS_COLUMNS = {
    "event_id": "str",
    "source": "str",
    "dataset": "str",
    "kind": "enum:event_kind",
    "title": "str",
    "event_date": "date",
    "scheduled_at": "ts?",
    "period": "str?",
    "previous_value": "float?",
    "previous_unit": "enum:unit?",
    "url": "str?",
    "details_json": "str",
    "fetch_id": "str",
    "run_id": "str",
    "ingested_at": "ts",
}


def test_events_schema_pinned() -> None:
    assert TABLES["events"] == EVENTS_COLUMNS
    assert columns("events") == tuple(EVENTS_COLUMNS)
    assert EVENTS_NATURAL_KEY == ("source", "dataset", "event_id")
    assert TABLE_SCHEMAS["events"] is EVENTS_SCHEMA


def _event(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_id": "auction:2026-03-10:7-year",
        "source": "fiscaldata",
        "dataset": "upcoming_auctions",
        "kind": "auction",
        "title": "7-Year Note auction",
        "event_date": date(2026, 3, 10),
        "scheduled_at": datetime(2026, 3, 10, 13, 0, tzinfo=ET),
        "period": None,
        "previous_value": None,
        "previous_unit": None,
        "url": None,
        "details_json": '{"offering_usd":"invented"}',
        "fetch_id": "F1",
        "run_id": "R1",
        "ingested_at": INGESTED,
    }
    row.update(over)
    return row


def test_auction_and_release_rows_validate() -> None:
    release = _event(
        event_id="release:10:2026-03-11",
        source="fred",
        dataset="release_dates",
        kind="release",
        title="Consumer Price Index",
        event_date=date(2026, 3, 11),
        scheduled_at=None,  # date only
        period="2026-02",
        previous_value=2.5,
        previous_unit="pct",
        url="https://fred.example.test/releases/10",
    )
    df = to_frame("events", [_event(), release])
    validate("events", df)
    assert df["scheduled_at"].iloc[0] == datetime(2026, 3, 10, 17, 0, tzinfo=UTC)
    assert df["scheduled_at"].isna().iloc[1]


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "earnings"},  # earnings dates are out of scope (P-024)
        {"previous_unit": "bananas"},
        {"event_id": ""},
    ],
)
def test_events_reject_bad_values(bad: dict[str, Any]) -> None:
    with pytest.raises(SchemaErrors):
        validate("events", to_frame("events", [_event(**bad)]))


def test_events_reject_naive_and_missing() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_frame("events", [_event(scheduled_at=datetime(2026, 3, 10, 13, 0))])
    with pytest.raises(ValueError, match="required"):
        to_frame("events", [_event(title=None)])


# ---------------------------------------------------------------------------------------------
# filings table
# ---------------------------------------------------------------------------------------------

FILINGS_COLUMNS = {
    "instrument_id": "str",
    "cik": "str",
    "accession": "str",
    "seq": "int",
    "row_kind": "enum:filing_row",
    "form": "enum:filing_form",
    "filed_date": "date",
    "period": "date?",
    "url": "str",
    "party": "str?",
    "party_cik": "str?",
    "party_role": "str?",
    "trade_date": "date?",
    "trade_code": "str?",
    "side": "enum:trade_side?",
    "shares": "float?",
    "shares_change": "float?",
    "price": "float?",
    "value_usd": "float?",
    "plan_10b5_1": "bool",
    "details_json": "str",
    "source": "str",
    "dataset": "str",
    "fetch_id": "str",
    "run_id": "str",
    "ingested_at": "ts",
}

EXMP_CIK = "0009999999"  # invented
EXMP_URL = "https://www.sec.gov/Archives/edgar/data/9999999/000999999926000001/"


def test_filings_schema_pinned() -> None:
    assert TABLES["filings"] == FILINGS_COLUMNS
    assert columns("filings") == tuple(FILINGS_COLUMNS)
    assert FILINGS_NATURAL_KEY == ("instrument_id", "accession", "seq")
    assert TABLE_SCHEMAS["filings"] is FILINGS_SCHEMA


def _filing(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "instrument_id": "EXMP",
        "cik": EXMP_CIK,
        "accession": "0009999999-26-000001",
        "seq": 0,
        "row_kind": "filing",
        "form": "10-Q",
        "filed_date": date(2026, 2, 19),
        "period": date(2025, 12, 31),
        "url": EXMP_URL,
        "plan_10b5_1": False,
        "details_json": "{}",
        "source": "edgar",
        "dataset": "submissions",
        "fetch_id": "F1",
        "run_id": "R1",
        "ingested_at": INGESTED,
    }
    row.update(over)
    return row


def _insider_trade(**over: Any) -> dict[str, Any]:
    base = {
        "accession": "0009999999-26-000002",
        "seq": 1,
        "row_kind": "insider_trade",
        "form": "4",
        "filed_date": date(2026, 3, 2),
        "period": None,
        "party": "Jane Example",
        "party_cik": "0008888888",
        "party_role": "Officer: CFO",
        "trade_date": date(2026, 2, 27),
        "trade_code": "S",
        "side": "sell",
        "shares": 1000.0,
        "price": 142.5,
        "value_usd": 142500.0,
        "plan_10b5_1": True,
        "dataset": "form4",
    }
    base.update(over)
    return _filing(**base)


def _holder_change(**over: Any) -> dict[str, Any]:
    base = {
        "accession": "0007777777-26-000003",
        "seq": 1,
        "row_kind": "holder_change",
        "form": "13F-HR",
        "filed_date": date(2026, 2, 13),
        "period": date(2025, 12, 31),  # the 13F as-of quarter
        "party": "Invented Asset Management",
        "party_cik": "0007777777",
        "shares": 50000.0,
        "shares_change": -12000.0,  # a cut: may be negative
        "value_usd": 7130000.0,
        "dataset": "13f",
    }
    base.update(over)
    return _filing(**base)


def test_filing_trade_and_holder_rows_validate() -> None:
    df = to_frame("filings", [_filing(), _insider_trade(), _holder_change()])
    validate("filings", df)
    assert list(df["row_kind"]) == ["filing", "insider_trade", "holder_change"]
    assert list(df["plan_10b5_1"]) == [False, True, False]


@pytest.mark.parametrize(
    "bad",
    [
        {"cik": "9999999"},  # not zero-padded to 10 digits
        {"accession": "000999999926000001"},  # no dashes
        {"form": "8-K"},  # not a stored form
        {"row_kind": "earnings"},
        {"seq": -1},
    ],
)
def test_filings_reject_bad_keys(bad: dict[str, Any]) -> None:
    with pytest.raises(SchemaErrors):
        validate("filings", to_frame("filings", [_filing(**bad)]))


@pytest.mark.parametrize(
    "bad",
    [
        {"side": "short"},
        {"shares": -5.0},  # direction lives in side, never in the sign
        {"price": -1.0},
        {"value_usd": -1.0},
        {"party_cik": "123"},
    ],
)
def test_insider_trade_rejects_bad_values(bad: dict[str, Any]) -> None:
    with pytest.raises(SchemaErrors):
        validate("filings", to_frame("filings", [_insider_trade(**bad)]))


# ---------------------------------------------------------------------------------------------
# store: layer, partition, round trip; neither table is an observation table
# ---------------------------------------------------------------------------------------------


def test_new_tables_live_under_derived() -> None:
    assert TABLE_LAYER["events"] == DERIVED_PREFIX
    assert TABLE_LAYER["filings"] == DERIVED_PREFIX
    assert table_prefix("events") == "derived/events/"
    assert (
        table_key(table="filings", part_date=PART, run_id="R1")
        == "derived/filings/date=2026-03-10/run=R1.parquet"
    )
    doc = backend.__doc__ or ""
    assert "events              ET date of ``ingested_at``" in doc
    assert "filings             ET date of ``ingested_at``" in doc


def test_partition_is_the_et_date_of_ingested_at() -> None:
    assert PARTITION_RULES["events"][0] == "ingested_at"
    assert PARTITION_RULES["filings"][0] == "ingested_at"
    # the event is next week, the filing last month: both belong to the fetch date's partition
    events = to_frame("events", [_event(event_date=date(2026, 3, 17))])
    filings = to_frame("filings", [_filing()])
    check_partition("events", events, PART)
    check_partition("filings", filings, PART)
    late = to_frame("events", [_event(ingested_at=datetime(2026, 3, 11, 3, 0, tzinfo=UTC))])
    check_partition("events", late, PART)  # 23:00 ET on the 10th
    with pytest.raises(ValueError, match="not partition date"):
        check_partition("events", events, date(2026, 3, 9))


def _ctx(store: LocalBackend) -> RunContext:
    return RunContext(
        run_id="RUNT64",
        job=JobKind.MORNING,
        trigger=Trigger.MANUAL,
        target_date=PART,
        now_utc=INGESTED,
        git_sha="deadbeef",
        config_hash="cafe",
        config=load_config(CONFIG_PATH),
        store=store,
    )


def test_append_and_read_round_trip(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path)
    ds = Datasets(store)
    ctx = _ctx(store)
    key_e = ds.append("events", to_frame("events", [_event()]), ctx, PART)
    rows = [_filing(), _insider_trade(), _holder_change()]
    key_f = ds.append("filings", to_frame("filings", rows), ctx, PART)
    assert key_e == "derived/events/date=2026-03-10/run=RUNT64.parquet"
    assert key_f == "derived/filings/date=2026-03-10/run=RUNT64.parquet"
    events, filings = ds.read("events"), ds.read("filings")
    validate("events", events)
    validate("filings", filings)
    assert list(filings["seq"]) == [0, 1, 1]
    assert filings["shares_change"].iloc[2] == -12000.0
    # zero-row frames are valid too (a run with nothing scheduled)
    validate("events", empty_frame("events"))
    validate("filings", empty_frame("filings"))


def test_neither_table_is_an_observation_table() -> None:
    assert "events" not in TABLE_VIEWS.values()
    assert "filings" not in TABLE_VIEWS.values()
    assert TABLE_VIEWS["obs_all"] == "observations"
    for table in ("events", "filings"):
        cols = set(columns(table))
        assert not {"value", "field", "interval", "as_of", "tier"} & cols
        assert not set(OBS_NATURAL_KEY) <= cols
    assert DAILY_CLOSE_FIELDS == ("close", "yield", "rate", "level")


# ---------------------------------------------------------------------------------------------
# source settings: fiscaldata, edgar, the SEC User-Agent env var and rate limit
# ---------------------------------------------------------------------------------------------


def test_real_config_has_fiscaldata_and_edgar_disabled_until_t65() -> None:
    cfg = load_config(CONFIG_PATH)
    fiscal, edgar = cfg.sources["fiscaldata"], cfg.sources["edgar"]
    assert not fiscal.enabled
    assert not edgar.enabled
    assert edgar.user_agent_env == "DESKBRIEF_SEC_USER_AGENT"
    assert edgar.user_agent is None  # nothing committed
    assert edgar.rate_per_min <= SEC_MAX_RATE_PER_MIN
    assert edgar.rate_per_min == 300  # 5 per second, half SEC's limit


def test_user_agent_env_is_exclusive_with_user_agent() -> None:
    base = {"rate_per_min": 10, "timeout_s": 5, "retries": 1}
    assert SourceSettings.model_validate(base).user_agent_env is None
    s = SourceSettings.model_validate({**base, "user_agent_env": "DESKBRIEF_SEC_USER_AGENT"})
    assert s.user_agent_env == "DESKBRIEF_SEC_USER_AGENT"
    with pytest.raises(ValidationError, match="not both"):
        SourceSettings.model_validate(
            {**base, "user_agent": "x", "user_agent_env": "DESKBRIEF_SEC_USER_AGENT"}
        )
    with pytest.raises(ValidationError):
        SourceSettings.model_validate({**base, "user_agent_env": "has space"})


def _raw() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return copy.deepcopy(data)


def test_edgar_requires_user_agent_env() -> None:
    raw = _raw()
    del raw["sources"]["edgar"]["user_agent_env"]
    with pytest.raises(ValidationError, match="user_agent_env is required"):
        Config.model_validate(raw)


def test_edgar_rate_above_sec_limit_rejected() -> None:
    raw = _raw()
    raw["sources"]["edgar"]["rate_per_min"] = 601
    with pytest.raises(ValidationError, match="exceeds SEC"):
        Config.model_validate(raw)
    raw["sources"]["edgar"]["rate_per_min"] = 600
    Config.model_validate(raw)


# ---------------------------------------------------------------------------------------------
# brief: regime rules, related pairs, note
# ---------------------------------------------------------------------------------------------


def test_real_config_regime_rules() -> None:
    brief = load_config(CONFIG_PATH).brief
    assert [s.label for s in brief.regime_rules] == ["Risk-off", "Risk-on"]
    assert (brief.quiet_label, brief.mixed_label) == ("Quiet", "Mixed")
    risk_off = brief.regime_rules[0]
    assert risk_off.min_met == 2
    assert [(r.market, r.direction) for r in risk_off.rules] == [
        ("ES_F", "down"),
        ("VIX", "up"),
        ("HY_OAS", "up"),
        ("UST_2Y", "down"),
    ]
    assert [r.label for r in risk_off.rules] == [
        "Equity futures down",
        "VIX up",
        "High-yield spreads wider",
        "Front-end yields down",
    ]
    risk_on = brief.regime_rules[1]
    # the mirror image: same markets, opposite directions
    flip = {"up": "down", "down": "up"}
    assert [(r.market, flip[r.direction]) for r in risk_on.rules] == [
        (r.market, r.direction) for r in risk_off.rules
    ]
    assert all(
        r.min_sigma == 1.0 and r.min_change is None for s in brief.regime_rules for r in s.rules
    )


def test_real_config_related_pairs() -> None:
    pairs = load_config(CONFIG_PATH).brief.related_pairs
    assert [(p.pair, p.limit, p.unit) for p in pairs] == [
        (("BRENT", "WTI"), 1.0, "pct"),
        (("GOLD_F", "GLD"), 1.0, "pct"),
    ]


def test_real_config_note() -> None:
    note = load_config(CONFIG_PATH).brief.note
    assert note.enabled
    assert note.model == "claude-sonnet-5-5"
    assert note.api_key_env == "ANTHROPIC_API_KEY"
    assert note.max_sentences == 5
    assert note.timeout_s == 30
    assert note.max_output_tokens == 2000
    assert note.monthly_cap_usd == 5.0
    assert (note.price_input_usd_per_mtok, note.price_output_usd_per_mtok) == (2.0, 10.0)
    for word in ("buy", "sell", "target", "will rise", "will fall", "recommend", "predict"):
        assert word in note.forbidden_words


def test_rule_needs_exactly_one_threshold() -> None:
    ok = RegimeRule.model_validate(
        {"market": "VIX", "direction": "up", "label": "VIX up", "min_sigma": 1.5}
    )
    assert ok.min_change is None
    size = RegimeRule.model_validate(
        {
            "market": "VIX",
            "direction": "up",
            "label": "VIX up",
            "min_change": 2,
            "change_unit": "pts",
        }
    )
    assert (size.min_change, size.change_unit) == (2, "pts")
    for bad in (
        {},  # neither
        {"min_sigma": 1.0, "min_change": 2, "change_unit": "pts"},  # both
        {"min_change": 2},  # size without its unit
        {"min_sigma": 1.0, "change_unit": "pts"},  # unit without a size
        {"min_sigma": 0},  # not positive
        {"min_change": 2, "change_unit": "furlongs"},
    ):
        with pytest.raises(ValidationError):
            RegimeRule.model_validate({"market": "VIX", "direction": "up", "label": "x", **bad})
    with pytest.raises(ValidationError):
        RegimeRule.model_validate(
            {"market": "VIX", "direction": "sideways", "label": "x", "min_sigma": 1}
        )


def test_rule_set_min_met_must_be_reachable() -> None:
    rule = {"market": "VIX", "direction": "up", "label": "VIX up", "min_sigma": 1}
    RegimeRuleSet.model_validate({"label": "Risk-off", "rules": [rule], "min_met": 1})
    for min_met in (0, 2):
        with pytest.raises(ValidationError):
            RegimeRuleSet.model_validate({"label": "Risk-off", "rules": [rule], "min_met": min_met})
    with pytest.raises(ValidationError):
        RegimeRuleSet.model_validate({"label": "Risk-off", "rules": [], "min_met": 1})


def test_brief_labels_unique_and_not_reserved() -> None:
    rule = {"market": "VIX", "direction": "up", "label": "VIX up", "min_sigma": 1}
    one = {"label": "Risk-off", "rules": [rule], "min_met": 1}
    BriefSettings.model_validate({"regime_rules": [one]})
    with pytest.raises(ValidationError, match="duplicate labels"):
        BriefSettings.model_validate({"regime_rules": [one, one]})
    with pytest.raises(ValidationError, match="reserved"):
        BriefSettings.model_validate({"regime_rules": [{**one, "label": "Quiet"}]})
    with pytest.raises(ValidationError, match="must differ"):
        BriefSettings.model_validate({"quiet_label": "Calm", "mixed_label": "Calm"})


def test_related_pair_shape() -> None:
    rp = RelatedPair.model_validate({"pair": ["BRENT", "WTI"], "limit": 1.0})
    assert rp.unit == "pct"
    for bad in (
        {"pair": ["WTI", "WTI"], "limit": 1.0},
        {"pair": ["BRENT", "WTI"], "limit": 0},
        {"pair": ["BRENT"], "limit": 1.0},
        {"pair": ["BRENT", "WTI"], "limit": 1.0, "unit": "furlongs"},
    ):
        with pytest.raises(ValidationError):
            RelatedPair.model_validate(bad)
    with pytest.raises(ValidationError, match="duplicate pair"):
        BriefSettings.model_validate(
            {
                "related_pairs": [
                    {"pair": ["BRENT", "WTI"], "limit": 1},
                    {"pair": ["WTI", "BRENT"], "limit": 2},
                ]
            }
        )


def test_note_defaults_and_bounds() -> None:
    note = NoteSettings()
    assert note.model == "claude-sonnet-5-5"
    assert note.monthly_cap_usd == 5.0
    assert note.forbidden_words == ()
    for bad in (
        {"monthly_cap_usd": 0},
        {"max_sentences": 0},
        {"timeout_s": 0},
        {"api_key_env": "lower"},
        {"forbidden_words": ["Buy"]},
        {"forbidden_words": [" sell"]},
        {"forbidden_words": ["buy", "buy"]},
        {"forbidden_words": [""]},
    ):
        with pytest.raises(ValidationError):
            NoteSettings.model_validate(bad)


def test_brief_cross_references_known_markets() -> None:
    raw = _raw()
    raw["brief"]["regime_rules"][0]["rules"][0]["market"] = "NOPE"
    with pytest.raises(ValidationError, match="unknown market 'NOPE'"):
        Config.model_validate(raw)
    raw = _raw()
    raw["brief"]["related_pairs"][0]["pair"] = ["BRENT", "NOPE"]
    with pytest.raises(ValidationError, match="unknown market 'NOPE'"):
        Config.model_validate(raw)


def test_brief_section_is_optional() -> None:
    raw = _raw()
    del raw["brief"]
    cfg = Config.model_validate(raw)
    assert cfg.brief == BriefSettings()
    assert cfg.brief.regime_rules == ()
    assert cfg.brief.note.model == "claude-sonnet-5-5"


# ---------------------------------------------------------------------------------------------
# events and filings settings (T65)
# ---------------------------------------------------------------------------------------------


def test_events_settings() -> None:
    cfg = load_config(CONFIG_PATH)
    assert cfg.events == EventsSettings()
    assert cfg.events.horizon_days == 7
    assert cfg.events.fred_releases == ()  # T65 lists them after its probe
    rel = FredRelease.model_validate(
        {"release_id": 10, "title": "Consumer Price Index", "time_et": "08:30"}
    )
    assert rel.time_et == "08:30"
    for bad in (
        {"release_id": 0, "title": "x", "time_et": "08:30"},
        {"release_id": "10", "title": "x", "time_et": "08:30"},
        {"release_id": 10, "title": "x", "time_et": "8.30"},
    ):
        with pytest.raises(ValidationError):
            FredRelease.model_validate(bad)
    dup = {"release_id": 10, "title": "x", "time_et": "08:30"}
    with pytest.raises(ValidationError, match="duplicate release_id"):
        EventsSettings.model_validate({"fred_releases": [dup, dup]})
    for bad_horizon in (0, 32):
        with pytest.raises(ValidationError):
            EventsSettings.model_validate({"horizon_days": bad_horizon})


def test_events_fred_releases_need_fred() -> None:
    raw = _raw()
    raw["events"] = {"fred_releases": [{"release_id": 10, "title": "x", "time_et": "08:30"}]}
    Config.model_validate(raw)
    del raw["sources"]["fred"]
    # fred feeds would also complain; the events error must be among the reasons
    with pytest.raises(ValidationError, match=r"events\.fred_releases needs the fred source"):
        Config.model_validate(raw)


def test_filings_settings() -> None:
    cfg = load_config(CONFIG_PATH)
    assert cfg.filings == FilingsSettings()
    assert cfg.filings.insider_days == 90
    company = {"instrument": "AAPL", "cik": EXMP_CIK, "cusip": "EXMP00010"}  # invented
    s = FilingsSettings.model_validate(
        {"companies": [company], "holders": [{"cik": "0007777777", "name": "Invented AM"}]}
    )
    assert s.companies[0].cik == EXMP_CIK
    for bad in (
        {"companies": [{**company, "cik": "123"}]},
        {"companies": [{**company, "cusip": "short"}]},
        {"companies": [company, company]},
        {"holders": [{"cik": "0007777777", "name": "a"}, {"cik": "0007777777", "name": "b"}]},
        {"insider_days": 0},
    ):
        with pytest.raises(ValidationError):
            FilingsSettings.model_validate(bad)


def test_filings_companies_must_be_configured_stocks() -> None:
    raw = _raw()
    raw["filings"] = {"companies": [{"instrument": "AAPL", "cik": EXMP_CIK}]}
    assert Config.model_validate(raw).filings.companies[0].instrument == "AAPL"
    raw["filings"] = {"companies": [{"instrument": "SPY", "cik": EXMP_CIK}]}
    with pytest.raises(ValidationError, match="SPY is a etf, not a stock"):
        Config.model_validate(raw)
    raw["filings"] = {"companies": [{"instrument": "EXMP", "cik": EXMP_CIK}]}
    with pytest.raises(ValidationError, match="unknown instrument 'EXMP'"):
        Config.model_validate(raw)
    raw["filings"] = {"companies": [{"instrument": "AAPL", "cik": EXMP_CIK}]}
    del raw["sources"]["edgar"]
    with pytest.raises(ValidationError, match="filings needs the edgar source"):
        Config.model_validate(raw)


def test_existing_sections_unchanged_by_defaults() -> None:
    """A config written before T64 (no brief/events/filings, no new sources) still loads."""
    raw = _raw()
    for key in ("brief", "events", "filings"):
        raw.pop(key, None)
    for src in ("fiscaldata", "edgar"):
        del raw["sources"][src]
    cfg = Config.model_validate(raw)
    assert cfg.brief == BriefSettings()
    assert cfg.events == EventsSettings()
    assert cfg.filings == FilingsSettings()
