"""The morning note, templated (P-024, card T66; was stretch card T50; PR #102 round 4).

Claude writes no prose. It receives the brief's own facts (the read, the rules met, Check before
using and each visible market's display strings) and returns structured output only, held to a
JSON schema: an ordered list of up to ``MAX_ITEMS`` picks ``{market_id, show}`` (``show`` is
``move``, ``move_and_level`` or ``move_and_sigma``) and up to ``MAX_CONTRASTS`` contrasts
``{a, b}``. Code then:

* validates every pick, dropping (and logging, never echoing) an unknown market, a market not in
  today's snapshot or movers, a market with no change to show, sigma asked of a move that is not
  ranked, a repeat, and a contrast that is neither in ``brief.related_pairs`` nor a pair named
  in Check before using;
* renders each sentence from fixed templates with the brief's exact display strings: the
  brief's display name, the change without its sign after a verb chosen from the sign and the
  market's kind (prices, yields and the rest rose/fell; credit spreads widened/narrowed; the
  2s10s curve steepened/flattened; a zero change "was unchanged"), the level and the sigma.

No text of the reply reaches the output: a reply string is only ever looked up as an id, and an
id is shown as the brief's display name. "Every number matches the stored data (n of n)" counts
the figures rendered, says how many belong to a market marked "check" (named in Check before
using; such a market's name carries "(check)", as in the movers and the snapshot), and is hidden
when there are none. The note's first line stays the read
(the regime rules in code).

No note, and the brief says "No note today" with the reason, when: the note is turned off, no
API key is set (``brief.note.api_key_env``; the key is read from the environment and never
logged), the month's spend would pass ``brief.note.monthly_cap_usd``, the API errors or times
out, the reply is declined, cut at the token limit or not the structured output asked for, or no
pick survives validation. Each call's cost is written to a spend ledger
(``reports/note_spend/month=YYYY-MM/``, by the call's ET month) the moment it returns; a timeout
counts the worst case. A call is made only when the worst case (``max_output_tokens`` at the
output price) still fits the cap. ``brief.note.forbidden_words`` stays in config but no longer
affects the output: the model writes no words.

Hosted mode (the public dashboard shows the stored note): the facts never hold a value of a tier
the hosted dashboard hides (tier 1 always, plus ``dashboard.hosted_hidden_tiers``), and a market
of such a tier cannot be picked.
"""
# ruff: noqa: RUF001, RUF003  (the sigma and minus signs are intentional output)

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Protocol, cast

from deskbrief.config import Config, NoteSettings
from deskbrief.ids import new_id
from deskbrief.logging import get_logger
from deskbrief.report.model import NOTE_NO_KEY, NOTE_OFF, SPREAD_ID, BriefModel, Note
from deskbrief.store.backend import REPORTS_PREFIX, StorageBackend
from deskbrief.timeutil import ensure_utc, to_et, utc_now

#: tiers the hosted dashboard never shows, whatever config says (mirrors app/components/data.py)
ALWAYS_HIDDEN = frozenset({1})

#: the most picks and contrasts a reply may hold; more are dropped
MAX_ITEMS = 5
MAX_CONTRASTS = 2
SHOWS = ("move", "move_and_level", "move_and_sigma")
_MINUS = "−"


# ---------------------------------------------------------------------------
# what may be picked
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Market:
    """One market the note may describe, with the brief's own display strings."""

    instrument_id: str
    name: str  # the brief's display name ("E-mini S&P 500 futures")
    kind: str  # price | spread | curve: decides the verb
    change: str  # the brief's signed change ("−1.6%", "+27bp"); "" when there is none
    level: str  # the brief's value ("6,012.25", "4.15%")
    sigma: str | None  # "3.6σ" for a mover (ranked, at or above the warn level); else None
    check: bool = False  # named in Check before using (the movers' and snapshot's "check")

    @property
    def sign(self) -> int:
        """+1, -1, or 0 for a change that displays as zero."""
        if not any(c.isdigit() and c != "0" for c in self.change):
            return 0
        return -1 if self.change.startswith((_MINUS, "-")) else 1

    @property
    def label(self) -> str:
        """The name as the note prints it, marked like the movers and the snapshot."""
        return f"{self.name} (check)" if self.check else self.name

    @property
    def magnitude(self) -> str:
        """The change without its sign ("1.6%"), as shown after a verb."""
        return self.change.lstrip("+-" + _MINUS)


#: the desk's short names for the note's prose (PR #102 round 4, email N3), as the mockup reads
#: ("S&P futures", "high-yield spread", "10-year"); a sentence starts with a capital. Anything
#: not listed uses the brief's full name without its parenthetical.
NOTE_NAMES = {
    "ES_F": "S&P futures",
    "WTI": "WTI crude",
    "BRENT": "Brent crude",
    "GOLD_F": "gold futures",
    "UST_3M": "3-month",
    "UST_2Y": "2-year",
    "UST_10Y": "10-year",
    "UST_20Y": "20-year",
    "UST_30Y": "30-year",
    "UST_10Y_REAL": "10-year real yield",
    "BEI_10Y": "10-year breakeven",
    "UST_2S10S": "2s10s curve",
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "USD_BROAD": "broad dollar",
    "VIX": "VIX",
    "N225": "Nikkei",
    "FTSE": "FTSE 100",
    "DAX": "DAX",
    "HY_OAS": "high-yield spread",
    "DFF": "fed funds rate",
    "UNRATE": "unemployment rate",
    "CPI": "CPI",
}


def _display_name(row_name: str) -> str:
    """A market's name for prose: the brief's full name without a parenthetical."""
    return re.sub(r"\s*\([^)]*\)", "", row_name).strip()


def note_name(instrument_id: str, row_name: str) -> str:
    """The desk's short name of a market (:data:`NOTE_NAMES`), else its display name."""
    return NOTE_NAMES.get(instrument_id) or _display_name(row_name)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def market_kind(instrument_id: str, name: str) -> str:
    """``curve`` (the 2s10s spread: steepened/flattened), ``spread`` (a credit spread:
    widened/narrowed) or ``price`` (prices, yields, FX, volatility: rose/fell)."""
    if instrument_id == SPREAD_ID:
        return "curve"
    if instrument_id.endswith("_OAS") or "spread" in name.lower():
        return "spread"
    return "price"


def markets(model: BriefModel, hidden: Collection[int]) -> dict[str, Market]:
    """Every market the note may pick: each visible mover, then each visible snapshot row, with
    the display strings the email shows for it. Only a mover carries its sigma (the ranking
    every surface reads). Nothing of a tier the hosted dashboard hides."""
    rows = {r.instrument_id: r for r in model.rows()}
    visible = {i for i, r in rows.items() if r.tier is not None and r.tier not in hidden}
    flagged = {i for c in model.caveats for i in c.instrument_ids}
    out: dict[str, Market] = {}
    for m in model.movers:
        if m.instrument_id in visible and m.instrument_id not in out:
            out[m.instrument_id] = Market(
                m.instrument_id,
                note_name(m.instrument_id, m.name),
                market_kind(m.instrument_id, m.name),
                m.change_text,
                m.value_text,
                f"{m.sigma:.1f}σ",
                m.instrument_id in flagged,
            )
    for g in model.snapshot:
        for r in g.rows:
            if r.instrument_id not in visible or r.instrument_id in out:
                continue
            row = rows[r.instrument_id]
            out[r.instrument_id] = Market(
                r.instrument_id,
                note_name(r.instrument_id, row.name),
                market_kind(r.instrument_id, row.name),
                r.change_text,
                r.value_text,
                None,  # sigma only for a ranked mover: a quiet move's sigma says nothing
                r.instrument_id in flagged,
            )
    return out


def allowed_contrasts(model: BriefModel, config: Config) -> frozenset[frozenset[str]]:
    """The pairs a contrast may name: ``brief.related_pairs`` and the pairs Check before using
    names."""
    pairs = {frozenset(p.pair) for p in config.brief.related_pairs}
    pairs |= {frozenset(c.instrument_ids) for c in model.caveats if len(c.instrument_ids) == 2}
    return frozenset(p for p in pairs if len(p) == 2)


def facts(model: BriefModel, config: Config, hidden: Collection[int]) -> dict[str, Any]:
    """The note's only input: the read, the rules met, Check before using (words only), the
    markets with their display strings, and the contrasts allowed. No hidden tier, no lineage."""
    table = markets(model, hidden)
    read = model.read
    return {
        "brief": to_et(model.report_time).strftime("%a %d %b %Y, %H:%M ET"),
        "read": read.text if read is not None else "",
        "rules_met": [r.label for r in read.met] if read is not None else [],
        "moved_more_than_usual": [
            m.instrument_id for m in model.movers if m.instrument_id in table
        ],
        "check_before_using": [
            {"item": c.title, "text": c.text}
            for c in model.caveats
            if all(i in table for i in c.instrument_ids)
        ],
        "markets": [
            {
                "market_id": k.instrument_id,
                "name": k.name,
                "change": k.change,
                "level": k.level,
                "sigma": k.sigma,
            }
            for k in table.values()
        ],
        "allowed_contrasts": sorted(
            sorted(p) for p in allowed_contrasts(model, config) if p <= table.keys()
        ),
    }


def schema(ids: Sequence[str]) -> dict[str, Any]:
    """The JSON schema the reply is held to: ids only, from today's markets."""
    market = {"type": "string", "enum": list(ids)}
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "market_id": market,
                        "show": {"type": "string", "enum": list(SHOWS)},
                    },
                    "required": ["market_id", "show"],
                    "additionalProperties": False,
                },
            },
            "contrasts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"a": market, "b": market},
                    "required": ["a", "b"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["items", "contrasts"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# validation and the templates
# ---------------------------------------------------------------------------

_VERBS = {
    "price": ("rose", "fell"),
    "spread": ("widened", "narrowed"),
    "curve": ("steepened", "flattened"),
}


def _moved(k: Market) -> str:
    """ "fell 1.6%" / "widened 27bp" / "was unchanged" (no sign after a verb)."""
    if k.sign == 0:
        return "was unchanged"
    up, down = _VERBS[k.kind]
    return f"{up if k.sign > 0 else down} {k.magnitude}"


def _sentence(k: Market, show: str) -> tuple[str, int]:
    """One market's sentence and the figures it shows."""
    n = 0 if k.sign == 0 else 1
    if show == "move_and_level":
        at = "at" if k.sign == 0 else "to"
        return _cap(f"{k.label} {_moved(k)} {at} {k.level}."), n + 1
    if show == "move_and_sigma":
        return _cap(f"{k.label} {_moved(k)}, a {k.sigma} day."), n + 1
    return _cap(f"{k.label} {_moved(k)}."), n


def _contrast(a: Market, b: Market) -> tuple[str, int]:
    n = (a.sign != 0) + (b.sign != 0)
    return _cap(f"{a.label} {_moved(a)} while {b.label} {_moved(b)}."), n


def _figures(k: Market, n: int) -> tuple[int, int]:
    """(figures, figures of a market not marked "check")."""
    return n, 0 if k.check else n


def _pick(table: Mapping[str, Market], obj: object, field: str) -> Market | None:
    """The market a reply field names, or None: the value is only ever a lookup key."""
    value = obj.get(field) if isinstance(obj, Mapping) else None
    return table.get(value) if isinstance(value, str) else None


@dataclass(frozen=True)
class Rendered:
    sentences: tuple[str, ...]
    n_numbers: int  # figures rendered
    n_checked: int  # of those, figures of markets not marked "check"
    dropped: tuple[str, ...]  # why each pick went (never a reply string)


def render(
    reply: object,
    table: Mapping[str, Market],
    contrasts: Collection[frozenset[str]],
    max_sentences: int,
) -> Rendered:
    """Validate a parsed reply against today's markets and render the kept picks. A reply value
    is only ever used as a key into ``table`` or ``SHOWS``; nothing of it is copied out."""
    dropped: list[str] = []
    if not isinstance(reply, Mapping):
        return Rendered((), 0, 0, ("the reply was not an object",))
    raw_items = reply.get("items")
    raw_pairs = reply.get("contrasts")
    items = list(raw_items) if isinstance(raw_items, list) else []
    pairs = list(raw_pairs) if isinstance(raw_pairs, list) else []
    if raw_items is not None and not isinstance(raw_items, list):
        dropped.append("items was not a list")
    if raw_pairs is not None and not isinstance(raw_pairs, list):
        dropped.append("contrasts was not a list")
    if len(items) > MAX_ITEMS:
        dropped.append(f"{len(items) - MAX_ITEMS} items over the limit of {MAX_ITEMS}")
    if len(pairs) > MAX_CONTRASTS:
        dropped.append(f"{len(pairs) - MAX_CONTRASTS} contrasts over the limit of {MAX_CONTRASTS}")

    kept_pairs: list[tuple[Market, Market]] = []
    for p in pairs[:MAX_CONTRASTS]:
        a, b = _pick(table, p, "a"), _pick(table, p, "b")
        if a is None or b is None:
            dropped.append("a contrast named a market not in today's brief")
            continue
        ids = frozenset((a.instrument_id, b.instrument_id))
        name = f"contrast {a.instrument_id}/{b.instrument_id}"
        if len(ids) < 2 or ids not in contrasts:
            dropped.append(f"{name} is not an allowed pair")
        elif not a.change or not b.change:
            dropped.append(f"{name} has no change to show")
        elif any(frozenset((x.instrument_id, y.instrument_id)) == ids for x, y in kept_pairs):
            dropped.append(f"{name} repeated")
        else:
            kept_pairs.append((a, b))
    in_pairs = {k.instrument_id for pair in kept_pairs for k in pair}

    out: list[tuple[str, int, int]] = []  # sentence, figures, figures not marked check
    seen: set[str] = set()
    for item in items[:MAX_ITEMS]:
        k = _pick(table, item, "market_id")
        show = item.get("show") if isinstance(item, Mapping) else None
        if k is None:
            dropped.append("an item named a market not in today's brief")
        elif not isinstance(show, str) or show not in SHOWS:
            dropped.append(f"{k.instrument_id}: unknown show")
        elif k.instrument_id in seen:
            dropped.append(f"{k.instrument_id}: repeated")
        elif k.instrument_id in in_pairs:
            dropped.append(f"{k.instrument_id}: already in a contrast")
        elif not k.change:
            dropped.append(f"{k.instrument_id}: no change to show")
        elif show == "move_and_level" and not k.level:
            dropped.append(f"{k.instrument_id}: no level to show")
        elif show == "move_and_sigma" and (k.sigma is None or k.sign == 0):
            dropped.append(f"{k.instrument_id}: sigma asked of a move that is not ranked")
        else:
            seen.add(k.instrument_id)
            text, n = _sentence(k, show)
            out.append((text, *_figures(k, n)))
    for a, b in kept_pairs:
        text, _ = _contrast(a, b)
        na, nb = (a.sign != 0), (b.sign != 0)
        out.append((text, na + nb, (0 if a.check else na) + (0 if b.check else nb)))
    if len(out) > max_sentences:
        dropped.append(f"{len(out) - max_sentences} sentences over the limit of {max_sentences}")
        out = out[:max_sentences]
    return Rendered(
        tuple(t for t, _, _ in out),
        sum(n for _, n, _ in out),
        sum(c for _, _, c in out),
        tuple(dropped),
    )


def hidden_tiers(config: Config) -> frozenset[int]:
    """Tiers the hosted dashboard hides (where the stored note is shown)."""
    return ALWAYS_HIDDEN | frozenset(config.dashboard.hosted_hidden_tiers)


def prompt(settings: NoteSettings) -> str:
    n = min(settings.max_sentences, MAX_ITEMS)
    return (
        "You choose what the morning note of Desk Brief, a markets data brief read by a "
        "hedge-fund desk, says. You write no prose: code renders every sentence from fixed "
        "templates with the brief's own figures. Using only the facts in the user message, "
        f"pick up to {n} markets worth a trader's attention this morning, most important "
        'first, and for each say what to show: "move", "move_and_level", or '
        '"move_and_sigma" (only for a market whose sigma is given). Optionally pick up to '
        f"{MAX_CONTRASTS} contrasts, only from allowed_contrasts, where two related markets "
        "moved differently. Prefer the markets that moved more than usual and those named in "
        "Check before using. Reply with the JSON object the schema asks for and nothing else."
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


#: where each API call's cost is recorded the moment it returns, one file per call, by the
#: ET month of the call (not the brief's date, so a backfill counts in the month it ran)
SPEND_PREFIX = f"{REPORTS_PREFIX}/note_spend"


def spend_key(at: datetime, call_id: str) -> str:
    return f"{SPEND_PREFIX}/month={to_et(at):%Y-%m}/{call_id}.json"


def record_spend(store: StorageBackend, at: datetime, note: Note) -> str:
    """Write one call's cost to the spend ledger (write-once); returns the key."""
    key = spend_key(at, new_id())
    body = {"cost_usd": note.cost_usd, "model": note.model, "at": ensure_utc(at).isoformat()}
    store.put_bytes(key, json.dumps(body, sort_keys=True).encode())
    return key


def month_spend(store: StorageBackend, at: date | datetime) -> float:
    """USD spent on notes in ``at``'s calendar month (ET): the sum of the ledger files of that
    month. A file that cannot be parsed counts as zero."""
    month = f"{to_et(at):%Y-%m}" if isinstance(at, datetime) else f"{at:%Y-%m}"
    total = 0.0
    for key in store.list(f"{SPEND_PREFIX}/month={month}/"):
        if not key.endswith(".json"):
            continue
        try:
            total += float(json.loads(store.get_bytes(key)).get("cost_usd") or 0.0)
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
    """The note for ``model``: one API call at most. ``spent_usd`` is this month's spend so far
    (:func:`month_spend`). :func:`with_note` guards it, so nothing here can cost the brief."""
    s = config.brief.note
    log = get_logger()
    if not s.enabled:
        log.info("note.skipped", reason=NOTE_OFF)
        return Note(reason=NOTE_OFF, model=s.model)
    key = (env.get(s.api_key_env) or "").strip()
    if not key:
        log.info("note.skipped", reason=NOTE_NO_KEY)
        return Note(reason=NOTE_NO_KEY, model=s.model)
    hidden = hidden_tiers(config)
    table = markets(model, hidden)
    if not table:
        return Note(reason="there is no market in today's brief to describe", model=s.model)
    pairs = allowed_contrasts(model, config)
    payload = facts(model, config, hidden)
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
            output_config={
                "effort": s.effort,
                "format": {"type": "json_schema", "schema": schema(sorted(table))},
            },
        )
    except Exception as exc:  # the key is never logged; only the error's type
        kind = type(exc).__name__
        timed_out = isinstance(exc, TimeoutError) or "Timeout" in kind
        log.warning("note.failed", error=kind)
        if timed_out:  # the request may still be billed: count the worst case
            return Note(reason="the API timed out", model=s.model, cost_usd=worst)
        return Note(reason=f"the API returned an error ({kind})", model=s.model)
    # the call's cost first, from the reply's usage, so nothing after it can lose it
    try:
        cost = _cost(s, *_usage(response))
    except Exception:  # an unreadable usage block: count the worst case
        cost = worst
    try:
        return _read_reply(response, s, table, pairs, cost)
    except Exception as exc:
        log.warning("note.unexpected", error=type(exc).__name__)
        reason = f"the note could not be written ({type(exc).__name__})"
        return Note(reason=reason, model=s.model, cost_usd=cost)


def _read_reply(
    response: Any,
    s: NoteSettings,
    table: Mapping[str, Market],
    pairs: Collection[frozenset[str]],
    cost: float,
) -> Note:
    log = get_logger()
    stop = getattr(response, "stop_reason", "")
    if stop == "refusal":
        return Note(reason="the model declined to write it", model=s.model, cost_usd=cost)
    if stop == "max_tokens":  # config.NoteSettings: a reply cut at the ceiling is no note
        return Note(reason="the reply stopped at the token limit", model=s.model, cost_usd=cost)
    text = _reply_text(response).strip()
    if not text:
        return Note(reason="the reply was empty", model=s.model, cost_usd=cost)
    try:
        reply = json.loads(text)
    except ValueError:
        log.info("note.malformed")
        return Note(reason="the reply was not the structured output asked for", model=s.model,
                    cost_usd=cost)  # fmt: skip
    result = render(reply, table, pairs, s.max_sentences)
    for why in result.dropped:
        log.info("note.pick_dropped", reason=why)
    if not result.sentences:
        return Note(reason="no pick in the reply was valid", dropped=result.dropped,
                    model=s.model, cost_usd=cost)  # fmt: skip
    return Note(
        sentences=result.sentences,
        n_checked=result.n_checked,
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
    now: datetime | None = None,
) -> BriefModel:
    """``model`` with its note; never raises (the morning report step calls this once). A call's
    cost goes to the spend ledger the moment it returns, before anything else can fail."""
    at = ensure_utc(now) if now is not None else utc_now()
    log = get_logger()
    try:
        spent = month_spend(store, at)
    except Exception as exc:  # cannot tell the spend: do not call
        log.warning("note.spend_unknown", error=type(exc).__name__)
        return replace(model, note=Note(reason="this month's spend could not be read"))
    try:
        note = write_note(model, config, env, spent_usd=spent, client_factory=client_factory)
    except Exception as exc:  # an unexpected bug costs the note, never the brief or its email
        log.warning("note.unexpected", error=type(exc).__name__)
        note = Note(reason=f"the note could not be written ({type(exc).__name__})")
    if note.cost_usd > 0:
        try:
            record_spend(store, at, note)
        except Exception as exc:
            log.warning("note.spend_not_recorded", error=type(exc).__name__)
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
    return note_of(json.loads(store.get_bytes(max(versions)[1])))


def note_of(doc: object) -> Note | None:
    """The note held by an archived brief's parsed ``model.json``; None when it holds none."""
    if not isinstance(doc, Mapping):
        return None
    raw = doc.get("note")
    if not isinstance(raw, Mapping):
        return None
    written = doc.get("report_time")
    return Note(
        sentences=tuple(str(x) for x in raw.get("sentences") or ()),
        n_checked=int(raw.get("n_checked") or 0),
        n_numbers=int(raw.get("n_numbers") or 0),
        reason=raw.get("reason"),
        model=str(raw.get("model") or ""),
        cost_usd=float(raw.get("cost_usd") or 0.0),
        written_at=ensure_utc(datetime.fromisoformat(written)) if written else None,
    )


__all__ = [
    "MAX_CONTRASTS",
    "MAX_ITEMS",
    "NOTE_NAMES",
    "Market",
    "Rendered",
    "allowed_contrasts",
    "facts",
    "market_kind",
    "markets",
    "month_spend",
    "note_name",
    "note_of",
    "record_spend",
    "render",
    "schema",
    "stored_note",
    "with_note",
    "write_note",
]
