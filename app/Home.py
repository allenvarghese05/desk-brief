"""Desk Brief dashboard: Overview (spec 10.3, page 1).

Run locally: ``DESKBRIEF_DISPLAY_MODE=local streamlit run app/Home.py``. Hosted (Streamlit
Community Cloud) leaves the variable unset, which means ``hosted``: tier-1 rows are never
rendered. Reads only; see ``app/components/data.py`` for the read-only R2 connection and the
adapter that T23 will switch to the serving files.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src"):  # `app` is a plain folder; src/ covers a non-installed checkout
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app import theme  # noqa: E402
from app.components import data, panels  # noqa: E402

from deskbrief.config import Config, load_config  # noqa: E402
from deskbrief.store.backend import StorageError  # noqa: E402


@st.cache_resource
def _config(path: str) -> Config:
    return load_config(path)


def main() -> None:
    st.set_page_config(page_title="Desk Brief", layout="wide")
    theme.apply()
    path = os.environ.get("DESKBRIEF_CONFIG") or str(ROOT / "config.yaml")
    config = _config(path)
    mode = data.resolve_mode(os.environ.get(config.dashboard.mode_env))
    try:
        snap = data.load_snapshot(config)
    except StorageError as exc:  # message names missing variables, never their values
        st.error(f"Cannot read the data store: {exc}")
        return
    ov = data.overview(snap, mode, data.hidden_tiers(mode, config))
    panels.top_bar(ov)
    panels.headline_strip(ov)
    for section in ov.sections:
        with st.expander(section.title, expanded=True):
            panels.section_table(section, ov.as_of)
    if ov.withheld:
        st.caption(
            f"{ov.withheld} instrument(s) withheld in {ov.mode} mode: live (tier 1) data is "
            "not shown here until provider display terms are verified."
        )
    panels.provenance_drawer(ov)
    with st.expander("Known limits"):
        for limit in ov.known_limits:
            st.markdown(f"- {limit.text}")


main()
