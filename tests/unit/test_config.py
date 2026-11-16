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
    assert len(cfg.instruments) == 31
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
    assert len(load_config().instruments) == 31


def test_cli_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(REAL)]) == 0
    assert "config: ok" in capsys.readouterr().out
    bad = _raw()
    bad["version"] = 2
    assert main([str(_write(tmp_path, bad))]) == 1
    assert "INVALID" in capsys.readouterr().err
    assert main([str(tmp_path / "missing.yaml")]) == 1
