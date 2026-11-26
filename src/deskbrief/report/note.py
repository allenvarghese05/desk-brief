"""The morning note, every number checked (P-024, card T66; was stretch card T50).

Claude receives only the brief's own model (the read, the movers, Check before using and the
snapshot) and writes four or five plain sentences. The prompt forbids advice, signals, targets
and predictions. The note is written once, by the morning job's report step, and stored in the
brief's ``model.json``; the dashboard shows that stored note and never calls the API.

The check (:func:`check`): every number in the reply is parsed with its unit and matched against
a value the model shows, within that value's display rounding (``1.6%`` matches a shown
``−1.6%``; ``1.7%`` does not). A sentence is dropped, and the reason logged, when it holds a
number that matches nothing, a number whose unit the model does not use for it, or a forbidden
word (``brief.note.forbidden_words``). Market names with digits ("S&P 500", "10Y", "2-year") are
names, not numbers. The kept sentences show "every number checked (n of n)".

No note, and the brief says "No note today" with the reason, when: the note is turned off, no
API key is set (``brief.note.api_key_env``; the key is read from the environment and never
logged), the month's spend would pass ``brief.note.monthly_cap_usd``, the API errors or times
out, the reply is empty or declined, or no sentence survives the check. Spend is the sum of the
``cost_usd`` stored with this month's archived notes (list prices in ``brief.note``); a call is
made only when the worst case (``max_output_tokens`` at the output price) still fits the cap.

Hosted mode (the public dashboard shows the stored note): the prompt never holds a value of a
tier the hosted dashboard hides (tier 1 always, plus ``dashboard.hosted_hidden_tiers``), so the
note cannot carry one either.
"""
# ruff: noqa: RUF001, RUF002  (the sigma and minus signs are intentional output)

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Protocol, cast

from deskbrief.config import Config, NoteSettings
from deskbrief.logging import get_logger
from deskbrief.report.model import BriefModel, Note
from deskbrief.store.backend import REPORTS_PREFIX, StorageBackend
from deskbrief.timeutil import to_et

#: tiers the hosted dashboard never shows, whatever config says (mirrors app/components/data.py)
ALWAYS_HIDDEN = frozenset({1})

#: a number in text: optional sign, optional $, digits (grouped or not), decimals, then a unit.
#: A number touching a letter or digit on either side ("10Y", "2s10s", "N225") is a name.
_NUMBER = re.compile(
    r"(?<![\w.,])(?P<sign>[+\-−])?(?P<usd>\$)?(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d+))?"
    r"(?:\s?(?P<unit>%|bp\b|basis points?\b|pts\b|points?\b|σ|sigma\b|contracts\b))?"
    r"(?![\w%])",
    re.IGNORECASE,
)
_UNITS = {
    "%": "pct", "bp": "bp", "basis point": "bp", "basis points": "bp", "pts": "pts",
    "point": "pts", "points": "pts", "σ": "sigma", "sigma": "sigma", "contracts": "contracts",
}  # fmt: skip
#: names with digits in them, never numbers
_NAMES = re.compile(
    r"S&P\s?500|Nasdaq[- ]100|Russell\s2000|Nikkei\s225|FTSE\s100|DAX\s40|"
    r"\b\d+[- ](?:year|month|week|day)s?\b|\b\d+s\d+s\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9$+\-−\"'(])")


@dataclass(frozen=True)
class Shown:
    """A number the model shows: its absolute value, unit and display decimals."""

    value: float
    unit: str  # pct | bp | pts | sigma | usd | contracts | "" (a bare level or rate)
    decimals: int


def numbers(text: str) -> list[Shown]:
    """Every number in ``text`` with its unit, names with digits left out."""
    out = []
    for m in _NUMBER.finditer(_NAMES.sub(" ", text)):
        dec = m["dec"] or ""
        value = float(m["int"].replace(",", "") + (f".{dec}" if dec else ""))
        unit = _UNITS.get((m["unit"] or "").lower(), "") or ("usd" if m["usd"] else "")
        out.append(Shown(value, unit, len(dec)))
    return out


def _matches(n: Shown, shown: Iterable[Shown]) -> bool:
    """``n`` equals a shown number of the same unit within that number's display rounding."""
    for s in shown:
        if s.unit != n.unit:
            continue
        if abs(n.value - s.value) <= 0.5 * 10.0 ** (-s.decimals) + 1e-9:
            return True
    return False


def _forbidden(sentence: str, words: Collection[str]) -> str | None:
    low = sentence.lower()
    for w in words:
        if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", low):
            return w
    return None


@dataclass(frozen=True)
class Checked:
    """What survived the check, and why each dropped sentence went."""

    kept: tuple[str, ...]
    n_numbers: int  # numbers in the kept sentences (all matched)
    dropped: tuple[str, ...]


def sentences(text: str) -> list[str]:
    out = []
    for line in text.replace("\r", "").split("\n"):
        line = line.strip().lstrip("-•* ").strip()
        out += [s.strip() for s in _SENTENCE.split(line) if s.strip()]
    return out


def check(reply: str, shown: Collection[Shown], forbidden: Collection[str], limit: int) -> Checked:
    """Keep the sentences whose every number matches a shown number with its unit and that hold
    no forbidden word; at most ``limit`` of them."""
    kept: list[str] = []
    dropped: list[str] = []
    total = 0
    for s in sentences(reply):
        word = _forbidden(s, forbidden)
        if word is not None:
            dropped.append(f"forbidden word {word!r}")
            continue
        found = numbers(s)
        bad = [n for n in found if not _matches(n, shown)]
        if bad:
            n = bad[0]
            what = f"{n.value:g}{'' if not n.unit else ' ' + n.unit}"
            dropped.append(f"number {what} matches no value in the brief")
            continue
        if len(kept) >= limit:
            dropped.append("over the sentence limit")
            continue
        kept.append(s)
        total += len(found)
    return Checked(tuple(kept), total, tuple(dropped))


# ---------------------------------------------------------------------------
# the facts Claude receives, and the numbers they show
# ---------------------------------------------------------------------------


def hidden_tiers(config: Config) -> frozenset[int]:
    """Tiers the hosted dashboard hides (where the stored note is shown)."""
    return ALWAYS_HIDDEN | frozenset(config.dashboard.hosted_hidden_tiers)


def facts(model: BriefModel, hidden: Collection[int]) -> dict[str, Any]:
    """The brief's model as the note's only input: no value of a hidden tier, no lineage."""
    tiers = {r.instrument_id: r.tier for r in model.rows()}
    visible = {i for i, t in tiers.items() if t is not None and t not in hidden}
    read = model.read
    return {
        "brief": to_et(model.report_time).strftime("%a %d %b %Y, %H:%M ET"),
        "read": read.text if read is not None else "",
        "rules_met": [r.label for r in read.met] if read is not None else [],
        "moved_more_than_usual": [
            {
                "market": m.short,
                "name": m.name,
                "value": m.value_text,
                "change": m.change_text,
                "sigma": f"{m.sigma:.1f}σ",
                "basis": m.basis,
            }
            for m in model.movers
            if m.instrument_id in visible
        ],
        "check_before_using": [
            {"item": c.title, "text": c.text}
            for c in model.caveats
            if all(i in visible for i in c.instrument_ids)
        ],
        "snapshot": [
            {"market": r.label, "value": r.value_text, "change": r.change_text, "note": r.meta}
            for g in model.snapshot
            for r in g.rows
            if r.instrument_id in visible
        ],
    }


def shown_numbers(payload: Mapping[str, Any]) -> list[Shown]:
    """Every number the facts show (a note may use these and nothing else)."""
    out: list[Shown] = []

    def walk(x: object) -> None:
        if isinstance(x, str):
            out.extend(numbers(x))
        elif isinstance(x, Mapping):
            for v in x.values():
                walk(v)
        elif isinstance(x, list | tuple):
            for v in x:
                walk(v)

    walk(payload)
    return out


def prompt(settings: NoteSettings) -> str:
    words = ", ".join(f'"{w}"' for w in settings.forbidden_words)
    n = settings.max_sentences
    return (
        "You write the morning note for Desk Brief, a markets data brief read by a hedge-fund "
        f"desk. Write {max(1, n - 1)} or {n} short sentences in plain English about what moved "
        "overnight and what to check, using only the facts in the user message.\n"
        "Rules:\n"
        "- Every number you write must appear in the facts exactly as shown there, with the "
        "same unit (%, bp, pts, σ, $). Write no other number: no dates, times, years or counts.\n"
        "- Describe what happened. Give no advice, signals, recommendations, price targets, "
        "forecasts or predictions, and never say what a market will or should do.\n"
        f"- Do not use these words: {words}.\n"
        "- One sentence per line. No headings, lists, markdown or preamble."
    )


# ---------------------------------------------------------------------------
# spend and the call
# ---------------------------------------------------------------------------


class _Messages(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class ClientLike(Protocol):
    """What the note needs of ``anthropic.Anthropic`` (tests pass a fake)."""

    @property
    def messages(self) -> _Messages: ...


ClientFactory = Callable[[str, float], ClientLike]


def _sdk_client(api_key: str, timeout_s: float) -> ClientLike:
    import anthropic

    # one attempt: a retry after a timeout could double the spend and the wait
    return cast(ClientLike, anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=0))


def month_spend(store: StorageBackend, report_date: date) -> float:
    """USD spent on notes this calendar month: the ``cost_usd`` stored in each archived brief
    (every version) dated in ``report_date``'s month. Unreadable files count as zero."""
    prefix = f"{REPORTS_PREFIX}/date={report_date:%Y-%m}-"
    total = 0.0
    for key in store.list(prefix):
        if not key.endswith("/model.json"):
            continue
        try:
            note = json.loads(store.get_bytes(key)).get("note") or {}
            total += float(note.get("cost_usd") or 0.0)
        except (ValueError, TypeError, AttributeError, OSError):
            continue
    return total


def _cost(settings: NoteSettings, input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * settings.price_input_usd_per_mtok
        + output_tokens * settings.price_output_usd_per_mtok
    ) / 1_000_000


def _reply_text(response: Any) -> str:
    blocks = getattr(response, "content", None) or ()
    return "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text")


def _usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    return int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)


def write_note(
    model: BriefModel,
    config: Config,
    env: Mapping[str, str],
    *,
    spent_usd: float = 0.0,
    client_factory: ClientFactory | None = None,
) -> Note:
    """The note for ``model``: one API call at most, never raising. ``spent_usd`` is this
    month's spend so far (:func:`month_spend`)."""
    s = config.brief.note
    log = get_logger()
    if not s.enabled:
        return Note(reason="the note is turned off in config", model=s.model)
    key = (env.get(s.api_key_env) or "").strip()
    if not key:
        return Note(reason="no API key is configured", model=s.model)
    payload = facts(model, hidden_tiers(config))
    system, user = prompt(s), "Facts (JSON):\n" + json.dumps(payload, ensure_ascii=False)
    worst = _cost(s, (len(system) + len(user)) // 3 + 200, s.max_output_tokens)
    if spent_usd + worst > s.monthly_cap_usd:
        return Note(
            reason=f"the monthly spend cap of ${s.monthly_cap_usd:.2f} is reached", model=s.model
        )
    try:
        client = (client_factory or _sdk_client)(key, s.timeout_s)
        response = client.messages.create(
            model=s.model,
            max_tokens=s.max_output_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={"effort": s.effort},
        )
    except Exception as exc:  # never let the note cost the brief; the key is never logged
        kind = type(exc).__name__
        timed_out = isinstance(exc, TimeoutError) or "Timeout" in kind
        log.warning("note.failed", error=kind)
        why = "the API timed out" if timed_out else f"the API returned an error ({kind})"
        return Note(reason=why, model=s.model)
    cost = _cost(s, *_usage(response))
    if getattr(response, "stop_reason", "") == "refusal":
        return Note(reason="the model declined to write it", model=s.model, cost_usd=cost)
    text = _reply_text(response).strip()
    if not text:
        return Note(reason="the reply was empty", model=s.model, cost_usd=cost)
    result = check(text, shown_numbers(payload), s.forbidden_words, s.max_sentences)
    for why in result.dropped:
        log.info("note.sentence_dropped", reason=why)
    if not result.kept:
        return Note(
            reason="no sentence passed the number check",
            dropped=result.dropped,
            model=s.model,
            cost_usd=cost,
        )
    return Note(
        sentences=result.kept,
        n_checked=result.n_numbers,
        n_numbers=result.n_numbers,
        dropped=result.dropped,
        model=s.model,
        cost_usd=cost,
    )


def with_note(
    model: BriefModel,
    config: Config,
    env: Mapping[str, str],
    store: StorageBackend,
    *,
    client_factory: ClientFactory | None = None,
) -> BriefModel:
    """``model`` with its note (the morning report step calls this once)."""
    try:
        spent = month_spend(store, model.report_date)
    except Exception as exc:  # cannot tell the spend: do not call
        get_logger().warning("note.spend_unknown", error=type(exc).__name__)
        return replace(model, note=Note(reason="this month's spend could not be read"))
    note = write_note(model, config, env, spent_usd=spent, client_factory=client_factory)
    return replace(model, note=note)


def stored_note(store: StorageBackend, report_date: date) -> Note | None:
    """The note stored with the newest archived brief of ``report_date`` (the dashboard reads
    this and never calls the API); None when no brief of that date is archived or it holds no
    note."""
    prefix = f"{REPORTS_PREFIX}/date={report_date.isoformat()}/"
    versions = []
    for key in store.list(prefix):
        m = re.match(r"v(\d+)/model\.json$", key[len(prefix) :])
        if m:
            versions.append((int(m[1]), key))
    if not versions:
        return None
    raw = json.loads(store.get_bytes(max(versions)[1])).get("note")
    if not isinstance(raw, Mapping):
        return None
    return Note(
        sentences=tuple(str(x) for x in raw.get("sentences") or ()),
        n_checked=int(raw.get("n_checked") or 0),
        n_numbers=int(raw.get("n_numbers") or 0),
        reason=raw.get("reason"),
        model=str(raw.get("model") or ""),
        cost_usd=float(raw.get("cost_usd") or 0.0),
    )


__all__ = [
    "Checked",
    "Shown",
    "check",
    "facts",
    "month_spend",
    "numbers",
    "shown_numbers",
    "stored_note",
    "with_note",
    "write_note",
]
