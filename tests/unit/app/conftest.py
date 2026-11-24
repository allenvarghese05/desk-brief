"""Fixtures for the dashboard tests.

SYNTHETIC FIXTURE: the store is built from ``brief_synth`` (made-up values, labelled there).
No real R2 and no network: the local backend only.
"""

from __future__ import annotations

import itertools
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import brief_synth
from brief_synth import MON_0700, World, monday_rows

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))  # `app` is a plain folder, not an installed package
HOME = REPO / "app" / "Home.py"

#: env vars the dashboard reads; cleared so a developer's shell cannot leak into a test
_ENV = (
    "DESKBRIEF_DISPLAY_MODE",
    "DESKBRIEF_STORAGE_BACKEND",
    "DESKBRIEF_STORAGE_BUCKET",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_READER_ACCESS_KEY_ID",
    "R2_READER_SECRET_ACCESS_KEY",
)


@pytest.fixture(autouse=True)
def _close_new_connections() -> Iterator[None]:
    """Close the shared DuckDB connections a test's stores opened (T56 keeps one per store for
    the process, with native threads), so the whole folder can run in one process (T58: the
    Home tests open many stores)."""
    from app.components import data

    before = set(data._SLOTS)
    yield
    for key in set(data._SLOTS) - before:
        slot = data._SLOTS.pop(key)
        if slot.current is not None and not slot.busy:
            slot.current.conn.close()


@pytest.fixture
def run_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., AppTest]:
    """``run_home(mode, rows=..., backend=...)`` runs Home.py against a synthetic local store."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    st.cache_data.clear()
    runs = itertools.count()

    def run(
        mode: str | None = "hosted",
        rows: list | None = None,  # type: ignore[type-arg]
        extra: Callable[[World], None] | None = None,
    ) -> AppTest:
        world = World(tmp_path / f"w{next(runs)}")
        world.observations(monday_rows() if rows is None else rows)
        if extra is not None:  # more tables: cross-checks, data checks, ...
            extra(world)
        world.dq(
            brief_synth.MON, [("fresh_a", "pass", None, None), ("fresh_b", "pass", None, None)]
        )
        world.run(brief_synth.MON)
        text = (REPO / "config.yaml").read_text(encoding="utf-8")
        text = text.replace("local_root: ./.data", f"local_root: {world.store.root}")
        cfg = world.store.root.parent / "config.yaml"
        cfg.write_text(text, encoding="utf-8")
        monkeypatch.setenv("DESKBRIEF_CONFIG", str(cfg))
        if mode is not None:
            monkeypatch.setenv("DESKBRIEF_DISPLAY_MODE", mode)
        from app.components import data

        monkeypatch.setattr(data, "current_time", lambda: MON_0700)
        return AppTest.from_file(str(HOME), default_timeout=60).run()

    return run


# --- T60: the flat-CSS checker (P-023: no containers) ------------------------------------------
#
# Default-deny: every rule, whatever its selector, fails when it sets a non-transparent
# background, a border other than one hairline top or bottom rule, a non-zero radius or a
# shadow. The only way through is a selector on FLAT_ALLOW, which holds P-023's exempt elements
# (each pattern is matched against the whole selector, so a container cannot borrow an exempt
# element's name: ``.db-kpi .db-tip`` styles a tooltip, ``.db-kpi`` is still a container).

_PICK_RE = r'div\[class\*="st-key-db-pick-[\w-]*"\]'
#: P-023's exempt elements, as full-selector regular expressions (checked against the
#: Streamlit 1.67 DOM: react-aria tabs and segmented controls)
FLAT_ALLOW: tuple[tuple[str, str], ...] = (
    ("hero change pill", r"\.db-pill(-(up|down|flat))?"),
    ("selected watchlist row", r"\.db-wl-row\.is-sel(::before)?"),
    ("hover highlight of a clickable row", _PICK_RE + r":hover \.db-(wl-row|mx-row)"),
    ("tooltips, popovers and dialogs", r"\.db-tip|\.db-tip\.db-wl-tip"),
    ("search field", r'div\[data-testid="stTextInput"\] input'),
    (
        "selected segment of a segmented control",
        r'\[data-testid="stButtonGroup"\] button\[aria-checked="true"\]',
    ),
    ("tier glyph", r"\.db-prov"),
    ("sigma tag", r"\.db-sig"),
    ("status dots", r"\.db-dot(-(ok|warn|fail|unknown|muted))?"),
    ("check markers and stale tags", r"\.db-mark(-(fail|warn|pass|stale|skip))?"),
    ("check squares", r"\.db-sq"),
    ("52-week range track and marker", r"\.db-range( span| \.db-range-fill)?"),
    ("selected-tab underline", r'\[data-testid="stTabs"\] \.react-aria-SelectionIndicator'),
    ("hairline under the tab row", r'\[data-testid="stTabs"\] \[role="tablist"\]::after'),
    (
        "keyboard focus indicators",
        r"\.db-(prov|sq):focus-visible|" + _PICK_RE + r":has\(button:focus-visible\)",
    ),
    ("opaque app header in the page colour", r'header\[data-testid="stHeader"\]'),
)

_NONE = {"0", "0px", "none", "transparent", "inherit", "initial", "unset", "rgba(0,0,0,0)"}
#: border properties that are not drawing: table layout only
_LAYOUT = {"border-collapse", "border-spacing"}


def _rules(css: str) -> list[tuple[str, str]]:
    """``(selector, declarations)`` of every style rule, inside ``@media`` blocks too."""
    css = re.sub(r"</?style>", "", css)
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out: list[tuple[str, str]] = []
    i, n = 0, len(css)
    while i < n:
        j = css.find("{", i)
        if j < 0:
            break
        head = css[i:j].strip()
        if head.startswith("@"):  # a block of rules: parse its body
            depth, k = 1, j + 1
            while k < n and depth:
                depth += {"{": 1, "}": -1}.get(css[k], 0)
                k += 1
            out += _rules(css[j + 1 : k - 1])
            i = k
            continue
        k = css.find("}", j)
        out.append((" ".join(head.split()), css[j + 1 : k]))
        i = k + 1
    return out


def _decls(body: str) -> list[tuple[str, str]]:
    out = []
    for d in body.split(";"):
        if ":" in d:
            prop, val = d.split(":", 1)
            val = re.sub(r"\s*!important\s*$", "", val.strip()).strip().lower()
            out.append((prop.strip().lower(), " ".join(val.split())))
    return out


def _zero(val: str) -> bool:
    return all(re.fullmatch(r"0(\.0+)?(px|em|rem|%)?", v) for v in val.split())


def _off(val: str) -> bool:
    return val.replace(" ", "") in _NONE or _zero(val)


def _problems(decls: list[tuple[str, str]]) -> list[str]:
    """What draws a container in one rule: any background, any shadow (prefixed ones too), any
    outline, any radius, and any border except one 1 px top or bottom hairline. Edges count
    whether set by the shorthand or by its longhands."""
    bad: list[str] = []
    edges: set[str] = set()
    for prop, val in decls:
        bare = re.sub(r"^-(webkit|moz|ms|o)-", "", prop)
        drawn = bare in ("background", "background-color", "background-image", "box-shadow")
        drawn |= bare == "text-shadow" or (bare == "filter" and "shadow" in val)
        if drawn:
            if not _off(val):
                bad.append(f"{prop}: {val}")
        elif bare.startswith("outline"):
            if bare != "outline-offset" and not _off(val):
                bad.append(f"{prop}: {val}")
        elif bare.startswith("border") and bare not in _LAYOUT:
            if bare.endswith("radius"):
                if not _zero(val):
                    bad.append(f"{prop}: {val}")
                continue
            m = re.fullmatch(r"border-(top|bottom)(-(width|style|color))?", bare)
            if m is None:  # border, border-left/right, -width/-style/-color, -inline, -block, ...
                if not _off(val):
                    bad.append(f"{prop}: {val}")
                continue
            edge, part = m.group(1), m.group(3)
            if part is None:  # the shorthand: one solid 1 px line, or nothing
                if _off(val):
                    continue
                if not re.fullmatch(r"1px solid \S+", val):
                    bad.append(f"{prop}: {val}")
                edges.add(edge)
            elif part == "width":
                if _off(val):
                    continue
                if val != "1px":
                    bad.append(f"{prop}: {val}")
                edges.add(edge)
            elif part == "style":
                if _off(val):
                    continue
                if val != "solid":
                    bad.append(f"{prop}: {val}")
                edges.add(edge)
    if len(edges) > 1:
        bad.append("more than one hairline rule (" + ", ".join(sorted(edges)) + ")")
    return bad


def _allowed(selector: str, allow: tuple[tuple[str, str], ...]) -> bool:
    return any(re.fullmatch(pattern, selector) for _, pattern in allow)


def flat_css_violations(css: str, allow: tuple[tuple[str, str], ...] = FLAT_ALLOW) -> list[str]:
    """Every rule of ``css`` that draws a container (see the block comment above), as
    ``"selector -> declaration"`` lines; empty when the stylesheet is flat."""
    out = []
    for selector, body in _rules(css):
        problems = _problems(_decls(body))
        if not problems:
            continue
        denied = [s.strip() for s in selector.split(",") if not _allowed(s.strip(), allow)]
        if denied:
            out += [f"{', '.join(denied)} -> {p}" for p in problems]
    return out


def assert_flat(css: str, allow: tuple[tuple[str, str], ...] = FLAT_ALLOW) -> None:
    bad = flat_css_violations(css, allow)
    assert not bad, "P-023: block containers must be flat:\n" + "\n".join(bad)


@pytest.fixture
def flat_css() -> Callable[[str], list[str]]:
    """The checker as a fixture, for test modules that cannot import this conftest by name."""
    return flat_css_violations
