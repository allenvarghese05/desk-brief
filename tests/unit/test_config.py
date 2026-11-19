from pathlib import Path
from typing import Any

import pytest
import yaml

from deskbrief.config import Config, config_hash, load_config, main

ROOT = Path(__file__).resolve().parents[2]
REAL = ROOT / "config.yaml"


def _raw() -> dict[str, Any]:
    data = yaml.safe_load(REAL.read_text())
    assert isinstance(data, dict)
    return data


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_real_config_loads() -> None:
    cfg = load_config(REAL)
    assert len(cfg.instruments) == 42  # T46 added five FRED series, T47 six Cboe indices
    assert {g.id for g in cfg.groups} >= {"sp500", "oil_wti", "ust_10y", "eurusd", "vix"}
    # every group references real instruments (validated) and pairs are what the spec says
    ust10 = next(g for g in cfg.groups if g.id == "ust_10y")
    assert (ust10.proxy, ust10.exact, ust10.relation, ust10.duration) == (
        "IEF",
        "UST_10Y",
        "inverse_duration",
        7.0,
    )


def test_every_proxy_instrument_has_a_label() -> None:
    cfg = load_config(REAL)
    for inst in cfg.instruments:
        if inst.is_proxy:
            assert inst.label, inst.id
            assert "proxy" in inst.label, inst.id


def test_lookups() -> None:
    cfg = load_config(REAL)
    assert cfg.instrument("SPY").unit == "USD"
    with pytest.raises(KeyError):
        cfg.instrument("NOPE")
    fred = cfg.feeds_for("fred")
    assert {i.id for i, _ in fred} >= {"UST_10Y", "WTI", "CPI", "DFF"}
    assert all(f.dataset == "iex_latest" for _, f in cfg.feeds_for("alpaca", "iex_latest"))


def test_config_hash_is_stable_and_sensitive(tmp_path: Path) -> None:
    a = load_config(REAL)
    assert config_hash(a) == config_hash(load_config(REAL))
    data = _raw()
    data["checks"]["anomaly"]["z_warn"] = 2.5
    assert config_hash(load_config(_write(tmp_path, data))) != config_hash(a)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["groups"].append({**d["groups"][0], "id": "x", "proxy": "NOPE"}), "NOPE"),
        (lambda d: d["instruments"][0]["feeds"][0].update(tier=4), "tier"),
        (lambda d: d["instruments"][0]["feeds"][0].update(source="bloomberg"), "bloomberg"),
        (lambda d: d["sources"].update(bloomberg=d["sources"]["fred"]), "unknown sources"),
        (lambda d: d["instruments"].append(d["instruments"][0]), "duplicate instrument"),
        (lambda d: d["report"].update(headline_instruments=["NOPE"]), "headline"),
        (lambda d: d["crosschecks"]["iex_vs_sip"].update(symbols=["WTI"]), "iex_vs_sip"),
        (lambda d: d["crosschecks"]["fx_vs_ecb"].update(pairs=["SPY"]), "fx_vs_ecb"),
        (lambda d: d["datasets"][0].update(expected_by_et="25:00"), "expected_by_et"),
        (lambda d: d["checks"]["validity"].update(pct=[5, 1]), "low < high"),
        (lambda d: d["groups"][0]["tolerance"].update(warn_bps=10, fail_bps=5), "fail_bps"),
        (lambda d: d["instruments"][0].update(surprise=1), "surprise"),
    ],
)
def test_invalid_configs_are_rejected(tmp_path: Path, mutate: Any, message: str) -> None:
    data = _raw()
    mutate(data)
    with pytest.raises(ValueError, match=message):
        load_config(_write(tmp_path, data))


def test_inverse_duration_group_needs_duration() -> None:
    data = _raw()
    g = next(g for g in data["groups"] if g["id"] == "ust_2y")
    del g["duration"]
    with pytest.raises(ValueError, match="duration"):
        Config.model_validate(data)


def test_return_group_needs_two_instruments() -> None:
    data = _raw()
    g = next(g for g in data["groups"] if g["id"] == "sp500")
    g.update(exact="SPY", official="SPY")
    with pytest.raises(ValueError, match="two distinct"):
        Config.model_validate(data)


def test_group_needs_a_member() -> None:
    data = _raw()
    data["groups"].append(
        {
            "id": "empty",
            "label": "x",
            "relation": "level",
            "tolerance": {"warn_bps": 1, "fail_bps": 2},
        }
    )
    with pytest.raises(ValueError, match="at least one"):
        Config.model_validate(data)


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="mapping"):
        load_config(p)


def test_default_path_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESKBRIEF_CONFIG", str(REAL))
    assert len(load_config().instruments) == 42


def test_cli_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(REAL)]) == 0
    assert "config: ok" in capsys.readouterr().out
    bad = _raw()
    bad["version"] = 2
    assert main([str(_write(tmp_path, bad))]) == 1
    assert "INVALID" in capsys.readouterr().err
    assert main([str(tmp_path / "missing.yaml")]) == 1


# --- review round 1 additions ------------------------------------------------------------
@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        # official without tier-3 feed
        (
            lambda d: next(g for g in d["groups"] if g["id"] == "gold").update(official="GOLD_F"),
            "tier-3",
        ),
        # inverse_duration needs a yield
        (
            lambda d: next(g for g in d["groups"] if g["id"] == "ust_10y").update(exact="SPY"),
            "must be a yield",
        ),
        (lambda d: d["crosschecks"]["official_pair"]["pairs"].append(["DGS99", "10 Yr"]), "DGS99"),
        (lambda d: d["crosschecks"]["official_pair"]["pairs"].append(["DGS10", "99 Yr"]), "99 Yr"),
        (lambda d: d["datasets"].append(d["datasets"][0]), "duplicate datasets"),
        (
            lambda d: d["datasets"].append({**d["datasets"][0], "dataset": "nothing"}),
            "no instrument feed",
        ),
        (lambda d: next(i for i in d["instruments"] if i["id"] == "USO").pop("label"), "label"),
        (
            lambda d: next(i for i in d["instruments"] if i["id"] == "EURUSD").update(unit="USD"),
            "fx_rate",
        ),
        (
            lambda d: next(i for i in d["instruments"] if i["id"] == "UST_2Y").update(unit="USD"),
            "unit pct",
        ),
        (
            lambda d: next(i for i in d["instruments"] if i["id"] == "SPY").update(unit="pct"),
            "unit USD",
        ),
        (lambda d: d["checks"]["validity"].pop("fx_rate"), "no range"),
        (lambda d: d["report"].update(headline_instruments=["VIX", "VIX"]), "duplicate"),
        (lambda d: d["instruments"][0]["feeds"][0].update(tier=True), "integer"),
        (lambda d: d["instruments"][0]["feeds"][0].update(tier=1.0), "integer"),
        # same provider symbol used by two instruments
        (
            lambda d: next(i for i in d["instruments"] if i["id"] == "QQQ")["feeds"].append(
                {"source": "alpaca", "dataset": "iex_latest", "symbol": "SPY", "tier": 1}
            ),
            "used by both",
        ),
        (lambda d: d["crosschecks"]["iex_vs_sip"].update(symbols=["AAPL"]), "iex_bars_1m"),
        (
            lambda d: next(i for i in d["instruments"] if i["id"] == "EURUSD").update(
                feeds=[
                    f
                    for f in next(i for i in d["instruments"] if i["id"] == "EURUSD")["feeds"]
                    if f["source"] != "ecb"
                ]
            ),
            "no ecb feed",
        ),
    ],
)
def test_more_invalid_configs(tmp_path: Path, mutate: Any, message: str) -> None:
    data = _raw()
    mutate(data)
    with pytest.raises(ValueError, match=message):
        load_config(_write(tmp_path, data))


def test_duplicate_yaml_keys_rejected(tmp_path: Path) -> None:
    text = REAL.read_text() + "\ninstruments: []\n"
    p = tmp_path / "dup.yaml"
    p.write_text(text)
    with pytest.raises(ValueError, match="duplicate key"):
        load_config(p)


def test_env_override_storage_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_config(REAL).storage.backend == "local"
    monkeypatch.setenv("DESKBRIEF_STORAGE_BACKEND", "r2")
    assert load_config(REAL).storage.backend == "r2"
    monkeypatch.setenv("DESKBRIEF_STORAGE_BACKEND", "s3")
    with pytest.raises(ValueError, match="backend"):
        load_config(REAL)


def test_feed_index_reverse_lookup() -> None:
    cfg = load_config(REAL)
    idx = cfg.feed_index("ecb", "eurofxref")
    assert set(idx) == {"EURUSD", "GBPUSD", "USDJPY"}
    inst, feed = idx["GBPUSD"]
    assert (inst.id, feed.tier) == ("GBPUSD", 3)
    assert cfg.feed_index("treasury", "par_curve")["10 Yr"][0].id == "UST_10Y"


def test_spec_universe_details() -> None:
    cfg = load_config(REAL)
    groups = {g.id: g for g in cfg.groups}
    assert (groups["oil_wti"].proxy, groups["oil_wti"].exact) == ("USO", "WTI")
    assert (groups["ust_2y"].proxy, groups["ust_2y"].official) == ("SHY", "UST_2Y")
    assert (groups["ust_long"].proxy, groups["ust_long"].official) == ("TLT", "UST_20Y")
    assert cfg.instrument("DFF").feeds[0].symbol == "DFF"
    assert {i.id for i in cfg.instruments if i.is_proxy} == {
        "SPY",
        "USO",
        "BNO",
        "GLD",
        "SHY",
        "IEF",
        "TLT",
    }
    # every iex_vs_sip symbol has both IEX and SIP minute bars
    for sym in cfg.crosschecks.iex_vs_sip.symbols:
        datasets = {f.dataset for f in cfg.instrument(sym).feeds}
        assert {"iex_bars_1m", "sip_bars_1m"} <= datasets


# -- T07A: per-dataset intraday schedule ---------------------------------------------------


def test_real_config_intraday_schedule() -> None:
    cfg = load_config(REAL)
    assert {(s.source, s.dataset) for s in cfg.intraday} == {
        ("alpaca", "iex_latest"),
        ("yahoo", "quote"),
        ("twelvedata", "fx_quote"),
    }
    assert all(s.calendar == "XNYS" and s.grace_minutes == 30 for s in cfg.intraday)


def test_intraday_defaults_to_empty_so_old_configs_load(tmp_path: Path) -> None:
    data = _raw()
    del data["intraday"]
    cfg = load_config(_write(tmp_path, data))
    assert cfg.intraday == ()


def test_intraday_entry_defaults(tmp_path: Path) -> None:
    data = _raw()
    data["intraday"] = [{"source": "yahoo", "dataset": "quote"}]
    (entry,) = load_config(_write(tmp_path, data)).intraday
    assert (entry.calendar, entry.grace_minutes) == ("XNYS", 30)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"source": "bloomberg", "dataset": "quote"}, "intraday bloomberg/quote: source not"),
        ({"source": "yahoo", "dataset": "nothing"}, "intraday yahoo/nothing: no instrument"),
        ({"source": "yahoo", "dataset": "quote", "grace_minutes": 0}, "grace_minutes"),
        ({"source": "yahoo", "dataset": "quote", "grace_minutes": -5}, "grace_minutes"),
        ({"source": "yahoo", "dataset": "quote", "calendar": "LSE"}, "calendar"),
        ({"source": "yahoo", "dataset": "quote", "surprise": 1}, "surprise"),
    ],
)
def test_invalid_intraday_entries_rejected(
    tmp_path: Path, entry: dict[str, Any], message: str
) -> None:
    data = _raw()
    data["intraday"] = [entry]
    with pytest.raises(ValueError, match=message):
        load_config(_write(tmp_path, data))


def test_duplicate_intraday_entries_rejected(tmp_path: Path) -> None:
    data = _raw()
    data["intraday"] = [data["intraday"][0], data["intraday"][0]]
    with pytest.raises(ValueError, match="duplicate intraday"):
        load_config(_write(tmp_path, data))
