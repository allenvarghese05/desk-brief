"""Collect ``inj_*.py`` fault-injection modules (spec §13) and share the pipeline fakes.

pytest only collects ``test_*.py`` by default; fault-injection files are prefixed ``inj_``
so they are recognisable at a glance, hence this hook. Other cards' ``inj_*`` files in this
directory are collected the same way.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "pipeline"))


def pytest_collect_file(parent: pytest.Collector, file_path: Path) -> Any:
    if file_path.suffix == ".py" and file_path.name.startswith("inj_"):
        return pytest.Module.from_parent(parent, path=file_path)
    return None
