"""Form 4/A matching across runs (PR #102 review rounds 2-3, card T65): a document missing this
run never changes what the reader counts, and an ambiguous amendment is never counted twice.

SYNTHETIC FIXTURE: every filing below is invented (the fictional Example Corp, CIK 0009999999,
invented owners "Sample John" and "Sample Ann", invented accessions and share counts), built on
the T65 fixtures in ``tests/fixtures/synthetic/edgar/``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import test_runner_t65 as T  # noqa: N812  (the T65 runner helpers)

from deskbrief.models import FetchRequest
from deskbrief.query import company_filings_from_frame
from deskbrief.sources.edgar import EdgarSource
from deskbrief.store.datasets import Datasets

FIX = T.EDGAR_FIX
BASE_SUBS = json.loads((FIX / "submissions_0009999999.json").read_text())
BUY = (FIX / "form4_buy.xml").read_text()
A, B = "0009999999-26-000009", "0009999999-26-000008"  # original (10 Nov) and its 4/A (12 Nov)
Answer = Callable[[FetchRequest, str], tuple[int, bytes]]


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", T.KEY)
    monkeypatch.setenv("DESKBRIEF_SEC_USER_AGENT", T.FAKE_UA)


def f4(
    *,
    shares: int,
    period: str = "2026-11-09",
    owner_cik: str = "0008888889",
    owner: str = "Sample John",
    original: str | None = None,
    amendment: bool = False,
) -> bytes:
    """An invented Form 4 (or 4/A) from the fixture, with its share count, period and owner."""
    t = BUY.replace("<transactionShares><value>200</value>",
                    f"<transactionShares><value>{shares}</value>")  # fmt: skip
    t = t.replace("<periodOfReport>2026-11-09</periodOfReport>",
                  f"<periodOfReport>{period}</periodOfReport>")  # fmt: skip
    t = t.replace("<transactionDate><value>2026-11-09</value>",
                  f"<transactionDate><value>{period}</value>")  # fmt: skip
    t = t.replace("0008888889", owner_cik).replace("Sample John", owner)
    if amendment:
        t = t.replace("<documentType>4</documentType>", "<documentType>4/A</documentType>")
        if original:
            t = t.replace("<aff10b5One>", f"<dateOfOriginalSubmission>{original}"
                          "</dateOfOriginalSubmission>\n  <aff10b5One>", 1)  # fmt: skip
    return t.encode()


def subs_with(extra: list[tuple[str, str, str, str, str]]) -> bytes:
    d = copy.deepcopy(BASE_SUBS)
    r = d["filings"]["recent"]
    for acc, filed, report, form, doc in extra:
        r["accessionNumber"].insert(0, acc)
        r["filingDate"].insert(0, filed)
        r["reportDate"].insert(0, report)
        r["form"].insert(0, form)
        r["acceptanceDateTime"].insert(0, filed + "T16:01:00.000Z")
        r["primaryDocument"].insert(0, "xslF345X05/" + doc)
        r["primaryDocDescription"].insert(0, form)
    return json.dumps(d).encode()


def answer(extra: Any = (), docs: Any = None, not_found: Any = ()) -> Answer:
    base = T.edgar_answer()
    subs = subs_with(list(extra))
    docs = docs or {}

    def a(req: FetchRequest, url: str) -> tuple[int, bytes]:
        name = url.rsplit("/", 1)[-1]
        if name in not_found:
            return 404, b"invented not found"
        if "submissions/CIK0009999999" in url:
            return 200, subs
        if name in docs:
            return 200, docs[name]
        return base(req, url)

    return a


def view(env: Any, day: int) -> tuple[list[str], int, float, int]:
    f = Datasets(env.store).read("filings")
    cf = company_filings_from_frame(f, "AAPL", T.THU + timedelta(days=day, hours=1), 90)
    ins = cf.insiders
    trades = sorted({t.accession for t in ins.trades})
    return trades, ins.n_buys, round(ins.buys_usd, 2), ins.n_ambiguous


def runs(tmp_path: Path, answers: list[Answer]) -> list[tuple[list[str], int, float, int]]:
    cfg = T._config(filings=True)
    env = T._env(tmp_path, cfg, [EdgarSource(cfg)], answers[0], T.THU)
    out = []
    for day, a in enumerate(answers):
        T._run(env, a, T.THU + timedelta(days=day))
        out.append(view(env, day))
    return out


# the original filed the day after the date its amendment names (accepted after the cut-off)
A4, B4 = "0009999999-26-000013", "0009999999-26-000014"
OFFDATE = {
    "extra": [(A4, "2026-11-14", "2026-11-13", "4", "form4_ann.xml"),
              (B4, "2026-11-16", "2026-11-13", "4/A", "form4a_ann.xml")],
    "docs": {"form4_ann.xml": f4(shares=100, period="2026-11-13", owner_cik="0008888890",
                                 owner="Sample Ann"),
             "form4a_ann.xml": f4(shares=120, period="2026-11-13", owner_cik="0008888890",
                                  owner="Sample Ann", original="2026-11-13", amendment=True)},
}  # fmt: skip

# two same-day Form 4s of one owner: the 4/A cannot tell which it restates
C6 = "0009999999-26-000016"
AMBIG = {
    "extra": [(C6, "2026-11-10", "2026-11-09", "4", "form4_buy2.xml")],
    "docs": {"form4_buy2.xml": f4(shares=300)},
}
B2 = "0009999999-26-000012"  # a second 4/A of the same owner, same original date
SECOND = {
    "extra": [(B2, "2026-11-13", "2026-11-09", "4/A", "form4a2_buy.xml")],
    "docs": {"form4a2_buy.xml": f4(shares=260, original="2026-11-10", amendment=True)},
}


def test_an_original_matched_by_its_period_that_404s_is_never_counted_again(
    tmp_path: Path,
) -> None:
    """Review B4: the amendment matched its original by period (filed a day later than the
    date it names); when that original 404s, the amendment is skipped too."""
    first, second = runs(
        tmp_path, [answer(**OFFDATE), answer(**OFFDATE, not_found={"form4_ann.xml"})]
    )
    assert A4 not in first[0]
    assert B4 in first[0]
    assert second == first


@pytest.mark.parametrize("gone", ["form4_buy2.xml", "form4_buy.xml"])
def test_an_ambiguous_amendment_stays_ambiguous_when_one_candidate_404s(
    tmp_path: Path, gone: str
) -> None:
    """Review B5: a missing candidate must not turn an ambiguous match into a sure one."""
    first, second = runs(tmp_path, [answer(**AMBIG), answer(**AMBIG, not_found={gone})])
    assert B in first[0]
    assert A not in first[0]
    assert C6 not in first[0]
    assert first[3] == 1  # flagged
    assert second == first


def test_two_ambiguous_amendments_of_one_owner_are_never_both_counted(tmp_path: Path) -> None:
    """Review B6: the latest of the two counts, the flag counts both."""
    both = {"extra": AMBIG["extra"] + SECOND["extra"], "docs": {**AMBIG["docs"], **SECOND["docs"]}}
    ((trades, _, _, n_ambiguous),) = runs(tmp_path, [answer(**both)])
    assert not {B, B2} <= set(trades)
    assert B2 in trades  # the later amendment
    assert n_ambiguous == 2


# -- round 4 (B7, N2, N1) ----------------------------------------------------------------------

# two 4/As by John restating two different originals filed long before the fetch window: both
# carry no candidates, and both must be counted (review round 4, B7)
O1, O2 = "0009999999-26-000021", "0009999999-26-000022"
OLD_TWO = {
    "extra": [(O1, "2026-11-20", "2026-06-01", "4/A", "form4a_old1.xml"),
              (O2, "2026-11-20", "2026-06-12", "4/A", "form4a_old2.xml")],
    "docs": {"form4a_old1.xml": f4(shares=11, period="2026-06-01", original="2026-06-02",
                                   amendment=True),
             "form4a_old2.xml": f4(shares=22, period="2026-06-12", original="2026-06-15",
                                   amendment=True)},
}  # fmt: skip


def _merge(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"extra": [e for p in parts for e in p.get("extra", [])],
            "docs": {k: v for p in parts for k, v in p.get("docs", {}).items()}}  # fmt: skip


def test_amendments_of_two_different_old_originals_are_both_counted(tmp_path: Path) -> None:
    ((trades, _, _, n_ambiguous),) = runs(tmp_path, [answer(**OLD_TWO)])
    assert {O1, O2} <= set(trades)
    assert n_ambiguous == 0  # nothing left out


def test_two_amendments_of_one_old_original_count_once_and_say_check(tmp_path: Path) -> None:
    """Same owner, same named original date and period, no candidates: they restate one
    original, so the later counts and the earlier is in the flag."""
    same = {
        "extra": [(O1, "2026-11-19", "2026-06-01", "4/A", "form4a_old1.xml"),
                  (O2, "2026-11-20", "2026-06-01", "4/A", "form4a_old2.xml")],
        "docs": {"form4a_old1.xml": f4(shares=11, period="2026-06-01", original="2026-06-02",
                                       amendment=True),
                 "form4a_old2.xml": f4(shares=12, period="2026-06-01", original="2026-06-02",
                                       amendment=True)},
    }  # fmt: skip
    ((trades, _, _, n_ambiguous),) = runs(tmp_path, [answer(**same)])
    assert O2 in trades
    assert O1 not in trades
    assert n_ambiguous == 1


def test_amendments_with_overlapping_candidates_are_never_both_counted(tmp_path: Path) -> None:
    """Review round 4, N2: a third Form 4 of John's (E) filed between his two 4/As gives them
    different candidate sets ({A, C6} and {A, C6, E}); both name the same original date and
    period, so only the later counts and both are in the flag."""
    e = "0009999999-26-000019"
    between = {"extra": [(e, "2026-11-12", "2026-11-09", "4", "form4_buy3.xml")],
               "docs": {"form4_buy3.xml": f4(shares=400)}}  # fmt: skip
    both = _merge(AMBIG, SECOND, between)
    subs = json.loads(subs_with(both["extra"]))
    recent = subs["filings"]["recent"]
    recent["filingDate"][recent["accessionNumber"].index(B)] = "2026-11-11"
    body = json.dumps(subs).encode()
    base = answer(**both)

    def a(req: FetchRequest, url: str) -> tuple[int, bytes]:
        return (200, body) if "submissions/CIK0009999999" in url else base(req, url)

    ((trades, _, _, n_ambiguous),) = runs(tmp_path, [a])
    assert not {B, B2} <= set(trades)
    assert B2 in trades
    assert n_ambiguous == 2


ANN_SAME_DAY = {
    "extra": [("0009999999-26-000017", "2026-11-10", "2026-11-09", "4", "form4_ann10.xml")],
    "docs": {"form4_ann10.xml": f4(shares=70, owner_cik="0008888890", owner="Sample Ann")},
}


def test_a_held_back_amendment_says_check_and_leaves_a_run_note(tmp_path: Path) -> None:
    """Review round 4, N1: a same-day Form 4 that 404s on every run holds John's readable 4/A
    back. The Company line says "check" and the run's notes name the amendment; once the
    document reads again, both clear."""
    cfg = T._config(filings=True)
    gone = answer(**ANN_SAME_DAY, not_found={"form4_ann10.xml"})
    env = T._env(tmp_path, cfg, [EdgarSource(cfg)], gone, T.THU)
    held_notes = []
    for day, a in enumerate([gone, gone, answer(**ANN_SAME_DAY)]):
        _, manifest = T._run(env, a, T.THU + timedelta(days=day))
        held_notes.append([n for n in manifest.notes if "held back" in n])
        f = Datasets(env.store).read("filings")
        cf = company_filings_from_frame(f, "AAPL", T.THU + timedelta(days=day, hours=1), 90)
        assert cf.insiders.n_held_back == (1 if day < 2 else 0), day
    assert held_notes[0] == [
        f"edgar: AAPL: 1 Form 4/A held back (a possible original was not readable): {B}"
    ]
    assert held_notes[2] == []


def test_the_company_line_names_held_back_and_grouped_out_amendments() -> None:
    from app.components.company import _amendment_notes

    text = "".join(_amendment_notes(1, 2))
    assert "1 amendment could not be matched to one original or restate one" in text
    assert "2 amendments held back because a possible original could not be read" in text
    assert _amendment_notes(0, 0) == []
