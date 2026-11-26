"""Helpers for the T65 source tests (importable as ``fakes_t65``).

SYNTHETIC FIXTURE: every document served here is an invented file under
``tests/fixtures/synthetic/{edgar,fiscaldata,fred_releases}``; no real provider data.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from config_support import enable_all_sources
from deskbrief.config import Config, FilingsCompany, FilingsHolder, FredRelease, load_config
from deskbrief.ids import new_id
from deskbrief.models import FetchRequest, JobKind, RawResponse, RunContext, Trigger

ROOT = Path(__file__).resolve().parents[3]
FIX = ROOT / "tests" / "fixtures" / "synthetic"
EDGAR = FIX / "edgar"
EXMP_CIK = "0009999999"  # invented issuer "Example Corp (EXMP)"
HOLDER_CIK = "0007777777"  # invented filer "Invented Asset Management"
EXMP_CUSIP = "EXMP00010"
#: The morning run of Thu 2026-11-26 (06:20 ET).
NOW = datetime(2026, 11, 26, 11, 20, tzinfo=UTC)
TARGET = date(2026, 11, 26)
#: A low-entropy, obviously fake contact User-Agent, built at runtime.
FAKE_UA = " ".join(["Invented", "Tester", "tester@example.invalid"])


def t65_config(
    *,
    fred_releases: bool = True,
    companies: bool = True,
    holders: bool = True,
    base: Config | None = None,
) -> Config:
    """The real config with every source on, two FRED releases, EXMP mapped onto ``AAPL``
    (an instrument id only; the data is invented) and one invented 13F holder."""
    cfg = enable_all_sources(base or load_config(ROOT / "config.yaml"))
    events = cfg.events.model_copy(
        update={
            "fred_releases": (
                (
                    FredRelease(release_id=10, title="Consumer Price Index", time_et="08:30"),
                    FredRelease(release_id=50, title="Employment Situation", time_et="08:30"),
                )
                if fred_releases
                else ()
            )
        }
    )
    filings = cfg.filings.model_copy(
        update={
            "companies": (
                (FilingsCompany(instrument="AAPL", cik=EXMP_CIK, cusip=EXMP_CUSIP),)
                if companies
                else ()
            ),
            "holders": (
                (FilingsHolder(cik=HOLDER_CIK, name="Invented Asset Management"),)
                if holders
                else ()
            ),
        }
    )
    return cfg.model_copy(update={"events": events, "filings": filings})


def ctx_for(config: Config, job: JobKind = JobKind.MORNING, now: datetime = NOW) -> RunContext:
    return RunContext(
        run_id="RUN-T65",
        job=job,
        trigger=Trigger.MANUAL,
        target_date=TARGET,
        now_utc=now,
        git_sha="test",
        config_hash="test",
        config=config,
        store=None,  # type: ignore[arg-type]  # plan() never touches the store
    )


Route = Callable[[str], tuple[int, bytes]]


class RoutedHttp:
    """An ``HttpClient`` that answers from ``route(url) -> (status, body)`` and records calls."""

    def __init__(self, route: Route, at: datetime = NOW) -> None:
        self.route = route
        self.at = at
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        req: FetchRequest,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        secret_params: Mapping[str, str] | None = None,
        secret_headers: Mapping[str, str] | None = None,
        units: int = 1,
    ) -> RawResponse:
        self.calls.append(
            {
                "url": url,
                "params": dict(params or {}),
                "secret_params": dict(secret_params or {}),
                "secret_headers": dict(secret_headers or {}),
            }
        )
        status, body = self.route(url)
        return RawResponse(
            request=req,
            fetch_id=new_id(),
            requested_at=self.at,
            responded_at=self.at,
            http_status=status,
            content_type="application/json",
            body=body,
            error=None if 200 <= status < 300 else f"HTTP {status}",
            url_redacted=url,
        )


def edgar_route(*, amendment: str = "restatement", fail: str | None = None) -> Route:
    """Serve the invented EDGAR fixtures. ``amendment`` picks the 13F-HR/A variant
    (``restatement`` or ``new_holdings``); ``fail`` makes URLs containing it answer 500."""

    def route(url: str) -> tuple[int, bytes]:
        if fail and fail in url:
            return 500, b"invented upstream error"
        name = url.rsplit("/", 1)[-1]
        if "submissions/CIK" in url:
            cik = name.removeprefix("CIK").removesuffix(".json")
            return 200, (EDGAR / f"submissions_{cik}.json").read_bytes()
        if name == "index.json":
            return 200, (EDGAR / "index.json").read_bytes()
        if name == "infotable.xml":
            nodash = url.rsplit("/", 2)[-2]
            suffix = (
                "_new_holdings" if nodash.endswith("000004") and amendment != "restatement" else ""
            )
            return 200, (EDGAR / f"infotable_{nodash}{suffix}.xml").read_bytes()
        if name == "primary_doc.xml":
            return 200, (EDGAR / f"cover_{amendment}.xml").read_bytes()
        path = EDGAR / name
        if path.exists():
            return 200, path.read_bytes()
        return 404, b"not found"

    return route
