"""Desk Brief dashboard: the router (T57, P-022).

Every page is an ``st.Page`` under ``st.navigation(position="top")``, so moving between pages,
and selecting an instrument (``st.query_params``), happens inside one Streamlit session with no
full browser reload. The URL paths are the ones the old multipage app used (``/``, ``/Macro``,
``/Cross_checks``, ...), so bookmarks and ``?id=`` deep links keep working. The page list sits
in the top bar; the sidebar page list and its toggle are hidden.

Run locally: ``DESKBRIEF_DISPLAY_MODE=local streamlit run app/Home.py``. Hosted (Streamlit
Community Cloud) leaves the variable unset, which means ``hosted``: tier-1 rows are never
rendered.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src"):  # `app` is a plain folder; src/ covers a non-installed checkout
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

APP = Path(__file__).resolve().parent

#: (file under app/, title in the top bar, URL path); the first is the default page
PAGES = (
    ("views/markets.py", "Markets", ""),
    ("pages/2_Macro.py", "Macro", "Macro"),
    ("pages/3_Cross_checks.py", "Cross checks", "Cross_checks"),
    ("pages/4_Data_health.py", "Data health", "Data_health"),
    ("pages/5_Sources.py", "Sources", "Sources"),
    ("pages/6_Runs.py", "Runs", "Runs"),
)


def pages() -> list[st.Page]:
    out = []
    for i, (file, title, url) in enumerate(PAGES):
        if i == 0:
            out.append(st.Page(APP / file, title=title, default=True))
        else:
            out.append(st.Page(APP / file, title=title, url_path=url))
    return out


def main() -> None:
    st.set_page_config(page_title="Desk Brief", layout="wide", initial_sidebar_state="collapsed")
    st.navigation(pages(), position="top").run()


if __name__ == "__main__":  # Streamlit runs the script as __main__; tests import PAGES
    main()
